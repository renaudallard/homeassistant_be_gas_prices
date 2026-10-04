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

"""Reading the gas meter out of a real recorder."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.energy.data import EnergyManager, async_get_manager
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.be_gas_prices import gas_meter, providers
from custom_components.be_gas_prices.backfill import backfill_prices
from custom_components.be_gas_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_DSO,
    CONF_GAS_METER,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_SUPPLIER,
    CONVERSION_MANUAL,
    DOMAIN,
    DSO_ORES,
    REGION_WALLONIA,
    TIER_T1,
)
from custom_components.be_gas_prices.providers import engie
from custom_components.be_gas_prices.providers.base import SupplierSnapshot
from tests import fixture_text

METER = "sensor.gas_meter"


def _import(hass: HomeAssistant, unit: str, start: datetime, hours: int, per_hour: float) -> None:
    rows = [
        StatisticData(
            start=start + timedelta(hours=n),
            state=100.0 + per_hour * (n + 1),
            sum=per_hour * (n + 1),
        )
        for n in range(hours)
    ]
    async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=METER,
            unit_class="volume" if unit in ("m³", "L") else None,
            unit_of_measurement=unit,
        ),
        rows,
    )


async def _zone(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")


async def test_daily_consumption_in_cubic_metres(recorder_mock: Any, hass: HomeAssistant) -> None:
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    _import(hass, "m³", start, 48, 0.5)
    await async_wait_recording_done(hass)
    assert await gas_meter.statistic_kind(hass, METER) == "volume"
    days = await gas_meter.daily_consumption(
        hass, METER, "volume", date(2026, 9, 1), date(2026, 9, 2)
    )
    # The recorder counts the first imported hour's sum from zero, so the
    # first day holds all of its 24 half cubic metres.
    assert days[date(2026, 9, 1)] == pytest.approx(12.0)
    assert days[date(2026, 9, 2)] == pytest.approx(12.0)


async def test_litres_are_converted_to_cubic_metres(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    _import(hass, "L", start, 48, 500.0)
    await async_wait_recording_done(hass)
    days = await gas_meter.daily_consumption(
        hass, METER, "volume", date(2026, 9, 2), date(2026, 9, 2)
    )
    assert days[date(2026, 9, 2)] == pytest.approx(12.0)


async def test_a_unit_nothing_converts_is_refused(recorder_mock: Any, hass: HomeAssistant) -> None:
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    _import(hass, "l", start, 4, 1.0)
    await async_wait_recording_done(hass)
    with pytest.raises(gas_meter.RecorderUnavailable):
        await gas_meter.statistic_kind(hass, METER)


async def test_a_slow_energy_manager_is_left_to_finish(hass: HomeAssistant) -> None:
    """A tick that stops waiting for the energy manager's first load does
    not cancel it: the load completes and the next tick finds the meter."""
    loaded = asyncio.Event()

    async def slow_initialize(self: EnergyManager) -> None:
        await loaded.wait()
        prefs: Any = {"energy_sources": [{"type": "gas", "stat_energy_from": METER}]}
        self.data = prefs

    with (
        patch.object(EnergyManager, "async_initialize", slow_initialize),
        patch.object(gas_meter, "_ENERGY_MANAGER_TIMEOUT_S", 0.01),
    ):
        assert await gas_meter.discover_energy_gas_meter(hass) == (None, 0)
        loaded.set()
        manager = await asyncio.wait_for(async_get_manager(hass), timeout=1)
        assert manager.data is not None
        assert await gas_meter.discover_energy_gas_meter(hass) == (METER, 1)


def _card() -> SupplierSnapshot:
    return engie.parse_snapshot(
        "engie_flow", REGION_WALLONIA, fixture_text("engie", "G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
    )


async def _setup_entry(
    hass: HomeAssistant,
    card: SupplierSnapshot | None = None,
    extra: dict[str, Any] | None = None,
    fetch_for_month: AsyncMock | None = None,
) -> MockConfigEntry:
    """An Engie Flow entry in Wallonia, on the September card unless given
    another, read from the meter. The price history it writes at setup is
    left out: through a real recorder that is minutes of work, and a test
    below does it for a day."""
    stub = replace(
        engie.EXTRACTOR,
        fetch=AsyncMock(return_value=card or _card()),
        fetch_index=AsyncMock(return_value={"ZTPDAM": {"2026-08": 61.537}}),
        fetch_for_month=fetch_for_month,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Engie Flow",
        data={
            CONF_REGION: REGION_WALLONIA,
            CONF_DSO: DSO_ORES,
            CONF_SUPPLIER: "engie",
            CONF_CONTRACT: "engie_flow",
            CONF_ANNUAL_CONSUMPTION_KWH: 17000.0,
            CONF_CONVERSION_MODE: CONVERSION_MANUAL,
            CONF_CONVERSION_FACTOR: 11.5,
            CONF_CARD_ARCHIVE: False,
            CONF_GAS_METER: METER,
            **(extra or {}),
        },
    )
    entry.add_to_hass(hass)
    with (
        patch.dict(providers.EXTRACTORS, {"engie": stub}),
        patch("custom_components.be_gas_prices.backfill_once_a_year", AsyncMock()),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        # Setup's own refresh reads no meter; the one that does runs after it.
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry


@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_the_year_is_billed_from_the_meter(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    # 1 m3 an hour from 1 September 00:00 to 15 September 10:00.
    hours = 14 * 24 + 10
    _import(hass, "m³", start, hours, 1.0)
    await async_wait_recording_done(hass)
    entry = await _setup_entry(hass)
    assert entry.state is ConfigEntryState.LOADED
    data = entry.runtime_data.data
    kwh = hours * 11.5
    assert data.ytd_kwh == pytest.approx(kwh)
    september = data.months[-1]
    assert september.month == "2026-09"
    assert september.kwh == pytest.approx(kwh)
    assert september.days == 15
    # Every month from January to August is billed too, with no gas and a
    # share of the year's fixed costs.
    assert [bill.month for bill in data.months][:2] == ["2026-01", "2026-02"]
    fixed_year = 50.0 + 140.93
    expected = (
        kwh * data.breakdown.all_in + fixed_year * (date(2026, 9, 15).timetuple().tm_yday) / 365
    )
    assert data.current_year_cost == pytest.approx(expected, rel=1e-6)
    state = hass.states.get("sensor.engie_flow_current_year_cost")
    assert state is not None and float(state.state) == pytest.approx(round(expected, 2), abs=0.01)


@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_the_price_history_is_written_hour_by_hour(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """From midnight to the last full hour, every hour at the month's price,
    per kWh and per m3 at the bill's factor."""
    await _zone(hass)
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data
    counts = await backfill_prices(hass, coordinator, date(2026, 9, 15))
    kwh, m3 = "sensor.engie_flow_current_price", "sensor.engie_flow_current_price_per_m3"
    assert counts[kwh] == counts[m3] == 10
    assert len(counts) == 5
    await async_wait_recording_done(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 15)))
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {kwh, m3}, "hour", None, {"mean"}
    )
    price = coordinator.data.breakdown.all_in
    assert [row["mean"] for row in stats[kwh]] == [pytest.approx(price)] * 10
    assert [row["mean"] for row in stats[m3]] == [pytest.approx(price * 11.5)] * 10
    assert stats[kwh][0]["start"] == start.timestamp()


