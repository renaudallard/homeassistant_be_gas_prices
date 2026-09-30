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

"""Mega gas card extractor, against the September 2026 cards."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_KEMPEN,
    DSO_ORES,
    DSO_RESA,
    DSO_SIBELGA,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.providers import mega
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import DsoOverlay, DsoTier, ExtractorError
from tests import approx, fixture_page, fixture_text

_CDN = "https://my.mega.be/resources/tarif/"


def _card(name: str) -> str:
    return fixture_text("mega", f"Mega-FR-NG-B2C-{name}.pdf")


def _index() -> dict[str, dict[str, float]]:
    return mega.parse_index_page(fixture_page("mega", "indexation-de-nos-produits-variables.html"))


def _overlay(prop: tuple[float, ...], fixed: tuple[float, ...], metering: float) -> DsoOverlay:
    """A DSO row as the card prints it: c EUR/kWh, EUR/year, EUR/year."""
    return DsoOverlay(
        tiers={
            tier: DsoTier(fixed_per_year=approx(f), proportional=approx(p / 100))
            for tier, p, f in zip((TIER_T1, TIER_T2, TIER_T3), prop, fixed, strict=True)
        },
        transport=approx(0.00165),
        metering_per_year=approx(metering),
    )


def test_smart_flex_is_priced_at_the_last_known_ztp() -> None:
    """The headline 8.34 is a twelve-month forecast; the card also prints its
    formula at August's index, 8.28, which is the figure kept."""
    snap = mega.parse_snapshot("mega_smart_flex", REGION_WALLONIA, _card("WL-092026-Smart0109"))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTP mensuel"
    assert not energy.settled
    assert energy.period == "month"
    assert energy.price == pytest.approx(0.0828)
    assert energy.yearly_fixed_fee == pytest.approx(68.9)
    # "ZTP x 1,08 + 1,15 c EUR/kWh" excluding VAT, with ZTP in c EUR/kWh,
    # grossed up by the card's 6%.
    assert energy.formula == "ZTP x 1,08 + 1,15 c€/kWh"
    assert energy.factor == pytest.approx(1.08 / 1000 * 1.06)
    assert energy.base == pytest.approx(0.0115 * 1.06)
    # Mega's own August value, 0,0617121 EUR/kWh, gives back the printed 8.28.
    assert energy.at(61.7121) == pytest.approx(0.0828, abs=5e-5)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)
    assert snap.taxes.vat_rate == 0.0


@pytest.mark.parametrize(
    ("contract", "name", "index", "formula", "price", "fee"),
    [
        (
            "mega_smart_flex",
            "WL-092026-Smart0109",
            "ZTP mensuel",
            "ZTP x 1,08 + 1,15 c€/kWh",
            0.0828,
            68.9,
        ),
        (
            "mega_cosy_flex",
            "WL-092026-Cosy0809",
            "ZTP mensuel",
            "ZTP x 1,08 + 1,15 c€/kWh",
            0.0828,
            106.0,
        ),
        (
            "mega_online_flex",
            "WL-092026-Online0109",
            "ZTP mensuel",
            "ZTP x 1,08 + 0,85 c€/kWh",
            0.0797,
            21.2,
        ),
        (
            "mega_prepaid_flex",
            "WL-092026-Prepaid",
            "ZTP mensuel",
            "ZTP x 1,08 + 0,20 c€/kWh",
            0.0728,
            42.4,
        ),
        (
            "mega_offpeak_flex",
            "WL-092026-Offpeak-Bi-Var",
            "TTF1",
            "TTF x 1,08 + 0,5 c€/kWh",
            0.0758,
            74.2,
        ),
        (
            "mega_offpeak_impact",
            "WL-092026-Offpeak-Impact-Var",
            "TTF1",
            "TTF x 1,08 + 0,5 c€/kWh",
            0.0758,
            74.2,
        ),
    ],
)
def test_flex_formula_reproduces_the_printed_price(
    contract: str, name: str, index: str, formula: str, price: float, fee: float
) -> None:
    """Every Flex card's price is its formula at the August value Mega
    publishes for the index it names, to the card's 0,01 c EUR/kWh."""
    energy = mega.parse_snapshot(contract, REGION_WALLONIA, _card(name)).energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == index
    assert energy.formula == formula
    assert energy.price == pytest.approx(price)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    assert energy.at(_index()[index]["2026-08"]) == pytest.approx(price, abs=5e-5)


