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

"""Frank Energie gas card extractor, against the September 2026 cards."""

from __future__ import annotations

import json
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
from custom_components.be_gas_prices.providers import frank
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import fixture_page, fixture_text

# pdfplumber takes 25 to 30 seconds over one of these cards on a Raspberry
# Pi, and the archive test reads two.
pytestmark = pytest.mark.timeout(180)

_PREFIX = "Frank Energie Tariefkaart "
STANDARD = _PREFIX + "Gas ZTP September 2026.pdf"
HV = _PREFIX + "Gas HV September 2026.pdf"
JN = _PREFIX + "Gas JN September 2026.pdf"
SLIM = _PREFIX + "Gas SL September 2026.pdf"
KORTING = _PREFIX + "Gas VT September 2026.pdf"
JULY = _PREFIX + "Gas ZTP Juli 2026.pdf"
MARCH = _PREFIX + "Gas ZTP Maart 2026.pdf"
JANUARY = _PREFIX + "Gas ZTP Januari 2026.pdf"
JANUARY_V2 = _PREFIX + "Gas ZTP Januari 2026 v2.pdf"
JANUARY_SL = _PREFIX + "Elektriciteit Gas ZTP SL Januari 2026.pdf"

SEPTEMBER_CARDS = {
    "frank_variable": STANDARD,
    "frank_variable_hv": HV,
    "frank_variable_jn": JN,
    "frank_variable_slim": SLIM,
    "frank_variable_korting": KORTING,
}

# Frank's cards do not print the index value their expected price is
# computed on. EBEM's cards print the same VNR estimate ("Geschatte ZTP
# (simulator)"): 62,20917 EUR/MWh for September 2026, 39,37696 for July and
# 27,11155 for January, and Frank's printed prices come out of it exactly.
VNR_SEPTEMBER = 62.20917


def _card(name: str) -> str:
    return fixture_text("frank", name, "layout")


def _assets() -> str:
    return fixture_page("frank", "sanity_gas_assets.json")


def _url(name: str) -> str:
    rows = json.loads(_assets())["result"]
    return str(next(r["url"] for r in rows if r["originalFilename"] == name))


@pytest.mark.parametrize(
    ("contract", "price", "factor", "base", "fee"),
    [
        # "(0,1000 x M ZTP RLP0N EOD EEX + 0,46) x 1,06", 2,92 EUR/maand, 7,0818.
        ("frank_variable", 0.070818, 0.1, 0.46, 2.92),
        # "+ 0,2", 8,50 EUR/maand, 6,8062.
        ("frank_variable_hv", 0.068062, 0.1, 0.2, 8.50),
        # "(0,1010 x ... + 0,44)", 2,17 EUR/maand, 7,1265.
        ("frank_variable_jn", 0.071265, 0.101, 0.44, 2.17),
        ("frank_variable_slim", 0.070818, 0.1, 0.46, 2.92),
        ("frank_variable_korting", 0.070818, 0.1, 0.46, 2.92),
    ],
)
def test_each_tier_reads_its_formula(
    contract: str, price: float, factor: float, base: float, fee: float
) -> None:
    snap = frank.parse_snapshot(contract, REGION_FLANDERS, _card(SEPTEMBER_CARDS[contract]))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "M ZTP RLP0N EOD EEX"
    assert not energy.settled
    # The expected price "volgens methode VNR voor september 2026".
    assert energy.price == pytest.approx(price)
    # EURct/kWh with the index in EUR/MWh, times the formula's own 1,06.
    assert energy.factor == pytest.approx(factor / 100.0 * 1.06)
    assert energy.base == pytest.approx(base / 100.0 * 1.06)
    # Printed per month.
    assert energy.yearly_fixed_fee == pytest.approx(fee * 12)
    assert energy.at(VNR_SEPTEMBER) == pytest.approx(price, abs=5e-7)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)
    assert snap.taxes.vat_rate == 0.0


@pytest.mark.parametrize("contract", sorted(SEPTEMBER_CARDS))
@pytest.mark.parametrize("card", sorted(SEPTEMBER_CARDS))
def test_a_tier_refuses_every_other_tiers_card(contract: str, card: str) -> None:
    """The title is the only place a card names its tier."""
    text = _card(SEPTEMBER_CARDS[card])
    if contract == card:
        assert frank.parse_snapshot(contract, REGION_FLANDERS, text).contract == contract
    else:
        with pytest.raises(ExtractorError):
            frank.parse_snapshot(contract, REGION_FLANDERS, text)


def test_dso_table_rebuilt_from_one_figure_per_line() -> None:
    snap = frank.parse_snapshot("frank_variable", REGION_FLANDERS, _card(STANDARD))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Antwerpen / 18,92 / 0,1654 / 2,26 / 15,68 / 0,91 / 83,22": databeheer,
    # transport, then proportional before fixed within each tier.
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    assert antwerpen.transport == pytest.approx(0.001654)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0226)
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.0091)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert TIER_T3 not in antwerpen.tiers
    assert snap.dsos[DSO_FLUVIUS_KEMPEN].tiers[TIER_T1].proportional == pytest.approx(0.0227)
    # West shares its line with its first figure.
    west = snap.dsos[DSO_FLUVIUS_WEST]
    assert west.metering_per_year == pytest.approx(18.92)
    assert west.tiers[TIER_T2].proportional == pytest.approx(0.0106)
    assert west.tiers[TIER_T2].fixed_per_year == pytest.approx(101.02)


