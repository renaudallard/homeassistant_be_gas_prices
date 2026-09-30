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

"""Billing a month and walking the year, without Home Assistant."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from custom_components.be_gas_prices.bill import bill_month, index_for, resolve_energy_price
from custom_components.be_gas_prices.const import CALIBER_Q10, DSO_ORES, REGION_WALLONIA
from custom_components.be_gas_prices.providers import engie
from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import SupplierSnapshot
from custom_components.be_gas_prices.running_costs import (
    Household,
    rolling_year_kwh,
    running_costs,
    to_kwh,
)
from tests import fixture_text

TABLE = {"ZTPDAM": {"2026-07": 53.116, "2026-08": 61.537}}


def _flow() -> SupplierSnapshot:
    return engie.parse_snapshot(
        "engie_flow", REGION_WALLONIA, fixture_text("engie", "G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
    )


def test_index_for_prefers_the_month_then_the_latest_before_it() -> None:
    assert index_for(TABLE, "ZTPDAM", "2026-07").value == 53.116  # type: ignore[union-attr]
    provisional = index_for(TABLE, "ZTPDAM", "2026-09")
    assert provisional is not None
    assert (provisional.value, provisional.month) == (61.537, "2026-08")
    assert index_for(TABLE, "ZTPDAM", "2026-06") is None
    assert index_for(None, "ZTPDAM", "2026-09") is None
    assert index_for(TABLE, "ZTP101", "2026-09") is None


def test_an_indexed_leg_is_priced_on_its_month() -> None:
    energy = _flow().energy
    assert isinstance(energy, IndexedRates)
    price, value = resolve_energy_price(energy, TABLE, "2026-07")
    assert value is not None and value.month == "2026-07"
    assert price == pytest.approx(energy.at(53.116))
    # No table: the card's printed figure.
    price, value = resolve_energy_price(energy, None, "2026-07")
    assert (price, value) == (pytest.approx(0.07643), None)


def test_a_fixed_leg_ignores_the_index() -> None:
    fixed = FixedRates(price=0.08, yearly_fixed_fee=60.0)
    assert resolve_energy_price(fixed, TABLE, "2026-07") == (0.08, None)


def test_bill_month_applies_the_law_to_the_delivery_month() -> None:
    """A card still printing the pre-August excise and energy contribution is
    billed on the law's figures for a September delivery."""
    card = _flow()
    stale = replace(
        card,
        taxes=replace(
            card.taxes,
            excise_bands=excise_bands(0.008724, 0.009864),
            energy_contribution=0.001058,
        ),
    )
    sept = bill_month(
        month="2026-09",
        card=stale,
        energy=stale.energy,
        table=TABLE,
        dso=DSO_ORES,
        annual_kwh=10_000.0,
        caliber=CALIBER_Q10,
        kwh=1000.0,
        days=30,
        days_in_year=365,
    )
    assert sept.breakdown.taxes == pytest.approx(0.0109286 + 0.000075)
    assert sept.provisional
    july = bill_month(
        month="2026-07",
        card=stale,
        energy=stale.energy,
        table=TABLE,
        dso=DSO_ORES,
        annual_kwh=10_000.0,
        caliber=CALIBER_Q10,
        kwh=1000.0,
        days=31,
        days_in_year=365,
    )
    assert july.breakdown.taxes == pytest.approx(0.008724 + 0.001058 + 0.000075)
    assert not july.provisional


def test_bill_month_accrues_the_fixed_costs_by_the_day() -> None:
    card = _flow()
    bill = bill_month(
        month="2026-07",
        card=card,
        energy=card.energy,
        table=TABLE,
        dso=DSO_ORES,
        annual_kwh=17_000.0,
        caliber=CALIBER_Q10,
        kwh=500.0,
        days=31,
        days_in_year=365,
    )
    # FLOW 50,00 plus ORES T2 140,93 a year, no metering, no levy.
    assert bill.fixed_cost == pytest.approx((50.0 + 140.93) * 31 / 365)
    assert bill.energy_cost == pytest.approx(500.0 * card.energy.at(53.116))  # type: ignore[union-attr]
    assert bill.total == pytest.approx(
        bill.energy_cost + bill.network_cost + bill.taxes_cost + bill.fixed_cost
    )


def test_to_kwh_needs_a_factor_for_every_month_that_used_gas() -> None:
    days = {date(2026, 8, 31): 2.0, date(2026, 9, 1): 3.0, date(2026, 9, 2): 0.0}
    assert to_kwh(days, lambda month: {"2026-08": 11.5, "2026-09": 11.0}.get(month)) == {
        date(2026, 8, 31): pytest.approx(23.0),
        date(2026, 9, 1): pytest.approx(33.0),
        date(2026, 9, 2): 0.0,
    }
    assert to_kwh(days, lambda month: 11.5 if month == "2026-08" else None) is None


def test_rolling_year_scales_a_few_missing_days_and_refuses_more() -> None:
    today = date(2026, 9, 29)
    full = {today - timedelta(days=n): 10.0 for n in range(365)}
    assert rolling_year_kwh(full, today) == pytest.approx(3650.0)
    gappy = {day: value for day, value in full.items() if day.day != 1}
    assert rolling_year_kwh(gappy, today) == pytest.approx(3650.0)
    short = {day: value for day, value in full.items() if day.month != 1}
    assert rolling_year_kwh(short, today) is None


def test_running_costs_bill_each_month_on_its_own_card() -> None:
    today = date(2026, 9, 10)
    card = _flow()
    august = replace(card, publication_label="2026-08")
    days = {
        date(2026, 1, 1) + timedelta(days=n): 40.0
        for n in range((today - date(2026, 1, 1)).days + 1)
    }
    household = Household(dso=DSO_ORES, caliber=CALIBER_Q10, annual_kwh=17_000.0)
    costs = running_costs(
        kwh_days=days,
        today=today,
        window_start=date(2026, 1, 1),
        household=household,
        current_card=card,
        month_card=lambda month: august if month == "2026-08" else None,
        energy_for=lambda snapshot: snapshot.energy,
        table=TABLE,
    )
    assert [bill.month for bill in costs.months] == [f"2026-{m:02d}" for m in range(1, 10)]
    assert costs.months_on_current_card == tuple(f"2026-{m:02d}" for m in range(1, 8))
    assert costs.ytd_kwh == pytest.approx(40.0 * len(days))
    assert costs.months[-1].days == 10
    assert costs.months[-1].kwh == pytest.approx(400.0)
    assert costs.current_year_cost == pytest.approx(sum(b.total for b in costs.months))
    assert costs.month_cost("2026-09") == pytest.approx(costs.months[-1].total)
    # No record of last year's remaining days: no year-end figure.
    assert costs.projected_year_end_cost is None
    assert costs.projected_year_kwh is None


def test_year_end_projection_uses_last_years_remaining_days() -> None:
    today = date(2026, 12, 20)
    card = _flow()
    days = {
        date(2025, 1, 1) + timedelta(days=n): 30.0
        for n in range((today - date(2025, 1, 1)).days + 1)
    }
    costs = running_costs(
        kwh_days=days,
        today=today,
        window_start=date(2026, 1, 1),
        household=Household(dso=DSO_ORES, caliber=CALIBER_Q10, annual_kwh=17_000.0),
        current_card=card,
        month_card=lambda month: None,
        energy_for=lambda snapshot: snapshot.energy,
        table=TABLE,
    )
    assert costs.projected_year_kwh == pytest.approx(costs.ytd_kwh + 11 * 30.0)
    assert costs.projected_year_end_cost is not None
    assert costs.projected_year_end_cost > costs.current_year_cost
