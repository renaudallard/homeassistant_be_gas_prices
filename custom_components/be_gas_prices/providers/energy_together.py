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

"""Energy Together gas card extractors: HOA Energy, Servolt Energie, Evident
Energie, Power2You, Prijspunten Energie and Smappee Smiles.

The six are brands of one supplier, Energy Together bv, run on one Odoo
module with one card template (read by :mod:`._energy_together`). Each is a
supplier of its own to a household, so each gets its own extractor, all
Flanders only and all indexed monthly on TTF_RLP.

Each brand lists its products on ``https://<host>/products``: one block per
product group, an ``<h3>`` with the group's name and the links to its cards,

    <a href="/web/content/xx.product.group.tariff.card/<id>/file" ...>
        <span class="fa fa-chevron-right"></span>
        <span>Tariefkaart_NOVA_NG</span>

where the gas card's name ends in "_NG" or "_Gas". Several groups share one
gas card (HOA's Volt and Nova both link NOVA_NG) under different ids of
byte-identical files, and the names drift (Power2You went from B2C_VAR_NG to
B2C_VAR_GAS to Variabel_Gas), so a contract is keyed on its product group
and the product the card prints is checked. The ids change every month.

``https://<host>/products/history`` is the archive: an ``<h2>`` per group,
then one row per card with its month ("september 2026"), newest first. Some
months carry two rows; the first is the later upload.

The index values are on the brand's "Indexatieparameters" page, which links
the platform's one publication (``/web/content/<id>``): a PDF with a table
"TTF_RLP (c€/kWh)" by month and year. It is the same document on every
brand's host, and a brand's page has linked an attachment that no longer
existed, so the other brands' pages are asked when a brand's own link fails.

The card is dated by its own month, and TTF_RLP is the RLP-weighted mean of
the delivery month's day-ahead prices: only known once the month is over, so
the printed price is the formula at the previous month's value.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from functools import partial

import aiohttp

from ..const import REGION_FLANDERS
from ._energy_together import INDEX, parse_card
from ._parse import elements, fold_accents, in_tag, require_contract, to_float
from ._pdf import NL_MONTHS, fetch_pdf_text, fetch_text, is_transient_fetch_error
from ._rates import Contract
from ._validity import future_month, month_card
from .base import ExtractorError, IndexTable, SupplierExtractor, SupplierSnapshot


@dataclass(frozen=True)
class _Brand:
    id: str
    label: str
    host: str


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    brand: _Brand
    group: str  # the product group on /products and /products/history
    product: str  # the product as the card names it, "" for none


_HOA = _Brand("hoa_energy", "HOA Energy", "www.hoa.energy")
_SERVOLT = _Brand("servolt", "Servolt Energie", "www.servoltenergie.be")
_EVIDENT = _Brand("evident", "Evident Energie", "www.evidentenergie.be")
_POWER2YOU = _Brand("power2you", "Power2You", "www.power2you.be")
_PRIJSPUNTEN = _Brand("prijspunten", "Prijspunten Energie", "www.prijspuntenenergie.be")
_SMAPPEE = _Brand("smappee_smiles", "Smappee Smiles", "www.smappeesmiles.be")
_BRANDS = (_HOA, _SERVOLT, _EVIDENT, _POWER2YOU, _PRIJSPUNTEN, _SMAPPEE)

# One contract per gas card. The groups not listed share a listed card: HOA's
# Volt (Nova) and Prime Plus (Prime), Servolt's Dynamic Control (Control),
# Evident's Dynamisch (Flexi), Power2You's Dynamic (Variabel) and Flow (Flex),
# Prijspunten's Comfortflex (Marktflex), Smappee's Lite, Smart and Sunplus
# (Variabel Smiles).
_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("hoa_energy_nova", "HOA Energy Nova", _HOA, "Nova", "NOVA"),
    _ContractDef("hoa_energy_prime", "HOA Energy Prime", _HOA, "Prime", "PRIME"),
    _ContractDef(
        "hoa_energy_apex_online", "HOA Energy APEX Online", _HOA, "APEX Online", "APEX Online"
    ),
    _ContractDef("servolt_control", "Servolt Control", _SERVOLT, "Control", "CONTROL"),
    _ContractDef("servolt_comfort", "Servolt Comfort", _SERVOLT, "Comfort", "COMFORT"),
    _ContractDef("servolt_solar", "Servolt Solar", _SERVOLT, "Servolt Solar", "SERVOLT SOLAR"),
    # The Evident card names no product.
    _ContractDef("evident_flexi", "Evident Energie Flexi", _EVIDENT, "Flexi", ""),
    _ContractDef("power2you_variabel", "Power2You Variabel", _POWER2YOU, "Variabel", "VARIABEL"),
    _ContractDef("power2you_flex", "Power2You Flex", _POWER2YOU, "Flex", "FLEX"),
    _ContractDef(
        "prijspunten_marktflex",
        "Prijspunten Energie Marktflex",
        _PRIJSPUNTEN,
        "Marktflex",
        "Marktflex",
    ),
    _ContractDef(
        "smappee_smiles_variabel",
        "Smappee Smiles Variabel",
        _SMAPPEE,
        "Variabel",
        "VARIABEL SMILES",
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _clean(fragment: str) -> str:
    return " ".join(html.unescape(fragment).split())


def _same(a: str, b: str) -> bool:
    return fold_accents(_clean(a)) == fold_accents(_clean(b))


def _is_gas(name: str) -> bool:
    return re.search(r"_(?:NG|GAS)$", name.strip(), re.IGNORECASE) is not None


# A card's link and the name after it, read with _parse.in_tag: one pattern
# running on through "[^>]*>" to the end of the link's tag scanned to the
# next ">" again from every link, and 440 kB of links that no ">" followed
# took 5,2 s, four times what half of them took.
_LINK_OPEN_RE = re.compile(r'href="(/web/content/xx\.product\.group\.tariff\.card/\d+/file)"')
_LINK = r">\s*<span[^>]*>\s*</span>\s*<span>([^<]*)</span>"
# A product group of /products is its "<h3>" heading and what follows it up
# to the next, and a group of /products/history its "<h2>" heading and what
# follows it up to the next: found from heading to heading (_groups), where
# "<h3>(.*?)</h3>(.*?)(?=<h3>|\Z)" looked to the end of the page again from
# every heading that no closing followed.
_GROUP_RE = re.compile("<h3>")
_GROUP_END_RE = re.compile("</h3>")
_LINK_RE = re.compile(_LINK)
_HISTORY_GROUP_RE = re.compile("<h2")
_HISTORY_GROUP_END_RE = re.compile("</h2>")
_HISTORY_ROW_RE = re.compile(_LINK + r"\s*</a>\s*</td>\s*<td[^>]*>\s*<span>([^<]*)</span>")


def _groups(
    page: str, heading: re.Pattern[str], closing: re.Pattern[str], *, attributes: bool
) -> Iterator[tuple[str, str]]:
    """Each group of ``page``: its heading's text, and what follows the
    heading up to the next ``heading`` or the end of the page."""
    for _opened, name, closed in elements(page, heading, closing, attributes=attributes):
        following = heading.search(page, closed.end())
        yield name, page[closed.end() : len(page) if following is None else following.start()]


def current_card_path(page: str, contract: _ContractDef) -> str:
    """The path of the gas card the product group links on /products."""
    for name, body in _groups(page, _GROUP_RE, _GROUP_END_RE, attributes=False):
        if not _same(name, contract.group):
            continue
        gas: list[str] = [
            opened[1] for opened, card in in_tag(body, _LINK_OPEN_RE, _LINK_RE) if _is_gas(card[1])
        ]
        if len(gas) != 1:
            raise ExtractorError(
                f"{contract.brand.label}: group {contract.group!r} links {len(gas)} gas cards"
            )
        return gas[0]
    raise ExtractorError(f"{contract.brand.label}: product group {contract.group!r} not listed")


def archived_card_path(page: str, contract: _ContractDef, year_month: date) -> str | None:
    """The path of the gas card the archive lists for ``year_month``, or None."""
    wanted = f"{NL_MONTHS[year_month.month - 1]} {year_month.year}"
    for name, body in _groups(page, _HISTORY_GROUP_RE, _HISTORY_GROUP_END_RE, attributes=True):
        if not _same(name, contract.group):
            continue
        for opened, row in in_tag(body, _LINK_OPEN_RE, _HISTORY_ROW_RE):
            if _is_gas(row[1]) and _same(row[2], wanted):
                return opened[1]
        return None
    return None


def _contract(contract_id: str, region: str) -> _ContractDef:
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Energy Together")
    if region != REGION_FLANDERS:
        raise ExtractorError(f"{contract.brand.label} {contract_id}: not sold in region {region!r}")
    return contract


def parse_snapshot(contract_id: str, region: str, text: str, source_url: str) -> SupplierSnapshot:
    """Parse one card's text as pypdf extracts it."""
    contract = _contract(contract_id, region)
    return parse_card(
        text,
        supplier=contract.brand.id,
        label=contract.brand.label,
        contract=contract_id,
        product=contract.product,
        source_url=source_url,
    )


