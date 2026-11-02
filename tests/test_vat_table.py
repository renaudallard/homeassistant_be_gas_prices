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

"""The card archive's VAT table: the consensus written from the cards
(``scripts/archive_cards.py``), the integration reading it
(``vat_rates.py``), and the months billed on it."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.be_gas_prices import archive_table, vat_rates
from custom_components.be_gas_prices.bill import MonthBill, bill_month, card_leg, contract_leg
from custom_components.be_gas_prices.const import (
    CALIBER_Q10,
    CONF_MANUAL_BASE,
    CONF_MANUAL_FACTOR,
    CONF_MANUAL_PRICE,
    DSO_FLUVIUS_ANTWERPEN,
    REGION_FLANDERS,
    REGION_WALLONIA,
    VAT_RATE_REDUCED,
)
from custom_components.be_gas_prices.manual_rate import manual_leg
from custom_components.be_gas_prices.providers import bolt, energiebe, octaplus
from custom_components.be_gas_prices.providers import energy_together as et
from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._rates import FixedRates, IndexedRates, on_vat
from custom_components.be_gas_prices.providers._resolve import resolve_federal_levies
from custom_components.be_gas_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)
from custom_components.be_gas_prices.snapshot_codec import (
    SnapshotDecodeError,
    snapshot_from_json,
    snapshot_to_json,
)
from custom_components.be_gas_prices.vat_rates import ensure_vat_rates
from tests import fixture_text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import archive_cards as ac  # type: ignore[import-not-found]

NOV = date(2026, 11, 1)
# What 21 % puts on a figure a parser grossed by 6 %.
RATIO = 1.21 / 1.06


# The consensus.


def test_three_suppliers_agreeing_make_the_months_rate() -> None:
    stated = {"eneco": {0.06}, "engie": {0.06}, "luminus": {0.06}}
    assert ac._vat_consensus(stated) == {
        "rate": 0.06,
        "suppliers": ["eneco", "engie", "luminus"],
    }


def test_a_disagreement_is_no_rate() -> None:
    stated = {"eneco": {0.06}, "engie": {0.06}, "luminus": {0.06}, "mega": {0.21}}
    assert ac._vat_consensus(stated) == {
        "disputed": {"0.06": ["eneco", "engie", "luminus"], "0.21": ["mega"]}
    }
    # One supplier stating two rates in a month disagrees with itself.
    two = {"eneco": {0.06, 0.21}, "engie": {0.06}, "luminus": {0.06}}
    assert "disputed" in ac._vat_consensus(two)


def test_two_suppliers_are_too_few() -> None:
    stated = {"eneco": {0.06}, "engie": {0.06}}
    assert ac._vat_consensus(stated) == {"too_few": {"0.06": ["eneco", "engie"]}}


def _row(out: Path, supplier: str, contract: str, month: str, rate: float | None) -> None:
    folder = out / "cards" / supplier / contract / "flanders"
    folder.mkdir(parents=True, exist_ok=True)
    taxes: dict[str, Any] = {"vat_rate": 0.0, "connection_fee": 0.0}
    if rate is not None:
        taxes["card_vat_rate"] = rate
    (folder / f"{month}.json").write_text(json.dumps({"taxes": taxes}))


def test_the_table_is_written_from_the_rows_by_card_month(tmp_path: Path) -> None:
    for supplier in ("eneco", "luminus", "ebem"):
        _row(tmp_path, supplier, "x", "2026-09", 0.06)
    # A card that states no rate adds nothing.
    _row(tmp_path, "energiebe", "energiebe_variable", "2026-09", None)
    for supplier in ("eneco", "luminus"):
        _row(tmp_path, supplier, "x", "2026-10", 0.06)
    ac._write_vat(tmp_path)
    table = json.loads((tmp_path / "vat.json").read_text())
    assert table == {
        "residential": {
            "2026-09": {"rate": 0.06, "suppliers": ["ebem", "eneco", "luminus"]},
            "2026-10": {"too_few": {"0.06": ["eneco", "luminus"]}},
        }
    }
    before = (tmp_path / "vat.json").read_bytes()
    ac._write_vat(tmp_path)
    assert (tmp_path / "vat.json").read_bytes() == before


# The reader.


def test_only_agreed_months_within_bounds_are_held() -> None:
    vat_rates.hold(
        {
            "residential": {
                "2026-10": {"rate": 0.06},
                "2026-11": {"disputed": {"0.06": ["a"], "0.21": ["b"]}},
                "2026-12": {"rate": 6},
                "2027-13": {"rate": 0.21},
                "not a month": {"rate": 0.21},
            }
        }
    )
    assert vat_rates.held_table() == {"residential": {"2026-10": {"rate": 0.06}}}
    # A disputed month keeps the last month that agreed.
    assert vat_rates.residential_vat(NOV) == 0.06


def test_a_month_before_the_table_is_billed_on_the_law_s_rate() -> None:
    assert vat_rates.residential_vat(NOV) == VAT_RATE_REDUCED
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.21}}})
    assert vat_rates.residential_vat(date(2026, 10, 1)) == VAT_RATE_REDUCED
    assert vat_rates.residential_vat(NOV) == 0.21
    # Never applied backwards, and carried forwards.
    assert vat_rates.residential_vat(date(2027, 3, 1)) == 0.21


def test_an_empty_table_does_not_wipe_a_held_one() -> None:
    vat_rates.hold({"residential": {"2026-10": {"rate": 0.06}}})
    vat_rates.hold({"residential": {"2026-11": {"disputed": {}}}})
    vat_rates.hold({"other": {}})
    assert vat_rates.residential_vat(NOV) == 0.06


def test_the_store_round_trips_and_never_replaces_a_read() -> None:
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.21}}})
    stored = vat_rates.held_table()
    vat_rates._RESIDENTIAL.clear()
    vat_rates.restore(stored)
    assert vat_rates.held_table() == stored
    vat_rates.restore({"residential": {"2026-11": {"rate": 0.06}}})
    assert vat_rates.held_table() == stored
    vat_rates._RESIDENTIAL.clear()
    unreadable: tuple[Any, ...] = (None, [], {"residential": "x"})
    for broken in unreadable:
        vat_rates.restore(broken)
        assert vat_rates.held_table() == {"residential": {}}, broken


async def test_the_table_is_read_once_a_day_and_a_failure_never_raises() -> None:
    table = json.dumps({"residential": {"2026-11": {"rate": 0.21}}})
    fetch = AsyncMock(return_value=table)
    with patch.object(archive_table, "fetch_text", fetch):
        await ensure_vat_rates(None)  # type: ignore[arg-type]
        await ensure_vat_rates(None)  # type: ignore[arg-type]
    assert fetch.await_count == 1
    assert vat_rates.residential_vat(NOV) == 0.21
    vat_rates._TABLE.fetched_at = dt_util.utcnow() - timedelta(days=2)
    for failure in (ExtractorError("timeout"), "not json", "[1, 2]"):
        vat_rates._TABLE.failed_at = None
        broken: AsyncMock | None = (
            AsyncMock(side_effect=failure) if isinstance(failure, Exception) else None
        )
        with patch.object(archive_table, "fetch_text", broken or AsyncMock(return_value=failure)):
            await ensure_vat_rates(None)  # type: ignore[arg-type]
        assert vat_rates._TABLE.failed_at is not None
        # What was read goes on billing.
        assert vat_rates.residential_vat(NOV) == 0.21


# Billed on it.


def _stating_none() -> TaxOverlay:
    """A card that states no VAT rate, as energie.be's and Energy Together's."""
    return TaxOverlay(excise_bands=excise_bands(0.0109, 0.0118), energy_contribution=0.0)


def test_the_law_s_figures_go_on_the_month_s_rate_where_a_card_states_none() -> None:
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.21}}})
    october = resolve_federal_levies(_stating_none(), date(2026, 10, 1))
    november = resolve_federal_levies(_stating_none(), NOV)
    assert october.excise_bands[0][1] == pytest.approx(0.01031 * 1.06)
    assert november.excise_bands[0][1] == pytest.approx(0.01031 * 1.21)
    # A card that states its rate keeps it, against the month's.
    stated = resolve_federal_levies(TaxOverlay(excise_bands=(), card_vat_rate=0.06), NOV)
    assert stated.excise_bands[0][1] == pytest.approx(0.01031 * 1.06)


def _assumed() -> IndexedRates:
    """A formula a parser grossed by 6 % because the card states no rate."""
    return IndexedRates(
        factor=0.1 * 1.06,
        base=0.002 * 1.06,
        index="TTF_RLP",
        price=0.008 * 1.06,
        vat_grossed=("factor", "base", "price"),
    )


def test_a_formula_grossed_on_an_assumed_rate_moves_to_the_month_s() -> None:
    moved = on_vat(_assumed(), 0.21)
    assert isinstance(moved, IndexedRates)
    assert moved.factor == pytest.approx(0.1 * 1.21)
    assert moved.base == pytest.approx(0.002 * 1.21)
    assert moved.price == pytest.approx(0.008 * 1.21)
    assert moved.vat_grossed == ()
    # At the rate assumed, and on a leg that names nothing, it stands.
    assert on_vat(_assumed(), VAT_RATE_REDUCED) == _assumed()
    printed = IndexedRates(factor=0.1, base=0.002, index="ZTPDAM", price=0.008)
    assert on_vat(printed, 0.21) is printed


def test_the_contract_leg_is_on_the_month_s_rate() -> None:
    """The card of the month and the signing card's formula moved, and the
    typed figures grossed by the month's rate, before the signed formula is
    priced at the index the card of the month prices its own figure at."""
    own = _assumed()
    signed = IndexedRates(
        factor=0.09 * 1.06,
        base=0.001 * 1.06,
        index="TTF_RLP",
        price=0.0,
        vat_grossed=("factor", "base"),
    )
    leg = contract_leg(own, signed, {}, 0.21)
    assert isinstance(leg, IndexedRates)
    assert leg.factor == pytest.approx(0.09 * 1.21)
    # The index the card of the month is priced at, read on one basis.
    index = (0.008 - 0.002) / 0.1
    assert leg.price == pytest.approx((0.09 * index + 0.001) * 1.21)
    # The card of the month's own leg, nothing laid over it.
    alone = contract_leg(own, None, {}, 0.21)
    assert isinstance(alone, IndexedRates)
    assert alone.factor == pytest.approx(0.1 * 1.21)
    assert alone.price == pytest.approx(0.008 * 1.21)
    typed = contract_leg(own, None, {CONF_MANUAL_FACTOR: 0.1, CONF_MANUAL_BASE: 0.2}, 0.21)
    assert isinstance(typed, IndexedRates)
    assert typed.factor == pytest.approx(0.001 * 1.21)
    assert typed.base == pytest.approx(0.002 * 1.21)


def test_a_month_is_billed_on_its_own_rate() -> None:
    """Energy Together's card states no rate and its parser grosses the
    formula and its price by 6 %: a month the table puts at 21 % bills the
    energy and the law's figures at 21 %, the month before it at 6 %."""
    card = et.parse_snapshot(
        "evident_flexi",
        REGION_FLANDERS,
        fixture_text("evident", "Tariefkaart_Flexi_Gas_2026-08.pdf"),
        "url",
    )
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.21}}})

    def bill(month: str) -> MonthBill:
        return bill_month(
            month=month,
            card=card,
            energy_for=card_leg,
            table=None,
            dso=DSO_FLUVIUS_ANTWERPEN,
            annual_kwh=10_000.0,
            caliber=CALIBER_Q10,
            kwh=1000.0,
            days=30,
            days_in_year=365,
        )

    october, november = bill("2026-10"), bill("2026-11")
    assert october.breakdown.energy == pytest.approx(card.energy.price)
    assert november.breakdown.energy == pytest.approx(card.energy.price * RATIO)
    assert november.breakdown.taxes == pytest.approx(october.breakdown.taxes * RATIO)


