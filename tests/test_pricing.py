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

"""The pure pricing engine and the per-household resolution it relies on."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from custom_components.be_gas_prices.const import (
    CALIBER_GT160,
    CALIBER_Q10,
    CALIBER_Q16,
    DSO_FLUVIUS_KEMPEN,
    DSO_ORES,
    DSO_SIBELGA,
    FLUVIUS_DATA_MANAGEMENT_HTVA,
    REGION_BRUSSELS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.pricing import (
    PricingError,
    compute_breakdown,
    energy_price,
    fixed_costs,
)
from custom_components.be_gas_prices.providers import engie
from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._resolve import (
    brussels_levy,
    effective_excise,
    resolve_connection_fee,
    resolve_federal_levies,
    resolve_network,
    tier_for,
)
from custom_components.be_gas_prices.providers.base import DsoOverlay, SupplierSnapshot, TaxOverlay
from tests import fixture_text


@pytest.mark.parametrize(
    ("kwh", "tier"),
    [
        (0.0, TIER_T1),
        (5_000.0, TIER_T1),
        (5_000.5, TIER_T2),
        (150_000.0, TIER_T2),
        (150_001.0, TIER_T3),
        (5_000_000.0, TIER_T3),
    ],
)
def test_tier_for(kwh: float, tier: str) -> None:
    assert tier_for(kwh) == tier


def test_excise_is_billed_per_slice() -> None:
    """CWaPE prints 1,119360 c EUR/kWh as the August to December 2026 excise
    for its 17 000 kWh reference household, which is exactly the slice
    average: 12 000 kWh at 1,09286 and 5 000 at 1,18296."""
    bands = excise_bands(0.0109286, 0.0118296)
    assert effective_excise(bands, 17_000.0) == pytest.approx(0.0111936, abs=1e-9)
    assert effective_excise(bands, 12_000.0) == pytest.approx(0.0109286)
    assert effective_excise(bands, 8_000.0) == pytest.approx(0.0109286)
    assert effective_excise(bands, 0.0) == pytest.approx(0.0109286)


def test_single_rate_card_is_one_open_band() -> None:
    bands = excise_bands(0.0109286, None)
    assert bands == ((None, 0.0109286),)
    assert effective_excise(bands, 30_000.0) == pytest.approx(0.0109286)


def _stale_taxes() -> TaxOverlay:
    # The pre-August 2026 scheme four suppliers still printed in September.
    return TaxOverlay(
        excise_bands=excise_bands(0.008724, 0.009864),
        energy_contribution=0.001058,
    )


def test_law_replaces_a_stale_card_from_august_2026() -> None:
    resolved = resolve_federal_levies(_stale_taxes(), date(2026, 9, 1))
    assert resolved.excise_bands == (
        (12_000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    assert resolved.energy_contribution == 0.0


@pytest.mark.parametrize(
    ("stated", "rate"),
    [(None, 0.06), (0.06, 0.06), (0.21, 0.21), (0.12, 0.12), (0.6, 0.06), (0.0, 0.06)],
)
def test_law_figures_carry_the_vat_the_card_states(stated: float | None, rate: float) -> None:
    """A card stating 21 % puts the law's figures on 21 % as it prints its
    own; a stated rate Belgium does not levy is a misread, and 6 % stands."""
    taxes = replace(_stale_taxes(), card_vat_rate=stated)
    july = resolve_federal_levies(taxes, date(2026, 7, 1))
    assert july.excise_bands[0][1] == pytest.approx(0.0087238 / 1.06 * (1 + rate))
    assert july.energy_contribution == pytest.approx(0.001057668 / 1.06 * (1 + rate))
    dsos = {"fluvius_imewo": DsoOverlay(tiers={}, transport=0.0, metering_per_year=0.0)}
    [fee] = (o.metering_per_year for o in resolve_network(dsos, taxes, date(2026, 7, 1)).values())
    assert fee == pytest.approx(FLUVIUS_DATA_MANAGEMENT_HTVA * (1 + rate))


def test_law_bills_a_month_before_august_on_its_own_rates() -> None:
    """July 2026: the quarterly high slice of art. 420 and the energy
    contribution, not what a card of another month prints."""
    resolved = resolve_federal_levies(_stale_taxes(), date(2026, 7, 1))
    assert resolved.excise_bands == (
        (12_000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.00989139)),
    )
    assert resolved.energy_contribution == pytest.approx(0.001057668)


def test_law_takes_each_quarter_s_excise() -> None:
    march = resolve_federal_levies(_stale_taxes(), date(2026, 3, 1))
    april = resolve_federal_levies(_stale_taxes(), date(2026, 4, 1))
    assert march.excise_bands[1][1] == pytest.approx(0.0090782 * 1.06)
    assert april.excise_bands[1][1] == pytest.approx(0.0093061 * 1.06)
    assert march.excise_bands[0][1] == april.excise_bands[0][1] == pytest.approx(0.0087238)


def test_law_leaves_a_month_before_the_window_as_printed() -> None:
    assert resolve_federal_levies(_stale_taxes(), date(2024, 12, 1)) == _stale_taxes()


def test_law_expires_into_the_card_in_january_2027() -> None:
    """The law steps the excise again on 1 January 2027 and the quarterly
    adjustment still applies, so no rate is assumed past the window. The
    energy contribution stays abolished."""
    resolved = resolve_federal_levies(_stale_taxes(), date(2027, 1, 1))
    assert resolved.excise_bands == _stale_taxes().excise_bands
    assert resolved.energy_contribution == 0.0


def test_law_does_not_touch_a_professional_card() -> None:
    pro = replace(_stale_taxes(), vat_rate=0.21)
    assert resolve_federal_levies(pro, date(2026, 9, 1)) == pro


def test_the_connection_fee_follows_the_law_on_a_walloon_card_only() -> None:
    card = _flow_wallonia()
    printed = replace(card.taxes, connection_fee=0.00075)
    september = date(2026, 9, 1)
    assert resolve_connection_fee(printed, card.dsos, september).connection_fee == pytest.approx(
        0.000075
    )
    flemish = {DSO_FLUVIUS_KEMPEN: card.dsos[DSO_ORES]}
    assert resolve_connection_fee(printed, flemish, september) == printed
    pro = replace(printed, vat_rate=0.21)
    assert resolve_connection_fee(pro, card.dsos, september) == pro
    assert resolve_connection_fee(printed, card.dsos, date(2027, 1, 1)) == printed


def test_brussels_levy_by_caliber_and_volume() -> None:
    osp = {"q10_le5000": 3.56, "q10_gt5000": 12.59, "q16": 30.40}
    assert brussels_levy(osp, CALIBER_Q10, 5_000.0) == pytest.approx(3.56)
    assert brussels_levy(osp, CALIBER_Q10, 17_000.0) == pytest.approx(12.59)
    assert brussels_levy(osp, CALIBER_Q16, 17_000.0) == pytest.approx(30.40)
    assert brussels_levy(None, CALIBER_Q10, 17_000.0) == 0.0
    # A caliber the card does not print is not a caliber that pays nothing.
    assert brussels_levy(osp, CALIBER_GT160, 17_000.0) is None


def test_a_caliber_the_card_leaves_out_cannot_be_priced() -> None:
    card = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_BRUSSELS,
        fixture_text("engie", "G_EASY_R_GREY_C_I_36_B_F_202609.pdf"),
    )
    osp = dict(card.taxes.osp_by_caliber or {})
    del osp[CALIBER_GT160]
    card = replace(card, taxes=replace(card.taxes, osp_by_caliber=osp))
    with pytest.raises(PricingError, match="Brussels levy"):
        fixed_costs(card, DSO_SIBELGA, 17_000.0, CALIBER_GT160)


def _flow_wallonia() -> SupplierSnapshot:
    return engie.parse_snapshot(
        "engie_flow", REGION_WALLONIA, fixture_text("engie", "G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
    )


def test_energy_price_uses_the_index_when_it_is_known() -> None:
    energy = _flow_wallonia().energy
    assert energy_price(energy, None) == pytest.approx(0.07643)
    assert energy_price(energy, 70.0) == pytest.approx((0.009335 + 0.00102 * 70.0) * 1.06)


def test_breakdown_on_an_engie_walloon_card() -> None:
    """17 000 kWh sits in T2: every kWh pays ORES's T2 proportional term."""
    snap = _flow_wallonia()
    breakdown = compute_breakdown(snap, DSO_ORES, 17_000.0, 0.07643)
    assert breakdown.energy == pytest.approx(0.07643)
    assert breakdown.network == pytest.approx(0.02206 + 0.00165)
    # Slice-averaged excise plus the VAT-exempt connection fee.
    assert breakdown.taxes == pytest.approx(0.0111936 + 0.000075, abs=1e-9)
    assert breakdown.all_in == pytest.approx(breakdown.energy + breakdown.network + breakdown.taxes)