def test_the_forecast_is_kept_when_the_last_known_price_is_missing() -> None:
    text = _card("WL-092026-Smart0109").replace("derniers prix constatés", "derniers prix")
    energy = mega.parse_snapshot("mega_smart_flex", REGION_WALLONIA, text).energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(0.0834)


@pytest.mark.parametrize(
    ("contract", "name", "price", "fee"),
    [
        ("mega_smart_fixed", "WL-092026-Smart0809-Fixed", 0.0808, 111.3),
        ("mega_cosy_fixed", "WL-092026-Cosy0109-Fixed", 0.0808, 143.1),
        ("mega_online_fixed", "WL-092026-Online0109-Fixed", 0.085, 58.3),
        ("mega_prepaid_fixed", "WL-092026-Prepaid0109-Fix", 0.0782, 47.7),
        ("mega_offpeak_fixed", "WL-092026-Offpeak-Bi0109-Fix", 0.0813, 74.2),
        ("mega_zen_fixed", "WL-092026-Zen0109-Fixed", 0.0792, 111.3),
    ],
)
def test_fixed_cards(contract: str, name: str, price: float, fee: float) -> None:
    snap = mega.parse_snapshot(contract, REGION_WALLONIA, _card(name))
    assert snap.energy == FixedRates(price=approx(price), yearly_fixed_fee=fee)
    assert snap.publication_label == "2026-09"


def test_wallonia_table_collapses_the_ores_sub_areas() -> None:
    snap = mega.parse_snapshot("mega_smart_flex", REGION_WALLONIA, _card("WL-092026-Smart0109"))
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "ORES (Brabant" / "wallon)" wraps above its row, then "4.29 2.21 1.64
    # 31.91 140.93 889.48", the same on all five sub-areas.
    assert snap.dsos[DSO_ORES] == _overlay((4.29, 2.21, 1.64), (31.91, 140.93, 889.48), 0.0)
    assert snap.dsos[DSO_RESA] == _overlay((4.64, 2.53, 2.24), (34.59, 122.05, 962.74), 0.0)


def test_wallonia_levies() -> None:
    snap = mega.parse_snapshot("mega_smart_flex", REGION_WALLONIA, _card("WL-092026-Smart0109"))
    # "Redevance de raccordement 0.0075" c EUR/kWh, VAT exempt.
    assert snap.taxes.connection_fee == pytest.approx(0.000075)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    # "La contribution energetique est fixee a 0 a partir du 1er aout 2026".
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_flanders_table_carries_the_data_management_fee() -> None:
    snap = mega.parse_snapshot("mega_smart_flex", REGION_FLANDERS, _card("VL-092026-Smart0109"))
    assert set(snap.dsos) == FLUVIUS_KEYS
    assert snap.dsos[DSO_FLUVIUS_ANTWERPEN] == _overlay(
        (2.26, 0.91, 0.59), (15.68, 83.22, 562.63), 18.92
    )
    # "Fluvius Halle-" / "Vilvoorde" wraps at its hyphen.
    assert snap.dsos[DSO_FLUVIUS_HALLE_VILVOORDE] == _overlay(
        (2.5, 0.98, 0.62), (17.59, 93.38, 631.27), 18.92
    )
    # Mega prints 16.16 where Fluvius and Engie print 16,17: read as printed.
    assert snap.dsos[DSO_FLUVIUS_KEMPEN].tiers[TIER_T1].fixed_per_year == pytest.approx(16.16)
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber is None
    assert (
        snap.energy
        == mega.parse_snapshot(
            "mega_smart_flex", REGION_WALLONIA, _card("WL-092026-Smart0109")
        ).energy
    )


def test_brussels_card_reads_the_per_meter_levy() -> None:
    snap = mega.parse_snapshot("mega_smart_flex", REGION_BRUSSELS, _card("BX-092026-Smart0109"))
    assert set(snap.dsos) == {DSO_SIBELGA}
    # "Sibelga 1.99 1.45 0.81 15.9 43.07 1001.55 24.95", the last column the
    # yearly "Mesure et comptage".
    assert snap.dsos[DSO_SIBELGA] == _overlay((1.99, 1.45, 0.81), (15.9, 43.07, 1001.55), 24.95)
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber == {
        "q10_le5000": pytest.approx(3.56),
        "q10_gt5000": pytest.approx(12.59),
        "q16": pytest.approx(30.4),
        "q25": pytest.approx(75.18),
        "q40": pytest.approx(150.35),
        "q65": pytest.approx(376.0),
        "q100": pytest.approx(522.79),
        "q160": pytest.approx(671.36),
        "gt160": pytest.approx(970.41),
    }


