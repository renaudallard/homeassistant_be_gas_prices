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

"""Bolt gas card extractor, against the August and September 2026 fixed cards."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_WEST,
    DSO_ORES,
    DSO_RESA,
    DSO_SIBELGA,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    REGIONS,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.providers import bolt
from custom_components.be_gas_prices.providers._rates import FixedRates
from custom_components.be_gas_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)
from tests import fixture_page, fixture_text

# pdfplumber takes about 45 seconds to lay out one Bolt card on a Raspberry
# Pi, most of it loading the vector art, and the first test to read a card
# pays for it.
pytestmark = pytest.mark.timeout(180)

_FIX_202609 = "fix_res_ng_fr_202609.pdf"
_FIX_202608 = "fix_res_ng_fr_202608.pdf"
_PLENTY_202609 = "plenty_fix_res_ng_fr_202609.pdf"


def _card(name: str) -> str:
    return fixture_text("bolt", name, "layout")


def _fixed(snap: SupplierSnapshot) -> tuple[float, float]:
    """A fixed card's price and yearly fee."""
    assert isinstance(snap.energy, FixedRates)
    return snap.energy.price, snap.energy.yearly_fixed_fee


def _single_rate(taxes: TaxOverlay) -> float:
    """The excise of a card printing one rate for every volume."""
    [(upper, rate)] = taxes.excise_bands
    assert upper is None
    return rate


def test_bolt_fixe_price_and_monthly_fee() -> None:
    snap = bolt.parse_snapshot("bolt_fix", REGION_FLANDERS, _card(_FIX_202609))
    # "Prix mensuel 8,02" c EUR/kWh and "€ 8,99 / mois".
    assert _fixed(snap) == pytest.approx((0.0802, 8.99 * 12))
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    # "TTC", and no rate stated.
    assert snap.taxes.card_vat_rate is None
    assert snap.taxes.vat_rate == 0.0


def test_plenty_fixe_differs_by_its_fee() -> None:
    snap = bolt.parse_snapshot("bolt_plenty_fix", REGION_WALLONIA, _card(_PLENTY_202609))
    # "€ 3,99 / mois".
    assert _fixed(snap) == pytest.approx((0.0802, 3.99 * 12))


def test_card_of_the_other_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="the card is for Bolt Fixe"):
        bolt.parse_snapshot("bolt_plenty_fix", REGION_FLANDERS, _card(_FIX_202609))


def test_flanders_table_skips_the_old_fluvius_rows() -> None:
    snap = bolt.parse_snapshot("bolt_fix", REGION_FLANDERS, _card(_FIX_202609))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Antwerpen 2,257 15,68 0,905 83,22 0,1654 18,92": proportional
    # before fixed within each tier.
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.02257)
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.00905)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.transport == pytest.approx(0.001654)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    # "Fluvius-West 2,695 19,03 1,055 101,02 ...", not the leftover
    # "Fluvius (Gaselwest) - - - - 0,1654 -" row above it.
    west = snap.dsos[DSO_FLUVIUS_WEST]
    assert west.tiers[TIER_T1].proportional == pytest.approx(0.02695)
    assert west.tiers[TIER_T2].fixed_per_year == pytest.approx(101.02)


def test_wallonia_table_as_printed() -> None:
    """Bolt's ORES and RESA mid-tier terms differ from the 2026 grid (4,289,
    2,206 and 2,529); the card is read as printed."""
    snap = bolt.parse_snapshot("bolt_fix", REGION_WALLONIA, _card(_FIX_202609))
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "ORES (Namur) 4,198 31,91 2,115 140,93 0,1654 -" on all five rows.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.04198)
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.02115)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.transport == pytest.approx(0.001654)
    assert ores.metering_per_year == 0.0
    # "TECTEO RESA 4,640 34,59 2,259 122,05 0,1654 -"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T1].fixed_per_year == pytest.approx(34.59)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.02259)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)


def test_walloon_levies() -> None:
    taxes = bolt.parse_snapshot("bolt_fix", REGION_WALLONIA, _card(_FIX_202609)).taxes
    # "Accise fédérale (c€/kWh) 1,09286 1,09286 1,09286": one rate, no band.
    assert _single_rate(taxes) == pytest.approx(0.0109286)
    # "Contribution sur l'énergie (c€/kWh) 2 - - -"
    assert taxes.energy_contribution == 0.0
    # "Redevance de raccordement (c€/kWh) 3 - 0,00750 -"
    assert taxes.connection_fee == pytest.approx(0.0000750)
    assert taxes.osp_by_caliber is None