def test_typed_figures_are_grossed_by_the_month_s_rate() -> None:
    fixed = manual_leg(FixedRates(price=0.07), {CONF_MANUAL_PRICE: 7.5}, 0.21)
    assert isinstance(fixed, FixedRates)
    assert fixed.price == pytest.approx(0.075 * 1.21)


@pytest.mark.parametrize(
    ("read", "grossed"),
    [
        (
            lambda: energiebe.parse_snapshot(
                "energiebe_variable",
                REGION_FLANDERS,
                fixture_text("energiebe", "Energie_be_Gas_Particulier_18ddc53692.pdf", "layout"),
            ),
            ("factor", "base"),
        ),
        (
            lambda: et.parse_snapshot(
                "evident_flexi",
                REGION_FLANDERS,
                fixture_text("evident", "Tariefkaart_Flexi_Gas_2026-08.pdf"),
                "url",
            ),
            ("factor", "base", "price"),
        ),
        (
            lambda: bolt.parse_snapshot(
                "bolt_variable",
                REGION_FLANDERS,
                fixture_text("bolt", "bolt_res_ng_fr_14.pdf", "layout"),
            ),
            ("factor", "base"),
        ),
        (
            lambda: octaplus.parse_snapshot(
                "octaplus_boostflex",
                REGION_WALLONIA,
                fixture_text("octaplus", "G_OCTA_BOOSTFLEX_RE_WL_FR.pdf", "layout"),
            ),
            ("factor", "base"),
        ),
        (
            lambda: octaplus.parse_older(
                "octaplus_flux",
                REGION_FLANDERS,
                fixture_text("octaplus", "2025-09 G OCTA+FLUX RE VL FR.pdf", "rows"),
            ),
            ("factor", "base"),
        ),
    ],
    ids=["energiebe", "energy_together", "bolt", "octaplus", "octaplus_older"],
)
def test_a_parser_names_the_fields_it_grosses(
    read: Callable[[], SupplierSnapshot], grossed: tuple[str, ...]
) -> None:
    """The cards that state no rate, whose formula their parser grosses by
    the residential rate."""
    card = read()
    assert card.taxes.card_vat_rate is None
    assert isinstance(card.energy, IndexedRates)
    assert card.energy.vat_grossed == grossed


def test_the_store_keeps_what_a_parser_grossed() -> None:
    card = energiebe.parse_snapshot(
        "energiebe_variable",
        REGION_FLANDERS,
        fixture_text("energiebe", "Energie_be_Gas_Particulier_18ddc53692.pdf", "layout"),
    )
    blob = snapshot_to_json(card)
    assert blob["energy"]["vat_grossed"] == ["factor", "base"]
    assert snapshot_from_json(blob) == card
    # A card stored before the field was added names none.
    older = {**blob, "schema": 1, "energy": {**blob["energy"]}}
    del older["energy"]["vat_grossed"]
    read = snapshot_from_json(older)
    assert isinstance(read.energy, IndexedRates) and read.energy.vat_grossed == ()
    with pytest.raises(SnapshotDecodeError):
        snapshot_from_json({**blob, "energy": {**blob["energy"], "vat_grossed": ["fee"]}})
