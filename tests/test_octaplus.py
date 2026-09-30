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

"""OCTA+ gas card extractor, against the September 2026 cards."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

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
from custom_components.be_gas_prices.providers import octaplus
from custom_components.be_gas_prices.providers._pdf import render_through
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import fixture_page, fixture_text

_LISTING = "https://srv.octaplus.be/websiterest/getTarifArchive"
_SHEET = "https://srv.octaplus.be/websiterest/getTariffSheet"


def _card(name: str) -> str:
    return fixture_text("octaplus", name, "layout")


def _v_test(month: str) -> float:
    """The forward value the variable cards price their estimate on: the last
    column ("ZTP Consommation moyenne ponderee") of paramètres_forward_fr.pdf."""
    text = fixture_text("octaplus", "paramètres_forward_fr.pdf", "layout")
    match = re.search(rf"^{re.escape(month)} .* (\d+,\d+)$", text, re.MULTILINE)
    assert match is not None
    return float(match.group(1).replace(",", "."))


def test_flux_is_indexed_on_ztp_rlp_m() -> None:
    snap = octaplus.parse_snapshot(
        "octaplus_flux", REGION_WALLONIA, _card("G_OCTA_FLUX_RE_WL_FR.pdf")
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTP RLP M"
    # Known only once the delivery month is over.
    assert not energy.settled
    assert energy.period == "month"
    # "ZTP RLP M * 1,010 + 2,160" in EUR/MWh excluding VAT, grossed up by 6%.
    assert energy.factor == pytest.approx(0.001010 * 1.06)
    assert energy.base == pytest.approx(0.002160 * 1.06)
    assert energy.formula == "ZTP RLP M * 1,010 + 2,160"
    # "Coût du gaz (c€/kWh) 6,89", the twelve-month estimate, and the fee.
    assert energy.price == pytest.approx(0.0689)
    assert energy.yearly_fixed_fee == pytest.approx(65.0)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.source_url == "https://files.octaplus.be/tariffs/G_OCTA_FLUX_RE_WL_FR.pdf"
    # "TVAC" and no rate anywhere on the card.
    assert snap.taxes.card_vat_rate is None
    assert snap.taxes.vat_rate == 0.0


def test_smart_variable_formula_is_spelled_differently() -> None:
    """ "ZTP RLP M* 1,15+ 10 EUR/MWh", with no HTVA; the estimate below shows
    it is excluding VAT like the others."""
    snap = octaplus.parse_snapshot(
        "octaplus_smartvariable", REGION_FLANDERS, _card("G_OCTA_SMARTVARIABLE_RE_VL_FR.pdf")
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(0.00115 * 1.06)
    assert energy.base == pytest.approx(0.010 * 1.06)
    assert energy.price == pytest.approx(0.0864)
    assert energy.yearly_fixed_fee == pytest.approx(160.0)


@pytest.mark.parametrize(
    ("contract", "region", "name"),
    [
        ("octaplus_flux", REGION_WALLONIA, "G_OCTA_FLUX_RE_WL_FR.pdf"),
        ("octaplus_smartvariable", REGION_FLANDERS, "G_OCTA_SMARTVARIABLE_RE_VL_FR.pdf"),
    ],
)
def test_printed_estimate_is_the_formula_at_the_v_test_value(
    contract: str, region: str, name: str
) -> None:
    """The card says its price is the formula at the current V-test value,
    published at octaplus.be/prixattendus: 62,209 EUR/MWh for 09/2026."""
    energy = octaplus.parse_snapshot(contract, region, _card(name)).energy
    assert isinstance(energy, IndexedRates)
    assert energy.at(_v_test("09/2026")) == pytest.approx(energy.price, abs=5e-5)


@pytest.mark.parametrize(
    ("contract", "region", "name", "price", "fee"),
    [
        ("octaplus_fixed", REGION_WALLONIA, "G_OCTA_Fixed_RE_WL_FR.pdf", 0.0825, 65.0),
        ("octaplus_ecofixed", REGION_FLANDERS, "G_OCTA_ECOFIXED_RE_VL_FR.pdf", 0.0945, 130.0),
    ],
)
def test_fixed_cards(contract: str, region: str, name: str, price: float, fee: float) -> None:
    snap = octaplus.parse_snapshot(contract, region, _card(name))
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.price == pytest.approx(price)
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.publication_label == "2026-09"


def test_flanders_table() -> None:
    snap = octaplus.parse_snapshot(
        "octaplus_ecofixed", REGION_FLANDERS, _card("G_OCTA_ECOFIXED_RE_VL_FR.pdf")
    )
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
    snap = octaplus.parse_snapshot(
        "octaplus_flux", REGION_WALLONIA, _card("G_OCTA_FLUX_RE_WL_FR.pdf")
    )
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
        "octaplus_flux", REGION_WALLONIA, _card("G_OCTA_FLUX_RE_WL_FR.pdf")
    ).taxes
    [(low_upper, low), (high_upper, high)] = taxes.excise_bands
    assert (low_upper, high_upper) == (12000.0, None)
    assert (low, high) == pytest.approx((0.010929, 0.01183))
    assert taxes.energy_contribution == pytest.approx(0.001058)
    assert taxes.connection_fee == pytest.approx(0.000075)
    assert taxes.osp_by_caliber is None


def test_flemish_card_has_no_connection_fee() -> None:
    taxes = octaplus.parse_snapshot(
        "octaplus_ecofixed", REGION_FLANDERS, _card("G_OCTA_ECOFIXED_RE_VL_FR.pdf")
    ).taxes
    assert taxes.excise_bands[0][1] == pytest.approx(0.010929)
    assert taxes.connection_fee == 0.0


def test_flemish_card_read_for_wallonia_fails_loud() -> None:
    """The Flemish card prints both DSO tables but no connection fee."""
    with pytest.raises(ExtractorError, match="connection fee"):
        octaplus.parse_snapshot(
            "octaplus_ecofixed", REGION_WALLONIA, _card("G_OCTA_ECOFIXED_RE_VL_FR.pdf")
        )


def test_brussels_is_not_sold() -> None:
    with pytest.raises(ExtractorError):
        octaplus.parse_snapshot("octaplus_flux", REGION_BRUSSELS, _card("G_OCTA_FLUX_RE_WL_FR.pdf"))


def test_card_of_another_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="the card is for FLUX"):
        octaplus.parse_snapshot(
            "octaplus_ecoflux", REGION_WALLONIA, _card("G_OCTA_FLUX_RE_WL_FR.pdf")
        )


def test_card_that_lost_its_formula_fails_loud() -> None:
    text = _card("G_OCTA_FLUX_RE_WL_FR.pdf").replace("ZTP RLP M *", "ZTP RLP *")
    with pytest.raises(ExtractorError, match="formula"):
        octaplus.parse_snapshot("octaplus_flux", REGION_WALLONIA, text)


def test_regions_and_kinds() -> None:
    by_id = {c.id: c for c in octaplus.EXTRACTOR.contracts}
    assert set(by_id) == {
        "octaplus_flux",
        "octaplus_ecoflux",
        "octaplus_smartvariable",
        "octaplus_fixed",
        "octaplus_ecofixed",
    }
    assert {c.regions for c in by_id.values()} == {frozenset({REGION_FLANDERS, REGION_WALLONIA})}
    assert by_id["octaplus_smartvariable"].kind == "indexed"
    assert by_id["octaplus_fixed"].kind == "fixed"


async def test_fetch_uses_the_file_name_the_tariff_page_links() -> None:
    text = _card("G_OCTA_Fixed_RE_WL_FR.pdf")
    with patch.object(octaplus, "fetch_pdf_text_layout", AsyncMock(return_value=text)) as fetched:
        snap = await octaplus.fetch(AsyncMock(), "octaplus_fixed", REGION_WALLONIA)
    url = "https://files.octaplus.be/tariffs/G_OCTA_Fixed_RE_WL_FR.pdf"
    assert fetched.call_args.args[1] == url
    assert snap.source_url == url


async def test_probe_heads_the_card() -> None:
    with patch.object(
        octaplus, "head_freshness_key", AsyncMock(return_value="Mon, 31 Aug 2026")
    ) as head:
        assert await octaplus.probe(AsyncMock(), "octaplus_flux", REGION_FLANDERS) == (
            "Mon, 31 Aug 2026"
        )
    assert head.call_args.args[1] == "https://files.octaplus.be/tariffs/G_OCTA_FLUX_RE_VL_FR.pdf"


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
        octaplus._archive_pdf(reply, "G_OCTA_FLUX_RE_WL_FR")


async def test_fetch_for_month_reads_the_archived_card_through_the_render_hook() -> None:
    rendered: list[tuple[str, str]] = []

    async def hook(variant: str, url: str, payload: bytes, render: Callable[[bytes], str]) -> str:
        rendered.append((variant, url))
        return render(payload)

    pages = {
        "AnneeMois=202608": "getTarifArchive_WL_202608_G_RE.json",
        "RequestedPDF=2026-08+G+OCTA%2BFLUX+RE+WL+FR.pdf": "getTariffSheet_2026-08 G OCTA+FLUX RE WL FR.json",
    }
    fetched = AsyncMock(side_effect=_archive(pages))
    with patch.object(octaplus, "fetch_text", fetched), render_through(hook):
        snap = await octaplus.fetch_for_month(
            AsyncMock(), "octaplus_flux", REGION_WALLONIA, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.publication_label == "2026-08"
    assert snap.valid_until == date(2026, 8, 31)
    # "Coût du gaz (c€/kWh) 6,14" on the August card.
    assert snap.energy.price == pytest.approx(0.0614)
    listing = fetched.call_args_list[0].args[1]
    assert listing == (
        f"{_LISTING}?Lang=FR&Region=WL&AnneeMois=202608&Nrj=G&Canal=website&TypeContrat=RE"
    )
    sheet = f"{_SHEET}?Canal=website&RequestedPDF=2026-08+G+OCTA%2BFLUX+RE+WL+FR.pdf"
    assert fetched.call_args_list[1].args[1] == sheet
    assert rendered == [("layout", sheet)]
    assert snap.source_url == sheet


async def test_fetch_for_month_spells_fixed_as_the_archive_does() -> None:
    """The live file is G_OCTA_Fixed_...; the archive lists FIXED."""
    pages = {
        "AnneeMois=202608": "getTarifArchive_VL_202608_G_RE.json",
        "RequestedPDF=": "getTariffSheet_missing.json",
    }
    fetched = AsyncMock(side_effect=_archive(pages))
    with patch.object(octaplus, "fetch_text", fetched):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_fixed", REGION_FLANDERS, date(2026, 8, 1)
            )
            is None
        )
    assert "RequestedPDF=2026-08+G+OCTA%2BFIXED+RE+VL+FR.pdf" in fetched.call_args_list[1].args[1]


@pytest.mark.parametrize(
    ("contract", "listed"),
    [("octaplus_fixed", "OCTA%2BFIXEDD+RE"), ("octaplus_ecofixed", "OCTA%2BECOFIXEDD+RE")],
)
async def test_fetch_for_month_takes_the_one_name_with_a_doubled_letter(
    contract: str, listed: str
) -> None:
    """March 2026 lists its fixed cards as FIXEDD and ECOFIXEDD."""
    pages = {
        "AnneeMois=202603": "getTarifArchive_WL_202603_G_RE.json",
        "RequestedPDF=": "getTariffSheet_missing.json",
    }
    fetched = AsyncMock(side_effect=_archive(pages))
    with patch.object(octaplus, "fetch_text", fetched):
        await octaplus.fetch_for_month(AsyncMock(), contract, REGION_WALLONIA, date(2026, 3, 1))
    assert listed in fetched.call_args_list[1].args[1]


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    """The July listing names the card, but the sheet answers August's."""
    pages = {
        "AnneeMois=202607": "getTarifArchive_WL_202607_G_RE.json",
        "RequestedPDF=": "getTariffSheet_2026-08 G OCTA+FLUX RE WL FR.json",
    }
    with patch.object(octaplus, "fetch_text", AsyncMock(side_effect=_archive(pages))):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_flux", REGION_WALLONIA, date(2026, 7, 1)
            )
            is None
        )


async def test_fetch_for_month_has_no_card_for_the_older_template() -> None:
    """The May 2026 card is the template before the June redesign."""
    pages = {
        "AnneeMois=202605": "getTarifArchive_WL_202605_G_RE.json",
        "RequestedPDF=": "getTariffSheet_2026-05 G OCTA+FLUX RE WL FR.json",
    }
    with patch.object(octaplus, "fetch_text", AsyncMock(side_effect=_archive(pages))):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_flux", REGION_WALLONIA, date(2026, 5, 1)
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
            AsyncMock(), "octaplus_flux", REGION_WALLONIA, date(2026, 8, 1)
        )


async def test_fetch_for_month_asks_nothing_for_brussels() -> None:
    fetched = AsyncMock()
    with patch.object(octaplus, "fetch_text", fetched):
        assert (
            await octaplus.fetch_for_month(
                AsyncMock(), "octaplus_flux", REGION_BRUSSELS, date(2026, 8, 1)
            )
            is None
        )
    fetched.assert_not_called()
