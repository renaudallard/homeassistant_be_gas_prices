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

"""Ecofix gas card extractor, against the September 2026 cards as the card
archive's OCR engine reads them."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
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
from custom_components.be_gas_prices.providers import ecofix
from custom_components.be_gas_prices.providers._pdf import extract_pdf_text_layout
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import (
    CardNotReadableError,
    ExtractorError,
    SupplierSnapshot,
)
from tests import FIXTURES, fixture_page, fixture_text

VAT = 1.06
FLEXY = "GAS_Ecofix_Flexy_NL_2026-09.ocr.txt"
ONLINE = "GAS_Ecofix_Flexy_Online_NL_2026-09.ocr.txt"


def _parse(name: str, contract: str, region: str = REGION_FLANDERS) -> SupplierSnapshot:
    return ecofix.parse_snapshot(contract, region, fixture_page("ecofix", name), "url")


def _energy(name: str, contract: str) -> IndexedRates:
    energy = _parse(name, contract).energy
    assert isinstance(energy, IndexedRates)
    return energy


@pytest.mark.parametrize(
    ("name", "contract", "price", "fee", "base"),
    [
        # "60,00 + 7,23 Maandprijs", "(0,1010*TTF-RLP-M) + 0,5200 c€/kWh".
        (FLEXY, "ecofix_flexy", 0.0723, 60.0, 0.5200),
        # "10,00 + 6,91 Maandprijs", "(0,1010*TTF-RLP-M) + 0,2200 c€/kWh".
        (ONLINE, "ecofix_flexy_online", 0.0691, 10.0, 0.2200),
    ],
)
def test_energy_leg(name: str, contract: str, price: float, fee: float, base: float) -> None:
    energy = _energy(name, contract)
    assert energy.index == "TTF-RLP-M"
    assert not energy.settled
    assert energy.price == pytest.approx(price)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    # c EUR/kWh against EUR/MWh, printed excluding VAT.
    assert energy.factor == pytest.approx(0.1010 / 100 * VAT)
    assert energy.base == pytest.approx(base / 100 * VAT)


def test_both_formulas_price_their_card_at_one_index_value() -> None:
    """Ecofix names no index value, but its two products are priced on the
    same one. Each card's printed price, taken back through its formula,
    gives that value to within the rounding of two decimals: a digit of a
    base misread would move one of them by several EUR/MWh."""
    implied = []
    for name, contract in ((FLEXY, "ecofix_flexy"), (ONLINE, "ecofix_flexy_online")):
        energy = _energy(name, contract)
        implied.append((energy.price - energy.base) / energy.factor)
    # A printed price moves by at most half a hundredth of a cent.
    rounding = 0.00005 / (0.1010 / 100 * VAT)
    assert abs(implied[0] - implied[1]) <= 2 * rounding


def test_card_month_and_vat() -> None:
    snap = _parse(FLEXY, "ecofix_flexy")
    assert snap.publication_label == "2026-09"
    assert str(snap.valid_until) == "2026-09-30"
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_flanders_table() -> None:
    snap = _parse(FLEXY, "ecofix_flexy")
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Kempen 16,17 2,275 85,83 0,882 580,25 0,552 18,92 0,165"
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].fixed_per_year == pytest.approx(16.17)
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.02275)
    assert kempen.tiers[TIER_T2].fixed_per_year == pytest.approx(85.83)
    assert kempen.tiers[TIER_T2].proportional == pytest.approx(0.00882)
    assert kempen.tiers[TIER_T3].fixed_per_year == pytest.approx(580.25)
    assert kempen.tiers[TIER_T3].proportional == pytest.approx(0.00552)
    assert kempen.metering_per_year == pytest.approx(18.92)
    assert kempen.transport == pytest.approx(0.00165)
    assert snap.taxes.connection_fee == 0.0


def test_wallonia_table_as_printed() -> None:
    """The five ORES rows agree and collapse. Three figures are the card's own
    errors, read as printed: the ORES terms 4,198 and 2,115 (Bolt prints the
    same, every other card 4,289 and 2,206), RESA's mid-tier fixed term
    140,93, which is ORES's (122,05 on the other cards), and RESA's mid-tier
    term 2,259 (Bolt prints the same, every other card 2,529)."""
    snap = _parse(FLEXY, "ecofix_flexy", REGION_WALLONIA)
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.04198)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.02115)
    assert ores.metering_per_year == 0.0
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.02259)
    assert resa.tiers[TIER_T3].proportional == pytest.approx(0.02241)


def test_levies_as_printed() -> None:
    """The September card still prints the pre-August federal levies, which
    the law replaces for the months it is known for."""
    taxes = _parse(FLEXY, "ecofix_flexy", REGION_WALLONIA).taxes
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0096229)),
    )
    assert taxes.energy_contribution == pytest.approx(0.0010577)
    # "Aansluitingsvergoeding 0,00750", c EUR/kWh.
    assert taxes.connection_fee == pytest.approx(0.000075)


def test_a_line_the_engine_was_not_sure_of_fails_the_parse() -> None:
    """Trusted text leaves out a line with a refused mark; the parse must
    fail on it rather than price without it."""
    text = fixture_page("ecofix", FLEXY)
    formula = next(line for line in text.splitlines() if "tariefformule" in line)
    with pytest.raises(ExtractorError, match="formula"):
        ecofix.parse_snapshot("ecofix_flexy", REGION_FLANDERS, text.replace(formula, ""), "url")


def test_the_card_before_the_monthly_price_is_refused() -> None:
    """Up to July 2026 the card printed only an expected yearly price
    ("60.00 3.49 Verwachte jaarprijs" in January), which is not the month's."""
    text = fixture_text("ecofix", "GAS_Ecofix_Flexy_NL_2026-01.pdf", "layout")
    with pytest.raises(ExtractorError, match="monthly price"):
        ecofix.parse_snapshot("ecofix_flexy", REGION_FLANDERS, text, "url")


