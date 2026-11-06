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

"""The CWaPE's approved Walloon gas distribution tariffs.

The CWaPE approves each Walloon gas operator's periodic tariffs and
publishes them year by year as a workbook and a PDF, "Tarifs périodiques de
distribution de gaz naturel - Prélèvement". Read for a custom Walloon entry
that takes its network off its operator's grid (:func:`cached_network`).

They are found through the documents service the CWaPE's tariff page is
built from (``/rest/documents``), which lists every document with its type,
its operator, the years it applies to and its file; the gas grids are type
203. The workbook is read, its first sheet being the household's. Each row
carries its market code: ``G140`` for the network use, whose "Fixe" row is
the fixed term a year and whose "gaz acheminé par conduites" row the rate per
kWh, then the public service (``G145``), levy (``G861``, ``G850``, ``G860``)
and regulatory balance (``G410``) rates per kWh, which the cards add into
each tier's rate. RESA's "supplément pour gaz porté" is billed only where
the gas is carried by road, and is left out. Wallonia bills no metering term
on gas, which the cards print as nothing.

A grid states the days it is valid. The one valid on a delivery month is
billed, else the latest that began before it, so from 1 January until the
year's grid is read the year before's stands in. Nothing read is kept over a
restart. Never raises: the caller is the coordinator tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import unquote

import aiohttp
from homeassistant.util import dt as dt_util

from . import xlsx
from .const import DSO_ORES, DSO_RESA
from .providers._pdf import fetch_bytes, fetch_text
from .providers.base import SheetNetwork

_LOGGER = logging.getLogger(__name__)

_SITE: Final = "https://www.cwape.be"
_GRID_TYPE: Final = "203"
_LISTING_URL: Final = f"{_SITE}/rest/documents?_format=json&documents_types%5B%5D={_GRID_TYPE}"
# The CWaPE's id for each operator, and the name its files begin with, which
# is checked too: an id that came to mean another operator would otherwise
# bill its rates without a sign.
_OPERATORS: Final[Mapping[str, tuple[str, str]]] = {
    DSO_ORES: ("453", "ORES"),
    DSO_RESA: ("225", "RESA"),
}
_TIMEOUT: Final = 30
_FAILURE_RETRY_S: Final = 6 * 3600
# RESA's workbook unpacks to about 1 MB.
_MAX_UNPACKED_BYTES: Final = 8 * 1024 * 1024
_CODE_RE: Final = re.compile(r"G\d{3}")
_NETWORK_CODE: Final = "G140"
# The public service rate, which every grid prints and without which the
# rows below the network use did not read.
_SERVICE_CODE: Final = "G145"
_PER_YEAR: Final = "(EUR/an)"
_PER_KWH: Final = "(EUR/kWh)"
_PIPED: Final = "conduites"
_PERIOD_RE: Final = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*(?:au|à)\s*(\d{2})\.(\d{2})\.(\d{4})")
# Sized on a column read as another, not on what the network ought to cost.
_MAX_FIXED: Final = 1000.0
_MAX_KWH: Final = 0.2


@dataclass(frozen=True, slots=True)
class GridNetwork:
    """One operator's network for a household as one approved grid gives
    it, and the days the grid is valid."""

    valid_from: date
    valid_to: date
    network: SheetNetwork


@dataclass(frozen=True, slots=True)
class _Document:
    operator: str
    year: int
    url: str


# Each grid read, by its address, for the life of the process; one that did
# not read is asked again after the backoff, and so is the listing.
_grids: dict[str, dict[str, GridNetwork]] = {}
_failed_at: dict[str, datetime] = {}
_listing: tuple[datetime, list[_Document]] | None = None
_locks: dict[str, asyncio.Lock] = {}


def cached_network(day: date, dso: str) -> SheetNetwork | None:
    """``dso``'s network on the grid valid on ``day``, else on the latest
    that began before it, never one that begins after, or ``None``."""
    held = valid_on(_grids.get(dso, {}).values(), day)
    return None if held is None else held.network


def valid_on(held: Iterable[GridNetwork], day: date) -> GridNetwork | None:
    """The grid of ``held`` valid on ``day``, else the latest that began
    before it, or ``None``."""
    started = [grid for grid in held if grid.valid_from <= day]
    valid = [grid for grid in started if day <= grid.valid_to]
    if not (valid or started):
        return None
    return max(valid or started, key=lambda grid: grid.valid_from)


def _covers(dso: str, year: int) -> bool:
    """Whether every month of ``year`` has a grid of ``dso`` valid on its
    first day."""
    periods = [(grid.valid_from, grid.valid_to) for grid in _grids.get(dso, {}).values()]
    return all(
        any(start <= date(year, month, 1) <= end for start, end in periods)
        for month in range(1, 13)
    )


async def ensure_network(session: aiohttp.ClientSession, year: int, dso: str) -> None:
    """Read ``dso``'s grids for ``year`` unless they cover it, and the year
    before's where those do not, never raising. Nothing for a key that is
    not a Walloon gas operator."""
    operator = _OPERATORS.get(dso)
    if operator is None or _covers(dso, year):
        return
    async with _locks.setdefault("cwape", asyncio.Lock()):
        documents: list[_Document] | None = None
        for wanted in (year, year - 1):
            if _covers(dso, year):
                return
            if documents is None:
                documents = await _listed(session)
            for doc in grid_documents(documents, wanted, operator):
                if doc.url in _grids.get(dso, {}) or _backing_off(doc.url):
                    continue
                try:
                    grid = await read_grid(session, doc)
                except Exception as err:  # the docstring promises no raise
                    _LOGGER.warning("CWaPE %s could not be read: %s", doc.url, err)
                    grid = None
                if grid is None:
                    _failed_at[doc.url] = dt_util.utcnow()
                else:
                    _grids.setdefault(dso, {})[doc.url] = grid


def _backing_off(url: str) -> bool:
    failed = _failed_at.get(url)
    if failed is None:
        return False
    if (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
        return True
    del _failed_at[url]
    return False


def grid_documents(
    documents: Sequence[_Document], year: int, operator: tuple[str, str]
) -> list[_Document]:
    """The operator's grid workbooks listed for ``year``."""
    grd, name = operator
    return [
        doc
        for doc in documents
        if doc.operator == grd
        and doc.year == year
        and doc.url.lower().endswith(".xlsx")
        and _names(doc.url, name)
    ]


