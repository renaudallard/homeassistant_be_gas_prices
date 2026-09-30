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

"""The features carried over from the electricity integration, unit by unit."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices import providers
from custom_components.be_gas_prices.compare import IndexCache, Quote, quote_contract
from custom_components.be_gas_prices.compare_table import quote_table
from custom_components.be_gas_prices.const import (
    CALIBER_Q10,
    CONF_CALIBER,
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_CUSTOM_CONNECTION_FEE,
    CONF_CUSTOM_EXCISE_HIGH,
    CONF_CUSTOM_EXCISE_LOW,
    CONF_CUSTOM_FEE,
    CONF_CUSTOM_LEVY,
    CONF_CUSTOM_METERING,
    CONF_CUSTOM_PRICE,
    CONF_CUSTOM_T1_FIXED,
    CONF_CUSTOM_T1_PROP,
    CONF_CUSTOM_T2_FIXED,
    CONF_CUSTOM_T2_PROP,
    CONF_CUSTOM_TRANSPORT,
    CONF_DSO,
    CONF_MANUAL_BASE,
    CONF_MANUAL_FACTOR,
    CONF_MANUAL_FEE,
    CONF_MANUAL_PRICE,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_SUPPLIER,
    CONF_YTD_FROM_CONTRACT_START,
    CUSTOM_CONTRACT,
    DSO_FLUVIUS_KEMPEN,
    DSO_ORES,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    SUPPLIER_CUSTOM,
    TIER_T2,
)
from custom_components.be_gas_prices.contract_periods import (
    PeriodBilling,
    current_period_start,
    periods_this_year,
    previous_contracts,
    record_switch,
)
from custom_components.be_gas_prices.daily_ranking import DailyRanking, ranking_minute
from custom_components.be_gas_prices.manual_rate import manual_leg
from custom_components.be_gas_prices.month_cards import MonthCardCache
from custom_components.be_gas_prices.pricing import compute_breakdown, fixed_costs
from custom_components.be_gas_prices.providers import engie
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers._resolve import resolve_network
from custom_components.be_gas_prices.providers.base import CardNotReadableError, SupplierSnapshot
from custom_components.be_gas_prices.providers.custom import build_snapshot
from custom_components.be_gas_prices.running_costs import Household, RunningCosts
from tests import approx, fixture_text

ENTRY = {
    CONF_SUPPLIER: "engie",
    CONF_CONTRACT: "engie_flow",
    CONF_REGION: "wallonia",
    CONF_DSO: DSO_ORES,
    CONF_CALIBER: "q10",
    CONF_YTD_FROM_CONTRACT_START: True,
    "gas_meter": "sensor.gas",
}


def test_recording_a_switch_keeps_the_old_contract_until_the_day_before() -> None:
    data = record_switch(dict(ENTRY), date(2026, 6, 15))
    [period] = previous_contracts(data)
    assert period["until"] == "2026-06-14"
    assert period[CONF_SUPPLIER] == "engie"
    assert "gas_meter" not in period
    assert data[CONF_CONTRACT_START_DATE] == "2026-06-15"
    # Counting from the new start would drop the days the old one supplied.
    assert CONF_YTD_FROM_CONTRACT_START not in data
    today = date(2026, 9, 1)
    assert periods_this_year(data, today) == [(period, date(2026, 1, 1), date(2026, 6, 14))]
    assert current_period_start(data, date(2026, 1, 1), today) == date(2026, 6, 15)


def test_an_earlier_contract_billed_from_its_start_keeps_that_start() -> None:
    """Counting the year from the contract start left January and February
    out before the switch; the switch must not bill them afterwards."""
    started = {**ENTRY, CONF_CONTRACT_START_DATE: "2026-03-01"}
    data = record_switch(started, date(2026, 7, 1))
    [period] = previous_contracts(data)
    assert periods_this_year(data, date(2026, 9, 1)) == [
        (period, date(2026, 3, 1), date(2026, 6, 30))
    ]
    # Without the option the year still starts on 1 January.
    data = record_switch({**started, CONF_YTD_FROM_CONTRACT_START: False}, date(2026, 7, 1))
    assert periods_this_year(data, date(2026, 9, 1))[0][1] == date(2026, 1, 1)


def test_a_switch_leaves_the_old_contract_answers_behind() -> None:
    """The steps after a switch set up the new contract: they must not offer
    the old one's end date or the figures typed from its contract."""
    old = {
        **ENTRY,
        CONF_CONTRACT_END_DATE: "2028-02-29",
        CONF_MANUAL_PRICE: 6.5,
        CONF_MANUAL_FEE: 80.0,
    }
    data = record_switch(old, date(2026, 6, 1))
    for key in (CONF_CONTRACT_END_DATE, CONF_MANUAL_PRICE, CONF_MANUAL_FEE):
        assert key not in data


