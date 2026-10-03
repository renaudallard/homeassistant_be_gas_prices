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

"""EnergyVision and Brusol gas card extractor, against the 2026 cards."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
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
from custom_components.be_gas_prices.providers import energyvision
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers._resolve import resolve_for_delivery
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import approx, fixture_page, fixture_text

_GSG = "energyvision_gas"
_GS1JVG = "energyvision_gas_1_jaar_vast"


def _card(name: str) -> str:
    return fixture_text("energyvision", name, "layout")


def test_variable_card_prints_the_vnr_estimate() -> None:
    snap = energyvision.parse_snapshot(_GSG, REGION_FLANDERS, _card("EV-0926-GSG-nl.pdf"))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTP-RLP-M"
    assert not energy.settled
    # "Fossiel gas – variabel 7,20€cent/kWh", "Vaste vergoeding 50 €/jaar".
    assert energy.price == pytest.approx(0.072)
    assert energy.yearly_fixed_fee == pytest.approx(50.0)
    # "1,02 x ZTP-RLP-M + 4,5 EUR/MWh", excluding VAT, grossed by the 6%.
    assert energy.factor == pytest.approx(1.02 * 1.06 / 1000)
    assert energy.base == pytest.approx(4.5 * 1.06 / 1000)
    # The printed price is the formula at the VNR estimate the parameter
    # document gives beside August (62,209), not at August's realised 61,822.
    assert energy.at(62.209) == pytest.approx(0.0720, abs=5e-5)
    assert energy.at(61.822) == pytest.approx(0.0716, abs=5e-5)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_fixed_card() -> None:
    snap = energyvision.parse_snapshot(_GS1JVG, REGION_FLANDERS, _card("EV-0926-GS1JVG-nl.pdf"))
    # "Fossiel gas – vast 7,96€cent/kWh", "Vaste vergoeding 75 €/jaar".
    assert snap.energy == FixedRates(price=approx(0.0796), yearly_fixed_fee=75.0)


def test_flanders_table_and_levies() -> None:
    snap = energyvision.parse_snapshot(_GSG, REGION_FLANDERS, _card("EV-0926-GSG-nl.pdf"))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "FLUVIUS ANTWERPEN 15,68 2,25641 83,22 0,90569 562,63 0,58608 18,92 0,165"
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0225641)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.0090569)
    assert antwerpen.tiers[TIER_T3].fixed_per_year == pytest.approx(562.63)
    assert antwerpen.tiers[TIER_T3].proportional == pytest.approx(0.0058608)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    assert antwerpen.transport == pytest.approx(0.00165)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_wallonia_table_is_proportional_first() -> None:
    snap = energyvision.parse_snapshot(_GSG, REGION_WALLONIA, _card("EV-0926-GSG-WAL-fr.pdf"))
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "BRABANT WALLON 4,28 31,91 2,20 140,93 1,63 889,48 0,165", the same on
    # all five ORES rows; the CWaPE grid says 4,28944, the card cuts it.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.0428)
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.022)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T3].proportional == pytest.approx(0.0163)
    assert ores.tiers[TIER_T3].fixed_per_year == pytest.approx(889.48)
    assert ores.transport == pytest.approx(0.00165)
    assert ores.metering_per_year == 0.0
    # "TECTEO RESA 4,63 34,59 2,52 122,05 2,24 962,74 0,165"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0463)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert snap.energy.price == pytest.approx(0.072)


def test_wallonia_connection_fee_is_read_as_printed() -> None:
    """ "Redevance de raccordement 0,07500 €cent/kWh": ten times the
    regulated 0,0075, a card error kept as printed."""
    taxes = energyvision.parse_snapshot(
        _GSG, REGION_WALLONIA, _card("EV-0926-GSG-WAL-fr.pdf")
    ).taxes
    assert taxes.connection_fee == pytest.approx(0.00075)
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )


def test_wallonia_fixed_card_prints_a_single_excise() -> None:
    """ "Accise spéciale 4,876 €cent/kWh" in place of the two gas bands."""
    snap = energyvision.parse_snapshot(_GS1JVG, REGION_WALLONIA, _card("EV-0926-GS1JVG-WAL-fr.pdf"))
    assert snap.energy == FixedRates(price=approx(0.0796), yearly_fixed_fee=75.0)
    assert snap.taxes.excise_bands == ((None, pytest.approx(0.04876)),)
    assert snap.taxes.connection_fee == pytest.approx(0.00075)


def test_the_walloon_connection_fee_is_billed_at_the_law() -> None:
    """The card's 0,075 c EUR/kWh is the low-voltage electricity rate; the
    Walloon order of 19 June 2003 sets 0,0075 for gas."""
    snap = energyvision.parse_snapshot(_GS1JVG, REGION_WALLONIA, _card("EV-0926-GS1JVG-WAL-fr.pdf"))
    billed = resolve_for_delivery(snap, date(2026, 9, 1))
    assert billed.taxes.connection_fee == pytest.approx(0.000075)
    # Past the months the law is known for, the card is read as printed.
    assert resolve_for_delivery(snap, date(2027, 1, 1)).taxes.connection_fee == pytest.approx(
        0.00075
    )


def test_brussels_card_reads_sibelga_and_the_levy() -> None:
    snap = energyvision.parse_snapshot(
        _GSG, REGION_BRUSSELS, _card("brusol/EV-0926-GSG-BXL-nl.pdf")
    )
    assert set(snap.dsos) == {DSO_SIBELGA}
    # "SIBELGA 1,99 15,90 1,45 43,07 0,81 1.001,55 0,165 24,96"
    sibelga = snap.dsos[DSO_SIBELGA]
    assert sibelga.tiers[TIER_T1].proportional == pytest.approx(0.0199)
    assert sibelga.tiers[TIER_T1].fixed_per_year == pytest.approx(15.90)
    assert sibelga.tiers[TIER_T2].proportional == pytest.approx(0.0145)
    assert sibelga.tiers[TIER_T2].fixed_per_year == pytest.approx(43.07)
    assert sibelga.tiers[TIER_T3].proportional == pytest.approx(0.0081)
    assert sibelga.tiers[TIER_T3].fixed_per_year == pytest.approx(1001.55)
    assert sibelga.transport == pytest.approx(0.00165)
    assert sibelga.metering_per_year == pytest.approx(24.96)
    # "<= 10 m3/h 2 3,56": the footnote digit is not the amount.
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
    assert snap.energy.price == pytest.approx(0.072)
    assert snap.taxes.connection_fee == 0.0


def test_first_brusol_upload_of_august_has_the_old_levies() -> None:
    snap = energyvision.parse_snapshot(
        _GSG, REGION_BRUSSELS, _card("brusol/2026-07/EV-0826-GSG-BXL-nl.pdf")
    )
    assert snap.publication_label == "2026-08"
    # "Energiebijdrage 0,10577", "Verbruik boven 12.000 kWh 0,96229".
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0096229)),
    )


def test_first_walloon_upload_of_august_prints_the_regulated_fee() -> None:
    snap = energyvision.parse_snapshot(_GSG, REGION_WALLONIA, _card("EV-0826-GSG-WAL-fr.pdf"))
    assert snap.publication_label == "2026-08"
    assert snap.energy.price == pytest.approx(0.0645)
    assert snap.taxes.connection_fee == pytest.approx(0.000075)
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)
    # "Consommation au-dessus de 12 000 kWh 0,96229"
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0096229)),
    )


def test_walloon_card_without_its_connection_fee_is_refused() -> None:
    """The re-upload of August the history page links has no fee row."""
    with pytest.raises(ExtractorError):
        energyvision.parse_snapshot(_GSG, REGION_WALLONIA, _card("EV-0826-GSG-WAL-fr_0.pdf"))


def test_card_of_the_other_product_is_refused() -> None:
    with pytest.raises(ExtractorError):
        energyvision.parse_snapshot(_GS1JVG, REGION_FLANDERS, _card("EV-0926-GSG-nl.pdf"))
    with pytest.raises(ExtractorError):
        energyvision.parse_snapshot(_GSG, REGION_FLANDERS, _card("EV-0926-GS1JVG-nl.pdf"))


def test_card_of_another_region_is_refused() -> None:
    with pytest.raises(ExtractorError):
        energyvision.parse_snapshot(_GSG, REGION_WALLONIA, _card("EV-0926-GSG-nl.pdf"))
    with pytest.raises(ExtractorError):
        energyvision.parse_snapshot(_GS1JVG, REGION_BRUSSELS, _card("EV-0926-GS1JVG-nl.pdf"))


_EV_FILES = "https://www.energyvision.be/sites/default/files/inline-files/"


@pytest.mark.parametrize(
    ("contract", "region", "page", "url", "card", "month"),
    [
        (
            _GSG,
            REGION_FLANDERS,
            "tariefkaart.html",
            _EV_FILES + "EV-0926-GSG-nl.pdf",
            "EV-0926-GSG-nl.pdf",
            "2026-09",
        ),
        (
            _GSG,
            REGION_WALLONIA,
            "tariefkaart.html",
            _EV_FILES + "EV-0926-GSG-WAL-fr.pdf",
            "EV-0926-GSG-WAL-fr.pdf",
            "2026-09",
        ),
        (
            _GS1JVG,
            REGION_WALLONIA,
            "tariefkaart.html",
            _EV_FILES + "EV-0926-GS1JVG-WAL-fr.pdf",
            "EV-0926-GS1JVG-WAL-fr.pdf",
            "2026-09",
        ),
        (
            _GSG,
            REGION_BRUSSELS,
            "brusol/signup-goedkope-stroom-nl.html",
            "https://www.brusol.be/sites/default/files/2026-08/EV-0926-GSG-BXL-nl.pdf",
            "brusol/EV-0926-GSG-BXL-nl.pdf",
            "2026-09",
        ),
        # The October page ends the card's href in a space.
        (
            _GSG,
            REGION_BRUSSELS,
            "brusol/signup-goedkope-stroom-nl-2026-10.html",
            "https://www.brusol.be/sites/default/files/2026-09/EV-1026-GSG-BXL-nl.pdf",
            "brusol/EV-1026-GSG-BXL-nl.pdf",
            "2026-10",
        ),
    ],
)
async def test_fetch_reads_the_card_the_page_links(
    contract: str, region: str, page: str, url: str, card: str, month: str
) -> None:
    listing = AsyncMock(return_value=fixture_page("energyvision", page))
    fetched = AsyncMock(return_value=_card(card))
    with (
        patch.object(energyvision, "fetch_text", listing),
        patch.object(energyvision, "fetch_pdf_text_layout", fetched),
    ):
        snap = await energyvision.fetch(AsyncMock(), contract, region)
    assert fetched.call_args.args[1] == url
    assert snap.source_url == url
    assert snap.publication_label == month


async def test_fetch_for_month_builds_the_plain_name() -> None:
    fetched = AsyncMock(return_value=_card("EV-0826-GSG-WAL-fr.pdf"))
    with patch.object(energyvision, "fetch_pdf_text_layout", fetched):
        snap = await energyvision.fetch_for_month(
            AsyncMock(), _GSG, REGION_WALLONIA, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert fetched.call_args.args[1] == (
        "https://www.energyvision.be/sites/default/files/inline-files/EV-0826-GSG-WAL-fr.pdf"
    )


async def test_fetch_for_month_tries_both_brusol_upload_folders() -> None:
    fetched = AsyncMock(
        side_effect=[
            ExtractorError("HTTP 404 fetching x"),
            _card("brusol/2026-07/EV-0826-GSG-BXL-nl.pdf"),
        ]
    )
    with patch.object(energyvision, "fetch_pdf_text_layout", fetched):
        snap = await energyvision.fetch_for_month(
            AsyncMock(), _GSG, REGION_BRUSSELS, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.publication_label == "2026-08"
    assert [call.args[1] for call in fetched.call_args_list] == [
        "https://www.brusol.be/sites/default/files/2026-08/EV-0826-GSG-BXL-nl.pdf",
        "https://www.brusol.be/sites/default/files/2026-07/EV-0826-GSG-BXL-nl.pdf",
    ]


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with patch.object(
        energyvision, "fetch_pdf_text_layout", AsyncMock(return_value=_card("EV-0926-GSG-nl.pdf"))
    ):
        assert (
            await energyvision.fetch_for_month(AsyncMock(), _GSG, REGION_FLANDERS, date(2026, 8, 1))
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            energyvision,
            "fetch_pdf_text_layout",
            AsyncMock(side_effect=ExtractorError("network error fetching x: reset")),
        ),
        pytest.raises(ExtractorError),
    ):
        await energyvision.fetch_for_month(AsyncMock(), _GSG, REGION_FLANDERS, date(2026, 8, 1))


async def test_fetch_for_month_has_no_brussels_fixed_card() -> None:
    fetched = AsyncMock()
    with patch.object(energyvision, "fetch_pdf_text_layout", fetched):
        assert (
            await energyvision.fetch_for_month(
                AsyncMock(), _GS1JVG, REGION_BRUSSELS, date(2026, 8, 1)
            )
            is None
        )
    fetched.assert_not_called()


def test_index_document_gas_table() -> None:
    table = energyvision.parse_index_document(_card("EV-0926-Indexatieparameters-nl.pdf"))
    values = table["ZTP-RLP-M"]
    assert values["2026-08"] == pytest.approx(61.822)
    assert values["2026-07"] == pytest.approx(52.996)
    # "Maart 2026 50.358 54.080", with dots.
    assert values["2026-03"] == pytest.approx(50.358)
    # The first months print "/ /", the ones to come nothing.
    assert min(values) == "2024-09"
    assert max(values) == "2026-08"
    assert len(values) == 24


async def test_fetch_index_follows_the_page_link() -> None:
    fetched = AsyncMock(return_value=_card("EV-0926-Indexatieparameters-nl.pdf"))
    with (
        patch.object(
            energyvision,
            "fetch_text",
            AsyncMock(return_value=fixture_page("energyvision", "indexatieparameters.html")),
        ),
        patch.object(energyvision, "fetch_pdf_text_layout", fetched),
    ):
        table = await energyvision.fetch_index(AsyncMock())
    assert fetched.call_args.args[1] == (
        "https://www.energyvision.be/sites/default/files/inline-files/"
        "EV-0926-Indexatieparameters-nl.pdf"
    )
    assert table["ZTP-RLP-M"]["2026-08"] == pytest.approx(61.822)


def test_regions_follow_the_published_cards() -> None:
    by_id = {c.id: c for c in energyvision.EXTRACTOR.contracts}
    assert by_id[_GSG].regions == frozenset({REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS})
    assert by_id[_GSG].kind == "indexed"
    assert by_id[_GS1JVG].regions == frozenset({REGION_FLANDERS, REGION_WALLONIA})
    assert by_id[_GS1JVG].kind == "fixed"
