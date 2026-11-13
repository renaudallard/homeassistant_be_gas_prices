"""network_sheet.py and its resolver: a custom entry's network off its DSO's
published tariff sheet."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices import brugel, cwape, fluvius, network_sheet
from custom_components.be_gas_prices.const import (
    CONF_CUSTOM_FEE,
    CONF_CUSTOM_METERING,
    CONF_CUSTOM_NETWORK_FROM_SHEET,
    CONF_CUSTOM_PRICE,
    CONF_CUSTOM_T1_FIXED,
    CONF_CUSTOM_T1_PROP,
    CONF_CUSTOM_T2_FIXED,
    CONF_CUSTOM_T2_PROP,
    CONF_CUSTOM_TRANSPORT,
    CONF_DSO,
    CONF_SUPPLIER,
    DSO_ORES,
    DSO_SIBELGA,
    SUPPLIER_CUSTOM,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.providers._resolve import resolve_for_delivery
from custom_components.be_gas_prices.providers.base import SheetNetwork
from custom_components.be_gas_prices.providers.custom import build_snapshot
from custom_components.be_gas_prices.snapshot_codec import snapshot_from_json, snapshot_to_json

_FIXTURES = Path(__file__).parent / "fixtures"
_NOV = date(2026, 11, 1)
# The dispatcher itself, which conftest stands in for in every other test.
_ENSURE = network_sheet.ensure_network_sheet


def _typed(dso: str, **extra: Any) -> dict[str, Any]:
    """A custom entry's typed card, VAT inclusive, its network typed by hand."""
    return {
        CONF_SUPPLIER: SUPPLIER_CUSTOM,
        CONF_DSO: dso,
        CONF_CUSTOM_PRICE: 8.0,
        CONF_CUSTOM_FEE: 60.0,
        CONF_CUSTOM_T1_FIXED: 10.0,
        CONF_CUSTOM_T1_PROP: 1.0,
        CONF_CUSTOM_T2_FIXED: 50.0,
        CONF_CUSTOM_T2_PROP: 0.5,
        CONF_CUSTOM_TRANSPORT: 0.165,
        CONF_CUSTOM_METERING: 12.0,
        **extra,
    }


def _hold_antwerpen() -> Any:
    terms = fluvius.parse_area_network(
        (_FIXTURES / "fluvius" / "gas_antwerpen_2026.xlsx").read_bytes(), 2026
    )
    assert terms is not None
    fluvius._area[(2026, "fluvius_antwerpen")] = terms
    return terms


def test_a_ticked_entry_bills_its_areas_sheet_with_vat() -> None:
    """The T1 and T2 terms and the metering off the sheet held for the
    delivery year, put on the month's VAT as the typed card is; the transport,
    which no sheet prints, stays as typed."""
    terms = _hold_antwerpen()
    card = build_snapshot(_typed("fluvius_antwerpen", **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}))
    overlay = resolve_for_delivery(card, _NOV).dsos["fluvius_antwerpen"]
    assert overlay.tiers[TIER_T1].fixed_per_year == pytest.approx(terms.t1_fixed * 1.06)
    assert overlay.tiers[TIER_T1].proportional == pytest.approx(terms.t1_proportional * 1.06)
    assert overlay.tiers[TIER_T2].fixed_per_year == pytest.approx(terms.t2_fixed * 1.06)
    assert overlay.tiers[TIER_T2].proportional == pytest.approx(terms.t2_proportional * 1.06)
    assert overlay.metering_per_year == pytest.approx(17.85 * 1.06)
    assert overlay.transport == pytest.approx(0.00165)


def test_the_typed_network_stands_unticked_or_with_no_sheet_held() -> None:
    """Unticked, the figures typed are billed whatever sheet is held; ticked
    with none held, they stand in, so the entry still prices."""
    _hold_antwerpen()
    typed = resolve_for_delivery(build_snapshot(_typed("fluvius_antwerpen")), _NOV)
    assert typed.dsos["fluvius_antwerpen"].tiers[TIER_T1].proportional == pytest.approx(0.01)
    fluvius._area.clear()
    ticked = build_snapshot(_typed("fluvius_antwerpen", **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}))
    overlay = resolve_for_delivery(ticked, _NOV).dsos["fluvius_antwerpen"]
    assert overlay.tiers[TIER_T1].fixed_per_year == pytest.approx(10.0)
    assert overlay.metering_per_year == pytest.approx(12.0)


def test_each_region_bills_its_own_regulators_sheet() -> None:
    """Brugel's in Brussels, the CWaPE's grid valid on the month in Wallonia,
    whose metering term is nothing."""
    from custom_components.be_gas_prices.providers._pdf_render import extract_pdf_text

    sibelga = brugel.parse_network(
        extract_pdf_text((_FIXTURES / "brugel" / "gaz_2026.pdf").read_bytes())
    )
    grid = cwape.parse_grid((_FIXTURES / "cwape" / "ores_gaz_2026.xlsx").read_bytes())
    assert sibelga is not None and grid is not None
    brugel._held[2026] = sibelga
    cwape._grids[DSO_ORES] = {"grid": grid}
    for dso, terms, metering in ((DSO_SIBELGA, sibelga, 23.54), (DSO_ORES, grid.network, 0.0)):
        card = build_snapshot(_typed(dso, **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}))
        overlay = resolve_for_delivery(card, _NOV).dsos[dso]
        assert overlay.tiers[TIER_T1].proportional == pytest.approx(terms.t1_proportional * 1.06)
        assert overlay.metering_per_year == pytest.approx(metering * 1.06)


