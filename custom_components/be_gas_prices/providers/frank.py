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

"""Frank Energie gas tariff card extractor.

Frank Energie sells one variable gas contract in Flanders in tiers, each
with its own monthly card. Read here: the standard one, HV, JN, Slim (only
beside a Slim electricity contract), Plus from November 2026 and Korting,
withdrawn from November 2026. The cards are file assets of the Sanity CMS
behind the site, found with the query the electricity cards use:

    https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be
        *[_type=="sanity.fileAsset" && originalFilename match "*Gas*"]

The file names carry the tier and the month: "... Tariefkaart Gas ZTP
September 2026.pdf" for the standard tier, "... Gas ZTP HV Oktober 2026
(1).pdf" for HV, whose September 2026 file drops the ZTP. Korting files are
"VT", Slim ones "SL" or "Slim" from month to month. Frank
re-uploads a card now and then ("... Maart 2026 (1).pdf", "... 2026 v2.pdf"),
sometimes after the next month's card, so a card is chosen by the month its
name gives and only then by upload time. Uploads go back to July 2023, which
makes the same query the archive, but the late 2024 names carry no year and
the January 2025 cards are the first to print the Fluvius areas of today.

Every tier prices the delivery month at

    (factor x M ZTP RLP0N EOD EEX + base) x 1,06 in EURct/kWh

with the index, the RLP-weighted ZTP day price published by EEX, in EUR/MWh.
The formula carries its VAT, so it is not grossed again. The card only
prints the formula at a VNR estimate ("Verwacht volgens methode VNR voor
september 2026"). Frank publishes the realised index month by month in the
"Index waarden" table of its terms page, which is what fetch_index reads.
The same page links the card of every tier sold today, which is what
discover reads for the live check.

The subscription is printed per month. The cashback a tier prints (JN's,
and HV's and Korting's up to September 2026) is not read: a snapshot has no
field for it.

The November 2026 JN card prints its subscription as 0,00 and waives it for
a contract's first twelve months, after which "gelden de abonnementskosten
volgens de tariefkaart Gas ZTP - JN - Oktober 2026". The subscription is
billed at the one the named card prints and waived for those months of a
contract signed on the card; see ``_with_waived_fee``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import REGION_FLANDERS
from ._network import (
    FLUVIUS_LABELS,
    METERING,
    T1_FIXED,
    T1_PROP,
    T2_FIXED,
    T2_PROP,
    TRANSPORT,
    excise_bands,
    printed_contribution,
    read_dsos,
    require_region,
)
from ._parse import (
    SIGN_CHARS,
    fold_accents,
    html_rows,
    month_date,
    parse_sign,
    require_contract,
    to_float,
)
from ._pdf import (
    MONTH_NAMES,
    NL_MONTHS,
    fetch_pdf_text_layout,
    fetch_text,
    parse_json,
    printed_vat_rate,
)
from ._rates import Contract, IndexedRates
from ._validity import end_of_month, future_month, month_card
from .base import (
    DsoOverlay,
    ExtractorError,
    IndexTable,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_SANITY_URL = "https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be"
# Where the files of the project's dataset the query reads are served, which
# every card link has to point into: all 221 gas uploads Sanity listed on
# 6 November 2026, from July 2023 on, did. A link anywhere else, which a
# tampered answer could give to reach a host on the household's network, is
# passed over unread.
_FILES_URL = "https://cdn.sanity.io/files/8navd656/production-be/"
_TERMS_URL = "https://www.frankenergie.be/nl/voorwaarden"
_GAS_ASSETS = '*[_type=="sanity.fileAsset" && originalFilename match "*Gas*"'

# The index as the card and the terms page both name it.
_INDEX = "M ZTP RLP0N EOD EEX"

_REGIONS = frozenset({REGION_FLANDERS})


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    # The tier words the card's title and the file names use; "" for the
    # standard tier, which names none.
    titles: tuple[str, ...]
    files: tuple[str, ...]
    # The first day Frank no longer sold the tier (``Contract.withdrawn``).
    withdrawn: date | None = None


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("frank_variable", "Frank Energie Gas Variabel", ("",), ("",)),
    _ContractDef("frank_variable_hv", "Frank Energie Gas Variabel HV", ("HV",), ("HV",)),
    # "June" until May 2025: the same wording ("voor klanten met een digitale
    # meter"), formula and 35 EUR cashback as the JN cards after it.
    _ContractDef(
        "frank_variable_jn", "Frank Energie Gas Variabel JN", ("JN", "June"), ("JN", "June")
    ),
    _ContractDef(
        "frank_variable_slim", "Frank Energie Gas Variabel Slim", ("Slim", "SL"), ("SL", "Slim")
    ),
    # From November 2026: the standard tier's formula and subscription, and
    # HV's six months' subscription for a contract ended in its first six.
    _ContractDef("frank_variable_plus", "Frank Energie Gas Variabel Plus", ("Plus",), ("Plus",)),
    # Titled "Korting ZTP" until September 2026, "VT ZTP" from October, as
    # its file was named all along. Withdrawn from November 2026: Frank
    # uploaded no November card for it while the other tiers had theirs on
    # 30 October, and its conditions page lists the gas cards of the
    # standard, HV, JN, Plus and Slim tiers alone on 4 November.
    _ContractDef(
        "frank_variable_korting",
        "Frank Energie Gas Variabel Korting",
        ("Korting", "VT"),
        ("VT",),
        withdrawn=date(2026, 11, 1),
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


# ---- finding a card ----------------------------------------------------------

# "Gas[ ZTP][ <tier words>] <Maand> <jaar>", in a file name whose runs of
# spaces are collapsed first (the August 2026 names put two before the
# month). A name without a month ("Gas ZTP Wintervast 2025") is another
# product. The first "Gas" is found, then the first month after it, and the
# tier words are what lies between: one pattern lazily spanning those words
# ran to the end of the name from every "Gas" in it, 19,5 s over a 32 kB
# name that the Sanity answer could carry.
_FILE_GAS_RE = re.compile(r"\bGas(?: ZTP)?(?= )", re.IGNORECASE)
_FILE_MONTH_RE = re.compile(r" (" + "|".join(NL_MONTHS) + r") (20\d{2})\b", re.IGNORECASE)


@dataclass(frozen=True, order=True)
class _Asset:
    month: date
    created: str
    url: str


def _file_month(name: str) -> tuple[str, date] | None:
    """The tier words and the month a collapsed file name gives, or None."""
    gas = _FILE_GAS_RE.search(name)
    month = None if gas is None else _FILE_MONTH_RE.search(name, gas.end())
    if gas is None or month is None:
        return None
    tier = name[gas.end() : month.start()].strip()
    return tier, date(int(month.group(2)), MONTH_NAMES[month.group(1).lower()], 1)


def _assets(rows: list[dict[str, Any]], contract: _ContractDef) -> list[_Asset]:
    """The rows that are ``contract``'s cards, whatever month."""
    out: list[_Asset] = []
    for row in rows:
        found = _file_month(" ".join(str(row.get("originalFilename", "")).split()))
        url = row.get("url")
        if found is None or not isinstance(url, str) or not url.startswith(_FILES_URL):
            continue
        tier, month = found
        if tier not in contract.files:
            continue
        out.append(_Asset(month, str(row.get("_createdAt", "")), url))
    return out