async def _read(
    session: aiohttp.ClientSession, contract: _ContractDef, path: str
) -> SupplierSnapshot:
    url = f"https://{contract.brand.host}{path}"
    return parse_snapshot(
        contract.contract_id, REGION_FLANDERS, await fetch_pdf_text(session, url), url
    )


async def fetch(session: aiohttp.ClientSession, contract_id: str, region: str) -> SupplierSnapshot:
    """The card the product group links today."""
    contract = _contract(contract_id, region)
    page = await fetch_text(session, f"https://{contract.brand.host}/products")
    return await _read(session, contract, current_card_path(page, contract))


async def _read_month(
    session: aiohttp.ClientSession, contract: _ContractDef, year_month: date
) -> SupplierSnapshot | None:
    page = await fetch_text(session, f"https://{contract.brand.host}/products/history")
    path = archived_card_path(page, contract, year_month)
    return None if path is None else await _read(session, contract, path)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card the brand's archive lists for a past month, or None.

    A month ahead of today in Home Assistant's zone is not asked for.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region != REGION_FLANDERS:
        return None
    if future_month(year_month):
        return None
    return await month_card(_read_month(session, contract, year_month), year_month)


# The publication's link, in its tag as the cards' are.
_INDEX_LINK_OPEN_RE = re.compile(r'href="(/web/content/\d+)"')
_INDEX_LINK_RE = re.compile(r">\s*De indexatieparameters")
# "GAS: TTF_RLP", then the row of years "MAAND 2025 2026", then the table up
# to "De parameter TTF_RLP", each looked for after the one before: one
# pattern lazily spanning them looked to the end of the text again from every
# row of years, in time growing with the cube of the text.
_INDEX_TABLE_RE = re.compile(r"GAS:\s*TTF_RLP")
_INDEX_YEARS_RE = re.compile(r"MAAND((?:[ \t]+20\d{2})+)[ \t]*\n")
_INDEX_TABLE_END = "De parameter TTF_RLP"
_INDEX_ROW_RE = re.compile(r"^\s*([A-Za-z]+)((?:[ \t]+\d+,\d+)*)[ \t]*$")


