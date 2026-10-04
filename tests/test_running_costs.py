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

from custom_components.be_gas_prices.bill import (
    MonthBill,
    bill_month,
    contract_leg,
    index_for,
    resolve_energy_price,
)
from custom_components.be_gas_prices.const import (
    CALIBER_Q10,
    CONF_MANUAL_BASE,
    CONF_MANUAL_FACTOR,
    CONF_MANUAL_PRICE,
    CONF_SUPPLIER,
    DSO_ORES,
    REGION_FLANDERS,
    REGION_WALLONIA,
    SUPPLIER_CUSTOM,
)
from custom_components.be_gas_prices.providers import engie, sparki
from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import SupplierSnapshot
from custom_components.be_gas_prices.running_costs import (
    Household,
    _remaining_days_last_year,
    meter_start,
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


def _sparki(name: str) -> SupplierSnapshot:
    return sparki.parse_snapshot(
        "sparki_self_service",
        REGION_FLANDERS,
        fixture_text(
            "sparki", f"Sparki_Tariefkaart_{name}_Particulier_SelfService_Gas_NL.pdf", "layout"
        ),
        "u",
    )


def test_a_variable_contract_is_billed_on_the_card_of_the_month() -> None:
    """The supplier sets a variable price every month, so a contract signed
    in August is billed at September's price in September."""
    august, september = _sparki("augustus").energy, _sparki("september").energy
    assert august.price != september.price
    assert contract_leg(september, august, {}) is september


def test_a_fixed_contract_keeps_the_price_it_was_signed_at() -> None:
    signed = FixedRates(price=0.10, yearly_fixed_fee=60.0)
    assert contract_leg(FixedRates(price=0.07), signed, {}) is signed


def test_a_signed_formula_moves_with_the_index_its_month_card_implies() -> None:
    """With no index values published (Ecofix), the formula signed at 60
    EUR/MWh is priced at the 65 the card of the month prints its price at,
    not held at the signing month's price."""
    signed = IndexedRates(factor=0.001, base=0.004, index="TTF", price=0.064)
    own = IndexedRates(factor=0.0011, base=0.005, index="TTF", price=0.0765)
    assert contract_leg(own, signed, {}).price == pytest.approx(signed.at(65.0))
    # Typed figures are priced at the same index.
    typed = contract_leg(own, signed, {CONF_MANUAL_FACTOR: 0.1, CONF_MANUAL_BASE: 0.2})
    assert typed.price == pytest.approx((0.1 * 65.0 + 0.2) / 100.0 * 1.06)
    # No signing card and nothing typed: the card's own leg as printed.
    assert contract_leg(own, None, {}) is own


def test_a_signed_formula_before_the_first_index_value_moves_with_its_card() -> None:
    """A supplier whose index values start after the month billed (Trevion's
    begin in March 2026) prices January as if it published none: a signed or
    typed formula at the index the card of the month implies, and from its
    first value on, at the values it publishes."""
    signed = IndexedRates(factor=0.001, base=0.004, index="TTF", price=0.064)
    own = IndexedRates(factor=0.0011, base=0.005, index="TTF", price=0.0765)
    table = {"TTF": {"2026-03": 40.0}}
    leg = contract_leg(own, signed, {})
    assert resolve_energy_price(leg, table, "2026-01")[0] == pytest.approx(signed.at(65.0))
    assert resolve_energy_price(leg, table, "2026-03")[0] == pytest.approx(signed.at(40.0))
    typed = contract_leg(own, None, {CONF_MANUAL_FACTOR: 0.1, CONF_MANUAL_BASE: 0.2})
    assert resolve_energy_price(typed, table, "2026-01")[0] == pytest.approx(
        (0.1 * 65.0 + 0.2) / 100.0 * 1.06
    )


def test_nothing_is_laid_over_a_typed_custom_card() -> None:
    """Signing figures an earlier flow stored on a custom entry, carried
    into an earlier contract by a recorded switch, leave its card alone."""
    own = FixedRates(price=0.075, yearly_fixed_fee=60.0)
    data = {CONF_SUPPLIER: SUPPLIER_CUSTOM, CONF_MANUAL_PRICE: 8.0}
    assert contract_leg(own, None, data) is own
    assert contract_leg(own, None, {**data, CONF_SUPPLIER: "engie"}).price == (
        pytest.approx(0.08 * 1.06)
    )


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


def test_a_settled_month_missing_from_the_table_is_provisional() -> None:
    """Engie publishes October's ZTP101 on its index page hours after the
    month starts; until then October is priced at September's value, which
    is not the price the month settles at."""
    card = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_WALLONIA,
        fixture_text("engie", "G_EASY_R_GREY_C_I_12_W_F_202609.pdf"),
    )
    assert isinstance(card.energy, IndexedRates) and card.energy.settled
    table = {"ZTP101": {"2026-09": 61.768}}

    def bill(month: str, table: dict[str, dict[str, float]] | None) -> MonthBill:
        return bill_month(
            month=month,
            card=card,
            energy=card.energy,
            table=table,
            dso=DSO_ORES,
            annual_kwh=10_000.0,
            caliber=CALIBER_Q10,
            kwh=1000.0,
            days=30,
            days_in_year=365,
        )

    assert not bill("2026-09", table).provisional
    october = bill("2026-10", table)
    assert october.index is not None and october.index.month == "2026-09"
    assert october.provisional
    # No table at all: the card's own figure, which a settled card sets at
    # its month's value, and only its month's: August priced on it is not.
    assert not bill("2026-09", None).provisional
    assert bill("2026-08", None).provisional


