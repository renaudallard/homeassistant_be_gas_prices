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

"""Engie gas card extractor, against the September 2026 cards."""

from __future__ import annotations

import re
import time
from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_KEMPEN,
    DSO_ORES,
    DSO_RESA,
    DSO_SIBELGA,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from custom_components.be_gas_prices.providers import engie
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates
from custom_components.be_gas_prices.providers._resolve import resolve_for_delivery
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import approx, fixture_page, fixture_text


def _card(name: str) -> str:
    return fixture_text("engie", name)


def test_easy_variable_priced_on_another_month_is_not_settled() -> None:
    """The October 2026 card first went out saying its price was "calculé
    sur la base du paramètre ZTP101 (Heren) du mois = 61,7680 €/MWh,
    d'application pour Septembre 2026": not its own month's value."""
    text = _card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf")
    assert "d’application pour Septembre\n2026" in text
    for stated in ("Août\n2026", "Septembre\n2025"):
        snap = engie.parse_snapshot(
            "engie_easy_variable",
            REGION_WALLONIA,
            text.replace("d’application pour Septembre\n2026", f"d’application pour {stated}"),
        )
        assert isinstance(snap.energy, IndexedRates)
        assert not snap.energy.settled


def test_easy_variable_is_settled_on_ztp101() -> None:
    """EASY Variable prices its month on ZTP101, known before delivery: the
    card's monthly price is the formula at that month's value, so it is final."""
    snap = engie.parse_snapshot(
        "engie_easy_variable", REGION_WALLONIA, _card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf")
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTP101"
    assert energy.settled
    assert energy.price == pytest.approx(0.08168)
    assert energy.yearly_fixed_fee == pytest.approx(55.0)
    # "Prelevement: 1,4055 + (0,1020 x ZTP101 (Heren)" in c EUR/kWh excluding
    # VAT, grossed up by the card's 6%.
    assert energy.factor == pytest.approx(0.001020 * 1.06)
    assert energy.base == pytest.approx(0.014055 * 1.06)
    # The card states the value it priced on: 61,7680 EUR/MWh.
    assert energy.at(61.768) == pytest.approx(0.08168, abs=5e-6)
    assert snap.publication_label == "2026-09"
    assert snap.valid_until == date(2026, 9, 30)
    assert snap.taxes.card_vat_rate == pytest.approx(0.06)


def test_flow_is_priced_at_the_last_known_ztpdam() -> None:
    """ZTPDAM is only known at month end, so the printed price is the formula
    at the last value ("Août 2026: 61,5370 €/MWh") and not settled."""
    snap = engie.parse_snapshot(
        "engie_flow", REGION_WALLONIA, _card("G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.index == "ZTPDAM"
    assert not energy.settled
    assert energy.price == pytest.approx(0.07643)
    assert energy.yearly_fixed_fee == pytest.approx(50.0)
    assert energy.at(61.537) == pytest.approx(0.07643, abs=5e-6)


def test_empty_house_has_its_own_formula_and_no_fee() -> None:
    snap = engie.parse_snapshot(
        "engie_empty_house", REGION_WALLONIA, _card("G_EMPTYHOUSE_R_GREY_C_I_00_W_F_202609.pdf")
    )
    energy = snap.energy
    assert isinstance(energy, IndexedRates)
    assert energy.factor == pytest.approx(0.002 * 1.06)
    assert energy.base == pytest.approx(0.01754 * 1.06)
    assert energy.price == pytest.approx(0.14905)
    assert energy.yearly_fixed_fee == 0.0


@pytest.mark.parametrize(
    ("contract", "name", "price", "fee"),
    [
        ("engie_easy_fixed", "G_EASY_R_GREY_C_F_12_W_F_202609.pdf", 0.07939, 60.0),
        ("engie_empower_fixed", "G_EMPOWER_R_GREY_C_F_00_W_F_202609.pdf", 0.07939, 60.0),
    ],
)
def test_fixed_cards(contract: str, name: str, price: float, fee: float) -> None:
    snap = engie.parse_snapshot(contract, REGION_WALLONIA, _card(name))
    assert snap.energy == FixedRates(price=approx(price), yearly_fixed_fee=fee)


@pytest.mark.parametrize(
    ("contract", "name", "crafted"),
    [
        (
            "engie_easy_fixed",
            "G_EASY_R_GREY_C_F_12_W_F_202609.pdf",
            "Redevance fixe " * 16000 + "\n",
        ),
        (
            "engie_easy_variable",
            "G_EASY_R_GREY_C_I_12_W_F_202609.pdf",
            "Prix mensuels" + "\n" * 16000,
        ),
    ],
    ids=["a line of fee headings", "blank lines after the monthly price heading"],
)
def test_crafted_price_blocks_are_read_past_in_time(contract: str, name: str, crafted: str) -> None:
    """The fixed card's pattern once scanned the rest of a line again from
    every "Redevance fixe" on it, and the variable card's split a run of
    blank lines after "Prix mensuels" every way before failing: either in
    front of the real card took 9,0 s or 7,0 s. The cards still read
    as they do alone."""
    card = _card(name)
    started = time.monotonic()
    snap = engie.parse_snapshot(contract, REGION_WALLONIA, crafted + "x\n" + card)
    assert time.monotonic() - started < 1.0
    assert snap == engie.parse_snapshot(contract, REGION_WALLONIA, card)


def test_wallonia_table_collapses_the_ores_sub_areas() -> None:
    snap = engie.parse_snapshot(
        "engie_easy_variable", REGION_WALLONIA, _card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf")
    )
    assert set(snap.dsos) == {DSO_ORES, DSO_RESA}
    ores = snap.dsos[DSO_ORES]
    # "ORES (Brabant Wallon) 31,91 4,289 140,93 2,206 889,48 1,639 0,165"
    assert ores.tiers[TIER_T1].fixed_per_year == pytest.approx(31.91)
    assert ores.tiers[TIER_T1].proportional == pytest.approx(0.04289)
    assert ores.tiers[TIER_T2].fixed_per_year == pytest.approx(140.93)
    assert ores.tiers[TIER_T2].proportional == pytest.approx(0.02206)
    assert ores.tiers[TIER_T3].fixed_per_year == pytest.approx(889.48)
    assert ores.transport == pytest.approx(0.00165)
    assert ores.metering_per_year == 0.0
    # "TECTEO - RESA 34,59 4,640 122,05 2,529 962,74 2,241 0,165"
    resa = snap.dsos[DSO_RESA]
    assert resa.tiers[TIER_T1].proportional == pytest.approx(0.0464)
    assert resa.tiers[TIER_T2].fixed_per_year == pytest.approx(122.05)
    assert resa.tiers[TIER_T2].proportional == pytest.approx(0.02529)


def test_wallonia_levies() -> None:
    snap = engie.parse_snapshot(
        "engie_easy_variable", REGION_WALLONIA, _card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf")
    )
    # "Redevance raccordement(3) 0,00750" c EUR/kWh, VAT exempt.
    assert snap.taxes.connection_fee == pytest.approx(0.000075)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0109286)),
        (None, pytest.approx(0.0118296)),
    )
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.osp_by_caliber is None


