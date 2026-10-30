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

"""The running year's cost in the recorder, back to the start of its window.

Home Assistant counts a new total sensor's statistics from its first value,
so a monthly graph of ``current_year_cost`` on a fresh entry shows the gas
used since the install and nothing of the months before. This writes the
sensor's hourly rows from the start of the window: the bills the last tick
made, each month's use spread over its hours as the meter recorded them and
each day's share of the fixed costs added at its local midnight, as the
sensor adds it. The last row is then the sensor's own figure.

A running total cannot be written into the middle of the recorded one as it
is: the rows have to continue the total recorded before the window, and the
rows the live compile wrote after them have to be moved onto the imported
total, or the dashboards read a step that never happened.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .backfill import _backfill_end, _hours, _statistic_id
from .bill import MonthBill
from .coordinator import GasCoordinator
from .gas_meter import RecorderUnavailable, hourly_changes, statistic_kind

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData

_LOGGER = logging.getLogger(__name__)

_KEY = "current_year_cost"


def cost_rows(
    bills: Sequence[MonthBill],
    changes: Mapping[datetime, float],
    hours: Sequence[datetime],
) -> list[tuple[datetime, float]] | None:
    """The running total at the end of each of ``hours``, from ``bills``.

    ``changes`` is the meter's change in each hour. A day's use is shared
    out in proportion to its hours that rose, so a day the meter fell on
    weighs what the bills counted for it, nothing when it fell overall.
    None when an hour falls on a day no bill covers.
    """
    days: dict[date, list[datetime]] = {}
    for hour in hours:
        days.setdefault(dt_util.as_local(hour).date(), []).append(hour)
    weight: dict[datetime, float] = {}
    for day_hours in days.values():
        rises = [max(0.0, changes.get(hour, 0.0)) for hour in day_hours]
        rose = sum(rises)
        used = max(0.0, sum(changes.get(hour, 0.0) for hour in day_hours))
        for hour, rise in zip(day_hours, rises, strict=True):
            weight[hour] = 0.0 if rose == 0.0 else rise * used / rose
    cost = dict.fromkeys(hours, 0.0)
    covered: set[date] = set()
    for bill in bills:
        span = [bill.first + timedelta(days=n) for n in range(bill.days)]
        covered.update(span)
        span_hours = [hour for day in span for hour in days.get(day, [])]
        if not span_hours:
            continue
        for day in span:
            if day in days:
                cost[days[day][0]] += bill.fixed_cost / bill.days
        variable = bill.total - bill.fixed_cost
        units = sum(weight[hour] for hour in span_hours)
        if units == 0.0:
            # Gas the bill counted in an hour after the last one written.
            cost[span_hours[-1]] += variable
            continue
        for hour in span_hours:
            cost[hour] += variable * weight[hour] / units
    if not covered.issuperset(days):
        return None
    total = 0.0
    rows: list[tuple[datetime, float]] = []
    for hour in hours:
        total += cost[hour]
        rows.append((hour, total))
    return rows


async def backfill_cost(hass: HomeAssistant, coordinator: GasCoordinator) -> dict[str, int]:
    """Write ``current_year_cost``'s hourly rows from the start of the
    window to the last full hour, on the bills of the last tick.

    Returns the rows written per statistic id. Nothing is written without a
    recorder, before a tick has billed the year off the meter, while the
    cost is unknown, or when the recorder did not answer one of the reads.
    """
    if "recorder" not in hass.config.components:
        return {}
    data = coordinator.data
    meter = coordinator.meter
    if data is None or meter is None or data.current_year_cost is None or not data.months:
        # A tick that read no meter holds the cost without its bills.
        return {}
    sid = _statistic_id(hass, coordinator.entry.entry_id, _KEY)
    if sid is None:
        return {}
    # The instant the sensor gives as its last reset.
    reset = dt_util.as_utc(dt_util.start_of_local_day(coordinator.window_start(data.day)))
    hours = _hours(reset, _backfill_end())
    if not hours:
        return {}
    try:
        kind = await statistic_kind(hass, meter)
        if kind is None:
            return {}
        changes = await hourly_changes(hass, meter, kind, reset, hours[-1] + timedelta(hours=1))
    except RecorderUnavailable as err:
        _LOGGER.debug("cost backfill: %s", err)
        return {}
    totals = cost_rows(data.months, changes, hours)
    if totals is None:
        # Billed for another day than today: the next tick bills this one.
        return {}
    if not await _import_cost_rows(hass, sid, totals, reset):
        return {}
    return {sid: len(totals)}


async def backfill_cost_once(hass: HomeAssistant, coordinator: GasCoordinator) -> None:
    """Write the year's cost the first time an entry bills it off a meter,
    and again when what the year is priced from or the meter changes."""
    today = dt_util.now().date()
    stamp = f"{today.year}:{coordinator.card_months_signature()}:{coordinator.meter}"
    if coordinator.cost_backfill_stamp == stamp:
        return
    try:
        counts = await backfill_cost(hass, coordinator)
    except Exception:
        _LOGGER.exception("cost history backfill failed")
        return
    if counts:
        coordinator.cost_backfill_stamp = stamp
        await coordinator.async_save()
        _LOGGER.debug("cost history backfill wrote %s", counts)


async def _import_cost_rows(
    hass: HomeAssistant, sid: str, totals: list[tuple[datetime, float]], reset: datetime
) -> bool:
    """Import ``totals`` as a continuation of the recorded total.

    The live compile carries its total on across a new cycle rather than
    restart it, so on an entry that ran through last year the row before
    the window holds last year's whole bill: the rows are shifted by what
    the total held before them. The rows the live compile wrote after them,
    short-term and hourly, are moved onto the imported total, the newest
    short-term row, which the compile resumes from, with them; a short-term
    row at the last imported hour seeds it when there is none yet.

    False, with nothing imported, when the recorder did not answer one of
    the reads: a total it could not read, taken for none, put every row
    below the recorded one.
    """
    from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
    from homeassistant.components.recorder.db_schema import Statistics, StatisticsShortTerm
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )

    base = await _total_before(hass, sid, totals[0][0], reset)
    if base is None:
        return False
    rows = [
        StatisticData(
            start=hour,
            state=round(total, 4),
            sum=round(base + total, 4),
            last_reset=reset,
        )
        for hour, total in totals
    ]
    last = rows[-1]
    short = await _read(hass, sid, last["start"] + timedelta(minutes=5), None, "5minute")
    hourly = await _read(hass, sid, last["start"] + timedelta(hours=1), None, "hour")
    if short is None or hourly is None:
        return False
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=sid,
        unit_class=None,
        unit_of_measurement="EUR",
    )
    instance = get_instance(hass)
    instance.async_import_statistics(metadata, [*rows, *_moved(last, reset, hourly)], Statistics)
    instance.async_import_statistics(
        metadata, [last, *_moved(last, reset, short)], StatisticsShortTerm
    )
    return True


async def _read(
    hass: HomeAssistant,
    sid: str,
    start: datetime,
    end: datetime | None,
    period: Literal["5minute", "hour", "month"],
) -> list[Any] | None:
    """The rows of ``period`` from ``start`` to ``end``, or to the newest
    when None; None when the recorder did not answer."""
    from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
    from homeassistant.components.recorder.statistics import statistics_during_period

    try:
        got = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            end,
            {sid},
            period,
            None,
            {"sum", "state", "last_reset"},
        )
    except Exception as err:  # whatever the recorder raised
        _LOGGER.debug("could not read the statistics of %s: %s", sid, err)
        return None
    return list(got.get(sid, []))


async def _total_before(
    hass: HomeAssistant, sid: str, first: datetime, reset: datetime
) -> float | None:
    """What the recorded total holds before ``first`` beyond the cost of
    this cycle: all of it for a row of an earlier cycle, 0.0 with nothing
    recorded, None when the recorder did not answer.

    The last row is found by month, then by hour within that month, so a
    long gap costs two short reads. A read by month runs on to the end of
    the month ``first`` opens when it falls on the 1st, as 1 January does,
    so a month from ``first`` on is not one before it.
    """
    months = await _read(hass, sid, first - timedelta(days=400), first, "month")
    if months is None:
        return None
    months = [row for row in months if row["start"] < first.timestamp()]
    if not months:
        return 0.0
    hours = await _read(hass, sid, datetime.fromtimestamp(months[-1]["start"], UTC), first, "hour")
    if hours is None:
        return None
    if not hours or hours[-1].get("sum") is None:
        return 0.0
    row = hours[-1]
    total = float(row["sum"])
    if datetime.fromtimestamp(row["start"], UTC) < reset:
        return total
    return total - float(row.get("state") or 0.0)


def _moved(last: StatisticData, reset: datetime, later: list[Any]) -> list[StatisticData]:
    """The live rows after ``last``, moved onto its total.

    Each keeps its state, and the total moves from ``last`` by what the live
    compile added: the cost's move within the cycle, and the whole state of
    a new one across its reset, as the compile counts it. A row of an
    earlier cycle is left where it is.
    """
    from homeassistant.components.recorder.models import StatisticData

    cycle = reset.timestamp()
    total = float(last.get("sum") or 0.0)
    before = float(last.get("state") or 0.0)
    opened = cycle
    moved: list[StatisticData] = []
    for row in later:
        state, row_reset = row.get("state"), row.get("last_reset")
        if state is None or row_reset is None or row_reset < cycle:
            continue
        if row_reset != opened:
            before, opened = 0.0, row_reset
        total += state - before
        before = state
        moved.append(
            StatisticData(
                start=datetime.fromtimestamp(row["start"], UTC),
                state=state,
                sum=round(total, 4),
                last_reset=datetime.fromtimestamp(row_reset, UTC),
            )
        )
    return moved
