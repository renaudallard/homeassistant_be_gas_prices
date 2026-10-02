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

"""TotalEnergies gas card extractor, against the September 2026 cards."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_KEMPEN,
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
    TIER_T3,
)
from custom_components.be_gas_prices.providers import totalenergies
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError, SupplierSnapshot
from tests import approx, fixture_text

_HISTORY = "Historique-valeurs-parametres-gaz-produits-actuels-FR.pdf"
_LATEST = "https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/"
_ARCHIVE = "https://totalenergies.be/static/marketing-documents/b2c/tariff-card/"


def _card(name: str) -> str:
    return fixture_text("totalenergies", name, "layout")


def _mycomfort(region: str, name: str) -> SupplierSnapshot:
    return totalenergies.parse_snapshot("totalenergies_mycomfort_variable", region, _card(name))


def test_mycomfort_variable_is_priced_at_the_last_known_ttf() -> None:
    """The card prints a forward estimate ("7,41 Tarif mensuel") and the formula
    at the last known TTF_M_RLP ("Compteur Simple : 7,33"); the latter is kept."""
    snap = _mycomfort(REGION_WALLONIA, "MYCOMFORT_GAS_WAL_FR.pdf")
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF_M_RLP"
    assert not energy.settled
    assert energy.period == "month"
    assert energy.price == pytest.approx(0.0733)
    assert energy.yearly_fixed_fee == pytest.approx(90.0)
    # "0.1007 * TTF_M_RLP + 0.704" in c EUR/kWh excluding VAT, grossed up by
    # the card's 6%.
    assert energy.factor == pytest.approx(0.001007 * 1.06)
    assert energy.base == pytest.approx(0.00704 * 1.06)
    assert energy.formula == "0.1007 * TTF_M_RLP + 0.704"
    # The rotated side stamp reads "18 septembre 2025" backwards; the card
    # month is the heading's.
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)
    assert snap.taxes.vat_rate == 0.0
    assert snap.source_url == _LATEST + "MYCOMFORT_GAS_WAL_FR.pdf"


@pytest.mark.parametrize(
    ("region", "name", "price", "base"),
    [
        (REGION_FLANDERS, "MYCOMFORT_GAS_VL_FR.pdf", 0.0722, 0.00604),
        (REGION_BRUSSELS, "MYCOMFORT_GAS_BXL_FR.pdf", 0.0809, 0.0142),
    ],
)
def test_mycomfort_variable_base_differs_by_region(
    region: str, name: str, price: float, base: float
) -> None:
    energy = _mycomfort(region, name).energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(price)
    assert energy.factor == pytest.approx(0.001007 * 1.06)
    assert energy.base == pytest.approx(base * 1.06)
    assert energy.yearly_fixed_fee == pytest.approx(90.0)


@pytest.mark.parametrize(
    ("contract", "name", "price", "fee", "factor", "base"),
    [
        (
            "totalenergies_myessential_variable",
            "MYESSENTIAL_GAS_WAL_FR.pdf",
            0.0724,
            35.0,
            0.1009,
            0.604,
        ),
        ("totalenergies_gaz_variable", "GAZ-VARIABLE_GAS_WAL_FR.pdf", 0.0733, 100.0, 0.1007, 0.704),
        ("totalenergies_impact_variable", "IMPACT_GAS_WAL_FR.pdf", 0.0733, 100.0, 0.1007, 0.704),
    ],
)
def test_variable_products(
    contract: str, name: str, price: float, fee: float, factor: float, base: float
) -> None:
    snap = totalenergies.parse_snapshot(contract, REGION_WALLONIA, _card(name))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(price)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    assert energy.factor == pytest.approx(factor / 100 * 1.06)
    assert energy.base == pytest.approx(base / 100 * 1.06)
    assert snap.publication_label == "2026-09"


def test_october_gaz_variable_leaves_its_indicative_price_blank() -> None:
    """From October 2026 the card prints "Compteur Simple : € cent/kWh"
    with no figure, and its fee as "100": the estimate, "8,12 Tarif
    mensuel", is the only price it prints."""
    text = _card("2026_10_GAZ-VARIABLE_GAS_WAL_FR.pdf")
    snap = totalenergies.parse_snapshot("totalenergies_gaz_variable", REGION_WALLONIA, text)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(0.0812)
    assert energy.yearly_fixed_fee == pytest.approx(100.0)
    assert energy.factor == pytest.approx(0.1007 / 100 * 1.06)
    assert energy.base == pytest.approx(0.704 / 100 * 1.06)
    assert snap.publication_label == "2026-10"