def test_july_card_prints_the_old_excise_and_the_energy_contribution() -> None:
    """Before August 2026 the card prints the contribution beside each
    excise slice, and the Smart Flex formula carried a base of 1."""
    snap = mega.parse_snapshot("mega_smart_flex", REGION_WALLONIA, _card("WL-072026-Smart0107"))
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0098914)),
    )
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.formula == "ZTP x 1,08 + 1 c€/kWh"
    # "pour le mois de juin 2026 ... 6.15", at June's 0,04443933 EUR/kWh.
    assert energy.price == pytest.approx(0.0615)
    assert energy.at(_index()["ZTP mensuel"]["2026-06"]) == pytest.approx(0.0615, abs=5e-5)
    assert snap.valid_until == date(2026, 7, 31)


def test_card_for_another_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="Smart Fixed"):
        mega.parse_snapshot("mega_zen_fixed", REGION_WALLONIA, _card("WL-092026-Smart0809-Fixed"))
    with pytest.raises(ExtractorError, match="Offpeak Impact Flex"):
        mega.parse_snapshot(
            "mega_offpeak_flex", REGION_WALLONIA, _card("WL-092026-Offpeak-Impact-Var")
        )


def test_card_for_another_region_is_refused() -> None:
    with pytest.raises(ExtractorError):
        mega.parse_snapshot("mega_smart_flex", REGION_BRUSSELS, _card("WL-092026-Smart0109"))
    with pytest.raises(ExtractorError):
        mega.parse_snapshot(
            "mega_offpeak_impact", REGION_FLANDERS, _card("WL-092026-Offpeak-Impact-Var")
        )


def test_unknown_contract_is_refused() -> None:
    with pytest.raises(ExtractorError):
        mega.parse_snapshot("mega_dynamic", REGION_WALLONIA, _card("WL-092026-Smart0109"))


@pytest.mark.parametrize(
    "missing",
    [
        "La formule tarifaire pour le gaz",
        "Weekend ZTP (EGSI)",
        "La contribution énergétique est fixée à 0",
        "Redevance de raccordement",
        "Coût du transport\n",
        "Prix du \nmois",
    ],
)
def test_card_that_lost_a_mandatory_figure_fails_loud(missing: str) -> None:
    text = _card("WL-092026-Smart0109")
    assert missing in text
    with pytest.raises(ExtractorError):
        mega.parse_snapshot("mega_smart_flex", REGION_WALLONIA, text.replace(missing, "x"))


def _listing() -> str:
    return fixture_page("mega", "cartes-tarifaires.html")


@pytest.mark.parametrize(
    ("contract", "region", "name"),
    [
        ("mega_smart_flex", REGION_WALLONIA, "WL-092026-Smart0109"),
        ("mega_smart_flex", REGION_BRUSSELS, "BX-092026-Smart0109"),
        ("mega_cosy_flex", REGION_FLANDERS, "VL-092026-Cosy0809"),
        # The gas Online Flex card, not the electricity "Online0109-Green".
        ("mega_online_flex", REGION_WALLONIA, "WL-092026-Online0109"),
        ("mega_prepaid_flex", REGION_WALLONIA, "WL-092026-Prepaid"),
        ("mega_offpeak_impact", REGION_WALLONIA, "WL-092026-Offpeak-Impact-Var"),
        ("mega_offpeak_fixed", REGION_BRUSSELS, "BX-092026-Offpeak-Bi0109-Fix"),
        ("mega_zen_fixed", REGION_FLANDERS, "VL-092026-Zen0109-Fixed"),
    ],
)
def test_listing_links_the_gas_card(contract: str, region: str, name: str) -> None:
    assert mega.card_url(_listing(), contract, region) == f"{_CDN}Mega-FR-NG-B2C-{name}.pdf"


