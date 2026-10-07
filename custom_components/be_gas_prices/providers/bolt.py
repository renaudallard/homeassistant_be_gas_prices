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

"""Bolt gas tariff card extractor.

Bolt prints one gas card per product for all three regions, all on the
listing page https://www.boltenergie.be/fr/listes-des-prix, which is how
:func:`fetch` finds the current one.

The fixed products, Bolt Fixe and Plenty Fixe, roll monthly under a YYYYMM
suffix, and the same URL for an earlier month is the archive:

    https://files.boltenergie.be/pricelists/fix/<fix|plenty_fix>_res_ng_fr_<YYYYMM>.pdf

The card says its price may change within the month ("Le prix proposé peut
évoluer dans le courant du mois"), and the September 2026 card was last
modified on the 14th, so the probe reads the card's own Last-Modified rather
than the listing's ETag. The two fixed cards print the same price and
tables; Plenty Fixe charges a lower platform fee and asks for a share in the
Plenty cooperative.

The variable products (Bolt Variable, Bolt Variable Online, Plenty Variable,
Plenty Variable Online) carry a version number instead:

    https://files.boltenergie.be/pricelists/var/<bolt|online|plenty|plenty_online>_res_ng_fr_<n>.pdf

Bolt publishes a new version on no fixed schedule (in 2026 June, twice for
Bolt Variable and Bolt Variable Online, then September and October, none in
July or August) and keeps serving the old ones. A version is in force until
the next, so a variable card has no end date, and the card of a past month is
the newest version whose title names that month or an earlier one. A contract keeps the formula of the card it was
signed on: the general terms put the price in the contract's particular
conditions and let Bolt change it only with two months' notice.

The four print the same formula, "TTF *1,049 + 10,10" in EUR/MWh excluding
VAT in October 2026, and differ by their platform fee. The card bills each
day at that day's TTF day-ahead value and spreads a consumption the grid
operator reports without daily values over the days by the Synergrid RLP, so
such a month is billed at the RLP-weighted mean of the month. The listing
publishes that mean as the "priceHistory" "gas" series in EUR/kWh, one value
per month, under no name; it matches OCTA+'s "TTF RLP Mois" (EGSI day-ahead
weighted by the RLP) to 0,03 EUR/MWh from September 2025 to September 2026.
The series also holds forward values for the running month and the ones
after, which :func:`fetch_index` leaves out. A household whose daily values
reach Bolt is billed on its own daily profile, which the monthly mean only
approximates.

The fee is billed per month and kept here per year. Every figure is VAT
inclusive ("TTC") except the formula, and no rate is stated; the residential
rate the law sets reproduces the variable card's printed price.

pdfplumber sets each table value on its own line after its label, so
:func:`_join_value_lines` puts every row back on one line before the shared
readers run. What the cards print, read as printed:

  - rows for Fluvius (Gaselwest), (Iveka), (Iverlek) and (Sibelgas), which
    no longer exist and carry transport only; they are ignored.
  - ORES proportional terms of 4,198 and 2,115 c EUR/kWh where the 2026 grid
    says 4,289 and 2,206. That grid with the regulatory balance term of CWaPE
    decision 1008, before decision 1146 of 16 October 2025 raised it, gives
    4,198 and 2,114.
  - RESA's mid-tier term 2,259, where the grid and every other card say
    2,529: most likely two digits swapped.
  - a single excise rate with no second band.
  - a Brussels levy table without the row above 160 m3/h.

The current three-page card starts with April 2026. The January and March
2026 fixed cards and the January 2025 variable one checked are an older
two-page template this module does not read, nor are the second June 2026
cards of Bolt Variable and Bolt Variable Online, whose Flanders rows and
levy column are missing from their text; such a month has no card here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    DSO_RESA,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    REGIONS,
    VAT_RATE_REDUCED,
)
from ._network import (
    FLUVIUS_LABELS,
    METERING,
    ORES_LABELS,
    T1_FIXED,
    T1_PROP,
    T2_FIXED,
    T2_PROP,
    TRANSPORT,
    excise_bands,
    osp_table,
    read_dsos,
    require_region,
)
from ._parse import (
    SIGN_CHARS,
    cell_value,
    fold_accents,
    month_date,
    parse_sign,
    require_contract,
    to_float,
)
from ._pdf import (
    MONTH_NAMES,
    fetch_pdf_text,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    is_transient_fetch_error,
)
from ._rates import Contract, FixedRates, IndexedRates, TariffKind
from ._validity import card_or_none, end_of_month, month_card
from .base import (
    DsoOverlay,
    ExtractorError,
    IndexTable,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_BASE_URL = "https://files.boltenergie.be/pricelists"
_LISTING_URL = "https://www.boltenergie.be/fr/listes-des-prix"
_CARD_RE = re.compile(r"pricelists/(fix|var)/([a-z_]+)_res_ng_fr_(\d+)\.pdf")

# The index the variable cards name, and the name fetch_index files it under.
_INDEX = "TTF"

# A card is about 1.7 MB and Bolt's CDN is slow now and then.
_PDF_TIMEOUT = 60


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    folder: str  # "fix", by month, or "var", by version
    slug: str  # file name prefix
    heading: str  # the product as the card's title prints it

    @property
    def kind(self) -> TariffKind:
        return "fixed" if self.folder == "fix" else "indexed"


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("bolt_fix", "Bolt Fixe", "fix", "fix", "Bolt Fixe"),
    _ContractDef("bolt_plenty_fix", "Bolt Plenty Fixe", "fix", "plenty_fix", "Plenty Fixe"),
    _ContractDef("bolt_variable", "Bolt Variable", "var", "bolt", "Bolt Variable"),
    _ContractDef(
        "bolt_variable_online", "Bolt Variable Online", "var", "online", "Bolt Variable Online"
    ),
    _ContractDef(
        "bolt_plenty_variable", "Bolt Plenty Variable", "var", "plenty", "Plenty Variable"
    ),
    _ContractDef(
        "bolt_plenty_variable_online",
        "Bolt Plenty Variable Online",
        "var",
        "plenty_online",
        "Plenty Variable Online",
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _card_url(contract: _ContractDef, suffix: str) -> str:
    return f"{_BASE_URL}/{contract.folder}/{contract.slug}_res_ng_fr_{suffix}.pdf"


async def _current_suffix(session: aiohttp.ClientSession, contract: _ContractDef) -> str:
    """The month or version of the card the listing links for ``contract``,
    the latest if it links several."""
    listing = await fetch_text(session, _LISTING_URL)
    suffixes: list[str] = [
        suffix
        for folder, slug, suffix in _CARD_RE.findall(listing)
        if (folder, slug) == (contract.folder, contract.slug)
    ]
    if not suffixes:
        raise ExtractorError(f"Bolt: the listing links no {contract.slug} gas card")
    return max(suffixes, key=int)


async def _current_url(session: aiohttp.ClientSession, contract: _ContractDef) -> str:
    return _card_url(contract, await _current_suffix(session, contract))


def _check_region(region: str) -> None:
    if region not in REGIONS:
        raise ExtractorError(f"Bolt: unknown region {region!r}")


async def probe(session: aiohttp.ClientSession, contract_id: str, region: str) -> str | None:
    """The current card's Last-Modified, which moves when Bolt reissues the
    card as well as when it links another one."""
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    try:
        url = await _current_url(session, contract)
    except ExtractorError:
        return None
    return await head_freshness_key(session, url)


async def fetch(session: aiohttp.ClientSession, contract_id: str, region: str) -> SupplierSnapshot:
    """The current card for ``contract_id``, read for ``region``."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Bolt")
    _check_region(region)
    return await _read(session, contract_id, region, await _current_url(session, contract))


