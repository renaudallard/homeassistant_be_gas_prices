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

"""Sparki gas card extractor, against the September 2026 cards."""

from __future__ import annotations

import re
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_ZENNE_DIJLE,
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
from custom_components.be_gas_prices.providers import sparki
from custom_components.be_gas_prices.providers._rates import VariableRates
from custom_components.be_gas_prices.providers.base import CardNotReadableError, ExtractorError
from tests import approx, fixture_page, fixture_text

SS_NL = "Sparki_Tariefkaart_september_Particulier_SelfService_Gas_NL.pdf"
SS_FR = "Sparki_Tariefkaart_september_Particulier_SelfService_Gas_FR.pdf"
AYS_NL = "Sparki_Tariefkaart_september_Particulier_AtYourService_Gas_NL.pdf"
SS_NL_AUGUST = "Sparki_Tariefkaart_augustus_Particulier_SelfService_Gas_NL.pdf"
UPLOADS = "https://sparki.be/wp-content/uploads"


def _card(name: str) -> str:
    return fixture_text("sparki", name, "layout")


def test_price_is_kept_as_printed() -> None:
    """ "21,20 7,79 Geschatte maandprijs": the formula names an undefined
    TTF, so the card's price is the energy leg and the formula is only kept."""
    snap = sparki.parse_snapshot("sparki_self_service", REGION_FLANDERS, _card(SS_NL), "url")
    assert snap.energy == VariableRates(
        price=approx(0.0779),
        yearly_fixed_fee=approx(21.20),
        formula="((0,105*TTF)+0,8)*1,06",
    )
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)


def test_printed_price_is_the_formula_at_the_vnr_estimate() -> None:
    """The NL card prints the index its price is "based on the VNR
    methodology": 6,24 c EUR/kWh, 62,4 EUR/MWh, which gives the 7,79 printed.
    A forward estimate, not a month's settled value."""
    text = _card(SS_NL)
    match = re.search(r"VNR methodologie\s+(\d+,\d+) c€/kWh", text)
    assert match is not None
    vnr = float(match.group(1).replace(",", ".")) * 10.0
    assert vnr == pytest.approx(62.4)
    price = sparki.parse_snapshot("sparki_self_service", REGION_FLANDERS, text, "url").energy.price
    # ((0,105*TTF)+0,8)*1,06 in c EUR/kWh, TTF in EUR/MWh.
    assert price * 100.0 == pytest.approx((0.105 * vnr + 0.8) * 1.06, abs=0.005)


def test_at_your_service_differs_by_its_fee() -> None:
    energy = sparki.parse_snapshot(
        "sparki_at_your_service", REGION_FLANDERS, _card(AYS_NL), "url"
    ).energy
    assert energy.price == pytest.approx(0.0779)
    assert energy.yearly_fixed_fee == pytest.approx(89.04)


def test_flanders_table_and_levies() -> None:
    snap = sparki.parse_snapshot("sparki_self_service", REGION_FLANDERS, _card(SS_NL), "url")
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius Antwerpen 15,68 2,257 83,22 0,905 562,63 0,586 18,92 0,165"
    antwerpen = snap.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert antwerpen.tiers[TIER_T1].fixed_per_year == pytest.approx(15.68)
    assert antwerpen.tiers[TIER_T1].proportional == pytest.approx(0.02257)
    assert antwerpen.tiers[TIER_T2].fixed_per_year == pytest.approx(83.22)
    assert antwerpen.tiers[TIER_T2].proportional == pytest.approx(0.00905)
    assert antwerpen.tiers[TIER_T3].fixed_per_year == pytest.approx(562.63)
    assert antwerpen.tiers[TIER_T3].proportional == pytest.approx(0.00586)
    assert antwerpen.metering_per_year == pytest.approx(18.92)
    assert antwerpen.transport == pytest.approx(0.00165)
    # "Fluvius Zenne-Dijle 18,40 2,602 97,72 1,017 660,65 0,641 18,92 0,165"
    zd = snap.dsos[DSO_FLUVIUS_ZENNE_DIJLE]
    assert zd.tiers[TIER_T1].fixed_per_year == pytest.approx(18.40)
    assert zd.tiers[TIER_T2].proportional == pytest.approx(0.01017)
    assert zd.tiers[TIER_T3].proportional == pytest.approx(0.00641)
    taxes = snap.taxes
    # Pre-August 2026 figures, read as printed; "0.9864" has a decimal point.
    assert taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087338)),
        (None, pytest.approx(0.009864)),
    )
    assert taxes.energy_contribution == pytest.approx(0.0010577)
    assert taxes.connection_fee == 0.0
    assert taxes.vat_rate == 0.0
    assert taxes.card_vat_rate == pytest.approx(0.06)


