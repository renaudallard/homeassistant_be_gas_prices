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

  - the card is for the current month, unless it has no end date;
  - every supplier's index publication (its ``fetch_index``) reads, lists
    every index its cards are priced on, and has a recent value for each;
  - the regulated figures agree across the fleet: per DSO the T1 and T2
    terms, transport and metering, and the federal excise, the energy
    contribution, the VAT rate a card states, the Walloon connection fee and
    the Brussels per-meter levy.
    The regulators and the law set these for everyone, so a card printing
    something the other suppliers' cards of the same month do not is stale or
    misread, and the integration bills what the card prints. Where it bills
    the law or the regulated figure instead (the federal levies and the Walloon
    connection fee of a month it knows the law for, a Fluvius data management
    fee the card leaves out), or the card prints a figure its provider notes
    document as the card's error, the departure is a notice rather than a
    failure;
  - the excise law on Justel, FPS Finance's excise tariff and this year's
    Fluvius gas tariff sheet read as the integration reads them, and the
    fleet's excise, energy contribution, VAT rate, Walloon connection fee
    and Fluvius metering fee measured against what the integration bills.

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
import itertools
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
from card_texts import StoredTexts, in_daemon_thread  # type: ignore[import-not-found]  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.be_gas_prices import excise_law, fluvius, tarbel  # noqa: E402
from custom_components.be_gas_prices.const import (  # noqa: E402
    EXCISE_LAW_URL,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_WALLONIA,
    SUPPLIER_CUSTOM,
    SUPPLIER_SOCIAL,
    TIER_T1,
    TIER_T2,
    VAT_RATE_REDUCED,
    WALLOON_CONNECTION_FEE,
    WALLOON_CONNECTION_FEE_KNOWN_FROM,
)
from custom_components.be_gas_prices.providers import all_extractors  # noqa: E402
from custom_components.be_gas_prices.providers._pdf import (  # noqa: E402
    fetch_text,
    is_transient_fetch_error,
    memoise_text_fetches,
    render_through,
)
from custom_components.be_gas_prices.providers._rates import IndexedRates  # noqa: E402
from custom_components.be_gas_prices.providers._resolve import (  # noqa: E402
    law_contribution,
    law_excise,
)
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
# A supplier whose fetches fail on the network this many times in a row is
# not answering this runner today, and every further fetch would cost the
# same retries and pauses: a host that accepts and never answers would run
# the job into its timeout before anything is reported or committed. Give up
# on it for the run instead.
GIVE_UP_AFTER = 3

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


class Patience:
    """Per-supplier count of transient failures in a row, and the verdict."""

    def __init__(self) -> None:
        self.failures: dict[str, int] = {}
        self.given_up: set[str] = set()

    def note(self, supplier: str, err: BaseException) -> bool:
        """Record one failed fetch; True when the supplier is now given up on."""
        if not is_transient(err):
            self.failures[supplier] = 0
            return False
        self.failures[supplier] = self.failures.get(supplier, 0) + 1
        if self.failures[supplier] >= GIVE_UP_AFTER:
            self.given_up.add(supplier)
            return True
        return False

    def ok(self, supplier: str) -> None:
        self.failures[supplier] = 0


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
    *,
    withdrawn: bool = False,
) -> list[tuple[SupplierExtractor, str, str]]:
    """Every (extractor, contract, region) with a card to fetch today, and
    with ``withdrawn`` the contracts the supplier no longer sells too.

    The custom supplier is typed in by the household and has no card, and a
    supplier past its withdrawal date has left the market: its last card
    stays up and stays stale, which is not worth a daily failure. So does a
    withdrawn contract's, whose past months only the archive's backfill asks
    for.
    """
    out: list[tuple[SupplierExtractor, str, str]] = []
    for ex in extractors:
        if ex.id == SUPPLIER_CUSTOM or (only and ex.id not in only):
            continue
        if ex.deprecated_until is not None and today > ex.deprecated_until:
            continue
        for contract in ex.contracts:
            if contract.withdrawn is not None and not withdrawn:
                continue
            out.extend((ex, contract.id, region) for region in sorted(contract.regions))
    return out


