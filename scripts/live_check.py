#!/usr/bin/env python3
# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Live check of every registered gas tariff card.

Fetches every registered (supplier, contract, region) from the supplier's own
publication with the extractor the integration uses, retrying a failure that
says nothing about the card, and checks what came back:

  - the card is for the current month;
  - every supplier's index publication (its ``fetch_index``) reads, lists
    every index its cards are priced on, and has a recent value for each;
  - the regulated figures agree across the fleet: per DSO the T1 and T2
    terms, transport and metering, and the federal excise, the energy
    contribution, the Walloon connection fee and the Brussels per-meter levy.
    The regulators and the law set these for everyone, so a card printing
    something the other suppliers' cards of the same month do not is stale or
    misread, and the integration bills what the card prints. Where it bills
    the law or the regulated figure instead (the federal levies and the Walloon
    connection fee of a month const.py knows, a Fluvius data management fee
    the card leaves out), the departure is a notice rather than a failure.

A card published as page images is parsed on the card archive's OCR reading
of its bytes (``--texts``); one the archive has not read yet is a notice.

Prints a markdown report to stdout and writes the labels of the failures to
``--fingerprint``, which the workflow hands to file_ci_issue.sh so the same
failures are filed once. Exits 0 when everything passed, 1 on a failure that
is not transient, 2 when every failure is transient (a timeout, a 5xx, 403,
408 or 429 on every attempt), which is reported and never filed, and 3 when
the check itself crashed.

``--texts DIR`` reads the gas card archive (the gas/ directory of a checkout of
be_price_cards): a card whose bytes it already holds is served its stored
text instead of being rendered again, which is most of a run's time.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import re
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# scripts/ is not a package, so it is put on sys.path above rather than
# imported by dotted path; mypy cannot follow that.
from card_texts import StoredTexts  # type: ignore[import-not-found]  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.be_gas_prices.const import (  # noqa: E402
    ENERGY_CONTRIBUTION_ZEROED_FROM,
    FLUVIUS_DATA_MANAGEMENT_KNOWN_FROM,
    FLUVIUS_DATA_MANAGEMENT_KNOWN_UNTIL,
    FLUVIUS_KEYS,
    GAS_EXCISE_KNOWN_FROM,
    GAS_EXCISE_KNOWN_UNTIL,
    REGION_BRUSSELS,
    REGION_WALLONIA,
    SUPPLIER_CUSTOM,
    TIER_T1,
    TIER_T2,
    WALLOON_CONNECTION_FEE_KNOWN_FROM,
    WALLOON_CONNECTION_FEE_KNOWN_UNTIL,
)
from custom_components.be_gas_prices.providers import all_extractors  # noqa: E402
from custom_components.be_gas_prices.providers._pdf import (  # noqa: E402
    is_transient_fetch_error,
    memoise_text_fetches,
    render_through,
)
from custom_components.be_gas_prices.providers._rates import IndexedRates  # noqa: E402
from custom_components.be_gas_prices.providers.base import (  # noqa: E402
    CardNotReadableError,
    SupplierExtractor,
    SupplierSnapshot,
)

BRUSSELS = ZoneInfo("Europe/Brussels")

_ATTEMPTS = 3
_BACKOFF_S = (10.0, 30.0)
# One fetch, download and parse together. The readers bound each download on
# their own; this bounds a parse that never returns, so the rest of the fleet
# is still checked and the report still prints.
_ATTEMPT_TIMEOUT_S = 300.0
# Days into a month during which a card still labelled for the previous month
# is a supplier publishing a little late rather than a stale card.
_GRACE_DAYS = 5
# How far behind the current month the newest value of an index a card is
# priced on may be. A monthly index is published in the first days after its
# month, so early in a month the newest value can be two months back; a
# quarterly one after its quarter, whose last month is then four months back.
_INDEX_MAX_LAG = {"month": 2, "quarter": 4}

Status = Literal["ok", "fail", "transient", "notice"]


