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

"""Elegant gas card extractor, against the September 2026 cards."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_MIDDEN_VLAANDEREN,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.providers import elegant
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import approx, fixture_page, fixture_text

_FLEX = "1788722319-flexgas_residential-0926.pdf"
_COMFORTFLEX = "1788722310-comfortflexgas_residential-0926.pdf"
_ZEKER_VAST = "1788722331-zekervastgas_residential-0926.pdf"
_ARCHIVED_FLEX = "archive_FlexGas_20260301.pdf"
_FLEX_OCTOBER = "1790833226-flexgas_residential.pdf"


def _card(name: str) -> str:
    return fixture_text("elegant", name, "layout")


@pytest.mark.parametrize(
    ("contract", "name"),
    [("elegant_flex", _FLEX), ("elegant_comfortflex", _COMFORTFLEX)],
)
def test_variable_cards_price_at_the_last_known_value(contract: str, name: str) -> None:
    snap = elegant.parse_snapshot(contract, REGION_FLANDERS, _card(name))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTFDAM"
    assert not energy.settled
    # "(c€/kWh) 7,20 Maandelijkse prijzen", "Vaste vergoeding (€/jaar)(1) 75,00".
    assert energy.price == pytest.approx(0.072)
    assert energy.yearly_fixed_fee == pytest.approx(75.0)
    # "(1,0250 x TTFDAM + 0,470) x 1,06" with TTFDAM in c EUR/kWh, VAT inside.
    assert energy.factor == pytest.approx(1.025 * 1.06 / 1000)
    assert energy.base == pytest.approx(0.470 * 1.06 / 100)
    # "de waarde van augustus 2026. Deze bedroeg 6,166 c€/kWh" is 61,66
    # EUR/MWh and reproduces the printed 7,20.
    assert energy.at(61.66) == pytest.approx(0.0720, abs=5e-5)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_zeker_vast_is_fixed() -> None:
    snap = elegant.parse_snapshot("elegant_zeker_vast", REGION_FLANDERS, _card(_ZEKER_VAST))
    assert snap.energy == FixedRates(price=approx(0.0803), yearly_fixed_fee=120.0)
    assert snap.publication_label == "2026-09"


def test_dso_table_and_the_lines_below_it() -> None:
    snap = elegant.parse_snapshot("elegant_flex", REGION_FLANDERS, _card(_FLEX))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Antwerpen 15,68 2,26 83,22 0,91", then "Tarief databeheer 18,92
    # €/jaar" and "Transportkosten 0,17 c€/kWh" for every DSO.
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0226)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.0091)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    # Rounded on the card; the Fluxys term is 0,16536.
    assert antwerpen.transport == pytest.approx(0.0017)
    # "Fluvius Midden-Vl. 16,33 2,32 86,71 0,91"
    midden = snap.dsos[DSO_FLUVIUS_MIDDEN_VLAANDEREN]
    assert midden.tiers[TIER_T1].fixed_per_year == pytest.approx(16.33)
    assert midden.tiers[TIER_T1].proportional == pytest.approx(0.0232)
    assert midden.tiers[TIER_T2].proportional == pytest.approx(0.0091)


def test_levies() -> None:
    taxes = elegant.parse_snapshot("elegant_flex", REGION_FLANDERS, _card(_FLEX)).taxes
    # "Verbruik 0 - 12000 kWh 1,093 c€/kWh", "Verbruik > 12000 kWh 1,183 c€/kWh"
    assert taxes.excise_bands == ((12000.0, pytest.approx(0.01093)), (None, pytest.approx(0.01183)))
    assert taxes.energy_contribution == 0.0
    assert taxes.connection_fee == 0.0
    assert taxes.osp_by_caliber is None


def test_archived_march_card() -> None:
    snap = elegant.parse_snapshot("elegant_flex", REGION_FLANDERS, _card(_ARCHIVED_FLEX))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.formula == "(1,0249 x TTFDAM + 0,435) x 1,06"
    assert energy.price == pytest.approx(0.0407)
    # "de waarde van februari 2026. Deze bedroeg 3,318 c€/kWh"
    assert energy.at(33.18) == pytest.approx(0.0407, abs=5e-5)
    assert snap.publication_label == "2026-03"
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.00872)),
        (None, pytest.approx(0.00962)),
    )


def test_flex_card_is_not_the_comfortflex_one() -> None:
    """The two print the same figures; only the heading tells them apart."""
    with pytest.raises(ExtractorError):
        elegant.parse_snapshot("elegant_comfortflex", REGION_FLANDERS, _card(_FLEX))


def test_other_region_is_refused() -> None:
    with pytest.raises(ExtractorError):
        elegant.parse_snapshot("elegant_flex", REGION_BRUSSELS, _card(_FLEX))


@pytest.mark.parametrize(
    ("contract", "name"),
    [
        ("elegant_flex", _FLEX),
        ("elegant_comfortflex", _COMFORTFLEX),
        ("elegant_zeker_vast", _ZEKER_VAST),
    ],
)
def test_listing_links_each_product(contract: str, name: str) -> None:
    html = fixture_page("elegant", "tariefkaarten.html")
    assert elegant._card_url(html, elegant._CONTRACTS_BY_ID[contract]) == (
        "https://www.datocms-assets.com/198110/" + name
    )


@pytest.mark.parametrize(
    ("contract", "name"),
    [
        ("elegant_flex", "1790833226-flexgas_residential.pdf"),
        ("elegant_comfortflex", "1790833226-comfortflexgas_residential.pdf"),
        ("elegant_zeker_vast", "1790833226-zekervastgas_residential.pdf"),
    ],
)
def test_listing_without_the_month_suffix(contract: str, name: str) -> None:
    """Since October 2026 the links name no month."""
    html = fixture_page("elegant", "tariefkaarten_2026-10.html")
    assert elegant._card_url(html, elegant._CONTRACTS_BY_ID[contract]) == (
        "https://www.datocms-assets.com/198110/" + name
    )


def test_the_last_upload_wins() -> None:
    old = "https://www.datocms-assets.com/198110/1788722319-flexgas_residential-0926.pdf"
    new = "https://www.datocms-assets.com/198110/1790833226-flexgas_residential.pdf"
    contract = elegant._CONTRACTS_BY_ID["elegant_flex"]
    assert elegant._card_url(f'"{new}" "{old}"', contract) == new
    assert elegant._card_url(f'"{old}" "{new}"', contract) == new


def test_october_flex_card() -> None:
    snap = elegant.parse_snapshot("elegant_flex", REGION_FLANDERS, _card(_FLEX_OCTOBER))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # "(c€/kWh) 8,68 Maandelijkse prijzen" at "september 2026. Deze bedroeg
    # 7,531 c€/kWh".
    assert energy.price == pytest.approx(0.0868)
    assert energy.at(75.31) == pytest.approx(0.0868, abs=5e-5)
    assert snap.publication_label == "2026-10"


async def test_fetch_reads_the_card_the_listing_links() -> None:
    fetched = AsyncMock(return_value=_card(_COMFORTFLEX))
    with (
        patch.object(
            elegant,
            "fetch_text",
            AsyncMock(return_value=fixture_page("elegant", "tariefkaarten.html")),
        ),
        patch.object(elegant, "fetch_pdf_text_layout", fetched),
    ):
        snap = await elegant.fetch(AsyncMock(), "elegant_comfortflex", REGION_FLANDERS)
    assert fetched.call_args.args[1] == "https://www.datocms-assets.com/198110/" + _COMFORTFLEX
    assert snap.source_url == fetched.call_args.args[1]
    assert snap.publication_label == "2026-09"


def _query(call: Any) -> Any:
    """The tRPC input a mocked fetch_text call sent."""
    params = call.kwargs["params"]
    assert params["batch"] == "1"
    return json.loads(params["input"])["0"]


async def test_fetch_for_month_asks_the_archive_for_the_first_of_the_month() -> None:
    search = AsyncMock(return_value=fixture_page("elegant", "archive_search_2026-03-15_gas.json"))
    chart = AsyncMock(return_value=_card(_ARCHIVED_FLEX))
    with (
        patch.object(elegant, "fetch_text", search),
        patch.object(elegant, "fetch_pdf_text_layout", chart),
    ):
        snap = await elegant.fetch_for_month(
            AsyncMock(), "elegant_flex", REGION_FLANDERS, date(2026, 3, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 3, 31)
    assert _query(search.call_args) == {
        "date": "2026-03-01",
        "energyType": "Gas",
        "customerType": "Residential",
    }
    offer = json.loads(fixture_page("elegant", "archive_search_2026-03-15_gas.json"))[0]
    path = offer["result"]["data"]["productOffers"][0]["tariffChartUrl"]
    assert "FlexGas_20260301" in path
    assert chart.call_args.args[1] == (
        "https://www.elegant.be/api/tarief-archief/tariff-chart?tariffChartPath="
        + quote(path, safe="")
    )


async def test_fetch_for_month_refuses_another_products_card() -> None:
    """Zeker & Vast's offer is found, but a Flex card behind it is refused."""
    chart = AsyncMock(return_value=_card(_ARCHIVED_FLEX))
    with (
        patch.object(
            elegant,
            "fetch_text",
            AsyncMock(return_value=fixture_page("elegant", "archive_search_2026-03-15_gas.json")),
        ),
        patch.object(elegant, "fetch_pdf_text_layout", chart),
    ):
        assert (
            await elegant.fetch_for_month(
                AsyncMock(), "elegant_zeker_vast", REGION_FLANDERS, date(2026, 3, 1)
            )
            is None
        )
    assert "ZekerVastGas_20260306" in chart.call_args.args[1]


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        patch.object(
            elegant,
            "fetch_text",
            AsyncMock(return_value=fixture_page("elegant", "archive_search_2026-03-15_gas.json")),
        ),
        patch.object(
            elegant, "fetch_pdf_text_layout", AsyncMock(return_value=_card(_ARCHIVED_FLEX))
        ),
    ):
        assert (
            await elegant.fetch_for_month(
                AsyncMock(), "elegant_flex", REGION_FLANDERS, date(2026, 4, 1)
            )
            is None
        )


