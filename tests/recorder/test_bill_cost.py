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

"""The bill_cost action, which prices a closed run of days off the meter.

Run on the real recorder, since what it prices is the meter's history.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockUser
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.be_gas_prices import bill_cost
from custom_components.be_gas_prices.const import CONF_CONVERSION_FACTOR, DOMAIN
from tests.recorder.test_gas_meter import _card, _import, _setup_entry, _zone

pytestmark = pytest.mark.timeout(method="thread")

# 1 m3 an hour from 1 June 00:00 to 15 September 00:00, Brussels.
FIRST = date(2026, 6, 1)
HOURS = (date(2026, 9, 15) - FIRST).days * 24


async def _meter(hass: HomeAssistant, first: date = FIRST, hours: int = HOURS) -> None:
    await _zone(hass)
    _import(hass, "m³", dt_util.as_utc(dt_util.start_of_local_day(first)), hours, 1.0)
    await async_wait_recording_done(hass)


async def _cost(
    hass: HomeAssistant, entry_id: str, start: str, end: str, context: Context | None = None
) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "bill_cost",
        {"entry_id": entry_id, "start": start, "end": end},
        blocking=True,
        return_response=True,
        context=context,
    )
    assert isinstance(response, dict)
    return response


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_the_days_are_priced_as_the_year_prices_them(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """July and August cost what the year's running cost bills them, each
    on the card in hand, which no card of their own replaces."""
    await _meter(hass)
    entry = await _setup_entry(hass)
    year = {bill.month: bill for bill in entry.runtime_data.data.months}

    got = await _cost(hass, entry.entry_id, "2026-07-01", "2026-08-31")

    assert [m["month"] for m in got["months"]] == ["2026-07", "2026-08"]
    for month in got["months"]:
        assert month["total"] == round(year[month["month"]].total, 2)
        assert month["kwh"] == pytest.approx(year[month["month"]].kwh)
    assert got["cost"] == round(year["2026-07"].total + year["2026-08"].total, 2)
    assert got["kwh"] == pytest.approx(62 * 24 * 11.5)
    assert got["covered"] is True
    assert (got["days"], got["days_on_record"]) == (62, 62)
    assert got["annual_kwh"] == 17000.0
    assert got["months_on_stand_in_card"] == ["2026-07", "2026-08"]
    # The index is held for August only.
    assert got["provisional_months"] == ["2026-07"]


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_a_last_month_card_that_cannot_price_gives_way(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """August's own card has no row for the household's DSO: the card in
    hand bills the month, as in the year, and the month is named."""
    await _meter(hass)
    card = _card()
    months = AsyncMock(return_value=replace(card, dsos={}, publication_label="2026-08"))
    entry = await _setup_entry(hass, fetch_for_month=months)
    year = {bill.month: bill for bill in entry.runtime_data.data.months}

    got = await _cost(hass, entry.entry_id, "2026-08-01", "2026-08-31")

    assert got["cost"] == round(year["2026-08"].total, 2)
    assert got["months_on_stand_in_card"] == ["2026-08"]


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_days_before_the_meter_are_not_covered(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A meter first recorded inside the days asked leaves them short: the
    answer says so, rather than the cost passing for the whole bill."""
    await _meter(hass)
    entry = await _setup_entry(hass)

    got = await _cost(hass, entry.entry_id, "2026-05-20", "2026-06-10")

    assert got["covered"] is False
    assert (got["days"], got["days_on_record"]) == (22, 10)
    assert got["first_day_on_record"] == "2026-06-01"
    assert got["kwh"] == pytest.approx(10 * 24 * 11.5)


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_a_last_day_not_recorded_is_not_covered(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Nothing recorded on the last day: its gas may come in a later day."""
    await _meter(hass, hours=(date(2026, 8, 31) - FIRST).days * 24)
    entry = await _setup_entry(hass)

    got = await _cost(hass, entry.entry_id, "2026-07-01", "2026-08-31")

    assert got["covered"] is False
    assert got["days_on_record"] == 61


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_a_total_started_over_is_not_covered(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A day the recorder's total went back lost the gas before it."""
    await _meter(hass)
    entry = await _setup_entry(hass)
    days = {date(2026, 6, 30) + timedelta(days=n): 24.0 for n in range(33)}
    days[date(2026, 7, 10)] = -500.0

    with patch.object(bill_cost, "daily_changes", AsyncMock(return_value=days)):
        got = await _cost(hass, entry.entry_id, "2026-07-01", "2026-08-01")

    assert got["days_on_record"] == 32
    assert got["covered"] is False


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_days_that_cannot_be_priced_are_refused(
    recorder_mock: Any,
    hass: HomeAssistant,
    enable_custom_integrations: None,
    hass_read_only_user: MockUser,
) -> None:
    await _meter(hass)
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data

    for start, end, key in (
        ("2026-08-02", "2026-08-01", "window_reversed"),
        ("2026-09-01", "2026-09-15", "window_not_closed"),
        ("2023-12-31", "2024-01-31", "bill_start_too_early"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await _cost(hass, entry.entry_id, start, end)
        assert err.value.translation_key == key
    assert (await _cost(hass, entry.entry_id, "2024-01-01", "2024-01-31"))["kwh"] == 0.0

    with (
        patch.object(coordinator, "switch_day", return_value=date(2026, 7, 15)),
        pytest.raises(ServiceValidationError) as err,
    ):
        await _cost(hass, entry.entry_id, "2026-07-14", "2026-07-31")
    assert err.value.translation_key == "window_before_switch"
    with patch.object(coordinator, "switch_day", return_value=date(2026, 7, 15)):
        await _cost(hass, entry.entry_id, "2026-07-15", "2026-07-31")

    with pytest.raises(ServiceValidationError) as err:
        await _cost(hass, "nope", "2026-07-01", "2026-07-31")
    assert err.value.translation_key == "unknown_entry"
    # The meter's history says when a household is away.
    with pytest.raises(Unauthorized):
        await _cost(
            hass,
            entry.entry_id,
            "2026-07-01",
            "2026-07-31",
            Context(user_id=hass_read_only_user.id),
        )


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_gas_without_a_conversion_factor_is_refused(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    await _meter(hass)
    entry = await _setup_entry(hass, extra={CONF_CONVERSION_FACTOR: None})

    with pytest.raises(HomeAssistantError) as err:
        await _cost(hass, entry.entry_id, "2026-07-01", "2026-07-31")
    assert err.value.translation_key == "no_conversion_factor"
