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

"""Eneco gas card extractor, against the September 2026 cards and the
archive issues back to December 2024."""

from __future__ import annotations

from datetime import date, datetime
from functools import cache
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_MIDDEN_VLAANDEREN,
    DSO_ORES,
    DSO_RESA,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.providers import eneco
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import (
    ExtractorError,
    IndexTable,
    SupplierSnapshot,
)
from tests import FIXTURES, approx, fixture_page, fixture_text

_CDN = "https://cdn.eneco.be/downloads/nl/general/tk"
_FLEX_2609 = "BC_032_012609_NL_ENECO_GAS_FLEX.pdf"
_FLEX_ONE_2609 = "BC_032_012609_NL_ENECO_GAS_FLEX_ONE.pdf"
_FIX_2609 = "BC_032_012609_NL_ENECO_GAS_FIX.pdf"
_FLEX_2608 = "BC_032_012608_NL_ENECO_GAS_FLEX.pdf"
_FLEX_2604 = "BC_032_012604_NL_ENECO_GAS_FLEX.pdf"
_FLEX_2412 = "BC_032_012412_NL_ENECO_GAS_FLEX.pdf"
_INDEX_PDF = "indexatieparameters-aardgas.pdf"


def _card(name: str) -> str:
    return fixture_text("eneco", name)


def _parse(contract: str, region: str, name: str) -> SupplierSnapshot:
    return eneco.parse_snapshot(contract, region, _card(name), f"{_CDN}/{name}")


@cache
def _index_text() -> str:
    return eneco.index_table_text((FIXTURES / "eneco" / _INDEX_PDF).read_bytes())


@cache
def _index_table() -> IndexTable:
    return eneco.parse_index_text(_index_text())


@pytest.mark.parametrize("region", [REGION_FLANDERS, REGION_WALLONIA])
def test_flex_is_priced_at_the_last_known_index(region: str) -> None:
    """TTFDAW-RLP-M is only known once the month is over, so the Maandprijs
    is the formula at the last value, footnoted "08/2026: €61,6374/MWh"."""
    snap = _parse("eneco_aardgas_flex", region, _FLEX_2609)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTFDAW-RLP-M"
    assert not energy.settled
    assert energy.period == "month"
    assert energy.price == pytest.approx(0.0767)
    assert energy.yearly_fixed_fee == pytest.approx(65.0)
    # "(0,1 X TTFDAW-RLP-M + 1,074) X 1,06" in c EUR/kWh: the printed 1,06
    # is applied once, not grossed up again.
    assert energy.formula == "(0,1 X TTFDAW-RLP-M + 1,074) X 1,06"
    assert energy.factor == pytest.approx(0.001 * 1.06)
    assert energy.base == pytest.approx(0.01074 * 1.06)
    assert energy.at(61.6374) == pytest.approx(0.0767, abs=5e-5)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.source_url == f"{_CDN}/{_FLEX_2609}"
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)
    assert snap.taxes.vat_rate == 0.0


def test_flex_one_has_its_own_base() -> None:
    """Same layout, factor and fee as Flex, a lower base: "(0,1 X
    TTFDAW-RLP-M + 0,437) X 1,06", 7,00 at the same footnoted value."""
    energy = _parse("eneco_aardgas_flex_one", REGION_FLANDERS, _FLEX_ONE_2609).energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(0.001 * 1.06)
    assert energy.base == pytest.approx(0.00437 * 1.06)
    assert energy.price == pytest.approx(0.07)
    assert energy.yearly_fixed_fee == pytest.approx(65.0)
    assert energy.at(61.6374) == pytest.approx(0.07, abs=5e-5)


def test_vast_is_fixed() -> None:
    # "65,00 9,13" under "VASTE VERGOEDING (€/jaar) VERBRUIK (€cent/kWh)".
    snap = _parse("eneco_aardgas_vast", REGION_WALLONIA, _FIX_2609)
    assert snap.energy == FixedRates(price=approx(0.0913), yearly_fixed_fee=65.0)
    assert snap.publication_label == "2026-09"


@pytest.mark.parametrize(
    ("contract", "name"),
    [
        ("eneco_aardgas_flex", _FLEX_ONE_2609),
        ("eneco_aardgas_flex_one", _FLEX_2609),
        ("eneco_aardgas_flex", _FIX_2609),
    ],
)
def test_card_of_another_product_is_refused(contract: str, name: str) -> None:
    """Flex and Flex One print the same layout, so only the title tells them
    apart."""
    with pytest.raises(ExtractorError, match="titled"):
        _parse(contract, REGION_FLANDERS, name)


