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

"""OCTA+ gas card extractor, against the October 2026 cards of the range
sold from that month, and Smart Variable's September card."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_WEST,
    DSO_ORES,
    DSO_RESA,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.month_cards import current_card
from custom_components.be_gas_prices.providers import octaplus
from custom_components.be_gas_prices.providers._pdf import render_through
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import fixture_page, fixture_text

_LISTING = "https://srv.octaplus.be/websiterest/getTarifArchive"
_SHEET = "https://srv.octaplus.be/websiterest/getTariffSheet"
BOOSTFLEX_WL = "G_OCTA_BOOSTFLEX_RE_WL_FR.pdf"
ECOBOOSTFLEX_VL = "G_OCTA_ECOBOOSTFLEX_RE_VL_FR.pdf"
BASICONLINE_VL = "G_OCTA_BASICONLINE_RE_VL_FR.pdf"
BOOSTFIX_WL = "G_OCTA_BOOSTFIX_RE_WL_FR.pdf"
ECOBOOSTFIX_VL = "G_OCTA_ECOBOOSTFIX_RE_VL_FR.pdf"
# Smart Variable's card for October, put up on 30 September, and September's.
SMARTVARIABLE_WL = "G_OCTA_SMARTVARIABLE_RE_WL_FR.pdf"
SMARTVARIABLE_VL_SEPTEMBER = "G_OCTA_SMARTVARIABLE_RE_VL_FR.pdf"


def _card(name: str) -> str:
    return fixture_text("octaplus", name, "layout")


def _v_test(month: str) -> float:
    """The forward value the variable cards price their estimate on: the last
    column ("ZTP Consommation moyenne ponderee") of paramètres_forward_fr.pdf."""
    text = fixture_text("octaplus", "paramètres_forward_fr.pdf", "layout")
    match = re.search(rf"^{re.escape(month)} .* (\d+,\d+)$", text, re.MULTILINE)
    assert match is not None
    return float(match.group(1).replace(",", "."))


def test_boost_flex_is_indexed_on_ztp_rlp_m() -> None:
    snap = octaplus.parse_snapshot("octaplus_boostflex", REGION_WALLONIA, _card(BOOSTFLEX_WL))
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTP RLP M"
    # Known only once the delivery month is over.
    assert not energy.settled
    assert energy.period == "month"
    # "ZTP RLP M * 1,020 + 3,200" in EUR/MWh excluding VAT, grossed up by 6%.
    assert energy.factor == pytest.approx(0.001020 * 1.06)
    assert energy.base == pytest.approx(0.003200 * 1.06)
    assert energy.formula == "ZTP RLP M * 1,020 + 3,200"
    # "Coût du gaz (c€/kWh) 7,82", the twelve-month estimate, and the fee.
    assert energy.price == pytest.approx(0.0782)
    assert energy.yearly_fixed_fee == pytest.approx(65.0)
    assert snap.publication_label == "2026-10"
    assert snap.valid_until == date(2026, 10, 31)
    assert snap.source_url == "https://files.octaplus.be/tariffs/" + BOOSTFLEX_WL
    # "TVAC" and no rate anywhere on the card.
    assert snap.taxes.card_vat_rate is None
    assert snap.taxes.vat_rate == 0.0


@pytest.mark.parametrize(
    ("contract", "region", "name", "factor", "base", "price", "fee"),
    [
        # "GAZ ECO BOOST FLEX", "ZTP RLP M * 1,010 + 16,910".
        ("octaplus_ecoboostflex", REGION_FLANDERS, ECOBOOSTFLEX_VL, 1.010, 16.910, 0.092, 150.0),
        # "GAZ BASIC ONLINE", "ZTP RLP M * 1,020 + 1,940".
        ("octaplus_basiconline", REGION_FLANDERS, BASICONLINE_VL, 1.020, 1.940, 0.0768, 40.0),
    ],
)
def test_the_other_indexed_products(
    contract: str, region: str, name: str, factor: float, base: float, price: float, fee: float
) -> None:
    energy = octaplus.parse_snapshot(contract, region, _card(name)).energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(factor / 1000 * 1.06)
    assert energy.base == pytest.approx(base / 1000 * 1.06)
    assert energy.price == pytest.approx(price)
    assert energy.yearly_fixed_fee == pytest.approx(fee)


def test_smart_variable_formula_is_spelled_differently() -> None:
    """ "ZTP RLP M* 1,15+ 10 EUR/MWh", with no HTVA; the estimate below shows
    it is excluding VAT like the others."""
    snap = octaplus.parse_snapshot(
        "octaplus_smartvariable", REGION_FLANDERS, _card(SMARTVARIABLE_VL_SEPTEMBER)
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(0.00115 * 1.06)
    assert energy.base == pytest.approx(0.010 * 1.06)
    assert energy.price == pytest.approx(0.0864)
    assert energy.yearly_fixed_fee == pytest.approx(160.0)


def test_printed_estimate_is_the_formula_at_the_v_test_value() -> None:
    """The card says its price is the formula at the current V-test value,
    published at octaplus.be/prixattendus: 62,209 EUR/MWh for 09/2026."""
    energy = octaplus.parse_snapshot(
        "octaplus_smartvariable", REGION_FLANDERS, _card(SMARTVARIABLE_VL_SEPTEMBER)
    ).energy
    assert isinstance(energy, IndexedRates)
    assert energy.at(_v_test("09/2026")) == pytest.approx(energy.price, abs=5e-5)


@pytest.mark.parametrize(
    ("contract", "region", "name", "price", "fee"),
    [
        ("octaplus_boostfix", REGION_WALLONIA, BOOSTFIX_WL, 0.0881, 110.0),
        ("octaplus_ecoboostfix", REGION_FLANDERS, ECOBOOSTFIX_VL, 0.1002, 150.0),
    ],
)
def test_fixed_cards(contract: str, region: str, name: str, price: float, fee: float) -> None:
    snap = octaplus.parse_snapshot(contract, region, _card(name))
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.price == pytest.approx(price)
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.publication_label == "2026-10"


def test_flanders_table() -> None:
    snap = octaplus.parse_snapshot("octaplus_ecoboostfix", REGION_FLANDERS, _card(ECOBOOSTFIX_VL))
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "Fluvius West 19,03 2,69 101,02 1,05 18,92 0,165"
    west = snap.dsos[DSO_FLUVIUS_WEST]
    assert west.tiers[TIER_T1].fixed_per_year == pytest.approx(19.03)
    assert west.tiers[TIER_T1].proportional == pytest.approx(0.0269)
    assert west.tiers[TIER_T2].fixed_per_year == pytest.approx(101.02)
    assert west.tiers[TIER_T2].proportional == pytest.approx(0.0105)
    assert west.metering_per_year == pytest.approx(18.92)
    assert west.transport == pytest.approx(0.00165)
    assert set(west.tiers) == {TIER_T1, TIER_T2}
    # "Fluvius Kempen 16,17 2,27 85,83 0,88 18,92 0,165"
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.0227)
    assert kempen.tiers[TIER_T2].fixed_per_year == pytest.approx(85.83)


def test_wallonia_table_collapses_the_ores_sub_areas() -> None:
    snap = octaplus.parse_snapshot("octaplus_boostflex", REGION_WALLONIA, _card(BOOSTFLEX_WL))
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    # "RESA 34,59 4,64 122,05 2,53 - 0,165"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].fixed_per_year == pytest.approx(34.59)
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.0253)
    assert resa.transport == pytest.approx(0.00165)
    assert resa.metering_per_year == 0.0
    # "ORES (Namur) 31,91 4,29 140,93 2,21 - 0,165", the same on all five rows.
    ores = snap.dsos[DSO_ORES]
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.0429)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.0221)


def test_walloon_levies_as_printed() -> None:
    """ "Consommation entre 0 & 12.000 kWh 1,0929 0,1058 0,0075" and
    "Consommation > 12.000 kWh 1,1830 0,00": the excise, the energy
    contribution the law zeroed in August but the card still prints, and
    the connection fee."""
    taxes = octaplus.parse_snapshot(
        "octaplus_boostflex", REGION_WALLONIA, _card(BOOSTFLEX_WL)
    ).taxes
    [(low_upper, low), (high_upper, high)] = taxes.excise_bands
    assert (low_upper, high_upper) == (12000.0, None)
    assert (low, high) == pytest.approx((0.010929, 0.01183))
    assert taxes.energy_contribution == pytest.approx(0.001058)
    assert taxes.connection_fee == pytest.approx(0.000075)
    assert taxes.osp_by_caliber is None


def test_flemish_card_has_no_connection_fee() -> None:
    taxes = octaplus.parse_snapshot(
        "octaplus_ecoboostfix", REGION_FLANDERS, _card(ECOBOOSTFIX_VL)
    ).taxes
    assert taxes.excise_bands[0][1] == pytest.approx(0.010929)
    assert taxes.connection_fee == 0.0


def test_flemish_card_read_for_wallonia_fails_loud() -> None:
    """The Flemish card prints both DSO tables but no connection fee."""
    with pytest.raises(ExtractorError, match="connection fee"):
        octaplus.parse_snapshot("octaplus_ecoboostfix", REGION_WALLONIA, _card(ECOBOOSTFIX_VL))


def test_brussels_is_not_sold() -> None:
    with pytest.raises(ExtractorError):
        octaplus.parse_snapshot("octaplus_boostflex", REGION_BRUSSELS, _card(BOOSTFLEX_WL))


@pytest.mark.parametrize(
    ("contract", "name", "named"),
    [
        # Two products whose headings share every word but one.
        ("octaplus_ecoboostflex", BOOSTFLEX_WL, "BOOST FLEX"),
        ("octaplus_boostfix", BOOSTFLEX_WL, "BOOST FLEX"),
        ("octaplus_boostflex", SMARTVARIABLE_WL, "SMARTVARIABLE"),
    ],
)
def test_card_of_another_product_is_refused(contract: str, name: str, named: str) -> None:
    with pytest.raises(ExtractorError, match=f"the card is for {named}"):
        octaplus.parse_snapshot(contract, REGION_WALLONIA, _card(name))


def test_a_formula_printed_with_decimal_points_is_read_whole() -> None:
    text = _card(BOOSTFLEX_WL).replace("* 1,020 + 3,200", "* 1.020 + 3.200")
    energy = octaplus.parse_snapshot("octaplus_boostflex", REGION_WALLONIA, text).energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(0.001020 * 1.06)
    assert energy.base == pytest.approx(0.003200 * 1.06)


def test_card_that_lost_its_formula_fails_loud() -> None:
    text = _card(BOOSTFLEX_WL).replace("ZTP RLP M *", "ZTP RLP *")
    with pytest.raises(ExtractorError, match="formula"):
        octaplus.parse_snapshot("octaplus_boostflex", REGION_WALLONIA, text)


def test_regions_and_kinds() -> None:
    """The range sold from October 2026; Flux, Eco Flux, Fixed and Eco Fixed
    were withdrawn."""
    by_id = {c.id: c for c in octaplus.EXTRACTOR.contracts}
    assert set(by_id) == {
        "octaplus_basiconline",
        "octaplus_boostflex",
        "octaplus_ecoboostflex",
        "octaplus_smartvariable",
        "octaplus_boostfix",
        "octaplus_ecoboostfix",
    }
    assert {c.regions for c in by_id.values()} == {frozenset({REGION_FLANDERS, REGION_WALLONIA})}
    assert by_id["octaplus_smartvariable"].kind == "indexed"
    assert by_id["octaplus_boostfix"].kind == "fixed"


async def test_fetch_uses_the_file_name_the_tariff_page_links() -> None:
    text = _card(BOOSTFIX_WL)
    with patch.object(octaplus, "fetch_pdf_text_layout", AsyncMock(return_value=text)) as fetched:
        snap = await octaplus.fetch(AsyncMock(), "octaplus_boostfix", REGION_WALLONIA)
    url = "https://files.octaplus.be/tariffs/" + BOOSTFIX_WL
    assert fetched.call_args.args[1] == url
    assert snap.source_url == url


async def test_probe_heads_the_card() -> None:
    """The month is in the key, so a card read before its month began is
    read again once it begins."""
    with (
        freeze_time(datetime(2026, 9, 30, 21, 59)),
        patch.object(
            octaplus, "head_freshness_key", AsyncMock(return_value="Wed, 30 Sep 2026")
        ) as head,
    ):
        probed = AsyncMock(), "octaplus_smartvariable", REGION_FLANDERS
        assert await octaplus.probe(*probed) == "Wed, 30 Sep 2026 2026-09"
        with freeze_time(datetime(2026, 9, 30, 22, 1)):
            assert await octaplus.probe(*probed) == "Wed, 30 Sep 2026 2026-10"
    assert head.call_args.args[1] == (
        "https://files.octaplus.be/tariffs/G_OCTA_SMARTVARIABLE_RE_VL_FR.pdf"
    )


_SEPTEMBER_ARCHIVE = {
    "AnneeMois=202609": "getTarifArchive_WL_202609_G_RE.json",
    "RequestedPDF=2026-09+G+OCTA%2BSMARTVARIABLE+RE+WL+FR.pdf": (
        "getTariffSheet_2026-09 G OCTA+SMARTVARIABLE RE WL FR.json"
    ),
}


async def test_the_running_month_s_card_stands_in_for_one_put_up_early() -> None:
    """On 30 September afternoon Smart Variable's card online is already
    October's: fetch returns it as it stands, which the card archive stores,
    and an installation is priced on September's from OCTA+'s archive."""
    online = AsyncMock(return_value=_card(SMARTVARIABLE_WL))
    archive = AsyncMock(side_effect=_archive(_SEPTEMBER_ARCHIVE))
    with (
        patch.object(octaplus, "fetch_pdf_text_layout", online),
        patch.object(octaplus, "fetch_text", archive),
    ):
        snap = await octaplus.fetch(AsyncMock(), "octaplus_smartvariable", REGION_WALLONIA)
        assert snap.publication_label == "2026-10"
        archive.assert_not_called()
        snap, source = await current_card(
            AsyncMock(),
            octaplus.EXTRACTOR,
            "octaplus_smartvariable",
            REGION_WALLONIA,
            "2026-09",
            use_archive=False,
        )
        assert (snap.publication_label, source) == ("2026-09", "live")
        # "Coût du gaz (c€/kWh) 8,64" on the September card, 9,49 on October's.
        assert snap.energy.price == pytest.approx(0.0864)
        archive.reset_mock()
        snap, _source = await current_card(
            AsyncMock(),
            octaplus.EXTRACTOR,
            "octaplus_smartvariable",
            REGION_WALLONIA,
            "2026-10",
            use_archive=False,
        )
        assert snap.publication_label == "2026-10"
        archive.assert_not_called()


async def test_a_card_put_up_early_is_kept_over_another_month_s() -> None:
    """The archive answering with another month's card is no stand-in."""
    online = AsyncMock(return_value=_card(SMARTVARIABLE_WL))
    august = octaplus.parse_snapshot(
        "octaplus_smartvariable", REGION_WALLONIA, _card(SMARTVARIABLE_WL)
    )
    august = replace(august, publication_label="2026-08", valid_until=date(2026, 8, 31))
    stub = replace(octaplus.EXTRACTOR, fetch_for_month=AsyncMock(return_value=august))
    with patch.object(octaplus, "fetch_pdf_text_layout", online):
        snap, _source = await current_card(
            AsyncMock(),
            stub,
            "octaplus_smartvariable",
            REGION_WALLONIA,
            "2026-09",
            use_archive=False,
        )
    assert snap.publication_label == "2026-10"