def test_brussels_row_and_levy_table() -> None:
    snap = bolt.parse_snapshot("bolt_fix", REGION_BRUSSELS, _card(_FIX_202609))
    # "SIBELGA 1,990 15,90 1,447 43,07 0,165 24,95"
    sibelga = snap.dsos[DSO_SIBELGA]
    assert sibelga.tiers[TIER_T1].proportional == pytest.approx(0.0199)
    assert sibelga.tiers[TIER_T1].fixed_per_year == pytest.approx(15.90)
    assert sibelga.tiers[TIER_T2].proportional == pytest.approx(0.01447)
    assert sibelga.tiers[TIER_T2].fixed_per_year == pytest.approx(43.07)
    assert sibelga.transport == pytest.approx(0.00165)
    assert sibelga.metering_per_year == pytest.approx(24.95)
    assert snap.taxes.connection_fee == 0.0
    # Eight rows, none above 160 m3/h; 12,54 where the ordinance's table and
    # the other cards say 12,59.
    assert snap.taxes.osp_by_caliber == pytest.approx(
        {
            "q10_le5000": 3.56,
            "q10_gt5000": 12.54,
            "q16": 30.40,
            "q25": 75.18,
            "q40": 150.35,
            "q65": 376.00,
            "q100": 522.79,
            "q160": 671.36,
        }
    )


def test_august_card_spells_its_month_aout_and_prints_the_old_levies() -> None:
    snap = bolt.parse_snapshot("bolt_fix", REGION_FLANDERS, _card(_FIX_202608))
    assert snap.publication_label == "2026-08"
    assert snap.valid_until == date(2026, 8, 31)
    assert _fixed(snap) == pytest.approx((0.0731, 8.99 * 12))
    # Still the rates before 1 August: excise 0,8724, contribution 0,1058.
    assert _single_rate(snap.taxes) == pytest.approx(0.008724)
    assert snap.taxes.energy_contribution == pytest.approx(0.001058)


def test_levy_row_missing_a_value_fails_loud() -> None:
    """The footnote digit must not slide into the Flanders column."""
    text = _card(_FIX_202609).replace(
        "Contribution sur l'énergie (c€/kWh) 2\n -\n -\n -",
        "Contribution sur l'énergie (c€/kWh) 2\n -\n -",
    )
    with pytest.raises(ExtractorError, match="Contribution"):
        bolt.parse_snapshot("bolt_fix", REGION_FLANDERS, text)


def test_only_the_fixed_products_are_registered() -> None:
    contracts = {c.id: c for c in bolt.EXTRACTOR.contracts}
    assert set(contracts) == {"bolt_fix", "bolt_plenty_fix"}
    assert {c.kind for c in contracts.values()} == {"fixed"}
    assert {c.regions for c in contracts.values()} == {frozenset(REGIONS)}
    assert bolt.EXTRACTOR.fetch_index is None


async def test_fetch_takes_the_card_the_listing_links() -> None:
    text = _card(_PLENTY_202609)
    with (
        patch.object(
            bolt, "fetch_text", AsyncMock(return_value=fixture_page("bolt", "listing_fr.html"))
        ),
        patch.object(bolt, "fetch_pdf_text_layout", AsyncMock(return_value=text)) as fetched,
    ):
        snap = await bolt.fetch(AsyncMock(), "bolt_plenty_fix", REGION_BRUSSELS)
    url = "https://files.boltenergie.be/pricelists/fix/plenty_fix_res_ng_fr_202609.pdf"
    assert fetched.call_args.args[1] == url
    assert snap.source_url == url


async def test_probe_heads_the_linked_card() -> None:
    with (
        patch.object(
            bolt, "fetch_text", AsyncMock(return_value=fixture_page("bolt", "listing_fr.html"))
        ),
        patch.object(
            bolt, "head_freshness_key", AsyncMock(return_value="Mon, 14 Sep 2026")
        ) as head,
    ):
        assert await bolt.probe(AsyncMock(), "bolt_fix", REGION_FLANDERS) == "Mon, 14 Sep 2026"
    assert head.call_args.args[1] == (
        "https://files.boltenergie.be/pricelists/fix/fix_res_ng_fr_202609.pdf"
    )


async def test_fetch_for_month_builds_the_month_url() -> None:
    with patch.object(
        bolt, "fetch_pdf_text_layout", AsyncMock(return_value=_card(_FIX_202608))
    ) as fetched:
        snap = await bolt.fetch_for_month(
            AsyncMock(), "bolt_fix", REGION_WALLONIA, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.publication_label == "2026-08"
    assert fetched.call_args.args[1] == (
        "https://files.boltenergie.be/pricelists/fix/fix_res_ng_fr_202608.pdf"
    )


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with patch.object(bolt, "fetch_pdf_text_layout", AsyncMock(return_value=_card(_FIX_202608))):
        assert (
            await bolt.fetch_for_month(AsyncMock(), "bolt_fix", REGION_WALLONIA, date(2026, 7, 1))
            is None
        )


async def test_fetch_for_month_without_a_card_is_none() -> None:
    with patch.object(
        bolt,
        "fetch_pdf_text_layout",
        AsyncMock(side_effect=ExtractorError("HTTP 404 fetching x")),
    ):
        assert (
            await bolt.fetch_for_month(AsyncMock(), "bolt_fix", REGION_FLANDERS, date(2026, 10, 1))
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            bolt,
            "fetch_pdf_text_layout",
            AsyncMock(side_effect=ExtractorError("network error fetching x: TimeoutError")),
        ),
        pytest.raises(ExtractorError),
    ):
        await bolt.fetch_for_month(AsyncMock(), "bolt_fix", REGION_FLANDERS, date(2026, 8, 1))