async def _query(session: aiohttp.ClientSession, query: str) -> list[dict[str, Any]]:
    body = await fetch_text(session, _SANITY_URL, params={"query": query})
    payload = parse_json(body, "Frank Energie: Sanity answer")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, list):
        raise ExtractorError("Frank Energie: Sanity answer has no result list")
    return [row for row in result if isinstance(row, dict)]


def _region(region: str) -> None:
    if region != REGION_FLANDERS:
        raise ExtractorError(f"Frank Energie: gas is not sold in region {region!r}")


class _OtherTierError(ExtractorError):
    """The card is another tier's than the one asked for."""


async def _read_month(
    session: aiohttp.ClientSession, contract: _ContractDef, region: str, assets: list[_Asset]
) -> SupplierSnapshot:
    """The tier's card among one month's uploads, newest upload first.

    A file named for one tier can carry another's card: "Gas ZTP Januari
    2026 v2.pdf" is the Korting card, uploaded after the standard tier's own.
    Only that is a reason to try the next upload; any other failure is the
    card's own.
    """
    for asset in sorted(assets, reverse=True):
        text = await fetch_pdf_text_layout(session, asset.url)
        try:
            snap = parse_snapshot(contract.contract_id, region, text, source_url=asset.url)
        except _OtherTierError:
            continue
        return await _with_waived_fee(session, contract, region, text, snap)
    raise ExtractorError(f"Frank Energie: no {contract.label} card among the uploads")