def _flow_card() -> SupplierSnapshot:
    return engie.parse_snapshot(
        "engie_flow", "wallonia", fixture_text("engie", "G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
    )


async def _bill_earlier(data: dict[str, Any], cards: dict[str, float]) -> list[RunningCosts]:
    """The earlier contracts of ``data`` billed at the end of September 2026,
    each month's card printing the price ``cards`` names for it (0.07 when
    it names none) and 10 kWh used a day."""
    base = _flow_card()

    def card(month: str) -> SupplierSnapshot:
        return replace(base, energy=FixedRates(price=cards.get(month, 0.07)))

    async def fetch_for_month(
        session: object, contract: str, region: str, first: date
    ) -> SupplierSnapshot:
        return card(f"{first:%Y-%m}")

    stub = replace(
        engie.EXTRACTOR,
        fetch=AsyncMock(return_value=card("2026-09")),
        fetch_for_month=fetch_for_month,
        fetch_index=None,
    )
    kwh_days = {date(2026, 1, 1) + timedelta(days=day): 10.0 for day in range(273)}
    with patch.dict(providers.EXTRACTORS, {"engie": stub}):
        costs, missing = await PeriodBilling(MonthCardCache()).bill(
            AsyncMock(), data, date(2026, 9, 30), kwh_days, 5_000.0, use_archive=False
        )
    assert missing == []
    return costs


def _energy_price(costs: RunningCosts) -> float:
    return sum(bill.energy_cost for bill in costs.months) / costs.ytd_kwh


async def test_an_earlier_contract_is_billed_on_its_signing_card() -> None:
    """A fixed contract signed in October 2025 and left on 1 July is billed
    at the price it was signed at, not at what each month offered new
    signers; typed figures still win over the card."""
    fixed = {**ENTRY, CONF_CONTRACT: "engie_easy_fixed", CONF_CONTRACT_START_DATE: "2025-10-15"}
    [earlier] = await _bill_earlier(record_switch(fixed, date(2026, 7, 1)), {"2025-10": 0.10})
    assert earlier.months[-1].month == "2026-06"
    assert _energy_price(earlier) == pytest.approx(0.10)
    typed = record_switch({**fixed, CONF_MANUAL_PRICE: 9.0}, date(2026, 7, 1))
    [earlier] = await _bill_earlier(typed, {"2025-10": 0.10})
    assert _energy_price(earlier) == pytest.approx(0.09 * 1.06)


async def test_an_earlier_contract_on_the_custom_supplier_is_billed_on_its_typed_card() -> None:
    custom = {
        **_custom_data(),
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_CONTRACT: CUSTOM_CONTRACT,
        CONF_REGION: REGION_BRUSSELS,
    }
    data = record_switch(custom, date(2026, 6, 1))
    session = AsyncMock()
    kwh_days = {date(2026, 1, 1) + timedelta(days=day): 10.0 for day in range(273)}
    costs, missing = await PeriodBilling(MonthCardCache()).bill(
        session, data, date(2026, 9, 30), kwh_days, 5_000.0, use_archive=True
    )
    assert missing == []
    [earlier] = costs
    assert earlier.months[-1].month == "2026-05"
    assert _energy_price(earlier) == pytest.approx(0.075)
    # The typed card has nothing to fetch, from the supplier or the archive.
    assert session.get.call_count == 0
    # Recorded without its typed card, it is reported rather than billed at 0.
    bare = {
        key: data[CONF_PREVIOUS_CONTRACTS][0][key]
        for key in (CONF_SUPPLIER, CONF_CONTRACT, CONF_REGION, CONF_DSO, "until")
    }
    costs, missing = await PeriodBilling(MonthCardCache()).bill(
        session,
        {CONF_PREVIOUS_CONTRACTS: [bare]},
        date(2026, 9, 30),
        kwh_days,
        5_000.0,
        use_archive=True,
    )
    assert (costs, missing) == ([], ["Expert: custom figures"])


def test_a_contract_that_ended_last_year_is_not_billed_this_year() -> None:
    data = dict(ENTRY)
    data[CONF_PREVIOUS_CONTRACTS] = [{**ENTRY, "until": "2025-11-30"}]
    assert periods_this_year(data, date(2026, 9, 1)) == []
    assert current_period_start(data, date(2026, 1, 1), date(2026, 9, 1)) == date(2026, 1, 1)


def test_malformed_periods_are_left_out() -> None:
    data = {CONF_PREVIOUS_CONTRACTS: [{"until": "not a date"}, "nonsense", {"until": "2026-02-01"}]}
    assert previous_contracts(data) == []


def test_manual_rate_replaces_a_fixed_price_and_fee() -> None:
    leg = FixedRates(price=0.08, yearly_fixed_fee=60.0)
    typed = manual_leg(leg, {CONF_MANUAL_PRICE: 7.5, CONF_MANUAL_FEE: 45.0})
    assert typed == FixedRates(price=approx(0.075 * 1.06), yearly_fixed_fee=45.0)
    assert manual_leg(leg, {}) is leg


def test_manual_rate_replaces_an_indexed_formula() -> None:
    leg = IndexedRates(factor=0.001, base=0.01, index="ZTPDAM", price=0.07, yearly_fixed_fee=50.0)
    typed = manual_leg(leg, {CONF_MANUAL_FACTOR: 0.1020, CONF_MANUAL_BASE: 0.9335})
    assert isinstance(typed, IndexedRates)
    assert typed.factor == pytest.approx(0.001020 * 1.06)
    assert typed.base == pytest.approx(0.009335 * 1.06)
    assert typed.index == "ZTPDAM"
    assert typed.yearly_fixed_fee == 50.0
    # A factor without a base is not a formula.
    assert manual_leg(leg, {CONF_MANUAL_FACTOR: 0.1}) is leg


def test_the_fluvius_data_management_fee_stands_in_where_a_card_prints_none() -> None:
    snap = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_FLANDERS,
        fixture_text("engie", "G_EASY_R_GREY_C_I_12_V_F_202609.pdf"),
    )
    blank = {key: replace(overlay, metering_per_year=0.0) for key, overlay in snap.dsos.items()}
    resolved = resolve_network(blank, snap.taxes, date(2026, 9, 1))
    assert resolved[DSO_FLUVIUS_KEMPEN].metering_per_year == pytest.approx(17.85 * 1.06)
    # A card that prints it keeps its own figure.
    assert resolve_network(snap.dsos, snap.taxes, date(2026, 9, 1)) == snap.dsos
    # Outside the tariff year it is known for, the card is read as printed.
    assert resolve_network(blank, snap.taxes, date(2027, 1, 1)) == blank


