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

"""Setting an entry up in Home Assistant and reading its sensors."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.be_gas_prices import providers
from custom_components.be_gas_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_DAILY_COMPARE,
    CONF_DSO,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_SUPPLIER,
    CONVERSION_MANUAL,
    DOMAIN,
    DSO_ORES,
    DSO_SIBELGA,
    REGION_WALLONIA,
)
from custom_components.be_gas_prices.providers import engie
from custom_components.be_gas_prices.providers.base import CardNotReadableError, ExtractorError
from tests import fixture_text

TABLE = {"ZTPDAM": {"2026-07": 53.116, "2026-08": 61.537}}
DATA = {
    CONF_REGION: REGION_WALLONIA,
    CONF_DSO: DSO_ORES,
    CONF_SUPPLIER: "engie",
    CONF_CONTRACT: "engie_flow",
    CONF_ANNUAL_CONSUMPTION_KWH: 17_000.0,
    CONF_CONVERSION_MODE: CONVERSION_MANUAL,
    CONF_CONVERSION_FACTOR: 11.5,
    CONF_CARD_ARCHIVE: False,
}


@pytest.fixture
def fetch() -> Iterator[AsyncMock]:
    snapshot = engie.parse_snapshot(
        "engie_flow", REGION_WALLONIA, fixture_text("engie", "G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
    )
    fetch = AsyncMock(return_value=snapshot)
    stub = replace(
        engie.EXTRACTOR,
        fetch=fetch,
        fetch_index=AsyncMock(return_value=TABLE),
        fetch_for_month=None,
    )
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        yield fetch


async def _setup(hass: HomeAssistant, data: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=data or DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_sensors_price_the_household(hass: HomeAssistant, fetch: AsyncMock) -> None:
    await _setup(hass)
    price = hass.states.get("sensor.engie_flow_current_price")
    assert price is not None
    # September's ZTPDAM is not out: priced at August's 61,537, which is the
    # card's own figure, plus ORES T2 at 17 000 kWh, transport, the slice
    # excise and the Walloon connection fee.
    expected = 0.07643 + 0.02206 + 0.00165 + 0.0111936 + 0.000075
    assert float(price.state) == pytest.approx(expected, abs=5e-6)
    assert price.attributes["tier"] == "t2"
    assert price.attributes["index_month"] == "2026-08"
    assert price.attributes["price_provisional"] is True
    per_m3 = hass.states.get("sensor.engie_flow_current_price_per_m3")
    assert per_m3 is not None
    assert float(per_m3.state) == pytest.approx(expected * 11.5, abs=1e-4)
    fixed = hass.states.get("sensor.engie_flow_fixed_costs_per_year")
    assert fixed is not None
    assert float(fixed.state) == pytest.approx(50.0 + 140.93)
    # No meter: the running costs are unknown rather than zero.
    cost = hass.states.get("sensor.engie_flow_current_year_cost")
    assert cost is not None and cost.state == "unknown"


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_failing_supplier_keeps_the_last_card(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
    for _ in range(2):
        # A requested refresh waits out the cooldown of the one before it.
        await coordinator.async_force_refresh()
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert fetch.await_count == 3
    price = hass.states.get("sensor.engie_flow_current_price")
    assert price is not None and price.state != "unavailable"
    assert price.attributes["last_error"].startswith("Engie:")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}")
    assert issue is not None


@pytest.mark.freeze_time("2026-09-02 10:00:00+02:00")
async def test_a_card_the_probe_finds_unchanged_does_not_go_stale(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A supplier publishes once a month: a week of probes answering the same
    key is a week of confirmations, not a week without a card."""
    stub = replace(providers.EXTRACTORS["engie"], probe=AsyncMock(return_value="etag-1"))
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry = await _setup(hass)
        for _ in range(8 * 24):
            freezer.tick(timedelta(hours=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert not coordinator.snapshot_stale()
    assert coordinator.snapshot_age() <= timedelta(hours=1)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"snapshot_stale_{entry.entry_id}")
    assert issue is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_card_fetched_at_setup_is_kept_against_its_probe_key(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The first tick fetches without comparing keys, and still records the
    one it fetched under, so an unchanged card is not fetched twice."""
    stub = replace(providers.EXTRACTORS["engie"], probe=AsyncMock(return_value="etag-1"))
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        await _setup(hass)
        for _ in range(3):
            freezer.tick(timedelta(hours=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    assert fetch.await_count == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_earlier_contract_that_cannot_be_priced_is_named(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Its days are missing from the year's cost, which the sensor says."""
    days = {date(2026, 1, 1) + timedelta(days=day): 10.0 for day in range(258)}
    gone = {
        CONF_SUPPLIER: "dats24",
        CONF_CONTRACT: "dats24_variable",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-05-31",
    }
    with patch(
        "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
        AsyncMock(return_value=("energy", days)),
    ):
        await _setup(hass, {**DATA, CONF_PREVIOUS_CONTRACTS: [gone]})
    cost = hass.states.get("sensor.engie_flow_current_year_cost")
    assert cost is not None and cost.state != "unknown"
    assert cost.attributes["unpriced_contracts"] == ["dats24"]


@pytest.mark.freeze_time("2026-09-15 23:05:00+02:00")
async def test_the_daily_ranking_starts_at_its_minute(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """An entry ranking at 23:46 does not wait for an hourly tick, which
    lands at 23:05 here and at 00:05 the next day."""
    ranked = AsyncMock(return_value=([], 0))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Engie Flow",
        data={**DATA, CONF_DAILY_COMPARE: True},
        entry_id="rank0044",
    )
    entry.add_to_hass(hass)
    with patch("custom_components.be_gas_prices.coordinator.rank", ranked):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # Setup's own follow-up refresh, which would otherwise run late.
        freezer.tick(timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert ranked.await_count == 0
        freezer.move_to("2026-09-15 23:46:00+02:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert ranked.await_count == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_failed_setup_leaves_no_month_cards_task_behind(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The past months are fetched in the background from the first tick;
    a setup that then fails must not leave that fetch running, a new one
    starting on every retry."""
    gate = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_month(*_args: Any) -> None:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=slow_month)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        # Sibelga is not on a Walloon card: the first tick cannot price it.
        entry = MockConfigEntry(
            domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_DSO: DSO_SIBELGA}
        )
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state.name == "SETUP_RETRY"
        await hass.async_block_till_done()
        assert cancelled.is_set()
        gate.set()


async def test_setup_retries_when_there_is_no_card_at_all(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    fetch.side_effect = ExtractorError("network error fetching x: timeout")
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state.name == "SETUP_RETRY"


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_card_published_as_images_prices_on_the_archive_reading(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """No reader here reads the card; the card archive's OCR reading of the
    running month prices the entry, and the Repairs card says so."""
    card = fetch.return_value
    fetch.side_effect = CardNotReadableError("card has no text layer")
    row = AsyncMock(return_value=(card, True))
    with patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
    # The running month's row; the past months' cards are asked for too.
    asked = [call.args[1:] for call in row.await_args_list]
    assert ("engie", "engie_flow", REGION_WALLONIA, "2026-09") in asked
    price = hass.states.get("sensor.engie_flow_current_price")
    assert price is not None and price.state != "unavailable"
    assert price.attributes["card_source"] == "ocr"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_read_by_ocr_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_card_published_as_images_without_a_reading_is_unreadable(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    fetch.side_effect = CardNotReadableError("card has no text layer")
    with (
        patch(
            "custom_components.be_gas_prices.month_cards.fetch_archived_row",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator.fetch_archived_card",
            AsyncMock(return_value=None),
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_CARD_ARCHIVE: True}
        )
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state.name == "SETUP_RETRY"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"card_read_by_ocr_{entry.entry_id}") is None
