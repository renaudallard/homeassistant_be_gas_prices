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

"""The energy_rate action, which gives the energy price month by month.

Run on the real recorder, as the entry it prices with reads its meter there.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.be_gas_prices.const import DOMAIN
from custom_components.be_gas_prices.providers._rates import FixedRates, VariableRates
from tests.recorder.test_bill_cost import _meter
from tests.recorder.test_gas_meter import _setup_entry

pytestmark = pytest.mark.timeout(method="thread")


async def _rates(
    hass: HomeAssistant, entry_id: str, start: str, end: str, context: Context | None = None
) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "energy_rate",
        {"entry_id": entry_id, "start": start, "end": end},
        blocking=True,
        return_response=True,
        context=context,
    )
    assert isinstance(response, dict)
    return response


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_each_month_is_priced_as_the_year_prices_its_energy(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """The months held are priced as the running cost bills their energy;
    September, whose index is not out, and the months to come take August's
    value and are provisional."""
    await _meter(hass)
    entry = await _setup_entry(hass)
    year = {bill.month: bill for bill in entry.runtime_data.data.months}

    got = await _rates(hass, entry.entry_id, "2026-07-10", "2026-11-03")

    months = {row["month"]: row for row in got["months"]}
    assert list(months) == ["2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    for month in ("2026-07", "2026-08", "2026-09"):
        bill = year[month]
        assert months[month]["eur_per_kwh"] == pytest.approx(bill.energy_cost / bill.kwh, abs=1e-6)
    # The index is held for August only.
    assert [m for m, row in months.items() if not row["provisional"]] == ["2026-08"]
    assert months["2026-10"]["eur_per_kwh"] == months["2026-09"]["eur_per_kwh"]
    assert months["2026-11"]["eur_per_kwh"] == months["2026-09"]["eur_per_kwh"]
    # No card of their own: July and August are on the card in hand, which
    # is September's own and the months to come's only one.
    assert [m for m, row in months.items() if row["stand_in_card"]] == ["2026-07", "2026-08"]


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_a_month_to_come_is_final_on_a_fixed_price_only(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """A variable card states its own month's price: October's is not
    known in September. A fixed price is the term's."""
    await _meter(hass)
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data

    for leg, ahead in (
        (FixedRates(price=0.1), False),
        (VariableRates(price=0.1), True),
    ):
        with patch.object(coordinator, "_energy_for", lambda card, vat, leg=leg: leg):
            got = await _rates(hass, entry.entry_id, "2026-08-01", "2026-10-31")
        assert [(row["month"], row["provisional"]) for row in got["months"]] == [
            ("2026-08", False),
            ("2026-09", False),
            ("2026-10", ahead),
        ]


@pytest.mark.timeout(300, method="thread")
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_months_that_cannot_be_priced_are_refused(
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
        ("2023-12-31", "2024-01-31", "bill_start_too_early"),
        ("2026-09-01", "2027-12-01", "rate_end_too_late"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await _rates(hass, entry.entry_id, start, end)
        assert err.value.translation_key == key
    # Fourteen months past September is November of next year.
    got = await _rates(hass, entry.entry_id, "2024-01-01", "2027-11-30")
    assert got["months"][-1]["month"] == "2027-11"
    assert len(got["months"]) == 47

    with (
        patch.object(coordinator, "switch_day", return_value=date(2026, 7, 15)),
        pytest.raises(ServiceValidationError) as err,
    ):
        await _rates(hass, entry.entry_id, "2026-07-14", "2026-07-31")
    assert err.value.translation_key == "window_before_switch"

    with pytest.raises(ServiceValidationError) as err:
        await _rates(hass, "nope", "2026-07-01", "2026-07-31")
    assert err.value.translation_key == "unknown_entry"
    # Each call may fetch cards from the supplier.
    with pytest.raises(Unauthorized):
        await _rates(
            hass,
            entry.entry_id,
            "2026-07-01",
            "2026-07-31",
            Context(user_id=hass_read_only_user.id),
        )