def test_levies_as_printed() -> None:
    snap = frank.parse_snapshot("frank_variable", REGION_FLANDERS, _card(STANDARD))
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    # No contribution row from August 2026, the month the law zeroed it.
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.connection_fee == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_july_card_still_prints_the_contribution() -> None:
    snap = frank.parse_snapshot("frank_variable", REGION_FLANDERS, _card(JULY))
    assert snap.publication_label == "2026-07"
    assert snap.taxes.energy_contribution == pytest.approx(0.001057)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.008724)),
        (None, pytest.approx(0.009332)),
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.at(39.37696) == pytest.approx(0.046616, abs=5e-7)


def test_a_card_before_august_without_the_contribution_is_refused() -> None:
    text = _card(JULY).replace("Bijdrage op Energie", "Bijdrage")
    with pytest.raises(ExtractorError):
        frank.parse_snapshot("frank_variable", REGION_FLANDERS, text)


def test_slim_title_as_sl_and_the_title_wins_over_the_sentence() -> None:
    """January 2026: the title's tier reads "SL", the price line "VREG",
    and the sentence under the title "getekend in december 2026"."""
    snap = frank.parse_snapshot("frank_variable_slim", REGION_FLANDERS, _card(JANUARY_SL))
    assert snap.publication_label == "2026-01"
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.price == pytest.approx(0.033614)
    assert energy.at(27.11155) == pytest.approx(0.033614, abs=5e-7)


def test_the_clipped_march_2026_cards_are_refused() -> None:
    """Every line lost its last character: "maart 202", "2,9" for 2,92."""
    with pytest.raises(ExtractorError):
        frank.parse_snapshot("frank_variable", REGION_FLANDERS, _card(MARCH))


def test_only_flanders() -> None:
    with pytest.raises(ExtractorError):
        frank.parse_snapshot("frank_variable", REGION_WALLONIA, _card(STANDARD))
    assert all(c.regions == {REGION_FLANDERS} for c in frank.EXTRACTOR.contracts)


def test_index_page() -> None:
    values = frank.parse_index_page(fixture_page("frank", "voorwaarden_nl.html"))[
        "M ZTP RLP0N EOD EEX"
    ]
    assert values["2026-08"] == pytest.approx(61.9)
    assert values["2026-07"] == pytest.approx(53.04)
    assert values["2025-02"] == pytest.approx(51.09)
    assert values["2023-08"] == pytest.approx(33.2083)
    assert "2026-09" not in values
    assert len(values) == 37


async def test_fetch_takes_the_newest_month_of_the_tier() -> None:
    with (
        patch.object(frank, "fetch_text", AsyncMock(return_value=_assets())),
        patch.object(frank, "fetch_pdf_text_layout", AsyncMock(return_value=_card(HV))) as fetched,
    ):
        snap = await frank.fetch(AsyncMock(), "frank_variable_hv", REGION_FLANDERS)
    assert fetched.call_args.args[1] == _url(HV)
    assert snap.source_url == _url(HV)
    assert snap.publication_label == "2026-09"


async def test_fetch_for_month_skips_an_upload_carrying_another_tier() -> None:
    """ "Gas ZTP Januari 2026 v2.pdf" is the Korting card, uploaded after the
    standard tier's own January card."""
    texts = {_url(JANUARY_V2): _card(JANUARY_V2), _url(JANUARY): _card(JANUARY)}
    with (
        patch.object(frank, "fetch_text", AsyncMock(return_value=_assets())) as queried,
        patch.object(
            frank, "fetch_pdf_text_layout", AsyncMock(side_effect=lambda _s, url: texts[url])
        ) as fetched,
    ):
        snap = await frank.fetch_for_month(
            AsyncMock(), "frank_variable", REGION_FLANDERS, date(2026, 1, 1)
        )
    assert snap is not None
    assert snap.source_url == _url(JANUARY)
    assert snap.valid_until == date(2026, 1, 31)
    assert [call.args[1] for call in fetched.call_args_list] == [_url(JANUARY_V2), _url(JANUARY)]
    query = queried.call_args.kwargs["params"]["query"]
    assert 'match "*Januari*"' in query
    assert 'match "*2026*"' in query


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        patch.object(frank, "fetch_text", AsyncMock(return_value=_assets())),
        patch.object(frank, "fetch_pdf_text_layout", AsyncMock(return_value=_card(STANDARD))),
    ):
        assert (
            await frank.fetch_for_month(
                AsyncMock(), "frank_variable", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_of_a_clipped_card_is_none() -> None:
    with (
        patch.object(frank, "fetch_text", AsyncMock(return_value=_assets())),
        patch.object(frank, "fetch_pdf_text_layout", AsyncMock(return_value=_card(MARCH))),
    ):
        assert (
            await frank.fetch_for_month(
                AsyncMock(), "frank_variable", REGION_FLANDERS, date(2026, 3, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(frank, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 x"))),
        pytest.raises(ExtractorError),
    ):
        await frank.fetch_for_month(
            AsyncMock(), "frank_variable", REGION_FLANDERS, date(2026, 8, 1)
        )


async def test_probe_is_the_newest_upload_time() -> None:
    with patch.object(frank, "fetch_text", AsyncMock(return_value=_assets())):
        assert (
            await frank.probe(AsyncMock(), "frank_variable", REGION_FLANDERS)
            == "2026-08-31T08:17:54Z"
        )
    with patch.object(frank, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 x"))):
        assert await frank.probe(AsyncMock(), "frank_variable", REGION_FLANDERS) is None


async def test_fetch_index_reads_the_terms_page() -> None:
    with patch.object(
        frank, "fetch_text", AsyncMock(return_value=fixture_page("frank", "voorwaarden_nl.html"))
    ) as fetched:
        table = await frank.fetch_index(AsyncMock())
    assert fetched.call_args.args[1] == "https://www.frankenergie.be/nl/voorwaarden"
    assert table["M ZTP RLP0N EOD EEX"]["2026-08"] == pytest.approx(61.9)
