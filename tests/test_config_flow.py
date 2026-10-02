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

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_gas_prices import calorific, config_flow, postcodes
from custom_components.be_gas_prices.compare import Quote
from custom_components.be_gas_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CALIBER,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_CUSTOM_FEE,
    CONF_CUSTOM_PRICE,
    CONF_CUSTOM_T1_FIXED,
    CONF_CUSTOM_T1_PROP,
    CONF_CUSTOM_T2_FIXED,
    CONF_CUSTOM_T2_PROP,
    CONF_CUSTOM_TRANSPORT,
    CONF_DAILY_COMPARE,
    CONF_DSO,
    CONF_GAS_METER,
    CONF_MANUAL_FEE,
    CONF_POSTCODE,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_STATION,
    CONF_SUPPLIER,
    CONF_SWITCH_DATE,
    CONF_TARIFF_CARD_DATE,
    CONF_YTD_FROM_CONTRACT_START,
    CONVERSION_MANUAL,
    CONVERSION_STATION,
    CUSTOM_CONTRACT,
    DOMAIN,
    DSO_ORES,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_WALLONIA,
    SUPPLIER_CUSTOM,
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


async def _household(hass: HomeAssistant, flow_id: str, factor: float | None) -> ConfigFlowResult:
    """The household form, with the bill's factor or left empty for a station."""
    user_input: dict[str, Any] = {
        CONF_ANNUAL_CONSUMPTION_KWH: 12000,
        CONF_CARD_ARCHIVE: False,
        CONF_DAILY_COMPARE: False,
    }
    if factor is not None:
        user_input[CONF_CONVERSION_FACTOR] = factor
    return await hass.config_entries.flow.async_configure(flow_id, user_input)


def _advanced(schema: Any) -> dict[Any, Any]:
    """The fields folded in the contract form's section."""
    return dict(schema.schema["advanced"].schema.schema)


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
    # The postcode named the operator, so it is not asked.
    assert result["step_id"] == "household"
    # The bill's factor carries the DSO's pressure and temperature correction
    # the station value leaves out, so it is asked on the household form;
    # left empty, the stations are listed.
    schema = result["data_schema"]
    assert schema is not None
    assert CONF_CONVERSION_FACTOR in schema.schema
    assert CONF_CONVERSION_MODE not in schema.schema
    with patch(
        "custom_components.be_gas_prices.config_flow._stations", AsyncMock(return_value=STATIONS)
    ):
        result = await _household(hass, result["flow_id"], None)
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
    # Sibelga is the only operator in Brussels.
    assert result["step_id"] == "household"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ANNUAL_CONSUMPTION_KWH: 17000,
            CONF_CALIBER: "q16",
            CONF_CONVERSION_FACTOR: 11.4,
            CONF_CARD_ARCHIVE: True,
            CONF_DAILY_COMPARE: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_DSO] == DSO_SIBELGA
    assert result["data"][CONF_CALIBER] == "q16"
    assert result["data"][CONF_CONVERSION_FACTOR] == 11.4
    assert result["data"][CONF_CONTRACT_START_DATE] == "2026-03-01"


