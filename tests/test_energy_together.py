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

"""Energy Together template and its six brands, against the September 2026 cards."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_MIDDEN_VLAANDEREN,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.pricing import PricingError, compute_breakdown
from custom_components.be_gas_prices.providers import energy_together as et
from custom_components.be_gas_prices.providers._energy_together import last_known_index
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError, SupplierSnapshot
from tests import fixture_page, fixture_text

VAT = 1.06

# (fixture folder, card, contract, "**" price in c EUR/kWh excluding VAT, fee
# in EUR/year, formula factor, formula base in EUR/MWh), as each card prints
# them.
CARDS = [
    ("hoa_energy", "Tariefkaart_NOVA_NG.pdf", "hoa_energy_nova", 7.67, 96.0, 1.0, 15.0),
    ("hoa_energy", "Tariefkaart_PRIME_NG.pdf", "hoa_energy_prime", 8.17, 96.0, 1.0, 20.0),
    (
        "hoa_energy",
        "Tariefkaart_APEX Online_NG.pdf",
        "hoa_energy_apex_online",
        6.57,
        30.0,
        1.0,
        4.0,
    ),
    ("servolt", "Tariefkaart_Control_NG.pdf", "servolt_control", 7.17, 96.0, 1.0, 10.0),
    ("servolt", "Tariefkaart_Comfort_NG.pdf", "servolt_comfort", 7.67, 96.0, 1.0, 15.0),
    ("servolt", "Tariefkaart_Servolt Solar_NG.pdf", "servolt_solar", 6.97, 35.0, 1.0, 8.0),
    ("evident", "Tariefkaart_Flexi_Gas.pdf", "evident_flexi", 7.47, 60.0, 1.0, 13.0),
    ("power2you", "Tariefkaart_Variabel_Gas.pdf", "power2you_variabel", 6.87, 60.0, 1.0, 7.0),
    ("power2you", "Tariefkaart_Flex_Gas.pdf", "power2you_flex", 7.48, 96.0, 1.05, 10.0),
    ("prijspunten", "Marktflex_NG.pdf", "prijspunten_marktflex", 7.37, 120.0, 1.0, 12.0),
    (
        "smappee_smiles",
        "Tariefkaart_VariabelSmiles_Gas.pdf",
        "smappee_smiles_variabel",
        6.87,
        60.0,
        1.0,
        7.0,
    ),
]

# The Flemish regulator's 2026 figures, VAT inclusive (T1 fixed, T1 c EUR/kWh,
# T2 fixed, T2 c EUR/kWh), which the card's rows carry in this order whatever
# their labels say.
REGULATOR = {
    DSO_FLUVIUS_ANTWERPEN: (15.68, 2.25641, 83.22, 0.90569),
    DSO_FLUVIUS_HALLE_VILVOORDE: (17.59, 2.49578, 93.38, 0.98026),
    DSO_FLUVIUS_IMEWO: (18.61, 2.62774, 98.81, 1.02399),
    DSO_FLUVIUS_KEMPEN: (16.17, 2.27489, 85.83, 0.88186),
    DSO_FLUVIUS_LIMBURG: (14.59, 2.24027, 77.46, 0.98293),
    DSO_FLUVIUS_MIDDEN_VLAANDEREN: (16.33, 2.31716, 86.71, 0.90988),
    DSO_FLUVIUS_WEST: (19.03, 2.69459, 101.02, 1.05500),
    DSO_FLUVIUS_ZENNE_DIJLE: (18.40, 2.60252, 97.72, 1.01649),
}


def _parse(folder: str, name: str, contract: str) -> SupplierSnapshot:
    return et.parse_snapshot(contract, REGION_FLANDERS, fixture_text(folder, name), "url")


@pytest.mark.parametrize(("folder", "name", "contract", "price", "fee", "factor", "base"), CARDS)
def test_energy_leg_is_the_formula_at_the_last_known_month(
    folder: str, name: str, contract: str, price: float, fee: float, factor: float, base: float
) -> None:
    """The "**" figure is the formula at the index the footnote names
    ("TTF-DAM 8/2026: €61,72903/MWh"), excluding VAT, grossed up at 6%."""
    text = fixture_text(folder, name)
    snap = et.parse_snapshot(contract, REGION_FLANDERS, text, "url")
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF_RLP"
    assert not energy.settled
    assert energy.period == "month"
    assert energy.price == pytest.approx(price / 100.0 * VAT)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    assert energy.factor == pytest.approx(factor / 1000.0 * VAT)
    assert energy.base == pytest.approx(base / 1000.0 * VAT)
    stated = last_known_index(text)
    assert stated is not None
    assert stated == (date(2026, 8, 1), pytest.approx(61.72903))
    # The card rounds the "**" price to 0,01 c EUR/kWh.
    assert energy.at(stated[1]) == pytest.approx(energy.price, abs=0.00005 * VAT)
    # The fixture folders are named after the brands' supplier ids.
    assert snap.supplier == folder
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)


@pytest.mark.parametrize(("folder", "name", "contract"), [c[:3] for c in CARDS])
def test_rows_carry_the_alphabetical_areas_figures(folder: str, name: str, contract: str) -> None:
    """Every row matches the regulator's figures for the area its POSITION
    names, to the card's two decimals, while its label names another area.
    The fixed terms alone already tell the eight areas apart."""
    snap = _parse(folder, name, contract)
    assert set(snap.dsos) == FLUVIUS_KEYS
    for dso, (t1_fixed, t1_prop, t2_fixed, t2_prop) in REGULATOR.items():
        overlay = snap.dsos[dso]
        assert overlay.tiers[TIER_T1].fixed_per_year == pytest.approx(t1_fixed)
        assert overlay.tiers[TIER_T1].proportional * 100 == pytest.approx(t1_prop, abs=0.006)
        assert overlay.tiers[TIER_T2].fixed_per_year == pytest.approx(t2_fixed)
        assert overlay.tiers[TIER_T2].proportional * 100 == pytest.approx(t2_prop, abs=0.006)
        assert set(overlay.tiers) == {TIER_T1, TIER_T2}
        # "18,92 0,165": databeheer and transport, once for every area.
        assert overlay.metering_per_year == pytest.approx(18.92)
        assert overlay.transport == pytest.approx(0.00165)


def test_the_label_limburg_carries_halle_vilvoorde() -> None:
    """HOA Nova prints "FLUVIUS LIMBURG 2,50 17,59 0,98 93,38", which Engie
    prints as "FLUVIUS HALLE-VILVOORDE 17,59 2,496 93,38 0,981", and
    "FLUVIUS MIDDEN-VLAANDEREN 2,24 14,59 0,98 77,46", Limburg's."""
    snap = _parse("hoa_energy", "Tariefkaart_NOVA_NG.pdf", "hoa_energy_nova")
    hv = snap.dsos[DSO_FLUVIUS_HALLE_VILVOORDE]
    assert hv.tiers[TIER_T1].proportional == pytest.approx(0.0250)
    assert hv.tiers[TIER_T1].fixed_per_year == pytest.approx(17.59)
    assert hv.tiers[TIER_T2].proportional == pytest.approx(0.0098)
    assert hv.tiers[TIER_T2].fixed_per_year == pytest.approx(93.38)
    limburg = snap.dsos[DSO_FLUVIUS_LIMBURG]
    assert limburg.tiers[TIER_T1].fixed_per_year == pytest.approx(14.59)
    assert limburg.tiers[TIER_T2].fixed_per_year == pytest.approx(77.46)


