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

"""energie.be gas card extractor, against the September 2026 cards."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_WEST,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.providers import energiebe
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import CardNotReadableError, ExtractorError
from tests import approx, fixture_page, fixture_text

VARIABLE = "Energie_be_Gas_Particulier_18ddc53692.pdf"
FIXED = "VAST_Energie_be_RES_GAS_pdf_276e3f8ac3.pdf"
OCTOBER = "Oktober_Energie_be_Gas_Particulier_18ddc53692.pdf"
AUGUST = "Augustus_Tariefkaart_Energie_be_Variabel_NG_RES_3aa9f66512.pdf"
DECEMBER_2025 = "December_Tariefkaart_Energie_be_Variabel_NG_Res_632ae083c5.pdf"
NOVEMBER_2023 = "November_Tariefkaart_Variabel_Energie_be_GAS_RES_5108a3762e.pdf"


def _card(name: str) -> str:
    return fixture_text("energiebe", name, "layout")


def test_variable_card_reads_the_formula_and_its_vnr_price() -> None:
    snap = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(VARIABLE))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF_RLP"
    assert not energy.settled
    assert energy.period == "month"
    # "6,88" printed at "(1,014x TTF_RLP+ 0,16) c EUR/kWh" excl. btw, fee 35.
    assert energy.price == pytest.approx(0.0688)
    assert energy.yearly_fixed_fee == pytest.approx(35.0)
    # TTF_RLP is in c EUR/kWh, the index in EUR/MWh: 1,014 / 1000 per EUR/MWh.
    assert energy.factor == pytest.approx(1.014 / 1000.0 * 1.06)
    assert energy.base == pytest.approx(0.0016 * 1.06)
    # The card prices its month on the VNR estimate 6,24 c EUR/kWh.
    assert energy.at(62.4) == pytest.approx(0.0688, abs=5e-5)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    # "Alle prijzen zijn inclusief btw" without a rate.
    assert snap.taxes.card_vat_rate is None
    assert snap.taxes.vat_rate == 0.0


def test_the_october_card_leaves_particulier_out_of_its_title() -> None:
    """ "Gas online – oktober 2026"."""
    text = _card(OCTOBER)
    assert "Gas online – oktober 2026" in text
    snap = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, text)
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(0.076)
    assert energy.factor == pytest.approx(1.014 / 1000.0 * 1.06)
    assert energy.base == pytest.approx(0.0016 * 1.06)
    assert energy.yearly_fixed_fee == pytest.approx(35.0)
    assert snap.publication_label == "2026-10"
    with pytest.raises(ExtractorError):
        energiebe.parse_snapshot("energiebe_fixed", REGION_FLANDERS, text)


def test_the_factor_is_read_per_card() -> None:
    """August printed 1,025, September 1,014: a cohort's own card matters."""
    energy = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(AUGUST)).energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(1.025 / 1000.0 * 1.06)
    # "6,20" on the VNR estimate 5,55.
    assert energy.price == pytest.approx(0.062)
    assert energy.at(55.5) == pytest.approx(0.062, abs=5e-5)


def test_fixed_card() -> None:
    snap = energiebe.parse_snapshot("energiebe_fixed", REGION_FLANDERS, _card(FIXED))
    assert snap.energy == FixedRates(price=approx(0.0806), yearly_fixed_fee=35.0)
    assert snap.publication_label == "2026-09"


@pytest.mark.parametrize(
    ("contract", "name"),
    [("energiebe_fixed", VARIABLE), ("energiebe_variable", FIXED)],
)
def test_card_of_the_other_product_is_refused(contract: str, name: str) -> None:
    with pytest.raises(ExtractorError):
        energiebe.parse_snapshot(contract, REGION_FLANDERS, _card(name))


def test_dso_table() -> None:
    snap = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(VARIABLE))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius (Antwerpen ) 2,26 15,68 0,91 83,22 0,1654 18,92": proportional
    # before fixed within each tier.
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0226)
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.0091)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert TIER_T3 not in antwerpen.tiers
    assert antwerpen.transport == pytest.approx(0.001654)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    # "Fluvius (Kempen) 2,27 16,17 0,88 85,83 ..."
    assert snap.dsos[DSO_FLUVIUS_KEMPEN].tiers[TIER_T1].proportional == pytest.approx(0.0227)
    # "Fluvius (West) 2,69 19,03 1,05 101,02 ..."
    assert snap.dsos[DSO_FLUVIUS_WEST].tiers[TIER_T2].fixed_per_year == pytest.approx(101.02)


def test_levies_as_printed() -> None:
    snap = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(VARIABLE))
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.010929)),
        (None, pytest.approx(0.01183)),
    )
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_december_2025_card() -> None:
    """The title has no "online" yet and the contribution prints "0, 1058"."""
    snap = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(DECEMBER_2025))
    assert snap.publication_label == "2025-12"
    assert snap.taxes.energy_contribution == pytest.approx(0.001058)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.008724)),
        (None, pytest.approx(0.009431)),
    )
    assert snap.dsos[DSO_FLUVIUS_KEMPEN].metering_per_year == pytest.approx(18.56)


def test_pre_2025_layout_is_refused() -> None:
    """November 2023: formula in EUR/MWh and the old Fluvius areas."""
    with pytest.raises(ExtractorError):
        energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(NOVEMBER_2023))


