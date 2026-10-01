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

"""Belvus gas card extractor, against the 2026 cards in both layouts."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_LIMBURG,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.providers import belvus
from custom_components.be_gas_prices.providers._rates import IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError, SupplierSnapshot
from tests import fixture_page, fixture_text

VAT = 1.06
SEPTEMBER = "Tariefkaart_FlexOnline_GAS_2026-09.pdf"
OCTOBER = "Tariefkaart_FlexOnline_GAS_2026-10.pdf"
AUGUST = "Tariefkaart_FlexOnline_GAS_2026-08.pdf"
JANUARY = "Tariefkaart_FlexOnline_GAS_2026-01.pdf"


def _card(name: str) -> str:
    return fixture_text("belvus", name)


def _parse(name: str, contract: str = "belvus_flex_online") -> SupplierSnapshot:
    return belvus.parse_snapshot(contract, REGION_FLANDERS, _card(name), "url")


@pytest.mark.parametrize(
    ("name", "contract", "price", "fee", "factor", "base", "index"),
    [
        # "6,84 * / 6,77 **", "(TTF_RLP × 1,04) + € 3,5/MWh", "€ 50/jaar".
        (SEPTEMBER, "belvus_flex_online", 6.77, 50.0, 1.04, 3.5, 61.729),
        (
            "Tariefkaart_SmartPlus_GAS_2026-09.pdf",
            "belvus_smart_plus",
            8.19,
            130.0,
            1.1,
            13.98,
            61.729,
        ),
        # The August layout is the Energy Together template: "**€c 5,87/kWh".
        (AUGUST, "belvus_flex_online", 5.87, 50.0, 1.04, 3.5, 53.075),
    ],
)
def test_energy_leg_in_both_layouts(
    name: str, contract: str, price: float, fee: float, factor: float, base: float, index: float
) -> None:
    energy = _parse(name, contract).energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "TTF_RLP"
    assert not energy.settled
    assert energy.price == pytest.approx(price / 100.0 * VAT)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    assert energy.factor == pytest.approx(factor / 1000.0 * VAT)
    assert energy.base == pytest.approx(base / 1000.0 * VAT)
    # The "**" figure is the formula at the footnote's value, to 0,01 c.
    assert energy.at(index) == pytest.approx(energy.price, abs=0.00005 * VAT)


def test_card_month() -> None:
    september = _parse(SEPTEMBER)
    assert september.publication_label == "2026-09"
    assert september.valid_until == date(2026, 9, 30)
    assert _parse(AUGUST).valid_until == date(2026, 8, 31)


def test_both_layouts_map_the_misassigned_labels_alike() -> None:
    """September prints "Fluvius Midden-Vlaanderen 2,24 14,59 0,98 77,46",
    August wraps "FLUVIUS MIDDEN-" above "VLAANDEREN 2,24 14,59 0,98 77,46":
    both are Limburg's figures, and the row labelled Limburg is
    Halle-Vilvoorde's."""
    september = _parse(SEPTEMBER)
    august = _parse(AUGUST)
    assert set(september.dsos) == FLUVIUS_KEYS
    assert september.dsos == august.dsos
    limburg = september.dsos[DSO_FLUVIUS_LIMBURG]
    assert limburg.tiers[TIER_T1].proportional == pytest.approx(0.0224)
    assert limburg.tiers[TIER_T1].fixed_per_year == pytest.approx(14.59)
    assert limburg.tiers[TIER_T2].proportional == pytest.approx(0.0098)
    assert limburg.tiers[TIER_T2].fixed_per_year == pytest.approx(77.46)
    hv = september.dsos[DSO_FLUVIUS_HALLE_VILVOORDE]
    assert hv.tiers[TIER_T1].fixed_per_year == pytest.approx(17.59)
    assert hv.tiers[TIER_T2].fixed_per_year == pytest.approx(93.38)
    # "€ 18,92/jaar" and "€ c 0,165/kW jaar", for every area.
    for overlay in september.dsos.values():
        assert overlay.metering_per_year == pytest.approx(18.92)
        assert overlay.transport == pytest.approx(0.00165)