async def test_the_dispatcher_reads_the_sheet_of_the_entry_s_region() -> None:
    """Fluvius's area sheet, Brugel's or the CWaPE's grids, each for its own
    DSOs, and nothing for an entry that types its network."""
    with (
        patch.object(fluvius, "ensure_area_network", AsyncMock()) as area,
        patch.object(brugel, "ensure_network", AsyncMock()) as sheet,
        patch.object(cwape, "ensure_network", AsyncMock()) as grids,
    ):
        for dso in ("fluvius_antwerpen", DSO_SIBELGA, DSO_ORES):
            await _ENSURE(None, _typed(dso, **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}), 2026)  # type: ignore[arg-type]
        await _ENSURE(None, _typed(DSO_ORES), 2026)  # type: ignore[arg-type]
    area.assert_awaited_once_with(None, 2026, "fluvius_antwerpen")
    sheet.assert_awaited_once_with(None, 2026)
    grids.assert_awaited_once_with(None, 2026, DSO_ORES)


def test_the_box_travels_with_the_card_and_is_left_out_unticked() -> None:
    ticked = build_snapshot(_typed(DSO_ORES, **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}))
    assert snapshot_to_json(ticked)["network_from_sheet"] is True
    assert snapshot_from_json(snapshot_to_json(ticked)).network_from_sheet is True
    typed = build_snapshot(_typed(DSO_ORES))
    assert "network_from_sheet" not in snapshot_to_json(typed)
    assert snapshot_from_json(snapshot_to_json(typed)).network_from_sheet is False


async def test_an_earlier_custom_contract_reads_its_own_dso_s_sheet() -> None:
    """A custom contract recorded at a switch is billed on its own typed card,
    so the fill reads the sheet of its own DSO for the year, ticked or not as
    it was; a supplier's contract asks for none."""
    from custom_components.be_gas_prices import contract_periods
    from custom_components.be_gas_prices.contract_periods import PeriodBilling, record_switch
    from custom_components.be_gas_prices.month_cards import MonthCardCache

    custom = {
        **_typed(DSO_SIBELGA, **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}),
        "contract": "custom_fixed",
        "region": "brussels",
    }
    data = record_switch(custom, date(2026, 6, 1))
    reads: Any = contract_periods.ensure_network_sheet
    await PeriodBilling(MonthCardCache()).fill(None, data, date(2026, 9, 1), use_archive=False)  # type: ignore[arg-type]
    [(_session, period, year)] = [call.args for call in reads.await_args_list]
    assert period[CONF_DSO] == DSO_SIBELGA and period[CONF_CUSTOM_NETWORK_FROM_SHEET] is True
    assert year == 2026


# The card archive's network table (network_archive.py).

_ORES = {
    "t1_fixed": 30.1,
    "t1_proportional": 0.0404664,
    "t2_fixed": 132.95,
    "t2_proportional": 0.0208089,
    "metering_per_year": 0.0,
}


def test_a_month_is_read_by_dso_and_kept_until_a_table_replaces_it() -> None:
    from custom_components.be_gas_prices import network_archive

    network_archive.hold({"months": {"2026-11": {DSO_ORES: _ORES}}})
    held = network_archive.network_for(_NOV, DSO_ORES)
    assert held is not None and held.t1_proportional == 0.0404664
    assert network_archive.network_for(date(2026, 10, 1), DSO_ORES) is None
    network_archive.hold({"months": {}})
    assert network_archive.network_for(_NOV, DSO_ORES) == held
    network_archive.restore({"months": {"2026-12": {DSO_ORES: _ORES}}})
    assert network_archive.network_for(date(2026, 12, 1), DSO_ORES) is None
    assert network_archive.held_table() == {"months": {"2026-11": {DSO_ORES: _ORES}}}


def test_every_month_of_the_archives_network_table_is_held() -> None:
    """network.json as the card archive published it on 6 November 2026,
    January 2025 to November 2026 for eleven DSOs, figure for figure."""
    from custom_components.be_gas_prices import network_archive

    table = json.loads((_FIXTURES / "card_archive" / "network.json").read_text())
    network_archive.hold(table)
    assert network_archive.held_table() == table
    held = network_archive.held_table()["months"]
    assert (min(held), max(held), len(held)) == ("2025-01", "2026-11", 23)