def _custom_data() -> dict[str, object]:
    return {
        CONF_DSO: DSO_SIBELGA,
        CONF_CALIBER: "q10",
        CONF_CUSTOM_PRICE: 7.5,
        CONF_CUSTOM_FEE: 60.0,
        CONF_CUSTOM_T1_FIXED: 15.9,
        CONF_CUSTOM_T1_PROP: 1.99,
        CONF_CUSTOM_T2_FIXED: 43.07,
        CONF_CUSTOM_T2_PROP: 1.447,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_METERING: 24.95,
        CONF_CUSTOM_EXCISE_LOW: 1.09286,
        CONF_CUSTOM_EXCISE_HIGH: 1.18296,
        CONF_CUSTOM_LEVY: 12.59,
    }


def test_the_custom_card_prices_like_a_real_one() -> None:
    snap = build_snapshot(_custom_data())
    assert snap.energy == FixedRates(price=approx(0.075), yearly_fixed_fee=60.0)
    breakdown = compute_breakdown(snap, DSO_SIBELGA, 17_000.0, snap.energy.price)
    assert breakdown.network == pytest.approx(0.01447 + 0.00165)
    assert breakdown.taxes == pytest.approx(0.0111936, abs=1e-9)
    fixed = fixed_costs(snap, DSO_SIBELGA, 17_000.0, "q10")
    assert fixed.total == pytest.approx(60.0 + 43.07 + 24.95 + 12.59)
    assert snap.dsos[DSO_SIBELGA].tiers[TIER_T2].fixed_per_year == pytest.approx(43.07)


def test_the_custom_card_without_a_second_excise_band_is_one_rate() -> None:
    data = _custom_data()
    data.pop(CONF_CUSTOM_EXCISE_HIGH)
    data[CONF_CUSTOM_CONNECTION_FEE] = 0.0
    assert build_snapshot(data).taxes.excise_bands == ((None, pytest.approx(0.0109286)),)


def _quote(label: str, cost: float | None, provisional: bool = False) -> Quote:
    return Quote(
        "s", label, label, cost, None if cost is None else cost / 17000, 100.0, True, provisional
    )


