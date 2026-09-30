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

"""The setup wizard and the options flow, step by step."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_gas_prices import calorific, postcodes
from custom_components.be_gas_prices.compare import Quote
from custom_components.be_gas_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CALIBER,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_DAILY_COMPARE,
    CONF_DSO,
    CONF_POSTCODE,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_STATION,
    CONF_SUPPLIER,
    CONF_SWITCH_DATE,
    CONVERSION_MANUAL,
    CONVERSION_STATION,
    DOMAIN,
    DSO_ORES,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_WALLONIA,
)

STATIONS = [
    calorific.Station(ean="541460900000000030", name="RESA LIEGE (GOS)"),
    calorific.Station(ean="541454827900000382", name="IDEG NAMUR (GOS)"),
]


@pytest.fixture(autouse=True)
def _no_setup() -> Iterator[None]:
    """The flow is what is tested; the entry it creates is not set up."""
    with patch("custom_components.be_gas_prices.async_setup_entry", AsyncMock(return_value=True)):
        yield


def _walloon_ores_postcode() -> str:
    """A postcode the generated table maps to ORES alone."""
    for code in sorted(postcodes.POSTCODES):
        match = postcodes.resolve(code)
        if match is not None and match.region == REGION_WALLONIA and match.dsos == (DSO_ORES,):
            return code
    raise AssertionError("no ORES postcode in the table")


async def _household(hass: HomeAssistant, flow_id: str, mode: str) -> ConfigFlowResult:
    return await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_ANNUAL_CONSUMPTION_KWH: 12000,
            CONF_CONVERSION_MODE: mode,
            CONF_CARD_ARCHIVE: False,
            CONF_DAILY_COMPARE: False,
        },
    )


async def test_postcode_resolves_the_region_and_the_dso(hass: HomeAssistant) -> None:
    code = _walloon_ores_postcode()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["step_id"] == "postcode"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTCODE: code}
    )
    assert result["step_id"] == "supplier"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    assert result["step_id"] == "contract"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTRACT: "engie_flow"}
    )
    assert result["step_id"] == "dso"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DSO: DSO_ORES})
    assert result["step_id"] == "household"
    # The bill's factor carries the DSO's pressure and temperature correction
    # the station value leaves out, so it is the one offered first.
    schema = result["data_schema"]
    assert schema is not None
    mode = next(key for key in schema.schema if key == CONF_CONVERSION_MODE)
    assert mode.default() == CONVERSION_MANUAL
    assert schema.schema[mode].config["options"][0] == CONVERSION_MANUAL
    with patch(
        "custom_components.be_gas_prices.config_flow._stations", AsyncMock(return_value=STATIONS)
    ):
        result = await _household(hass, result["flow_id"], CONVERSION_STATION)
        assert result["step_id"] == "station"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STATION: STATIONS[1].ean}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Engie Flow"
    data = result["data"]
    assert data[CONF_REGION] == REGION_WALLONIA
    assert data[CONF_DSO] == DSO_ORES
    assert data[CONF_POSTCODE] == code
    assert data[CONF_STATION] == STATIONS[1].ean
    assert CONF_CALIBER not in data


async def test_blank_postcode_asks_the_region(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_POSTCODE: ""})
    assert result["step_id"] == "region"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: REGION_BRUSSELS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CONTRACT: "engie_easy_variable", CONF_CONTRACT_START_DATE: "2026-03-01"},
    )
    # A start date offers the figures signed at; left empty, the card gives them.
    assert result["step_id"] == "signed_rate"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "dso"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DSO: DSO_SIBELGA}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ANNUAL_CONSUMPTION_KWH: 17000,
            CONF_CALIBER: "q16",
            CONF_CONVERSION_MODE: CONVERSION_MANUAL,
            CONF_CARD_ARCHIVE: True,
            CONF_DAILY_COMPARE: False,
        },
    )
    assert result["step_id"] == "factor"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONVERSION_FACTOR: 11.4}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CALIBER] == "q16"
    assert result["data"][CONF_CONVERSION_FACTOR] == 11.4
    assert result["data"][CONF_CONTRACT_START_DATE] == "2026-03-01"


async def test_a_postcode_partly_on_an_unpriced_network_is_warned_about(
    hass: HomeAssistant,
) -> None:
    """Baarle-Hertog: only Zondereigen is on Fluvius Kempen, the rest on the
    Dutch network of Enexis."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTCODE: "2387"}
    )
    assert result["step_id"] == "unpriced_network"
    assert result["description_placeholders"] == {
        "postcode": "2387",
        "operators": "Enexis",
        "dsos": "Fluvius Kempen",
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "supplier"


async def test_an_unknown_postcode_is_refused(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTCODE: "0042"}
    )
    assert result["step_id"] == "postcode"
    assert result["errors"] == {CONF_POSTCODE: "unknown_postcode"}


def _postcode_without_gas() -> str:
    for code in sorted(postcodes.POSTCODES):
        match = postcodes.resolve(code)
        if match is not None and not match.dsos:
            return code
    raise AssertionError("every postcode has a gas network")