def _failure(label: str, err: BaseException) -> Check:
    detail = f"{type(err).__name__}: {err}"
    if isinstance(err, CardNotReadableError):
        # A card published as page images, which only the card archive's
        # OCR reading of these very bytes lets the check parse. Until the
        # archive has read them no change here can: reported, never filed.
        # The archive files a card its OCR could not read.
        return Check(label, "notice", detail)
    return Check(label, "transient" if is_transient(err) else "fail", detail)


def _freshness(label: str, snapshot: SupplierSnapshot, today: date) -> Check:
    """Whether the card is for the current month. A card for a later month
    is a supplier publishing early, which is fine. A card with no end date
    applies until the supplier replaces it, and the fetch reads the one the
    supplier links today, so it cannot be late."""
    name = f"{label}: card month"
    if snapshot.valid_until is None:
        return Check(name, "ok", f"{snapshot.publication_label}, in force until replaced")
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
    """One supplier's cards in turn, then its index publication. What is
    left once the supplier is given up on is reported as transient."""
    checks: list[Check] = []
    cards: list[Card] = []
    patience = Patience()
    skipped = f"not fetched: gave up on {ex.id} after {GIVE_UP_AFTER} network failures in a row"
    for contract, region in wanted:
        label = f"{ex.id}/{contract}/{region}"
        if ex.id in patience.given_up:
            checks.append(Check(f"{label}: fetch", "transient", skipped))
            continue
        try:
            snapshot = await fetch_with_retry(
                functools.partial(ex.fetch, session, contract, region), sleep=sleep
            )
        except Exception as err:  # one card must not stop the check
            checks.append(_failure(f"{label}: fetch", err))
            patience.note(ex.id, err)
            continue
        patience.ok(ex.id)
        checks.append(Check(f"{label}: fetch", "ok"))
        checks.append(_freshness(label, snapshot, today))
        checks.append(_tiers(label, ex, snapshot, today))
        cards.append(Card(ex, contract, region, snapshot))
    if ex.fetch_index is not None and ex.id in patience.given_up:
        checks.append(Check(f"{ex.id}: index publication", "transient", skipped))
    elif ex.fetch_index is not None:
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
_VAT = "VAT rate"
# The levies the integration bills from the law rather than from the card
# (providers/_resolve.py): the excise, the energy contribution and the
# Walloon connection fee, for the months _law_figure gives. A card printing
# another figure for such a month is still wrong and reported, but nobody is
# billed on it, so it does not fail the check.
_EXCISE_SLICES = {_EXCISE_LOW: 0, _EXCISE_HIGH: 1}


# Card errors the provider notes in docs/providers document, by supplier,
# scope and figure: the value the card prints, the day the allowance lapses
# and why it is allowed. They are billed as printed, but already known, so a
# card printing that value is a notice; one printing anything else for them
# is a new departure and fails again, and so does the same value once the
# allowance has lapsed. Each one is a 2026 network term or levy, so each
# lapses with the tariff year: a card printing it in 2027 is news again.
_TARIFF_YEAR_END = date(2027, 1, 1)
_KNOWN_CARD_ERRORS: dict[tuple[str, str, str], tuple[float, date, str]] = {
    ("bolt", "ores", "T1 proportional"): (
        0.04198,
        _TARIFF_YEAR_END,
        "the 2026 grid with an older regulatory balance term",
    ),
    ("bolt", "ores", "T2 proportional"): (
        0.02115,
        _TARIFF_YEAR_END,
        "the 2026 grid with an older regulatory balance term",
    ),
    ("bolt", "resa", "T2 proportional"): (
        0.02259,
        _TARIFF_YEAR_END,
        "2,529 with two digits swapped",
    ),
    ("bolt", REGION_BRUSSELS, "levy q10_gt5000"): (
        12.54,
        _TARIFF_YEAR_END,
        "12,59 on the other cards",
    ),
    ("ebem", "fluvius_kempen", "T1 proportional"): (
        0.0212,
        _TARIFF_YEAR_END,
        "the 2025 figure kept in 2026",
    ),
    ("ecofix", "ores", "T1 proportional"): (
        0.04198,
        _TARIFF_YEAR_END,
        "the ORES term Bolt prints",
    ),
    ("ecofix", "ores", "T2 proportional"): (
        0.02115,
        _TARIFF_YEAR_END,
        "the ORES term Bolt prints",
    ),
    ("ecofix", "resa", "T2 fixed"): (
        140.93,
        _TARIFF_YEAR_END,
        "ORES's term printed for RESA",
    ),
    ("ecofix", "resa", "T2 proportional"): (
        0.02259,
        _TARIFF_YEAR_END,
        "the RESA term Bolt prints",
    ),
}