async def fetch(session: aiohttp.ClientSession, contract_id: str, region: str) -> SupplierSnapshot:
    """The card of the newest month ``contract_id``'s tier has one for."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Frank Energie")
    _region(region)
    if contract.withdrawn is not None:
        raise ExtractorError(
            f"Frank Energie withdrew {contract.label} on {contract.withdrawn:%Y-%m-%d}: "
            "only its past cards are read"
        )
    # Five tiers a month: the last 40 uploads reach back further than any
    # re-upload has lagged.
    rows = await _query(
        session, _GAS_ASSETS + "]{originalFilename,url,_createdAt} | order(_createdAt desc)[0..39]"
    )
    assets = _assets(rows, contract)
    if not assets:
        raise ExtractorError(f"Frank Energie: no card found for {contract.label}")
    # Frank uploads a month's cards in the last days of the month before,
    # and until that month begins the running one's card is in force.
    months = {asset.month for asset in assets}
    newest = max((m for m in months if not future_month(m)), default=max(months))
    return await _read_month(session, contract, region, [a for a in assets if a.month == newest])


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card Frank published for ``year_month``, or None.

    The month is the one the file name gives, so no clock is involved; of
    several uploads the latest of the tier wins. A month with no card is
    None, only a transient failure raises, and a card naming another month
    is refused.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region != REGION_FLANDERS:
        return None
    wanted = date(year_month.year, year_month.month, 1)
    query = (
        _GAS_ASSETS
        + f' && originalFilename match "*{NL_MONTHS[wanted.month - 1].capitalize()}*"'
        + f' && originalFilename match "*{wanted.year}*"'
        + "]{originalFilename,url,_createdAt}"
    )
    return await month_card(_read_listed(session, contract, region, query, wanted), wanted)


async def _read_listed(
    session: aiohttp.ClientSession, contract: _ContractDef, region: str, query: str, wanted: date
) -> SupplierSnapshot | None:
    assets = [a for a in _assets(await _query(session, query), contract) if a.month == wanted]
    if not assets:
        return None
    return await _read_month(session, contract, region, assets)


async def probe(session: aiohttp.ClientSession, contract_id: str, region: str) -> str | None:
    """The upload time of the newest gas file, a new card changing it, and
    the running month, so a card uploaded before its month began is fetched
    once it begins.

    One short query, where reading a card takes half a minute of rendering
    on a Raspberry Pi.
    """
    if contract_id not in _CONTRACTS_BY_ID or region != REGION_FLANDERS:
        return None
    try:
        rows = await _query(session, _GAS_ASSETS + "]{_createdAt} | order(_createdAt desc)[0..0]")
    except ExtractorError:
        return None
    if not rows:
        return None
    return f"{rows[0].get('_createdAt')} {dt_util.now().date():%Y-%m}"


# ---- the card ----------------------------------------------------------------


def parse_snapshot(
    contract_id: str, region: str, text: str, *, source_url: str = _SANITY_URL
) -> SupplierSnapshot:
    """Parse one card's text as pdfplumber lays it out."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Frank Energie")
    _region(region)
    card_month = _card_month(text, contract)
    vat_rate = printed_vat_rate(text, _VAT_RE)
    if vat_rate is None:
        raise ExtractorError("Frank Energie: VAT rate not found")
    return SupplierSnapshot(
        supplier="frank",
        contract=contract_id,
        energy=_energy(text),
        dsos=_dsos(text),
        taxes=TaxOverlay(
            excise_bands=_excise(text),
            energy_contribution=_energy_contribution(text, card_month),
            card_vat_rate=vat_rate,
        ),
        source_url=source_url,
        publication_label=f"{card_month:%Y-%m}",
        valid_until=end_of_month(card_month.year, card_month.month),
    )


