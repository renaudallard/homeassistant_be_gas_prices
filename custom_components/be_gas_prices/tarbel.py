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

"""The federal special excise on residential gas, off FPS Finance's excise
tariff.

Article 419 of the programme law of 27 December 2004 sets each year's rate
(excise_law.py reads it on Justel), and article 420 §3/1 moves both
household slices every quarter on the quarter's gas price, by a notice in
the Moniteur belge that Justel does not consolidate. FPS Finance publishes
the rates in force as one workbook, TarBel's annex 7, with a row per slice
and period:

    https://eservices.minfin.fgov.be/extTariffBrowser/FileResourceForHomePageServlet?fname=accijnzen.xlsx&lang=NL

Sheet "Q" lists "Aardgas gebruikt als verwarmingsbrandstof voor niet-zakelijk
gebruik, m.u.v. de beschermde residentiële afnemer", "voor een jaarlijks
verbruik van 0 tot 12 MWh" or "vanaf 12 MWh", "van toepassing vanaf
01/10/2026 tot en met 31/12/2026", in EUR per MWh: the ordinary excise
(code 100) and the special one (200) on one line each. The columns are found
by their headers, since each year's GN codes add a column before them. The
protected residential customer ("beschermde residentiële afnemer") has one
row per period and no slices, a rate the quarterly notices do not move.

The energy contribution is code 300. Up to July 2026 it has rows of its own,
"enkel de bijdrage op de energie", "van toepassing tot en met 31/07/2026"
with no first day, at 0,9978 EUR/MWh, while the excise rows of those quarters
say "enkel de bijzondere accijns" and carry 0 under that code: that 0 is not
the contribution. A row that says neither carries both, as the slices
from August 2026 on do, the contribution at 0. A month no row gives is
left to the law (excise_law.py).

What is read is held for the process, read again daily, and kept in every
entry's store. A period the workbook drops stays held: FPS Finance deletes
codes once their period is long past. A period listed with no end covers
the rest of its first year only: article 419 sets a new rate each 1 January,
which Justel gives, and an open period would otherwise bill the old one
over it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from itertools import pairwise
from typing import Any, Final

import aiohttp
from homeassistant.util import dt as dt_util

from . import xlsx
from .const import EXCISE_BAND_KWH, TARBEL_URL
from .providers._pdf import fetch_bytes
from .providers.base import ExtractorError

_LOGGER = logging.getLogger(__name__)

_REFRESH_S: Final = 24 * 3600
_FAILURE_RETRY_S: Final = 6 * 3600
_TIMEOUT: Final = 30
# The workbook is about 280 KB zipped and 2,6 MB unpacked, over ten sheets.
_MAX_UNPACKED_BYTES: Final = 16 * 1024 * 1024
# A rate past this is a misread, a unit slipped by ten: the highest the law
# has voted is under 0,015 EUR/kWh.
_MAX_EUR_PER_KWH: Final = 0.05

_SHEET: Final = "Q"
_DESCRIPTION: Final = "Omschrijving van de GN-code"
_CODES: Final = "Vak 47 (1)"
_RATES: Final = "Vak 47 (3)"
_UNIT: Final = "Maatstaf van Heffing"
_GAS: Final = "Aardgas gebruikt als verwarmingsbrandstof voor niet-zakelijk gebruik"
_PROTECTED: Final = "beschermde residentiële afnemer"
_NOT_PROTECTED: Final = f"m.u.v. de {_PROTECTED}"
# A code of the energy contribution alone, and one of the excise alone, whose
# 300 is no contribution.
_CONTRIBUTION_ONLY: Final = "enkel de bijdrage op de energie"
_EXCISE_ONLY: Final = "enkel de bijzondere accijns"
_BAND: Final = f"{EXCISE_BAND_KWH / 1000:g} MWh"
_LOW: Final = f"van 0 tot {_BAND}"
_HIGH: Final = f"vanaf {_BAND}"
_PER_MWH: Final = "EUR per MWh"
_PERIOD_RE: Final = re.compile(
    r"van toepassing vanaf (\d{2})/(\d{2})/(\d{4})(?:\s+tot en met (\d{2})/(\d{2})/(\d{4}))?"
)
# A contribution row's period, which names its last day only.
_UNTIL_RE: Final = re.compile(r"van toepassing tot en met (\d{2})/(\d{2})/(\d{4})", re.IGNORECASE)
# The ordinary and the special excise, and the energy contribution.
_EXCISE_CODES: Final = ("100", "200")
_CONTRIBUTION_CODES: Final = ("300",)

# (first day, last day or None, EUR/kWh excluding VAT up to the band, above it).
type Period = tuple[date, date | None, float, float]
# (first day, last day or None, EUR/kWh excluding VAT): the protected rate.
type ProtectedPeriod = tuple[date, date | None, float]
# (first day or None, last day or None, EUR/kWh excluding VAT): the energy
# contribution, a row of its own naming no first day.
type ContributionPeriod = tuple[date | None, date | None, float]

_HELD: dict[date, Period] = {}
_PROTECTED_HELD: dict[date, ProtectedPeriod] = {}
_CONTRIBUTION_HELD: dict[date, ContributionPeriod] = {}
_fetched_at: datetime | None = None
_failed_at: datetime | None = None
# Entries set up together read the workbook once.
_lock = asyncio.Lock()


class TarbelError(ExtractorError):
    """The workbook could not be read as TarBel's annex 7."""