# DSO rows whose T2 a card is documented to misprint, by supplier and DSO:
# the day the allowance lapses, and why. The parser drops a T2 whose rate
# is not below T1's, and the household above 5 000 kWh there gets a pricing
# error rather than a bill.
_KNOWN_MISSING_T2: dict[tuple[str, str], tuple[date, str]] = {
    ("dots", "fluvius_limburg"): (_TARIFF_YEAR_END, "its T1 term printed as T2"),
}


def _tiers(label: str, ex: SupplierExtractor, snapshot: SupplierSnapshot, today: date) -> Check:
    """Whether every DSO row of a residential card prices T2, the tier of
    most households: a row without it leaves every one of them there
    unpriced, and gives the fleet no figure to compare."""
    name = f"{label}: T2 rows"
    if snapshot.taxes.vat_rate != 0.0:
        return Check(name, "ok", "a professional card")
    missing = sorted(dso for dso, overlay in snapshot.dsos.items() if TIER_T2 not in overlay.tiers)
    if not missing:
        return Check(name, "ok")
    known = {
        dso: _KNOWN_MISSING_T2[(ex.id, dso)][1]
        for dso in missing
        if (ex.id, dso) in _KNOWN_MISSING_T2 and today < _KNOWN_MISSING_T2[(ex.id, dso)][0]
    }
    detail = "; ".join(
        f"{dso}: {known[dso]}" if dso in known else f"{dso}: no T2 term read" for dso in missing
    )
    return Check(name, "notice" if len(known) == len(missing) else "fail", detail)


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
    on another excise scale, so it is left out rather than converted. So is
    the social tariff's: its network part is the CREG's, not any operator's,
    and its levies are the protected customer's.
    """
    snapshot = card.snapshot
    if (
        snapshot.taxes.vat_rate != 0.0
        or snapshot.supplier == SUPPLIER_SOCIAL
        or label_month(snapshot.publication_label) is None
    ):
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
    # for both slices; one printing no gas excise holds none.
    if taxes.excise_bands:
        rows.append(("federal", _EXCISE_LOW, "EUR/kWh", taxes.excise_bands[0][1]))
        rows.append(("federal", _EXCISE_HIGH, "EUR/kWh", taxes.excise_bands[-1][1]))
    rows.append(("federal", _CONTRIBUTION, "EUR/kWh", taxes.energy_contribution))
    if taxes.card_vat_rate is not None:
        # The rate the card states for its customers: the law's figures and
        # the formulas printed excluding VAT are put on 6 % on every card
        # that states none.
        rows.append(("federal", _VAT, "rate", taxes.card_vat_rate))
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
_COARSEST = {"EUR/kWh": 1e-4, "EUR/year": 1e-2, "rate": 1e-4}


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
    if unit == "rate":
        return f"{round(value * 100, 5):.10g} %"
    return f"{round(value, 5):.10g} {unit}"


def agree(a: float, b: float, unit: str) -> bool:
    """Whether two printed figures are the same figure: they differ by no
    more than one unit of the last digit the less precise one carries."""
    return abs(a - b) <= max(_step(a, unit), _step(b, unit)) + 1e-12


def _law_figure(name: str, month: str) -> float | None:
    """What the integration bills ``month`` on for the figure ``name``, as a
    residential card prints it: the excise of a slice and the energy
    contribution including VAT, the VAT exempt Walloon connection fee, and
    the residential VAT rate it puts a figure printed excluding VAT on where
    a card states none. None when it bills the card's."""
    parsed = label_month(month)
    if parsed is None:
        return None
    if name == _VAT:
        return VAT_RATE_REDUCED
    if name == _CONNECTION_FEE:
        return WALLOON_CONNECTION_FEE if parsed >= WALLOON_CONNECTION_FEE_KNOWN_FROM else None
    if name == _CONTRIBUTION:
        contribution = law_contribution(date(parsed[0], parsed[1], 1))
        return None if contribution is None else contribution * (1 + VAT_RATE_REDUCED)
    slice_ = _EXCISE_SLICES.get(name)
    if slice_ is None:
        return None
    rates = law_excise(date(parsed[0], parsed[1], 1))
    return None if rates is None else rates[slice_] * (1 + VAT_RATE_REDUCED)