async def test_the_custom_supplier_is_not_asked_its_signing_rate(hass: HomeAssistant) -> None:
    """Its card is the figures the household types; a start date must not
    ask for a second price that would be laid over them."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_POSTCODE: ""})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: REGION_WALLONIA}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLIER: SUPPLIER_CUSTOM}
    )
    assert "signed_rate" not in _advanced(result["data_schema"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CONTRACT: CUSTOM_CONTRACT, CONF_CONTRACT_START_DATE: "2026-03-01"},
    )
    assert result["step_id"] == "dso"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DSO: DSO_ORES})
    assert result["step_id"] == "custom_energy"


async def test_the_contract_form_folds_the_rarely_needed_fields(hass: HomeAssistant) -> None:
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
    schema = result["data_schema"]
    assert schema is not None
    assert list(schema.schema) == [CONF_CONTRACT, CONF_CONTRACT_START_DATE, "advanced"]
    assert schema.schema["advanced"].options == {"collapsed": True}
    assert list(_advanced(schema)) == [
        CONF_TARIFF_CARD_DATE,
        CONF_CONTRACT_END_DATE,
        CONF_YTD_FROM_CONTRACT_START,
        "signed_rate",
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CONTRACT: "engie_easy_fixed",
            CONF_CONTRACT_START_DATE: "2026-03-01",
            "advanced": {CONF_CONTRACT_END_DATE: "2026-02-01"},
        },
    )
    assert result["errors"] == {"base": "end_before_start"}
    # The form comes back with the section's fields as typed.
    schema = result["data_schema"]
    assert schema is not None
    end = next(key for key in _advanced(schema) if key == CONF_CONTRACT_END_DATE)
    assert end.description == {"suggested_value": "2026-02-01"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_CONTRACT: "engie_easy_fixed",
            CONF_CONTRACT_START_DATE: "2026-03-01",
            "advanced": {CONF_TARIFF_CARD_DATE: "2026-02-10"},
        },
    )
    # The contract's figures are only asked for when the household says so.
    assert result["step_id"] == "dso"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DSO: DSO_ORES})
    result = await _household(hass, result["flow_id"], 11.4)
    data = result["data"]
    assert data[CONF_TARIFF_CARD_DATE] == "2026-02-10"
    assert data[CONF_YTD_FROM_CONTRACT_START] is False
    assert "advanced" not in data
    assert "signed_rate" not in data


async def test_settings_unfold_the_figures_the_entry_holds(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_TARIFF_CARD_DATE: "2026-02-10", CONF_MANUAL_FEE: 50.0}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_REGION: REGION_WALLONIA}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    schema = result["data_schema"]
    assert schema is not None
    assert schema.schema["advanced"].options == {"collapsed": False}
    advanced = _advanced(schema)
    card_month = next(key for key in advanced if key == CONF_TARIFF_CARD_DATE)
    assert card_month.description == {"suggested_value": "2026-02-10"}
    figures = next(key for key in advanced if key == "signed_rate")
    assert figures.default() is True
    # Turned off, the typed figures go.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CONTRACT: "engie_flow",
            "advanced": {CONF_TARIFF_CARD_DATE: "2026-02-10", "signed_rate": False},
        },
    )
    assert result["step_id"] == "dso"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DSO: DSO_ORES}
    )
    result = await _household_options(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_MANUAL_FEE not in entry.data
    assert entry.data[CONF_TARIFF_CARD_DATE] == "2026-02-10"


async def test_typed_figures_alone_unfold_the_section(hass: HomeAssistant) -> None:
    """A fee typed as 0 is a figure all the same: the section that holds
    the switch for it is shown open."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MANUAL_FEE: 0.0})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: "engie"},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    schema = result["data_schema"]
    assert schema is not None
    assert schema.schema["advanced"].options == {"collapsed": False}


@pytest.mark.parametrize(
    ("held", "folded"),
    [
        ({CONF_TARIFF_CARD_DATE: "2026-02-10"}, False),
        ({CONF_CONTRACT_END_DATE: "2027-03-01"}, False),
        ({CONF_CONTRACT_START_DATE: "2026-03-01", CONF_YTD_FROM_CONTRACT_START: True}, False),
        # The start date is not in the section.
        ({CONF_CONTRACT_START_DATE: "2026-03-01"}, True),
    ],
)
async def test_settings_unfold_the_section_for_what_it_holds(
    hass: HomeAssistant, held: dict[str, Any], folded: bool
) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, **held})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: "engie"},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    schema = result["data_schema"]
    assert schema is not None
    assert schema.schema["advanced"].options == {"collapsed": folded}
    # Each date the section holds is offered again, so saving keeps it.
    for key in (CONF_TARIFF_CARD_DATE, CONF_CONTRACT_END_DATE):
        if key in held:
            field = next(k for k in _advanced(schema) if k == key)
            assert field.description == {"suggested_value": held[key]}