def test_october_myessential_prints_its_estimate_with_a_dot() -> None:
    """ "8.03 Tarif mensuel" above "35", where the other cards print a comma."""
    text = _card("2026_10_MYESSENTIAL_GAS_WAL_FR.pdf")
    assert "8.03 Tarif mensuel" in text
    snap = totalenergies.parse_snapshot("totalenergies_myessential_variable", REGION_WALLONIA, text)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # The indicative price, "Compteur Simple : 8,69", is printed again.
    assert energy.price == pytest.approx(0.0869)
    assert energy.yearly_fixed_fee == pytest.approx(35.0)
    assert energy.factor == pytest.approx(0.1009 / 100 * 1.06)
    assert energy.base == pytest.approx(0.604 / 100 * 1.06)
    assert snap.publication_label == "2026-10"


@pytest.mark.parametrize(
    ("contract", "region", "name", "price", "fee"),
    [
        ("totalenergies_gaz_fixed", REGION_WALLONIA, "GAZ-FIXE_GAS_WAL_FR.pdf", 0.0845, 100.0),
        (
            "totalenergies_mycomfort_fixed",
            REGION_WALLONIA,
            "MYCOMFORT-FIXED_GAS_WAL_FR.pdf",
            0.0845,
            90.0,
        ),
        (
            "totalenergies_myessential_fixed",
            REGION_WALLONIA,
            "MYESSENTIAL-FIXED_GAS_WAL_FR.pdf",
            0.0835,
            35.0,
        ),
        (
            "totalenergies_mycomfort_fixed",
            REGION_FLANDERS,
            "MYCOMFORT-FIXED_GAS_VL_FR.pdf",
            0.0835,
            90.0,
        ),
        (
            "totalenergies_mycomfort_fixed",
            REGION_BRUSSELS,
            "MYCOMFORT-FIXED_GAS_BXL_FR.pdf",
            0.0921,
            90.0,
        ),
    ],
)
def test_fixed_cards(contract: str, region: str, name: str, price: float, fee: float) -> None:
    """The fee then the price on one row, "90,00 8,45 Tarif annuel". The Gaz
    Fixe stamp reads "février 2026" backwards, which must not date the card."""
    snap = totalenergies.parse_snapshot(contract, region, _card(name))
    assert snap.energy == FixedRates(price=approx(price), yearly_fixed_fee=fee)
    assert snap.publication_label == "2026-09"
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


@pytest.mark.parametrize(
    ("contract", "region", "name", "index_month"),
    [
        (
            "totalenergies_mycomfort_variable",
            REGION_WALLONIA,
            "MYCOMFORT_GAS_WAL_FR.pdf",
            "2026-08",
        ),
        ("totalenergies_mycomfort_variable", REGION_FLANDERS, "MYCOMFORT_GAS_VL_FR.pdf", "2026-08"),
        (
            "totalenergies_mycomfort_variable",
            REGION_BRUSSELS,
            "MYCOMFORT_GAS_BXL_FR.pdf",
            "2026-08",
        ),
        (
            "totalenergies_myessential_variable",
            REGION_WALLONIA,
            "MYESSENTIAL_GAS_WAL_FR.pdf",
            "2026-08",
        ),
        ("totalenergies_gaz_variable", REGION_WALLONIA, "GAZ-VARIABLE_GAS_WAL_FR.pdf", "2026-08"),
        ("totalenergies_impact_variable", REGION_WALLONIA, "IMPACT_GAS_WAL_FR.pdf", "2026-08"),
        (
            "totalenergies_mycomfort_variable",
            REGION_WALLONIA,
            "2026_8_MYCOMFORT_GAS_WAL_FR.pdf",
            "2026-07",
        ),
        (
            "totalenergies_gaz_variable",
            REGION_WALLONIA,
            "2026_3_GAZ-VARIABLE_GAS_WAL_FR.pdf",
            "2026-02",
        ),
    ],
)
def test_printed_price_is_the_formula_at_the_previous_month(
    contract: str, region: str, name: str, index_month: str
) -> None:
    """The indicative price is the card's own formula at the previous month's
    TTF_M_RLP as TotalEnergies publishes it, to the card's two decimals."""
    energy = totalenergies.parse_snapshot(contract, region, _card(name)).energy
    history = totalenergies.parse_index_history(_card(_HISTORY))
    assert isinstance(energy, IndexedRates)
    assert energy.at(history["TTF_M_RLP"][index_month]) == pytest.approx(energy.price, abs=5e-5)