def test_a_walloon_card_without_the_connection_fee_is_read() -> None:
    """The law's fee is billed whatever a card prints."""
    text = _card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf").replace("Redevance raccordement", "x")
    snap = engie.parse_snapshot("engie_easy_variable", REGION_WALLONIA, text)
    assert snap.taxes.connection_fee == 0.0


def test_a_july_card_prints_the_energy_contribution() -> None:
    """Up to July 2026 the cards print "Cotisation sur l'énergie 0,10577",
    the law's 0,9978 EUR/MWh rounded, which a July delivery is billed on;
    the August law zeroed it."""
    snap = engie.parse_snapshot(
        "engie_easy_variable", REGION_WALLONIA, _card("G_EASY_R_GREY_C_I_12_W_F_202607.pdf")
    )
    assert snap.publication_label == "2026-07"
    assert snap.taxes.energy_contribution == pytest.approx(0.0010577)
    assert snap.taxes.excise_bands == (
        (12000.0, pytest.approx(0.0087238)),
        (None, pytest.approx(0.0098914)),
    )
    july = resolve_for_delivery(snap, date(2026, 7, 1))
    assert july.taxes.energy_contribution == pytest.approx(0.0009978 * 1.06)


def test_a_card_before_august_without_the_contribution_is_refused() -> None:
    text = _card("G_EASY_R_GREY_C_I_12_W_F_202607.pdf").replace("Cotisation sur l", "Cotis")
    with pytest.raises(ExtractorError, match="energy contribution"):
        engie.parse_snapshot("engie_easy_variable", REGION_WALLONIA, text)