@pytest.mark.freeze_time("2026-01-01 10:30:00+01:00")
@pytest.mark.parametrize("held", [True, False])
async def test_a_month_before_the_window_is_priced_on_its_own_card(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None, held: bool
) -> None:
    """Asked from 31 December, the backfill prices December on December's
    card, fetched for it, and skips it where no card is found rather than
    drawing it at today's price."""
    await _zone(hass)
    card = _card()
    december = replace(
        card,
        energy=replace(card.energy, price=0.05),
        publication_label="2025-12",
        valid_until=date(2025, 12, 31),
    )
    months = AsyncMock(return_value=december if held else None)
    entry = await _setup_entry(hass, fetch_for_month=months)
    coordinator = entry.runtime_data
    months.reset_mock()
    counts = await backfill_prices(hass, coordinator, date(2025, 12, 31))
    assert [c.args[3] for c in months.await_args_list] == [date(2025, 12, 1)]
    kwh = "sensor.engie_flow_current_price"
    assert counts[kwh] == (24 if held else 0) + 10
    await async_wait_recording_done(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2025, 12, 31)))
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {kwh}, "hour", None, {"mean"}
    )
    means = [row["mean"] for row in stats[kwh]]
    price = coordinator.data.breakdown.all_in
    assert means[-10:] == [pytest.approx(price)] * 10
    if held:
        assert means[0] == pytest.approx(price - card.energy.price + 0.05)


