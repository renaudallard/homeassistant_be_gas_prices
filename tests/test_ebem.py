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

"""EBEM gas card extractor, against the September 2026 card."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_KEMPEN,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.providers import ebem
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import fixture_page, fixture_text

SEPTEMBER = "ebem_tariefkaart-gas-09-2026.pdf"
AUGUST = "ebem_tariefkaart-gas-08-2026.pdf"
DECEMBER_2025 = "ebem_tariefkaart-gas-12-2025.pdf"
PARAMETERS = "ebem_parameters_indexen-09-2026.pdf"


def _card(name: str) -> str:
    return fixture_text("ebem", name, "layout")


def test_aardgas_variabel_is_priced_at_the_previous_months_index() -> None:
    snap = ebem.parse_snapshot("ebem_variable", REGION_FLANDERS, _card(SEPTEMBER))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTP-RLP0"
    assert not energy.settled
    # "0,105 ZTP +0,675 7,1661 c EUR/kWh 7,5961 c EUR/kWh 7,2070 ... 7,6394":
    # the price including VAT at last month's index, not the VNR estimate.
    assert energy.price == pytest.approx(0.075961)
    # "Vaste vergoeding 66,04 EUR/jaar 70,00 EUR/jaar".
    assert energy.yearly_fixed_fee == pytest.approx(70.0)
    assert energy.factor == pytest.approx(0.00105 * 1.06)
    assert energy.base == pytest.approx(0.00675 * 1.06)
    # "Vorige maand bedroeg deze index 61,82".
    assert energy.at(61.82) == pytest.approx(0.075961, abs=5e-7)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)
    assert snap.taxes.vat_rate == 0.0


def test_gas_plus_shares_the_pdf_with_its_own_formula() -> None:
    snap = ebem.parse_snapshot("ebem_gas_plus", REGION_FLANDERS, _card(SEPTEMBER))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    # "0,105 ZTP +0,625 7,1161 c EUR/kWh 7,5431 EUR/kWh": the unit typo on the
    # second figure is tolerated.
    assert energy.price == pytest.approx(0.075431)
    assert energy.yearly_fixed_fee == pytest.approx(60.0)
    assert energy.base == pytest.approx(0.00625 * 1.06)
    assert energy.at(61.82) == pytest.approx(0.075431, abs=5e-7)
    assert snap.publication_label == "2026-09"


def test_august_card() -> None:
    energy = ebem.parse_snapshot("ebem_variable", REGION_FLANDERS, _card(AUGUST)).energy
    assert isinstance(energy, IndexedRates)
    # "6,6122 c EUR/kWh" incl. VAT at July's 52,98.
    assert energy.price == pytest.approx(0.066122)
    assert energy.at(52.98) == pytest.approx(0.066122, abs=5e-7)


def test_a_card_without_the_product_block_is_refused() -> None:
    text = _card(SEPTEMBER).replace("TARIEFKAART EBEM G@S+", "TARIEFKAART EBEM Webonly")
    with pytest.raises(ExtractorError):
        ebem.parse_snapshot("ebem_gas_plus", REGION_FLANDERS, text)


def test_dso_table() -> None:
    snap = ebem.parse_snapshot("ebem_variable", REGION_FLANDERS, _card(SEPTEMBER))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Antwerpen 15,68 2,26 83,22 0,91 562,63 0,59 18,92 1,65": fixed
    # before proportional, three tiers, transport in EUR/MWh.
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0226)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.0091)
    assert antwerpen.tiers[TIER_T3].fixed_per_year == pytest.approx(562.63)
    assert antwerpen.tiers[TIER_T3].proportional == pytest.approx(0.0059)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    assert antwerpen.transport == pytest.approx(0.00165)
    # "Fluvius Halle Vilvoorde 17.59 2,50 ...": a dot for the decimal comma.
    assert snap.dsos[DSO_FLUVIUS_HALLE_VILVOORDE].tiers[TIER_T1].fixed_per_year == (
        pytest.approx(17.59)
    )
    # "Fluvius Kempen 16,17 2,12 ...": read as printed, although every other
    # supplier and Fluvius itself print 2,27 (2,27489).
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.0212)
    assert kempen.tiers[TIER_T1].fixed_per_year == pytest.approx(16.17)


def test_levies_as_printed() -> None:
    snap = ebem.parse_snapshot("ebem_variable", REGION_FLANDERS, _card(SEPTEMBER))
    # "0-12 MWh 1,09286 c EUR/kWh", "12-20.000 MWh 1,18296 c EUR/kWh".
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    # "Huishoudelijk: 0,00000 c EUR/kWh".
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_december_2025_card_and_its_split_figure() -> None:
    """ "Fluvius Antwerpen 14,24 2,10 75 ,60 ..." still reads as 75,60."""
    snap = ebem.parse_snapshot("ebem_variable", REGION_FLANDERS, _card(DECEMBER_2025))
    assert snap.publication_label == "2025-12"
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(75.60)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0210)
    assert antwerpen.metering_per_year == pytest.approx(18.56)
    assert snap.taxes.energy_contribution == pytest.approx(0.00105764)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0094309)),
    )


def test_only_flanders() -> None:
    with pytest.raises(ExtractorError):
        ebem.parse_snapshot("ebem_variable", REGION_BRUSSELS, _card(SEPTEMBER))
    assert all(c.regions == {REGION_FLANDERS} for c in ebem.EXTRACTOR.contracts)


def test_parameters_document() -> None:
    values = ebem.parse_parameters(fixture_text("ebem", PARAMETERS, "layout"))["ZTP-RLP0"]
    # "augustus 171,65214 29,2231 augustus 37,52 32,05 61,82".
    assert values["2024-08"] == pytest.approx(37.52)
    assert values["2025-08"] == pytest.approx(32.05)
    assert values["2026-08"] == pytest.approx(61.82)
    assert values["2024-01"] == pytest.approx(29.94)
    # "juni ... juni 34,08 35,66 44,70".
    assert values["2026-06"] == pytest.approx(44.70)
    # "september ... 36,25 31,95 ...................".
    assert "2026-09" not in values
    assert len(values) == 32


def test_october_parameters_document_corrects_the_heading() -> None:
    """From October 2026 the column reads "Argus ZTP-RLP", which the
    simulator's "Geschatte ZTP" table above it must not be taken for."""
    text = fixture_text("ebem", "ebem_parameters_indexen-10-2026.pdf", "layout")
    assert "Argus ZTP-RPL" not in text
    values = ebem.parse_parameters(text)["ZTP-RLP0"]
    # "september 232,04318 34,72795 september 36,25 31,95 75,26".
    assert values["2026-09"] == pytest.approx(75.26)
    assert values["2026-08"] == pytest.approx(61.82)