def test_flanders_table_carries_the_data_management_fee() -> None:
    snap = engie.parse_snapshot(
        "engie_easy_variable", REGION_FLANDERS, _card("G_EASY_R_GREY_C_I_12_V_F_202609.pdf")
    )
    assert set(snap.dsos) == FLUVIUS_KEYS
    # "FLUVIUS KEMPEN 16,17 2,275 85,83 0,882 580,25 0,552 18,92 0,165"
    kempen = snap.dsos[DSO_FLUVIUS_KEMPEN]
    assert kempen.tiers[TIER_T1].fixed_per_year == pytest.approx(16.17)
    assert kempen.tiers[TIER_T1].proportional == pytest.approx(0.02275)
    assert kempen.tiers[TIER_T2].proportional == pytest.approx(0.00882)
    assert kempen.metering_per_year == pytest.approx(18.92)
    assert kempen.transport == pytest.approx(0.00165)
    assert snap.taxes.connection_fee == 0.0


def test_brussels_card_reads_the_per_meter_levy() -> None:
    snap = engie.parse_snapshot(
        "engie_easy_variable", REGION_BRUSSELS, _card("G_EASY_R_GREY_C_I_36_B_F_202609.pdf")
    )
    sibelga = snap.dsos[DSO_SIBELGA]
    # "SIBELGA 15,90 1,990 43,07 1,447 1001,55 0,808 24,95 449,24 0,165": the
    # 449,24 monthly-reading fee is not what a yearly-read meter pays.
    assert sibelga.tiers[TIER_T1].fixed_per_year == pytest.approx(15.90)
    assert sibelga.tiers[TIER_T3].fixed_per_year == pytest.approx(1001.55)
    assert sibelga.metering_per_year == pytest.approx(24.95)
    assert sibelga.transport == pytest.approx(0.00165)
    assert snap.taxes.osp_by_caliber == {
        "q10_le5000": pytest.approx(3.56),
        "q10_gt5000": pytest.approx(12.59),
        "q16": pytest.approx(30.40),
        "q25": pytest.approx(75.18),
        "q40": pytest.approx(150.35),
        "q65": pytest.approx(376.00),
        "q100": pytest.approx(522.79),
        "q160": pytest.approx(671.36),
        "gt160": pytest.approx(970.41),
    }


def test_card_for_another_region_is_refused() -> None:
    with pytest.raises(ExtractorError):
        engie.parse_snapshot(
            "engie_basic_online",
            REGION_BRUSSELS,
            _card("G_BASIC_ONLINE_R_GREY_C_I_24_W_F_202609.pdf"),
        )


def test_unknown_contract_is_refused() -> None:
    with pytest.raises(ExtractorError):
        engie.parse_snapshot(
            "engie_dynamic", REGION_WALLONIA, _card("G_FLOW_R_GREY_C_I_24_W_F_202609.pdf")
        )


def test_card_that_lost_its_formula_fails_loud() -> None:
    text = _card("G_FLOW_R_GREY_C_I_24_W_F_202609.pdf").replace("Prélèvement", "Prelev")
    with pytest.raises(ExtractorError):
        engie.parse_snapshot("engie_flow", REGION_WALLONIA, text)


_FLOW_2609 = "G_FLOW_R_GREY_C_I_24_W_F_202609.pdf"
_FLOW_FORMULA = "Prélèvement: 0,9335 + (0,1020 x ZTPDAM (Heren)"
_RESULT_SENTENCE = "\nLe résultat de ces formules est un montant exprimé en c€/kWh."
_FLOW_FACTOR_TEN_TIMES = ("(0,1020 x ZTPDAM", "(1,0200 x ZTPDAM")
_FLOW_INDEX_IN_CENTS = ("connue (Août 2026: 61,5370\n€/MWh)", "connue (Août 2026: 6,15370\nc€/kWh)")


def _flow_energy(text: str) -> IndexedRates:
    energy = engie.parse_snapshot("engie_flow", REGION_WALLONIA, text).energy
    assert isinstance(energy, IndexedRates)
    return energy


def test_a_formula_stated_in_another_unit_is_refused() -> None:
    """Since October 2025 the cards say the formula gives c€/kWh. Read on
    that scale, a formula restated in another unit would come out ten times
    off."""
    text = _card(_FLOW_2609)
    assert text.count(_RESULT_SENTENCE) == 1
    assert _flow_energy(text).formula == _FLOW_FORMULA
    restated = text.replace(_RESULT_SENTENCE, _RESULT_SENTENCE.replace("c€/kWh", "€/MWh"))
    with pytest.raises(ExtractorError, match="states its formula in €/MWh, expected c€/kWh"):
        _flow_energy(restated)