def test_a_signed_leg_is_final_only_on_a_settled_card_of_its_month() -> None:
    """With no index values, a signed formula is priced at the index the
    month's card was set at: final where that card is settled, provisional
    where it went out on the month before's value, whatever the signing card
    was."""
    july = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_WALLONIA,
        fixture_text("engie", "G_EASY_R_GREY_C_I_12_W_F_202607.pdf"),
    )
    text = fixture_text("engie", "G_EASY_R_GREY_C_I_12_W_F_202609.pdf")
    settled = engie.parse_snapshot("engie_easy_variable", REGION_WALLONIA, text)
    early = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_WALLONIA,
        text.replace("d’application pour Septembre\n2026", "d’application pour Août\n2026"),
    )
    assert isinstance(july.energy, IndexedRates) and july.energy.settled
    assert isinstance(early.energy, IndexedRates) and not early.energy.settled

    def provisional(card: SupplierSnapshot) -> bool:
        leg = contract_leg(card.energy, july.energy, {CONF_SUPPLIER: "engie"})
        return bill_month(
            month="2026-09",
            card=card,
            energy=leg,
            table=None,
            dso=DSO_ORES,
            annual_kwh=10_000.0,
            caliber=CALIBER_Q10,
            kwh=1000.0,
            days=30,
            days_in_year=365,
        ).provisional

    assert not provisional(settled)
    assert provisional(early)
    # The comparisons quote the own contract on the card with its leg laid
    # over it: the same answer.
    for card, expected in ((settled, False), (early, True)):
        leg = contract_leg(card.energy, july.energy, {CONF_SUPPLIER: "engie"})
        quoted = replace(card, energy=leg)
        assert (
            bill_month(
                month="2026-09",
                card=quoted,
                energy=quoted.energy,
                table=None,
                dso=DSO_ORES,
                annual_kwh=10_000.0,
                caliber=CALIBER_Q10,
                kwh=1000.0,
                days=30,
                days_in_year=365,
            ).provisional
            is expected
        )


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


def test_to_kwh_needs_a_factor_for_every_billed_month_that_used_gas() -> None:
    days = {date(2026, 8, 31): 2.0, date(2026, 9, 1): 3.0, date(2026, 9, 2): 0.0}
    year = date(2026, 1, 1)
    assert to_kwh(days, lambda month: {"2026-08": 11.5, "2026-09": 11.0}.get(month), year) == {
        date(2026, 8, 31): pytest.approx(23.0),
        date(2026, 9, 1): pytest.approx(33.0),
        date(2026, 9, 2): 0.0,
    }
    assert to_kwh(days, lambda month: 11.5 if month == "2026-08" else None, year) is None


def test_a_day_before_the_billed_year_without_a_factor_is_left_out() -> None:
    """A station Atrias lists only since March 2025: last year's days before
    it are left out of the rolling year, this year's bill is whole."""
    days = {date(2025, 2, 28): 2.0, date(2025, 3, 1): 3.0, date(2026, 3, 1): 4.0}
    factors = {"2025-03": 11.5, "2026-03": 11.0}
    assert to_kwh(days, factors.get, date(2026, 1, 1)) == {
        date(2025, 3, 1): pytest.approx(34.5),
        date(2026, 3, 1): pytest.approx(44.0),
    }


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


@pytest.mark.parametrize(
    ("today", "start"),
    [
        (date(2026, 10, 1), date(2025, 10, 1)),
        (date(2028, 6, 15), date(2027, 6, 15)),
        (date(2028, 2, 29), date(2027, 2, 28)),
        (date(2029, 1, 20), date(2028, 1, 20)),
    ],
)
def test_the_meter_is_read_from_today_s_date_last_year(today: date, start: date) -> None:
    assert meter_start(today) == start


@pytest.mark.parametrize("today", [date(2028, 6, 15), date(2029, 1, 20), date(2027, 6, 15)])
def test_the_days_read_project_the_year_end_across_a_29_february(today: date) -> None:
    """Every day from meter_start to today on record: the rolling year and
    the projection both find what they read, a 29 February in between or
    not."""
    start = meter_start(today)
    days = {start + timedelta(days=n): 10.0 for n in range((today - start).days + 1)}
    assert rolling_year_kwh(days, today) is not None
    assert _remaining_days_last_year(days, today) is not None