async def test_a_card_put_up_early_is_kept_when_the_archive_has_none() -> None:
    online = AsyncMock(return_value=_card(SMARTVARIABLE_WL))
    with (
        patch.object(octaplus, "fetch_pdf_text_layout", online),
        patch.object(octaplus, "fetch_text", AsyncMock(side_effect=_archive({}))),
    ):
        snap, _source = await current_card(
            AsyncMock(),
            octaplus.EXTRACTOR,
            "octaplus_smartvariable",
            REGION_WALLONIA,
            "2026-09",
            use_archive=False,
        )
    assert snap.publication_label == "2026-10"


def test_index_table_reads_ztp_rlp_by_delivery_month() -> None:
    table = octaplus.parse_index(fixture_text("octaplus", "paramètres_gaz_fr.pdf", "layout"))
    values = table["ZTP RLP M"]
    # "08/2026 53,734 45,112 61,729 61,899 -": the fourth column.
    assert values["2026-08"] == pytest.approx(61.899)
    assert values["2026-07"] == pytest.approx(53.043)
    assert values["2024-11"] == pytest.approx(44.32)
    assert values["2024-07"] == pytest.approx(32.037)
    # September is not over: "09/2026 61,479 45,112 - - -".
    assert "2026-09" not in values
    assert len(values) == 26


