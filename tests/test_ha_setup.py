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
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.be_gas_prices import calorific, providers
from custom_components.be_gas_prices.compare import IndexCache, Quote, quote_contract
from custom_components.be_gas_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_CUSTOM_EXCISE_LOW,
    CONF_CUSTOM_PRICE,
    CONF_CUSTOM_T1_FIXED,
    CONF_CUSTOM_T1_PROP,
    CONF_CUSTOM_T2_FIXED,
    CONF_CUSTOM_T2_PROP,
    CONF_CUSTOM_TRANSPORT,
    CONF_DAILY_COMPARE,
    CONF_DSO,
    CONF_GAS_METER,
    CONF_MANUAL_FACTOR,
    CONF_MANUAL_PRICE,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_STATION,
    CONF_SUPPLIER,
    CONVERSION_MANUAL,
    CONVERSION_STATION,
    CUSTOM_CONTRACT,
    DOMAIN,
    DSO_FLUVIUS_IMEWO,
    DSO_ORES,
    DSO_RESA,
    DSO_SIBELGA,
    REGION_FLANDERS,
    REGION_WALLONIA,
    SUPPLIER_CUSTOM,
    TIER_T1,
)
from custom_components.be_gas_prices.coordinator import GasCoordinator
from custom_components.be_gas_prices.gas_meter import RecorderUnavailable
from custom_components.be_gas_prices.month_cards import MonthCard
from custom_components.be_gas_prices.providers import engie, octaplus
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import CardNotReadableError, ExtractorError
from custom_components.be_gas_prices.running_costs import Household
from custom_components.be_gas_prices.snapshot_codec import snapshot_to_json
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