def _names(url: str, name: str) -> bool:
    """Whether the file ``url`` points at is named after operator ``name``."""
    file = unquote(url.rsplit("/", 1)[-1])
    return re.match(rf"{re.escape(name)}(?![A-Za-z])", file, re.IGNORECASE) is not None


async def _listed(session: aiohttp.ClientSession) -> list[_Document]:
    """Every gas grid the service lists, kept for the backoff, answered or
    not, so a CWaPE that does not answer costs one timeout per six hours."""
    global _listing
    now = dt_util.utcnow()
    if _listing is not None and (now - _listing[0]).total_seconds() < _FAILURE_RETRY_S:
        return _listing[1]
    try:
        documents = await read_listing(session)
    except Exception as err:  # the docstring promises no raise
        _LOGGER.warning("CWaPE document listing could not be read: %s", err)
        documents = []
    _listing = (now, documents)
    return documents


async def read_listing(session: aiohttp.ClientSession) -> list[_Document]:
    """Every gas grid the service lists, read now. Raises where it does not
    answer."""
    text = await fetch_text(session, _LISTING_URL, timeout=_TIMEOUT)
    return await asyncio.to_thread(parse_listing, text)


def parse_listing(text: str) -> list[_Document]:
    """The listing's gas grids that apply to one year, each with its
    operator, year and address."""
    payload: Any = json.loads(text)
    documents: list[_Document] = []
    for item in payload if isinstance(payload, list) else ():
        if not isinstance(item, dict):
            continue
        kind, grd, years, path = (
            item.get(key)
            for key in (
                "field_document_type",
                "field_grd",
                "field_date_application",
                "field_file",
            )
        )
        if kind != _GRID_TYPE or not all(isinstance(f, str) for f in (grd, years, path)):
            continue
        span = re.fullmatch(r"(\d{4}) - (\d{4})", str(years).strip())
        if span is None or span[1] != span[2] or not str(path).startswith("/"):
            continue
        documents.append(_Document(str(grd), int(span[1]), _SITE + str(path)))
    return documents


async def read_grid(session: aiohttp.ClientSession, doc: _Document) -> GridNetwork | None:
    """What one grid gives, or ``None`` where it does not read. Raises on a
    failed fetch or a file that is not a workbook."""
    payload = await fetch_bytes(session, doc.url, timeout=_TIMEOUT)
    return await asyncio.to_thread(parse_grid, payload)


def parse_grid(payload: bytes) -> GridNetwork | None:
    """A household's network off an operator's grid workbook, or ``None``
    when the period, a term or the public service rate is missing, or a
    figure is out of bounds. Raises one of ``xlsx.UNREADABLE`` on a payload
    that is not a workbook; the caller logs it."""
    rows = xlsx.rows(payload, max_unpacked=_MAX_UNPACKED_BYTES)
    period = next(
        (found for row in rows for text in row.values() if (found := _PERIOD_RE.search(text))),
        None,
    )
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
    if period is None or columns is None:
        return None
    fixed = rate = None
    charges = [0.0, 0.0]
    serviced = False
    for row in rows:
        texts = list(row.values())
        values = [_number(row.get(column, "")) for column in columns]
        if _NETWORK_CODE in texts:
            if _PER_YEAR in texts and _labelled(texts, "Fixe"):
                fixed = values
            elif _PER_KWH in texts and any(_PIPED in text for text in texts):
                rate = values
        elif _PER_KWH in texts and any(_CODE_RE.fullmatch(text) for text in texts):
            if None in values:
                return None
            serviced = serviced or _SERVICE_CODE in texts
            charges = [
                total + float(value or 0.0) for total, value in zip(charges, values, strict=True)
            ]
    if fixed is None or rate is None or None in fixed or None in rate or not serviced:
        return None
    t1_fixed, t2_fixed = (float(value or 0.0) for value in fixed)
    t1_kwh, t2_kwh = (
        float(value or 0.0) + charge for value, charge in zip(rate, charges, strict=True)
    )
    if not (
        0.0 < t1_fixed < _MAX_FIXED
        and 0.0 < t2_fixed < _MAX_FIXED
        and 0.0 < t2_kwh <= t1_kwh < _MAX_KWH
    ):
        return None
    day, month, year, end_day, end_month, end_year = (int(part) for part in period.groups())
    return GridNetwork(
        valid_from=date(year, month, day),
        valid_to=date(end_year, end_month, end_day),
        network=SheetNetwork(
            t1_fixed=round(t1_fixed, 2),
            t1_proportional=t1_kwh,
            t2_fixed=round(t2_fixed, 2),
            t2_proportional=t2_kwh,
            metering_per_year=0.0,
        ),
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