def test_levies_are_read_as_printed() -> None:
    for name in (SEPTEMBER, AUGUST):
        taxes = _parse(name).taxes
        assert taxes.excise_bands == (
            (12000.0, pytest.approx(0.008724)),
            (None, pytest.approx(0.009457)),
        )
        assert taxes.energy_contribution == pytest.approx(0.001057)
        assert taxes.vat_rate == 0.0
        assert taxes.card_vat_rate is None


def test_january_card_reprints_the_2025_table_and_is_refused() -> None:
    """The January to March 2026 cards print 2025's table, with "FLUVIUS
    LIMBURG 2,09 13,32 70,72 70,72" and databeheer 18,56."""
    with pytest.raises(ExtractorError, match="not degressive"):
        _parse(JANUARY)


def test_card_of_another_product_is_refused() -> None:
    with pytest.raises(ExtractorError):
        _parse(SEPTEMBER, "belvus_smart_plus")


def test_only_flanders() -> None:
    with pytest.raises(ExtractorError):
        belvus.parse_snapshot("belvus_flex_online", REGION_WALLONIA, _card(SEPTEMBER), "url")


def test_index_value_is_the_month_before_the_card() -> None:
    assert belvus.index_value(_card(SEPTEMBER)) == ("2026-08", pytest.approx(61.729))
    assert belvus.index_value(_card(AUGUST)) == ("2026-07", pytest.approx(53.075))
    # "laatst gekende waarde van Belpex TTF-DAM 12/2025: €27,652/MWh"; the
    # card's table is refused, its footnote is still the card's own.
    assert belvus.index_value(_card(JANUARY)) == ("2025-12", pytest.approx(27.652))
    # From October 2026: "laatst gekende waarde van TTF RLP-M 9/2026: €75,35/MWh".
    assert belvus.index_value(_card(OCTOBER)) == ("2026-09", pytest.approx(75.35))


def test_index_value_naming_another_month_is_dropped() -> None:
    text = _card(SEPTEMBER).replace("TTF-DAM 8/2026", "TTF-DAM 8/2036")
    assert belvus.index_value(text) is None


def test_listing_gives_every_month_newest_first() -> None:
    page = fixture_page("belvus", "historische-tariefkaarten.html")
    flex = belvus._CONTRACTS_BY_ID["belvus_flex_online"]
    months = belvus.listed_months(page, flex)
    assert months[0] == date(2026, 9, 1)
    assert months[-1] == date(2026, 1, 1)
    assert len(months) == 9
    smart = belvus._CONTRACTS_BY_ID["belvus_smart_plus"]
    assert belvus.listed_months(page, smart)[0] == date(2026, 9, 1)


def test_a_folder_that_is_no_month_is_skipped() -> None:
    flex = belvus._CONTRACTS_BY_ID["belvus_flex_online"]
    page = "".join(
        f'<a href="/public/tariefkaarten/{month}/Tariefkaart_FlexOnline_GAS.pdf">'
        for month in ("2026-13", "2026-00", "0000-09", "2026-09")
    )
    assert belvus.listed_months(page, flex) == [date(2026, 9, 1)]


async def test_fetch_reads_the_newest_listed_card() -> None:
    with (
        patch.object(
            belvus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("belvus", "historische-tariefkaarten.html")),
        ),
        patch.object(belvus, "fetch_pdf_text", AsyncMock(return_value=_card(SEPTEMBER))) as card,
    ):
        snap = await belvus.fetch(AsyncMock(), "belvus_flex_online", REGION_FLANDERS)
    url = "https://www.belvus.be/public/tariefkaarten/2026-09/Tariefkaart_FlexOnline_GAS.pdf"
    assert card.call_args.args[1] == url
    assert snap.source_url == url
    assert snap.supplier == "belvus"