@freeze_time("2026-11-06 12:00:00+01:00")
def test_only_a_network_month_from_2000_to_next_year_is_held() -> None:
    """A table is written to every entry's Store on each tick, and months
    made up by a tampered archive would make it as large as they like."""
    from custom_components.be_gas_prices import network_archive

    made_up = {f"{year}-01": {DSO_ORES: _ORES} for year in range(2028, 22028)}
    real = {"2026-11": {DSO_ORES: _ORES}, "2027-12": {DSO_ORES: _ORES}}
    network_archive.hold({"months": {**made_up, "1999-12": {DSO_ORES: _ORES}, **real}})
    assert network_archive.held_table() == {"months": real}


@pytest.mark.parametrize(
    "spoilt",
    [
        {"t1_proportional": 4.04664},
        {"t1_fixed": 3010.0},
        {"metering_per_year": -1.0},
        {"t2_fixed": "132.95"},
        {"t2_fixed": True},
        {"transport": 0.00165},
    ],
)
def test_a_dso_month_that_does_not_read_whole_is_left_out(spoilt: dict[str, Any]) -> None:
    """A figure out of bounds or of another type, or one no sheet gives,
    drops the DSO's month whole, and so does a missing one; the others stay."""
    from custom_components.be_gas_prices import network_archive

    short = {k: v for k, v in _ORES.items() if k != "t2_fixed"}
    network_archive.hold(
        {"months": {"2026-11": {DSO_ORES: {**_ORES, **spoilt}, "resa": _ORES, "sibelga": short}}}
    )
    assert network_archive.network_for(_NOV, DSO_ORES) is None
    assert network_archive.network_for(_NOV, "sibelga") is None
    assert network_archive.network_for(_NOV, "resa") is not None


def test_the_archive_stands_in_for_a_month_no_sheet_held_covers() -> None:
    """A month the entry's own reading of the sheets does not give is billed
    on what the card archive kept of it, ahead of the figures typed; a sheet
    held is billed first."""
    from custom_components.be_gas_prices import network_archive

    network_archive.hold({"months": {"2026-11": {DSO_ORES: _ORES}}})
    card = build_snapshot(_typed(DSO_ORES, **{CONF_CUSTOM_NETWORK_FROM_SHEET: True}))
    overlay = resolve_for_delivery(card, _NOV).dsos[DSO_ORES]
    assert overlay.tiers[TIER_T1].proportional == pytest.approx(0.0404664 * 1.06)
    assert overlay.metering_per_year == 0.0
    grid = cwape.parse_grid((_FIXTURES / "cwape" / "ores_gaz_2026.xlsx").read_bytes())
    assert grid is not None
    cwape._grids[DSO_ORES] = {
        "grid": replace(grid, network=replace(grid.network, t1_proportional=0.05))
    }
    overlay = resolve_for_delivery(card, _NOV).dsos[DSO_ORES]
    assert overlay.tiers[TIER_T1].proportional == pytest.approx(0.05 * 1.06)


def test_an_earlier_year_s_sheet_comes_after_the_archive_s_month() -> None:
    """The read of the year's sheet failed here and left the year before's
    held, while the card archive's run read the year's: the month is billed
    on the archive's row, the sheet of its own year, and the earlier sheet
    only stands in where the archive has none either."""
    from custom_components.be_gas_prices import network_archive

    january = date(2027, 1, 1)
    earlier = _hold_antwerpen()
    assert network_sheet.held_network(january, "fluvius_antwerpen") == earlier
    row = {
        "t1_fixed": 15.0,
        "t1_proportional": 0.022,
        "t2_fixed": 80.0,
        "t2_proportional": 0.009,
        "metering_per_year": 18.0,
    }
    network_archive.hold({"months": {"2027-01": {"fluvius_antwerpen": row}}})
    assert network_sheet.held_network(january, "fluvius_antwerpen") == SheetNetwork(**row)
    # Its own year's sheet held, 2026 is billed on it whatever the archive has.
    network_archive.hold({"months": {"2026-11": {"fluvius_antwerpen": row}}})
    assert network_sheet.held_network(_NOV, "fluvius_antwerpen") == earlier
    # February has no row: the earlier sheet stands in.
    assert network_sheet.held_network(date(2027, 2, 1), "fluvius_antwerpen") == earlier


def test_each_reader_tells_its_own_year_s_sheet_from_a_stand_in() -> None:
    terms = _hold_antwerpen()
    assert fluvius.cached_area_network(2026, "fluvius_antwerpen", stand_in=False) == terms
    assert fluvius.cached_area_network(2027, "fluvius_antwerpen", stand_in=False) is None
    brugel._held[2026] = terms
    assert brugel.cached_network(2026, stand_in=False) == terms
    assert brugel.cached_network(2027, stand_in=False) is None
    assert brugel.cached_network(2027) == terms
    grid = cwape.parse_grid((_FIXTURES / "cwape" / "ores_gaz_2026.xlsx").read_bytes())
    assert grid is not None
    cwape._grids[DSO_ORES] = {"grid": grid}
    assert cwape.cached_network(grid.valid_to, DSO_ORES, stand_in=False) == grid.network
    after = grid.valid_to + timedelta(days=1)
    assert cwape.cached_network(after, DSO_ORES, stand_in=False) is None
    assert cwape.cached_network(after, DSO_ORES) == grid.network