async def _read(
    session: aiohttp.ClientSession, contract_id: str, region: str, url: str
) -> SupplierSnapshot:
    text = await fetch_pdf_text_layout(session, url, timeout=_PDF_TIMEOUT)
    return parse_snapshot(contract_id, region, text, source_url=url)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card in force in a past month, or None.

    A fixed card is addressed by its month, which answers 404 when Bolt has
    no card for it; a card naming another month is refused. A variable card
    is the newest version whose title names the month or an earlier one. A
    transient failure raises so the month cache retries it; any other
    failure, a card from the older template included, is a month with no
    card.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region not in REGIONS:
        return None
    if contract.folder == "fix":
        url = _card_url(contract, f"{year_month:%Y%m}")
        return await month_card(_read(session, contract_id, region, url), year_month)
    return await card_or_none(_read_in_force(session, contract, region, year_month))


async def _read_in_force(
    session: aiohttp.ClientSession, contract: _ContractDef, region: str, year_month: date
) -> SupplierSnapshot | None:
    url = await _url_in_force(session, contract, year_month)
    return None if url is None else await _read(session, contract.contract_id, region, url)


async def _url_in_force(
    session: aiohttp.ClientSession, contract: _ContractDef, year_month: date
) -> str | None:
    """The variable card in force at the end of ``year_month``.

    Walks down from the version the listing links, reading each title with
    pypdf, several times faster than the layout reader. A number Bolt
    skipped (Plenty has no 12) answers 404 and is passed over; a title the
    module does not read stops the walk, since the older versions are of the
    older template too.
    """
    for version in range(int(await _current_suffix(session, contract)), 0, -1):
        url = _card_url(contract, str(version))
        try:
            text = await fetch_pdf_text(session, url, timeout=_PDF_TIMEOUT)
        except ExtractorError as err:
            if is_transient_fetch_error(str(err)):
                raise
            continue
        if _card_month(text, contract) <= year_month:
            return url
    return None