async def test_a_postcode_without_gas_can_be_cleared_for_the_region(hass: HomeAssistant) -> None:
    """The error says to clear the postcode, and a cleared field is sent as
    nothing at all: it must lead to the region, not back to the error."""
    code = _postcode_without_gas()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_POSTCODE: code}
    )
    assert result["errors"] == {CONF_POSTCODE: "no_gas_network"}
    schema = result["data_schema"]
    assert schema is not None
    [field] = schema.schema
    assert field.description == {"suggested_value": code}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "region"


async def test_options_clearing_the_postcode_leads_to_the_region(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_POSTCODE: _walloon_ores_postcode()}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "postcode"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "region"


async def test_atrias_down_falls_back_to_the_factor(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_POSTCODE: ""})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: REGION_WALLONIA}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTRACT: "engie_flow"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DSO: DSO_ORES})
    with patch(
        "custom_components.be_gas_prices.config_flow._stations",
        AsyncMock(side_effect=calorific.CalorificError("HTTP 503")),
    ):
        result = await _household(hass, result["flow_id"], CONVERSION_STATION)
    assert result["step_id"] == "factor"
    assert result["errors"] == {"base": "stations_unavailable"}


async def test_an_empty_factor_picks_the_station_instead(hass: HomeAssistant) -> None:
    """The factor is the default, and a household with no bill at hand has
    no way back to the household step: leaving it empty lists the stations."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_POSTCODE: ""})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: REGION_WALLONIA}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTRACT: "engie_flow"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DSO: DSO_ORES})
    result = await _household(hass, result["flow_id"], CONVERSION_MANUAL)
    assert result["step_id"] == "factor"
    with patch(
        "custom_components.be_gas_prices.config_flow._stations", AsyncMock(return_value=STATIONS)
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "station"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STATION: STATIONS[0].ean}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_CONVERSION_MODE] == CONVERSION_STATION
    assert data[CONF_STATION] == STATIONS[0].ean
    assert CONF_CONVERSION_FACTOR not in data


def _entry(hass: HomeAssistant) -> MockConfigEntry:
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
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_compare_quotes_both_contracts(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    own = Quote("engie", "engie_flow", "Engie Flow", 1500.0, 0.08, 190.0, True, True)
    other = Quote(
        "engie", "engie_easy_fixed", "Engie Easy Fixe", 1450.0, 0.077, 200.0, False, False
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    assert result["step_id"] == "compare_contract"
    with patch(
        "custom_components.be_gas_prices.config_flow.quote_contract",
        AsyncMock(side_effect=[other, own]),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CONTRACT: "engie_easy_fixed"}
        )
    assert result["step_id"] == "compare_result"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    table = placeholders["table"]
    assert "**Engie Flow †**" in table and "-50.00" in table
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "compare_done"
    assert entry.data[CONF_CONTRACT] == "engie_flow"


@pytest.mark.freeze_time("2026-09-15 12:00:00+02:00")
async def test_options_switch_keeps_the_earlier_contract(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "switch"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SWITCH_DATE: "2026-10-01"}
    )
    assert result["errors"] == {CONF_SWITCH_DATE: "switch_date_invalid"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SWITCH_DATE: "2026-06-01"}
    )
    assert result["step_id"] == "supplier"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    # The form suggests the switch date as the contract start, and the new
    # contract's signing figures are asked for that date.
    schema = result["data_schema"]
    assert schema is not None
    start = next(key for key in schema.schema if key == CONF_CONTRACT_START_DATE)
    assert start.description == {"suggested_value": "2026-06-01"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CONTRACT: "engie_easy_fixed", CONF_CONTRACT_START_DATE: "2026-06-01"},
    )
    assert result["step_id"] == "signed_rate"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DSO: DSO_ORES}
    )
    result = await _household_options(hass, result["flow_id"])
    # Suggested rather than a default, so clearing it reaches the flow empty.
    schema = result["data_schema"]
    assert schema is not None
    factor = next(key for key in schema.schema if key == CONF_CONVERSION_FACTOR)
    assert factor.description == {"suggested_value": 11.5}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONVERSION_FACTOR: 11.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [previous] = entry.data[CONF_PREVIOUS_CONTRACTS]
    assert previous[CONF_CONTRACT] == "engie_flow"
    assert previous["until"] == "2026-05-31"
    assert entry.data[CONF_CONTRACT] == "engie_easy_fixed"
    assert entry.data[CONF_CONTRACT_START_DATE] == "2026-06-01"
    assert entry.title == "Engie Easy Fixe"


async def _household_options(hass: HomeAssistant, flow_id: str) -> ConfigFlowResult:
    return await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_ANNUAL_CONSUMPTION_KWH: 17000,
            CONF_CONVERSION_MODE: CONVERSION_MANUAL,
            CONF_CARD_ARCHIVE: False,
            CONF_DAILY_COMPARE: False,
        },
    )
