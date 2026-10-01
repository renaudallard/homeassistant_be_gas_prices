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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component
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
)
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
                CONF_CONVERSION_MODE: CONVERSION_MANUAL,
                CONF_CARD_ARCHIVE: False,
                CONF_DAILY_COMPARE: False,
            },
            {CONF_CONVERSION_FACTOR: 11.5},
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
    own = coordinator.own_contract()
    assert own is not None and own.read_by_ocr
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"card_read_by_ocr_{entry.entry_id}") is not None
    assert issues.async_get_issue(DOMAIN, f"card_unreadable_{entry.entry_id}") is None


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