# A line holding nothing but figures and dashes: the values pdfplumber sets
# on their own lines after the label they belong to.
_VALUES_ONLY_RE = re.compile(r"^\s*(?:\d+(?:[.,]\d+)*|-)(?:\s+(?:\d+(?:[.,]\d+)*|-))*\s*$")


def _join_value_lines(text: str) -> str:
    """``text`` with every line of bare values appended to the line above.

    The card's glyph stream breaks the line after each value, so a row comes
    out as its label and then one value per line, blank lines between rows.
    Rejoined, "Fluvius Imewo\\n 2,628\\n 18,61\\n ..." reads "Fluvius Imewo
    2,628 18,61 ...", which is the shape the shared readers expect.
    """
    lines: list[str] = []
    for line in text.replace("\u2028", "\n").splitlines():
        if not line.strip():
            continue
        if lines and _VALUES_ONLY_RE.match(line):
            lines[-1] = f"{lines[-1]} {line.strip()}"
        else:
            lines.append(line)
    return "\n".join(lines)


def parse_snapshot(
    contract_id: str, region: str, text: str, *, source_url: str = _LISTING_URL
) -> SupplierSnapshot:
    """Parse the card's text as pdfplumber lays it out, for one region."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Bolt")
    _check_region(region)
    text = _join_value_lines(text)
    card_month = _card_month(text, contract)
    fixed = contract.kind == "fixed"
    return SupplierSnapshot(
        supplier="bolt",
        contract=contract_id,
        energy=_fixed(text) if fixed else _indexed(text),
        dsos=_dsos(text, region),
        taxes=_taxes(text, region),
        source_url=source_url,
        publication_label=f"{card_month:%Y-%m}",
        # A variable card is in force until Bolt publishes the next.
        valid_until=end_of_month(card_month.year, card_month.month) if fixed else None,
    )


# "Carte Tarifaire / Bolt Fixe / Septembre 2026 /Résidentiel /Gaz". The August
# 2026 card spells its month "Aôut", which folds to "aout" all the same.
_HEADING_RE = re.compile(r"Carte Tarifaire\s+(.+?)\s+(\S+)\s+(\d{4})\s*/Résidentiel\s*/Gaz")


def _card_month(text: str, contract: _ContractDef) -> date:
    """The month of the card's title, once the title names this product."""
    match = _HEADING_RE.search(text)
    if match is None:
        raise ExtractorError("Bolt: card title not found")
    if match.group(1) != contract.heading:
        raise ExtractorError(f"Bolt {contract.contract_id}: the card is for {match.group(1)}")
    month = MONTH_NAMES.get(fold_accents(match.group(2)))
    if month is None:
        raise ExtractorError(f"Bolt: card month {match.group(2)!r} not understood")
    return month_date(match.group(3), month, "Bolt")