def test_every_contract_is_on_the_listing_where_it_is_sold() -> None:
    for contract in mega.EXTRACTOR.contracts:
        for region in contract.regions:
            assert "-NG-B2C-" in mega.card_url(_listing(), contract.id, region)
    with pytest.raises(ExtractorError):
        mega.card_url(_listing(), "mega_offpeak_impact", REGION_FLANDERS)


@pytest.mark.parametrize(
    ("current", "month", "expected"),
    [
        # A mid-month reissue: August's card is the one issued on the 1st.
        ("WL-092026-Cosy0809", date(2026, 8, 1), "WL-082026-Cosy0108"),
        ("WL-092026-Smart0809-Fixed", date(2026, 8, 1), "WL-082026-Smart0108-Fixed"),
        ("WL-092026-Offpeak-Bi0109-Fix", date(2026, 7, 1), "WL-072026-Offpeak-Bi0107-Fix"),
        ("BX-092026-Zen0109-Fixed", date(2025, 12, 1), "BX-122025-Zen0112-Fixed"),
        # No issue date in the suffix.
        ("WL-092026-Prepaid", date(2026, 8, 1), "WL-082026-Prepaid"),
        # The current month is the listing's own latest issue.
        ("WL-092026-Cosy0809", date(2026, 9, 1), "WL-092026-Cosy0809"),
    ],
)
def test_archive_url_moves_both_months(current: str, month: date, expected: str) -> None:
    url = mega.archive_url(f"{_CDN}Mega-FR-NG-B2C-{current}.pdf", month)
    assert url == f"{_CDN}Mega-FR-NG-B2C-{expected}.pdf"


async def test_fetch_reads_the_card_the_listing_links() -> None:
    with (
        patch.object(mega, "fetch_text", AsyncMock(return_value=_listing())),
        patch.object(
            mega, "fetch_pdf_text", AsyncMock(return_value=_card("BX-092026-Smart0109"))
        ) as fetched,
    ):
        snap = await mega.fetch(AsyncMock(), "mega_smart_flex", REGION_BRUSSELS)
    url = f"{_CDN}Mega-FR-NG-B2C-BX-092026-Smart0109.pdf"
    assert fetched.call_args.args[1] == url
    assert snap.source_url == url
    assert set(snap.dsos) == {DSO_SIBELGA}


async def test_fetch_for_month_builds_the_first_issue_of_the_month() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(mega, "fetch_text", AsyncMock(return_value=_listing())),
        patch.object(
            mega, "fetch_pdf_text", AsyncMock(return_value=_card("WL-082026-Cosy0108"))
        ) as fetched,
    ):
        snap = await mega.fetch_for_month(
            AsyncMock(), "mega_cosy_flex", REGION_WALLONIA, date(2026, 8, 1)
        )
    assert fetched.call_args.args[1] == f"{_CDN}Mega-FR-NG-B2C-WL-082026-Cosy0108.pdf"
    assert snap is not None
    assert snap.publication_label == "2026-08"
    assert snap.valid_until == date(2026, 8, 31)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # The August card's own formula, and its price at July's index.
    assert energy.formula == "ZTP x 1,08 + 1 c€/kWh"
    assert energy.at(_index()["ZTP mensuel"]["2026-07"]) == pytest.approx(0.0716, abs=5e-5)