@pytest.mark.freeze_time("2026-09-15 00:30:00+02:00")
async def test_a_backfill_with_no_hour_to_write_clears_nothing(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Asked from today half an hour into it: no hour is compiled yet, so a
    clear would delete the history and write nothing back."""
    await _zone(hass)
    entry = await _setup_entry(hass)
    instance = get_instance(hass)
    with patch.object(instance, "async_clear_statistics") as cleared:
        counts = await backfill_prices(hass, entry.runtime_data, date(2026, 9, 15), clear=True)
    assert counts == {}
    cleared.assert_not_called()


@pytest.mark.freeze_time("2026-09-15 10:02:00+02:00")
async def test_the_hour_the_recorder_is_compiling_is_left_to_it(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Two minutes past ten the recorder may not have queued its compile of
    nine to ten yet: an import before it would make it fail."""
    await _zone(hass)
    entry = await _setup_entry(hass)
    counts = await backfill_prices(hass, entry.runtime_data, date(2026, 9, 15))
    assert counts["sensor.engie_flow_current_price"] == 9


@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_the_price_history_stops_at_a_recorded_switch(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """The hours before the switch were the earlier contract's: asked from
    the day before, the backfill writes the current contract's day only."""
    await _zone(hass)
    earlier = {
        CONF_SUPPLIER: "engie",
        CONF_CONTRACT: "engie_easy_fixed",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-09-14",
    }
    entry = await _setup_entry(hass, extra={CONF_PREVIOUS_CONTRACTS: [earlier]})
    coordinator = entry.runtime_data
    assert coordinator.switch_day() == date(2026, 9, 15)
    counts = await backfill_prices(hass, coordinator, date(2026, 9, 14))
    assert counts["sensor.engie_flow_current_price"] == 10


@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_the_price_history_stops_at_a_switch_recorded_last_year(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A start date reaching back before a change made last November leaves
    the earlier contract's hours alone."""
    await _zone(hass)
    earlier = {
        CONF_SUPPLIER: "engie",
        CONF_CONTRACT: "engie_easy_fixed",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2025-11-14",
    }
    entry = await _setup_entry(hass, extra={CONF_PREVIOUS_CONTRACTS: [earlier]})
    assert entry.runtime_data.switch_day() == date(2025, 11, 15)


@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_a_card_that_cannot_bill_the_meter_fails_the_refresh(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A card without the household's tier (as Dots's Limburg row reads)
    fails the refresh with the reason, the months the meter bills included."""
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    _import(hass, "m³", start, 24, 1.0)
    await async_wait_recording_done(hass)
    card = _card()
    ores = card.dsos[DSO_ORES]
    card = replace(
        card, dsos={**card.dsos, DSO_ORES: replace(ores, tiers={TIER_T1: ores.tiers[TIER_T1]})}
    )
    entry = await _setup_entry(hass, card)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.runtime_data.last_error == "ores tariff has no T2 on this card"