def test_daily_ranking_saving_and_round_trip() -> None:
    quotes = [_quote("a", 1500.0), _quote("b", 1600.0), _quote("c", None)]
    ranking = DailyRanking.from_quotes(date(2026, 9, 29), quotes, ("s", "b"))
    assert ranking.own_cost == 1600.0
    assert ranking.saving == pytest.approx(100.0)
    assert len(ranking.rows) == 2
    assert DailyRanking.from_json(ranking.to_json()) == ranking
    assert DailyRanking.from_json({"day": "bad"}) is None
    assert DailyRanking.from_quotes(date(2026, 9, 29), quotes, ("s", "zz")).saving is None


def test_ranking_minute_is_stable_and_within_the_day() -> None:
    assert ranking_minute("abc") == ranking_minute("abc")
    assert 0 <= ranking_minute("abc") < 24 * 60


async def test_a_quote_bills_the_data_management_fee_a_card_leaves_out() -> None:
    """Luminus's Flemish cards print no data management fee; a quote bills the
    regulated one, as the running costs do."""
    card = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_FLANDERS,
        fixture_text("engie", "G_EASY_R_GREY_C_I_12_V_F_202609.pdf"),
    )
    card = replace(
        card,
        dsos={key: replace(overlay, metering_per_year=0.0) for key, overlay in card.dsos.items()},
    )
    extractor = replace(
        engie.EXTRACTOR, fetch_index=AsyncMock(return_value={"ZTP101": {"2026-09": 61.768}})
    )
    quote = await quote_contract(
        AsyncMock(),
        extractor,
        "engie_easy_variable",
        REGION_FLANDERS,
        Household(DSO_FLUVIUS_KEMPEN, CALIBER_Q10, 17_000.0),
        "2026-09",
        IndexCache(),
        use_archive=False,
        card=card,
    )
    # The supplier's 55, Kempen's mid tier 85,83 and the 2026 data management
    # fee, 17,85 excluding VAT.
    assert quote.fixed == pytest.approx(55.0 + 85.83 + 17.85 * 1.06)
    assert quote.all_in is not None and quote.fixed is not None
    assert quote.annual_cost == pytest.approx(17_000.0 * quote.all_in + quote.fixed)


async def test_a_card_published_as_images_is_quoted_on_the_archive_reading() -> None:
    card = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_FLANDERS,
        fixture_text("engie", "G_EASY_R_GREY_C_I_12_V_F_202609.pdf"),
    )
    extractor = replace(
        engie.EXTRACTOR,
        fetch=AsyncMock(side_effect=CardNotReadableError("card has no text layer")),
        fetch_index=None,
    )
    household = Household(DSO_FLUVIUS_KEMPEN, CALIBER_Q10, 17_000.0)
    row = AsyncMock(return_value=(card, True))
    with patch("custom_components.be_gas_prices.month_cards.fetch_archived_row", row):
        quote = await quote_contract(
            AsyncMock(),
            extractor,
            "engie_easy_variable",
            REGION_FLANDERS,
            household,
            "2026-09",
            IndexCache(),
            use_archive=True,
        )
        refused = await quote_contract(
            AsyncMock(),
            extractor,
            "engie_easy_variable",
            REGION_FLANDERS,
            household,
            "2026-09",
            IndexCache(),
            use_archive=False,
        )
    assert quote.read_by_ocr and quote.error is None and quote.annual_cost is not None
    assert row.await_count == 1
    assert "Engie Easy Variable OCR" in quote_table([quote], own=None)
    # An entry that keeps the archive out is not priced on it.
    assert refused.error is not None and refused.annual_cost is None


def test_quote_table_bolds_the_own_row_and_signs_the_gap() -> None:
    own = _quote("own", 1600.0)
    table = quote_table(
        [_quote("cheap", 1500.0, provisional=True), own, _quote("broken", None)], own=own
    )
    lines = table.splitlines()
    assert lines[2].startswith("| 1 | cheap † | 1,500.00 | -100.00 |")
    assert "**own**" in lines[3] and "+0.00" in lines[3]
    assert lines[4].startswith("| - | broken | - | - |")


def test_the_custom_card_follows_the_entry_dso_and_caliber() -> None:
    data = _custom_data()
    data[CONF_DSO] = DSO_ORES
    assert set(build_snapshot(data).dsos) == {DSO_ORES}
    # The levy typed for the smallest caliber covers both halves of it.
    assert build_snapshot(_custom_data()).taxes.osp_by_caliber == {
        "q10_le5000": 12.59,
        "q10_gt5000": 12.59,
    }
    data = _custom_data()
    data[CONF_CALIBER] = "q16"
    assert build_snapshot(data).taxes.osp_by_caliber == {"q16": 12.59}
