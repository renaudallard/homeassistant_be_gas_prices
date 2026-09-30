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

"""Luminus gas card extractor, against the September 2026 cards."""

from __future__ import annotations

from datetime import date, datetime
from functools import cache
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_KEMPEN,
    DSO_ORES,
    DSO_RESA,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.providers import luminus
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import FIXTURES, approx, fixture_page, fixture_text

_COMFYFLEX_W = "LUMINUS_PL_202609_ZGR2A0_FR_WAL_ComfyFlex_Gas_1_year_Online_Sales_Luminusbe.pdf"
_COMFYFLEX_V = "LUMINUS_PL_202609_ZGR2A0_FR_FLA_ComfyFlex_Gas_1_year_Online_Sales_Luminusbe.pdf"
_COMFYFLEX_PLUS_W = (
    "LUMINUS_PL_202609_ZGR2A0I_FR_WAL_ComfyFlex_Gas_1_year_Online_Sales_Luminusbe.pdf"
)
_MAXXFLEX_W = "LUMINUS_PL_202609_ZGR2A2X_FR_WAL_MaxxFlex_Gas_2_year_Online_Sales_Luminusbe.pdf"
_MAXXFLEX_V = "LUMINUS_PL_202609_ZGR2A2X_FR_FLA_MaxxFlex_Gas_2_year_Online_Sales_Luminusbe.pdf"
_BASICFLEX_W = "LUMINUS_PL_202609_ZGR1A0D_FR_WAL_BasicFlex_Gas_0_year_Online_Sales_Luminusbe.pdf"
_COMFY_W = "LUMINUS_PL_202609_ZGR2B0_FR_WAL_Comfy_Gas_0_year_Online_Sales_Luminusbe.pdf"
_COMFY_PLUS_W = "LUMINUS_PL_202609_ZGR2B0I_FR_WAL_Comfy_Gas_0_year_Online_Sales_Luminusbe.pdf"
_MAXXFIX_W = "LUMINUS_PL_202609_ZGR2B2X_FR_WAL_MaxxFix_Gas_2_year_Online_Sales_Luminusbe.pdf"
_BASICFIX_W = "LUMINUS_PL_202609_ZGR1B2D_FR_WAL_BasicFix_Gas_2_year_Online_Sales_Luminusbe.pdf"
_COMFYFLEX_2025 = (
    "LUMINUS_PL_202512_ZGR2A0_FR_FLA_ComfyFlex_Gas_1_year_Direct_Mail_Archive_Price_Lists.pdf"
)
_BASICFIX_2025 = (
    "LUMINUS_PL_202512_ZGR1B2D_FR_FLA_BasicFix_Gas_2_year_Direct_Mail_Archive_Price_Lists.pdf"
)
_ARCHIVE_AUGUST = "archive_ComfyFlex_Gaz_Wallonia_2026-08.pdf"
_ARCHIVE_PRODUCTS = "archive_products_Gas_Wallonia_2026-08.json"
_INDEX_PDF = "file-52a69f0e2a8fdd2905723e58474ddc38bd49de04-pdf.pdf"
_INDEX_PAGE = "parametres-d-indexation_fr.html"

# The card prints its formula coefficients rounded ("Les montants mentionnes
# sont arrondis") and its price to 0,01 c EUR/kWh, so the formula at the named
# index lands within 0,01 c EUR/kWh of the printed price, not exactly on it.
_ROUNDING = 1e-4


def _card(name: str) -> str:
    return fixture_text("luminus", name)


@cache
def _index_text() -> str:
    """pdfplumber takes seconds on the index PDF, so it is read once."""
    return luminus.index_table_text((FIXTURES / "luminus" / _INDEX_PDF).read_bytes())


def test_comfyflex_indexes_quarterly_on_ttf_dahw() -> None:
    """The printed price is the formula at the last closed quarter's value
    ("TTF DAHW = 45,65 EUR/MWh (valeur de l'indice du 2ieme trimestre 2026)"),
    known only once the quarter is over."""
    snap = luminus.parse_snapshot("luminus_comfyflex", REGION_WALLONIA, _card(_COMFYFLEX_W))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF DAHW"
    assert energy.period == "quarter"
    assert not energy.settled
    assert energy.price == pytest.approx(0.0700)
    assert energy.yearly_fixed_fee == pytest.approx(50.0)
    # "0,1004 x TTF DAHW + 0,0000 x TTF 1-0-3 + 2,0204" in c EUR/kWh excluding
    # VAT, grossed up by the card's 6%.
    assert energy.factor == pytest.approx(0.001004 * 1.06)
    assert energy.base == pytest.approx(0.020204 * 1.06)
    assert energy.formula == "0,1004 x TTF DAHW + 0,0000 x TTF 1-0-3 + 2,0204"
    assert energy.at(45.65) == pytest.approx(0.0700, abs=_ROUNDING)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_flanders_card_prices_the_energy_differently() -> None:
    snap = luminus.parse_snapshot("luminus_comfyflex", REGION_FLANDERS, _card(_COMFYFLEX_V))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # "+ 1,8004" against Wallonia's 2,0204, on the same index value.
    assert energy.base == pytest.approx(0.018004 * 1.06)
    assert energy.price == pytest.approx(0.0677)
    assert energy.at(45.65) == pytest.approx(0.0677, abs=_ROUNDING)