def test_index_table_with_another_heading_fails_loud() -> None:
    text = fixture_text("octaplus", "paramètres_gaz_fr.pdf", "layout")
    with pytest.raises(ExtractorError):
        octaplus.parse_index(text.replace("ZTP RLP Mois", "ZTP Mois"))


def _archive(pages: dict[str, str]) -> Callable[..., Any]:
    """A fetch_text stand-in answering each URL with a fixture page: the
    listing by region and month, the sheet by card name."""

    async def fetch(_session: Any, url: str, **_kwargs: Any) -> str:
        for key, name in pages.items():
            if key in url:
                return fixture_page("octaplus", name)
        raise ExtractorError(f"HTTP 404 fetching {url}")

    return fetch


@pytest.mark.parametrize("payload", ["n0t b@se64", "Fichier non trouvé"])
def test_an_archive_sheet_that_is_not_base64_is_an_extractor_error(payload: str) -> None:
    """Text that is not ASCII raises a plain ValueError out of b64decode,
    which the month cache would not catch."""
    reply = json.dumps({"Response": {"TariffSheet": f"data:application/pdf;base64,{payload}"}})
    with pytest.raises(ExtractorError, match="bad base64"):
        octaplus._archive_pdf(reply, "G_OCTA_BOOSTFLEX_RE_WL_FR")


