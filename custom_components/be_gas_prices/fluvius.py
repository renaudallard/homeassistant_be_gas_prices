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

"""The Fluvius data management fee of a yearly-read gas meter, off its
yearly tariff sheet.

Fluvius publishes the VREG-approved gas distribution tariffs as one XLSX per
network area and year. The listing links each sheet through a publication
page of its own:

    https://www.fluvius.be/nl/factuur-en-tarieven/netkosten/netkosten-afname-aardgas
    /nl/publicatie/fluvius-antwerpen-distributienettarief-afname-aardgas-01012026-31122026
    /sites/fluvius/files/2025-11/distributienettarieven-gas-afname-fluvius-antwerpen-...xlsx

"Tarief databeheer", row "Jaaropname (o.a. digitale gasmeter G4 en G6 ...)",
is the fee in EUR a year excluding VAT. It is one figure for all eight areas
(17,51 in 2025, 17,85 in 2026), so the first sheet of the year is read. The
DSO bills it for every connection whatever the supplier, and the resolver
puts it on a Flemish card that prints none (Luminus's).

A custom entry that takes its network off the sheet reads its own area's
(:func:`ensure_area_network`): the fixed and proportional terms of T1 and T2
with every charge per kWh printed below them added, the public service,
pension and levy rates whatever each year calls them, and the fee above.
That is what every card prints for the area, less VAT.

What is read is held for the process, per year, and kept in every entry's
store so a restart without the network still knows it. A year whose sheet
could not be read is asked again after a six hour backoff; a sheet once read
is not asked again.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Final

import aiohttp
from homeassistant.util import dt as dt_util

from . import xlsx
from .const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_MIDDEN_VLAANDEREN,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
)
from .providers._pdf import fetch_bytes, fetch_text
from .providers.base import ExtractorError, SheetNetwork

_LOGGER = logging.getLogger(__name__)

_SITE: Final = "https://www.fluvius.be"
LISTING_URL: Final = f"{_SITE}/nl/factuur-en-tarieven/netkosten/netkosten-afname-aardgas"
_PUBLICATION_RE: Final = r'href="(/nl/publicatie/[a-z0-9-]+-afname-aardgas-0101{year}-3112{year})"'
_SHEET_RE: Final = re.compile(r'href="(/sites/fluvius/files/[^"]+\.xlsx)"')
_TIMEOUT: Final = 20
_FAILURE_RETRY_S: Final = 6 * 3600
# A sheet is about 25 KB zipped and a few hundred KB unpacked. The cap on the
# unpacked size refuses a zip bomb before anything is read out of it.
_MAX_UNPACKED_BYTES: Final = 4 * 1024 * 1024
# Sized on the slip it catches, a factor of ten or a column read as a fee,
# not on what the tariff ought to be.
_MIN_FEE: Final = 1.0
_MAX_FEE: Final = 100.0
_FEE_LABEL: Final = "Jaaropname"
# One area's page, named the same way.
_AREA_PUBLICATION_RE: Final = (
    r'href="(/nl/publicatie/fluvius-{area}-distributienettarief-afname-'
    r'aardgas-0101{year}-3112{year})"'
)
_AREA_SLUGS: Final[dict[str, str]] = {
    DSO_FLUVIUS_ANTWERPEN: "antwerpen",
    DSO_FLUVIUS_HALLE_VILVOORDE: "halle-vilvoorde",
    DSO_FLUVIUS_IMEWO: "imewo",
    DSO_FLUVIUS_KEMPEN: "kempen",
    DSO_FLUVIUS_LIMBURG: "limburg",
    DSO_FLUVIUS_MIDDEN_VLAANDEREN: "midden-vlaanderen",
    DSO_FLUVIUS_WEST: "west",
    DSO_FLUVIUS_ZENNE_DIJLE: "zenne-dijle",
}
_PER_KWH: Final = "EUR/kWh"
_PER_YEAR: Final = "EUR/jaar"
# Sized on a column read as another, not on what the network ought to cost.
_MAX_FIXED: Final = 1000.0
_MAX_KWH: Final = 0.2

# year -> EUR/year excluding VAT.
_cache: dict[int, float] = {}
_failed_at: dict[int, datetime] = {}
_locks: dict[int, asyncio.Lock] = {}
# (year, DSO key) -> the area's network, kept the same way.
_area: dict[tuple[int, str], SheetNetwork] = {}
_area_failed_at: dict[tuple[int, str], datetime] = {}
_area_locks: dict[tuple[int, str], asyncio.Lock] = {}


def data_management_fee(year: int) -> float | None:
    """The fee for a yearly-read meter in ``year``, EUR/year excluding VAT,
    or ``None`` while that year's sheet has not been read."""
    return _cache.get(year)


