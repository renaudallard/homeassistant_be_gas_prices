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

"""The federal special excise on residential gas, read from the law.

Article 419, g), of the programme law of 27 December 2004 sets the excise a
household pays per MWh of gas, and Justel serves its consolidated text. Under
"consommation non professionnelle", "b) autres" prints one paragraph per
slice of the annual volume, up to 12 MWh and above it, each a rate followed
by the steps the law has already voted ("A partir du 1er janvier 2027: ..."),
so a step is billed in its own month without anyone editing a constant.

The day the rate in force took effect is read off the amendment that wrote
it: Justel brackets every passage an amendment wrote and titles the bracket
with that amendment's entry into force, so the date is the one of the
innermost bracket around the rate. Article 420 §3/1 moves both slices every
quarter by a notice Justel does not consolidate: FPS Finance's excise tariff
lists those (tarbel.py) and is billed first, so what is read here bills a
quarter the tariff does not list yet, a year's voted step until its first
notice.

What is read is held for the process, the law read again daily, and kept in
every entry's store so a restart without the network still knows it. A later
amendment that rewrites the article drops the steps before it from the text,
so the steps held from an earlier read are kept up to the first day the new
text dates.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from html import unescape
from itertools import pairwise
from typing import Any, Final

import aiohttp
from homeassistant.util import dt as dt_util

from .const import EXCISE_BAND_KWH, EXCISE_LAW_URL
from .providers._pdf import MONTH_NAMES, fetch_text
from .providers.base import ExtractorError

_LOGGER = logging.getLogger(__name__)

_REFRESH_S: Final = 24 * 3600
_FAILURE_RETRY_S: Final = 6 * 3600
# A rate past this is a misread, a unit slipped by ten, not a rate: the
# highest step the law has voted is under 0,015 EUR/kWh.
_MAX_EUR_PER_KWH: Final = 0.05

# (first day, EUR/kWh excluding VAT up to the band, above it), ascending.
type Step = tuple[date, float, float]

_ARTICLE_RE = re.compile(r"NAME='Art\.419'(.*?)(?:NAME='Art\.419bis'|NAME='Art\.420')", re.S)
# An amendment's bracket, opening and closing. The opening carries the
# amendment as a title: <L 2026-05-30/01, art. 38, 071; En vigueur : 01-08-2026>.
_OPEN_RE = re.compile(
    r"\[<sup>(?:(?!</sup>).)*?En vigueur\s*:\s*(\d{2})-(\d{2})-(\d{4})(?:(?!</sup>).)*</sup>",
    re.S,
)
_CLOSE_RE = re.compile(r"\]<sup>(?:(?!</sup>).)*</sup>", re.S)
_OPEN = "\x01"
_CLOSE = "\x02"
_TAG_RE = re.compile(r"<[^>]+>")

_GAS = "gaz naturel relevant des codes NC 2711 11 00 et 2711 21 00"
# The paragraph after the gas one.
_COAL = "houille, coke et lignite"
_HOUSEHOLD_RE = re.compile(r"consommation non[- ]professionnelle\s*:")
_OTHERS_RE = re.compile(r"b[.)]\s*autres\s*:")
_LOW_RE = re.compile(r"pour la tranche de 0 à ([\d.]+) MWh\s*:")
_HIGH_RE = re.compile(r"pour la tranche à partir de ([\d.]+) MWh\s*:")
_STEP_RE = re.compile(r"A partir du (1er|\d{1,2}) (" + "|".join(MONTH_NAMES) + r") (\d{4})\s*:")
_EXCISE_RE = re.compile(r"droit d['’]accise\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh")
_SPECIAL_RE = re.compile(r"droit d['’]accise spécial\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh")

_HELD: list[Step] = []
_fetched_at: datetime | None = None
_failed_at: datetime | None = None


class ExciseLawError(ExtractorError):
    """The page could not be read as article 419."""


def _number(raw: str) -> float:
    return float(raw.replace(".", "").replace(",", "."))


def _rate(chunk: str) -> float:
    """The ordinary plus the special excise one paragraph sets, EUR/kWh."""
    ordinary = _EXCISE_RE.findall(chunk)
    special = _SPECIAL_RE.findall(chunk)
    if len(ordinary) != 1 or len(special) != 1:
        raise ExciseLawError("no single excise rate")
    rate = (_number(ordinary[0]) + _number(special[0])) / 1000.0
    if not 0.0 <= rate < _MAX_EUR_PER_KWH:
        raise ExciseLawError(f"implausible excise {rate} EUR/kWh")
    return rate


def _amendment(text: str, at: int) -> date:
    """The entry into force of the innermost amendment around ``at``."""
    stack: list[str] = []
    for pos in range(at):
        if text[pos] == _OPEN:
            stack.append(text[pos + 1 : pos + 11])
        elif text[pos] == _CLOSE and stack:
            stack.pop()
    if not stack:
        raise ExciseLawError("no entry into force around the rate")
    day, month, year = stack[-1].split("-")
    return date(int(year), int(month), int(day))


def _steps(text: str, start: int, end: int) -> list[tuple[date, float]]:
    """The rate set between ``start`` and ``end`` and the steps after it."""
    chunk = text[start:end]
    heads = list(_STEP_RE.finditer(chunk))
    bounds = [0, *(m.start() for m in heads), len(chunk)]
    # Dated where the rate itself is printed: an amendment that rewrites only
    # the rate opens its bracket after the label.
    special = _SPECIAL_RE.search(chunk, 0, bounds[1])
    if special is None:
        raise ExciseLawError("no special excise")
    out = [(_amendment(text, start + special.start()), _rate(chunk[: bounds[1]]))]
    for i, head in enumerate(heads):
        day, month, year = head.groups()
        when = date(int(year), MONTH_NAMES[month], 1 if day == "1er" else int(day))
        out.append((when, _rate(chunk[head.end() : bounds[i + 2]])))
    if not all(a[0] < b[0] for a, b in pairwise(out)):
        raise ExciseLawError("steps out of order")
    return out


def parse(page: str) -> tuple[Step, ...]:
    """The household excise article 419 sets for the two slices, with their
    steps.

    Raises :class:`ExciseLawError` on anything that is not the article as
    expected, rather than reading half of it: that includes a band other
    than the one the pricing slices the year at, and a date the calendar
    does not have or a number that does not convert, which would otherwise
    escape the backoff in :func:`ensure_excise_law` as ValueError.
    """
    try:
        return _parse(page)
    except (ValueError, OverflowError) as err:
        raise ExciseLawError(f"unreadable article: {err}") from err


def _parse(page: str) -> tuple[Step, ...]:
    match = _ARTICLE_RE.search(page)
    if match is None:
        raise ExciseLawError("article 419 not found")
    marked = _CLOSE_RE.sub(_CLOSE, match.group(1))
    marked = _OPEN_RE.sub(lambda m: f"{_OPEN}{m[1]}-{m[2]}-{m[3]} ", marked)
    text = " ".join(unescape(_TAG_RE.sub(" ", marked)).split())
    start = text.find(_GAS)
    end = text.find(_COAL, start)
    if start < 0 or end < 0:
        raise ExciseLawError("no natural gas paragraph")
    household = _HOUSEHOLD_RE.search(text, start, end)
    if household is None:
        raise ExciseLawError("no non-professional consumption")
    others = _OTHERS_RE.search(text, household.end(), end)
    if others is None:
        raise ExciseLawError("no household rate")
    low = _LOW_RE.search(text, others.end(), end)
    high = _HIGH_RE.search(text, others.end(), end)
    if low is None or high is None or high.start() < low.end():
        raise ExciseLawError("no two slices")
    band = EXCISE_BAND_KWH / 1000.0
    if _number(low[1]) != band or _number(high[1]) != band:
        raise ExciseLawError(f"slices at {low[1]} and {high[1]} MWh, not {band:g}")
    lows = _steps(text, low.end(), high.start())
    highs = _steps(text, high.end(), end)
    if [when for when, _rate in lows] != [when for when, _rate in highs]:
        raise ExciseLawError("the two slices step on different days")
    return tuple((when, a, b) for (when, a), (_when, b) in zip(lows, highs, strict=True))


def excise_for(month: date) -> tuple[float, float] | None:
    """The two slices' excise in force on ``month``'s first day, EUR/kWh
    excluding VAT, or ``None`` before the first step held."""
    first = month.replace(day=1)
    rates = None
    for when, low, high in _HELD:
        if when > first:
            break
        rates = (low, high)
    return rates


def hold(steps: tuple[Step, ...]) -> None:
    """Hold ``steps`` from the first day they date on, keeping the steps
    held from before that day."""
    if not steps:
        return
    _HELD[:] = [step for step in _HELD if step[0] < steps[0][0]] + list(steps)


def _stored(rows: Any) -> tuple[Step, ...]:
    out: list[Step] = []
    if not isinstance(rows, list):
        return ()
    for row in rows:
        try:
            step = (date.fromisoformat(row[0]), float(row[1]), float(row[2]))
        except (TypeError, ValueError, IndexError, KeyError):
            return ()
        if not all(0.0 <= rate < _MAX_EUR_PER_KWH for rate in step[1:]):
            return ()
        out.append(step)
    return tuple(out) if all(a[0] < b[0] for a, b in pairwise(out)) else ()


def restore(rows: Any) -> None:
    """Hold the steps an entry's store kept, when nothing is held yet.

    Several entries restore their own copy at start-up and one of them may
    already have read the law, so a stored copy never replaces it.
    """
    steps = _stored(rows)
    if steps and not _HELD:
        _HELD[:] = steps


def held_table() -> list[list[Any]]:
    """What is held, in the shape :func:`restore` reads, for an entry's store."""
    return [[when.isoformat(), low, high] for when, low, high in _HELD]


