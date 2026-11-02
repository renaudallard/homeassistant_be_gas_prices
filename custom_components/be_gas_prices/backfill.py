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

"""The price history in the recorder, back to the start of the year's window.

A fresh entry has no statistics, so the History and Energy dashboards would
draw its price from the install moment only. This writes an hourly ``mean``
row for each price sensor over a window, every month priced the way the
running costs price it: on its own card where the month cards hold one, on
the current card otherwise, the index value of the month where the supplier
published one. A month before the year's window, which a start date asked
for can reach, is priced on its own card alone, fetched for the occasion,
and left out where none is found: today's card says nothing of last year.

Run once per calendar year by itself, and again on demand through the
``backfill_statistics`` service. The running year's cost is written by
backfill_cost.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .bill import month_key
from .const import DOMAIN
from .coordinator import GasCoordinator
from .pricing import PriceBreakdown

_LOGGER = logging.getLogger(__name__)

# How many hours the backfill prices between turns of the event loop: a long
# month. A year of five series at once held the loop for a fifth of a second
# on a Raspberry Pi.
_HOURS_PER_TURN = 744

# The price sensors and how each reads a month's price, with their unit.
_SERIES: tuple[tuple[str, str], ...] = (
    ("current_price", "EUR/kWh"),
    ("energy_component", "EUR/kWh"),
    ("network_component", "EUR/kWh"),
    ("taxes_component", "EUR/kWh"),
    ("current_price_m3", "EUR/m³"),
)


def _value(key: str, breakdown: PriceBreakdown, factor: float | None) -> float | None:
    if key == "current_price":
        return breakdown.all_in
    if key == "energy_component":
        return breakdown.energy
    if key == "network_component":
        return breakdown.network
    if key == "taxes_component":
        return breakdown.taxes
    return None if factor is None else breakdown.all_in * factor


def _statistic_id(hass: HomeAssistant, entry_id: str, key: str) -> str | None:
    """The entity id of one of the entry's sensors, which is its statistic id.

    Looked up in the entity registry by unique id, so a renamed entity keeps
    its history; None while the platform has not registered it yet.
    """
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{entry_id}_{key}")


def _hours(start: datetime, end: datetime) -> list[datetime]:
    """Every UTC hour in [start, end), on the hour."""
    hour = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if hour < start:
        hour += timedelta(hours=1)
    out: list[datetime] = []
    while hour < end:
        out.append(hour)
        hour += timedelta(hours=1)
    return out


def _newest_statistics_run(hass: HomeAssistant) -> datetime | None:
    """The start of the newest period the recorder has compiled, read on its
    own executor."""
    from homeassistant.components.recorder.db_schema import StatisticsRuns
    from homeassistant.components.recorder.models import process_timestamp
    from homeassistant.helpers.recorder import session_scope
    from sqlalchemy import func

    with session_scope(hass=hass, read_only=True) as session:
        start: datetime | None = session.query(func.max(StatisticsRuns.start)).scalar()
    return process_timestamp(start)


async def _backfill_end(hass: HomeAssistant) -> datetime:
    """The end of the newest hour the recorder has compiled.

    The recorder writes an hour's rows when it compiles that hour's last
    five minutes, ten seconds after the hour, and much later after a
    restart: its catch-up for the hours Home Assistant was down waits for
    startup to finish. It inserts those rows without an upsert, so a row an
    import got in first makes the insert fail, and the recorder then rolls
    back that whole period for every entity, its run marker with it, and
    never compiles it again. Each compiled period leaves a run marker and
    the catch-up resumes after the newest one, so every hour that ends by
    the end of that period is settled. Without the marker, the hour that
    has just ended is taken as not compiled yet.
    """
    from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
    from homeassistant.components.recorder.db_schema import StatisticsShortTerm

    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    try:
        newest = await get_instance(hass).async_add_executor_job(_newest_statistics_run, hass)
    except Exception as err:  # whatever the recorder raised
        _LOGGER.debug("could not read the recorder's last statistics run: %s", err)
        newest = None
    if newest is None:
        return hour - timedelta(hours=1)
    end: datetime = newest + StatisticsShortTerm.duration
    return min(hour, end.replace(minute=0, second=0, microsecond=0))


async def backfill_prices(
    hass: HomeAssistant,
    coordinator: GasCoordinator,
    start: date,
    *,
    clear: bool = False,
) -> dict[str, int]:
    """Write the hourly price rows from ``start``, or from the day the
    current contract took over at the last recorded switch, to the newest
    hour the recorder has compiled.

    Returns the rows written per statistic id, none without a recorder.
    ``clear`` deletes the sensors' statistics first, whole, since the
    recorder has no windowed delete: whatever sat before ``start`` is gone
    too.
    """
    if "recorder" not in hass.config.components:
        return {}
    from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import async_import_statistics

    switched = coordinator.switch_day()
    if switched is not None and start < switched:
        # The hours before were an earlier contract's, this year's or last,
        # which the price sensors showed then and which this, pricing the
        # current contract, would overwrite.
        start = switched
    entry_id = coordinator.entry.entry_id
    ids = {key: _statistic_id(hass, entry_id, key) for key, _unit in _SERIES}
    present = {key: sid for key, sid in ids.items() if sid is not None}
    if not present:
        return {}
    begin = dt_util.as_utc(dt_util.start_of_local_day(start))
    hours = _hours(begin, await _backfill_end(hass))
    if not hours:
        # Asked from today in its first hour: nothing to write yet, and a
        # clear would delete the history for nothing.
        return {}
    if clear:
        get_instance(hass).async_clear_statistics(list(present.values()))
    window = month_key(coordinator.window_start(dt_util.now().date()))
    early = sorted(
        {m for m in (month_key(dt_util.as_local(h).date()) for h in hours) if m < window}
    )
    if early:
        await coordinator.async_fill_month_cards(early)
    rows: dict[str, list[Any]] = {key: [] for key in present}
    prices: dict[str, tuple[PriceBreakdown, float | None] | None] = {}
    for n, hour in enumerate(hours, 1):
        if n % _HOURS_PER_TURN == 0:
            await asyncio.sleep(0)
        month = month_key(dt_util.as_local(hour).date())
        if month not in prices:
            prices[month] = coordinator.month_price(month, own_card=month < window)
        priced = prices[month]
        if priced is None:
            continue
        breakdown, factor = priced
        for key in present:
            value = _value(key, breakdown, factor)
            if value is not None:
                rows[key].append(StatisticData(start=hour, mean=value, min=value, max=value))
    units = dict(_SERIES)
    counts: dict[str, int] = {}
    for key, sid in present.items():
        counts[sid] = len(rows[key])
        if not rows[key]:
            continue
        async_import_statistics(
            hass,
            StatisticMetaData(
                mean_type=StatisticMeanType.ARITHMETIC,
                has_sum=False,
                name=None,
                source="recorder",
                statistic_id=sid,
                unit_class=None,
                unit_of_measurement=units[key],
            ),
            rows[key],
        )
    return counts


async def backfill_once_a_year(hass: HomeAssistant, coordinator: GasCoordinator) -> None:
    """Fill the year's price history the first time an entry runs in it.

    Stamped on the coordinator's store per calendar year, and redone when
    the card the current month is priced on changes month, so a year whose
    late months were drawn on a stand-in is redrawn once their cards land.
    """
    today = dt_util.now().date()
    stamp = f"{today.year}:{coordinator.card_months_signature()}"
    if coordinator.backfill_stamp == stamp:
        return
    try:
        counts = await backfill_prices(hass, coordinator, coordinator.window_start(today))
    except Exception:
        _LOGGER.exception("price history backfill failed")
        return
    if counts:
        coordinator.backfill_stamp = stamp
        await coordinator.async_save()
        _LOGGER.debug("price history backfill wrote %s", counts)