def test_power2you_prints_three_decimals() -> None:
    snap = _parse("power2you", "Tariefkaart_Flex_Gas.pdf", "power2you_flex")
    # "FLUVIUS MIDDEN-VLAANDEREN 2,24 14,59 0,983 77,46" is Limburg's.
    assert snap.dsos[DSO_FLUVIUS_LIMBURG].tiers[TIER_T2].proportional == pytest.approx(0.00983)
    # "FLUVIUS HALLE-VILVOORDE 2,60 18,4 1,016 97,72" is Zenne-Dijle's.
    zd = snap.dsos[DSO_FLUVIUS_ZENNE_DIJLE]
    assert zd.tiers[TIER_T1].fixed_per_year == pytest.approx(18.40)
    assert zd.tiers[TIER_T2].proportional == pytest.approx(0.01016)


def test_levies_are_read_as_printed() -> None:
    """The template still prints the pre-August 2026 excise and energy
    contribution; the law override corrects the delivery month later."""
    taxes = _parse("hoa_energy", "Tariefkaart_NOVA_NG.pdf", "hoa_energy_nova").taxes
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.008724)),
        (None, pytest.approx(0.009457)),
    )
    assert taxes.energy_contribution == pytest.approx(0.001057)
    assert taxes.connection_fee == 0.0
    assert taxes.osp_by_caliber is None
    assert taxes.vat_rate == 0.0
    # "Alle prijzen zijn inclusief BTW": no rate is stated.
    assert taxes.card_vat_rate is None


