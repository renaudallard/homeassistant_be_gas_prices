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

"""A card that left its usual address, read where its supplier lists it now.

In November 2026 Elegant renamed its files, Engie gave Empty House a new
document code and Dots moved its card to a rebuilt site. Each new address
stood on the supplier's own pages, but the entries on those cards waited for
a release to read them.

``with_moved_fallbacks`` wraps the registered ``fetch`` of the suppliers
below. When the usual address has no card or its listing names none
(``CardNotListedError``), or it serves a card of a month before the running
one, it asks the supplier's pages for the product's card of the running
month and reads each address with the supplier's own reader, keeping the
first card that is that month's. Only an address that is the product's card
by construction is read: the same product under its own name or code, or a
card its own page links on the supplier's own site. A sibling product is no
such card. When nothing qualifies the usual answer stands, the error or the
older card.

Three suppliers are not among them. Luminus's ``fetch`` asks its archive for
the running month's card itself when the usual address has none. Belvus's
reads both pages that list its cards, which is where its November cards
were. EnergyVision's asks every folder its listing's script asks, newest
upload first, and its card page is no better guide: in November 2026 it
linked the first edition of a card a corrected upload had replaced.

The wrapped ``fetch`` keeps the name and module of the supplier's own,
which ``inspect.unwrap`` gives back. The live check asks that one, so it
keeps failing at the usual address until the extractor reads the card
there, and ``scripts/card_finder.py`` reports where the card went, from the
same candidates.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import replace
from datetime import date
from html import unescape
from itertools import islice
from urllib.parse import quote, urljoin

import aiohttp
from homeassistant.util import dt as dt_util

from . import dots, elegant, engie
from ._pdf import error_text, fetch_text, is_missing_card_error, on_site
from ._validity import end_of_month
from .base import (
    CardNotListedError,
    ExtractorError,
    SnapshotFetcher,
    SupplierExtractor,
    SupplierSnapshot,
)

_LOGGER = logging.getLogger(__name__)

# An address, where it came from, and whether it is the product's card for the
# month by construction.
Candidate = tuple[str, str, bool]
Candidates = Callable[[aiohttp.ClientSession, str, str, date], Awaitable[list[Candidate]]]
ReadAt = Callable[[aiohttp.ClientSession, str, str, str], Awaitable[SupplierSnapshot]]

# The addresses read for one card, best first.
_MAX_TRIED = 4

# The links or names of one page a search looks at, each once. In November
# 2026 Dots's page linked its card twice and Elegant's listing named six
# cards, while 8 MiB of markup can hold 600 000 links, which took over a
# minute to join on the event loop.
_MAX_LISTED = 100

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

# Dots's product page, at its new address first; the old one redirects.
_DOTS_PAGES = ("/nl/tariefkaarten/dots-connect-gas-digital", "/dots-connect-gas-digital")
# What Dots has linked its card as: an Odoo attachment, then its card API.
_DOTS_CARD_RE = re.compile(r"/(?:web/content/\d+|api/tariefkaarten/[\w-]+)")


def _squash(name: str) -> str:
    """A product name with its spaces, signs and case taken out."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _listed(found: Iterable[str]) -> list[str]:
    """What a page lists, each once and :data:`_MAX_LISTED` at most, spelt
    as the page spells it. Its entities are decoded once it is kept: decoding
    the page first took seconds on 8 MiB of them."""
    return list(islice(dict.fromkeys(found), _MAX_LISTED))


async def _page(session: aiohttp.ClientSession, url: str) -> str:
    """A page's text, or nothing when it cannot be read: the search goes on
    with the supplier's other pages."""
    try:
        return await fetch_text(session, url)
    except ExtractorError:
        return ""