def _billed_from_law(name: str, month: str) -> bool:
    # A card stating another VAT rate Belgium levies is billed on it.
    return name != _VAT and _law_figure(name, month) is not None


def _sheet_fee(figure: Figure) -> float | None:
    """The data management fee including VAT a Fluvius row of a card is
    billed when it prints none, from the year's tariff sheet, or None for
    any other figure and a year whose sheet was not read."""
    parsed = label_month(figure.month)
    if figure.name != "metering" or figure.scope not in FLUVIUS_KEYS or parsed is None:
        return None
    fee = fluvius.data_management_fee(parsed[0])
    return None if fee is None else fee * (1 + VAT_RATE_REDUCED)


def _filled_from_regulation(figure: Figure) -> bool:
    """A Fluvius row that prints no data management fee, in a year whose
    tariff sheet was read: the integration bills the sheet's fee
    (providers/_resolve.py), so the card is wrong but nobody is billed on it."""
    return figure.value == 0.0 and _sheet_fee(figure) is not None


def _awaits_sheet(figure: Figure) -> bool:
    """A Fluvius row that prints no data management fee: billed the year's
    fee off the tariff sheet once the integration has read it."""
    return figure.name == "metering" and figure.scope in FLUVIUS_KEYS and figure.value == 0.0


def _billed(first: Figure) -> tuple[float, str] | None:
    """What the integration bills for ``first``'s figure and month whatever
    the card prints, and where that comes from, or None."""
    law = _law_figure(first.name, first.month)
    if law is not None:
        return law, "the law"
    fee = _sheet_fee(first)
    return None if fee is None else (fee, "the Fluvius sheet")


def _known_error(figure: Figure, today: date) -> str | None:
    """Why a figure the provider notes document as the card's error is
    allowed today, or None."""
    known = _KNOWN_CARD_ERRORS.get((figure.supplier, figure.scope, figure.name))
    if known is None or today >= known[1] or not agree(figure.value, known[0], figure.unit):
        return None
    return known[2]


