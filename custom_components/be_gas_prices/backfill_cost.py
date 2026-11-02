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

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .backfill import _backfill_end, _hours, _statistic_id
from .bill import MonthBill
from .coordinator import GasCoordinator
from .gas_meter import RecorderUnavailable, hourly_changes, statistic_kind

_LOGGER = logging.getLogger(__name__)

_KEY = "current_year_cost"

# The locks _import_cost_rows holds while it joins a cycle, by statistic id.
_JOIN_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey("be_gas_prices_cost_joins")


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
    window to the newest hour the recorder has compiled, on the bills of
    the last tick.

    Returns the rows written per statistic id. Nothing is written without a
    recorder, before a tick has billed the year off the meter, while the
    cost is unknown, for a sensor the recorder does not compile, or when it
    did not answer one of the reads.
    """
    if "recorder" not in hass.config.components:
        return {}
    data = coordinator.data
    meter = coordinator.meter
    if data is None or meter is None or data.current_year_cost is None or not data.months:
        # A tick that read no meter holds the cost without its bills.
        return {}
    sid = _statistic_id(hass, coordinator.entry.entry_id, _KEY)
    if sid is None or not _compiled(hass, sid):
        return {}
    # The instant the sensor gives as its last reset.
    reset = dt_util.as_utc(dt_util.start_of_local_day(coordinator.window_start(data.day)))
    hours = _hours(reset, await _backfill_end(hass))
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
    # Off the event loop: a year of hours is a twentieth of a second of it on
    # a Raspberry Pi.
    totals = await hass.async_add_executor_job(cost_rows, data.months, changes, hours)
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
    """Import ``totals`` as the cycle of the recorded total.

    The rows continue the total recorded before the window: the live compile
    carries its total on across a new cycle rather than restart it, so on an
    entry that ran through last year the row before the window holds last
    year's whole bill.

    The rows the live compile wrote are moved onto the imported total by the
    recorder: an adjustment queued ahead of the import shifts the total of
    every row from the reset by what lies between the two, five-minute and
    hourly alike, and the import then writes its hours over the live ones. A
    row compiled before the adjustment reaches the queue is shifted with the
    rest, and one compiled after it carries on from a shifted one. Read and
    written back from here, the rows the recorder compiled between the read
    and the import stayed on the old total, and the next hour fell by the
    whole bill. The five-minute rows are all the sensor's own, so they climb
    as it did; where it showed another figure than the rewritten cycle, the
    difference lands in the hour after the last one written.

    False, with nothing imported, when the recorder did not answer one of
    the reads, or holds no live row of the sensor yet to join the cycle to:
    a new sensor's first is compiled within five minutes.
    """
    from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
    from homeassistant.components.recorder.db_schema import Statistics
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )

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
    # One join at a time, each read once the one before it is written: the
    # shift is what the reads found between the two totals, and two joins
    # that read before either wrote, a tick's and a service call's, moved the
    # live rows twice.
    async with hass.data.setdefault(_JOIN_LOCKS, {}).setdefault(sid, asyncio.Lock()):
        base = await _total_before(hass, sid, totals[0][0])
        if base is None:
            return False
        anchor = await _live_anchor(hass, sid, totals[-1][0] + timedelta(hours=1))
        if anchor is None:
            return False
        state, total = anchor
        rows = [
            StatisticData(
                start=hour,
                state=round(value, 4),
                sum=round(base + value, 4),
                last_reset=reset,
            )
            for hour, value in totals
        ]
        # Within a cycle the live compile adds to the total exactly what the
        # state moves by, a new price included, so every live row stands the
        # same distance above or below its state, and one shift puts them all
        # on the imported total. None is needed once they are there, as on
        # every run after the first.
        shift = round(base - (total - state), 4)
        if shift:
            instance.async_adjust_statistics(sid, reset, shift, "EUR")
        instance.async_import_statistics(metadata, rows, Statistics)
        await instance.async_block_till_done()
    return True


def _compiled(hass: HomeAssistant, sid: str) -> bool:
    """Whether the recorder compiles the statistics of ``sid``: an enabled
    sensor it records. The cycle joins the live rows, and a sensor it does
    not compile never has any, which every tick would read the meter's year
    for again."""
    from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]

    found = er.async_get(hass).async_get(sid)
    if found is not None and found.disabled_by is not None:
        return False
    # The filter is_entity_recorded reads: newer releases (2026.9.4) only
    # re-export that function from the recorder package, and strict mypy
    # refuses the import there.
    recorded = get_instance(hass).entity_filter
    return recorded is None or recorded(sid)


async def _live_anchor(hass: HomeAssistant, sid: str, end: datetime) -> tuple[float, float] | None:
    """The state and the total of a live row of the cycle near ``end``: the
    sensor's hour before it, or, for a sensor that started after it, its
    first five minutes. None when the recorder did not answer, or holds
    neither."""
    rows = await _read(hass, sid, end - timedelta(hours=1), end, "hour")
    if rows is not None and not rows:
        rows = await _read(hass, sid, end, None, "5minute")
    for row in rows or []:
        if row.get("state") is not None and row.get("sum") is not None:
            return float(row["state"]), float(row["sum"])
    return None


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
            {"sum", "state"},
        )
    except Exception as err:  # whatever the recorder raised
        _LOGGER.debug("could not read the statistics of %s: %s", sid, err)
        return None
    return list(got.get(sid, []))


async def _total_before(hass: HomeAssistant, sid: str, first: datetime) -> float | None:
    """What the recorded total holds before ``first``, the whole of an
    earlier cycle: 0.0 with nothing recorded, None when the recorder did not
    answer. The window opens at the cycle's reset, so no row before its first
    hour is of this cycle.

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
    return float(hours[-1]["sum"])