async def ensure_excise_law(session: aiohttp.ClientSession) -> None:
    """Read the law once a day.

    A failure keeps what is held and is retried after ``_FAILURE_RETRY_S``;
    nothing here raises, since the caller is the coordinator tick.
    """
    global _fetched_at, _failed_at
    now = dt_util.utcnow()
    if _fetched_at is not None and (now - _fetched_at).total_seconds() < _REFRESH_S:
        return
    if _failed_at is not None and (now - _failed_at).total_seconds() < _FAILURE_RETRY_S:
        return
    try:
        steps = parse(await fetch_text(session, EXCISE_LAW_URL, timeout=15))
    except ExciseLawError as err:
        # The page came back and is not the article: a layout change, or a
        # bot check served instead of the law.
        _LOGGER.warning("The gas excise law could not be read: %s", err)
        _failed_at = now
        return
    except ExtractorError as err:
        # With nothing held, a month neither FPS Finance's tariff nor the
        # table in const.py gives is billed the excise its card prints, which
        # a stale card gets wrong, so say so once per retry. A held copy goes
        # on billing the law.
        log = _LOGGER.debug if _HELD else _LOGGER.warning
        log(
            "The gas excise law could not be fetched from Justel, so a month FPS "
            "Finance's tariff does not list is billed its card's own excise: %s",
            err,
        )
        _failed_at = now
        return
    hold(steps)
    _fetched_at = now
    _failed_at = None