def _columns(rows: list[dict[str, str]]) -> tuple[str, str, str, str]:
    """The columns of the description, the codes, the rates and the unit."""
    for row in rows:
        found = {
            name: next((col for col, text in row.items() if text.startswith(name)), None)
            for name in (_DESCRIPTION, _CODES, _RATES, _UNIT)
        }
        if all(found.values()):
            return (
                str(found[_DESCRIPTION]),
                str(found[_CODES]),
                str(found[_RATES]),
                str(found[_UNIT]),
            )
    raise TarbelError("no header row")


def _excise(codes: str, rates: str, wanted: tuple[str, ...] = _EXCISE_CODES) -> float:
    """What one row sets under the ``wanted`` codes, EUR/kWh: the ordinary
    plus the special excise by default."""
    names, values = codes.split(), rates.split()
    if not names or len(names) != len(values):
        raise TarbelError(f"codes {codes!r} against rates {rates!r}")
    if not set(wanted) <= set(names):
        raise TarbelError(f"codes {codes!r} without {wanted}")
    per_mwh = sum(
        float(value.replace(",", "."))
        for name, value in zip(names, values, strict=True)
        if name in wanted
    )
    rate = per_mwh / 1000.0
    if not 0.0 <= rate < _MAX_EUR_PER_KWH:
        raise TarbelError(f"implausible rate {rate} EUR/kWh under {wanted}")
    return rate


def _period(text: str) -> tuple[date, date | None] | None:
    """The first and last day a row's description says it applies, the last
    None for a period still open."""
    period = _PERIOD_RE.search(text)
    if period is None:
        return None
    day, month, year, *last = period.groups()
    start = date(int(year), int(month), int(day))
    end = None if last[0] is None else date(int(last[2]), int(last[1]), int(last[0]))
    return start, end


def _gas_rows(payload: bytes) -> list[tuple[str, str, str, str]]:
    """Each gas row's description, codes, rates and unit."""
    rows = xlsx.rows(payload, max_unpacked=_MAX_UNPACKED_BYTES, sheet=_SHEET)
    description, codes, rates, unit = _columns(rows)
    out = []
    for row in rows:
        text = " ".join(row.get(description, "").split())
        if text.startswith(_GAS):
            out.append((text, row.get(codes, ""), row.get(rates, ""), row.get(unit, "")))
    return out


def parse(payload: bytes) -> tuple[Period, ...]:
    """The household excise by period the workbook lists, oldest first.

    Raises :class:`TarbelError`, or one of :data:`xlsx.UNREADABLE`, on
    anything that is not the workbook as expected rather than reading half
    of it: a gas row without its slice, period or unit, a period with one
    slice only, or periods that overlap.
    """
    return _household(_gas_rows(payload))