async def test_fetch_for_month_skips_the_legacy_products() -> None:
    chart = AsyncMock()
    with (
        patch.object(
            elegant,
            "fetch_text",
            AsyncMock(return_value=fixture_page("elegant", "archive_search_2025-06-15_gas.json")),
        ),
        patch.object(elegant, "fetch_pdf_text_layout", chart),
    ):
        assert (
            await elegant.fetch_for_month(
                AsyncMock(), "elegant_flex", REGION_FLANDERS, date(2025, 6, 1)
            )
            is None
        )
    chart.assert_not_called()


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            elegant, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await elegant.fetch_for_month(
            AsyncMock(), "elegant_flex", REGION_FLANDERS, date(2026, 3, 1)
        )


def test_index_is_found_by_name() -> None:
    assert (
        elegant.parse_index_id(fixture_page("elegant", "energyExchanges.listIndexes_Gas.json")) == 2
    )


def test_index_rates_by_delivery_month() -> None:
    table = elegant.parse_rates(fixture_page("elegant", "energyExchanges.getRates_2.json"))
    values = table["TTFDAM"]
    # The values the September and March cards name, 6,166 and 3,318 c€/kWh.
    assert values["2026-08"] == pytest.approx(61.6623084)
    assert values["2026-02"] == pytest.approx(33.1830827)
    assert "2026-09" not in values


