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

"""Dots Energy gas card extractor, against the September 2026 card."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_MIDDEN_VLAANDEREN,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.providers import dots
from custom_components.be_gas_prices.providers._rates import VariableRates
from custom_components.be_gas_prices.providers.base import ExtractorError, SupplierSnapshot
from tests import approx, fixture_page, fixture_text

CARD = "Tariefkaart NG Dots Connect - Digital 09_2026.pdf"


def _parse() -> SupplierSnapshot:
    return dots.parse_snapshot(
        "dots_connect_digital", REGION_FLANDERS, fixture_text("dots", CARD), "url"
    )


def test_price_is_kept_as_printed() -> None:
    """ "Gas 1,05 * M ZTP rek. gem. EGSI EEX + 0,9 7,873" and
    "€5,3/maand/EAN": the card defines its index two ways, so the M-1 price
    is the energy leg and the formula is only kept."""
    snap = _parse()
    assert snap.energy == VariableRates(
        price=approx(0.07873),
        yearly_fixed_fee=approx(63.6),
        formula="1,05 * M ZTP rek. gem. EGSI EEX + 0,9",
    )
    assert snap.publication_label == "2026-09"
    assert snap.valid_until is not None
    assert snap.valid_until.isoformat() == "2026-09-30"


def test_levies_are_read_as_printed() -> None:
    """The new excise, rounded, beside the old energy contribution, which the
    law set to zero from August 2026."""
    taxes = _parse().taxes
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.01093)),
        (None, pytest.approx(0.01183)),
    )
    assert taxes.energy_contribution == pytest.approx(0.0010577)
    assert taxes.vat_rate == 0.0
    assert taxes.card_vat_rate == pytest.approx(0.06)


def test_distribution_table() -> None:
    snap = _parse()
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Antwerpen 15,68 2,256 83,22 0,906 562,63 0,586 18,92 0,165"
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.02256)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.00906)
    assert antwerpen.tiers[TIER_T3].fixed_per_year == pytest.approx(562.63)
    assert antwerpen.tiers[TIER_T3].proportional == pytest.approx(0.00586)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    assert antwerpen.transport == pytest.approx(0.00165)


def test_wrong_cells_drop_their_tier() -> None:
    """ "Fluvius Limburg 14,59 2,240 77,46 2,240 ...": the mid tier repeats the
    small tier's term, where Engie and Sparki print 0,983. Three T3 terms
    repeat their T2 value (Engie and Sparki: 0,621, 0,552, 0,577)."""
    dsos = _parse().dsos
    assert list(dsos[DSO_FLUVIUS_LIMBURG].tiers) == [TIER_T1]
    for key, t2 in (
        (DSO_FLUVIUS_HALLE_VILVOORDE, 0.0098),
        (DSO_FLUVIUS_KEMPEN, 0.00882),
        (DSO_FLUVIUS_MIDDEN_VLAANDEREN, 0.0091),
    ):
        assert list(dsos[key].tiers) == [TIER_T1, TIER_T2]
        assert dsos[key].tiers[TIER_T2].proportional == pytest.approx(t2)
    assert TIER_T3 in dsos[DSO_FLUVIUS_ANTWERPEN].tiers


def test_internal_card_is_refused() -> None:
    """ "Dots Internal" is reserved for Dots's staff and is not registered."""
    with pytest.raises(ExtractorError, match="Connect - Digital"):
        dots.parse_snapshot(
            "dots_connect_digital",
            REGION_FLANDERS,
            fixture_text("dots", "Tariefkaart NG Dots Internal.pdf"),
            "url",
        )


def test_only_flanders_and_the_one_contract() -> None:
    text = fixture_text("dots", CARD)
    with pytest.raises(ExtractorError):
        dots.parse_snapshot("dots_connect_digital", REGION_WALLONIA, text, "url")
    with pytest.raises(ExtractorError):
        dots.parse_snapshot("dots_internal", REGION_FLANDERS, text, "url")


def test_card_that_lost_its_price_line_fails_loud() -> None:
    text = fixture_text("dots", CARD).replace("Gas 1,05", "Aardgas 1,05")
    with pytest.raises(ExtractorError, match="price"):
        dots.parse_snapshot("dots_connect_digital", REGION_FLANDERS, text, "url")


def test_product_page_links_the_card() -> None:
    page = fixture_page("dots", "dots_dots-connect-gas-digital.html")
    assert dots.card_path(page) == (
        "/web/content/98918?unique=adb0e23a7ee37894023a0016f9738428b50941fb&download=true"
    )


async def test_fetch_reads_the_linked_card() -> None:
    with (
        patch.object(
            dots,
            "fetch_text",
            AsyncMock(return_value=fixture_page("dots", "dots_dots-connect-gas-digital.html")),
        ) as page,
        patch.object(
            dots, "fetch_pdf_text", AsyncMock(return_value=fixture_text("dots", CARD))
        ) as card,
    ):
        snap = await dots.fetch(AsyncMock(), "dots_connect_digital", REGION_FLANDERS)
    assert page.call_args.args[1] == "https://www.dotsenergy.be/dots-connect-gas-digital"
    url = (
        "https://www.dotsenergy.be/web/content/98918"
        "?unique=adb0e23a7ee37894023a0016f9738428b50941fb&download=true"
    )
    assert card.call_args.args[1] == url
    assert snap.source_url == url
    assert snap.supplier == "dots"


def test_extractor_has_no_archive_or_index() -> None:
    extractor = dots.EXTRACTOR
    assert extractor.id == "dots"
    assert [c.id for c in extractor.contracts] == ["dots_connect_digital"]
    assert extractor.contracts[0].kind == "variable"
    assert extractor.regions() == frozenset({REGION_FLANDERS})
    assert extractor.fetch_for_month is None
    assert extractor.fetch_index is None