def test_august_card_and_evident_typo() -> None:
    """Evident's August card names the index month "7/2036"; the card is
    still dated by its title and priced on its "**" figure."""
    snap = _parse("evident", "Tariefkaart_Flexi_Gas_2026-08.pdf", "evident_flexi")
    assert snap.publication_label == "2026-08"
    assert snap.valid_until == date(2026, 8, 31)
    assert snap.energy.price == pytest.approx(0.0661 * VAT)
    text = fixture_text("evident", "Tariefkaart_Flexi_Gas_2026-08.pdf")
    assert last_known_index(text) == (date(2036, 7, 1), pytest.approx(53.07468))
    nova = _parse("hoa_energy", "Tariefkaart_NOVA_NG_2026-08.pdf", "hoa_energy_nova")
    assert nova.publication_label == "2026-08"
    assert isinstance(nova.energy, IndexedRates)
    assert nova.energy.at(53.07468) == pytest.approx(0.0681 * VAT, abs=0.00005 * VAT)


def test_card_of_another_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="not 'NOVA'"):
        _parse("hoa_energy", "Tariefkaart_PRIME_NG.pdf", "hoa_energy_nova")
    # Evident prints no product, so a named card is not Evident's.
    with pytest.raises(ExtractorError):
        et.parse_snapshot(
            "evident_flexi",
            REGION_FLANDERS,
            fixture_text("smappee_smiles", "Tariefkaart_VariabelSmiles_Gas.pdf"),
            "url",
        )


def test_relabelled_rows_are_refused() -> None:
    """Were the template's labels corrected, position would no longer say
    whose figures a row carries, so the card is refused rather than mapped."""
    text = fixture_text("hoa_energy", "Tariefkaart_NOVA_NG.pdf").replace(
        "FLUVIUS LIMBURG 2,50", "FLUVIUS HALLE-VILVOORDE 2,50"
    )
    with pytest.raises(ExtractorError, match="order"):
        et.parse_snapshot("hoa_energy_nova", REGION_FLANDERS, text, "url")


def test_the_2025_broken_row_loses_its_mid_tier() -> None:
    """The 2025 cards print "FLUVIUS LIMBURG 2,09 13,32 70,72 70,72": read by
    position that would bill Halle-Vilvoorde's mid tier 70,72 c EUR/kWh."""
    snap = _parse("servolt", "Tariefkaart_Control_NG_2025-12.pdf", "servolt_control")
    tiers = snap.dsos[DSO_FLUVIUS_HALLE_VILVOORDE].tiers
    assert list(tiers) == [TIER_T1]
    assert tiers[TIER_T1].fixed_per_year == pytest.approx(13.32)
    assert tiers[TIER_T1].proportional == pytest.approx(0.0209)
    assert all(
        TIER_T2 in snap.dsos[key].tiers for key in FLUVIUS_KEYS - {DSO_FLUVIUS_HALLE_VILVOORDE}
    )
    compute_breakdown(snap, DSO_FLUVIUS_HALLE_VILVOORDE, 4_000.0, 0.03)
    with pytest.raises(PricingError, match="T2"):
        compute_breakdown(snap, DSO_FLUVIUS_HALLE_VILVOORDE, 17_000.0, 0.03)


def test_card_that_lost_its_formula_fails_loud() -> None:
    text = fixture_text("hoa_energy", "Tariefkaart_NOVA_NG.pdf").replace("TTF_RLP x 1", "TTF x 1")
    with pytest.raises(ExtractorError, match="formula"):
        et.parse_snapshot("hoa_energy_nova", REGION_FLANDERS, text, "url")


def test_only_flanders_and_known_contracts() -> None:
    text = fixture_text("hoa_energy", "Tariefkaart_NOVA_NG.pdf")
    with pytest.raises(ExtractorError):
        et.parse_snapshot("hoa_energy_nova", REGION_WALLONIA, text, "url")
    with pytest.raises(ExtractorError):
        et.parse_snapshot("hoa_energy_volt", REGION_FLANDERS, text, "url")