def parse_index_publication(text: str) -> IndexTable:
    """The "GAS: TTF_RLP" table of the publication, in EUR/MWh.

    One column per year and one row per month, in c EUR/kWh. A month not
    known yet has no figure in its year's column, which is always the last.
    It has two decimals where the cards' footnotes have three to five, and
    is sometimes cut and sometimes rounded (January 2026: 34,058 EUR/MWh on
    the cards, 3,40 here; July 2026: 53,07468 and 5,31), within 0,1 EUR/MWh
    either way.
    """
    table = _INDEX_TABLE_RE.search(text)
    heading = None if table is None else _INDEX_YEARS_RE.search(text, table.end())
    end = -1 if heading is None else text.find(_INDEX_TABLE_END, heading.end())
    if heading is None or end < 0:
        raise ExtractorError("Energy Together: TTF_RLP table not found")
    years = heading.group(1).split()
    values: dict[str, float] = {}
    for line in text[heading.end() : end].splitlines():
        row = _INDEX_ROW_RE.match(line)
        if row is None or row.group(1).lower() not in NL_MONTHS:
            continue
        month = NL_MONTHS.index(row.group(1).lower()) + 1
        figures = row.group(2).split()
        if len(figures) > len(years):
            raise ExtractorError(
                f"Energy Together: TTF_RLP row {row.group(1)} has too many figures"
            )
        for year, figure in zip(years, figures, strict=False):
            values[f"{year}-{month:02d}"] = to_float(figure) * 10.0
    if not values:
        raise ExtractorError("Energy Together: TTF_RLP table is empty")
    return {INDEX: values}


async def _publication(session: aiohttp.ClientSession, host: str) -> IndexTable:
    page = await fetch_text(session, f"https://{host}/indexatieparameters")
    link = next(in_tag(page, _INDEX_LINK_OPEN_RE, _INDEX_LINK_RE), None)
    if link is None:
        raise ExtractorError(f"Energy Together: no index publication linked on {host}")
    return parse_index_publication(await fetch_pdf_text(session, f"https://{host}{link[0][1]}"))


async def _fetch_index(brand: _Brand, session: aiohttp.ClientSession) -> IndexTable:
    """The brand's own publication, or the platform's through another brand's
    page when the brand's link is broken.

    When every page fails, a transient failure is raised in preference to
    the brand's own, so the caller retries rather than gives up.
    """
    hosts = (brand.host, *(other.host for other in _BRANDS if other is not brand))
    errors: list[ExtractorError] = []
    for host in hosts:
        try:
            return await _publication(session, host)
        except ExtractorError as err:
            errors.append(err)
    transient = [err for err in errors if is_transient_fetch_error(str(err))]
    raise (transient or errors)[0]


def _extractor(brand: _Brand) -> SupplierExtractor:
    return SupplierExtractor(
        id=brand.id,
        label=brand.label,
        contracts=tuple(
            Contract(
                id=c.contract_id,
                label=c.label,
                kind="indexed",
                regions=frozenset({REGION_FLANDERS}),
            )
            for c in _CONTRACTS
            if c.brand is brand
        ),
        fetch=fetch,
        fetch_for_month=fetch_for_month,
        fetch_index=partial(_fetch_index, brand),
        sweep_cost_s=0.5,
    )


EXTRACTORS: tuple[SupplierExtractor, ...] = tuple(_extractor(brand) for brand in _BRANDS)