_PRICE_RE = re.compile(r"Prix mensuel\s+(\d+,\d+)")
_FEE_RE = re.compile(r"€\s*(\d+(?:,\d+)?)\s*/\s*mois")


def _price_and_fee(text: str) -> tuple[float, float]:
    """The printed price in EUR/kWh and the platform fee per year."""
    price = _PRICE_RE.search(text)
    fee = _FEE_RE.search(text)
    if price is None or fee is None:
        raise ExtractorError("Bolt: price or platform fee not found")
    return to_float(price.group(1)) / 100.0, to_float(fee.group(1)) * 12.0


def _fixed(text: str) -> FixedRates:
    price, fee = _price_and_fee(text)
    return FixedRates(price=price, yearly_fixed_fee=fee)


# "Simple 65,38 €/MWh TTF *1,049 + 10,10" under "Type de compteur TTF Q3 2026
# Formule tarifaire (€/MWh, HTVA)": the TTF value the printed price is set at,
# then the formula.
_FORMULA_RE = re.compile(
    r"^Simple\s+\d+,\d+\s*€/MWh\s+"
    r"(TTF\s*\*\s*(\d+,\d+)\s*([" + SIGN_CHARS + r"])\s*(\d+,\d+))\s*$",
    re.MULTILINE,
)


def _indexed(text: str) -> IndexedRates:
    formula = _FORMULA_RE.search(text)
    if formula is None:
        raise ExtractorError("Bolt: variable card formula not found")
    price, fee = _price_and_fee(text)
    # The card states no VAT rate; the residential one the law sets is what
    # reproduces its printed price. EUR/MWh to EUR/kWh, grossed.
    vat = 1.0 + VAT_RATE_REDUCED
    base = parse_sign(formula.group(3)) * to_float(formula.group(4))
    return IndexedRates(
        factor=to_float(formula.group(2)) / 1000.0 * vat,
        base=base / 1000.0 * vat,
        index=_INDEX,
        price=price,
        yearly_fixed_fee=fee,
        formula=formula.group(1),
    )


# Per tier the proportional term then the fixed one, then transport and the
# metering fee ("-" in Wallonia).
_COLUMNS = (T1_PROP, T1_FIXED, T2_PROP, T2_FIXED, TRANSPORT, METERING)
_LABELS: dict[str, dict[str, str]] = {
    REGION_FLANDERS: FLUVIUS_LABELS,
    REGION_WALLONIA: {**ORES_LABELS, "TECTEO RESA": DSO_RESA},
    REGION_BRUSSELS: {"SIBELGA": DSO_SIBELGA},
}


def _dsos(text: str, region: str) -> dict[str, DsoOverlay]:
    dsos = read_dsos(
        text,
        _LABELS[region],
        _COLUMNS,
        supplier="Bolt",
        after="Distribution et transport",
        before="Taxes et redevances",
    )
    require_region(dsos, region, "Bolt")
    return dsos


# A levy row: its label, a footnote digit on some, then one value per region
# (Flandres, Wallonie, Bruxelles), each a figure with decimals or a dash. A
# row that lost a value does not match, rather than shifting the footnote
# digit into the Flanders column.
_TAX_VALUE = r"[ \t]+(\d+,\d+|-)"
_TAX_COLUMN = {REGION_FLANDERS: 0, REGION_WALLONIA: 1, REGION_BRUSSELS: 2}


def _tax_cell(text: str, label: str, region: str) -> float | None:
    """The region's value on the levy row ``label``, None for a dash."""
    match = re.search(rf"^{label}(?:[ \t]+\d)?{_TAX_VALUE * 3}[ \t]*$", text, re.MULTILINE)
    if match is None:
        raise ExtractorError(f"Bolt: levy row {label!r} not found")
    return cell_value(match.group(1 + _TAX_COLUMN[region]))