def test_fixed_costs_on_an_engie_brussels_card() -> None:
    snap = engie.parse_snapshot(
        "engie_easy_variable",
        REGION_BRUSSELS,
        fixture_text("engie", "G_EASY_R_GREY_C_I_36_B_F_202609.pdf"),
    )
    costs = fixed_costs(snap, DSO_SIBELGA, 17_000.0, CALIBER_Q10)
    assert costs.supplier == pytest.approx(55.0)
    assert costs.distribution == pytest.approx(43.07)
    assert costs.metering == pytest.approx(24.95)
    assert costs.levy == pytest.approx(12.59)
    assert costs.total == pytest.approx(55.0 + 43.07 + 24.95 + 12.59)
    small = fixed_costs(snap, DSO_SIBELGA, 4_000.0, CALIBER_Q10)
    assert small.distribution == pytest.approx(15.90)
    assert small.levy == pytest.approx(3.56)


def test_ores_bills_no_metering_and_wallonia_no_levy() -> None:
    fixed = fixed_costs(_flow_wallonia(), DSO_ORES, 17_000.0, CALIBER_Q10)
    assert fixed.metering == 0.0
    assert fixed.levy == 0.0


def test_a_dso_the_card_does_not_list_cannot_be_priced() -> None:
    with pytest.raises(PricingError):
        compute_breakdown(_flow_wallonia(), DSO_SIBELGA, 17_000.0, 0.07643)