@pytest.mark.parametrize(
    ("folder", "contract", "card_id"),
    [
        # Nova's own group, not Volt's, which links a byte-identical NOVA_NG.
        ("hoa_energy", "hoa_energy_nova", 1757),
        ("hoa_energy", "hoa_energy_prime", 1759),
        ("hoa_energy", "hoa_energy_apex_online", 1761),
        ("servolt", "servolt_control", 1811),
        ("servolt", "servolt_solar", 1815),
        ("evident", "evident_flexi", 1747),
        ("power2you", "power2you_variabel", 1775),
        ("power2you", "power2you_flex", 1777),
        ("prijspunten", "prijspunten_marktflex", 1787),
        ("smappee_smiles", "smappee_smiles_variabel", 1807),
    ],
)
def test_current_card_is_the_groups_gas_link(folder: str, contract: str, card_id: int) -> None:
    page = fixture_page(folder, "products.html")
    path = et.current_card_path(page, et._CONTRACTS_BY_ID[contract])
    assert path == f"/web/content/xx.product.group.tariff.card/{card_id}/file"


def test_group_missing_from_the_listing_fails_loud() -> None:
    with pytest.raises(ExtractorError, match="not listed"):
        et.current_card_path(
            fixture_page("hoa_energy", "products.html"), et._CONTRACTS_BY_ID["servolt_control"]
        )


def test_archive_is_keyed_on_the_group_through_name_drift() -> None:
    history = fixture_page("power2you", "products_history.html")
    variabel = et._CONTRACTS_BY_ID["power2you_variabel"]
    # "Tariefkaart_B2C_VAR_NG", mei 2024.
    assert et.archived_card_path(history, variabel, date(2024, 5, 1)) == (
        "/web/content/xx.product.group.tariff.card/3/file"
    )
    # Two rows for juni 2024 (54 B2C_VAR_GAS above 36 B2C_VAR_NG): the later upload.
    assert et.archived_card_path(history, variabel, date(2024, 6, 1)) == (
        "/web/content/xx.product.group.tariff.card/54/file"
    )
    assert et.archived_card_path(history, variabel, date(2024, 12, 1)) is None


async def test_fetch_reads_the_listed_card() -> None:
    text = fixture_text("hoa_energy", "Tariefkaart_NOVA_NG.pdf")
    with (
        patch.object(
            et, "fetch_text", AsyncMock(return_value=fixture_page("hoa_energy", "products.html"))
        ) as listing,
        patch.object(et, "fetch_pdf_text", AsyncMock(return_value=text)) as card,
    ):
        snap = await et.fetch(AsyncMock(), "hoa_energy_nova", REGION_FLANDERS)
    assert listing.call_args.args[1] == "https://www.hoa.energy/products"
    url = "https://www.hoa.energy/web/content/xx.product.group.tariff.card/1757/file"
    assert card.call_args.args[1] == url
    assert snap.source_url == url
    assert snap.supplier == "hoa_energy"


async def test_fetch_for_month_reads_the_archive() -> None:
    history = fixture_page("hoa_energy", "products_history.html")
    text = fixture_text("hoa_energy", "Tariefkaart_NOVA_NG_2026-08.pdf")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(et, "fetch_text", AsyncMock(return_value=history)) as listing,
        patch.object(et, "fetch_pdf_text", AsyncMock(return_value=text)) as card,
    ):
        snap = await et.fetch_for_month(
            AsyncMock(), "hoa_energy_nova", REGION_FLANDERS, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert listing.call_args.args[1] == "https://www.hoa.energy/products/history"
    assert card.call_args.args[1] == (
        "https://www.hoa.energy/web/content/xx.product.group.tariff.card/1671/file"
    )


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    history = fixture_page("hoa_energy", "products_history.html")
    text = fixture_text("hoa_energy", "Tariefkaart_NOVA_NG.pdf")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(et, "fetch_text", AsyncMock(return_value=history)),
        patch.object(et, "fetch_pdf_text", AsyncMock(return_value=text)),
    ):
        assert (
            await et.fetch_for_month(
                AsyncMock(), "hoa_energy_nova", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_before_the_archive_is_none() -> None:
    """HOA's archive starts in February 2026."""
    history = fixture_page("hoa_energy", "products_history.html")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(et, "fetch_text", AsyncMock(return_value=history)),
        patch.object(et, "fetch_pdf_text", AsyncMock()) as card,
    ):
        assert (
            await et.fetch_for_month(
                AsyncMock(), "hoa_energy_nova", REGION_FLANDERS, date(2026, 1, 1)
            )
            is None
        )
    card.assert_not_called()


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            et, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await et.fetch_for_month(AsyncMock(), "servolt_control", REGION_FLANDERS, date(2026, 8, 1))


async def test_fetch_for_month_does_not_ask_for_the_future() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(et, "fetch_text", AsyncMock()) as listing,
    ):
        assert (
            await et.fetch_for_month(
                AsyncMock(), "servolt_control", REGION_FLANDERS, date(2026, 10, 1)
            )
            is None
        )
    listing.assert_not_called()