async def _elegant(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> list[Candidate]:
    contract = elegant._CONTRACTS_BY_ID[contract_id]
    listed = _listed(_ELEGANT_LISTED_RE.findall(await fetch_text(session, elegant._LISTING_URL)))
    # The archive key is the listed name with its spaces and "&" taken out.
    wanted = _squash(contract.archive_key)
    return [
        (f"{elegant._CURRENT_URL}/{quote(name)}_Residential.pdf", "Elegant's listing", True)
        for name in dict.fromkeys(unescape(name.replace("\\u0026", "&")) for name in listed)
        if _squash(name) == wanted
    ]


async def _engie(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> list[Candidate]:
    contract = engie._CONTRACTS_BY_ID[contract_id]
    area = engie._REGION_TO_CODE[region]
    found: dict[str, bool] = {}
    for url in _ENGIE_PAGES:
        for m in _ENGIE_CODE_RE.finditer(await _page(session, url)):
            name, rate, _months, where = m.groups()
            if (name, where) == (contract.family, area):
                # The same rate is the same product under a new term; another
                # is a sibling product.
                found[m.group(0)] = rate == contract.rate
    return sorted(
        (
            (
                f"{engie._API_URL}?document={code}&monthOffset=0&segment=R&language=F",
                "Engie's prices page",
                same,
            )
            for code, same in found.items()
        ),
        key=lambda candidate: not candidate[2],
    )


async def _dots(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> list[Candidate]:
    found: dict[str, str] = {}
    for path in _DOTS_PAGES:
        page_url = f"{dots._HOST}{path}"
        links = _HREF_RE.findall(await _page(session, page_url))
        # Only a link that spells a card's address is joined.
        for link in _listed(filter(_DOTS_CARD_RE.search, links)):
            url = urljoin(page_url, unescape(link))
            # Dots's own site only: the card is no other host's.
            if on_site(url, dots._HOST) and _DOTS_CARD_RE.search(url) is not None:
                found.setdefault(url, "Dots's product page")
    # One card under its plain address and its download one is one card.
    plain = {url.split("?")[0]: where for url, where in found.items() if "/api/" in url}
    rest = {url: where for url, where in found.items() if "/api/" not in url}
    # The reader takes only the Flemish Connect - Digital card.
    return [(url, where, True) for url, where in {**plain, **rest}.items()]


# The suppliers covered, with the reader that reads a card at a given address
# as their ``fetch`` reads it and the candidates their pages give.
FALLBACKS: dict[str, tuple[ReadAt, Candidates]] = {
    "elegant": (elegant._read, _elegant),
    "engie": (engie._read, _engie),
    "dots": (dots._read, _dots),
}


def _is_month(snap: SupplierSnapshot, month: date) -> bool:
    """Whether the card is the one for ``month``, a first of the month."""
    return snap.valid_until == end_of_month(month.year, month.month)


async def read_moved(
    session: aiohttp.ClientSession,
    supplier: str,
    contract_id: str,
    region: str,
    month: date,
) -> SupplierSnapshot | None:
    """The product's card for ``month`` where its supplier's pages put it,
    or None when no address the pages give is that card, the search for one
    having failed included."""
    read_at, candidates = FALLBACKS[supplier]
    try:
        found = await candidates(session, contract_id, region, month)
    except Exception as err:
        # A page that cannot be read, or one the search trips over, such as
        # a link urljoin refuses: no address found, the usual answer stands.
        _LOGGER.debug(
            "%s/%s/%s: no moved card looked for: %s: %s",
            supplier,
            contract_id,
            region,
            type(err).__name__,
            error_text(err),
        )
        return None
    for url, where, _exact in [c for c in found if c[2]][:_MAX_TRIED]:
        try:
            snap = await read_at(session, contract_id, region, url)
        except ExtractorError:
            continue
        if _is_month(snap, month):
            _LOGGER.warning(
                "%s/%s/%s: the card is not at its usual address; read it at %s "
                "(found on %s) until a release reads it there",
                supplier,
                contract_id,
                region,
                url,
                where,
            )
            return snap
    return None


def _with_moved_fallback(supplier: str, fetch: SnapshotFetcher) -> SnapshotFetcher:
    # Under the supplier's own name and module, which the live check tells
    # the readers apart by, and with ``__wrapped__``, which gives its own
    # fetch back.
    @functools.wraps(fetch)
    async def fetch_or_moved(
        session: aiohttp.ClientSession, contract_id: str, region: str
    ) -> SupplierSnapshot:
        month = dt_util.now().date().replace(day=1)
        try:
            snap = await fetch(session, contract_id, region)
        except ExtractorError as err:
            if not (is_missing_card_error(str(err)) or isinstance(err, CardNotListedError)):
                raise
            moved = await read_moved(session, supplier, contract_id, region, month)
            if moved is None:
                raise
            return moved
        if snap.valid_until is None or snap.valid_until >= month:
            return snap
        return await read_moved(session, supplier, contract_id, region, month) or snap

    return fetch_or_moved


def with_moved_fallbacks(
    extractors: Mapping[str, SupplierExtractor],
) -> dict[str, SupplierExtractor]:
    """The registry with the covered suppliers' ``fetch`` wrapped."""
    return {
        key: (
            replace(extractor, fetch=_with_moved_fallback(key, extractor.fetch))
            if key in FALLBACKS
            else extractor
        )
        for key, extractor in extractors.items()
    }
