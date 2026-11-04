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

"""Look for a supplier's card where its extractor did not find it.

The live check runs this for a card that failed to fetch or is still the
month before's. It reads the pages the supplier lists its cards on, keeps the
addresses that name the product, and downloads each to see whether it names
the running month. It only reports: the address it gives is for a person to
check and adopt, and nothing here changes what the integration reads.

A card that moved is found where the supplier itself lists it. In November
2026 Elegant renamed its files, Engie gave Empty House a new document code,
EnergyVision put its uploads in month folders, Luminus gave BasicFlex a new
product id, Belvus linked its cards from its services page alone and Dots
moved its card to a new site, and each new address stood on the supplier's
own pages before any extractor read it.

Each supplier publishes its own way, so each has a short function naming
its candidates, with where each came from:

* Elegant's listing links every product's card by name;
* Engie's prices page names every residential document code;
* EnergyVision's tariefkaart page links its files, and its uploads sit in
  the month's folder, the month before's or ``inline-files``;
* Luminus's price-list archive gives each month's product id, so the
  month's id and the month before's are asked for the month's card;
* Belvus links each month's cards from its services page and its history;
* Dots's product page links or embeds the card.

Usage: python scripts/card_finder.py <supplier> <contract> <region> [YYYY-MM]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin
from zoneinfo import ZoneInfo

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.be_gas_prices.providers import (  # noqa: E402
    belvus,
    dots,
    elegant,
    energyvision,
    engie,
    luminus,
)
from custom_components.be_gas_prices.providers._parse import fold_accents  # noqa: E402
from custom_components.be_gas_prices.providers._pdf import (  # noqa: E402
    MONTH_NAMES,
    fetch_pdf_text,
    fetch_text,
)
from custom_components.be_gas_prices.providers.base import ExtractorError  # noqa: E402

# The rows a missing or moved card fails: the check named after the label's
# colon, on a "<supplier>/<contract>/<region>" label.
_TRIGGERS = frozenset({"fetch", "card month"})
# The candidates downloaded for one card, best first.
_MAX_TRIED = 4
# One card's whole search, pages and downloads.
_SEARCH_TIMEOUT_S = 120.0

# A link as markup carries it.
_HREF_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"'<>]+)["']""", re.IGNORECASE)

# Elegant's listing spells each card "/Current/<product> Gas_Residential.pdf",
# with its electricity cards beside them.
_ELEGANT_LISTED_RE = re.compile(r"/Current/([^\"'<>/]+?)_Residential\.pdf")

# Engie's residential prices pages, which name every document code the
# document API serves, "G_EMPTYHOUSE_R_GREY_C_I_36_V_F".
_ENGIE_PAGES = (
    "https://www.engie.be/fr/energie/electricite-gaz/prix-conditions/",
    "https://www.engie.be/nl/energie/elektriciteit-gas/prijzen-voorwaarden/",
)
_ENGIE_CODE_RE = re.compile(r"\bG_([A-Z0-9]+)_R_GREY_C_([A-Z])_(\d{2})_([VWB])_F\b")

# EnergyVision's page that links its files, where the tariefkaart listing
# builds them in a script.
_ENERGYVISION_PAGE = "/nl-be/tariefkaart-2"

# Belvus's cards, "/public/tariefkaarten/2026-11/Tariefkaart_FlexOnline_GAS.pdf".
_BELVUS_CARD_RE = re.compile(
    r"/public/tariefkaarten/(\d{4})-(\d{2})/Tariefkaart_([A-Za-z0-9]+)_GAS\.pdf"
)

# Dots's product page, at its new address first; the old one redirects.
_DOTS_PAGES = ("/nl/tariefkaarten/dots-connect-gas-digital", "/dots-connect-gas-digital")
# What Dots has linked its card as: an Odoo attachment, then its card API.
_DOTS_CARD_RE = re.compile(r"/(?:web/content/\d+|api/tariefkaarten/[\w-]+)")

Candidates = list[tuple[str, str]]
Finder = Callable[[aiohttp.ClientSession, str, str, date], Awaitable[Candidates]]


def _squash(name: str) -> str:
    """A product name with its spaces, signs and case taken out."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _previous(month: date) -> date:
    return (month.replace(day=1) - timedelta(days=1)).replace(day=1)


async def _page(session: aiohttp.ClientSession, url: str) -> str:
    """A page's text, or nothing when it cannot be read: the search goes on
    with the supplier's other pages."""
    try:
        return unescape(await fetch_text(session, url))
    except ExtractorError:
        return ""