def test_index_publication_in_eur_per_mwh() -> None:
    """ "TTF_RLP (c€/kWh)", one column per year: August 2026 is 6,17."""
    table = et.parse_index_publication(fixture_text("hoa_energy", "Indexatieparameters.pdf"))
    ttf = table["TTF_RLP"]
    assert ttf["2026-08"] == pytest.approx(61.7)
    assert ttf["2026-07"] == pytest.approx(53.1)
    assert ttf["2026-01"] == pytest.approx(34.0)
    assert ttf["2025-12"] == pytest.approx(27.6)
    assert ttf["2025-09"] == pytest.approx(32.0)
    # September 2026 is only known once the month is over.
    assert "2026-09" not in ttf
    assert len(ttf) == 20


async def test_index_falls_back_to_another_brands_page() -> None:
    """Smappee's page links /web/content/985787, which answers 404; the other
    brands link the platform's publication."""
    pages = {
        "https://www.smappeesmiles.be/indexatieparameters": fixture_page(
            "smappee_smiles", "indexatieparameters.html"
        ),
        "https://www.hoa.energy/indexatieparameters": fixture_page(
            "hoa_energy", "indexatieparameters.html"
        ),
    }
    publication = fixture_text("hoa_energy", "Indexatieparameters.pdf")

    async def pdf(_session: object, url: str) -> str:
        if url == "https://www.hoa.energy/web/content/1023433":
            return publication
        raise ExtractorError(f"HTTP 404 fetching {url}")

    async def page(_session: object, url: str) -> str:
        return pages[url]

    smappee = next(e for e in et.EXTRACTORS if e.id == "smappee_smiles")
    assert smappee.fetch_index is not None
    with (
        patch.object(et, "fetch_text", AsyncMock(side_effect=page)) as listing,
        patch.object(et, "fetch_pdf_text", AsyncMock(side_effect=pdf)) as card,
    ):
        table = await smappee.fetch_index(AsyncMock())
    assert table["TTF_RLP"]["2026-08"] == pytest.approx(61.7)
    assert [c.args[1] for c in listing.call_args_list] == list(pages)
    assert card.call_args_list[0].args[1] == "https://www.smappeesmiles.be/web/content/985787"


def test_one_extractor_per_brand() -> None:
    by_id = {e.id: e for e in et.EXTRACTORS}
    assert list(by_id) == [
        "hoa_energy",
        "servolt",
        "evident",
        "power2you",
        "prijspunten",
        "smappee_smiles",
    ]
    assert [c.id for c in by_id["hoa_energy"].contracts] == [
        "hoa_energy_nova",
        "hoa_energy_prime",
        "hoa_energy_apex_online",
    ]
    for extractor in et.EXTRACTORS:
        assert extractor.regions() == frozenset({REGION_FLANDERS})
        assert extractor.fetch_for_month is not None
        assert all(c.kind == "indexed" for c in extractor.contracts)


async def test_index_failing_everywhere_raises_a_transient_error() -> None:
    """A brand's own dead link must not hide that the others were down."""
    hoa = next(e for e in et.EXTRACTORS if e.id == "hoa_energy")
    assert hoa.fetch_index is not None

    async def page(_session: object, url: str) -> str:
        if url == "https://www.hoa.energy/indexatieparameters":
            raise ExtractorError(f"HTTP 404 fetching {url}")
        raise ExtractorError(f"HTTP 503 fetching {url}")

    with (
        patch.object(et, "fetch_text", AsyncMock(side_effect=page)),
        pytest.raises(ExtractorError, match=r"^HTTP 503"),
    ):
        await hoa.fetch_index(AsyncMock())