def test_only_its_products_and_regions() -> None:
    text = fixture_page("ecofix", FLEXY)
    with pytest.raises(ExtractorError):
        ecofix.parse_snapshot("ecofix_flexy", REGION_BRUSSELS, text, "url")
    with pytest.raises(ExtractorError):
        ecofix.parse_snapshot("ecofix_motion", REGION_FLANDERS, text, "url")


@pytest.mark.parametrize(
    "name", ["GAS_Ecofix_Flexy_NL_2026-09.pdf", "GAS_Ecofix_Flexy_Online_NL_2026-09.pdf"]
)
def test_the_published_card_is_page_images(name: str) -> None:
    """What an installation downloads: no reader here gets a card out of it."""
    with pytest.raises(CardNotReadableError):
        extract_pdf_text_layout((FIXTURES / "ecofix" / name).read_bytes())


async def test_fetch_reads_the_dutch_card_for_either_region() -> None:
    reader = AsyncMock(return_value=fixture_page("ecofix", ONLINE))
    with patch.object(ecofix, "fetch_pdf_text_layout", reader):
        snap = await ecofix.fetch(AsyncMock(), "ecofix_flexy_online", REGION_WALLONIA)
    assert reader.await_args is not None
    assert reader.await_args.args[1] == (
        "https://portal.ecofixgp.be/docs/prices/current/GAS_Ecofix_Flexy_Online_NL.pdf"
    )
    assert snap.contract == "ecofix_flexy_online"
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}


async def test_probe_is_the_card_last_modified() -> None:
    head = AsyncMock(return_value="Mon, 31 Aug 2026 11:30:43 GMT")
    with patch.object(ecofix, "head_freshness_key", head):
        assert await ecofix.probe(AsyncMock(), "ecofix_flexy", REGION_FLANDERS) == (
            "Mon, 31 Aug 2026 11:30:43 GMT"
        )
        assert await ecofix.probe(AsyncMock(), "ecofix_flexy", REGION_BRUSSELS) is None
    assert head.await_count == 1


def test_no_index_table_and_no_archive_of_its_own() -> None:
    assert ecofix.EXTRACTOR.fetch_index is None
    assert ecofix.EXTRACTOR.fetch_for_month is None
    assert {c.id for c in ecofix.EXTRACTOR.contracts} == {"ecofix_flexy", "ecofix_flexy_online"}