def test_comfyflex_plus_is_its_own_card() -> None:
    snap = luminus.parse_snapshot(
        "luminus_comfyflex_plus", REGION_WALLONIA, _card(_COMFYFLEX_PLUS_W)
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.base == pytest.approx(0.023954 * 1.06)
    assert energy.price == pytest.approx(0.0740)
    assert energy.at(45.65) == pytest.approx(0.0740, abs=_ROUNDING)


@pytest.mark.parametrize(
    ("name", "region", "base", "price"),
    [
        # "TTF DAH M = 61,438 EUR/MWh (valeur de l'indice de aout 2026)",
        # "0,1001 x TTF DAH M + 1,5650".
        (_MAXXFLEX_W, REGION_WALLONIA, 0.015650, 0.0818),
        # "+ 1,3450": 7,9446 at the rounded coefficients against 7,95 printed.
        (_MAXXFLEX_V, REGION_FLANDERS, 0.013450, 0.0795),
    ],
)
def test_maxxflex_indexes_on_the_delivery_month(
    name: str, region: str, base: float, price: float
) -> None:
    snap = luminus.parse_snapshot("luminus_maxxflex", region, _card(name))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF DAH M"
    assert energy.period == "month"
    assert not energy.settled
    assert energy.factor == pytest.approx(0.001001 * 1.06)
    assert energy.base == pytest.approx(base * 1.06)
    assert energy.price == pytest.approx(price)
    assert energy.at(61.438) == pytest.approx(price, abs=_ROUNDING)


def test_basicflex_indexes_on_the_rlp_weighted_month() -> None:
    snap = luminus.parse_snapshot("luminus_basicflex", REGION_WALLONIA, _card(_BASICFLEX_W))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # "TTF DAH RLP M = 61,637 EUR/MWh", "0,1001 x TTF DAH RLP M + 0,5609".
    assert energy.index == "TTF DAH RLP M"
    assert energy.period == "month"
    assert energy.base == pytest.approx(0.005609 * 1.06)
    assert energy.price == pytest.approx(0.0714)
    assert energy.yearly_fixed_fee == pytest.approx(20.0)
    assert energy.at(61.637) == pytest.approx(0.0714, abs=_ROUNDING)


@pytest.mark.parametrize(
    ("contract", "name", "price", "fee"),
    [
        ("luminus_comfy", _COMFY_W, 0.0960, 50.0),
        ("luminus_comfy_plus", _COMFY_PLUS_W, 0.1003, 50.0),
        ("luminus_maxxfix", _MAXXFIX_W, 0.0902, 50.0),
        ("luminus_basicfix", _BASICFIX_W, 0.0768, 20.0),
    ],
)
def test_fixed_cards(contract: str, name: str, price: float, fee: float) -> None:
    snap = luminus.parse_snapshot(contract, REGION_WALLONIA, _card(name))
    assert snap.energy == FixedRates(price=approx(price), yearly_fixed_fee=fee)


@pytest.mark.parametrize(
    ("contract", "name"),
    [
        # The file names do not tell Comfy+ from Comfy; only the title does.
        ("luminus_comfy_plus", _COMFY_W),
        ("luminus_comfy", _COMFY_PLUS_W),
        # What the endpoint serves for the electricity-only "dynamic" slug.
        ("luminus_comfyflex", _MAXXFLEX_W),
    ],
)
def test_card_for_another_product_is_refused(contract: str, name: str) -> None:
    with pytest.raises(ExtractorError, match="card is for"):
        luminus.parse_snapshot(contract, REGION_WALLONIA, _card(name))


def test_card_for_another_region_is_refused() -> None:
    with pytest.raises(ExtractorError, match="not FL"):
        luminus.parse_snapshot("luminus_comfyflex", REGION_FLANDERS, _card(_COMFYFLEX_W))
    with pytest.raises(ExtractorError, match="not sold"):
        luminus.parse_snapshot("luminus_comfyflex", REGION_BRUSSELS, _card(_COMFYFLEX_W))


def test_unknown_contract_is_refused() -> None:
    with pytest.raises(ExtractorError):
        luminus.parse_snapshot("luminus_dynamic", REGION_WALLONIA, _card(_MAXXFLEX_W))


def test_wallonia_table_collapses_the_ores_sub_areas() -> None:
    snap = luminus.parse_snapshot("luminus_comfyflex", REGION_WALLONIA, _card(_COMFYFLEX_W))
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "ORES (Brabant Wallon) 4,29 31,91 2,21 140,93 1,64 889,48 0,1654": the
    # variable term before the fixed one in each tier.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.0429)
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.0221)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T3].proportional == pytest.approx(0.0164)
    assert ores.tiers[TIER_T3].fixed_per_year == pytest.approx(889.48)
    assert ores.transport == pytest.approx(0.001654)
    assert ores.metering_per_year == 0.0
    # "TECTEO RESA 4,64 34,59 2,53 122,05 2,24 962,74 0,1654"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T1].fixed_per_year == pytest.approx(34.59)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert resa.tiers[TIER_T3].fixed_per_year == pytest.approx(962.74)