def test_flanders_rows_include_the_wrapped_label() -> None:
    snap = _parse("eneco_aardgas_flex", REGION_FLANDERS, _FLEX_2609)
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "FLUVIUS MIDDEN VLAANDEREN" / "(INTERGEM) 16,33 2,32 86,71 0,91 18,92",
    # the one label pypdf wraps.
    midden = snap.dsos[DSO_FLUVIUS_MIDDEN_VLAANDEREN]
    assert midden.tiers[TIER_T1].fixed_per_year == pytest.approx(16.33)
    assert midden.tiers[TIER_T1].proportional == pytest.approx(0.0232)
    assert midden.tiers[TIER_T2].fixed_per_year == pytest.approx(86.71)
    assert midden.tiers[TIER_T2].proportional == pytest.approx(0.0091)
    assert midden.metering_per_year == pytest.approx(18.92)
    # Transport is the Fluxys note: "0,165 €cent/kWh (incl. btw)".
    assert midden.transport == pytest.approx(0.00165)
    assert set(midden.tiers) == {TIER_T1, TIER_T2}
    # "FLUVIUS HALLE VILVOORDE 17,59 2,50 93,38 0,98 18,92"
    halle = snap.dsos[DSO_FLUVIUS_HALLE_VILVOORDE]
    assert halle.tiers[TIER_T1].fixed_per_year == pytest.approx(17.59)
    assert halle.tiers[TIER_T1].proportional == pytest.approx(0.025)
    assert halle.tiers[TIER_T2].fixed_per_year == pytest.approx(93.38)
    assert halle.tiers[TIER_T2].proportional == pytest.approx(0.0098)
    # "FLUVIUS KEMPEN (IVEKA) 16,17 2,27 85,83 0,88 18,92" is Fluvius Kempen.
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.0227)
    assert kempen.tiers[TIER_T2].fixed_per_year == pytest.approx(85.83)
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_wallonia_table_collapses_the_ores_sub_areas() -> None:
    snap = _parse("eneco_aardgas_flex", REGION_WALLONIA, _FLEX_2609)
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "ORES (Namur) 31,91 4,29 140,93 2,21 -": no metering in Wallonia.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.0429)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.0221)
    assert ores.metering_per_year == 0.0
    assert ores.transport == pytest.approx(0.00165)
    # "TECTEO RESA 34,59 4,64 122,05 2,53 -"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].fixed_per_year == pytest.approx(34.59)
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.0253)


def test_wallonia_levies() -> None:
    snap = _parse("eneco_aardgas_flex", REGION_WALLONIA, _FLEX_2609)
    # "Aansluitingsvergoeding < 1 GWh 0,0075 €cent/kWh", VAT exempt.
    assert snap.taxes.connection_fee == pytest.approx(0.000075)
    # "Verbruik tussen 0 en 12.000 kWh 1,0929 0,0000" and "Verbruik >
    # 12.000 kWh 1,1830 0,0000".
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.010929)),
        (None, pytest.approx(0.01183)),
    )
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_pre_august_levies_are_read_as_printed() -> None:
    """The April 2026 card prints the excise and energy contribution of its
    time; the law override corrects a delivery month later, not the parser."""
    taxes = _parse("eneco_aardgas_flex", REGION_WALLONIA, _FLEX_2604).taxes
    # "Verbruik tussen 0 en 12.000 kWh 0,8724 0,1058", "> 12.000 kWh 0,9864
    # 0,1058".
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.008724)),
        (None, pytest.approx(0.009864)),
    )
    assert taxes.energy_contribution == pytest.approx(0.001058)


def test_card_without_the_walloon_connection_fee_fails_loud() -> None:
    text = _card(_FLEX_2609).replace("Aansluitingsvergoeding", "Aansluiting")
    with pytest.raises(ExtractorError, match="connection fee"):
        eneco.parse_snapshot("eneco_aardgas_flex", REGION_WALLONIA, text, "")
    # A Flemish household pays no such fee, so its card still reads.
    snap = eneco.parse_snapshot("eneco_aardgas_flex", REGION_FLANDERS, text, "")
    assert snap.taxes.connection_fee == 0.0


def test_card_without_the_transport_note_fails_loud() -> None:
    text = _card(_FLEX_2609).replace("raamt Fluxys", "raamt")
    with pytest.raises(ExtractorError, match="transport"):
        eneco.parse_snapshot("eneco_aardgas_flex", REGION_FLANDERS, text, "")


def test_card_that_lost_its_formula_fails_loud() -> None:
    text = _card(_FLEX_2609).replace("TTFDAW-RLP-M +", "TTF +")
    with pytest.raises(ExtractorError, match="formula"):
        eneco.parse_snapshot("eneco_aardgas_flex", REGION_FLANDERS, text, "")


