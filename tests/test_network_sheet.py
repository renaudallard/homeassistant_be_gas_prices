"""network_sheet.py and its resolver: a custom entry's network off its DSO's
published tariff sheet."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

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