def _departures(group: list[Figure], today: date, sheet_unread: bool) -> Iterator[Check]:
    """The cards of one figure, month and scope that depart from the rest.

    The consensus comes in two steps. First the figures most voters agree
    with. Then, among them, the one the fleet prints: the figure most voters
    print as it is or to fewer digits, taken among those two voters print as
    they are where there are any, the most precise one among equals so the
    report shows the regulator's digits. A figure printed to fewer digits
    agrees with finer ones on both sides of it ("0,99" with 0,9891 and with
    a stale 0,9864 alike), so it is the consensus only where no finer one is
    shared, and one voter's own digits (EBEM's 0,105764 for the law's
    0,1057668 the others round) are not the consensus over the others'. A
    consensus printed to fewer digits than figures it agrees with that
    disagree with each other cannot tell which is right. That, and a
    disagreement with no majority, is reported as a notice: two voters
    against two is not a verdict on either. While the Fluvius sheet did not
    answer (``sheet_unread``), a Fluvius row printing no fee is transient:
    the source did not answer, which says nothing about the card.
    """
    first = group[0]
    unit = first.unit
    what = f"{first.scope} {first.name} for {first.month}"
    values = sorted({f.value for f in group})
    support = {v: {f.voter for f in group if agree(f.value, v, unit)} for v in values}
    top = max(values, key=lambda v: (len(support[v]), -_step(v, unit), v))
    cluster = [v for v in values if agree(v, top, unit)]
    printed_as = {
        v: {
            f.voter
            for f in group
            if f.value == v or (_step(f.value, unit) > _step(v, unit) and agree(f.value, v, unit))
        }
        for v in cluster
    }
    shared = [v for v in cluster if len({f.voter for f in group if f.value == v}) >= 2]
    best = max(shared or cluster, key=lambda v: (len(printed_as[v]), -_step(v, unit), v))
    billed = _billed(first)
    if (
        billed is not None
        and len(support[best]) >= 2
        and not agree(best, billed[0], unit)
        and len(support[best]) > len({f.voter for f in group if agree(f.value, billed[0], unit)})
    ):
        # Most of the fleet prints another figure than the one billed: the
        # law or the sheet changed and was misread or not followed, or a
        # figure typed in const.py is wrong. Every residential month is
        # billed on it.
        yield Check(
            f"{first.scope}: {first.name} for {first.month} against {billed[1]}",
            "fail",
            f"{', '.join(sorted({f.supplier for f in group if agree(f.value, best, unit)}))} "
            f"print {shown(best, unit)} where the integration bills {shown(billed[0], unit)}",
        )
    elif billed is not None and agree(best, billed[0], unit):
        # A consensus printed to fewer digits agrees with a stale figure
        # too: a finer one it agrees with that the billed figure does not is
        # the one comparison left, made by too few voters for a verdict.
        for v in cluster:
            if _step(v, unit) < _step(best, unit) and not agree(v, billed[0], unit):
                yield Check(
                    f"{first.scope}: {first.name} for {first.month} against {billed[1]}",
                    "notice",
                    f"{', '.join(sorted({f.supplier for f in group if f.value == v}))} "
                    f"print {shown(v, unit)} where the integration bills "
                    f"{shown(billed[0], unit)}; the others print fewer digits",
                )
    rivals = [v for v in values if not agree(v, best, unit)]
    # Finer figures the consensus agrees with but that disagree with each
    # other: printed to fewer digits, it cannot tell which one is right.
    finer = [v for v in values if _step(v, unit) < _step(best, unit) and agree(v, best, unit)]
    split = any(not agree(a, b, unit) for a, b in itertools.combinations(finer, 2))
    if not rivals and not split:
        return
    agreeing = sorted({f.supplier for f in group if agree(f.value, best, unit)})
    if (
        split
        or len(support[best]) < 2
        or any(len(support[v]) >= len(support[best]) for v in rivals)
    ):
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
        elif sheet_unread and all(_awaits_sheet(f) for f in printed_by):
            status, detail = "transient", detail + "; the Fluvius sheet did not answer"
        elif None not in (reasons := {_known_error(f, today) for f in printed_by}):
            why = "; ".join(sorted(reason for reason in reasons if reason))
            status, detail = (
                "notice",
                detail + f"; a known card error ({why}), see docs/providers/{supplier}.md",
            )
        yield Check(f"{supplier}: {what}", status, detail)


def consensus(
    cards: Iterable[Card], today: date | None = None, *, sheet_unread: bool = False
) -> list[Check]:
    """Every card that departs from what the fleet prints, figure by figure,
    among the cards of the same month. ``today``, Brussels's by default, is
    what the known card errors lapse against; ``sheet_unread`` says the
    year's Fluvius sheet did not answer."""
    today = today or datetime.now(BRUSSELS).date()
    groups: dict[tuple[str, str, str], list[Figure]] = {}
    for card in cards:
        for figure in figures(card):
            groups.setdefault((figure.month, figure.scope, figure.name), []).append(figure)
    checks: list[Check] = []
    for key in sorted(groups):
        checks.extend(_departures(groups[key], today, sheet_unread))
    return checks