@dataclass(frozen=True)
class Check:
    """One line of the report. The label names what was checked and never
    carries a measurement, since the failing labels are what an issue is
    fingerprinted on; the figures go in the detail."""

    label: str
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class Card:
    """A card that fetched and parsed, as the fleet checks need it."""

    extractor: SupplierExtractor
    contract: str
    region: str
    snapshot: SupplierSnapshot


def is_transient(err: BaseException) -> bool:
    """Whether a failure says nothing about the card: a timeout, or what the
    readers wrap a network error or a 5xx, 403, 408 or 429 status in."""
    return isinstance(err, TimeoutError) or is_transient_fetch_error(str(err))


async def fetch_with_retry[T](
    factory: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> T:
    """``factory()`` awaited, again after a pause when it failed transiently.

    A fresh awaitable per attempt, since one can only be awaited once. A
    failure that is not transient (a parse error, a 404) is raised at once:
    retrying it only delays the report.
    """
    for attempt in range(_ATTEMPTS):
        try:
            return await asyncio.wait_for(factory(), _ATTEMPT_TIMEOUT_S)
        except Exception as err:
            if attempt == _ATTEMPTS - 1 or not is_transient(err):
                raise
            await sleep(_BACKOFF_S[attempt])
    raise AssertionError("unreachable")


def label_month(label: str) -> tuple[int, int] | None:
    """(year, month) of a card's publication label, which every gas
    extractor writes as "YYYY-MM", or None for anything else."""
    match = re.fullmatch(r"(\d{4})-(\d{2})", label)
    if match is None or not 1 <= int(match.group(2)) <= 12:
        return None
    return int(match.group(1)), int(match.group(2))


def _lag(today: date, month: tuple[int, int]) -> int:
    """How many months ``month`` is before today's."""
    return (today.year - month[0]) * 12 + today.month - month[1]


def targets(
    extractors: Iterable[SupplierExtractor],
    today: date,
    only: frozenset[str] = frozenset(),
) -> list[tuple[SupplierExtractor, str, str]]:
    """Every (extractor, contract, region) with a card to fetch today.

    The custom supplier is typed in by the household and has no card, and a
    supplier past its withdrawal date has left the market: its last card
    stays up and stays stale, which is not worth a daily failure.
    """
    out: list[tuple[SupplierExtractor, str, str]] = []
    for ex in extractors:
        if ex.id == SUPPLIER_CUSTOM or (only and ex.id not in only):
            continue
        if ex.deprecated_until is not None and today > ex.deprecated_until:
            continue
        for contract in ex.contracts:
            out.extend((ex, contract.id, region) for region in sorted(contract.regions))
    return out


def _failure(label: str, err: BaseException) -> Check:
    detail = f"{type(err).__name__}: {err}"
    if isinstance(err, CardNotReadableError):
        # A card published as page images, which only the card archive's
        # OCR reading of these very bytes lets the check parse. Until the
        # archive has read them no change here can: reported, never filed.
        return Check(label, "notice", detail)
    return Check(label, "transient" if is_transient(err) else "fail", detail)


def _freshness(label: str, snapshot: SupplierSnapshot, today: date) -> Check:
    """Whether the card is for the current month. A card for a later month
    is a supplier publishing early, which is fine."""
    name = f"{label}: card month"
    month = label_month(snapshot.publication_label)
    if month is None:
        return Check(name, "fail", f"unreadable publication label {snapshot.publication_label!r}")
    lag = _lag(today, month)
    allowed = 1 if today.day <= _GRACE_DAYS else 0
    if lag > allowed:
        return Check(
            name,
            "fail",
            f"the card is for {snapshot.publication_label}, {lag} month(s) before {today}",
        )
    return Check(name, "ok", snapshot.publication_label)


def _index_checks(
    ex: SupplierExtractor, table: dict[str, dict[str, float]], cards: list[Card], today: date
) -> Iterator[Check]:
    """The index publication against the cards priced on it.

    Only the indices a card names are held to a recent value: a publication
    also lists indices no product uses any more, and their last value can be
    years old without anything being wrong.
    """
    if not any(table.values()):
        yield Check(f"{ex.id}: index publication", "fail", "the publication lists no value")
        return
    yield Check(f"{ex.id}: index publication", "ok", ", ".join(sorted(table)))
    named = sorted(
        {
            (card.snapshot.energy.index, card.snapshot.energy.period)
            for card in cards
            if isinstance(card.snapshot.energy, IndexedRates)
        }
    )
    for index, period in named:
        label = f"{ex.id}: index {index}"
        series = table.get(index)
        if not series:
            yield Check(
                label,
                "fail",
                f"the cards are priced on {index}, which the publication does not list",
            )
            continue
        newest = max(series)
        month = label_month(newest)
        if month is None:
            yield Check(label, "fail", f"unreadable month {newest!r}")
            continue
        allowed = _INDEX_MAX_LAG[period]
        if _lag(today, month) > allowed:
            yield Check(
                label,
                "fail",
                f"the newest value is for {newest}, more than {allowed} months before {today}",
            )
            continue
        yield Check(label, "ok", f"{newest}: {series[newest]:g} EUR/MWh")


async def _check_supplier(
    session: aiohttp.ClientSession,
    ex: SupplierExtractor,
    wanted: list[tuple[str, str]],
    today: date,
    sleep: Callable[[float], Awaitable[Any]],
) -> tuple[list[Check], list[Card]]:
    """One supplier's cards in turn, then its index publication."""
    checks: list[Check] = []
    cards: list[Card] = []
    for contract, region in wanted:
        label = f"{ex.id}/{contract}/{region}"
        try:
            snapshot = await fetch_with_retry(
                functools.partial(ex.fetch, session, contract, region), sleep=sleep
            )
        except Exception as err:  # one card must not stop the check
            checks.append(_failure(f"{label}: fetch", err))
            continue
        checks.append(Check(f"{label}: fetch", "ok"))
        checks.append(_freshness(label, snapshot, today))
        cards.append(Card(ex, contract, region, snapshot))
    if ex.fetch_index is not None:
        try:
            table = await fetch_with_retry(functools.partial(ex.fetch_index, session), sleep=sleep)
        except Exception as err:  # reported like a card
            checks.append(_failure(f"{ex.id}: index publication", err))
        else:
            checks.extend(_index_checks(ex, table, cards, today))
    return checks, cards


# ---- the regulated figures across the fleet --------------------------------

_EXCISE_LOW = "excise up to 12000 kWh"
_EXCISE_HIGH = "excise above 12000 kWh"
_CONTRIBUTION = "energy contribution"
_CONNECTION_FEE = "connection fee"
# The levies the integration bills from the law rather than from the card for
# the months const.py knows the law for (providers/_resolve.py): the federal
# ones and the Walloon connection fee. A card printing another figure for such
# a month is still wrong and reported, but nobody is billed on it, so it does
# not fail the check.
_LAW_WINDOWS: dict[str, tuple[tuple[int, int], tuple[int, int] | None]] = {
    _EXCISE_LOW: (GAS_EXCISE_KNOWN_FROM, GAS_EXCISE_KNOWN_UNTIL),
    _EXCISE_HIGH: (GAS_EXCISE_KNOWN_FROM, GAS_EXCISE_KNOWN_UNTIL),
    _CONTRIBUTION: (ENERGY_CONTRIBUTION_ZEROED_FROM, None),
    _CONNECTION_FEE: (WALLOON_CONNECTION_FEE_KNOWN_FROM, WALLOON_CONNECTION_FEE_KNOWN_UNTIL),
}


@dataclass(frozen=True)
class Figure:
    """One regulated figure as one card prints it.

    ``scope`` is what the figure belongs to: a DSO key for the network terms,
    "federal" for the excise and the energy contribution, the region for a
    regional levy.
    """

    month: str
    scope: str
    name: str
    unit: str
    value: float
    supplier: str
    voter: str
    card: str


def voter(ex: SupplierExtractor) -> str:
    """Whom a card's figures count for: the module that reads it.

    Several brands can sell on one platform's card template, read by one
    module (six on Energy Together's): their cards are one reading of the
    regulated figures printed six times, and counted six times they would
    outvote every other supplier on whatever that template gets wrong.
    """
    fetch: Any = ex.fetch
    while isinstance(fetch, functools.partial):
        fetch = fetch.func
    return str(getattr(fetch, "__module__", ex.id))


def figures(card: Card) -> Iterator[Figure]:
    """Every regulated figure the card prints.

    A card priced excluding VAT (a professional one) is on another basis, and
    on another excise scale, so it is left out rather than converted.
    """
    snapshot = card.snapshot
    if snapshot.taxes.vat_rate != 0.0 or label_month(snapshot.publication_label) is None:
        return
    rows: list[tuple[str, str, str, float]] = []
    for dso, overlay in snapshot.dsos.items():
        for tier in (TIER_T1, TIER_T2):
            terms = overlay.tiers.get(tier)
            if terms is not None:
                rows.append((dso, f"{tier.upper()} fixed", "EUR/year", terms.fixed_per_year))
                rows.append((dso, f"{tier.upper()} proportional", "EUR/kWh", terms.proportional))
        rows.append((dso, "transport", "EUR/kWh", overlay.transport))
        rows.append((dso, "metering", "EUR/year", overlay.metering_per_year))
    taxes = snapshot.taxes
    # A card printing one excise rate holds one open band, which then stands
    # for both slices.
    rows.append(("federal", _EXCISE_LOW, "EUR/kWh", taxes.excise_bands[0][1]))
    rows.append(("federal", _EXCISE_HIGH, "EUR/kWh", taxes.excise_bands[-1][1]))
    rows.append(("federal", _CONTRIBUTION, "EUR/kWh", taxes.energy_contribution))
    if card.region == REGION_WALLONIA:
        rows.append((REGION_WALLONIA, _CONNECTION_FEE, "EUR/kWh", taxes.connection_fee))
    if card.region == REGION_BRUSSELS:
        for caliber, amount in (taxes.osp_by_caliber or {}).items():
            rows.append((REGION_BRUSSELS, f"levy {caliber}", "EUR/year", amount))
    for scope, name, unit, value in rows:
        yield Figure(
            month=snapshot.publication_label,
            scope=scope,
            name=name,
            unit=unit,
            value=value,
            supplier=card.extractor.id,
            voter=voter(card.extractor),
            card=f"{card.contract}/{card.region}",
        )


# The coarsest digit a card prints a regulated figure to: the second decimal
# of a c EUR/kWh and of a euro a year. A figure read back coarser than that
# lost its trailing zeros, not its digits: EnergyVision's "2,20" reads as
# 0.022, and taken at face value it would agree with Bolt's 2,115 and hide it.
_COARSEST = {"EUR/kWh": 1e-4, "EUR/year": 1e-2}


def _step(value: float, unit: str) -> float:
    """One unit of the last digit ``value`` carries. The cards print the
    regulators' figures to as few as two decimals, and a card rounding or
    truncating a figure is still printing that figure. Zero is exact."""
    if value == 0.0:
        return 0.0
    for places in range(12):
        if abs(round(value, places) - value) < 1e-12:
            return min(10.0**-places, _COARSEST[unit])
    return 1e-12


def shown(value: float, unit: str) -> str:
    """A figure the way the cards print it: per kWh in c EUR, per year in EUR."""
    if unit == "EUR/kWh":
        return f"{round(value * 100, 7):.10g} c EUR/kWh"
    return f"{round(value, 5):.10g} {unit}"


def agree(a: float, b: float, unit: str) -> bool:
    """Whether two printed figures are the same figure: they differ by no
    more than one unit of the last digit the less precise one carries."""
    return abs(a - b) <= max(_step(a, unit), _step(b, unit)) + 1e-12


def _billed_from_law(name: str, month: str) -> bool:
    window = _LAW_WINDOWS.get(name)
    parsed = label_month(month)
    if window is None or parsed is None:
        return False
    start, end = window
    return parsed >= start and (end is None or parsed < end)


def _filled_from_regulation(figure: Figure) -> bool:
    """A Fluvius row that prints no data management fee, in the tariff year
    const.py knows the fee for: the integration bills the regulated one
    (providers/_resolve.py), so the card is wrong but nobody is billed on it."""
    parsed = label_month(figure.month)
    return (
        figure.name == "metering"
        and figure.value == 0.0
        and figure.scope in FLUVIUS_KEYS
        and parsed is not None
        and FLUVIUS_DATA_MANAGEMENT_KNOWN_FROM <= parsed < FLUVIUS_DATA_MANAGEMENT_KNOWN_UNTIL
    )


def _departures(group: list[Figure]) -> Iterator[Check]:
    """The cards of one figure, month and scope that depart from the rest.

    The figure most voters agree with is the consensus, the most precise one
    among equals so the report shows the regulator's digits. A disagreement
    with no majority is reported as a notice: two voters against two is not a
    verdict on either.
    """
    first = group[0]
    unit = first.unit
    what = f"{first.scope} {first.name} for {first.month}"
    values = sorted({f.value for f in group})
    support = {v: {f.voter for f in group if agree(f.value, v, unit)} for v in values}
    best = max(values, key=lambda v: (len(support[v]), -_step(v, unit), v))
    rivals = [v for v in values if not agree(v, best, unit)]
    if not rivals:
        return
    agreeing = sorted({f.supplier for f in group if agree(f.value, best, unit)})
    if len(support[best]) < 2 or any(len(support[v]) >= len(support[best]) for v in rivals):
        printed = "; ".join(
            f"{shown(v, unit)} on {', '.join(sorted({f.supplier for f in group if f.value == v}))}"
            for v in values
        )
        yield Check(f"fleet: {what}", "notice", f"no majority: {printed}")
        return
    from_law = _billed_from_law(first.name, first.month)
    departing: dict[str, list[Figure]] = {}
    for f in group:
        if not agree(f.value, best, unit):
            departing.setdefault(f.supplier, []).append(f)
    for supplier, printed_by in sorted(departing.items()):
        printed = "; ".join(
            f"{shown(v, unit)} on {', '.join(sorted(f.card for f in printed_by if f.value == v))}"
            for v in sorted({f.value for f in printed_by})
        )
        detail = f"prints {printed} where {', '.join(agreeing)} print {shown(best, unit)}"
        status: Status = "fail"
        if from_law:
            status, detail = "notice", detail + "; billed from the law for this month"
        elif all(_filled_from_regulation(f) for f in printed_by):
            status, detail = "notice", detail + "; billed the regulated fee for this month"
        yield Check(f"{supplier}: {what}", status, detail)


def consensus(cards: Iterable[Card]) -> list[Check]:
    """Every card that departs from what the fleet prints, figure by figure,
    among the cards of the same month."""
    groups: dict[tuple[str, str, str], list[Figure]] = {}
    for card in cards:
        for figure in figures(card):
            groups.setdefault((figure.month, figure.scope, figure.name), []).append(figure)
    checks: list[Check] = []
    for key in sorted(groups):
        checks.extend(_departures(groups[key]))
    return checks


async def check_fleet(
    session: aiohttp.ClientSession,
    extractors: Iterable[SupplierExtractor],
    today: date,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> list[Check]:
    """Every check of the run, in report order.

    The suppliers are checked side by side and each one's cards in turn,
    which keeps the load on any one site what an installation puts on it.
    One text memo spans the run, so a listing page several products share is
    downloaded once.
    """
    registry = tuple(extractors)
    wanted: dict[str, list[tuple[str, str]]] = {}
    for ex, contract, region in targets(registry, today):
        wanted.setdefault(ex.id, []).append((contract, region))
    with memoise_text_fetches({}):
        parts = await asyncio.gather(
            *(
                _check_supplier(session, ex, wanted[ex.id], today, sleep)
                for ex in registry
                if ex.id in wanted
            )
        )
    checks = [check for part, _cards in parts for check in part]
    checks.extend(consensus(card for _part, cards in parts for card in cards))
    return checks


def exit_code(checks: Iterable[Check]) -> int:
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        return 1
    if "transient" in statuses:
        return 2
    return 0


def render_fingerprint(checks: Iterable[Check]) -> str:
    """The failing labels, one per line and sorted: what the issue is filed on."""
    return "".join(
        f"{label}\n" for label in sorted({c.label for c in checks if c.status == "fail"})
    )


def _table(lines: list[str], title: str, note: str | None, checks: list[Check]) -> None:
    if not checks:
        return
    lines += [f"## {title}", ""]
    if note:
        lines += [note, ""]
    lines += ["| Check | Detail |", "| --- | --- |"]
    for check in checks:
        detail = check.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{check.label}` | {detail} |")
    lines.append("")


def render_report(checks: list[Check]) -> str:
    by_status: dict[str, list[Check]] = {"ok": [], "fail": [], "transient": [], "notice": []}
    for check in checks:
        by_status[check.status].append(check)
    lines = [
        f"# Live check: {len(by_status['ok'])} pass, {len(by_status['fail'])} fail, "
        f"{len(by_status['transient'])} transient, {len(by_status['notice'])} notices",
        "",
    ]
    _table(lines, "Failures", None, by_status["fail"])
    _table(
        lines,
        "Transient failures",
        "These failed on the network on every attempt. That says nothing about "
        "the card, so they are reported and never filed.",
        by_status["transient"],
    )
    _table(
        lines,
        "Notices",
        "Figures the integration does not bill as printed, figures the cards "
        "disagree on with no majority to measure them against, and cards published "
        "as page images the card archive has not read yet. Reported, never filed.",
        by_status["notice"],
    )
    lines += ["## All checks", ""]
    lines += [f"- [{'x' if c.status == 'ok' else ' '}] {c.label}" for c in checks]
    return "\n".join(lines) + "\n"


async def _run(texts: Path | None, fingerprint: Path) -> int:
    today = datetime.now(BRUSSELS).date()
    cache = StoredTexts(texts) if texts is not None else None
    async with aiohttp.ClientSession() as session:
        with ExitStack() as hooks:
            if cache is not None:
                hooks.enter_context(render_through(cache.render))
            checks = await check_fleet(session, all_extractors(), today)
    print(render_report(checks))
    if cache is not None:
        print(
            f"_{cache.served} of {cache.served + cache.rendered} cards were served "
            f"from the archive's texts; {cache.rendered} were rendered._"
        )
    fingerprint.write_text(render_fingerprint(checks), encoding="utf-8")
    return exit_code(checks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--texts",
        type=Path,
        default=None,
        metavar="DIR",
        help="the gas directory of a be_price_cards checkout; cards it holds are not rendered again",
    )
    parser.add_argument(
        "--fingerprint",
        type=Path,
        default=ROOT / "live_check_failures.txt",
        metavar="FILE",
        help="where to write the failing labels",
    )
    args = parser.parse_args()
    # The providers take the month off Home Assistant's clock, which an
    # installation sets to its own zone, and a Belgian one is in Brussels:
    # on UTC the 1st starts two hours late.
    dt_util.set_default_time_zone(BRUSSELS)
    try:
        return asyncio.run(_run(args.texts, args.fingerprint))
    except Exception:
        # Not a card's failure but this script's, so it gets its own code and
        # the workflow fails the run instead of filing it against a supplier.
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