async def test_figures_left_from_another_supplier_do_not_unfold_a_custom_entry(
    hass: HomeAssistant,
) -> None:
    """An entry with a typed fee moved to the custom supplier: there is no
    switch for the fee in its section, so the section stays folded and the
    fee goes."""
    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_MANUAL_FEE: 50.0})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: SUPPLIER_CUSTOM},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    schema = result["data_schema"]
    assert schema is not None
    assert schema.schema["advanced"].options == {"collapsed": True}
    assert "signed_rate" not in _advanced(schema)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CONTRACT: CUSTOM_CONTRACT}
    )
    assert result["step_id"] == "dso"


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
        result = await _household(hass, result["flow_id"], None)
    assert result["step_id"] == "factor"
    assert result["errors"] == {"base": "stations_unavailable"}


async def test_the_station_list_covers_atrias_two_latest_months(hass: HomeAssistant) -> None:
    """A station listed at 0 in August, out of use that month only, can
    still be picked from July's value."""
    july, august = STATIONS[0], STATIONS[1]
    files: dict[str, Any] = {
        "GCV202607.txt": {july: 11.5, august: 11.4},
        "GCV202608.txt": {august: 11.3},
    }

    async def fetch_month(_session: Any, _context: Any, _key: Any, path: str) -> Any:
        if files[path] is None:
            raise calorific.CalorificError("calorific value file has a bad value 'n/a'")
        return files[path]

    with (
        patch.object(calorific, "build_ssl_context", lambda: None),
        patch.object(calorific, "subscription_key", AsyncMock(return_value="k")),
        patch.object(
            calorific,
            "list_months",
            AsyncMock(
                return_value={
                    "2026-06": "GCV202606.txt",
                    "2026-07": "GCV202607.txt",
                    "2026-08": "GCV202608.txt",
                }
            ),
        ),
        patch.object(calorific, "fetch_month", fetch_month),
    ):
        stations = await config_flow._stations(hass)
        assert {station.ean for station in stations} == {july.ean, august.ean}
        # Either month unreadable: the other's list all the same.
        files["GCV202607.txt"] = None
        stations = await config_flow._stations(hass)
        assert {station.ean for station in stations} == {august.ean}
        files["GCV202607.txt"] = {july: 11.5, august: 11.4}
        files["GCV202608.txt"] = None
        stations = await config_flow._stations(hass)
        assert {station.ean for station in stations} == {july.ean, august.ean}
        # Neither: Atrias cannot be read.
        files["GCV202607.txt"] = None
        with pytest.raises(calorific.CalorificError):
            await config_flow._stations(hass)


async def test_a_station_out_of_the_list_is_kept_on_offer(hass: HomeAssistant) -> None:
    """An entry's station Atrias listed at 0 lately: its settings save with
    it rather than asking for another one."""
    entry = _entry(hass)
    data = {k: v for k, v in entry.data.items() if k != CONF_CONVERSION_FACTOR}
    hass.config_entries.async_update_entry(
        entry, data={**data, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: "5414"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(
        "custom_components.be_gas_prices.config_flow._stations", AsyncMock(return_value=STATIONS)
    ):
        for user_input in (
            {"next_step_id": "settings"},
            {},
            {CONF_REGION: REGION_WALLONIA},
            {CONF_SUPPLIER: "engie"},
            {CONF_CONTRACT: "engie_flow"},
            {CONF_DSO: DSO_ORES},
        ):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input
            )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ANNUAL_CONSUMPTION_KWH: 15000,
                CONF_CARD_ARCHIVE: False,
                CONF_DAILY_COMPARE: False,
            },
        )
        assert result["step_id"] == "station"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_STATION: "5414"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_STATION] == "5414"


async def test_atrias_down_keeps_the_station_an_entry_names(hass: HomeAssistant) -> None:
    """Editing the settings of an entry on a station while Atrias is down:
    the station it has is kept rather than the edit being held up."""
    entry = _entry(hass)
    data = {k: v for k, v in entry.data.items() if k != CONF_CONVERSION_FACTOR}
    hass.config_entries.async_update_entry(
        entry,
        data={**data, CONF_CONVERSION_MODE: CONVERSION_STATION, CONF_STATION: STATIONS[0].ean},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(
        "custom_components.be_gas_prices.config_flow._stations",
        AsyncMock(side_effect=calorific.CalorificError("HTTP 503")),
    ):
        for user_input in (
            {"next_step_id": "settings"},
            {},
            {CONF_REGION: REGION_WALLONIA},
            {CONF_SUPPLIER: "engie"},
            {CONF_CONTRACT: "engie_flow"},
            {CONF_DSO: DSO_ORES},
        ):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], user_input
            )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ANNUAL_CONSUMPTION_KWH: 15000,
                CONF_CARD_ARCHIVE: False,
                CONF_DAILY_COMPARE: False,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_STATION] == STATIONS[0].ean
    assert entry.data[CONF_CONVERSION_MODE] == CONVERSION_STATION
    assert entry.data[CONF_ANNUAL_CONSUMPTION_KWH] == 15000