def test_wallonia_levies() -> None:
    snap = luminus.parse_snapshot("luminus_comfyflex", REGION_WALLONIA, _card(_COMFYFLEX_W))
    # "0-12.000 kWh : 1,0929 c EUR/kWh, >= 12.001 kWh : 1,1830 c EUR/kWh"
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.010929)),
        (None, pytest.approx(0.011830)),
    )
    # "Cotisation sur l'energie" printed as a dash, the connection fee 0,0075.
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.connection_fee == pytest.approx(0.000075)
    assert snap.taxes.osp_by_caliber is None
    assert snap.taxes.vat_rate == 0.0


def test_flanders_table_has_every_fluvius_area_and_no_data_management() -> None:
    snap = luminus.parse_snapshot("luminus_comfyflex", REGION_FLANDERS, _card(_COMFYFLEX_V))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Kempen 2,27 16,17 0,88 85,83 0,55 580,25 0,1654"
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.0227)
    assert kempen.tiers[TIER_T1].fixed_per_year == pytest.approx(16.17)
    assert kempen.tiers[TIER_T2].proportional == pytest.approx(0.0088)
    assert kempen.tiers[TIER_T2].fixed_per_year == pytest.approx(85.83)
    assert kempen.tiers[TIER_T3].proportional == pytest.approx(0.0055)
    assert kempen.tiers[TIER_T3].fixed_per_year == pytest.approx(580.25)
    assert kempen.transport == pytest.approx(0.001654)
    # The 2026 cards print no databeheer column.
    assert kempen.metering_per_year == 0.0
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.excise_bands[0] == (12000.0, pytest.approx(0.010929))


@pytest.mark.parametrize(
    ("region", "metering", "connection_fee"),
    [(REGION_FLANDERS, 18.56, 0.0), (REGION_WALLONIA, 0.0, 0.000075)],
)
def test_2025_card_serves_both_regions(region: str, metering: float, connection_fee: float) -> None:
    """Until December 2025 one card covered Flanders and Wallonia, with a data
    management column and one run of tax figures per region."""
    snap = luminus.parse_snapshot("luminus_comfyflex", region, _card(_COMFYFLEX_2025))
    assert snap.publication_label == "2025-12"
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # "TTF DAHW = 32,44 EUR/MWh", "0,1000 x TTF DAHW + 0,0000 x TTF 1-0-3 + 1,6933"
    assert energy.price == pytest.approx(0.0523)
    assert energy.at(32.44) == pytest.approx(0.0523, abs=_ROUNDING)
    # "Fluvius Antwerpen 2,10 14,24 0,87 75,60 0,58 511,10 0,1654 18,56" and
    # "ORES (Brabant Wallon) 4,04 30,86 2,04 135,42 1,50 853,33 0,1654 -".
    key = DSO_FLUVIUS_ANTWERPEN if region == REGION_FLANDERS else DSO_ORES
    dso = snap.dsos[key]
    assert dso.metering_per_year == pytest.approx(metering)
    assert dso.tiers[TIER_T1].fixed_per_year == pytest.approx(
        14.24 if region == REGION_FLANDERS else 30.86
    )
    # The pre-August 2026 levies, read as printed: the law override is applied
    # for the delivery month later.
    assert snap.taxes.energy_contribution == pytest.approx(0.001058)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.008724)),
        (None, pytest.approx(0.009431)),
    )
    assert snap.taxes.connection_fee == pytest.approx(connection_fee)