async def test_fetch_for_month_reads_the_archived_card_through_the_render_hook() -> None:
    rendered: list[tuple[str, str]] = []

    async def hook(variant: str, url: str, payload: bytes, render: Callable[[bytes], str]) -> str:
        rendered.append((variant, url))
        return render(payload)

    pages = {
        "AnneeMois=202610": "getTarifArchive_WL_202610_G_RE.json",
        "RequestedPDF=2026-10+G+OCTA%2BBOOSTFLEX+RE+WL+FR.pdf": (
            "getTariffSheet_2026-10 G OCTA+BOOSTFLEX RE WL FR.json"
        ),
    }
    fetched = AsyncMock(side_effect=_archive(pages))
    with patch.object(octaplus, "fetch_text", fetched), render_through(hook):
        snap = await octaplus.fetch_for_month(
            AsyncMock(), "octaplus_boostflex", REGION_WALLONIA, date(2026, 10, 1)
        )
    assert snap is not None
    assert snap.publication_label == "2026-10"
    assert snap.valid_until == date(2026, 10, 31)
    assert snap.energy.price == pytest.approx(0.0782)
    listing = fetched.call_args_list[0].args[1]
    assert listing == (
        f"{_LISTING}?Lang=FR&Region=WL&AnneeMois=202610&Nrj=G&Canal=website&TypeContrat=RE"
    )
    sheet = f"{_SHEET}?Canal=website&RequestedPDF=2026-10+G+OCTA%2BBOOSTFLEX+RE+WL+FR.pdf"
    assert fetched.call_args_list[1].args[1] == sheet
    assert rendered == [("layout", sheet)]
    assert snap.source_url == sheet