def _taxes(text: str, region: str) -> TaxOverlay:
    excise = _tax_cell(text, r"Accise fédérale \(c€/kWh\)", region)
    if excise is None:
        raise ExtractorError("Bolt: no excise for the region")
    # A dash is the card saying no contribution, which the law made so from
    # August 2026; the row itself is still required.
    contribution = _tax_cell(text, r"Contribution sur l.énergie \(c€/kWh\)", region)
    connection_fee = 0.0
    if region == REGION_WALLONIA:
        fee = _tax_cell(text, r"Redevance de raccordement \(c€/kWh\)", region)
        if fee is None:
            raise ExtractorError("Bolt: Walloon connection fee not found")
        connection_fee = fee / 100.0
    return TaxOverlay(
        excise_bands=excise_bands(excise / 100.0, None),
        energy_contribution=(contribution or 0.0) / 100.0,
        connection_fee=connection_fee,
        osp_by_caliber=_osp(text) if region == REGION_BRUSSELS else None,
    )


# "6 of 10 m3/h 4 3,56", "25m3/h 75,18": the caliber, a footnote digit on the
# two smallest-caliber rows, the amount.
_OSP_ROW_RE = re.compile(r"^(?:6 of 10|\d+) ?m3/h(?:[ \t]+\d)?[ \t]+(\d+,\d+)[ \t]*$", re.MULTILINE)


def _osp(text: str) -> dict[str, float]:
    start = text.find("Obligations de service publique")
    if start < 0:
        raise ExtractorError("Bolt: Brussels levy table not found")
    end = text.find("Informations sur les éléments", start)
    block = text[start:] if end < 0 else text[start:end]
    amounts = [to_float(value) for value in _OSP_ROW_RE.findall(block)]
    return osp_table(amounts, supplier="Bolt")


# ---- index values ------------------------------------------------------------

_HISTORY_KEY = '"priceHistory":'


def parse_index(listing: str, today: date) -> IndexTable:
    """The listing's monthly gas series in EUR/MWh, the months before
    ``today``'s only.

    The series sits in the page's data as {"date": ..., "price": ...} rows,
    each dated the first of its month at midnight in Belgium ("2026-08-31T
    22:00:00+00:00" is September) and priced in EUR/kWh. The running month
    and the ones after hold forward values, not what was billed.
    """
    start = listing.find(_HISTORY_KEY)
    if start < 0:
        raise ExtractorError("Bolt: the listing has no price history")
    try:
        history, _ = json.JSONDecoder().raw_decode(listing, start + len(_HISTORY_KEY))
    except ValueError as err:
        raise ExtractorError(f"Bolt: price history unreadable: {err}") from err
    rows = history.get("gas") if isinstance(history, dict) else None
    if not isinstance(rows, list):
        raise ExtractorError("Bolt: the price history has no gas series")
    current = f"{today:%Y-%m}"
    values: dict[str, float] = {}
    for row in rows:
        stamp = row.get("date") if isinstance(row, dict) else None
        price = row.get("price") if isinstance(row, dict) else None
        if (
            not isinstance(stamp, str)
            or not isinstance(price, int | float)
            or isinstance(price, bool)
        ):
            raise ExtractorError(f"Bolt: unexpected gas price history row {row!r}")
        try:
            month = f"{dt_util.as_local(datetime.fromisoformat(stamp)):%Y-%m}"
        except ValueError as err:
            raise ExtractorError(f"Bolt: unexpected gas price history date {stamp!r}") from err
        if month < current:
            values[month] = price * 1000.0
    if not values:
        raise ExtractorError("Bolt: the gas price history holds no past month")
    return {_INDEX: values}


async def fetch_index(session: aiohttp.ClientSession) -> IndexTable:
    """The monthly means Bolt publishes for the months that are over."""
    return parse_index(await fetch_text(session, _LISTING_URL), dt_util.now().date())


EXTRACTOR = SupplierExtractor(
    id="bolt",
    label="Bolt",
    contracts=tuple(Contract(id=c.contract_id, label=c.label, kind=c.kind) for c in _CONTRACTS),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
    fetch_index=fetch_index,
    sweep_cost_s=45.0,
)