@pytest.fixture(autouse=True)
def _no_card_archive_rows() -> Iterator[None]:
    """An entry that lets the card archive be read asks it for its past
    months' cards in the background, and for a stand-in when its card cannot
    be read; no test here may reach the network."""
    with (
        patch(
            "custom_components.be_gas_prices.month_cards.fetch_archived_card",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator.fetch_archived_row",
            AsyncMock(return_value=None),
        ),
    ):
        yield


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
    # Setup's own refresh reads no meter; the one that does runs after it.
    await hass.async_block_till_done(wait_background_tasks=True)
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
        # Each press is a fetch of its own.
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


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_network_failure_keeps_an_unreadable_card_marked(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A timeout between two fetches of a card published as images says
    nothing about the card: its Repairs card is not swapped for the one about
    a layout change."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    issues = ir.async_get(hass)
    fetch.side_effect = CardNotReadableError("card has no text layer")
    for _ in range(2):
        await coordinator.async_force_refresh(wait=True)
    fetch.side_effect = ExtractorError("network error fetching https://x: timeout")
    await coordinator.async_force_refresh(wait=True)
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_disabling_a_retrying_entry_clears_its_repairs_cards(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Home Assistant does not unload an entry whose setup is retrying, so
    its cards are cleared when it stops."""
    fetch.side_effect = CardNotReadableError("card has no text layer")
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issues = ir.async_get(hass)
    key = f"card_unreadable_{entry.entry_id}"
    assert issues.async_get_issue(DOMAIN, key) is not None
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, key) is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
@pytest.mark.parametrize("start", ["0001-01-01", "1970-01-01", "2024-12-31"])
async def test_a_backfill_before_last_year_is_refused(
    hass: HomeAssistant, fetch: AsyncMock, start: str
) -> None:
    """The card archive keeps a year of cards: an older start has none to
    price on, and year 1 used to crash the service."""
    entry = await _setup(hass)
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            "backfill_statistics",
            {"entry_id": entry.entry_id, "start_date": start},
            blocking=True,
            return_response=True,
        )
    assert raised.value.translation_key == "start_date_too_early"
    assert raised.value.translation_placeholders == {"earliest": "2025-01-01"}


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_backfill_from_the_future_is_refused_before_any_clear(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A date picked a year too late, with clear on, would delete the price
    history and write nothing back."""
    entry = await _setup(hass)
    with (
        patch("custom_components.be_gas_prices.backfill_prices") as backfill,
        pytest.raises(ServiceValidationError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            "backfill_statistics",
            {"entry_id": entry.entry_id, "start_date": "2026-09-16", "clear": True},
            blocking=True,
            return_response=True,
        )
    assert raised.value.translation_key == "start_date_in_future"
    backfill.assert_not_called()


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_refresh_outliving_the_entry_leaves_nothing_behind(
    hass: HomeAssistant,
    fetch: AsyncMock,
    freezer: FrozenDateTimeFactory,
    hass_storage: dict[str, Any],
) -> None:
    """A refresh the service asked for is still fetching when the entry is
    removed: its Repairs card and its store are not brought back."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
    for _ in range(2):
        await coordinator.async_force_refresh(wait=True)
    issues = ir.async_get(hass)
    issue = f"extractor_failed_{entry.entry_id}"
    assert issues.async_get_issue(DOMAIN, issue) is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        await release.wait()
        raise ExtractorError("Engie: variable price block or formula not found")

    fetch.side_effect = slow
    freezer.tick(timedelta(seconds=11))
    call = hass.async_create_task(
        hass.services.async_call(DOMAIN, "refresh", {"entry_id": entry.entry_id}, blocking=True)
    )
    await asyncio.wait_for(started.wait(), 5)
    await hass.config_entries.async_remove(entry.entry_id)
    release.set()
    await call
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, issue) is None
    assert f"{DOMAIN}.{entry.entry_id}" not in hass_storage


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_refresh_outliving_a_reload_leaves_the_new_coordinator_alone(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The old coordinator's slow refresh ends after a reload: it raises no
    card over the new coordinator's state."""
    entry = await _setup(hass)
    old = entry.runtime_data
    started = asyncio.Event()
    release = asyncio.Event()
    card = fetch.return_value

    async def slow(*_args: Any, **_kwargs: Any) -> Any:
        started.set()
        await release.wait()
        raise ExtractorError("Engie: variable price block or formula not found")

    fetch.side_effect = slow
    old._failures = 1
    freezer.tick(timedelta(seconds=11))
    call = hass.async_create_task(
        hass.services.async_call(DOMAIN, "refresh", {"entry_id": entry.entry_id}, blocking=True)
    )
    await asyncio.wait_for(started.wait(), 5)
    fetch.side_effect = None
    fetch.return_value = card
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data is not old
    release.set()
    await call
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


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


@pytest.mark.freeze_time("2026-09-30 23:30:00+02:00")
async def test_a_card_whose_month_is_over_is_asked_for_again(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """Without a probe, September's card is not kept through 1 October for
    the rest of its TTL: every tick asks until October's is out."""
    await _setup(hass)
    assert fetch.await_count == 1
    for count in (2, 3):
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert fetch.await_count == count
    september = fetch.return_value
    fetch.return_value = replace(
        september, publication_label="2026-10", valid_until=date(2026, 10, 31)
    )
    for count in (4, 4):
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert fetch.await_count == count


@pytest.mark.freeze_time("2026-09-30 23:40:00+02:00")
async def test_the_month_cost_resets_with_the_tick_not_the_clock(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The daily ranking writes the states at its own minute, which can come
    after midnight and before the first tick of the day: September's figures
    must not go out with October's last_reset."""
    entry = await _setup(hass)
    freezer.move_to("2026-10-01 00:06:00+02:00")
    entry.runtime_data.async_update_listeners()
    await hass.async_block_till_done()
    month = hass.states.get("sensor.engie_flow_current_month_cost")
    assert month is not None
    assert month.attributes["last_reset"].startswith("2026-09-01T00:00:00")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    month = hass.states.get("sensor.engie_flow_current_month_cost")
    assert month is not None
    assert month.attributes["last_reset"].startswith("2026-10-01T00:00:00")


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_month_cost_attributes_cover_a_switch_month_whole(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """With a change of contract on 10 September, the month's state adds up
    both contracts' bills, and its attributes do the same."""
    earlier = {
        CONF_SUPPLIER: "engie",
        CONF_CONTRACT: "engie_flow",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-09-09",
    }
    days = {date(2026, 1, 1) + timedelta(days=n): 10.0 for n in range(258)}
    with patch(
        "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
        AsyncMock(return_value=("energy", days)),
    ):
        entry = await _setup(hass, {**DATA, CONF_PREVIOUS_CONTRACTS: [earlier]})
    september = [b for b in entry.runtime_data.data.months if b.month == "2026-09"]
    assert len(september) == 2
    state = hass.states.get("sensor.engie_flow_current_month_cost")
    assert state is not None
    assert state.attributes["kwh"] == pytest.approx(150.0)
    parts = ("energy_eur", "network_eur", "taxes_eur", "fixed_eur")
    assert sum(state.attributes[k] for k in parts) == pytest.approx(float(state.state), abs=0.03)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_months_on_the_current_card_name_every_contract_s(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """No month card is served: the earlier contract's months, its last one
    included, are billed on today's card like the current contract's."""
    earlier = {
        CONF_SUPPLIER: "engie",
        CONF_CONTRACT: "engie_flow",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-06-30",
    }
    days = {date(2026, 1, 1) + timedelta(days=n): 10.0 for n in range(258)}
    with patch(
        "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
        AsyncMock(return_value=("energy", days)),
    ):
        await _setup(hass, {**DATA, CONF_PREVIOUS_CONTRACTS: [earlier]})
    state = hass.states.get("sensor.engie_flow_current_year_cost")
    assert state is not None
    assert state.attributes["months_on_current_card"] == [f"2026-{m:02d}" for m in range(1, 9)]


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_earlier_contract_s_last_month_falls_back_like_any_other(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The earlier contract ends in June, and June's own card lists no ORES
    row: June is billed on today's card and named, rather than the whole
    contract going unpriced."""
    card = fetch.return_value
    june = replace(
        card,
        dsos={k: v for k, v in card.dsos.items() if k != DSO_ORES},
        publication_label="2026-06",
        valid_until=date(2026, 6, 30),
    )

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        return june if month == date(2026, 6, 1) else None

    earlier = {
        CONF_SUPPLIER: "engie",
        CONF_CONTRACT: "engie_flow",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-06-30",
    }
    days = {date(2026, 1, 1) + timedelta(days=n): 10.0 for n in range(258)}
    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with (
        patch.dict(providers.EXTRACTORS, {"engie": stub}),
        patch(
            "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
            AsyncMock(return_value=("energy", days)),
        ),
    ):
        entry = await _setup(hass, {**DATA, CONF_PREVIOUS_CONTRACTS: [earlier]})
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    cost = hass.states.get("sensor.engie_flow_current_year_cost")
    assert cost is not None
    assert cost.attributes["unpriced_contracts"] == []
    assert "2026-06" in cost.attributes["months_on_current_card"]
    assert cost.attributes["ytd_kwh"] == pytest.approx(2580.0)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_last_year_s_unpriced_contract_is_not_named_in_the_new_year(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    gone = {
        CONF_SUPPLIER: "dats24",
        CONF_CONTRACT: "dats24_variable",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-05-31",
    }
    days = {date(2025, 1, 1) + timedelta(days=n): 10.0 for n in range(800)}
    with patch(
        "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
        AsyncMock(return_value=("energy", days)),
    ):
        entry = await _setup(hass, {**DATA, CONF_PREVIOUS_CONTRACTS: [gone]})
        cost = hass.states.get("sensor.engie_flow_current_year_cost")
        assert cost is not None and cost.attributes["unpriced_contracts"] == ["dats24"]
        freezer.move_to("2027-01-02 10:00:00+01:00")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    cost = hass.states.get("sensor.engie_flow_current_year_cost")
    assert cost is not None and cost.attributes["unpriced_contracts"] == []


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_setup_does_not_wait_on_the_earlier_contract_s_month_cards(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The earlier contract's months are fetched in the background, like
    the current one's, rather than inside the tick setup waits on."""
    gate = asyncio.Event()
    asked: list[date] = []

    async def slow(_session: Any, _contract: str, _region: str, month: date) -> Any:
        asked.append(month)
        await gate.wait()
        return None

    earlier = {
        CONF_SUPPLIER: "engie",
        CONF_CONTRACT: "engie_flow",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-06-30",
    }
    days = {date(2026, 1, 1) + timedelta(days=n): 10.0 for n in range(258)}
    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=slow)
    with (
        patch.dict(providers.EXTRACTORS, {"engie": stub}),
        patch(
            "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
            AsyncMock(return_value=("energy", days)),
        ),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_PREVIOUS_CONTRACTS: [earlier]}
        )
        entry.add_to_hass(hass)
        assert await asyncio.wait_for(hass.config_entries.async_setup(entry.entry_id), 5)
        assert entry.state is ConfigEntryState.LOADED
        gate.set()
        await hass.async_block_till_done()
    assert date(2026, 1, 1) in asked


@pytest.mark.freeze_time("2026-09-30 18:00:00+02:00")
async def test_a_card_put_up_early_is_read_once_its_month_begins(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """October's card is online on 30 September under a probe key that will
    not change again: September's card stands in for it that evening, and
    from 1 October the card is read again although the key is the same."""
    september = fetch.return_value
    october = replace(september, publication_label="2026-10", valid_until=date(2026, 10, 31))
    fetch.return_value = october

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        return {date(2026, 9, 1): september, date(2026, 10, 1): october}.get(month)

    stub = replace(
        providers.EXTRACTORS["engie"],
        fetch_for_month=for_month,
        probe=AsyncMock(return_value="Wed, 30 Sep 2026 12:57:20 GMT"),
    )
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry = await _setup(hass)
        assert entry.runtime_data.data.snapshot.publication_label == "2026-09"
        freezer.move_to("2026-10-01 10:00:00+02:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert entry.runtime_data.data.snapshot.publication_label == "2026-10"


async def test_an_images_only_supplier_without_the_archive_stops_the_entry(
    hass: HomeAssistant,
) -> None:
    """An Ecofix entry that keeps the archive out has no card it can read:
    it stops with that reason rather than retrying and downloading."""
    fetch = AsyncMock(side_effect=CardNotReadableError("card has no text layer"))
    stub = replace(providers.EXTRACTORS["ecofix"], fetch=fetch)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ecofix Flexy",
        data={**DATA, CONF_SUPPLIER: "ecofix", CONF_CONTRACT: "ecofix_flexy"},
    )
    entry.add_to_hass(hass)
    with patch.dict(providers.EXTRACTORS, {"ecofix": stub}):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None and "Ecofix" in entry.reason
    fetch.assert_not_called()


async def test_a_withdrawn_contract_stops_the_entry_with_its_reason(
    hass: HomeAssistant,
) -> None:
    """OCTA+ Flux was withdrawn in October 2026: no card of it is read, and
    the entry says so rather than retrying or blaming a layout change."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="OCTA+ Flux",
        data={**DATA, CONF_SUPPLIER: "octaplus", CONF_CONTRACT: "octaplus_flux"},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None and "OCTA+ Flux" in entry.reason


@pytest.mark.freeze_time("2026-10-15 10:00:00+02:00")
async def test_a_withdrawn_contract_s_entry_is_set_up_once_another_is_picked(
    hass: HomeAssistant,
) -> None:
    card = octaplus.parse_snapshot(
        "octaplus_boostflex",
        REGION_WALLONIA,
        fixture_text("octaplus", "G_OCTA_BOOSTFLEX_RE_WL_FR.pdf", "layout"),
    )
    stub = replace(
        octaplus.EXTRACTOR,
        fetch=AsyncMock(return_value=card),
        fetch_index=AsyncMock(return_value={}),
        fetch_for_month=None,
        probe=None,
    )
    data = {**DATA, CONF_SUPPLIER: "octaplus", CONF_CONTRACT: "octaplus_flux"}
    entry = MockConfigEntry(domain=DOMAIN, title="OCTA+ Flux", data=data)
    entry.add_to_hass(hass)
    with patch.dict(providers.EXTRACTORS, {"octaplus": stub}):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        flows = hass.config_entries.options
        result = await flows.async_init(entry.entry_id)
        for user_input in (
            {"next_step_id": "settings"},
            {},
            {CONF_REGION: REGION_WALLONIA},
            {CONF_SUPPLIER: "octaplus"},
            {CONF_CONTRACT: "octaplus_boostflex"},
            {CONF_DSO: DSO_ORES},
            {
                CONF_ANNUAL_CONSUMPTION_KWH: 17000.0,
                CONF_CONVERSION_FACTOR: 11.5,
                CONF_CARD_ARCHIVE: False,
                CONF_DAILY_COMPARE: False,
            },
        ):
            result = await flows.async_configure(result["flow_id"], user_input)
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


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


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_ranking_stored_for_another_contract_is_not_served(
    hass: HomeAssistant, fetch: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    """Today's ranking was made while the household was on another contract:
    its saving is against a contract left, so it is not shown."""
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "ranking": {
                "day": "2026-09-15",
                "own": ["luminus", "luminus_comfyflex"],
                "rows": [
                    ["engie", "engie_flow", "Engie Flow", 1400.0],
                    ["luminus", "luminus_comfyflex", "Luminus ComfyFlex", 1900.0],
                ],
            }
        },
    }
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.daily_ranking is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_ranking_made_under_other_settings_is_ranked_again(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A reload keeps the day's ranking; a change of the yearly volume ranks
    the day again, since the saving was priced for another household."""
    cheaper = Quote("luminus", "x", "Cheap", 1000.0, 0.08, 100.0, True, False)
    ranked = AsyncMock(return_value=([cheaper], 0))
    with (
        patch("custom_components.be_gas_prices.coordinator.rank", ranked),
        patch("custom_components.be_gas_prices.coordinator.ranking_minute", return_value=0),
    ):
        entry = await _setup(hass, {**DATA, CONF_DAILY_COMPARE: True})
        assert ranked.await_count == 1
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert ranked.await_count == 1
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ANNUAL_CONSUMPTION_KWH: 4000.0}
        )
        await hass.async_block_till_done()
        assert ranked.await_count == 2


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_ranking_that_priced_nothing_is_tried_again(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    failed = Quote("luminus", "x", "Cheap", None, None, None, False, False, "timeout")
    cheaper = Quote("luminus", "x", "Cheap", 1000.0, 0.08, 100.0, True, False)
    ranked = AsyncMock(side_effect=[([failed], 0), ([cheaper], 0)])
    with (
        patch("custom_components.be_gas_prices.coordinator.rank", ranked),
        patch("custom_components.be_gas_prices.coordinator.ranking_minute", return_value=0),
    ):
        entry = await _setup(hass, {**DATA, CONF_DAILY_COMPARE: True})
        assert entry.runtime_data.daily_ranking is None
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert ranked.await_count == 2
    ranking = entry.runtime_data.daily_ranking
    assert ranking is not None and ranking.rows


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_ranking_quotes_the_household_at_what_it_pays(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The own row is the entry's own price, typed figures included, not
    the card of the month a new customer would sign."""
    data = {**DATA, CONF_CONTRACT_START_DATE: "2026-03-01", CONF_MANUAL_FACTOR: 0.2}
    entry = await _setup(hass, data)
    own = entry.runtime_data.own_contract()
    assert own is not None and own.contract == "engie_flow"
    energy = own.card.energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(0.2 / 100 * 1.06)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_ranking_that_priced_only_the_household_is_tried_again(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The network down at the ranking minute: the own row is priced on the
    card in hand, every other quote fails, and the day is ranked again."""
    own = Quote("engie", "engie_flow", "Engie Flow", 1500.0, 0.09, 100.0, True, False)
    failed = Quote("luminus", "x", "Cheap", None, None, None, False, False, "timeout")
    cheaper = Quote("luminus", "x", "Cheap", 1000.0, 0.08, 100.0, True, False)
    ranked = AsyncMock(side_effect=[([own, failed], 0), ([cheaper, own], 0)])
    with (
        patch("custom_components.be_gas_prices.coordinator.rank", ranked),
        patch("custom_components.be_gas_prices.coordinator.ranking_minute", return_value=0),
    ):
        entry = await _setup(hass, {**DATA, CONF_DAILY_COMPARE: True})
        assert entry.runtime_data.daily_ranking is None
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    ranking = entry.runtime_data.daily_ranking
    assert ranking is not None and ranking.saving == pytest.approx(500.0)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_pricing_error_clears_once_the_card_prices_again(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The ORES row has no T1: a measured year falling into it fails the
    tick, and the reason goes once the year is back in T2, though the card,
    not due, is not fetched again."""
    card = fetch.return_value
    ores = card.dsos[DSO_ORES]
    no_t1 = replace(ores, tiers={k: v for k, v in ores.tiers.items() if k != "t1"})
    fetch.return_value = replace(card, dsos={**card.dsos, DSO_ORES: no_t1})

    def days(per_day: float) -> dict[date, float]:
        return {date(2025, 9, 15) + timedelta(days=n): per_day for n in range(366)}

    meter = AsyncMock(return_value=("energy", days(50.0)))
    with patch("custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter", meter):
        entry = await _setup(hass)
        coordinator = entry.runtime_data
        meter.return_value = ("energy", days(10.0))
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
        assert not coordinator.last_update_success
        assert coordinator.last_error
        meter.return_value = ("energy", days(50.0))
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.last_error == ""
    assert coordinator.data.last_error == ""
    assert fetch.await_count == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_one_unreadable_calorific_month_leaves_the_others_read(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """March's file has a bad value: the months after it are read all the
    same, and August converts on August's value."""
    station = "541234"
    months = {f"2026-{m:02d}": f"GCV2026{m:02d}.txt" for m in range(1, 9)}

    async def fetch_month(_session: Any, _context: Any, _key: Any, path: str) -> Any:
        if path == "GCV202603.txt":
            raise calorific.CalorificError("calorific value file has a bad value 'n/a'")
        return {calorific.Station(ean=station, name="X"): 11.0 + int(path[7:9]) / 100}

    data = {**DATA, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: station}
    with (
        patch.object(calorific, "build_ssl_context", lambda: None),
        patch.object(calorific, "subscription_key", AsyncMock(return_value="k")),
        patch.object(calorific, "list_months", AsyncMock(return_value=months)),
        patch.object(calorific, "fetch_month", fetch_month),
    ):
        entry = await _setup(hass, data)
    assert entry.runtime_data._m3_factor("2026-08") == (pytest.approx(11.08), "2026-08")


_READ_METER = "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter"


def _a_year_of_gas() -> dict[date, float]:
    return {date(2025, 9, 15) + timedelta(days=n): 10.0 for n in range(366)}


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_setup_s_own_refresh_reads_no_meter(hass: HomeAssistant, fetch: AsyncMock) -> None:
    """Home Assistant waits on it: the meter is read by the refresh setup
    starts in the background once it is done, once, though the past months'
    cards landing ask for a reprice of their own too."""
    read = AsyncMock(return_value=("energy", _a_year_of_gas()))
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    with patch(_READ_METER, read):
        coordinator = GasCoordinator(hass, entry, defer_meter_reads=True)
        await coordinator.async_refresh()
        assert read.await_count == 0
        assert coordinator.meter_reads_pending
        await coordinator.async_reprice(lambda: coordinator.meter_reads_pending)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert read.await_count == 1
    assert not coordinator.meter_reads_pending
    assert coordinator.data.current_year_cost is not None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_restart_shows_the_figures_from_before_it_until_the_meter_is_read(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    with patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))):
        entry = await _setup(hass)
    before = entry.runtime_data.data
    assert before.current_year_cost is not None and before.annual_kwh_measured
    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", _a_year_of_gas())

    with patch(_READ_METER, slow):
        assert await hass.config_entries.async_reload(entry.entry_id)
        coordinator = entry.runtime_data
        held = coordinator.data
        assert coordinator.meter_reads_pending
        assert held.current_year_cost == pytest.approx(before.current_year_cost)
        assert held.current_month_cost == pytest.approx(before.current_month_cost)
        assert held.rolling_year_kwh == pytest.approx(before.rolling_year_kwh)
        assert held.annual_kwh == pytest.approx(before.annual_kwh)
        assert held.annual_kwh_measured
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator.meter_reads_pending
    assert coordinator.data.months


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_setup_s_own_tick_still_names_the_meter(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """It reads no statistics, but which meter it is comes from the Energy
    dashboard's settings: the several-meters card and the meter attribute
    are there at once, the unit card once the meter is read."""
    gate = asyncio.Event()

    async def kind(_hass: Any, _meter: str) -> Any:
        await gate.wait()
        raise RecorderUnavailable("sensor.gas reports in a unit that is no volume")

    with (
        patch(
            "custom_components.be_gas_prices.coordinator.discover_energy_gas_meter",
            AsyncMock(return_value=("sensor.gas", 2)),
        ),
        patch("custom_components.be_gas_prices.coordinator.statistic_kind", kind),
    ):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        issues = ir.async_get(hass)
        assert entry.runtime_data.meter_reads_pending
        assert entry.runtime_data.data.meter == "sensor.gas"
        assert issues.async_get_issue(DOMAIN, f"several_meters_{entry.entry_id}")
        assert not issues.async_get_issue(DOMAIN, f"meter_unit_{entry.entry_id}")
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert issues.async_get_issue(DOMAIN, f"meter_unit_{entry.entry_id}")


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_meter_read_after_setup_asks_no_supplier_again(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A card that fails once at setup counts once: the meter read setup
    starts right after its own tick leaves the card to the next tick."""
    card = fetch.return_value
    fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
    with patch(
        "custom_components.be_gas_prices.coordinator.fetch_archived_row",
        AsyncMock(return_value=(card, False)),
    ):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
    assert not entry.runtime_data.meter_reads_pending
    assert fetch.await_count == 1
    assert entry.runtime_data.failures == 1
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_setup_reads_the_meter_when_the_typed_volume_cannot_be_priced(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The ORES row has T1 only and the typed 17 000 kWh fall in T2: the
    meter's 3 650 kWh price, so setup reads it rather than retry for good."""
    card = fetch.return_value
    ores = card.dsos[DSO_ORES]
    t1_only = replace(ores, tiers={TIER_T1: ores.tiers[TIER_T1]})
    fetch.return_value = replace(card, dsos={**card.dsos, DSO_ORES: t1_only})
    read = AsyncMock(return_value=("energy", _a_year_of_gas()))
    with patch(_READ_METER, read):
        entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.annual_kwh_measured
    assert not entry.runtime_data.meter_reads_pending
    assert read.await_count == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_price_history_waits_for_the_meter_read(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Drawn on the measured volume, not the typed one setup's own tick
    priced on: its stamp would keep that year as drawn."""
    volumes: list[float] = []

    async def backfill(_hass: Any, coordinator: Any) -> None:
        volumes.append(coordinator.household.annual_kwh)

    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", _a_year_of_gas())

    with (
        patch("custom_components.be_gas_prices.backfill_once_a_year", backfill),
        patch(_READ_METER, slow),
    ):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert volumes == []
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert volumes and volumes[0] == pytest.approx(3650.0)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_ignored_meter_card_stays_ignored_through_a_restart(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The registry keeps an ignored card inactive across a restart: setup's
    own tick, which reads no meter, must not delete it for the meter read to
    create it afresh."""
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    issues = ir.async_get(hass)
    key = f"meter_unit_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        key,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="meter_unit",
        translation_placeholders={"entry": "x", "supplier": "y", "error": "z"},
    )
    issues.async_ignore(DOMAIN, key, True)

    async def kind(_hass: Any, _meter: str) -> Any:
        raise RecorderUnavailable("sensor.gas reports in a unit that is no volume")

    with (
        patch(
            "custom_components.be_gas_prices.coordinator.discover_energy_gas_meter",
            AsyncMock(return_value=("sensor.gas", 1)),
        ),
        patch("custom_components.be_gas_prices.coordinator.statistic_kind", kind),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    issue = issues.async_get_issue(DOMAIN, key)
    assert issue is not None and issue.dismissed_version is not None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_daily_ranking_waits_for_the_meter_read(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Neither setup's own tick nor the ranking minute's listener ranks the
    day before the meter is read: the household is priced on its measured
    volume, and a day once ranked is not ranked again."""
    volumes: list[float] = []

    async def ranked(
        _session: Any, _region: str, household: Any, _month: str, **_kwargs: Any
    ) -> Any:
        volumes.append(household.annual_kwh)
        return ([], 0)

    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", _a_year_of_gas())

    with (
        patch("custom_components.be_gas_prices.coordinator.rank", ranked),
        patch("custom_components.be_gas_prices.coordinator.ranking_minute", return_value=0),
        patch(_READ_METER, slow),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_DAILY_COMPARE: True}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        entry.runtime_data.maybe_rank(date(2026, 9, 15))
        await hass.async_block_till_done()
        assert volumes == []
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert volumes == [pytest.approx(3650.0)]


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_forced_fetch_waiting_on_setup_is_not_made_meter_only(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A Repairs fix flow confirmed while setup's own refresh runs waits on
    its lock: its forced fetch asks the supplier, the meter read setup
    starts after it being the one that asks no one."""
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    fix: dict[str, Any] = {}
    real = GasCoordinator._refresh_index

    async def fix_flow(coordinator: GasCoordinator) -> None:
        await coordinator.async_force_refresh(wait=True)
        fix["forced_left"] = coordinator._force_refresh

    async def index(self: GasCoordinator) -> None:
        if "task" not in fix:
            fix["task"] = asyncio.create_task(fix_flow(self))
            await asyncio.sleep(0)
        await real(self)

    with (
        patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))),
        patch.object(GasCoordinator, "_refresh_index", index),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await fix["task"]
        await hass.async_block_till_done(wait_background_tasks=True)
    # The fix flow's own refresh made the forced fetch.
    assert fix["forced_left"] is False


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_meter_read_that_fails_still_names_the_meter_problem(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The held measured volume prices at setup, but the recorder fails the
    meter read after it, which then cannot price the typed volume: the tick
    fails, and the card naming the recorder problem is raised all the
    same."""
    card = fetch.return_value
    ores = card.dsos[DSO_ORES]
    t1_only = replace(ores, tiers={TIER_T1: ores.tiers[TIER_T1]})
    fetch.return_value = replace(card, dsos={**card.dsos, DSO_ORES: t1_only})
    discover = AsyncMock(return_value=("sensor.gas", 1))
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    with (
        patch("custom_components.be_gas_prices.coordinator.discover_energy_gas_meter", discover),
        patch(
            "custom_components.be_gas_prices.coordinator.statistic_kind",
            AsyncMock(return_value="energy"),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator.daily_consumption",
            AsyncMock(return_value=_a_year_of_gas()),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    async def broken(_hass: Any, _meter: str) -> Any:
        raise RecorderUnavailable("recorder query for sensor.gas failed: database locked")

    with (
        patch("custom_components.be_gas_prices.coordinator.discover_energy_gas_meter", discover),
        patch("custom_components.be_gas_prices.coordinator.statistic_kind", broken),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data
    assert not coordinator.last_update_success
    assert not coordinator.meter_reads_pending
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"meter_unit_{entry.entry_id}") is not None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_restart_on_a_named_meter_shows_its_held_figures(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The figures are held with the meter they were read off, which setup's
    own tick resolves before it looks them up."""
    data = {**DATA, CONF_GAS_METER: "sensor.gas"}
    with (
        patch(
            "custom_components.be_gas_prices.coordinator.statistic_kind",
            AsyncMock(return_value="energy"),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator.daily_consumption",
            AsyncMock(return_value=_a_year_of_gas()),
        ),
    ):
        entry = await _setup(hass, data)
    before = entry.runtime_data.data.current_year_cost
    assert before is not None
    gate = asyncio.Event()

    async def slow(*_args: Any) -> Any:
        await gate.wait()
        return _a_year_of_gas()

    with (
        patch(
            "custom_components.be_gas_prices.coordinator.statistic_kind",
            AsyncMock(return_value="energy"),
        ),
        patch("custom_components.be_gas_prices.coordinator.daily_consumption", slow),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.meter_reads_pending
        assert entry.runtime_data.data.current_year_cost == pytest.approx(before)
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_month_cards_landing_after_setup_ask_no_supplier_again(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The past months' cards take a round trip to land: the tick they ask
    for prices again on them, without fetching the failing card a second
    time and counting it twice."""
    card = fetch.return_value
    fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
    gate = asyncio.Event()

    async def slow_archive(*_args: Any, **_kwargs: Any) -> Any:
        await gate.wait()

    with (
        patch(
            "custom_components.be_gas_prices.coordinator.fetch_archived_row",
            AsyncMock(return_value=(card, False)),
        ),
        patch("custom_components.be_gas_prices.month_cards.fetch_archived_card", slow_archive),
    ):
        entry = MockConfigEntry(
            domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_CARD_ARCHIVE: True}
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == 1
    assert entry.runtime_data.failures == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_restart_reads_the_meter_once(hass: HomeAssistant, fetch: AsyncMock) -> None:
    """The past months' cards land from the cache at once and ask for a
    reprice before the meter read setup starts: the first to run reads the
    meter, and the other has nothing left to do."""
    read = AsyncMock(return_value=("energy", _a_year_of_gas()))
    with patch(_READ_METER, read):
        entry = await _setup(hass)
        before = read.await_count
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert read.await_count - before == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_fetch_forced_during_the_startup_reprice_is_made(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The refresh button pressed while the meter is read after setup, with
    August's card landing then: the press and the reprice August asks for
    both wait on the lock, and whichever runs first makes the one fetch."""
    card = fetch.return_value
    august = replace(card, publication_label="2026-08", valid_until=date(2026, 8, 31))
    meter_gate = asyncio.Event()
    card_gate = asyncio.Event()
    reads = 0

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        if month != date(2026, 8, 1):
            return None
        await card_gate.wait()
        return august

    async def slow(_self: Any, _today: date) -> Any:
        nonlocal reads
        reads += 1
        await meter_gate.wait()
        return ("energy", _a_year_of_gas())

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}), patch(_READ_METER, slow):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(20):
            await asyncio.sleep(0)
        coordinator = entry.runtime_data
        assert reads == 1
        before = fetch.await_count
        await coordinator.async_force_refresh()
        card_gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        meter_gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=15))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert reads == 2
    assert fetch.await_count == before + 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_fetch_forced_during_a_long_startup_meter_read_is_made(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The meter read after setup outlasts the cooldown a requested refresh
    would wait out, which Home Assistant drops with the lock held: the
    press waits on the lock itself and is fetched once the read is done."""
    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", _a_year_of_gas())

    with patch(_READ_METER, slow):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        before = fetch.await_count
        await coordinator.async_force_refresh()
        freezer.tick(timedelta(seconds=12))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == before + 1
    assert not coordinator._force_refresh


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_fix_flow_during_the_startup_reprice_fetches_once(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The stale card's Repairs fix confirmed while the meter is read after
    setup, the card failing for good: one fetch, one failure, and no card
    saying the extractor failed."""
    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", _a_year_of_gas())

    with patch(_READ_METER, slow):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        before = fetch.await_count
        fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
        flow = hass.async_create_task(coordinator.async_force_refresh(wait=True))
        for _ in range(10):
            await asyncio.sleep(0)
        gate.set()
        await flow
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=30))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == before + 1
    assert coordinator.failures == 1
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_press_during_a_long_hourly_tick_is_fetched_after_it(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """An hourly tick whose meter read outlasts the cooldown of a requested
    refresh: the press is fetched once that tick is done, not an hour on."""
    gate = asyncio.Event()
    slow_reads = False

    async def read(_self: Any, _today: date) -> Any:
        if slow_reads:
            await gate.wait()
        return ("energy", _a_year_of_gas())

    with patch(_READ_METER, read):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        coordinator = entry.runtime_data
        slow_reads = True
        freezer.tick(timedelta(minutes=61))
        async_fire_time_changed(hass)
        for _ in range(20):
            await asyncio.sleep(0)
        before = fetch.await_count
        await coordinator.async_force_refresh()
        freezer.tick(timedelta(seconds=12))
        async_fire_time_changed(hass)
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=60))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == before + 1
    assert not coordinator._force_refresh


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_forced_fetch_that_failed_is_not_made_again_by_a_reprice(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The refresh button pressed during the startup meter read, the card
    failing: the fetch is made once, and the reprice August's card asks for
    a moment later prices without fetching it again."""
    card = fetch.return_value
    august = replace(card, publication_label="2026-08", valid_until=date(2026, 8, 31))
    meter_gate = asyncio.Event()
    card_gate = asyncio.Event()

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        if month != date(2026, 8, 1):
            return None
        await card_gate.wait()
        return august

    async def slow(_self: Any, _today: date) -> Any:
        await meter_gate.wait()
        return ("energy", _a_year_of_gas())

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}), patch(_READ_METER, slow):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(20):
            await asyncio.sleep(0)
        coordinator = entry.runtime_data
        before = fetch.await_count
        fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
        await coordinator.async_force_refresh()
        meter_gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        assert fetch.await_count == before + 1
        freezer.tick(timedelta(seconds=5))
        card_gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == before + 1
    assert coordinator.failures == 1
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_refresh_service_returns_once_its_fetch_is_done(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """An automation that refreshes, then reads the prices, reads those the
    fetch gave."""
    entry = await _setup(hass)
    card = fetch.return_value
    release = asyncio.Event()

    async def slow(*_args: Any, **_kwargs: Any) -> Any:
        await release.wait()
        return card

    fetch.side_effect = slow
    call = hass.async_create_task(
        hass.services.async_call(DOMAIN, "refresh", {"entry_id": entry.entry_id}, blocking=True)
    )
    for _ in range(50):
        await asyncio.sleep(0)
    assert not call.done()
    release.set()
    await call
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_press_during_an_hourly_probe_fetches_once(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The hourly tick waits on the supplier's probe when the button is
    pressed: it reads the press past the probe and fetches for it, and the
    press's own tick has nothing left to fetch, so a failing card counts
    once."""
    gate = asyncio.Event()
    probing = asyncio.Event()
    slow_probe = False

    async def probe(*_args: Any) -> str:
        if slow_probe:
            probing.set()
            await gate.wait()
        return "etag-1"

    stub = replace(providers.EXTRACTORS["engie"], probe=probe)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry = await _setup(hass)
        coordinator = entry.runtime_data
        before = fetch.await_count
        fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
        slow_probe = True
        freezer.tick(timedelta(minutes=61))
        async_fire_time_changed(hass)
        await asyncio.wait_for(probing.wait(), 5)
        await coordinator.async_force_refresh()
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == before + 1
    assert coordinator.failures == 1
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_reprice_queued_before_a_press_makes_its_fetch(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """August's card lands during the startup meter read, so its reprice
    waits on the lock, then the button is pressed: the reprice, first in
    line, turns full for the fetch the press asked for, and the press's own
    tick then has nothing left to do, so the meter is read twice in all,
    not three times."""
    card = fetch.return_value
    august = replace(card, publication_label="2026-08", valid_until=date(2026, 8, 31))
    meter_gate = asyncio.Event()
    card_gate = asyncio.Event()
    reads = 0

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        if month != date(2026, 8, 1):
            return None
        await card_gate.wait()
        return august

    async def slow(_self: Any, _today: date) -> Any:
        nonlocal reads
        reads += 1
        await meter_gate.wait()
        return ("energy", _a_year_of_gas())

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}), patch(_READ_METER, slow):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(50):
            await asyncio.sleep(0)
        coordinator = entry.runtime_data
        card_gate.set()
        for _ in range(50):
            await asyncio.sleep(0)
        before = fetch.await_count
        await coordinator.async_force_refresh()
        meter_gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=15))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert fetch.await_count == before + 1
    assert reads == 2


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_press_on_a_custom_entry_counts_as_made(hass: HomeAssistant) -> None:
    """The typed card has nothing to fetch: a press is made by the tick it
    asks for, so a reprice after it stays one."""
    data = {
        **DATA,
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_CUSTOM_PRICE: 7.5,
        CONF_CUSTOM_T1_FIXED: 15.0,
        CONF_CUSTOM_T1_PROP: 2.0,
        CONF_CUSTOM_T2_FIXED: 80.0,
        CONF_CUSTOM_T2_PROP: 1.0,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_EXCISE_LOW: 1.09286,
    }
    with patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))):
        entry = await _setup(hass, data)
        coordinator = entry.runtime_data
        await coordinator.async_force_refresh(wait=True)
    assert coordinator._force_made == coordinator._force_asked == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_past_months_cards_landing_after_setup_are_priced(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """August's card takes a round trip to land: the reprice it asks for
    bills August on it rather than on the current card."""
    card = fetch.return_value
    august = replace(card, publication_label="2026-08", valid_until=date(2026, 8, 31))
    gate = asyncio.Event()

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        if month != date(2026, 8, 1):
            return None
        await gate.wait()
        return august

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with (
        patch.dict(providers.EXTRACTORS, {"engie": stub}),
        patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))),
    ):
        entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        for _ in range(20):
            await asyncio.sleep(0)
        assert "2026-08" in entry.runtime_data.data.months_on_current_card
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert "2026-08" not in entry.runtime_data.data.months_on_current_card


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_ignored_meter_card_stays_ignored_through_a_failed_setup(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Setup's own tick fails before it has a card, having read no meter:
    the card the user ignored is left as it is for the retry."""
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    issues = ir.async_get(hass)
    key = f"meter_unit_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        key,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="meter_unit",
        translation_placeholders={"entry": "x", "supplier": "y", "error": "z"},
    )
    issues.async_ignore(DOMAIN, key, True)
    fetch.side_effect = ExtractorError("network error fetching x: timeout")
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue = issues.async_get_issue(DOMAIN, key)
    assert issue is not None and issue.dismissed_version is not None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_ignored_extractor_card_stays_ignored_through_a_restart(
    hass: HomeAssistant, fetch: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    """The store keeps the count of failed fetches: the first one after a
    restart does not start it again at one, deleting the card the user
    ignored for the next to create it afresh. A good fetch still clears it."""
    card = fetch.return_value
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    store = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[store] = {
        "version": 1,
        "key": store,
        "data": {
            "snapshot": snapshot_to_json(card),
            "fetched_at": "2026-09-14T08:00:00+00:00",
            "failures": 2,
        },
    }
    entry.add_to_hass(hass)
    issues = ir.async_get(hass)
    key = f"extractor_failed_{entry.entry_id}"
    ir.async_create_issue(
        hass,
        DOMAIN,
        key,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="extractor_failed",
        translation_placeholders={"entry": "x", "supplier": "y", "error": "z"},
    )
    issues.async_ignore(DOMAIN, key, True)
    fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.failures == 3
    issue = issues.async_get_issue(DOMAIN, key)
    assert issue is not None and issue.dismissed_version is not None
    fetch.side_effect = None
    await coordinator.async_force_refresh(wait=True)
    assert coordinator.failures == 0
    assert issues.async_get_issue(DOMAIN, key) is None
    await coordinator.async_save()
    assert hass_storage[store]["data"]["failures"] == 0


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_unreadable_card_stays_marked_through_a_restart(
    hass: HomeAssistant, fetch: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    """The first check after a restart fails on the network: the card the
    store says is unreadable stays so, rather than its count of failures
    being read as a layout change."""
    card = fetch.return_value
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    store = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[store] = {
        "version": 1,
        "key": store,
        "data": {
            "snapshot": snapshot_to_json(card),
            "fetched_at": "2026-09-14T08:00:00+00:00",
            "failures": 2,
            "unreadable": True,
        },
    }
    entry.add_to_hass(hass)
    fetch.side_effect = ExtractorError("network error fetching x: timeout")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_setup_that_read_the_meter_and_failed_names_the_meter_problem(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The typed volume falls in a tier the card does not price, so setup's
    tick reads the meter after all; its unit cannot be read, the tick fails,
    and the card naming the meter problem is raised."""
    card = fetch.return_value
    ores = card.dsos[DSO_ORES]
    t1_only = replace(ores, tiers={TIER_T1: ores.tiers[TIER_T1]})
    fetch.return_value = replace(card, dsos={**card.dsos, DSO_ORES: t1_only})

    async def bad_unit(_hass: Any, _meter: str) -> Any:
        raise RecorderUnavailable("sensor.gas reports in a unit that is no volume")

    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.be_gas_prices.coordinator.discover_energy_gas_meter",
            AsyncMock(return_value=("sensor.gas", 1)),
        ),
        patch("custom_components.be_gas_prices.coordinator.statistic_kind", bad_unit),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"meter_unit_{entry.entry_id}")
    assert issue is not None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_reprice_with_nothing_to_do_leaves_no_flag(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Its flag is set only once it holds the lock and has work: one left
    set would make the next hourly tick skip its card, index and calorific
    checks."""
    with patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))):
        entry = await _setup(hass)
        coordinator = entry.runtime_data
        await coordinator.async_reprice(lambda: False)
    assert not coordinator._reprice_only


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_restart_does_not_hold_a_typed_volume_as_measured(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A month of meter history prices the year on the typed volume, and the
    figures held for the next restart do not say it was measured."""
    month = {date(2026, 8, 15) + timedelta(days=n): 10.0 for n in range(31)}
    with patch(_READ_METER, AsyncMock(return_value=("energy", month))):
        entry = await _setup(hass)
    assert entry.runtime_data.data.current_year_cost is not None
    assert not entry.runtime_data.data.annual_kwh_measured
    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", month)

    with patch(_READ_METER, slow):
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.meter_reads_pending
        assert not entry.runtime_data.data.annual_kwh_measured
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_failed_meter_read_keeps_the_figures_held_for_a_restart(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The recorder failing an hourly read leaves the costs unknown for that
    tick, but a restart still shows those the last good read gave."""
    data = {**DATA, CONF_GAS_METER: "sensor.gas"}
    kind = AsyncMock(return_value="energy")
    with (
        patch("custom_components.be_gas_prices.coordinator.statistic_kind", kind),
        patch(
            "custom_components.be_gas_prices.coordinator.daily_consumption",
            AsyncMock(return_value=_a_year_of_gas()),
        ),
    ):
        entry = await _setup(hass, data)
        coordinator = entry.runtime_data
        held = coordinator._held
        assert held is not None and held["current_year_cost"] is not None
        kind.side_effect = RecorderUnavailable("recorder query for sensor.gas failed")
        await coordinator.async_refresh()
        assert coordinator.data.current_year_cost is None
    assert coordinator._held == held


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_restart_names_the_earlier_contracts_left_unpriced(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """Held with the figures they are missing from, and shown with them
    until the meter is read again."""
    gone = {
        CONF_SUPPLIER: "dats24",
        CONF_CONTRACT: "dats24_variable",
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        "until": "2026-05-31",
    }
    with patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))):
        entry = await _setup(hass, {**DATA, CONF_PREVIOUS_CONTRACTS: [gone]})
    assert entry.runtime_data.data.unpriced_periods == ("dats24",)
    gate = asyncio.Event()

    async def slow(_self: Any, _today: date) -> Any:
        await gate.wait()
        return ("energy", _a_year_of_gas())

    with patch(_READ_METER, slow):
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.meter_reads_pending
        assert entry.runtime_data.data.unpriced_periods == ("dats24",)
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_held_figures_cover_only_their_month_year_and_settings(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A month or year turned over while Home Assistant was down, or a
    setting edited since, leaves a figure unknown rather than show a period
    it does not cover."""
    with patch(_READ_METER, AsyncMock(return_value=("energy", _a_year_of_gas()))):
        entry = await _setup(hass)
    coordinator = entry.runtime_data
    digest = coordinator._settings_digest()
    same = coordinator._held_for(date(2026, 9, 20), digest)
    assert same["current_month_cost"] is not None and same["current_year_cost"] is not None
    next_month = coordinator._held_for(date(2026, 10, 1), digest)
    assert "current_month_cost" not in next_month
    assert next_month["current_year_cost"] is not None
    next_year = coordinator._held_for(date(2027, 1, 1), digest)
    assert "current_year_cost" not in next_year and "current_month_cost" not in next_year
    assert next_year["annual_kwh"] is not None
    assert coordinator._held_for(date(2026, 9, 20), "edited") == {}
    # The Energy dashboard's gas source changed, which no setting says.
    coordinator.meter = "sensor.other_gas"
    assert coordinator._held_for(date(2026, 9, 20), digest) == {}


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_atrias_failing_at_setup_is_asked_again_next_tick(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """With no value held for the station every running cost waits on
    Atrias: a failed first read is asked again on the next tick, not a day
    later, and a forced refresh asks too."""
    station = "541234"
    months = {"2026-07": "GCV202607.txt", "2026-08": "GCV202608.txt"}
    key = AsyncMock(side_effect=calorific.CalorificError("network error fetching x: timeout"))
    fetch_month = AsyncMock(return_value={calorific.Station(ean=station, name="X"): 11.2})
    data = {**DATA, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: station}
    with (
        patch.object(calorific, "build_ssl_context", lambda: None),
        patch.object(calorific, "subscription_key", key),
        patch.object(calorific, "list_months", AsyncMock(return_value=months)),
        patch.object(calorific, "fetch_month", fetch_month),
    ):
        entry = await _setup(hass, data)
        coordinator = entry.runtime_data
        assert coordinator._m3_factor("2026-08") == (None, None)
        key.side_effect = None
        key.return_value = "k"
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
        assert coordinator._m3_factor("2026-08") == (pytest.approx(11.2), "2026-08")
        assert key.await_count == 2
        await coordinator.async_force_refresh(wait=True)
        assert key.await_count == 3


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_atrias_files_failing_with_nothing_held_are_asked_again_next_tick(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The list answers but every file fails: still nothing for the station,
    so the next tick asks again rather than the next day."""
    station = "541234"
    months = {"2026-07": "GCV202607.txt", "2026-08": "GCV202608.txt"}
    fetch_month = AsyncMock(side_effect=calorific.CalorificError("network error fetching x"))
    data = {**DATA, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: station}
    with (
        patch.object(calorific, "build_ssl_context", lambda: None),
        patch.object(calorific, "subscription_key", AsyncMock(return_value="k")),
        patch.object(calorific, "list_months", AsyncMock(return_value=months)),
        patch.object(calorific, "fetch_month", fetch_month),
    ):
        entry = await _setup(hass, data)
        coordinator = entry.runtime_data
        assert coordinator._m3_factor("2026-08") == (None, None)
        fetch_month.side_effect = None
        fetch_month.return_value = {calorific.Station(ean=station, name="X"): 11.2}
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
    assert coordinator._m3_factor("2026-08") == (pytest.approx(11.2), "2026-08")


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_atrias_files_the_parser_refuses_are_read_once_a_day(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A layout the parser refuses fails the same way next hour: the files
    are not all downloaded again every tick."""
    data = {**DATA, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: "541234"}
    months = {"2026-07": "GCV202607.txt", "2026-08": "GCV202608.txt"}
    fetch_month = AsyncMock(
        side_effect=calorific.CalorificError("calorific value file has no header")
    )
    with (
        patch.object(calorific, "build_ssl_context", lambda: None),
        patch.object(calorific, "subscription_key", AsyncMock(return_value="k")),
        patch.object(calorific, "list_months", AsyncMock(return_value=months)),
        patch.object(calorific, "fetch_month", fetch_month),
    ):
        entry = await _setup(hass, data)
        for _ in range(3):
            freezer.tick(timedelta(hours=1))
            await entry.runtime_data.async_refresh()
    assert fetch_month.await_count == 2


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
@pytest.mark.parametrize("first", ["2025-02", "2025-11"])
async def test_a_month_before_the_rolling_year_without_a_factor_keeps_the_costs(
    hass: HomeAssistant, fetch: AsyncMock, first: str
) -> None:
    """Atrias lists the station only since a month after the meter's first
    day read: January 2025, a month no cost of September 2026 reads, or
    November 2025, a new station, whose earlier days only the rolling year
    reads. This year's costs are priced all the same."""
    station = "541234"
    months = {f"{y}-{m:02d}": f"GCV{y}{m:02d}.txt" for y in (2025, 2026) for m in range(1, 13)}
    months = {k: v for k, v in months.items() if first <= k <= "2026-08"}

    async def fetch_month(_session: Any, _context: Any, _key: Any, path: str) -> Any:
        return {calorific.Station(ean=station, name="X"): 11.0}

    async def consumption(_hass: Any, _meter: str, _kind: str, start: date, end: date) -> Any:
        return {start + timedelta(days=n): 1.0 for n in range((end - start).days + 1)}

    data = {**DATA, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: station}
    with (
        patch.object(calorific, "build_ssl_context", lambda: None),
        patch.object(calorific, "subscription_key", AsyncMock(return_value="k")),
        patch.object(calorific, "list_months", AsyncMock(return_value=months)),
        patch.object(calorific, "fetch_month", fetch_month),
        patch(
            "custom_components.be_gas_prices.coordinator.GasCoordinator._meter",
            AsyncMock(return_value="sensor.gas"),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator.statistic_kind",
            AsyncMock(return_value="volume"),
        ),
        patch("custom_components.be_gas_prices.coordinator.daily_consumption", consumption),
    ):
        await _setup(hass, data)
    cost = hass.states.get("sensor.engie_flow_current_year_cost")
    assert cost is not None and cost.state not in ("unknown", "unavailable")
    assert cost.attributes["ytd_kwh"] == pytest.approx(258 * 11.0)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
@pytest.mark.parametrize(
    ("same_release", "served", "price"),
    [(True, True, 1.0), (False, True, 0.07643), (False, False, 1.0)],
)
async def test_month_cards_stored_by_another_release_are_read_again(
    hass: HomeAssistant,
    fetch: AsyncMock,
    hass_storage: dict[str, Any],
    same_release: bool,
    served: bool,
    price: float | None,
) -> None:
    """A release may read a card better than the one that stored it: the
    past months' cards another release stored are fetched afresh, and kept
    when no card of the month can be found."""
    card = fetch.return_value
    august = replace(card, publication_label="2026-08", valid_until=date(2026, 8, 31))
    misread = replace(august, energy=replace(august.energy, price=1.0))
    release = (await async_get_integration(hass, DOMAIN)).version
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "release": release if same_release else "0.0.1",
            "months": {
                "engie/engie_flow/wallonia/2026-08": {
                    "snapshot": snapshot_to_json(misread),
                    "source": "supplier",
                    "fetched_at": "2026-09-01T00:00:00+00:00",
                }
            },
        },
    }

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        return august if served and month == date(2026, 8, 1) else None

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    held = entry.runtime_data._month_card("2026-08")
    assert (None if held is None else held.energy.price) == (
        None if price is None else pytest.approx(price)
    )


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_custom_entry_names_no_month_billed_on_another_card(
    hass: HomeAssistant,
) -> None:
    """The typed card is every month's own: none is billed on a stand-in."""
    data = {
        **DATA,
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_CUSTOM_PRICE: 7.5,
        CONF_CUSTOM_T1_FIXED: 15.0,
        CONF_CUSTOM_T1_PROP: 2.0,
        CONF_CUSTOM_T2_FIXED: 80.0,
        CONF_CUSTOM_T2_PROP: 1.0,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_EXCISE_LOW: 1.09286,
    }
    days = {date(2026, 1, 1) + timedelta(days=n): 10.0 for n in range(258)}
    with patch(
        "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter",
        AsyncMock(return_value=("energy", days)),
    ):
        await _setup(hass, data)
    cost = hass.states.get("sensor.engie_flow_current_year_cost")
    assert cost is not None and cost.state != "unknown"
    assert cost.attributes["months_on_current_card"] == []


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_price_history_stamp_moves_with_the_settings(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A change of DSO prices every past month otherwise, so the history is
    worth drawing again."""
    entry = await _setup(hass)
    before = entry.runtime_data.card_months_signature()
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DSO: DSO_RESA})
    await hass.async_block_till_done()
    assert entry.runtime_data.card_months_signature() != before


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_price_history_stamp_moves_with_a_card_read_again(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """A release that reads March's card better changes March's prices, not
    which months have a card: the history is worth drawing again."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    card = fetch.return_value
    rows = coordinator._months._rows
    key = "engie/engie_flow/wallonia/2026-03"
    rows[key] = MonthCard(card, "supplier", dt_util.utcnow())
    before = coordinator.card_months_signature()
    read_again = replace(card, energy=replace(card.energy, price=card.energy.price * 2))
    rows[key] = MonthCard(read_again, "supplier", dt_util.utcnow())
    assert coordinator.card_months_signature() != before


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_price_history_falls_back_like_the_running_costs(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """August's own card lists no ORES row: the running costs bill August on
    today's card, and the price history draws it the same way rather than
    leaving the month out."""
    card = fetch.return_value
    august = replace(
        card,
        dsos={k: v for k, v in card.dsos.items() if k != DSO_ORES},
        publication_label="2026-08",
        valid_until=date(2026, 8, 31),
    )

    async def for_month(_session: Any, _contract: str, _region: str, month: date) -> Any:
        return august if month == date(2026, 8, 1) else None

    stub = replace(providers.EXTRACTORS["engie"], fetch_for_month=for_month)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry = await _setup(hass)
    coordinator = entry.runtime_data
    assert coordinator._month_card("2026-08") is august
    priced, today = coordinator.month_price("2026-08"), coordinator.month_price("2026-09")
    assert priced is not None and today is not None
    assert priced[0] == today[0]
    assert coordinator.month_price("2026-08", own_card=True) is None


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_optional_sensor_goes_with_its_option(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    with patch("custom_components.be_gas_prices.coordinator.rank", AsyncMock(return_value=([], 0))):
        entry = await _setup(
            hass, {**DATA, CONF_CONTRACT_END_DATE: "2027-05-31", CONF_DAILY_COMPARE: True}
        )
        registry = er.async_get(hass)
        ids = ("sensor.engie_flow_contract_end_date", "sensor.engie_flow_potential_saving")
        assert all(registry.async_get(entity_id) is not None for entity_id in ids)
        data = {k: v for k, v in entry.data.items() if k != CONF_CONTRACT_END_DATE}
        hass.config_entries.async_update_entry(entry, data={**data, CONF_DAILY_COMPARE: False})
        await hass.async_block_till_done()
    assert all(registry.async_get(entity_id) is None for entity_id in ids)
    assert all(hass.states.get(entity_id) is None for entity_id in ids)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_stored_card_of_another_region_is_not_restored(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The household moves from Wallonia to Flanders while the supplier is
    down: the Walloon card in the store cannot price a Fluvius DSO, so the
    card archive is asked instead."""
    walloon = fetch.return_value
    flemish = replace(walloon, dsos={DSO_FLUVIUS_IMEWO: walloon.dsos[DSO_ORES]})
    archived = AsyncMock(return_value=(flemish, False))
    with patch("custom_components.be_gas_prices.coordinator.fetch_archived_row", archived):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
        await entry.runtime_data.async_save()
        archived.reset_mock()
        fetch.side_effect = ExtractorError("network error fetching engie: timeout")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REGION: REGION_FLANDERS, CONF_DSO: DSO_FLUVIUS_IMEWO}
        )
        await hass.async_block_till_done()
    assert archived.await_count >= 1
    assert entry.runtime_data.data.card_source == "archive"


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
@pytest.mark.parametrize(("stored_for", "restored"), [("engie", True), ("energiebe", False)])
async def test_a_stored_index_table_is_its_own_supplier_s_only(
    hass: HomeAssistant,
    fetch: AsyncMock,
    hass_storage: dict[str, Any],
    stored_for: str,
    restored: bool,
) -> None:
    """With the index fetch down, a table restored from the store prices the
    entry only when its own supplier published it."""
    entry = MockConfigEntry(domain=DOMAIN, title="Engie Flow", data=DATA)
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {"index": {"ZTPDAM": {"2026-09": 99.0}}, "index_supplier": stored_for},
    }
    down = replace(
        providers.EXTRACTORS["engie"],
        fetch_index=AsyncMock(side_effect=ExtractorError("HTTP 500 fetching x")),
    )
    with patch.dict(providers.EXTRACTORS, {"engie": down}):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    index = entry.runtime_data.data.index
    assert (index is not None and index.value == 99.0) is restored


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_failed_index_fetch_with_no_table_held_is_asked_again_next_tick(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A new entry whose first index read fails prices every indexed month
    at the card's printed figure: it asks again on the next tick, not twelve
    hours later."""
    index = AsyncMock(side_effect=[ExtractorError("network error fetching x: timeout"), TABLE])
    stub = replace(providers.EXTRACTORS["engie"], fetch_index=index)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry = await _setup(hass)
        coordinator = entry.runtime_data
        assert coordinator.data.index is None
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
        assert coordinator.data.index is not None
        freezer.tick(timedelta(hours=1))
        await coordinator.async_refresh()
    assert index.await_count == 2


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_an_index_page_the_parser_refuses_is_not_read_every_hour(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """A changed layout fails the same way each time: with no table held it
    still waits the usual twelve hours, rather than reading every document
    of the source each hour."""
    index = AsyncMock(side_effect=ExtractorError("Engie: index table not found"))
    stub = replace(providers.EXTRACTORS["engie"], fetch_index=index)
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        entry = await _setup(hass)
        for _ in range(3):
            freezer.tick(timedelta(hours=1))
            await entry.runtime_data.async_refresh()
    assert index.await_count == 1


async def test_a_custom_entry_s_saving_is_against_its_typed_card(hass: HomeAssistant) -> None:
    """The custom supplier is not among the suppliers ranked: its own card
    is handed to the ranking and quoted beside them, so the saving is known."""
    cheaper = Quote("engie", "engie_flow", "Engie Flow", 100.0, 0.08, 190.0, True, False)

    async def ranked(
        session: Any, region: str, household: Any, month: str, **kwargs: Any
    ) -> tuple[list[Quote], int]:
        held = kwargs["own"]
        assert held is not None and held.contract == CUSTOM_CONTRACT
        own = await quote_contract(
            session,
            held.extractor,
            held.contract,
            region,
            household,
            month,
            IndexCache(),
            use_archive=False,
            card=held.card,
        )
        return [cheaper, own], 0

    data = {
        **DATA,
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_DAILY_COMPARE: True,
        CONF_CUSTOM_PRICE: 7.5,
        CONF_CUSTOM_T1_FIXED: 15.0,
        CONF_CUSTOM_T1_PROP: 2.0,
        CONF_CUSTOM_T2_FIXED: 80.0,
        CONF_CUSTOM_T2_PROP: 1.0,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_EXCISE_LOW: 1.09286,
    }
    with (
        patch("custom_components.be_gas_prices.coordinator.rank", ranked),
        patch("custom_components.be_gas_prices.coordinator.ranking_minute", return_value=0),
    ):
        entry = await _setup(hass, data)
    ranking = entry.runtime_data.daily_ranking
    assert ranking is not None and ranking.own_cost is not None
    assert ranking.saving == pytest.approx(ranking.own_cost - 100.0)


@pytest.mark.freeze_time("2026-01-01 10:30:00+01:00")
async def test_a_custom_entry_prices_last_year_on_its_typed_card(hass: HomeAssistant) -> None:
    """A backfill reaching before the window prices a custom entry's months
    on its typed card, the only card it has, and fetches nothing for them."""
    data = {
        **DATA,
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_CUSTOM_PRICE: 7.5,
        CONF_CUSTOM_T1_FIXED: 15.0,
        CONF_CUSTOM_T1_PROP: 2.0,
        CONF_CUSTOM_T2_FIXED: 80.0,
        CONF_CUSTOM_T2_PROP: 1.0,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_EXCISE_LOW: 1.09286,
    }
    entry = await _setup(hass, data)
    coordinator = entry.runtime_data
    archived = AsyncMock(return_value=None)
    with patch("custom_components.be_gas_prices.month_cards.fetch_archived_card", archived):
        await coordinator.async_fill_month_cards(["2025-12"])
    archived.assert_not_called()
    december = coordinator.month_price("2025-12", own_card=True)
    assert december is not None
    assert december[0].energy == pytest.approx(0.075)


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_custom_entry_ignores_signing_figures_left_from_an_earlier_flow(
    hass: HomeAssistant,
) -> None:
    """The flow once asked a custom entry with a start date for its signing
    price too, and that price was laid over the typed card."""
    data = {
        **DATA,
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_CONTRACT_START_DATE: "2026-03-01",
        CONF_MANUAL_PRICE: 8.0,
        CONF_CUSTOM_PRICE: 7.5,
        CONF_CUSTOM_T1_FIXED: 15.0,
        CONF_CUSTOM_T1_PROP: 2.0,
        CONF_CUSTOM_T2_FIXED: 80.0,
        CONF_CUSTOM_T2_PROP: 1.0,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_EXCISE_LOW: 1.09286,
    }
    await _setup(hass, data)
    energy = hass.states.get("sensor.engie_flow_current_price")
    assert energy is not None
    assert energy.attributes["card_source"] == "live"
    component = hass.states.get("sensor.engie_flow_energy_component")
    assert component is not None and float(component.state) == pytest.approx(0.075)


@pytest.mark.freeze_time("2026-09-02 10:00:00+02:00")
async def test_the_stale_card_repair_stays_until_a_fetch_works(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    card = fetch.return_value
    entry = await _setup(hass)
    fetch.side_effect = ExtractorError("HTTP 500 fetching x")
    for _ in range(8 * 24):
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    issue_id = f"snapshot_stale_{entry.entry_id}"
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    assert await async_setup_component(hass, "repairs", {})
    flows = repairs_flow_manager(hass)
    assert flows is not None
    # The fetch still fails: the flow aborts and the card stays.
    result = await flows.async_init(DOMAIN, data={"issue_id": issue_id})
    result = await flows.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "still_stale"
    assert issues.async_get_issue(DOMAIN, issue_id) is not None
    # It works again: the flow ends and the card goes.
    fetch.side_effect = None
    fetch.return_value = card
    result = await flows.async_init(DOMAIN, data={"issue_id": issue_id})
    result = await flows.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert issues.async_get_issue(DOMAIN, issue_id) is None


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
async def test_the_archive_reading_of_a_card_is_kept_like_a_card_read_here(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The card archive's reading of the month's card, OCR or not, is due
    when it ages, not on every tick."""
    card = fetch.return_value
    fetch.side_effect = CardNotReadableError("card has no text layer")
    row = AsyncMock(return_value=(card, False))
    with patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
        assert entry.runtime_data.data.card_source == "archive"
        for _ in range(3):
            freezer.tick(timedelta(hours=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    assert fetch.await_count == 1


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_a_stand_in_from_the_archive_is_asked_for_again_at_once(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    card = fetch.return_value
    fetch.side_effect = ExtractorError("Engie: variable price block or formula not found")
    with patch(
        "custom_components.be_gas_prices.coordinator.fetch_archived_row",
        AsyncMock(return_value=(card, False)),
    ):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
        assert entry.runtime_data.data.card_source == "archive"
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert fetch.await_count == 2


@pytest.mark.freeze_time("2026-09-15 10:00:00+02:00")
async def test_the_own_contract_read_off_an_image_is_quoted_so(
    hass: HomeAssistant, fetch: AsyncMock
) -> None:
    """The comparisons mark the own row OCR like any row read that way."""
    card = fetch.return_value
    fetch.side_effect = CardNotReadableError("card has no text layer")
    row = AsyncMock(return_value=(card, True))
    with patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
    own = entry.runtime_data.own_contract()
    assert own is not None and own.read_by_ocr
    quote = await quote_contract(
        AsyncMock(),
        own.extractor,
        own.contract,
        REGION_WALLONIA,
        Household(dso=DSO_ORES, caliber="q10", annual_kwh=17000.0),
        "2026-09",
        IndexCache(),
        use_archive=False,
        card=own.card,
        read_by_ocr=own.read_by_ocr,
    )
    assert quote.read_by_ocr


@pytest.mark.freeze_time("2026-09-30 10:00:00+02:00")
async def test_waiting_for_the_archive_to_read_a_new_month_is_no_unreadable_card(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """September's card was read off its image by the card archive. From 1
    October it is asked for again every tick, and until the archive reads
    October's, the entry keeps September's reading and its Repairs card."""
    card = fetch.return_value
    fetch.side_effect = CardNotReadableError("card has no text layer")

    async def row(_session: Any, _supplier: str, _contract: str, _region: str, month: str) -> Any:
        return (card, True) if month == "2026-09" else None

    stub = replace(providers.EXTRACTORS["engie"], probe=AsyncMock(return_value="Mon, 31 Aug"))
    with (
        patch.dict(providers.EXTRACTORS, {"engie": stub}),
        patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row),
        patch(
            "custom_components.be_gas_prices.coordinator.fetch_archived_row",
            AsyncMock(return_value=None),
        ),
    ):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
        coordinator = entry.runtime_data
        assert coordinator.card_read_by_ocr
        freezer.move_to("2026-10-01 06:30:00+02:00")
        for _ in range(4):
            freezer.tick(timedelta(hours=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    assert coordinator.card_read_by_ocr and not coordinator.card_unreadable
    assert coordinator.failures == 0
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_read_by_ocr_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-09-15 20:00:00+02:00")
async def test_an_image_card_reissued_in_the_month_is_read_again_by_the_next_day(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """The supplier replaces its card under a new key; the archive reads the
    new one the next morning, which the entry then takes within a day."""
    old = fetch.return_value
    new = replace(old, publication_label="reissued")
    fetch.side_effect = CardNotReadableError("card has no text layer")
    state = {"row": old, "key": "Mon, 31 Aug"}

    async def row(_session: Any, _supplier: str, _contract: str, _region: str, month: str) -> Any:
        return (state["row"], True) if month == "2026-09" else None

    async def probe(_session: Any, _contract: str, _region: str) -> str:
        return state["key"]

    stub = replace(providers.EXTRACTORS["engie"], probe=probe)
    with (
        patch.dict(providers.EXTRACTORS, {"engie": stub}),
        patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row),
    ):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
        coordinator = entry.runtime_data
        state["key"] = "Tue, 15 Sep"
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        state["row"] = new
        for _ in range(24):
            freezer.tick(timedelta(hours=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    assert coordinator.data.snapshot.publication_label == "reissued"
    assert coordinator.card_read_by_ocr


@pytest.mark.freeze_time("2026-10-01 08:00:00+02:00")
async def test_a_new_entry_on_last_month_s_ocr_reading_waits_for_the_archive(
    hass: HomeAssistant, fetch: AsyncMock, freezer: FrozenDateTimeFactory
) -> None:
    """Set up on 1 October, before the archive read October's card off its
    image: September's reading stands in, and is waited on like one."""
    card = fetch.return_value
    fetch.side_effect = CardNotReadableError("card has no text layer")

    async def row(_session: Any, _supplier: str, _contract: str, _region: str, month: str) -> Any:
        return (card, True) if month == "2026-09" else None

    with (
        patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row),
        patch("custom_components.be_gas_prices.coordinator.fetch_archived_row", row),
    ):
        entry = await _setup(hass, {**DATA, CONF_CARD_ARCHIVE: True})
        coordinator = entry.runtime_data
        for _ in range(3):
            freezer.tick(timedelta(hours=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
    assert coordinator.data.card_source == "archive"
    assert coordinator.card_read_by_ocr and not coordinator.card_unreadable
    assert coordinator.failures == 0
    assert not coordinator.snapshot_stale()
    own = coordinator.own_contract()
    assert own is not None and own.read_by_ocr
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_read_by_ocr_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is None
    assert issues.async_get_issue(DOMAIN, f"snapshot_stale_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-10-01 08:00:00+02:00")
async def test_the_wait_for_the_archive_survives_a_restart(
    hass: HomeAssistant, fetch: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    """Restarted on 1 October with September's reading in the store and
    October's card not read by the archive yet: still a wait."""
    card = fetch.return_value
    fetch.side_effect = CardNotReadableError("card has no text layer")
    entry = MockConfigEntry(
        domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_CARD_ARCHIVE: True}
    )
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "snapshot": snapshot_to_json(card),
            "fetched_at": "2026-09-30T06:00:00+00:00",
            "read_by_ocr": True,
        },
    }
    with (
        patch(
            "custom_components.be_gas_prices.month_cards.fetch_archived_row",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator.fetch_archived_row",
            AsyncMock(return_value=None),
        ),
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = entry.runtime_data
    assert coordinator.card_read_by_ocr and not coordinator.card_unreadable
    assert coordinator.failures == 0
    own = coordinator.own_contract()
    assert own is not None and own.read_by_ocr
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is None


@pytest.mark.freeze_time("2026-10-01 08:00:00+02:00")
async def test_a_restored_reading_stays_marked_through_a_network_failure(
    hass: HomeAssistant, fetch: AsyncMock, hass_storage: dict[str, Any]
) -> None:
    """The first check after a restart fails on the network: the store
    still says the card in hand is the archive's reading."""
    card = fetch.return_value
    fetch.side_effect = ExtractorError("network error fetching x: timeout")
    entry = MockConfigEntry(
        domain=DOMAIN, title="Engie Flow", data={**DATA, CONF_CARD_ARCHIVE: True}
    )
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "snapshot": snapshot_to_json(card),
            "fetched_at": "2026-09-30T06:00:00+00:00",
            "read_by_ocr": True,
        },
    }
    with patch(
        "custom_components.be_gas_prices.coordinator.fetch_archived_row",
        AsyncMock(return_value=None),
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    await entry.runtime_data.async_save()
    assert hass_storage[key]["data"]["read_by_ocr"] is True


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
            "custom_components.be_gas_prices.coordinator.fetch_archived_row",
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
    # Removed while retrying, it is never unloaded: its cards go with it.
    await hass.config_entries.async_remove(entry.entry_id)
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is None