def test_pre_2025_card_reads_for_wallonia_only() -> None:
    """The December 2024 card still lists Gaselwest, Iveka and the other
    Fluvius DSOs that merged away, so no Flemish household can be priced on
    it. Its Walloon rows and its ">" bulleted energy block read."""
    with pytest.raises(ExtractorError, match="fluvius_halle_vilvoorde"):
        _parse("eneco_aardgas_flex", REGION_FLANDERS, _FLEX_2412)
    snap = _parse("eneco_aardgas_flex", REGION_WALLONIA, _FLEX_2412)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # "5,84 > Maandprijs", "(0,1 X TTFDAW-RLP-M + 1,098) X 1,06", footnoted
    # "11/2024: €44,1632/MWh", which prices 5,8452: the card rounds it down.
    assert energy.price == pytest.approx(0.0584)
    assert energy.base == pytest.approx(0.01098 * 1.06)
    assert energy.at(44.1632) == pytest.approx(0.0584, abs=6e-5)
    # "ORES (Brabant Wallon) 25,74 3,80 104,68 1,86 -" and "TECTEO - RESA
    # 33,37 3,71 117,77 2,02 -", with 2024's "0,162 €cent/kWh" transport.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(25.74)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.0186)
    assert ores.transport == pytest.approx(0.00162)
    assert snap.dsos[DSO_RESA].tiers[TIER_T2].fixed_per_year == pytest.approx(117.77)
    assert snap.publication_label == "2024-12"


def test_brussels_is_refused() -> None:
    with pytest.raises(ExtractorError, match="not sold"):
        _parse("eneco_aardgas_flex", REGION_BRUSSELS, _FLEX_2609)


def test_unknown_contract_is_refused() -> None:
    with pytest.raises(ExtractorError):
        _parse("eneco_power_flex", REGION_FLANDERS, _FLEX_2609)


def test_contracts_are_sold_in_flanders_and_wallonia() -> None:
    by_id = {c.id: c for c in eneco.EXTRACTOR.contracts}
    assert {c: by_id[c].kind for c in by_id} == {
        "eneco_aardgas_vast": "fixed",
        "eneco_aardgas_flex": "indexed",
        "eneco_aardgas_flex_one": "indexed",
    }
    assert all(c.regions == {REGION_FLANDERS, REGION_WALLONIA} for c in by_id.values())


def test_listing_links_each_product_once() -> None:
    listing = fixture_page("eneco", "listing_nl.html")
    assert eneco.card_url(listing, "eneco_aardgas_flex") == f"{_CDN}/{_FLEX_2609}"
    assert eneco.card_url(listing, "eneco_aardgas_flex_one") == f"{_CDN}/{_FLEX_ONE_2609}"
    assert eneco.card_url(listing, "eneco_aardgas_vast") == f"{_CDN}/{_FIX_2609}"
    with pytest.raises(ExtractorError, match="GAS_FLEX_ONE"):
        eneco.card_url(listing.replace("GAS_FLEX_ONE", "GAS_X"), "eneco_aardgas_flex_one")


async def test_fetch_reads_the_card_the_listing_links() -> None:
    with (
        patch.object(
            eneco, "fetch_text", AsyncMock(return_value=fixture_page("eneco", "listing_nl.html"))
        ),
        patch.object(
            eneco, "fetch_pdf_text", AsyncMock(return_value=_card(_FLEX_ONE_2609))
        ) as fetched,
    ):
        snap = await eneco.fetch(AsyncMock(), "eneco_aardgas_flex_one", REGION_WALLONIA)
    assert fetched.call_args.args[1] == f"{_CDN}/{_FLEX_ONE_2609}"
    assert snap.source_url == f"{_CDN}/{_FLEX_ONE_2609}"
    assert snap.contract == "eneco_aardgas_flex_one"


async def test_fetch_for_month_builds_the_issue_url() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(eneco, "fetch_pdf_text", AsyncMock(return_value=_card(_FLEX_2608))) as fetched,
    ):
        snap = await eneco.fetch_for_month(
            AsyncMock(), "eneco_aardgas_flex", REGION_FLANDERS, date(2026, 8, 1)
        )
    assert fetched.call_args.args[1] == f"{_CDN}/{_FLEX_2608}"
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert snap.source_url == f"{_CDN}/{_FLEX_2608}"