def test_parameters_agree_with_the_cards() -> None:
    """Each card's "vorige maand" figure is the parameters' previous month."""
    values = ebem.parse_parameters(fixture_text("ebem", PARAMETERS, "layout"))["ZTP-RLP0"]
    assert "index 61,82" in _card(SEPTEMBER)
    assert values["2026-08"] == pytest.approx(61.82)
    assert "index 52,98" in _card(AUGUST)
    assert values["2026-07"] == pytest.approx(52.98)
    assert "index 29,90" in _card(DECEMBER_2025)
    assert values["2025-11"] == pytest.approx(29.90)


async def test_fetch_takes_the_newest_gas_card() -> None:
    with (
        patch.object(
            ebem, "fetch_text", AsyncMock(return_value=fixture_page("ebem", "tarieven.html"))
        ),
        patch.object(
            ebem, "fetch_pdf_text_layout", AsyncMock(return_value=_card(SEPTEMBER))
        ) as fetched,
    ):
        snap = await ebem.fetch(AsyncMock(), "ebem_gas_plus", REGION_FLANDERS)
    assert fetched.call_args.args[1] == (
        "https://www.ebem.be/media/y3vfcgeh/ebem_tariefkaart-gas-09-2026.pdf"
    )
    assert snap.source_url == fetched.call_args.args[1]


async def test_fetch_for_month_resolves_the_card_of_that_month() -> None:
    with (
        patch.object(
            ebem, "fetch_text", AsyncMock(return_value=fixture_page("ebem", "tarieven.html"))
        ),
        patch.object(
            ebem, "fetch_pdf_text_layout", AsyncMock(return_value=_card(AUGUST))
        ) as fetched,
    ):
        snap = await ebem.fetch_for_month(
            AsyncMock(), "ebem_variable", REGION_FLANDERS, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert fetched.call_args.args[1] == (
        "https://www.ebem.be/media/ugwfbd1t/ebem_tariefkaart-gas-08-2026.pdf"
    )


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        patch.object(
            ebem, "fetch_text", AsyncMock(return_value=fixture_page("ebem", "tarieven.html"))
        ),
        patch.object(ebem, "fetch_pdf_text_layout", AsyncMock(return_value=_card(SEPTEMBER))),
    ):
        assert (
            await ebem.fetch_for_month(
                AsyncMock(), "ebem_variable", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_skips_the_names_before_2025() -> None:
    """ "gas-10-2024_web2.pdf" is not a card this reads: None, no download."""
    fetched = AsyncMock()
    with (
        patch.object(
            ebem, "fetch_text", AsyncMock(return_value=fixture_page("ebem", "tarieven.html"))
        ),
        patch.object(ebem, "fetch_pdf_text_layout", fetched),
    ):
        assert (
            await ebem.fetch_for_month(
                AsyncMock(), "ebem_variable", REGION_FLANDERS, date(2024, 10, 1)
            )
            is None
        )
    fetched.assert_not_called()


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            ebem, "fetch_text", AsyncMock(side_effect=ExtractorError("network error fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await ebem.fetch_for_month(AsyncMock(), "ebem_variable", REGION_FLANDERS, date(2026, 8, 1))


async def test_fetch_index_reads_the_linked_parameters_document() -> None:
    with (
        patch.object(
            ebem, "fetch_text", AsyncMock(return_value=fixture_page("ebem", "tarieven.html"))
        ),
        patch.object(
            ebem,
            "fetch_pdf_text_layout",
            AsyncMock(return_value=fixture_text("ebem", PARAMETERS, "layout")),
        ) as fetched,
    ):
        table = await ebem.fetch_index(AsyncMock())
    assert fetched.call_args.args[1] == (
        "https://www.ebem.be/media/v35p3zj2/ebem_parameters_indexen-09-2026.pdf"
    )
    assert table["ZTP-RLP0"]["2026-08"] == pytest.approx(61.82)