async def test_an_empty_factor_picks_the_station_instead(hass: HomeAssistant) -> None:
    """A household with no bill at hand leaves the factor empty and picks its
    reception station."""
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
        "custom_components.be_gas_prices.config_flow._stations", AsyncMock(return_value=STATIONS)
    ):
        result = await _household(hass, result["flow_id"], None)
        assert result["step_id"] == "station"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_STATION: STATIONS[0].ean}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_CONVERSION_MODE] == CONVERSION_STATION
    assert data[CONF_STATION] == STATIONS[0].ean
    assert CONF_CONVERSION_FACTOR not in data


async def test_a_cleared_factor_is_not_offered_again(hass: HomeAssistant) -> None:
    """The settings of an entry on its bill's factor: the user clears it for
    a station while Atrias is down, and the factor form comes back empty."""
    entry = _entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: "engie"},
        {CONF_CONTRACT: "engie_flow"},
        {CONF_DSO: DSO_ORES},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    with patch(
        "custom_components.be_gas_prices.config_flow._stations",
        AsyncMock(side_effect=calorific.CalorificError("HTTP 503")),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ANNUAL_CONSUMPTION_KWH: 17000,
                CONF_CARD_ARCHIVE: False,
                CONF_DAILY_COMPARE: False,
            },
        )
    assert result["step_id"] == "factor"
    assert result["errors"] == {"base": "stations_unavailable"}
    schema = result["data_schema"]
    assert schema is not None
    factor = next(key for key in schema.schema if key == CONF_CONVERSION_FACTOR)
    assert (factor.description or {}).get("suggested_value") is None


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


async def test_an_images_only_supplier_needs_the_card_archive(hass: HomeAssistant) -> None:
    """Ecofix's cards are page images: without the archive's reading the
    entry could not be priced, so the option cannot be off."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_POSTCODE: ""})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: REGION_WALLONIA}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "ecofix"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONTRACT: "ecofix_flexy"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_DSO: DSO_ORES})
    result = await _household(hass, result["flow_id"], 11.4)
    assert result["step_id"] == "household"
    assert result["errors"] == {CONF_CARD_ARCHIVE: "card_archive_needed"}
    # The form comes back with the factor as typed.
    schema = result["data_schema"]
    assert schema is not None
    factor = next(key for key in schema.schema if key == CONF_CONVERSION_FACTOR)
    assert factor.description == {"suggested_value": 11.4}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_ANNUAL_CONSUMPTION_KWH: 12000,
            CONF_CONVERSION_FACTOR: 11.4,
            CONF_CARD_ARCHIVE: True,
            CONF_DAILY_COMPARE: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_the_archive_error_keeps_a_cleared_meter_cleared(hass: HomeAssistant) -> None:
    """In the settings of an Ecofix entry the user clears its meter and
    leaves the archive off: the form comes back without the old meter."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ecofix Flexy",
        data={
            CONF_REGION: REGION_WALLONIA,
            CONF_DSO: DSO_ORES,
            CONF_SUPPLIER: "ecofix",
            CONF_CONTRACT: "ecofix_flexy",
            CONF_ANNUAL_CONSUMPTION_KWH: 12000.0,
            CONF_CONVERSION_MODE: CONVERSION_MANUAL,
            CONF_CONVERSION_FACTOR: 11.5,
            CONF_CARD_ARCHIVE: True,
            CONF_GAS_METER: "sensor.old_meter",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: "ecofix"},
        {CONF_CONTRACT: "ecofix_flexy"},
        {CONF_DSO: DSO_ORES},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    result = await _household_options(hass, result["flow_id"])
    assert result["errors"] == {CONF_CARD_ARCHIVE: "card_archive_needed"}
    schema = result["data_schema"]
    assert schema is not None
    meter = next(key for key in schema.schema if key == CONF_GAS_METER)
    assert (meter.description or {}).get("suggested_value") is None


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
    # The custom supplier has no card to quote, so it is not offered.
    schema = result["data_schema"]
    assert schema is not None
    offered = [o["value"] for o in schema.schema[CONF_SUPPLIER].config["options"]]
    assert "engie" in offered and SUPPLIER_CUSTOM not in offered
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