def test_march_card_formula_spelling_and_stale_excise() -> None:
    """The March 2026 card wrote "0.1007*TTFM_RLP+0,67" and printed the excise
    of the time (0,87 / 0,96), which is read as printed."""
    snap = totalenergies.parse_snapshot(
        "totalenergies_gaz_variable", REGION_WALLONIA, _card("2026_3_GAZ-VARIABLE_GAS_WAL_FR.pdf")
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF_M_RLP"
    assert energy.base == pytest.approx(0.0067 * 1.06)
    assert energy.price == pytest.approx(0.0425)
    assert snap.publication_label == "2026-03"
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087)),
        (None, pytest.approx(0.0096)),
    )


def test_wallonia_table_collapses_the_ores_sub_areas() -> None:
    snap = _mycomfort(REGION_WALLONIA, "MYCOMFORT_GAS_WAL_FR.pdf")
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "RESA SA 4,64 2,53 2,24 34,59 122,05 962,74 0,17 0,00 0,01 0,11": all
    # proportional terms first, then the fixed ones.
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.0253)
    assert resa.tiers[TIER_T3].proportional == pytest.approx(0.0224)
    assert resa.tiers[TIER_T1].fixed_per_year == pytest.approx(34.59)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert resa.tiers[TIER_T3].fixed_per_year == pytest.approx(962.74)
    assert resa.transport == pytest.approx(0.0017)
    assert resa.metering_per_year == 0.0
    # "ORES (Namur - Namen) 4,29 2,21 1,64 31,91 140,93 889,48 ..." on all five
    # sub-area rows.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.0429)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T3].fixed_per_year == pytest.approx(889.48)


def test_wallonia_levies_are_read_as_printed() -> None:
    """The card rounds: excise 1,09 / 1,18, connection fee 0,01 c EUR/kWh, and
    it still prints the energy contribution at 0,11 in the DSO table."""
    taxes = _mycomfort(REGION_WALLONIA, "MYCOMFORT_GAS_WAL_FR.pdf").taxes
    assert taxes.excise_bands == ((12000.0, pytest.approx(0.0109)), (None, pytest.approx(0.0118)))
    assert taxes.energy_contribution == pytest.approx(0.0011)
    assert taxes.connection_fee == pytest.approx(0.0001)
    assert taxes.osp_by_caliber is None


def test_flanders_table_carries_the_data_management_fee() -> None:
    snap = _mycomfort(REGION_FLANDERS, "MYCOMFORT_GAS_VL_FR.pdf")
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Kempen 2,27 0,88 0,55 16,16 85,83 580,25 0,17 18,92 0,11"
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.0227)
    assert kempen.tiers[TIER_T2].proportional == pytest.approx(0.0088)
    assert kempen.tiers[TIER_T3].proportional == pytest.approx(0.0055)
    assert kempen.tiers[TIER_T1].fixed_per_year == pytest.approx(16.16)
    assert kempen.tiers[TIER_T2].fixed_per_year == pytest.approx(85.83)
    assert kempen.tiers[TIER_T3].fixed_per_year == pytest.approx(580.25)
    assert kempen.transport == pytest.approx(0.0017)
    assert kempen.metering_per_year == pytest.approx(18.92)
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.energy_contribution == pytest.approx(0.0011)
    assert snap.taxes.osp_by_caliber is None


def test_brussels_card_reads_the_per_meter_levy() -> None:
    snap = _mycomfort(REGION_BRUSSELS, "MYCOMFORT_GAS_BXL_FR.pdf")
    # "SIBELGA 1,99 1,45 0,81 15,90 43,07 1001,55 0,17 24,96 0,11"
    sibelga = snap.dsos[DSO_SIBELGA]
    assert sibelga.tiers[TIER_T1].proportional == pytest.approx(0.0199)
    assert sibelga.tiers[TIER_T2].proportional == pytest.approx(0.0145)
    assert sibelga.tiers[TIER_T3].proportional == pytest.approx(0.0081)
    assert sibelga.tiers[TIER_T1].fixed_per_year == pytest.approx(15.90)
    assert sibelga.tiers[TIER_T2].fixed_per_year == pytest.approx(43.07)
    assert sibelga.tiers[TIER_T3].fixed_per_year == pytest.approx(1001.55)
    assert sibelga.transport == pytest.approx(0.0017)
    assert sibelga.metering_per_year == pytest.approx(24.96)
    assert snap.taxes.energy_contribution == pytest.approx(0.0011)
    assert snap.taxes.connection_fee == 0.0
    # "<= 10 m³/h 5 3,56": the 5 and 6 are footnote marks, not amounts.
    assert snap.taxes.osp_by_caliber == {
        "q10_le5000": pytest.approx(3.56),
        "q10_gt5000": pytest.approx(12.59),
        "q16": pytest.approx(30.40),
        "q25": pytest.approx(75.18),
        "q40": pytest.approx(150.35),
        "q65": pytest.approx(376.00),
        "q100": pytest.approx(522.79),
        "q160": pytest.approx(671.36),
        "gt160": pytest.approx(970.41),
    }