def test_a_result_sentence_a_line_lower_is_read() -> None:
    """A line put between the formula and its sentence leaves the sentence
    within the reach it is looked for in, and the card reads."""
    text = _card(_FLOW_2609)
    line = "\nLe prix du gaz de votre Contrat est indexé mensuellement. Le"
    assert text.count(line) == 1
    moved = text.replace(_RESULT_SENTENCE, line + _RESULT_SENTENCE)
    assert _flow_energy(moved).formula == _FLOW_FORMULA


@pytest.mark.parametrize("where", ["dropped", "past its reach"])
def test_a_card_since_october_2025_without_its_sentence_is_refused(where: str) -> None:
    """Every card for October 2025 on prints the result sentence, the only
    statement of the formula's own unit. A card for those months whose
    sentence is gone, or no longer under the formula, is refused rather than
    read as the older cards are."""
    text = _card(_FLOW_2609)
    assert text.count(_RESULT_SENTENCE) == 1
    restated = text.replace(_RESULT_SENTENCE, "")
    if where == "past its reach":
        restated += _RESULT_SENTENCE
    with pytest.raises(ExtractorError, match="states its formula in no unit, expected c€/kWh"):
        _flow_energy(restated)


def test_a_formula_and_price_restated_without_the_sentence_are_refused() -> None:
    """The formula and its price restated in €/MWh, the sentence dropped: the
    formula still gives the price back at the value the card names, so only
    the card's month, September 2026, tells the sentence is missing."""
    text = _card(_FLOW_2609)
    for printed, restated in (
        ("Prélèvement: 0,9335 + (0,1020 x", "Prélèvement: 9,335 + (1,020 x"),
        ("Prix mensuels\n7,643\n", "Prix mensuels\n76,43\n"),
        (_RESULT_SENTENCE, ""),
    ):
        assert text.count(printed) == 1
        text = text.replace(printed, restated)
    with pytest.raises(ExtractorError, match="states its formula in no unit, expected c€/kWh"):
        _flow_energy(text)


_FLOW_2509 = "G_FLOW_R_GREY_C_I_24_W_F_202509.pdf"


@pytest.mark.parametrize(
    ("printed", "restated", "error"),
    [
        ("Prélèvement: 1,0620 +", "Prélèvement: 10,620 +", r"the card prints 4\.521"),
        ("(0,1000 x ZTPDAM", "(1,0000 x ZTPDAM", r"the card prints 4\.521"),
        (
            "connue (Août 2025: 32,0330\n€/MWh)",
            "connue (Août 2025: 3,20330\nc€/kWh)",
            "no ZTPDAM value",
        ),
    ],
)
def test_a_card_stating_no_unit_is_held_to_its_printed_price(
    printed: str, restated: str, error: str
) -> None:
    """The cards of April 2024 to September 2025 have no such sentence, and
    the September 2025 card reads without it. Its formula has to give the
    printed 4,521 back at the ZTPDAM value the card names: a factor or base
    ten times off does not, and a card naming no value in €/MWh cannot be
    checked."""
    text = _card(_FLOW_2509)
    assert "Le résultat" not in text
    assert text.count(printed) == 1
    energy = _flow_energy(text)
    assert energy.formula == "Prélèvement: 1,0620 + (0,1000 x ZTPDAM (Heren)"
    assert energy.at(32.033) == pytest.approx(0.04521, abs=5e-6)
    with pytest.raises(ExtractorError, match=error):
        _flow_energy(text.replace(printed, restated))


@pytest.mark.parametrize(
    ("restatements", "error"),
    [
        pytest.param(
            [_FLOW_FACTOR_TEN_TIMES], r"the card prints 7\.643", id="value still in €/MWh"
        ),
        pytest.param(
            [_FLOW_FACTOR_TEN_TIMES, _FLOW_INDEX_IN_CENTS], "no ZTPDAM value", id="value in c€/kWh"
        ),
    ],
)
def test_a_card_restating_only_the_index_is_refused(
    restatements: list[tuple[str, str]], error: str
) -> None:
    """The result sentence says nothing of the index. A card that restated
    the index alone in c€/kWh, its factor ten times larger, would still say
    c€/kWh there; held to its printed 7,643 at the ZTPDAM value it names in
    €/MWh, it misses it, and naming the value in c€/kWh instead leaves
    nothing to check the formula on."""
    text = _card(_FLOW_2609)
    for printed, restated in restatements:
        assert text.count(printed) == 1
        text = text.replace(printed, restated)
    assert _RESULT_SENTENCE in text
    with pytest.raises(ExtractorError, match=error):
        _flow_energy(text)


