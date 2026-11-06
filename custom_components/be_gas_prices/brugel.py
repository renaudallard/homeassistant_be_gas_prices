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

"""Brugel's published Brussels gas distribution tariffs.

Sibelga's regulated gas network charges are set by Brugel and published once
a year as one small PDF, "Grille tarifaire - Gaz / Distribution Gaz Année
<year>", stating "prix hors TVA", at

    https://brugel.brussels/publication/document/notype/<year-1>/fr/Tarif-distribution-Gaz-<year>.pdf

Read for a custom Brussels entry that takes its network off the sheet
(:func:`cached_network`). Every row ends in one figure per tier, T1 to T5:
the fixed term of the network use ("X = EUR / an"), its rate per kWh ("Y =
EUR / kWh"), and below them the public service, regulatory balance, pension
and levy rates per kWh, which the cards add into each tier's rate. A
household reading its meter once a year pays the "Comptage YMR" term. The
yearly figures are marked indicative, the daily ones beside them being what
is billed, and are those every card prints.

Nothing is kept over a restart. A year whose sheet is not served yet stands
on the latest earlier one held, and the tick reads the year before's while
it is not. Never raises: the caller is the coordinator tick.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Final

import aiohttp
from homeassistant.util import dt as dt_util

from .providers._pdf import fetch_pdf_bytes
from .providers._pdf_render import extract_pdf_text
from .providers.base import ExtractorError, SheetNetwork

_LOGGER = logging.getLogger(__name__)

_URL: Final = (
    "https://brugel.brussels/publication/document/notype/{published}"
    "/fr/Tarif-distribution-Gaz-{year}.pdf"
)
_TIMEOUT: Final = 30
_FAILURE_RETRY_S: Final = 6 * 3600
_PER_KWH: Final = "EUR / kWh"
_FIGURE_RE: Final = re.compile(r"\d{1,3}(?:\.\d{3})*,\d+")
_FIXED_RE: Final = re.compile(r"X\s*=\s*EUR\s*/\s*an")
_RATE_RE: Final = re.compile(r"Y\s*=\s*EUR\s*/\s*kWh")
_METERING_RE: Final = re.compile(r"^\s*Comptage YMR\b.*EUR\s*/\s*an", re.IGNORECASE)
# Sized on a column read as another, not on what the network ought to cost.
_MAX_FIXED: Final = 1000.0
_MAX_KWH: Final = 0.2
_MAX_METERING: Final = 100.0

# year -> the network its sheet gives.
_held: dict[int, SheetNetwork] = {}
_failed_at: dict[int, datetime] = {}
_locks: dict[int, asyncio.Lock] = {}


def cached_network(year: int) -> SheetNetwork | None:
    """The year's network, else the latest earlier year's held, never a
    later one, or ``None`` before any sheet is read."""
    held = [held_year for held_year in _held if held_year <= year]
    return _held[max(held)] if held else None


async def ensure_network(session: aiohttp.ClientSession, year: int) -> None:
    """Read ``year``'s sheet unless it is held or failed lately, and the year
    before's while it is not served. Never raises."""
    for wanted in (year, year - 1):
        if wanted in _held:
            return
        async with _locks.setdefault(wanted, asyncio.Lock()):
            if wanted in _held:
                return
            failed = _failed_at.get(wanted)
            if (
                failed is not None
                and (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S
            ):
                continue
            try:
                terms = await read_network(session, wanted)
            except (ExtractorError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Brugel %d gas tariff sheet not read: %s", wanted, err)
                terms = None
            if terms is None:
                _failed_at[wanted] = dt_util.utcnow()
                continue
            _held[wanted] = terms
            return


async def read_network(session: aiohttp.ClientSession, year: int) -> SheetNetwork | None:
    """The year's network as its sheet gives it, or ``None`` where it does
    not read. Raises where the sheet is not served or is not a PDF."""
    payload = await fetch_pdf_bytes(
        session, _URL.format(published=year - 1, year=year), timeout=_TIMEOUT
    )
    return parse_network(await asyncio.to_thread(extract_pdf_text, payload))


def parse_network(text: str) -> SheetNetwork | None:
    """The network a sheet's text gives a household on T1 or T2, or ``None``
    when a term is missing, no rate per kWh is printed below them or a figure
    is out of bounds."""
    fixed = rate = metering = None
    charges = [0.0, 0.0]
    seen_charge = False
    for line in text.splitlines():
        figures = [float(f.replace(".", "").replace(",", ".")) for f in _FIGURE_RE.findall(line)]
        if _FIXED_RE.search(line):
            fixed = figures[:2]
        elif _RATE_RE.search(line):
            rate = figures[:2]
        elif _METERING_RE.search(line):
            metering = figures[:1]
        elif _PER_KWH in line and rate is not None:
            # A row that prints no figure, the regulatory balances in 2026,
            # charges nothing.
            seen_charge = seen_charge or bool(figures)
            for tier, value in enumerate(figures[:2]):
                charges[tier] += value
    if (
        fixed is None
        or rate is None
        or metering is None
        or len(fixed) < 2
        or len(rate) < 2
        or len(metering) < 1
        or not seen_charge
    ):
        return None
    t1_kwh, t2_kwh = (value + charge for value, charge in zip(rate, charges, strict=True))
    if not (
        0.0 < fixed[0] < _MAX_FIXED
        and 0.0 < fixed[1] < _MAX_FIXED
        and 0.0 < t2_kwh <= t1_kwh < _MAX_KWH
        and 0.0 < metering[0] < _MAX_METERING
    ):
        return None
    return SheetNetwork(
        t1_fixed=fixed[0],
        t1_proportional=t1_kwh,
        t2_fixed=fixed[1],
        t2_proportional=t2_kwh,
        metering_per_year=metering[0],
    )