def test_card_of_another_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="myComfort Variable"):
        totalenergies.parse_snapshot(
            "totalenergies_myessential_variable",
            REGION_WALLONIA,
            _card("MYCOMFORT_GAS_WAL_FR.pdf"),
        )


def test_card_of_another_region_is_refused() -> None:
    with pytest.raises(ExtractorError):
        _mycomfort(REGION_FLANDERS, "MYCOMFORT_GAS_WAL_FR.pdf")


def test_impact_is_not_sold_outside_wallonia() -> None:
    with pytest.raises(ExtractorError):
        totalenergies.parse_snapshot(
            "totalenergies_impact_variable", REGION_BRUSSELS, _card("IMPACT_GAS_WAL_FR.pdf")
        )


def test_unknown_contract_is_refused() -> None:
    with pytest.raises(ExtractorError):
        totalenergies.parse_snapshot(
            "totalenergies_mydynamic", REGION_WALLONIA, _card("MYCOMFORT_GAS_WAL_FR.pdf")
        )


@pytest.mark.parametrize(
    ("printed", "damaged"),
    [
        ("TVA % incluse", "TVA incluse"),
        ("Formule tarifaire", "Formule"),
        ("Tarif mensuel", "Tarif"),
        ("> 12.000 kWh", "plus de 12.000 kWh"),
        ("RESA SA", "RESA"),
    ],
)
def test_card_missing_a_mandatory_figure_fails_loud(printed: str, damaged: str) -> None:
    text = _card("MYCOMFORT_GAS_WAL_FR.pdf").replace(printed, damaged)
    with pytest.raises(ExtractorError):
        totalenergies.parse_snapshot("totalenergies_mycomfort_variable", REGION_WALLONIA, text)


def test_index_history_reads_the_three_indices_by_delivery_month() -> None:
    table = totalenergies.parse_index_history(_card(_HISTORY))
    assert set(table) == {"TTF_M_RLP", "ZTP_M_RLP", "ZTP_Q_RLP"}
    # "08/2026 61,662 61,824": the quarter is not over, so no ZTP_Q_RLP yet.
    assert table["TTF_M_RLP"]["2026-08"] == pytest.approx(61.662)
    assert table["ZTP_M_RLP"]["2026-08"] == pytest.approx(61.824)
    assert "2026-08" not in table["ZTP_Q_RLP"]
    # "06/2026 44,983 44,696 45,453": the quarterly value on each of its months.
    assert table["TTF_M_RLP"]["2026-06"] == pytest.approx(44.983)
    for month in ("2026-04", "2026-05", "2026-06"):
        assert table["ZTP_Q_RLP"][month] == pytest.approx(45.453)
    # From "06/2024 34,174 34,084 30,165" to August 2026, and no September yet.
    assert table["TTF_M_RLP"]["2024-06"] == pytest.approx(34.174)
    assert len(table["TTF_M_RLP"]) == 27
    assert "2026-09" not in table["TTF_M_RLP"]


def test_index_history_with_another_column_order_is_refused() -> None:
    text = _card(_HISTORY).replace("TTF_M_RLP* ZTP_M_RLP*", "ZTP_M_RLP* TTF_M_RLP*")
    with pytest.raises(ExtractorError):
        totalenergies.parse_index_history(text)


def test_regions_follow_the_cards_totalenergies_publishes() -> None:
    by_id = {c.id: c for c in totalenergies.EXTRACTOR.contracts}
    assert by_id["totalenergies_impact_variable"].regions == frozenset({REGION_WALLONIA})
    assert by_id["totalenergies_mycomfort_variable"].regions == frozenset(REGIONS)
    assert by_id["totalenergies_mycomfort_variable"].label == "TotalEnergies myComfort Variable"
    assert by_id["totalenergies_gaz_fixed"].kind == "fixed"