async def _elegant(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    contract = elegant._CONTRACTS_BY_ID[contract_id]
    page = (await fetch_text(session, elegant._LISTING_URL)).replace("\\u0026", "&")
    # The archive key is the listed name with its spaces and "&" taken out.
    wanted = _squash(contract.archive_key)
    return [
        (f"{elegant._CURRENT_URL}/{quote(name)}_Residential.pdf", "Elegant's listing")
        for name in dict.fromkeys(_ELEGANT_LISTED_RE.findall(unescape(page)))
        if _squash(name) == wanted
    ]


async def _engie(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    contract = engie._CONTRACTS_BY_ID[contract_id]
    area = engie._REGION_TO_CODE[region]
    exact: dict[str, None] = {}
    family: dict[str, None] = {}
    for url in _ENGIE_PAGES:
        for m in _ENGIE_CODE_RE.finditer(await _page(session, url)):
            name, rate, _months, where = m.groups()
            if (name, where) != (contract.family, area):
                continue
            # The same rate is the same product under a new term; another is
            # a sibling product, offered only when nothing closer is.
            (exact if rate == contract.rate else family)[m.group(0)] = None
    return [
        (
            f"{engie._API_URL}?document={code}&monthOffset=0&segment=R&language=F",
            "Engie's prices page",
        )
        for code in exact or family
    ]


async def _energyvision(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    contract = energyvision._CONTRACTS_BY_ID[contract_id]
    card = contract.cards[region]
    named = re.compile(
        rf"/EV-{month:%m%y}-{re.escape(contract.code)}-{re.escape(card.token)}"
        r"(?:_\d+)?\.pdf$",
        re.IGNORECASE,
    )
    page_url = card.page or f"{card.site}{_ENERGYVISION_PAGE}"
    found: dict[str, str] = {}
    for link in _HREF_RE.findall(await _page(session, page_url)):
        url = urljoin(page_url, link)
        if named.search(url):
            found.setdefault(url, "its card page")
    # Where its listing's script looks for the month's name.
    for url in energyvision._month_urls(contract, card, month):
        found.setdefault(url, "its upload folders")
    return list(found.items())


async def _luminus(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    contract = luminus._CONTRACTS_BY_ID[contract_id]
    tab = luminus._REGION_TO_TAB[region]
    found: dict[str, str] = {}
    for signing in (month, _previous(month)):
        try:
            product_id = await luminus._archive_product_id(
                session, contract, tab, f"{signing:%Y-%m}"
            )
        except ExtractorError:
            continue
        if product_id is not None:
            url = luminus._archive_card_url(product_id, month, tab)
            found.setdefault(url, f"the product id of {signing:%Y-%m}")
    return list(found.items())


async def _belvus(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    contract = belvus._CONTRACTS_BY_ID[contract_id]
    found: dict[str, str] = {}
    for page_url, where in (
        (belvus._CURRENT_URL, "Belvus's services page"),
        (belvus._LISTING_URL, "Belvus's history page"),
    ):
        for m in _BELVUS_CARD_RE.finditer(await _page(session, page_url)):
            year, number, product = m.groups()
            if (int(year), int(number)) != (month.year, month.month):
                continue
            if _squash(product) == _squash(contract.file):
                found.setdefault(urljoin(page_url, m.group(0)), where)
    return list(found.items())


async def _dots(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> Candidates:
    found: dict[str, str] = {}
    for path in _DOTS_PAGES:
        page_url = f"{dots._HOST}{path}"
        for link in _HREF_RE.findall(await _page(session, page_url)):
            card = _DOTS_CARD_RE.search(urljoin(page_url, link))
            if card is not None:
                found.setdefault(urljoin(page_url, link), "Dots's product page")
    # One card under its plain address and its download one is one card.
    plain = {url.split("?")[0]: where for url, where in found.items() if "/api/" in url}
    rest = {url: where for url, where in found.items() if "/api/" not in url}
    return list({**plain, **rest}.items())


FINDERS: dict[str, Finder] = {
    "elegant": _elegant,
    "engie": _engie,
    "energyvision": _energyvision,
    "luminus": _luminus,
    "belvus": _belvus,
    "dots": _dots,
}

_CONTRACTS: dict[str, Mapping[str, Any]] = {
    "elegant": elegant._CONTRACTS_BY_ID,
    "engie": engie._CONTRACTS_BY_ID,
    "energyvision": energyvision._CONTRACTS_BY_ID,
    "luminus": luminus._CONTRACTS_BY_ID,
    "belvus": belvus._CONTRACTS_BY_ID,
    "dots": dots._CONTRACTS,
}


def _covered(supplier: str, contract_id: str) -> bool:
    return supplier in FINDERS and contract_id in _CONTRACTS[supplier]


def _month_re(month: date) -> re.Pattern[str]:
    """The month as a card names it, accents folded: "novembre 2026",
    "november 2026" or "11/2026"."""
    names = sorted(
        (name for name, number in MONTH_NAMES.items() if number == month.month),
        key=len,
        reverse=True,
    )
    return re.compile(
        rf"\b(?:{'|'.join(map(re.escape, names))})\s+{month.year}\b"
        rf"|\b{month.month:02d}/{month.year}\b",
        re.IGNORECASE,
    )


async def _reason(session: aiohttp.ClientSession, url: str, month: date) -> str | None:
    """Why the card at ``url`` is not ``month``'s, or None when it names it."""
    try:
        text = await fetch_pdf_text(session, url, timeout=30)
    except ExtractorError as err:
        return f"{type(err).__name__}: {err}"
    if _month_re(month).search(fold_accents(text)) is None:
        return f"names no {month:%Y-%m}"
    return None


async def find(
    session: aiohttp.ClientSession,
    supplier: str,
    contract_id: str,
    region: str,
    month: date,
) -> list[tuple[str, str, str | None]]:
    """Each candidate address for ``month``'s card, where it came from and
    why it is not that card, None when it is, best first."""
    candidates = await FINDERS[supplier](session, contract_id, region, month)
    return [
        (url, where, await _reason(session, url, month)) for url, where in candidates[:_MAX_TRIED]
    ]


async def describe(
    session: aiohttp.ClientSession,
    supplier: str,
    contract_id: str,
    region: str,
    month: date,
) -> str:
    """One line on what the search found, for a live-check row."""
    try:
        results = await asyncio.wait_for(
            find(session, supplier, contract_id, region, month), _SEARCH_TIMEOUT_S
        )
    except Exception as err:  # report only: a search that fails says so
        return f"card finder: the search failed: {type(err).__name__}: {err}"
    found = [f"{url} ({where})" for url, where, reason in results if reason is None]
    if found:
        return f"card finder: {month:%Y-%m}'s card at {', '.join(found)}"
    if not results:
        return f"card finder: {supplier}'s pages list no card for it"
    return (
        f"card finder: none of the {len(results)} addresses {supplier}'s pages "
        f"give names {month:%Y-%m}"
    )


async def annotate(checks: list[Any], session: aiohttp.ClientSession, today: date) -> list[Any]:
    """The checks, with what the search found added to every failing row of
    a card it covers that failed to fetch or is still the month before's.
    Report only: no row's status changes."""
    month = today.replace(day=1)
    rows: dict[tuple[str, str, str], list[int]] = {}
    for index, check in enumerate(checks):
        if check.status != "fail":
            continue
        head, _, what = check.label.partition(": ")
        parts = head.split("/")
        if what not in _TRIGGERS or len(parts) != 3:
            continue
        supplier, contract_id, region = parts
        if _covered(supplier, contract_id):
            rows.setdefault((supplier, contract_id, region), []).append(index)
    out = list(checks)
    for (supplier, contract_id, region), indexes in rows.items():
        note = await describe(session, supplier, contract_id, region, month)
        for index in indexes:
            check = out[index]
            out[index] = replace(check, detail=f"{check.detail}; {note}" if check.detail else note)
    return out


async def _main(args: argparse.Namespace) -> int:
    if not _covered(args.supplier, args.contract):
        print(f"no finder for {args.supplier}/{args.contract}", file=sys.stderr)
        return 2
    if args.month:
        month = datetime.strptime(args.month, "%Y-%m").date()
    else:
        month = datetime.now(ZoneInfo("Europe/Brussels")).date().replace(day=1)
    async with aiohttp.ClientSession() as session:
        results = await find(session, args.supplier, args.contract, args.region, month)
    for url, where, reason in results:
        print(f"{reason or 'names ' + format(month, '%Y-%m')}: {url} ({where})")
    return 0 if any(reason is None for _url, _where, reason in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("supplier")
    parser.add_argument("contract")
    parser.add_argument("region")
    parser.add_argument("month", nargs="?", help="YYYY-MM; the running month by default")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