# The title is the card's first line, its parts set apart by dashes:
# "Tariefkaart gas variabel contract" ("Tariefkaart gas variabel" from 1
# October 2026, "Frank Energie Variabel" on the card re-uploaded on 2
# October) and the month for the standard tier, "Frank Energie Variabel",
# the tier and the month for the others. The tier reads "HV ZTP",
# "JN", "Slim" or "Korting ZTP" ("VT ZTP" from October 2026), and "SL" or
# "Slim ZTP" on older Slim cards. It is the one place the card names its
# tier, so it is checked against the contract: the tiers' formulas and fees
# can differ. Nothing may follow the tier but "ZTP", which keeps out the
# January 2025 product whose title names both Korting and Slim. The year has
# to be whole: the March 2026 cards lost the last character of every line
# ("maart 202", "(EUR/maand 2,9" for 2,92) and are refused rather than read
# short.
_DASH = "[" + SIGN_CHARS + "]"
_TITLE_RE = re.compile(
    r"(?:Tariefkaart gas variabel(?: contract)?|Frank Energie Variabel(?:\s*"
    + _DASH
    + r"\s*(\S+)(?:\s+ZTP)?)?)"
    r"\s*" + _DASH + r"\s*([a-z]+)\s+(\d{4})\s*",
    re.IGNORECASE,
)


def _card_month(text: str, contract: _ContractDef) -> date:
    title = text.lstrip().partition("\n")[0]
    match = _TITLE_RE.fullmatch(title.strip())
    month = MONTH_NAMES.get(fold_accents(match.group(2))) if match else None
    if match is None or month is None:
        raise ExtractorError(f"Frank Energie: card title {title!r} not understood")
    if (match.group(1) or "") not in contract.titles:
        raise _OtherTierError(f"Frank Energie: card {title!r} is not {contract.label}")
    return month_date(match.group(3), month, "Frank Energie")


# "Gedurende de eerste 12 maanden van het contract ontvang je een maandelijkse
# korting op de abonnementskosten, waardoor deze worden verlaagd naar € 0,00
# per maand. Na afloop van deze periode gelden de abonnementskosten volgens
# de tariefkaart Gas ZTP - JN - Oktober 2026", under the JN card's "0,00*".
_FEE_WAIVER_RE = re.compile(
    r"eerste\s+(\d+)\s+maanden\s+van\s+het\s+contract\s+ontvang\s+je\s+een\s+"
    r"maandelijkse\s+korting\s+op\s+de\s+abonnementskosten"
)
_FEE_CARD_RE = re.compile(
    r"abonnementskosten\s+volgens\s+de\s+tariefkaart\s+Gas\s+ZTP\s*"
    + _DASH
    + r"\s*(\S+)\s*"
    + _DASH
    + r"\s*([A-Za-z]+)\s+(\d{4})"
)

# The yearly subscription of the cards a waiver names, by (contract, month):
# a published card's subscription does not change, and reading one takes
# half a minute of rendering on a Raspberry Pi.
_NAMED_FEES: dict[tuple[str, date], float] = {}


async def _with_waived_fee(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region: str,
    text: str,
    snap: SupplierSnapshot,
) -> SupplierSnapshot:
    """``snap`` with the subscription its card waives for a contract's
    first months: billed at the one the earlier card it names prints, and
    waived for those months of a contract signed on it. A card waiving none
    is returned as it is; one naming no earlier card of its own tier fails,
    since the subscription billed after the waiver would be a guess."""
    waiver = _FEE_WAIVER_RE.search(text)
    if waiver is None:
        return snap
    named = _FEE_CARD_RE.search(text)
    month = None if named is None else MONTH_NAMES.get(named.group(2).lower())
    when = None if named is None or month is None else date(int(named.group(3)), month, 1)
    if (
        named is None
        or when is None
        or named.group(1) not in contract.files
        or snap.valid_until is None
        or when > snap.valid_until.replace(day=1)
        or when == snap.valid_until.replace(day=1)
    ):
        raise ExtractorError(
            f"Frank Energie: the {contract.label} card waives its subscription "
            "in a way not priced here"
        )
    yearly = _NAMED_FEES.get((contract.contract_id, when))
    if yearly is None:
        card = await fetch_for_month(session, contract.contract_id, region, when)
        if card is None:
            raise ExtractorError(
                f"Frank Energie: the {when:%m/%Y} {contract.label} card a waiver names "
                "is not published"
            )
        yearly = card.energy.yearly_fixed_fee
        _NAMED_FEES[(contract.contract_id, when)] = yearly
    return replace(
        snap,
        energy=replace(snap.energy, yearly_fixed_fee=yearly),
        fee_waived_months=int(waiver.group(1)),
    )