def test_wallonia_card() -> None:
    snap = sparki.parse_snapshot("sparki_self_service", REGION_WALLONIA, _card(SS_FR), "url")
    assert snap.energy.price == pytest.approx(0.0779)
    assert snap.publication_label == "2026-09"
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "ORES (Brabant Wallon) 31,91 4,289 140,93 2,206 889,48 1,639 0,165", the
    # same on all five sub-area rows.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.04289)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.02206)
    assert ores.tiers[TIER_T3].proportional == pytest.approx(0.01639)
    assert ores.metering_per_year == 0.0
    assert ores.transport == pytest.approx(0.00165)
    # "TECTEO - RESA 34,59 4,640 122,05 2,529 962,74 2,241 0,165"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.02529)
    # "Redevance raccordement 0,00750" c EUR/kWh.
    assert snap.taxes.connection_fee == pytest.approx(0.000075)
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_card_for_another_product_or_region_is_refused() -> None:
    with pytest.raises(ExtractorError, match="Self Service"):
        sparki.parse_snapshot("sparki_self_service", REGION_FLANDERS, _card(AYS_NL), "url")
    with pytest.raises(ExtractorError, match="wallonia"):
        sparki.parse_snapshot("sparki_self_service", REGION_WALLONIA, _card(SS_NL), "url")
    with pytest.raises(ExtractorError):
        sparki.parse_snapshot("sparki_self_service", REGION_BRUSSELS, _card(SS_NL), "url")


def test_walloon_card_without_its_connection_fee_fails_loud() -> None:
    text = _card(SS_FR).replace("Redevance raccordement", "Redevance")
    with pytest.raises(ExtractorError, match="connection fee"):
        sparki.parse_snapshot("sparki_self_service", REGION_WALLONIA, text, "url")


def test_listing_newest_first_per_product_and_language() -> None:
    page = fixture_page("sparki", "tariefkaarten.html")
    self_service = sparki._CONTRACTS_BY_ID["sparki_self_service"]
    cards = sparki.listed_cards(page, self_service, REGION_FLANDERS)
    assert cards[0] == (
        "september",
        f"{UPLOADS}/2026/09/Sparki_Tariefkaart_september_Particulier_SelfService_Gas_NL.pdf",
    )
    # April to September; the April to June cards were uploaded in July.
    assert [month for month, _ in cards] == [
        "september",
        "augustus",
        "juli",
        "juni",
        "mei",
        "april",
    ]
    assert cards[-1][1].startswith(f"{UPLOADS}/2026/07/")
    french = sparki.listed_cards(page, self_service, REGION_WALLONIA)
    assert french[0][1].endswith("_september_Particulier_SelfService_Gas_FR.pdf")


async def test_fetch_reads_the_newest_card() -> None:
    with (
        patch.object(
            sparki,
            "fetch_text",
            AsyncMock(return_value=fixture_page("sparki", "tariefkaarten.html")),
        ),
        patch.object(
            sparki, "fetch_pdf_text_layout", AsyncMock(return_value=_card(AYS_NL))
        ) as card,
    ):
        snap = await sparki.fetch(AsyncMock(), "sparki_at_your_service", REGION_FLANDERS)
    url = f"{UPLOADS}/2026/09/Sparki_Tariefkaart_september_Particulier_AtYourService_Gas_NL.pdf"
    assert card.call_args.args[1] == url
    assert snap.source_url == url