def parse_protected(payload: bytes) -> tuple[ProtectedPeriod, ...]:
    """The protected residential customer's excise by period, oldest first.
    Raises like :func:`parse`."""
    return _protected(_gas_rows(payload))


def parse_contribution(payload: bytes) -> tuple[ContributionPeriod, ...]:
    """The household energy contribution by period, oldest first. Raises
    like :func:`parse`, and on two figures for one period."""
    return _contribution(_gas_rows(payload))


def _household(gas: list[tuple[str, str, str, str]]) -> tuple[Period, ...]:
    slices: dict[tuple[date, date | None], dict[str, float]] = {}
    for text, codes, rates, unit in gas:
        if _NOT_PROTECTED not in text or _CONTRIBUTION_ONLY in text:
            continue
        name = "low" if _LOW in text else "high" if _HIGH in text else None
        period = _period(text)
        if name is None or period is None or unit != _PER_MWH:
            raise TarbelError(f"a gas row without its slice, period or unit: {text!r}")
        start, end = period
        rate = _excise(codes, rates)
        held = slices.setdefault((start, end), {})
        if held.get(name, rate) != rate:
            raise TarbelError(f"two {name} rates from {start}")
        held[name] = rate
    out = []
    # A period with no end sorts after any that ends, so two slices from the
    # same day, one open and one closed, are refused rather than compared.
    for (start, end), rate_of in sorted(
        slices.items(), key=lambda item: (item[0][0], item[0][1] or date.max)
    ):
        if set(rate_of) != {"low", "high"}:
            raise TarbelError(f"one slice only from {start}")
        out.append((start, end, rate_of["low"], rate_of["high"]))
    if not out:
        raise TarbelError("no residential gas row")
    if any(a[1] is None or a[1] >= b[0] for a, b in pairwise(out)):
        raise TarbelError("periods overlap")
    return tuple(out)


def _protected(gas: list[tuple[str, str, str, str]]) -> tuple[ProtectedPeriod, ...]:
    out: list[ProtectedPeriod] = []
    for text, codes, rates, unit in gas:
        if _PROTECTED not in text or _NOT_PROTECTED in text:
            continue
        period = _period(text)
        if period is None or unit != _PER_MWH:
            raise TarbelError(f"a protected gas row without its period or unit: {text!r}")
        out.append((*period, _excise(codes, rates)))
    out.sort(key=lambda row: (row[0], row[1] or date.max))
    if not out:
        raise TarbelError("no protected residential gas row")
    if any(a[1] is None or a[1] >= b[0] for a, b in pairwise(out)):
        raise TarbelError("protected periods overlap")
    return tuple(out)


def _contribution(gas: list[tuple[str, str, str, str]]) -> tuple[ContributionPeriod, ...]:
    found: dict[tuple[date | None, date | None], float] = {}
    for text, codes, rates, unit in gas:
        if _NOT_PROTECTED not in text or _EXCISE_ONLY in text:
            continue
        period = _period(text) or _until(text)
        if period is None or unit != _PER_MWH:
            raise TarbelError(f"a contribution row without its period or unit: {text!r}")
        rate = _excise(codes, rates, _CONTRIBUTION_CODES)
        if found.setdefault(period, rate) != rate:
            raise TarbelError(f"two energy contributions from {period[0]} to {period[1]}")
    out = sorted(
        ((start, end, rate) for (start, end), rate in found.items()),
        key=lambda row: (row[0] or date.min, row[1] or date.max),
    )
    if not out:
        raise TarbelError("no energy contribution row")
    if any(a[1] is None or a[1] >= (b[0] or date.min) for a, b in pairwise(out)):
        raise TarbelError("contribution periods overlap")
    return tuple(out)


def _until(text: str) -> tuple[None, date] | None:
    """A period naming its last day only."""
    until = _UNTIL_RE.search(text)
    if until is None:
        return None
    day, month, year = until.groups()
    return None, date(int(year), int(month), int(day))