def test_index_rates_ignore_a_row_that_is_not_a_month() -> None:
    """The answer filtered on 2026 ends with a one-day row for 1 January
    carrying December's 27,65."""
    values = elegant.parse_rates(fixture_page("elegant", "energyExchanges.getRates_2_2026.json"))
    assert values["TTFDAM"]["2026-01"] == pytest.approx(34.2627172)
    assert len(values["TTFDAM"]) == 8


async def test_fetch_index_looks_the_id_up_first() -> None:
    answers = AsyncMock(
        side_effect=[
            fixture_page("elegant", "energyExchanges.listIndexes_Gas.json"),
            fixture_page("elegant", "energyExchanges.getRates_2.json"),
        ]
    )
    with patch.object(elegant, "fetch_text", answers):
        table = await elegant.fetch_index(AsyncMock())
    assert _query(answers.call_args_list[0]) == {"energyType": "Gas"}
    assert _query(answers.call_args_list[1]) == {"exchangeId": 2}
    assert table["TTFDAM"]["2026-08"] == pytest.approx(61.6623084)


def test_contracts() -> None:
    by_id = {c.id: c for c in elegant.EXTRACTOR.contracts}
    assert {c: by_id[c].kind for c in by_id} == {
        "elegant_flex": "indexed",
        "elegant_comfortflex": "indexed",
        "elegant_zeker_vast": "fixed",
    }
    assert by_id["elegant_zeker_vast"].label == "Elegant Zeker & Vast"
    assert all(c.regions == frozenset({REGION_FLANDERS}) for c in by_id.values())
