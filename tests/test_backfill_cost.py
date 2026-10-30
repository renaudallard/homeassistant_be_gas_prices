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

"""Spreading the year's bills over its hours, without Home Assistant."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.be_gas_prices.backfill import _hours
from custom_components.be_gas_prices.backfill_cost import backfill_cost_once, cost_rows
from custom_components.be_gas_prices.bill import MonthBill
from custom_components.be_gas_prices.pricing import PriceBreakdown


def _bill(first: date, days: int, kwh: float, rate: float, fixed: float) -> MonthBill:
    return MonthBill(
        month=f"{first:%Y-%m}",
        kwh=kwh,
        days=days,
        first=first,
        breakdown=PriceBreakdown(energy=rate, network=0.0, taxes=0.0, all_in=rate),
        energy_cost=kwh * rate,
        network_cost=0.0,
        taxes_cost=0.0,
        fixed_cost=fixed,
        index=None,
        provisional=False,
    )


def _local(day: date, hour: int = 0) -> datetime:
    return dt_util.as_utc(dt_util.start_of_local_day(day) + timedelta(hours=hour))


def test_the_last_row_is_the_year_billed() -> None:
    """Two months at 1 kWh an hour: each hour costs its month's rate, each
    day's fixed share lands on its local midnight, and the total at the end
    is what the bills add up to."""
    hours = _hours(_local(date(2026, 1, 1)), _local(date(2026, 2, 3)))
    bills = [
        _bill(date(2026, 1, 1), 31, 31 * 24, 0.10, 31.0),
        _bill(date(2026, 2, 1), 2, 2 * 24, 0.20, 4.0),
    ]
    rows = cost_rows(bills, dict.fromkeys(hours, 1.0), hours)
    assert rows is not None
    assert [hour for hour, _total in rows] == hours
    totals = dict(rows)
    assert totals[_local(date(2026, 1, 1))] == pytest.approx(1.0 + 0.10)
    assert totals[_local(date(2026, 1, 1), 1)] == pytest.approx(1.0 + 0.20)
    assert totals[_local(date(2026, 2, 1))] - totals[_local(date(2026, 1, 31), 23)] == (
        pytest.approx(2.0 + 0.20)
    )
    assert rows[-1][1] == pytest.approx(sum(bill.total for bill in bills))


@pytest.mark.parametrize(("day", "count"), [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_a_clock_change_day_counts_its_fixed_share_once(day: date, count: int) -> None:
    hours = _hours(_local(day), _local(day + timedelta(days=1)))
    assert len(hours) == count
    bill = _bill(day, 1, float(count), 0.10, 2.0)
    rows = cost_rows([bill], dict.fromkeys(hours, 1.0), hours)
    assert rows is not None
    assert rows[0][1] == pytest.approx(2.0 + 0.10)
    assert rows[-1][1] == pytest.approx(bill.total)


def test_each_contract_prices_its_own_days_of_a_month() -> None:
    """A switch on 3 September: the first two days on the earlier
    contract's rate, the rest of the month on the current one's."""
    hours = _hours(_local(date(2026, 9, 1)), _local(date(2026, 9, 5)))
    bills = [
        _bill(date(2026, 9, 1), 2, 48.0, 0.10, 0.0),
        _bill(date(2026, 9, 3), 2, 48.0, 0.30, 0.0),
    ]
    rows = cost_rows(bills, dict.fromkeys(hours, 1.0), hours)
    assert rows is not None
    totals = dict(rows)
    assert totals[_local(date(2026, 9, 2), 23)] == pytest.approx(4.8)
    assert totals[_local(date(2026, 9, 3))] == pytest.approx(4.8 + 0.30)
    assert rows[-1][1] == pytest.approx(4.8 + 14.4)


def test_a_day_the_meter_fell_on_weighs_what_it_was_billed() -> None:
    """The first day falls overall and is billed no gas; the second rises
    by 23 and falls by 11 in one hour, so its 12 are shared over the hours
    that rose."""
    day1, day2 = date(2026, 9, 1), date(2026, 9, 2)
    hours = _hours(_local(day1), _local(day2 + timedelta(days=1)))
    changes = dict.fromkeys(hours, 1.0)
    changes[_local(day1, 12)] = -50.0
    changes[_local(day2, 12)] = -11.0
    bill = _bill(day1, 2, 12.0, 1.0, 0.0)
    rows = cost_rows([bill], changes, hours)
    assert rows is not None
    totals = dict(rows)
    assert totals[_local(day1, 23)] == 0.0
    assert totals[_local(day2, 1)] - totals[_local(day2)] == pytest.approx(12.0 / 23)
    assert totals[_local(day2, 12)] == totals[_local(day2, 11)]
    assert rows[-1][1] == pytest.approx(12.0)


def test_gas_after_the_last_hour_written_lands_on_it() -> None:
    """The tick billed gas from an hour the backfill stops before: the cost
    is still there by the last row, on the last hour."""
    day = date(2026, 9, 1)
    hours = _hours(_local(day), _local(day, 10))
    bill = _bill(day, 1, 5.0, 1.0, 1.0)
    rows = cost_rows([bill], {}, hours)
    assert rows is not None
    assert rows[-2][1] == pytest.approx(1.0)
    assert rows[-1][1] == pytest.approx(6.0)


def test_an_hour_no_bill_covers_writes_nothing() -> None:
    """Bills made yesterday do not reach today's hours."""
    hours = _hours(_local(date(2026, 9, 1)), _local(date(2026, 9, 2), 3))
    assert cost_rows([_bill(date(2026, 9, 1), 1, 24.0, 1.0, 0.0)], {}, hours) is None


async def test_the_cost_is_written_again_only_when_its_inputs_move() -> None:
    """Stamped once written: the same cards and meter write nothing more, a
    new meter writes again, and a run that wrote nothing is not stamped."""
    coordinator = SimpleNamespace(
        cost_backfill_stamp=None,
        meter="sensor.gas",
        card_months_signature=lambda: "cards",
        async_save=AsyncMock(),
    )
    written = AsyncMock(return_value={})
    with patch("custom_components.be_gas_prices.backfill_cost.backfill_cost", written):
        await backfill_cost_once(MagicMock(), coordinator)  # type: ignore[arg-type]
        assert coordinator.cost_backfill_stamp is None
        written.return_value = {"sensor.gas_current_year_cost": 3}
        await backfill_cost_once(MagicMock(), coordinator)  # type: ignore[arg-type]
        await backfill_cost_once(MagicMock(), coordinator)  # type: ignore[arg-type]
        assert written.await_count == 2
        coordinator.meter = "sensor.other"
        await backfill_cost_once(MagicMock(), coordinator)  # type: ignore[arg-type]
    assert written.await_count == 3
    assert coordinator.async_save.await_count == 2
