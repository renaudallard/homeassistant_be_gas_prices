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

"""The price history in the recorder, back to 1 January.

A fresh entry has no statistics, so the History and Energy dashboards would
draw its price from the install moment only. This writes an hourly ``mean``
row for each price sensor over a window, every month priced the way the
running costs price it: on its own card where the month cards hold one, on
the current card otherwise, the index value of the month where the supplier
published one.

Run once per calendar year by itself, and again on demand through the
``backfill_statistics`` service. The running costs are not backfilled: they
come from the meter's own history and are recomputed from it on every tick.
"""

from __future__ import annotations

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


async def backfill_prices(
    hass: HomeAssistant,
    coordinator: GasCoordinator,
    start: date,
    *,
    clear: bool = False,
) -> dict[str, int]:
    """Write the hourly price rows from ``start`` to the last full hour.

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

    entry_id = coordinator.entry.entry_id
    ids = {key: _statistic_id(hass, entry_id, key) for key, _unit in _SERIES}
    present = {key: sid for key, sid in ids.items() if sid is not None}
    if not present:
        return {}
    if clear:
        get_instance(hass).async_clear_statistics(list(present.values()))
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    begin = dt_util.as_utc(dt_util.start_of_local_day(start))
    rows: dict[str, list[Any]] = {key: [] for key in present}
    prices: dict[str, tuple[PriceBreakdown, float | None] | None] = {}
    for hour in _hours(begin, now):
        month = month_key(dt_util.as_local(hour).date())
        if month not in prices:
            prices[month] = coordinator.month_price(month)
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