@pytest.mark.parametrize(("printed", "kept"), [("7,646", True), ("7,647", False)])
def test_the_printed_price_is_held_within_five_hundredths_of_a_percent(
    printed: str, kept: bool
) -> None:
    """The formula gives 7,6429 at 61,537: a price printed 7,646 is within
    0,05% of it, 7,647 is not. Every archived card is within 0,012%."""
    text = _card(_FLOW_2609)
    assert text.count("Prix mensuels\n7,643\n") == 1
    text = text.replace("Prix mensuels\n7,643\n", f"Prix mensuels\n{printed}\n")
    if kept:
        assert _flow_energy(text).price == pytest.approx(float(printed.replace(",", ".")) / 100)
    else:
        with pytest.raises(
            ExtractorError, match=re.escape(f"the card prints {printed.replace(',', '.')}")
        ):
            _flow_energy(text)


def test_an_index_value_printed_with_a_dot_is_read() -> None:
    """The cards print the index value with a decimal comma. One printed
    with a dot, as other Belgian cards print some figures, is the same
    value and the formula is still checked on it."""
    text = _card(_FLOW_2609)
    assert text.count("2026: 61,5370\n€/MWh)") == 1
    dotted = text.replace("2026: 61,5370\n€/MWh)", "2026: 61.5370\n€/MWh)")
    assert _flow_energy(dotted).formula == _FLOW_FORMULA
    with pytest.raises(ExtractorError, match=r"the card prints 7\.643"):
        _flow_energy(dotted.replace(*_FLOW_FACTOR_TEN_TIMES))


def test_index_page_reads_both_indices_by_delivery_month() -> None:
    table = engie.parse_index_page(fixture_page("engie", "parametres-indexation-gaz.html"))
    # The row the September card priced on, and the value the FLOW card names.
    assert table["ZTP101"]["2026-09"] == pytest.approx(61.768)
    assert table["ZTP101"]["2026-08"] == pytest.approx(53.605)
    assert table["ZTPDAM"]["2026-08"] == pytest.approx(61.537)
    # September's ZTPDAM is not known until the month is over.
    assert "2026-09" not in table["ZTPDAM"]


def test_regions_follow_the_codes_engie_publishes() -> None:
    by_id = {c.id: c for c in engie.EXTRACTOR.contracts}
    assert by_id["engie_basic_online"].regions == frozenset({REGION_FLANDERS, REGION_WALLONIA})
    assert by_id["engie_easy_variable"].regions == frozenset(
        {REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS}
    )


async def test_fetch_links_the_card_it_read() -> None:
    """The endpoint answers 400 without a document, so the snapshot names
    the card's own address."""
    with patch.object(
        engie,
        "fetch_pdf_text",
        AsyncMock(return_value=_card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf")),
    ) as fetched:
        snap = await engie.fetch(AsyncMock(), "engie_easy_variable", REGION_WALLONIA)
    url = fetched.call_args.args[1]
    assert "document=G_EASY_R_GREY_C_I_12_W_F" in url
    assert snap.source_url == url


async def test_fetch_for_month_counts_the_offset_in_brussels_time() -> None:
    """Just after midnight on 1 September in Brussels a UTC host is still on
    31 August. The offset for August is 1, not 0, or the archive would return
    the September card for August."""
    session = AsyncMock()
    text = _card("G_EASY_R_GREY_C_I_12_W_F_202608.pdf")
    with (
        freeze_time(datetime(2026, 8, 31, 22, 30)),
        patch.object(engie, "fetch_pdf_text", AsyncMock(return_value=text)) as fetched,
    ):
        snap = await engie.fetch_for_month(
            session, "engie_easy_variable", REGION_WALLONIA, date(2026, 8, 1)
        )
    assert snap is not None
    assert snap.valid_until == date(2026, 8, 31)
    assert "monthOffset=1" in fetched.call_args.args[1]
    assert "G_EASY_R_GREY_C_I_12_W_F" in fetched.call_args.args[1]


async def test_fetch_for_month_refuses_a_card_for_another_month() -> None:
    session = AsyncMock()
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            engie,
            "fetch_pdf_text",
            AsyncMock(return_value=_card("G_EASY_R_GREY_C_I_12_W_F_202609.pdf")),
        ),
    ):
        assert (
            await engie.fetch_for_month(
                session, "engie_easy_variable", REGION_WALLONIA, date(2026, 8, 1)
            )
            is None
        )