def test_only_flanders() -> None:
    with pytest.raises(ExtractorError):
        energiebe.parse_snapshot("energiebe_variable", REGION_WALLONIA, _card(VARIABLE))
    assert all(c.regions == {REGION_FLANDERS} for c in energiebe.EXTRACTOR.contracts)


def test_index_document() -> None:
    table = energiebe.parse_index_document(
        fixture_text("energiebe", "document_key_Indexation.pdf", "plain")
    )
    values = table["TTF_RLP"]
    # "Augustus 3,76 3,24 6,17" c EUR/kWh under 2024 2025 2026.
    assert values["2024-08"] == pytest.approx(37.6)
    assert values["2025-08"] == pytest.approx(32.4)
    assert values["2026-08"] == pytest.approx(61.7)
    assert values["2024-01"] == pytest.approx(30.0)
    # "September 3,63 3,20": 2026 has not reached September.
    assert values["2025-09"] == pytest.approx(32.0)
    assert "2026-09" not in values
    assert len(values) == 32


def test_the_realised_index_reprices_the_august_card() -> None:
    table = energiebe.parse_index_document(
        fixture_text("energiebe", "document_key_Indexation.pdf", "plain")
    )
    energy = energiebe.parse_snapshot("energiebe_variable", REGION_FLANDERS, _card(AUGUST)).energy
    assert isinstance(energy, IndexedRates)
    # (1,025 x 6,17 + 0,16) x 1,06 against the 6,20 printed on the estimate.
    assert energy.at(table["TTF_RLP"]["2026-08"]) == pytest.approx(0.0687331, abs=1e-7)


async def test_fetch_reads_the_gas_document_of_the_contract() -> None:
    session = AsyncMock()
    with (
        patch.object(
            energiebe,
            "fetch_text",
            AsyncMock(return_value=fixture_page("energiebe", "contracts.json")),
        ),
        patch.object(
            energiebe, "fetch_pdf_text_layout", AsyncMock(return_value=_card(FIXED))
        ) as fetched,
    ):
        snap = await energiebe.fetch(session, "energiebe_fixed", REGION_FLANDERS)
    url = fetched.call_args.args[1]
    assert url.endswith("/VAST_Energie_be_RES_GAS_pdf_276e3f8ac3.pdf")
    assert snap.source_url == url
    assert isinstance(snap.energy, FixedRates)


async def test_fetch_for_month_reads_the_archive_row() -> None:
    session = AsyncMock()
    listing = fixture_page("energiebe", "tariff-cards_res_Variable.json")
    with (
        patch.object(energiebe, "fetch_text", AsyncMock(return_value=listing)) as listed,
        patch.object(
            energiebe, "fetch_pdf_text_layout", AsyncMock(return_value=_card(AUGUST))
        ) as fetched,
    ):
        snap = await energiebe.fetch_for_month(
            session, "energiebe_variable", REGION_FLANDERS, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert listed.call_args.kwargs["params"] == {
        "isProfessional": "false",
        "tariffType": "Variable",
    }
    assert fetched.call_args.args[1].endswith(AUGUST)


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    listing = fixture_page("energiebe", "tariff-cards_res_Variable.json")
    with (
        patch.object(energiebe, "fetch_text", AsyncMock(return_value=listing)),
        patch.object(energiebe, "fetch_pdf_text_layout", AsyncMock(return_value=_card(AUGUST))),
    ):
        assert (
            await energiebe.fetch_for_month(
                AsyncMock(), "energiebe_variable", REGION_FLANDERS, date(2026, 7, 1)
            )
            is None
        )


async def test_fetch_for_month_without_a_row_or_a_text_layer_is_none() -> None:
    listing = fixture_page("energiebe", "tariff-cards_res_Variable.json")
    unreadable = AsyncMock(side_effect=CardNotReadableError("card has no text layer"))
    with (
        patch.object(energiebe, "fetch_text", AsyncMock(return_value=listing)),
        patch.object(energiebe, "fetch_pdf_text_layout", unreadable),
    ):
        # The running month is never on the archive.
        assert (
            await energiebe.fetch_for_month(
                AsyncMock(), "energiebe_variable", REGION_FLANDERS, date(2026, 9, 1)
            )
            is None
        )
        # The 2025 cards up to November are page images.
        assert (
            await energiebe.fetch_for_month(
                AsyncMock(), "energiebe_variable", REGION_FLANDERS, date(2025, 6, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            energiebe,
            "fetch_text",
            AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x")),
        ),
        pytest.raises(ExtractorError),
    ):
        await energiebe.fetch_for_month(
            AsyncMock(), "energiebe_variable", REGION_FLANDERS, date(2026, 8, 1)
        )


async def test_fetch_index_reads_the_indexation_document() -> None:
    with patch.object(
        energiebe,
        "fetch_pdf_text",
        AsyncMock(return_value=fixture_text("energiebe", "document_key_Indexation.pdf", "plain")),
    ) as fetched:
        table = await energiebe.fetch_index(AsyncMock())
    assert fetched.call_args.args[1].endswith("?key=Indexation")
    assert table["TTF_RLP"]["2026-08"] == pytest.approx(61.7)