async def check_fleet(
    session: aiohttp.ClientSession,
    extractors: Iterable[SupplierExtractor],
    today: date,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    sheet_unread: bool = False,
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
    fleet = (card for _part, cards in parts for card in cards)
    checks.extend(consensus(fleet, today, sheet_unread=sheet_unread))
    return checks


async def check_excise_law(
    session: aiohttp.ClientSession,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Check:
    """Article 419 read from Justel, as the integration reads it, and held
    for the fleet's excise to be measured against: run before the fleet."""
    label = "federal: gas excise law"
    try:
        page = await fetch_with_retry(
            functools.partial(fetch_text, session, EXCISE_LAW_URL, timeout=30), sleep=sleep
        )
        excise_law.hold(excise_law.parse(page))
        excise_law.hold_protected(excise_law.parse_protected(page))
    except Exception as err:  # reported like a card
        return _failure(label, err)
    return Check(label, "ok")


async def check_excise_tariff(
    session: aiohttp.ClientSession,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Check:
    """FPS Finance's excise tariff read as the integration reads it, and held
    for the fleet's excise to be measured against: run before the fleet. It
    gives the quarters the notices of art. 420 set, which Justel does not."""
    label = "federal: gas excise tariff"
    try:
        periods, protected = await fetch_with_retry(
            functools.partial(tarbel.fetch_tariff, session), sleep=sleep
        )
        tarbel.hold(periods)
        if not protected:
            raise tarbel.TarbelError("no protected customer rate read")
        tarbel.hold_protected(protected)
    except Exception as err:  # reported like a card
        return _failure(label, err)
    return Check(label, "ok")


def check_protected_excise(today: date, *, law_read: bool, tariff_read: bool) -> Check | None:
    """The protected customer's excise the social tariff bills this month,
    Justel's against FPS Finance's tariff: no card prints it, so no fleet
    can measure it. Both read and listing the month, they must agree; both
    read and neither listing it, the social tariff prices nothing. None
    while a source that failed leaves nothing to compare, its own check
    being the failure."""
    label = f"federal: protected excise for {today:%Y-%m}"
    law = excise_law.protected_excise_for(today) if law_read else None
    listed = tarbel.protected_excise_for(today) if tariff_read else None
    rate = law if law is not None else listed
    if rate is None:
        if not (law_read and tariff_read):
            return None
        return Check(label, "fail", "neither the law nor FPS Finance's tariff gives one")
    if law is not None and listed is not None and abs(law - listed) > 1e-9:
        return Check(
            label,
            "fail",
            f"the law gives {shown(law, 'EUR/kWh')} where FPS Finance's tariff gives "
            f"{shown(listed, 'EUR/kWh')}, excluding VAT",
        )
    return Check(label, "ok", f"{shown(rate, 'EUR/kWh')} excluding VAT")


async def check_fluvius_sheet(
    session: aiohttp.ClientSession,
    today: date,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> Check:
    """This year's Fluvius gas tariff sheet read as the integration reads
    it, and held for the fleet's Fluvius fees to be measured against: run
    before the fleet. A year Fluvius lists no sheet for, or a sheet that no
    longer gives the fee, fails: the fee a Flemish card leaves out is then
    left out of the bill too."""
    label = f"fluvius: {today.year} gas tariff sheet"
    try:
        fee = await fetch_with_retry(
            functools.partial(fluvius.fetch_fee, session, today.year), sleep=sleep
        )
    except Exception as err:  # reported like a card
        return _failure(label, err)
    if fee is None:
        return Check(label, "fail", "no sheet listed, or no yearly-read data management fee on it")
    fluvius.hold(today.year, fee)
    return Check(label, "ok")


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


async def _render(
    _variant: str, _url: str, payload: bytes, renderer: Callable[[bytes], str]
) -> str:
    return str(await in_daemon_thread(renderer, payload))


async def _run(texts: Path | None, fingerprint: Path) -> int:
    today = datetime.now(BRUSSELS).date()
    cache = StoredTexts(texts) if texts is not None else None
    async with aiohttp.ClientSession() as session:
        with ExitStack() as hooks:
            # Every render goes through a hook that runs it in a daemon
            # thread, so one that never returns cannot hold the exit.
            hooks.enter_context(render_through(_render if cache is None else cache.render))
            checks = [await check_excise_law(session), await check_excise_tariff(session)]
            protected = check_protected_excise(
                today, law_read=checks[0].status == "ok", tariff_read=checks[1].status == "ok"
            )
            if protected is not None:
                checks.append(protected)
            sheet = await check_fluvius_sheet(session, today)
            checks.append(sheet)
            checks += await check_fleet(
                session, all_extractors(), today, sheet_unread=sheet.status == "transient"
            )
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