async def test_fetch_for_month_raises_on_a_transient_failure() -> None:
    session = AsyncMock()
    with (
        freeze_time(datetime(2026, 9, 15, 12)),
        patch.object(
            engie,
            "fetch_pdf_text",
            AsyncMock(side_effect=ExtractorError("HTTP 503 fetching x")),
        ),
        pytest.raises(ExtractorError),
    ):
        await engie.fetch_for_month(session, "engie_flow", REGION_WALLONIA, date(2026, 8, 1))


async def test_fetch_for_month_does_not_ask_for_the_future() -> None:
    with freeze_time(datetime(2026, 9, 15, 12)):
        assert (
            await engie.fetch_for_month(
                AsyncMock(), "engie_flow", REGION_WALLONIA, date(2026, 10, 1)
            )
            is None
        )


async def test_empty_house_cards_are_asked_under_the_code_of_their_month() -> None:
    """Empty House became a three-year product in November 2026 and its code
    moved from 00 to 36. The new code serves November's card and nothing
    before it, the old one every month up to October, so each card is asked
    under the code of its own month. Asked under 00, the current card
    answered 404 from 1 November."""
    october = _card("G_EMPTYHOUSE_R_GREY_C_I_00_W_F_202610.pdf")
    november = _card("G_EMPTYHOUSE_R_GREY_C_I_36_W_F_202611.pdf")
    served = {
        "G_EMPTYHOUSE_R_GREY_C_I_36_W_F&monthOffset=0": november,
        "G_EMPTYHOUSE_R_GREY_C_I_00_W_F&monthOffset=1": october,
    }
    asked: list[str] = []

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        key = url.split("document=")[1].split("&segment=")[0]
        asked.append(key)
        if key in served:
            return served[key]
        raise ExtractorError(f"HTTP 404 fetching {url}")

    with (
        freeze_time(datetime(2026, 11, 3, 11)),
        patch.object(engie, "fetch_pdf_text", _fake_pdf),
    ):
        current = await engie.fetch(AsyncMock(), "engie_empty_house", REGION_WALLONIA)
        closed = await engie.fetch_for_month(
            AsyncMock(), "engie_empty_house", REGION_WALLONIA, date(2026, 10, 1)
        )
    assert asked == [
        "G_EMPTYHOUSE_R_GREY_C_I_36_W_F&monthOffset=0",
        "G_EMPTYHOUSE_R_GREY_C_I_00_W_F&monthOffset=1",
    ]
    assert current.publication_label == "2026-11"
    assert closed is not None and closed.publication_label == "2026-10"
    # The formula did not move with the term: 1,7540 + 0,2000 x ZTPDAM.
    for snap in (current, closed):
        energy = snap.energy
        assert isinstance(energy, IndexedRates)
        assert energy.factor == pytest.approx(0.002 * 1.06)
        assert energy.base == pytest.approx(0.01754 * 1.06)

    # The card archive replays a card with the clock on the day it was read,
    # so one read in October is asked where it was read.
    served.clear()
    served["G_EMPTYHOUSE_R_GREY_C_I_00_W_F&monthOffset=0"] = october
    asked.clear()
    with (
        freeze_time(datetime(2026, 10, 29, 11)),
        patch.object(engie, "fetch_pdf_text", _fake_pdf),
    ):
        replayed = await engie.fetch(AsyncMock(), "engie_empty_house", REGION_WALLONIA)
    assert asked == ["G_EMPTYHOUSE_R_GREY_C_I_00_W_F&monthOffset=0"]
    assert replayed.publication_label == "2026-10"

    # A month on, November is a past month and stays under the new code.
    served.clear()
    served["G_EMPTYHOUSE_R_GREY_C_I_36_W_F&monthOffset=1"] = november
    asked.clear()
    with (
        freeze_time(datetime(2026, 12, 2, 11)),
        patch.object(engie, "fetch_pdf_text", _fake_pdf),
    ):
        past = await engie.fetch_for_month(
            AsyncMock(), "engie_empty_house", REGION_WALLONIA, date(2026, 11, 1)
        )
    assert asked == ["G_EMPTYHOUSE_R_GREY_C_I_36_W_F&monthOffset=1"]
    assert past is not None and past.publication_label == "2026-11"