async def test_options_compare_the_own_contract_quotes_it_once(hass: HomeAssistant) -> None:
    """Picking the household's own contract shows it once, at what it pays,
    rather than beside a second quote on the card of the month."""
    entry = _entry(hass)
    own = Quote("engie", "engie_flow", "Engie Flow", 1500.0, 0.08, 190.0, True, True)
    quoted = AsyncMock(return_value=own)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SUPPLIER: "engie"}
    )
    with patch("custom_components.be_gas_prices.config_flow.quote_contract", quoted):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CONTRACT: "engie_flow"}
        )
    assert quoted.await_count == 1
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    rows = [line for line in placeholders["table"].splitlines() if "Engie Flow" in line]
    assert rows == ["| 1 | **Engie Flow †** | 1,500.00 | +0.00 | 0.0800 |"]


async def test_compare_all_ranks_a_custom_household_s_own_card(hass: HomeAssistant) -> None:
    """The typed card is handed to the ranking, so the household's own
    contract is in the table, in bold."""
    data = {
        CONF_REGION: REGION_WALLONIA,
        CONF_DSO: DSO_ORES,
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_ANNUAL_CONSUMPTION_KWH: 17000.0,
        CONF_CONVERSION_MODE: CONVERSION_MANUAL,
        CONF_CONVERSION_FACTOR: 11.5,
        CONF_CARD_ARCHIVE: False,
        CONF_CUSTOM_PRICE: 8.0,
        CONF_CUSTOM_FEE: 60.0,
        CONF_CUSTOM_T1_FIXED: 10.0,
        CONF_CUSTOM_T1_PROP: 3.0,
        CONF_CUSTOM_T2_FIXED: 50.0,
        CONF_CUSTOM_T2_PROP: 2.0,
        CONF_CUSTOM_TRANSPORT: 0.2,
    }
    entry = MockConfigEntry(domain=DOMAIN, title="Custom", data=data)
    entry.add_to_hass(hass)
    own = Quote(SUPPLIER_CUSTOM, CUSTOM_CONTRACT, "Custom", 1600.0, 0.09, 100.0, False, False)
    cheaper = Quote("engie", "engie_flow", "Engie Flow", 1500.0, 0.08, 100.0, True, False)
    calls: list[dict[str, Any]] = []

    async def ranked(*_args: Any, **kwargs: Any) -> tuple[list[Quote], int]:
        calls.append(kwargs)
        # Long enough for the flow to show its progress first, as a real
        # ranking does.
        await asyncio.sleep(0.01)
        return [cheaper, own], 0

    with patch("custom_components.be_gas_prices.config_flow.rank", ranked):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(result["flow_id"])
    own = calls[0]["own"]
    assert (own.contract, own.card.energy.price) == (CUSTOM_CONTRACT, pytest.approx(0.08))
    assert result["step_id"] == "compare_all_result"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert "| 2 | **Custom** | 1,600.00 | +0.00 |" in placeholders["table"]


_EARLIER = {
    CONF_SUPPLIER: "engie",
    CONF_CONTRACT: "engie_easy_fixed",
    CONF_REGION: REGION_WALLONIA,
    CONF_DSO: DSO_ORES,
}