async def test_fetch_for_month_takes_the_month_in_brussels_time() -> None:
    """Half past midnight on 1 September in Brussels is still 31 August in
    UTC. September is this month, not one ahead, so its card is asked for."""
    with (
        freeze_time(datetime(2026, 8, 31, 22, 30)),
        patch.object(eneco, "fetch_pdf_text", AsyncMock(return_value=_card(_FLEX_2609))) as fetched,
    ):
        snap = await eneco.fetch_for_month(
            AsyncMock(), "eneco_aardgas_flex", REGION_WALLONIA, date(2026, 9, 1)
        )
    assert snap is not None
    assert fetched.call_args.args[1] == f"{_CDN}/{_FLEX_2609}"


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(eneco, "fetch_pdf_text", AsyncMock(return_value=_card(_FLEX_2609))),
    ):
        assert (
            await eneco.fetch_for_month(
                AsyncMock(), "eneco_aardgas_flex", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            eneco, "fetch_pdf_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await eneco.fetch_for_month(
            AsyncMock(), "eneco_aardgas_flex", REGION_FLANDERS, date(2026, 8, 1)
        )


async def test_fetch_for_month_before_a_product_existed_is_no_card() -> None:
    """Flex One was first issued in September 2026: August answers 404."""
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            eneco, "fetch_pdf_text", AsyncMock(side_effect=ExtractorError("HTTP 404 fetching x"))
        ) as fetched,
    ):
        assert (
            await eneco.fetch_for_month(
                AsyncMock(), "eneco_aardgas_flex_one", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )
    assert fetched.call_args.args[1] == f"{_CDN}/BC_032_012608_NL_ENECO_GAS_FLEX_ONE.pdf"


async def test_fetch_for_month_does_not_ask_for_the_future_or_brussels() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(eneco, "fetch_pdf_text", AsyncMock()) as fetched,
    ):
        assert (
            await eneco.fetch_for_month(
                AsyncMock(), "eneco_aardgas_flex", REGION_FLANDERS, date(2026, 10, 1)
            )
            is None
        )
        assert (
            await eneco.fetch_for_month(
                AsyncMock(), "eneco_aardgas_flex", REGION_BRUSSELS, date(2026, 8, 1)
            )
            is None
        )
    fetched.assert_not_awaited()


def test_index_pdf_places_each_figure_under_its_heading() -> None:
    """Rows with empty cells print fewer figures than there are columns:
    "Juli 2026 52,99 45,11 45,75" is TTFDAW-RLP-M, TTF103 and TTF303."""
    lines = _index_text().splitlines()
    assert lines[0] == "TTF-DAHW TTFDAW-RLP TTFDAW-RLP-M ZTP-S41 ZTP-S31 TTF103 TTF303"
    assert "Juli 2026 - - 52,99 - - 45,11 45,75" in lines
    assert "Januari 2023 54,06 55,18 - 54,31 54,31 119,23 135,49" in lines
    table = _index_table()
    monthly = table["TTFDAW-RLP-M"]
    assert monthly["2026-08"] == pytest.approx(61.64)
    assert monthly["2026-07"] == pytest.approx(52.99)
    assert monthly["2023-03"] == pytest.approx(44.79)
    # Not yet published: September 2026, and the months before the index.
    assert "2026-09" not in monthly
    assert "2023-02" not in monthly
    assert table["TTF103"]["2026-09"] == pytest.approx(45.11)
    assert table["TTF303"]["2026-09"] == pytest.approx(45.75)
    assert table["ZTP-S41"]["2026-06"] == pytest.approx(45.63)
    assert "2026-07" not in table["TTF-DAHW"]


def test_published_index_reproduces_the_cards() -> None:
    """The indexation PDF rounds to two decimals what the card footnotes to
    four (61,64 for 61,6374), and prices the month the same to the cent."""
    monthly = _index_table()["TTFDAW-RLP-M"]
    for name, month, price in ((_FLEX_2609, "2026-08", 0.0767), (_FLEX_2608, "2026-07", 0.0676)):
        energy = _parse("eneco_aardgas_flex", REGION_FLANDERS, name).energy
        assert isinstance(energy, IndexedRates)
        assert energy.at(monthly[month]) == pytest.approx(price, abs=5e-5)


def test_index_text_without_the_monthly_column_fails_loud() -> None:
    with pytest.raises(ExtractorError):
        eneco.parse_index_text(_index_text().replace("TTFDAW-RLP-M", "TTFDAW-RLP-Q"))


async def test_fetch_index_reads_the_indexation_pdf() -> None:
    with patch.object(
        eneco, "fetch_pdf_rendered", AsyncMock(return_value=_index_text())
    ) as fetched:
        table = await eneco.fetch_index(AsyncMock())
    assert fetched.call_args.args[1] == (
        "https://cdn.eneco.be/downloads/nl/b2c/acq/indexatieparameters-aardgas.pdf"
    )
    assert fetched.call_args.kwargs["render"] is eneco.index_table_text
    assert table["TTFDAW-RLP-M"]["2026-08"] == pytest.approx(61.64)