_VAT_RE = re.compile(r"incl\.\s*(\d+)\s*%\s*btw")
_FEE_RE = re.compile(r"Abonnementskost \(EUR/maand\)\s+(\d+,\d+)")
_FORMULA_RE = re.compile(
    r"Formule Gasprijs:\s*\(\s*(\d+,\d+)\s*x\s*" + r"\s+".join(_INDEX.split()) + r"\s*"
    r"(" + _DASH + r")\s*(\d+(?:,\d+)?)\s*\)\s*x\s*(\d+,\d+)\s*in EURct/kWh"
)
# The regulator is "VNR" on the recent cards and "VREG", its old name, on
# the older ones.
_ESTIMATE_RE = re.compile(
    r"Verwacht volgens methode (?:VNR|VREG) voor [a-z]+ \d{4} \(EURct/kWh\)\s+(\d+,\d+)"
)


def _energy(text: str) -> IndexedRates:
    formula = _FORMULA_RE.search(text)
    estimate = _ESTIMATE_RE.search(text)
    fee = _FEE_RE.search(text)
    if formula is None or estimate is None or fee is None:
        raise ExtractorError("Frank Energie: formula, expected price or subscription not found")
    # EURct/kWh with the index in EUR/MWh, times the multiplier the formula
    # itself prints.
    vat = to_float(formula.group(4))
    base = parse_sign(formula.group(2)) * to_float(formula.group(3))
    return IndexedRates(
        factor=to_float(formula.group(1)) / 100.0 * vat,
        base=base / 100.0 * vat,
        index=_INDEX,
        price=to_float(estimate.group(1)) / 100.0,
        yearly_fixed_fee=to_float(fee.group(1)) * 12.0,
        formula=" ".join(formula.group(0).split()),
    )


# "Fluvius | Databeheer EUR/jaar | Transport EURct/kWh | Kleinverbruik
# Variabel, Vast | Gemiddeld verbruik Variabel, Vast", the areas without the
# "Fluvius" prefix.
_COLUMNS = (METERING, TRANSPORT, T1_PROP, T1_FIXED, T2_PROP, T2_FIXED)
_LABELS = {label.removeprefix("Fluvius "): key for label, key in FLUVIUS_LABELS.items()}
# The table comes out one figure per line under its area; joining every line
# that opens on a figure to the one above rebuilds the rows.
_FIGURE_LINE_RE = re.compile(r"\n[ \t]*(?=\d)")


def _dsos(text: str) -> dict[str, DsoOverlay]:
    # "N ettarieven": the heading's first letter is set apart.
    start = re.search(r"N\s?ettarieven", text)
    end = text.find("Informatie over uw tariefformule")
    if start is None or end < start.end():
        raise ExtractorError("Frank Energie: network table not found")
    dsos = read_dsos(
        _FIGURE_LINE_RE.sub(" ", text[start.end() : end]),
        _LABELS,
        _COLUMNS,
        supplier="Frank Energie",
    )
    require_region(dsos, REGION_FLANDERS, "Frank Energie")
    return dsos


# The lower band's figure falls on the next line.
_EXCISE_RE = re.compile(
    r"Bijzondere accijns op Energie \(EURct/kWh\) (kleiner|groter) dan 12\.000 kWh\s+(\d+,\d+)"
)
_CONTRIBUTION_RE = re.compile(r"Bijdrage op Energie \(EURct/kWh\)\s+(\d+,\d+)")


def _excise(text: str) -> tuple[tuple[float | None, float], ...]:
    rates = {m.group(1): to_float(m.group(2)) / 100.0 for m in _EXCISE_RE.finditer(text)}
    if set(rates) != {"kleiner", "groter"}:
        raise ExtractorError("Frank Energie: federal excise rows not found")
    return excise_bands(rates["kleiner"], rates["groter"])