async def test_fetch_for_month_counts_the_month_in_brussels_time() -> None:
    """Just after midnight on 1 September in Brussels a UTC host is still on
    31 August: September is the current month, not a future one."""
    with (
        freeze_time(datetime(2026, 8, 31, 22, 30)),
        patch.object(mega, "fetch_text", AsyncMock(return_value=_listing())),
        patch.object(
            mega, "fetch_pdf_text", AsyncMock(return_value=_card("WL-092026-Cosy0809"))
        ) as fetched,
    ):
        snap = await mega.fetch_for_month(
            AsyncMock(), "mega_cosy_flex", REGION_WALLONIA, date(2026, 9, 1)
        )
    assert snap is not None
    assert fetched.call_args.args[1] == f"{_CDN}Mega-FR-NG-B2C-WL-092026-Cosy0809.pdf"


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(mega, "fetch_text", AsyncMock(return_value=_listing())),
        patch.object(mega, "fetch_pdf_text", AsyncMock(return_value=_card("WL-092026-Smart0109"))),
    ):
        assert (
            await mega.fetch_for_month(
                AsyncMock(), "mega_smart_flex", REGION_WALLONIA, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_treats_the_cdn_stub_as_no_card() -> None:
    stub = ExtractorError("expected a PDF at x, payload starts with b'<!DOCTYPE html>'")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(mega, "fetch_text", AsyncMock(return_value=_listing())),
        patch.object(mega, "fetch_pdf_text", AsyncMock(side_effect=stub)),
    ):
        assert (
            await mega.fetch_for_month(
                AsyncMock(), "mega_offpeak_flex", REGION_WALLONIA, date(2026, 1, 1)
            )
            is None
        )


@pytest.mark.parametrize("failing", ["fetch_text", "fetch_pdf_text"])
async def test_fetch_for_month_raises_on_a_transient_failure(failing: str) -> None:
    mocks = {
        "fetch_text": AsyncMock(return_value=_listing()),
        "fetch_pdf_text": AsyncMock(return_value=_card("WL-082026-Cosy0108")),
    }
    mocks[failing] = AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(mega, "fetch_text", mocks["fetch_text"]),
        patch.object(mega, "fetch_pdf_text", mocks["fetch_pdf_text"]),
        pytest.raises(ExtractorError),
    ):
        await mega.fetch_for_month(AsyncMock(), "mega_cosy_flex", REGION_WALLONIA, date(2026, 8, 1))


async def test_fetch_for_month_does_not_ask_for_the_future() -> None:
    listing = AsyncMock(return_value=_listing())
    with freeze_time(datetime(2026, 9, 15, 12)), patch.object(mega, "fetch_text", listing):
        assert (
            await mega.fetch_for_month(
                AsyncMock(), "mega_smart_flex", REGION_WALLONIA, date(2026, 10, 1)
            )
            is None
        )
    listing.assert_not_called()


async def test_fetch_for_month_ignores_a_region_the_product_is_not_sold_in() -> None:
    assert (
        await mega.fetch_for_month(
            AsyncMock(), "mega_offpeak_impact", REGION_BRUSSELS, date(2026, 8, 1)
        )
        is None
    )


def test_index_page_reads_both_series_by_delivery_month() -> None:
    table = _index()
    assert set(table) == {"ZTP mensuel", "TTF1"}
    # "01-08-2026 01-09-2026 EUR0.0617121" in EUR/kWh, kept in EUR/MWh.
    assert table["ZTP mensuel"]["2026-08"] == pytest.approx(61.7121)
    assert table["ZTP mensuel"]["2026-07"] == pytest.approx(53.26036)
    assert table["ZTP mensuel"]["2023-01"] == pytest.approx(62.00871)
    assert table["TTF1"]["2026-08"] == pytest.approx(61.54081)
    assert table["TTF1"]["2021-01"] == pytest.approx(20.33607)
    # One value per month, gap free, and September is not known yet.
    assert len(table["ZTP mensuel"]) == 44
    assert len(table["TTF1"]) == 68
    assert "2026-09" not in table["ZTP mensuel"]


def test_index_page_without_the_series_fails_loud() -> None:
    page = fixture_page("mega", "indexation-de-nos-produits-variables.html")
    with pytest.raises(ExtractorError, match="TTF1"):
        mega.parse_index_page(page.replace('data-line="TTF1"', 'data-line="gone"'))


async def test_fetch_index_reads_mega_s_page() -> None:
    page = fixture_page("mega", "indexation-de-nos-produits-variables.html")
    with patch.object(mega, "fetch_text", AsyncMock(return_value=page)) as fetched:
        table = await mega.fetch_index(AsyncMock())
    assert fetched.call_args.args[1] == mega._INDEX_URL
    assert table["TTF1"]["2026-07"] == pytest.approx(53.28965)


def test_contracts_and_regions() -> None:
    by_id = {c.id: c for c in mega.EXTRACTOR.contracts}
    assert len(by_id) == 12
    assert by_id["mega_offpeak_impact"].regions == frozenset({REGION_WALLONIA})
    assert by_id["mega_smart_flex"].regions == frozenset(
        {REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS}
    )
    assert {c.kind for c in by_id.values()} == {"fixed", "indexed"}
    assert sum(c.kind == "indexed" for c in by_id.values()) == 6
    assert not any(c.professional for c in by_id.values())
