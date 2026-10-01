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

"""Trevion gas card extractor, against its March to October 2026 cards."""

from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_KEMPEN,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.providers import trevion
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import fixture_page, fixture_text

_CONTRACT = "trevion_gas_flex"
_SEPTEMBER = "Tariefkaart-Gas-Flex-Particulier-202609.pdf"
_MARCH = "Tariefkaart-Gas-Flex-Particulier-202603.pdf"
_OCTOBER = "Tariefkaart-Gas-Flex-Particulier-202610.pdf"


def _card(name: str) -> str:
    return fixture_text("trevion", name)


def _by_url(url: str) -> str:
    """The fixture card a listing href names."""
    return _card(PurePosixPath(url).name)


def test_september_card_prices_at_the_last_known_value() -> None:
    snap = trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, _card(_SEPTEMBER))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF_RLP"
    assert not energy.settled
    # "Energiekost Variabel (c€/kWh) Abonnementskost (€/jaar)" / "7,20 39".
    assert energy.price == pytest.approx(0.072)
    assert energy.yearly_fixed_fee == pytest.approx(39.0)
    # "(0,102 x TTF_RLP + 0,50) x 1,06": the VAT is inside, not grossed again.
    assert energy.factor == pytest.approx(0.102 * 1.06 / 100)
    assert energy.base == pytest.approx(0.50 * 1.06 / 100)
    # The value the card names, "augustus 2026 (61,72 €/MWh)", reproduces
    # the printed 7,20 to its rounding.
    assert energy.at(61.72) == pytest.approx(0.0720, abs=5e-5)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_dso_table_reads_every_fluvius_row() -> None:
    snap = trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, _card(_SEPTEMBER))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Antwerpen 15,68 2,26 83,22 0,91 0,165 18,92"
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.0226)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.0091)
    assert antwerpen.transport == pytest.approx(0.00165)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    # "Fuvius Halle-Vilvoorde 17,59 2,50 93,38 0.98 0,165 18,92": a typo in the
    # label and a dot in the figure.
    halle = snap.dsos[DSO_FLUVIUS_HALLE_VILVOORDE]
    assert halle.tiers[TIER_T1].fixed_per_year == pytest.approx(17.59)
    assert halle.tiers[TIER_T2].proportional == pytest.approx(0.0098)
    # Read as printed: 2,28 where the Fluvius tariff is 2,27489.
    assert snap.dsos[DSO_FLUVIUS_KEMPEN].tiers[TIER_T1].proportional == pytest.approx(0.0228)


def test_september_levies() -> None:
    taxes = trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, _card(_SEPTEMBER)).taxes
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    # "Bijdrage op de Energie (c€/kWh) 0"
    assert taxes.energy_contribution == 0.0
    assert taxes.connection_fee == 0.0
    assert taxes.osp_by_caliber is None


def test_march_card_carries_the_levies_before_august() -> None:
    snap = trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, _card(_MARCH))
    assert snap.publication_label == "2026-03"
    assert snap.energy.price == pytest.approx(0.0413)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0096229)),
    )
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)


def test_a_contribution_printed_with_a_decimal_point_is_read_whole() -> None:
    text = _card(_MARCH).replace("(c€/kWh) 0,10577", "(c€/kWh) 0.10577")
    taxes = trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, text).taxes
    assert taxes.energy_contribution == pytest.approx(0.0010577)


def test_published_index_is_the_month_before_the_card() -> None:
    assert trevion.published_index(_card(_SEPTEMBER)) == (date(2026, 8, 1), 61.72)
    # "juli 2026 (53.07 €/MWh)", with a dot.
    assert trevion.published_index(_card("Tariefkaart-Gas-Flex-Particulier-202608-1.pdf")) == (
        date(2026, 7, 1),
        53.07,
    )


def test_published_index_skips_the_card_that_names_its_own_month() -> None:
    """The March card says "maart 2026 (33,30 €/MWh)", but 33,30 is
    February's value: neither month can be trusted to it."""
    assert trevion.published_index(_card(_MARCH)) is None


def test_listing_gives_one_card_per_month_and_its_reissue() -> None:
    cards = trevion._listed_cards(fixture_page("trevion", "tariefkaarten.html"))
    assert sorted(cards) == [date(2026, month, 1) for month in range(3, 10)]
    assert cards[date(2026, 8, 1)].endswith("/Tariefkaart-Gas-Flex-Particulier-202608-1.pdf")
    # The Professioneel cards are on the same page and are not read.
    assert not any("Professioneel" in url for url in cards.values())


async def test_fetch_reads_the_newest_listed_card() -> None:
    with (
        patch.object(
            trevion,
            "fetch_text",
            AsyncMock(return_value=fixture_page("trevion", "tariefkaarten.html")),
        ),
        patch.object(
            trevion, "fetch_pdf_text", AsyncMock(side_effect=lambda _s, url: _by_url(url))
        ),
    ):
        snap = await trevion.fetch(AsyncMock(), _CONTRACT, REGION_FLANDERS)
    assert snap.publication_label == "2026-09"
    assert snap.source_url == "https://trevion.be/tariefkaarten/" + _SEPTEMBER