def _energy_contribution(text: str, card_month: date) -> float:
    """The energy contribution as printed; Frank dropped the row from its
    August 2026 cards."""
    match = _CONTRIBUTION_RE.search(text)
    return printed_contribution(
        None if match is None else match.group(1), card_month, supplier="Frank Energie"
    )


# ---- index values ------------------------------------------------------------

_PERIOD_RE = re.compile(r"([A-Za-z]+) (20\d{2})")
_VALUE_RE = re.compile(r"\d+(?:[.,]\d+)?")


def parse_index_page(page: str) -> IndexTable:
    """The M ZTP RLP0N EOD EEX column of the terms page's "Index waarden"
    table, in EUR/MWh, month by month ("Augustus 2026" | ... | "61.9").

    The column is found by its heading, since the table carries four indices
    and has dropped one of them before (M TTF RLP0N EOD EEX, empty since June
    2024). Its figures are dot decimals, with the odd comma elsewhere in the
    table.
    """
    start = page.find("Index waarden")
    end = page.find("</table>", start)
    if start < 0 or end < 0:
        raise ExtractorError("Frank Energie: index table not found")
    column: int | None = None
    values: dict[str, float] = {}
    for cells in html_rows(page[start:end]):
        if column is None:
            if _INDEX in cells:
                column = cells.index(_INDEX)
            continue
        period = _PERIOD_RE.fullmatch(cells[0]) if cells else None
        month = MONTH_NAMES.get(period.group(1).lower()) if period else None
        if period is None or month is None or len(cells) <= column:
            raise ExtractorError(f"Frank Energie: unexpected index row {cells!r}")
        if _VALUE_RE.fullmatch(cells[column]):
            values[f"{period.group(2)}-{month:02d}"] = to_float(cells[column])
    if not values:
        raise ExtractorError(f"Frank Energie: no {_INDEX} values in the index table")
    return {_INDEX: values}


async def fetch_index(session: aiohttp.ClientSession) -> IndexTable:
    """Frank's published M ZTP RLP0N EOD EEX values."""
    return parse_index_page(await fetch_text(session, _TERMS_URL))


# The terms page links the card of every product Frank sells today, the gas
# ones as "Actuele Tariefkaart Gas Variabel (ZTP)" followed by the tier's
# word but for the standard tier. On 5 November 2026 they were the standard,
# HV, JN, Slim and Plus tiers: Korting's "VT" link left the page with the
# tier, and Plus's came with its first card. A link's text stands in the
# HTML, ended by "<", and in the page's script payload, ended by an escaped
# quote.
_CATALOG_RE = re.compile(r"Actuele Tariefkaart Gas\b([^<\\\"]*)")
_CATALOG_TIER_RE = re.compile(r"Variabel \(ZTP\)(?: (\w+))?")
_TIER_IDS = {title.casefold(): c.contract_id for c in _CONTRACTS for title in c.titles}


def parse_catalog(page: str) -> set[str]:
    """The products the terms page links a gas card for, by contract id.

    A tier no contract reads is ``frank_variable_<word>`` and any other gas
    card ``frank_gas_<words>``, so the live check names what it found.
    """
    out: set[str] = set()
    for match in _CATALOG_RE.finditer(page):
        name = " ".join(match.group(1).split())
        tier = _CATALOG_TIER_RE.fullmatch(name)
        if tier is None:
            out.add("frank_gas_" + "_".join(re.findall(r"\w+", name.casefold())))
            continue
        word = (tier.group(1) or "").casefold()
        out.add(_TIER_IDS.get(word, f"frank_variable_{word}"))
    return out


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The gas products Frank sells today, as its terms page links them."""
    return parse_catalog(await fetch_text(session, _TERMS_URL))


EXTRACTOR = SupplierExtractor(
    id="frank",
    label="Frank Energie",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind="indexed",
            regions=_REGIONS,
            withdrawn=c.withdrawn,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
    fetch_index=fetch_index,
    discover=discover,
    sweep_cost_s=29.0,
)