def test_2025_title_without_the_online_marker_is_the_same_product() -> None:
    """The December 2025 card is titled "Luminus BasicFix Gaz", the product
    the 2026 cards title "BasicFix Online Gaz"."""
    snap = luminus.parse_snapshot("luminus_basicfix", REGION_FLANDERS, _card(_BASICFIX_2025))
    assert snap.energy == FixedRates(price=approx(0.0531), yearly_fixed_fee=20.0)


def test_forward_term_weighted_in_fails_loud() -> None:
    text = _card(_COMFYFLEX_W).replace("0,0000  x  TTF 1-0-3", "0,0100  x  TTF 1-0-3")
    with pytest.raises(ExtractorError, match="TTF 1-0-3"):
        luminus.parse_snapshot("luminus_comfyflex", REGION_WALLONIA, text)


def test_walloon_card_without_connection_fee_fails_loud() -> None:
    text = _card(_COMFYFLEX_W).replace("\n0,0075\n", "\n-\n")
    with pytest.raises(ExtractorError, match="connection fee"):
        luminus.parse_snapshot("luminus_comfyflex", REGION_WALLONIA, text)


def test_index_pdf_places_each_figure_under_its_year() -> None:
    """The quarterly table leaves Q3 and Q4 2026 empty; read as text alone
    "Q3 35,320 32,436" would not say which two years they are."""
    lines = _index_text().splitlines()
    assert lines[0] == "TTFDAHW 2024 2025 2026"
    assert "Q2 31,539 35,358 45,649" in lines
    assert "Q3 35,320 32,436 -" in lines
    assert "TTFDAH RLP M 2024 2025 2026" in lines
    assert "Août 37,614 32,154 61,637" in lines


def test_index_table_holds_the_values_the_cards_name() -> None:
    table = luminus.parse_index_text(_index_text())
    assert set(table) == {"TTF DAHW", "TTF DAH M", "TTF DAH RLP M"}
    # Q2 2026 fills its three months; the ComfyFlex card prints it as 45,65.
    for month in ("2026-04", "2026-05", "2026-06"):
        assert table["TTF DAHW"][month] == pytest.approx(45.649)
    assert "2026-07" not in table["TTF DAHW"]
    # The values the MaxxFlex and BasicFlex cards name for August 2026.
    assert table["TTF DAH M"]["2026-08"] == pytest.approx(61.438)
    assert table["TTF DAH RLP M"]["2026-08"] == pytest.approx(61.637)
    assert table["TTF DAH M"]["2024-01"] == pytest.approx(29.921)
    assert "2026-09" not in table["TTF DAH M"]


@pytest.mark.parametrize(
    ("contract", "name"),
    [
        ("luminus_comfyflex", _COMFYFLEX_W),
        ("luminus_maxxflex", _MAXXFLEX_W),
        ("luminus_basicflex", _BASICFLEX_W),
    ],
)
def test_card_index_names_are_the_published_ones(contract: str, name: str) -> None:
    energy = luminus.parse_snapshot(contract, REGION_WALLONIA, _card(name)).energy
    assert isinstance(energy, IndexedRates)
    assert energy.index in luminus.parse_index_text(_index_text())


def test_index_rows_of_the_wrong_period_are_refused() -> None:
    with pytest.raises(ExtractorError):
        luminus.parse_index_text("TTFDAHM 2025 2026\nQ1 30,000 31,000")


def test_indexation_page_links_the_index_pdf() -> None:
    assert luminus.parse_index_page(fixture_page("luminus", _INDEX_PAGE)) == (
        "https://www.luminus.be/api-next/download/"
        "?fileId=file-52a69f0e2a8fdd2905723e58474ddc38bd49de04-pdf&openFile=true"
    )


async def test_fetch_index_follows_the_page_link() -> None:
    with (
        patch.object(
            luminus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("luminus", _INDEX_PAGE)),
        ),
        patch.object(luminus, "fetch_pdf_rendered", AsyncMock(return_value=_index_text())) as pdf,
    ):
        table = await luminus.fetch_index(AsyncMock())
    assert "file-52a69f0e2a8fdd2905723e58474ddc38bd49de04-pdf" in pdf.call_args.args[1]
    assert table["TTF DAH M"]["2026-07"] == pytest.approx(53.206)


async def test_fetch_builds_the_regional_url() -> None:
    with patch.object(
        luminus, "fetch_pdf_text", AsyncMock(return_value=_card(_COMFYFLEX_V))
    ) as fetched:
        snap = await luminus.fetch(AsyncMock(), "luminus_comfyflex", REGION_FLANDERS)
    url = fetched.call_args.args[1]
    assert url == (
        "https://www.luminus.be/api-next/get-pricelist/"
        "?documentSlug=comfyflex&energyType=gas&language=fr&tabValue=Flanders"
    )
    assert snap.source_url == url