@pytest.mark.freeze_time("2026-09-15 12:00:00+02:00")
@pytest.mark.parametrize(
    ("extra", "switched", "valid"),
    [
        # Last year, though a change of last year is recorded.
        ({CONF_PREVIOUS_CONTRACTS: [{**_EARLIER, "until": "2025-03-31"}]}, "2025-12-01", False),
        # The day of the last change: the contract it closes supplied none.
        (
            {
                CONF_PREVIOUS_CONTRACTS: [{**_EARLIER, "until": "2026-05-31"}],
                CONF_CONTRACT_START_DATE: "2026-06-01",
            },
            "2026-06-01",
            False,
        ),
        # Before the current contract started.
        ({CONF_CONTRACT_START_DATE: "2026-06-01"}, "2026-03-01", False),
        ({}, "2026-01-01", False),
        ({CONF_CONTRACT_START_DATE: "2026-06-01"}, "2026-06-02", True),
        ({}, "2026-01-02", True),
    ],
)
async def test_a_change_must_close_a_contract_that_supplied_this_year(
    hass: HomeAssistant, extra: dict[str, Any], switched: str, valid: bool
) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, **extra})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "switch"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SWITCH_DATE: switched}
    )
    if valid:
        assert result["step_id"] == "supplier"
    else:
        assert result["errors"] == {CONF_SWITCH_DATE: "switch_date_invalid"}


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
    # The form suggests the switch date as the contract start.
    schema = result["data_schema"]
    assert schema is not None
    start = next(key for key in schema.schema if key == CONF_CONTRACT_START_DATE)
    assert start.description == {"suggested_value": "2026-06-01"}
    # The year starts with the earlier contract: counting from this one's
    # start is not offered, since it could not apply.
    assert CONF_YTD_FROM_CONTRACT_START not in _advanced(schema)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CONTRACT: "engie_easy_fixed",
            CONF_CONTRACT_START_DATE: "2026-06-01",
            "advanced": {"signed_rate": True},
        },
    )
    assert result["step_id"] == "signed_rate"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DSO: DSO_ORES}
    )
    # Suggested rather than a default, so clearing it reaches the flow empty.
    schema = result["data_schema"]
    assert schema is not None
    factor = next(key for key in schema.schema if key == CONF_CONVERSION_FACTOR)
    assert factor.description == {"suggested_value": 11.5}
    result = await _household_options(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    [previous] = entry.data[CONF_PREVIOUS_CONTRACTS]
    assert previous[CONF_CONTRACT] == "engie_flow"
    assert previous["until"] == "2026-05-31"
    assert entry.data[CONF_CONTRACT] == "engie_easy_fixed"
    assert entry.data[CONF_CONTRACT_START_DATE] == "2026-06-01"
    assert entry.title == "Engie Easy Fixe"


async def test_settings_keep_a_title_the_user_typed(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    hass.config_entries.async_update_entry(entry, title="My house")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: "engie"},
        {CONF_CONTRACT: "engie_flow"},
        {CONF_DSO: DSO_ORES},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    result = await _household_options(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.title == "My house"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("OCTA+ Flux", "OCTA+ Boost Flex"),
        ("My house", "My house"),
        ("OCTA+ at home", "OCTA+ at home"),
    ],
)
async def test_settings_retitle_an_entry_whose_contract_was_withdrawn(
    hass: HomeAssistant, before: str, after: str
) -> None:
    """The entry was titled "OCTA+ Flux" by the wizard; Flux was withdrawn,
    and the contract picked instead gives the title. One the user typed
    stays."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=before,
        data={
            CONF_REGION: REGION_WALLONIA,
            CONF_DSO: DSO_ORES,
            CONF_SUPPLIER: "octaplus",
            CONF_CONTRACT: "octaplus_flux",
            CONF_ANNUAL_CONSUMPTION_KWH: 17000.0,
            CONF_CONVERSION_MODE: CONVERSION_MANUAL,
            CONF_CONVERSION_FACTOR: 11.5,
            CONF_CARD_ARCHIVE: False,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    for user_input in (
        {"next_step_id": "settings"},
        {},
        {CONF_REGION: REGION_WALLONIA},
        {CONF_SUPPLIER: "octaplus"},
        {CONF_CONTRACT: "octaplus_boostflex"},
        {CONF_DSO: DSO_ORES},
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    result = await _household_options(hass, result["flow_id"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.title == after


async def _household_options(hass: HomeAssistant, flow_id: str) -> ConfigFlowResult:
    return await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_ANNUAL_CONSUMPTION_KWH: 17000,
            CONF_CONVERSION_FACTOR: 11.5,
            CONF_CARD_ARCHIVE: False,
            CONF_DAILY_COMPARE: False,
        },
    )