async def test_fetch_for_month_builds_the_folder_url() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(belvus, "fetch_pdf_text", AsyncMock(return_value=_card(AUGUST))) as card,
    ):
        snap = await belvus.fetch_for_month(
            AsyncMock(), "belvus_flex_online", REGION_FLANDERS, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert card.call_args.args[1] == (
        "https://www.belvus.be/public/tariefkaarten/2026-08/Tariefkaart_FlexOnline_GAS.pdf"
    )


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(belvus, "fetch_pdf_text", AsyncMock(return_value=_card(SEPTEMBER))),
    ):
        assert (
            await belvus.fetch_for_month(
                AsyncMock(), "belvus_flex_online", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_missing_folder_is_none() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            belvus, "fetch_pdf_text", AsyncMock(side_effect=ExtractorError("HTTP 404 fetching x"))
        ),
    ):
        assert (
            await belvus.fetch_for_month(
                AsyncMock(), "belvus_smart_plus", REGION_FLANDERS, date(2025, 12, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            belvus, "fetch_pdf_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await belvus.fetch_for_month(
            AsyncMock(), "belvus_flex_online", REGION_FLANDERS, date(2026, 8, 1)
        )


async def test_fetch_for_month_does_not_ask_for_the_future() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(belvus, "fetch_pdf_text", AsyncMock()) as card,
    ):
        snap = await belvus.fetch_for_month(
            AsyncMock(), "belvus_flex_online", REGION_FLANDERS, date(2026, 10, 1)
        )
    assert snap is None
    card.assert_not_called()


async def test_fetch_for_month_counts_months_in_brussels_time() -> None:
    """22:30 UTC on 30 September is already October in Brussels, so October
    is this month rather than a month ahead."""
    with (
        freeze_time(datetime(2026, 9, 30, 22, 30)),
        patch.object(belvus, "fetch_pdf_text", AsyncMock(return_value=_card(SEPTEMBER))) as card,
    ):
        await belvus.fetch_for_month(
            AsyncMock(), "belvus_flex_online", REGION_FLANDERS, date(2026, 10, 1)
        )
    assert card.call_args.args[1] == (
        "https://www.belvus.be/public/tariefkaarten/2026-10/Tariefkaart_FlexOnline_GAS.pdf"
    )


async def test_fetch_index_reads_the_newest_cards() -> None:
    cards = {
        "https://www.belvus.be/public/tariefkaarten/2026-09/Tariefkaart_FlexOnline_GAS.pdf": _card(
            SEPTEMBER
        ),
        "https://www.belvus.be/public/tariefkaarten/2026-08/Tariefkaart_FlexOnline_GAS.pdf": _card(
            AUGUST
        ),
    }

    async def pdf(_session: object, url: str) -> str:
        return cards[url]

    with (
        patch.object(belvus, "_INDEX_MONTHS", 2),
        patch.object(
            belvus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("belvus", "historische-tariefkaarten.html")),
        ),
        patch.object(belvus, "fetch_pdf_text", AsyncMock(side_effect=pdf)),
    ):
        table = await belvus.fetch_index(AsyncMock())
    assert table == {
        "TTF_RLP": {"2026-08": pytest.approx(61.729), "2026-07": pytest.approx(53.075)}
    }


@pytest.mark.parametrize(
    ("error", "kept"),
    [("HTTP 404 fetching the July card", True), ("network error fetching x: timeout", False)],
)
async def test_fetch_index_passes_over_a_dead_card_link(error: str, kept: bool) -> None:
    """A July card the listing links but the site no longer serves: August
    and September still give their values. A site that is down fails."""

    async def pdf(_session: object, url: str) -> str:
        if "/2026-09/" in url:
            return _card(SEPTEMBER)
        if "/2026-08/" in url:
            return _card(AUGUST)
        raise ExtractorError(error)

    with (
        patch.object(belvus, "_INDEX_MONTHS", 3),
        patch.object(
            belvus,
            "fetch_text",
            AsyncMock(return_value=fixture_page("belvus", "historische-tariefkaarten.html")),
        ),
        patch.object(belvus, "fetch_pdf_text", AsyncMock(side_effect=pdf)),
    ):
        if not kept:
            with pytest.raises(ExtractorError, match="network"):
                await belvus.fetch_index(AsyncMock())
            return
        table = await belvus.fetch_index(AsyncMock())
    assert set(table["TTF_RLP"]) == {"2026-08", "2026-07"}


def test_extractor() -> None:
    extractor = belvus.EXTRACTOR
    assert extractor.id == "belvus"
    assert [c.id for c in extractor.contracts] == ["belvus_flex_online", "belvus_smart_plus"]
    assert extractor.regions() == frozenset({REGION_FLANDERS})
    assert all(c.kind == "indexed" for c in extractor.contracts)
    assert extractor.fetch_index is not None