async def test_probe_reads_the_content_md5() -> None:
    with patch.object(luminus, "head_freshness_key", AsyncMock(return_value="t5+6ZUFZ")) as head:
        assert await luminus.probe(AsyncMock(), "luminus_comfy_plus", REGION_WALLONIA) == "t5+6ZUFZ"
    assert "documentSlug=comfy-plus" in head.call_args.args[1]
    assert head.call_args.kwargs["prefer"] == ("Content-MD5",)
    assert await luminus.probe(AsyncMock(), "luminus_comfy", REGION_BRUSSELS) is None


async def test_fetch_for_month_resolves_the_archive_product() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            luminus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("luminus", _ARCHIVE_PRODUCTS)),
        ) as listed,
        patch.object(
            luminus, "fetch_pdf_text", AsyncMock(return_value=_card(_ARCHIVE_AUGUST))
        ) as fetched,
    ):
        snap = await luminus.fetch_for_month(
            AsyncMock(), "luminus_comfyflex", REGION_WALLONIA, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert listed.call_args.args[1] == (
        "https://www.luminus.be/api/pricelist/products?language=FR"
        "&customerSegment=Residential&energyType=Gas&region=Wallonia&signing=2026-08"
    )
    # "Luminus ComfyFlex Gaz" in the list, not "Luminus ComfyFlex+ Gaz".
    assert fetched.call_args.args[1] == (
        "https://www.luminus.be/api/pricelist/pdf?language=FR"
        "&productId=a1p0800000BygAeAAJ&date=2026-08&region=Wallonia&inline=true"
    )


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            luminus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("luminus", _ARCHIVE_PRODUCTS)),
        ),
        patch.object(luminus, "fetch_pdf_text", AsyncMock(return_value=_card(_ARCHIVE_AUGUST))),
    ):
        assert (
            await luminus.fetch_for_month(
                AsyncMock(), "luminus_comfyflex", REGION_WALLONIA, date(2026, 7, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            luminus, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await luminus.fetch_for_month(
            AsyncMock(), "luminus_maxxflex", REGION_FLANDERS, date(2026, 8, 1)
        )


async def test_fetch_for_month_is_none_for_a_card_that_is_not_there() -> None:
    """The PDF endpoint answers "Pdf not available" as JSON under a 200."""
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            luminus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("luminus", _ARCHIVE_PRODUCTS)),
        ),
        patch.object(
            luminus,
            "fetch_pdf_text",
            AsyncMock(side_effect=ExtractorError("expected a PDF at x, payload starts with b''")),
        ),
    ):
        assert (
            await luminus.fetch_for_month(
                AsyncMock(), "luminus_maxxfix", REGION_WALLONIA, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_does_not_ask_for_the_future() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(luminus, "fetch_text", AsyncMock()) as listed,
    ):
        assert (
            await luminus.fetch_for_month(
                AsyncMock(), "luminus_comfy", REGION_WALLONIA, date(2026, 10, 1)
            )
            is None
        )
    listed.assert_not_called()


async def test_fetch_for_month_takes_the_month_in_brussels_time() -> None:
    """At 22:30 UTC on 30 September it is already 1 October in Brussels, so
    October is the current month and is asked for."""
    with (
        freeze_time(datetime(2026, 9, 30, 22, 30)),
        patch.object(
            luminus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("luminus", _ARCHIVE_PRODUCTS)),
        ) as listed,
        patch.object(
            luminus,
            "fetch_pdf_text",
            AsyncMock(side_effect=ExtractorError("expected a PDF at x, payload starts with b''")),
        ),
    ):
        await luminus.fetch_for_month(
            AsyncMock(), "luminus_comfy", REGION_WALLONIA, date(2026, 10, 1)
        )
    assert "signing=2026-10" in listed.call_args.args[1]


def test_every_product_is_sold_in_flanders_and_wallonia() -> None:
    by_id = {c.id: c for c in luminus.EXTRACTOR.contracts}
    assert len(by_id) == 8
    for contract in by_id.values():
        assert contract.regions == frozenset({REGION_FLANDERS, REGION_WALLONIA})
    assert by_id["luminus_basicflex"].label == "Luminus BasicFlex Online"
    assert by_id["luminus_comfyflex_plus"].kind == "indexed"
    assert by_id["luminus_comfy_plus"].kind == "fixed"