async def test_fetch_index_takes_each_value_from_the_following_card() -> None:
    with (
        patch.object(
            trevion,
            "fetch_text",
            AsyncMock(return_value=fixture_page("trevion", "tariefkaarten.html")),
        ),
        patch.object(
            trevion, "fetch_pdf_text", AsyncMock(side_effect=lambda _s, url: _by_url(url))
        ),
    ):
        table = await trevion.fetch_index(AsyncMock())
    # OCTA+ publishes the same months as 51,264 / 46,001 / 46,941 / 45,076 /
    # 53,075 / 61,729; February is only on the mislabelled March card.
    assert table == {
        "TTF_RLP": {
            "2026-03": 51.26,
            "2026-04": 46.0,
            "2026-05": 46.94,
            "2026-06": 45.08,
            "2026-07": 53.07,
            "2026-08": 61.72,
        }
    }


async def test_fetch_index_survives_a_dead_link_but_not_an_outage() -> None:
    def card_or_404(_session: object, url: str) -> str:
        if url.endswith("202609.pdf"):
            raise ExtractorError(f"HTTP 404 fetching {url}")
        return _by_url(url)

    listing = AsyncMock(return_value=fixture_page("trevion", "tariefkaarten.html"))
    with (
        patch.object(trevion, "fetch_text", listing),
        patch.object(trevion, "fetch_pdf_text", AsyncMock(side_effect=card_or_404)),
    ):
        table = await trevion.fetch_index(AsyncMock())
    assert "2026-08" not in table["TTF_RLP"]
    assert table["TTF_RLP"]["2026-07"] == pytest.approx(53.07)
    with (
        patch.object(trevion, "fetch_text", listing),
        patch.object(
            trevion, "fetch_pdf_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await trevion.fetch_index(AsyncMock())


async def test_fetch_for_month_follows_the_listing_href() -> None:
    fetched = AsyncMock(side_effect=lambda _s, url: _by_url(url))
    with (
        patch.object(
            trevion,
            "fetch_text",
            AsyncMock(return_value=fixture_page("trevion", "tariefkaarten.html")),
        ),
        patch.object(trevion, "fetch_pdf_text", fetched),
    ):
        snap = await trevion.fetch_for_month(
            AsyncMock(), _CONTRACT, REGION_FLANDERS, date(2026, 5, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 5, 31)
    assert fetched.call_args.args[1] == (
        "https://trevion.be/tariefkaarten/Tariefkaart-Gas-Flex-Particulier-202605-1.pdf"
    )


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        patch.object(
            trevion,
            "fetch_text",
            AsyncMock(return_value=fixture_page("trevion", "tariefkaarten.html")),
        ),
        patch.object(trevion, "fetch_pdf_text", AsyncMock(return_value=_card(_SEPTEMBER))),
    ):
        assert (
            await trevion.fetch_for_month(AsyncMock(), _CONTRACT, REGION_FLANDERS, date(2026, 5, 1))
            is None
        )


async def test_fetch_for_month_is_none_for_an_unlisted_month() -> None:
    fetched = AsyncMock()
    with (
        patch.object(
            trevion,
            "fetch_text",
            AsyncMock(return_value=fixture_page("trevion", "tariefkaarten.html")),
        ),
        patch.object(trevion, "fetch_pdf_text", fetched),
    ):
        assert (
            await trevion.fetch_for_month(AsyncMock(), _CONTRACT, REGION_FLANDERS, date(2026, 2, 1))
            is None
        )
    fetched.assert_not_called()


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            trevion, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await trevion.fetch_for_month(AsyncMock(), _CONTRACT, REGION_FLANDERS, date(2026, 5, 1))


def test_october_card_brackets_the_product() -> None:
    """From October 2026 the title reads "Trevion Gas Flex (Particulier)"."""
    text = _card(_OCTOBER)
    assert "Trevion Gas Flex (Particulier)" in text
    snap = trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, text)
    assert snap.publication_label == "2026-10"
    assert snap.energy.price == pytest.approx(0.0868)
    assert trevion.published_index(text) == (date(2026, 9, 1), 75.35)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Gas Flex Particulier", "Gas Flex Professioneel"),
        ("Gas Flex (Particulier)", "Gas Flex (Professioneel)"),
    ],
)
def test_professional_card_is_refused(old: str, new: str) -> None:
    name = _SEPTEMBER if "(" not in old else _OCTOBER
    text = _card(name).replace(old, new)
    with pytest.raises(ExtractorError, match="not the Gas Flex Particulier card"):
        trevion.parse_snapshot(_CONTRACT, REGION_FLANDERS, text)


def test_other_region_is_refused() -> None:
    with pytest.raises(ExtractorError):
        trevion.parse_snapshot(_CONTRACT, REGION_WALLONIA, _card(_SEPTEMBER))


def test_contract_is_flemish_and_indexed() -> None:
    (contract,) = trevion.EXTRACTOR.contracts
    assert contract.id == _CONTRACT
    assert contract.label == "Trevion Gas Flex"
    assert contract.kind == "indexed"
    assert contract.regions == frozenset({REGION_FLANDERS})