def _covers(start: date | None, end: date | None, day: date) -> bool:
    """Whether a period from ``start`` to ``end`` covers ``day``, an open
    one up to the end of its first year, one with no first day every day up
    to its last."""
    if start is None:
        return end is not None and day <= end
    return start <= day and (day <= end if end is not None else day.year == start.year)


def excise_for(month: date) -> tuple[float, float] | None:
    """The two slices' excise in force on ``month``'s first day, EUR/kWh
    excluding VAT, or ``None`` where no period held covers it."""
    first = month.replace(day=1)
    rates = None
    for start, end, low, high in sorted(_HELD.values()):
        if _covers(start, end, first):
            rates = (low, high)
    return rates


def protected_excise_for(month: date) -> float | None:
    """The protected residential customer's excise in force on ``month``'s
    first day, EUR/kWh excluding VAT, or ``None`` where no period held
    covers it."""
    first = month.replace(day=1)
    rate = None
    for start, end, value in sorted(_PROTECTED_HELD.values()):
        if _covers(start, end, first):
            rate = value
    return rate


def contribution_for(month: date) -> float | None:
    """The household energy contribution in force on ``month``'s first day,
    EUR/kWh excluding VAT, or ``None`` where no period held covers it."""
    first = month.replace(day=1)
    rate = None
    for _key, (start, end, value) in sorted(_CONTRIBUTION_HELD.items()):
        if _covers(start, end, first):
            rate = value
    return rate


def hold(periods: tuple[Period, ...]) -> None:
    """Hold ``periods``, replacing a held period that starts on the same day
    and keeping the others."""
    for period in periods:
        _HELD[period[0]] = period


def hold_protected(periods: tuple[ProtectedPeriod, ...]) -> None:
    """:func:`hold` for the protected rate."""
    for period in periods:
        _PROTECTED_HELD[period[0]] = period


def hold_contribution(periods: tuple[ContributionPeriod, ...]) -> None:
    """:func:`hold` for the energy contribution, a period with no first day
    keyed as the earliest."""
    for period in periods:
        _CONTRIBUTION_HELD[period[0] or date.min] = period


def _stored(rows: Any) -> tuple[Period, ...]:
    if not isinstance(rows, list):
        return ()
    out: list[Period] = []
    for row in rows:
        try:
            start = date.fromisoformat(row[0])
            end = None if row[1] is None else date.fromisoformat(row[1])
            period = (start, end, float(row[2]), float(row[3]))
        except (TypeError, ValueError, IndexError, KeyError):
            return ()
        if not all(0.0 <= rate < _MAX_EUR_PER_KWH for rate in period[2:]):
            return ()
        out.append(period)
    return tuple(out)


def _stored_protected(rows: Any) -> tuple[ProtectedPeriod, ...]:
    if not isinstance(rows, list):
        return ()
    out: list[ProtectedPeriod] = []
    for row in rows:
        try:
            start = date.fromisoformat(row[0])
            end = None if row[1] is None else date.fromisoformat(row[1])
            period = (start, end, float(row[2]))
        except (TypeError, ValueError, IndexError, KeyError):
            return ()
        if not 0.0 <= period[2] < _MAX_EUR_PER_KWH:
            return ()
        out.append(period)
    return tuple(out)


def _stored_contribution(rows: Any) -> tuple[ContributionPeriod, ...]:
    if not isinstance(rows, list):
        return ()
    out: list[ContributionPeriod] = []
    for row in rows:
        try:
            start = None if row[0] is None else date.fromisoformat(row[0])
            end = None if row[1] is None else date.fromisoformat(row[1])
            period = (start, end, float(row[2]))
        except (TypeError, ValueError, IndexError, KeyError):
            return ()
        if (start is None and end is None) or not 0.0 <= period[2] < _MAX_EUR_PER_KWH:
            return ()
        out.append(period)
    return tuple(out)


def restore(rows: Any) -> None:
    """Hold the periods an entry's store kept, when nothing is held yet.

    Several entries restore their own copy at start-up and one of them may
    already have read the workbook, so a stored copy never replaces it.
    """
    periods = _stored(rows)
    if periods and not _HELD:
        hold(periods)


