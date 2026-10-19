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

What is read is held for the process, per year, and kept in every entry's
store so a restart without the network still knows it. A year whose sheet
could not be read is asked again after a six hour backoff; a sheet once read
is not asked again.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
import zlib
from datetime import datetime
from typing import Any, Final

import aiohttp
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from homeassistant.util import dt as dt_util

from .providers._pdf import USER_AGENT, fetch_text, guard_redirect, read_capped
from .providers.base import ExtractorError

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

_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_SHEET: Final = "xl/worksheets/sheet1.xml"

# year -> EUR/year excluding VAT.
_cache: dict[int, float] = {}
_failed_at: dict[int, datetime] = {}
_locks: dict[int, asyncio.Lock] = {}


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
        except (
            ValueError,
            KeyError,
            IndexError,
            zipfile.BadZipFile,
            zlib.error,
            EOFError,
            ET.ParseError,
            DefusedXmlException,
        ) as err:
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
    url = _SITE + sheet.group(1)
    async with session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
    ) as resp:
        guard_redirect(url, resp)
        resp.raise_for_status()
        payload = await read_capped(resp, url)
    return parse_sheet(payload, year)


def _rows(payload: bytes) -> list[dict[str, str]]:
    """The worksheet's cells as text, one ``{column: text}`` per row."""
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        if sum(info.file_size for info in z.infolist()) > _MAX_UNPACKED_BYTES:
            raise ValueError("workbook unpacks past the size cap")
        try:
            shared = ET.fromstring(z.read("xl/sharedStrings.xml"))
        except KeyError:
            strings: list[str] = []
        else:
            strings = [
                "".join(t.text or "" for t in si.iter(_NS + "t")) for si in shared.iter(_NS + "si")
            ]
        sheet = ET.fromstring(z.read(_SHEET))
    rows: list[dict[str, str]] = []
    for row in sheet.iter(_NS + "row"):
        cells: dict[str, str] = {}
        for cell in row.iter(_NS + "c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(_NS + "t"))
            else:
                value = cell.find(_NS + "v")
                if value is None or value.text is None:
                    continue
                text = strings[int(value.text)] if kind == "s" else value.text
            cells[cell.get("r", "").rstrip("0123456789")] = text.strip()
        rows.append(cells)
    return rows


def parse_sheet(payload: bytes, year: int) -> float | None:
    """The yearly-read data management fee a sheet gives, EUR/year excluding
    VAT.

    ``None`` when the workbook is not the year's gas tariff list, prints no
    single figure on the fee's row, or prints one outside the bounds. Raises
    on a payload that is not a workbook; the caller logs it.
    """
    rows = _rows(payload)
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