async def test_fetch_reads_the_latest_card_of_the_region() -> None:
    text = _card("MYCOMFORT_GAS_VL_FR.pdf")
    with patch.object(totalenergies, "fetch_pdf_text_layout", AsyncMock(return_value=text)) as got:
        snap = await totalenergies.fetch(
            AsyncMock(), "totalenergies_mycomfort_variable", REGION_FLANDERS
        )
    assert got.call_args.args[1] == _LATEST + "MYCOMFORT_GAS_VL_FR.pdf"
    assert snap.source_url == _LATEST + "MYCOMFORT_GAS_VL_FR.pdf"


async def test_probe_heads_the_latest_card() -> None:
    with patch.object(
        totalenergies, "head_freshness_key", AsyncMock(return_value="Tue, 01 Sep 2026")
    ) as head:
        key = await totalenergies.probe(AsyncMock(), "totalenergies_gaz_fixed", REGION_BRUSSELS)
        assert key == "Tue, 01 Sep 2026"
        assert head.call_args.args[1] == _LATEST + "GAZ-FIXE_GAS_BXL_FR.pdf"
        assert (
            await totalenergies.probe(AsyncMock(), "totalenergies_impact_variable", REGION_FLANDERS)
            is None
        )
        assert head.call_count == 1


async def test_fetch_for_month_asks_for_the_unpadded_month() -> None:
    text = _card("2026_8_MYCOMFORT_GAS_WAL_FR.pdf")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(totalenergies, "fetch_pdf_text_layout", AsyncMock(return_value=text)) as got,
    ):
        snap = await totalenergies.fetch_for_month(
            AsyncMock(), "totalenergies_mycomfort_variable", REGION_WALLONIA, date(2026, 8, 1)
        )
    assert got.call_args.args[1] == _ARCHIVE + "2026_8_MYCOMFORT_GAS_WAL_FR.pdf"
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert snap.source_url == _ARCHIVE + "2026_8_MYCOMFORT_GAS_WAL_FR.pdf"
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(0.0641)


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    text = _card("MYCOMFORT_GAS_WAL_FR.pdf")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(totalenergies, "fetch_pdf_text_layout", AsyncMock(return_value=text)),
    ):
        assert (
            await totalenergies.fetch_for_month(
                AsyncMock(), "totalenergies_mycomfort_variable", REGION_WALLONIA, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            totalenergies,
            "fetch_pdf_text_layout",
            AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x")),
        ),
        pytest.raises(ExtractorError),
    ):
        await totalenergies.fetch_for_month(
            AsyncMock(), "totalenergies_gaz_fixed", REGION_WALLONIA, date(2026, 8, 1)
        )


async def test_fetch_for_month_treats_the_soft_404_as_no_card() -> None:
    """A month the archive does not hold answers 200 with an HTML page."""
    soft_404 = ExtractorError("expected a PDF at x, payload starts with b'<!DOCTYPE html>'")
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(totalenergies, "fetch_pdf_text_layout", AsyncMock(side_effect=soft_404)),
    ):
        assert (
            await totalenergies.fetch_for_month(
                AsyncMock(), "totalenergies_gaz_fixed", REGION_WALLONIA, date(2025, 1, 1)
            )
            is None
        )


async def test_fetch_for_month_follows_the_brussels_clock() -> None:
    """At 00:30 on 1 October in Brussels a UTC host is still in September. The
    October card may be asked for, and the September one it gets is refused."""
    text = _card("MYCOMFORT_GAS_WAL_FR.pdf")
    with (
        freeze_time(datetime(2026, 9, 30, 22, 30)),
        patch.object(totalenergies, "fetch_pdf_text_layout", AsyncMock(return_value=text)) as got,
    ):
        snap = await totalenergies.fetch_for_month(
            AsyncMock(), "totalenergies_mycomfort_variable", REGION_WALLONIA, date(2026, 10, 1)
        )
    assert got.call_args.args[1] == _ARCHIVE + "2026_10_MYCOMFORT_GAS_WAL_FR.pdf"
    assert snap is None


async def test_fetch_for_month_does_not_ask_for_the_future_or_an_unsold_region() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(totalenergies, "fetch_pdf_text_layout", AsyncMock()) as got,
    ):
        assert (
            await totalenergies.fetch_for_month(
                AsyncMock(), "totalenergies_gaz_fixed", REGION_WALLONIA, date(2026, 10, 1)
            )
            is None
        )
        assert (
            await totalenergies.fetch_for_month(
                AsyncMock(), "totalenergies_impact_variable", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )
    got.assert_not_called()