def restore_protected(rows: Any) -> None:
    """:func:`restore` for the protected rate."""
    periods = _stored_protected(rows)
    if periods and not _PROTECTED_HELD:
        hold_protected(periods)


def restore_contribution(rows: Any) -> None:
    """:func:`restore` for the energy contribution."""
    periods = _stored_contribution(rows)
    if periods and not _CONTRIBUTION_HELD:
        hold_contribution(periods)


def held_table() -> list[list[Any]]:
    """What is held, in the shape :func:`restore` reads, for an entry's store."""
    return [
        [start.isoformat(), None if end is None else end.isoformat(), low, high]
        for start, end, low, high in sorted(_HELD.values())
    ]


def protected_held_table() -> list[list[Any]]:
    """What is held of the protected rate, in the shape
    :func:`restore_protected` reads."""
    return [
        [start.isoformat(), None if end is None else end.isoformat(), rate]
        for start, end, rate in sorted(_PROTECTED_HELD.values())
    ]


def contribution_held_table() -> list[list[Any]]:
    """What is held of the energy contribution, in the shape
    :func:`restore_contribution` reads."""
    return [
        [
            None if start is None else start.isoformat(),
            None if end is None else end.isoformat(),
            rate,
        ]
        for _key, (start, end, rate) in sorted(_CONTRIBUTION_HELD.items())
    ]


type Tariff = tuple[tuple[Period, ...], tuple[ProtectedPeriod, ...], tuple[ContributionPeriod, ...]]


def _parse_all(payload: bytes) -> Tariff:
    gas = _gas_rows(payload)
    household = _household(gas)
    try:
        protected = _protected(gas)
    except TarbelError as err:
        # Only the social tariff bills it, and the law gives it from the
        # wording in force: the household rates stay read.
        _LOGGER.warning("The protected customer's gas excise tariff could not be read: %s", err)
        protected = ()
    try:
        contribution = _contribution(gas)
    except (TarbelError, ValueError) as err:
        # The law gives it from the wording in force, and const.py before:
        # the excise stays read.
        _LOGGER.warning("The gas energy contribution tariff could not be read: %s", err)
        contribution = ()
    return household, protected, contribution


async def fetch_tariff(session: aiohttp.ClientSession) -> Tariff:
    """The household, the protected customer's and the energy contribution's
    periods the workbook lists today, parsed off the event loop: a few
    tenths of a second on a Raspberry Pi. No protected or contribution
    period when those rows cannot be read."""
    payload = await fetch_bytes(session, TARBEL_URL, timeout=_TIMEOUT)
    return await asyncio.to_thread(_parse_all, payload)


async def ensure_tariff(session: aiohttp.ClientSession) -> None:
    """Read the workbook once a day.

    A failure keeps what is held and is retried after ``_FAILURE_RETRY_S``;
    nothing here raises, since the caller is the coordinator tick.
    """
    global _fetched_at, _failed_at
    async with _lock:
        now = dt_util.utcnow()
        if _fetched_at is not None and (now - _fetched_at).total_seconds() < _REFRESH_S:
            return
        if _failed_at is not None and (now - _failed_at).total_seconds() < _FAILURE_RETRY_S:
            return
        try:
            periods, protected, contribution = await fetch_tariff(session)
        except (TarbelError, *xlsx.UNREADABLE) as err:
            # The workbook came back and is not annex 7 as expected.
            _LOGGER.warning("The gas excise tariff could not be read: %s", err)
            _failed_at = now
            return
        except ExtractorError as err:
            # What is held goes on billing; with nothing held, a quarter
            # const.py does not type is billed on the law's yearly figure.
            log = _LOGGER.debug if _HELD else _LOGGER.warning
            log("The gas excise tariff could not be fetched from FPS Finance: %s", err)
            _failed_at = now
            return
        hold(periods)
        hold_protected(protected)
        hold_contribution(contribution)
        _fetched_at = now
        _failed_at = None