async def test_fetch_for_month_finds_the_month_by_name() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            sparki,
            "fetch_text",
            AsyncMock(return_value=fixture_page("sparki", "tariefkaarten.html")),
        ),
        patch.object(
            sparki, "fetch_pdf_text_layout", AsyncMock(return_value=_card(SS_NL_AUGUST))
        ) as card,
    ):
        snap = await sparki.fetch_for_month(
            AsyncMock(), "sparki_self_service", REGION_FLANDERS, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert snap.energy.price == pytest.approx(0.0702)
    assert card.call_args.args[1] == (
        f"{UPLOADS}/2026/08/Sparki_Tariefkaart_augustus_Particulier_SelfService_Gas_NL.pdf"
    )


async def test_fetch_for_month_reads_past_another_year_s_unreadable_card() -> None:
    """In September 2027 the listing links a September card for each year;
    the 2027 one failing to read does not hide the 2026 one."""
    name = "Sparki_Tariefkaart_september_Particulier_SelfService_Gas_NL.pdf"
    listing = "".join(f'<a href="{UPLOADS}/{year}/09/{name}">x</a>' for year in (2027, 2026))

    async def pdf(_session: object, url: str) -> str:
        if "/2027/" in url:
            raise CardNotReadableError("card has no text layer")
        return _card(SS_NL)

    with (
        freeze_time(datetime(2027, 9, 15, 12)),
        patch.object(sparki, "fetch_text", AsyncMock(return_value=listing)),
        patch.object(sparki, "fetch_pdf_text_layout", AsyncMock(side_effect=pdf)),
    ):
        snap = await sparki.fetch_for_month(
            AsyncMock(), "sparki_self_service", REGION_FLANDERS, date(2026, 9, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 9, 30)


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            sparki,
            "fetch_text",
            AsyncMock(return_value=fixture_page("sparki", "tariefkaarten.html")),
        ),
        patch.object(sparki, "fetch_pdf_text_layout", AsyncMock(return_value=_card(SS_NL))),
    ):
        assert (
            await sparki.fetch_for_month(
                AsyncMock(), "sparki_self_service", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_before_the_listing_is_none() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            sparki,
            "fetch_text",
            AsyncMock(return_value=fixture_page("sparki", "tariefkaarten.html")),
        ),
        patch.object(sparki, "fetch_pdf_text_layout", AsyncMock()) as card,
    ):
        assert (
            await sparki.fetch_for_month(
                AsyncMock(), "sparki_self_service", REGION_FLANDERS, date(2026, 3, 1)
            )
            is None
        )
    card.assert_not_called()


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            sparki, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x"))
        ),
        pytest.raises(ExtractorError),
    ):
        await sparki.fetch_for_month(
            AsyncMock(), "sparki_self_service", REGION_FLANDERS, date(2026, 8, 1)
        )


async def test_fetch_for_month_does_not_ask_for_the_future() -> None:
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(sparki, "fetch_text", AsyncMock()) as listing,
    ):
        assert (
            await sparki.fetch_for_month(
                AsyncMock(), "sparki_self_service", REGION_FLANDERS, date(2026, 10, 1)
            )
            is None
        )
    listing.assert_not_called()


def test_extractor() -> None:
    extractor = sparki.EXTRACTOR
    assert extractor.id == "sparki"
    assert [c.id for c in extractor.contracts] == ["sparki_self_service", "sparki_at_your_service"]
    assert extractor.regions() == frozenset({REGION_FLANDERS, REGION_WALLONIA})
    assert all(c.kind == "variable" for c in extractor.contracts)
    assert extractor.fetch_index is None