async def test_fetch_for_month_reads_a_sheet_the_archive_does_not_hold_as_none() -> None:
    """A name the sheet endpoint does not hold is answered 200 with "Ok":
    "False", which is no card rather than a failed fetch."""
    pages = {
        "AnneeMois=202610": "getTarifArchive_WL_202610_G_RE.json",
        "RequestedPDF=": "getTariffSheet_missing.json",
    }
    fetched = AsyncMock(side_effect=_archive(pages))
    with patch.object(octaplus, "fetch_text", fetched):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_boostfix", REGION_WALLONIA, date(2026, 10, 1)
            )
            is None
        )
    assert (
        "RequestedPDF=2026-10+G+OCTA%2BBOOSTFIX+RE+WL+FR.pdf" in fetched.call_args_list[1].args[1]
    )


def test_an_archive_name_with_a_doubled_letter_is_taken_when_it_is_the_only_one() -> None:
    """March 2026 listed its fixed cards as FIXEDD and ECOFIXEDD."""
    contract = octaplus._CONTRACTS_BY_ID["octaplus_boostfix"]
    doubled = "2026-03 G OCTA+BOOSTFIXX RE WL FR.pdf"
    names = [doubled, "2026-03 G OCTA+ECOBOOSTFIX RE WL FR.pdf"]
    assert octaplus._pick_archive_name(names, contract, "WL", date(2026, 3, 1)) == doubled
    twice = [*names, "2026-03 G OCTA+BOOSSTFIX RE WL FR.pdf"]
    assert octaplus._pick_archive_name(twice, contract, "WL", date(2026, 3, 1)) is None


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    """The July listing names the card, but the sheet answers September's."""
    pages = {
        "AnneeMois=202607": "getTarifArchive_WL_202607_G_RE.json",
        "RequestedPDF=": "getTariffSheet_2026-09 G OCTA+SMARTVARIABLE RE WL FR.json",
    }
    with patch.object(octaplus, "fetch_text", AsyncMock(side_effect=_archive(pages))):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_smartvariable", REGION_WALLONIA, date(2026, 7, 1)
            )
            is None
        )


async def test_fetch_for_month_has_no_card_for_the_older_template() -> None:
    """The May 2026 card is the template before the June redesign ("Tarif :
    Smart Variable")."""
    pages = {
        "AnneeMois=202605": "getTarifArchive_WL_202605_G_RE.json",
        "RequestedPDF=": "getTariffSheet_2026-05 G OCTA+SMARTVARIABLE RE WL FR.json",
    }
    with patch.object(octaplus, "fetch_text", AsyncMock(side_effect=_archive(pages))):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_smartvariable", REGION_WALLONIA, date(2026, 5, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    with (
        patch.object(
            octaplus,
            "fetch_text",
            AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x")),
        ),
        pytest.raises(ExtractorError),
    ):
        await octaplus.fetch_for_month(
            AsyncMock(), "octaplus_boostflex", REGION_WALLONIA, date(2026, 10, 1)
        )


async def test_fetch_for_month_asks_nothing_for_brussels() -> None:
    fetched = AsyncMock()
    with patch.object(octaplus, "fetch_text", fetched):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_boostflex", REGION_BRUSSELS, date(2026, 10, 1)
            )
            is None
        )
    fetched.assert_not_called()