async def ensure_tariff_sheet(session: aiohttp.ClientSession, year: int) -> None:
    """Read ``year``'s sheet unless it is held or failed lately.

    Never raises: the caller is the coordinator tick.
    """
    if year in _cache:
        return
    async with _locks.setdefault(year, asyncio.Lock()):
        if year in _cache:
            return
        failed = _failed_at.get(year)
        if failed is not None and (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
            return
        try:
            fee = await fetch_fee(session, year)
        except (ExtractorError, aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Fluvius %d gas tariff sheet not fetched: %s", year, err)
            fee = None
        except xlsx.UNREADABLE as err:
            # The page came back and is not the workbook as expected, or
            # its compressed data is damaged or cut short.
            _LOGGER.warning("Fluvius %d gas tariff sheet could not be read: %s", year, err)
            fee = None
        if fee is None:
            _failed_at[year] = dt_util.utcnow()
            return
        hold(year, fee)


def hold(year: int, fee: float) -> None:
    """Hold ``fee`` as ``year``'s, as read off its sheet."""
    _cache[year] = fee
    _failed_at.pop(year, None)


async def fetch_fee(session: aiohttp.ClientSession, year: int) -> float | None:
    """The listing, the year's first publication page, then its workbook:
    the fee, or ``None`` when Fluvius lists no sheet for ``year`` or the
    sheet does not give it. Raises on a failed fetch or a payload that is not
    a workbook."""
    listing = await fetch_text(session, LISTING_URL, timeout=_TIMEOUT)
    publication = re.search(_PUBLICATION_RE.format(year=year), listing)
    if publication is None:
        _LOGGER.debug("Fluvius lists no %d gas tariff sheet yet", year)
        return None
    page = await fetch_text(session, _SITE + publication.group(1), timeout=_TIMEOUT)
    sheet = _SHEET_RE.search(page)
    if sheet is None:
        _LOGGER.debug("Fluvius %d gas publication page links no workbook", year)
        return None
    payload = await fetch_bytes(session, _SITE + sheet.group(1), timeout=_TIMEOUT)
    # Off the event loop, as every workbook is read: what it unpacks to and
    # how long that takes is up to the payload.
    return await asyncio.to_thread(parse_sheet, payload, year)


def parse_sheet(payload: bytes, year: int) -> float | None:
    """The yearly-read data management fee a sheet gives, EUR/year excluding
    VAT.

    ``None`` when the workbook is not the year's gas tariff list, prints no
    single figure on the fee's row, or prints one outside the bounds. Raises
    on a payload that is not a workbook; the caller logs it.
    """
    rows = xlsx.rows(payload, max_unpacked=_MAX_UNPACKED_BYTES)
    title = f"aardgas - tarieflijst periodieke distributienettarieven {year}"
    if not any(title in text.lower() for row in rows for text in row.values()):
        return None
    row = next(
        (row for row in rows if any(text.startswith(_FEE_LABEL) for text in row.values())),
        None,
    )
    if row is None:
        return None
    numbers = []
    for text in row.values():
        try:
            numbers.append(float(text))
        except ValueError:
            continue
    if len(numbers) != 1 or not _MIN_FEE <= numbers[0] <= _MAX_FEE:
        return None
    # The workbook stores 17.850000000000001; the tariff is set in cents.
    return round(numbers[0], 2)


def cached_area_network(year: int, dso: str, *, stand_in: bool = True) -> SheetNetwork | None:
    """``dso``'s network for ``year``, else the latest earlier year's held,
    never a later one, or ``None`` before any of its sheets is read. Without
    ``stand_in``, the year's own sheet or ``None``."""
    if not stand_in:
        return _area.get((year, dso))
    held = [held_year for held_year, area in _area if area == dso and held_year <= year]
    return _area[(max(held), dso)] if held else None


async def ensure_area_network(session: aiohttp.ClientSession, year: int, dso: str) -> None:
    """Read ``dso``'s network off its own sheet for ``year`` unless it is
    held or failed lately, and the year before's while the listing has no
    sheet for the year yet. Nothing for a key that is not a Fluvius area.

    Never raises: the caller is the coordinator tick.
    """
    if dso not in _AREA_SLUGS:
        return
    for wanted in (year, year - 1):
        key = (wanted, dso)
        if key in _area:
            return
        async with _area_locks.setdefault(key, asyncio.Lock()):
            if key in _area:
                return
            failed = _area_failed_at.get(key)
            if (
                failed is not None
                and (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S
            ):
                continue
            try:
                terms = await read_area_network(session, wanted, _AREA_SLUGS[dso])
            except (ExtractorError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Fluvius %d gas sheet for %s not fetched: %s", wanted, dso, err)
                terms = None
            except xlsx.UNREADABLE as err:
                _LOGGER.warning(
                    "Fluvius %d gas sheet for %s could not be read: %s", wanted, dso, err
                )
                terms = None
            if terms is None:
                _area_failed_at[key] = dt_util.utcnow()
                continue
            _area[key] = terms
            return


async def read_area_network(
    session: aiohttp.ClientSession, year: int, area: str
) -> SheetNetwork | None:
    """One area's network for ``year`` as its sheet gives it, or ``None``
    where the listing has no such sheet or it does not read. Raises on a
    failed fetch or a payload that is not a workbook."""
    listing = await fetch_text(session, LISTING_URL, timeout=_TIMEOUT)
    publication = re.search(_AREA_PUBLICATION_RE.format(area=area, year=year), listing)
    if publication is None:
        return None
    page = await fetch_text(session, _SITE + publication.group(1), timeout=_TIMEOUT)
    sheet = _SHEET_RE.search(page)
    if sheet is None:
        return None
    payload = await fetch_bytes(session, _SITE + sheet.group(1), timeout=_TIMEOUT)
    return await asyncio.to_thread(parse_area_network, payload, year)


def parse_area_network(payload: bytes, year: int) -> SheetNetwork | None:
    """The network a sheet gives a household on T1 or T2, excluding VAT.

    The "Vaste term" and "Proportionele term" of the T1 and T2 columns, with
    every other row priced per kWh added to the proportional one, down to the
    remarks: the system management, public service, pension and levy rates,
    whose labels change from one year to the next. ``None`` when the workbook
    is not the year's gas tariff list, a term or the fee is missing, no
    charge per kWh is printed, or a figure is out of bounds. Raises on a
    payload that is not a workbook; the caller logs it.
    """
    rows = xlsx.rows(payload, max_unpacked=_MAX_UNPACKED_BYTES)
    title = f"aardgas - tarieflijst periodieke distributienettarieven {year}"
    if not any(title in text.lower() for row in rows for text in row.values()):
        return None
    columns = next(
        (
            (t1, t2)
            for row in rows
            for t1 in (_column(row, "T1"),)
            for t2 in (_column(row, "T2"),)
            if t1 is not None and t2 is not None
        ),
        None,
    )
    if columns is None:
        return None
    fixed = proportional = None
    fee = None
    charges = [0.0, 0.0]
    seen_charge = False
    for row in rows:
        texts = list(row.values())
        if any(text.startswith("Opmerkingen") for text in texts):
            break
        unit = _PER_KWH if _PER_KWH in texts else _PER_YEAR if _PER_YEAR in texts else None
        values = [_number(row.get(column, "")) for column in columns]
        if unit == _PER_YEAR and _labelled(texts, "Vaste term") and fixed is None:
            fixed = values
        elif unit == _PER_KWH and _labelled(texts, "Proportionele term"):
            proportional = values
        elif unit == _PER_YEAR and _labelled(texts, _FEE_LABEL):
            fee = values[0]
        elif unit == _PER_KWH and proportional is not None:
            seen_charge = seen_charge or any(value is not None for value in values)
            charges = [total + (value or 0.0) for total, value in zip(charges, values, strict=True)]
    if fixed is None or proportional is None or fee is None or not seen_charge:
        return None
    if None in fixed or None in proportional:
        return None
    t1_fixed, t2_fixed = (float(value or 0.0) for value in fixed)
    t1_kwh, t2_kwh = (
        float(value or 0.0) + charge for value, charge in zip(proportional, charges, strict=True)
    )
    if not (
        0.0 < t1_fixed < _MAX_FIXED
        and 0.0 < t2_fixed < _MAX_FIXED
        and 0.0 < t2_kwh <= t1_kwh < _MAX_KWH
        and _MIN_FEE <= fee <= _MAX_FEE
    ):
        return None
    return SheetNetwork(
        t1_fixed=round(t1_fixed, 2),
        t1_proportional=t1_kwh,
        t2_fixed=round(t2_fixed, 2),
        t2_proportional=t2_kwh,
        metering_per_year=round(fee, 2),
    )


def _column(row: dict[str, str], header: str) -> str | None:
    return next((column for column, text in row.items() if text == header), None)


def _labelled(texts: list[str], label: str) -> bool:
    return any(text.strip().startswith(label) for text in texts)


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def held_table() -> dict[str, float]:
    """What is held, in the shape :func:`restore` reads, for an entry's store."""
    return {str(year): fee for year, fee in sorted(_cache.items())}


def restore(table: Any) -> None:
    """Hold the fees an entry's store kept, for a year nothing holds yet."""
    if not isinstance(table, dict):
        return
    for year, fee in table.items():
        try:
            held = int(year), float(fee)
        except (TypeError, ValueError):
            continue
        if _MIN_FEE <= held[1] <= _MAX_FEE:
            _cache.setdefault(*held)
