"""A withdrawn contract's network after the year of its last card: the
distribution and metering terms off its DSO's sheet of the delivery year,
as a custom entry ticked for it takes them (network_sheet.py)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.be_gas_prices import (
    brugel,
    contract_periods,
    cwape,
    fluvius,
    network_archive,
    network_sheet,
    providers,
)
from custom_components.be_gas_prices.compare import IndexCache, quote_contract
from custom_components.be_gas_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_DSO,
    CONF_REGION,
    CONF_SUPPLIER,
    CONVERSION_MANUAL,
    DOMAIN,
    DSO_ORES,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    TIER_T1,
    TIER_T2,
)
from custom_components.be_gas_prices.contract_periods import PeriodBilling, record_switch
from custom_components.be_gas_prices.month_cards import MonthCardCache
from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._pdf_render import extract_pdf_text
from custom_components.be_gas_prices.providers._rates import FixedRates
from custom_components.be_gas_prices.providers.base import (
    DsoOverlay,
    DsoTier,
    ExtractorError,
    SheetNetwork,
    SupplierSnapshot,
    TaxOverlay,
)
from custom_components.be_gas_prices.running_costs import Household
from custom_components.be_gas_prices.snapshot_codec import snapshot_to_json
from tests.harness import MockConfigEntry

_FIXTURES = Path(__file__).parent / "fixtures"
_READ_METER = "custom_components.be_gas_prices.coordinator.GasCoordinator._read_meter"
# The dispatcher itself, which conftest stands in for in every test.
_ENSURE = network_sheet.ensure_network_sheet
_VAT = 1.06
# The household's own figures, VAT inclusive as a card prints them: T2 at
# the 17 000 kWh typed, far enough from every sheet to tell them apart.
_T2_FIXED = 40.0
_T2_PROP = 0.003
_TRANSPORT = 0.00165
_METERING = 5.0

# A withdrawn contract of each region, at a DSO a fixture sheet covers.
_CASES = {
    "fluvius": ("eneco", "eneco_aardgas_vast", REGION_FLANDERS, "fluvius_antwerpen"),
    "sibelga": ("mega", "mega_prepaid_fixed", REGION_BRUSSELS, DSO_SIBELGA),
    "ores": ("eneco", "eneco_aardgas_vast", REGION_WALLONIA, DSO_ORES),
}


@pytest.fixture(autouse=True)
def _no_card_archive_rows() -> Iterator[None]:
    with (
        patch(
            "custom_components.be_gas_prices.month_cards.fetch_archived_card",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.be_gas_prices.coordinator_card.fetch_archived_row",
            AsyncMock(return_value=None),
        ),
    ):
        yield


def _last_card(supplier: str, contract: str, dso: str) -> SupplierSnapshot:
    """The card of October 2026, the last month the contract was sold."""
    return SupplierSnapshot(
        supplier=supplier,
        contract=contract,
        energy=FixedRates(price=0.08, yearly_fixed_fee=60.0),
        dsos={
            dso: DsoOverlay(
                tiers={
                    TIER_T1: DsoTier(fixed_per_year=10.0, proportional=0.01),
                    TIER_T2: DsoTier(fixed_per_year=_T2_FIXED, proportional=_T2_PROP),
                },
                transport=_TRANSPORT,
                metering_per_year=_METERING,
            )
        },
        taxes=TaxOverlay(excise_bands=excise_bands(0.0034, None)),
        source_url="https://example.invalid/card.pdf",
        publication_label="2026-10",
        valid_until=date(2026, 10, 31),
    )


def _hold_sheet(dso: str, year: int) -> SheetNetwork:
    """Hold the fixture sheet of ``dso``'s regulator as ``year``'s."""
    if dso == DSO_SIBELGA:
        terms = brugel.parse_network(
            extract_pdf_text((_FIXTURES / "brugel" / "gaz_2026.pdf").read_bytes())
        )
        assert terms is not None
        brugel._held[year] = terms
        return terms
    if dso == DSO_ORES:
        grid = cwape.parse_grid((_FIXTURES / "cwape" / "ores_gaz_2026.xlsx").read_bytes())
        assert grid is not None
        cwape._grids[DSO_ORES] = {
            "grid": replace(grid, valid_from=date(year, 1, 1), valid_to=date(year, 12, 31))
        }
        return grid.network
    area = fluvius.parse_area_network(
        (_FIXTURES / "fluvius" / "gas_antwerpen_2026.xlsx").read_bytes(), 2026
    )
    assert area is not None
    fluvius._area[(year, dso)] = area
    return area


def _stub(supplier: str, card: SupplierSnapshot) -> Any:
    """The supplier as a withdrawn contract meets it: no card of today's,
    and its own archive serving the last month's card alone."""

    async def fetch_for_month(_s: object, _c: str, _r: str, first: date) -> Any:
        return card if first == date(2026, 10, 1) else None

    return replace(
        providers.EXTRACTORS[supplier],
        fetch=AsyncMock(side_effect=ExtractorError("withdrawn")),
        fetch_for_month=AsyncMock(side_effect=fetch_for_month),
        fetch_on_day=None,
        fetch_index=AsyncMock(return_value={}),
        probe=None,
    )


def _stub_for(case: str) -> Any:
    supplier, contract, _region, dso = _CASES[case]
    return _stub(supplier, _last_card(supplier, contract, dso))


def _data(supplier: str, contract: str, region: str, dso: str, **extra: Any) -> dict[str, Any]:
    return {
        CONF_REGION: region,
        CONF_DSO: dso,
        CONF_SUPPLIER: supplier,
        CONF_CONTRACT: contract,
        CONF_ANNUAL_CONSUMPTION_KWH: 17_000.0,
        CONF_CONVERSION_MODE: CONVERSION_MANUAL,
        CONF_CONVERSION_FACTOR: 11.5,
        CONF_CARD_ARCHIVE: False,
        **extra,
    }


def _gas_so_far(today: date) -> dict[date, float]:
    """10 kWh a day from 1 January: short of a measured year, so the tier
    is the 17 000 kWh typed."""
    first = date(today.year, 1, 1)
    return {first + timedelta(days=n): 10.0 for n in range((today - first).days + 1)}


async def _set_up(
    hass: HomeAssistant, case: str, today: str, **extra: Any
) -> tuple[MockConfigEntry, Any]:
    supplier, contract, region, dso = _CASES[case]
    stub = _stub_for(case)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Withdrawn", data=_data(supplier, contract, region, dso, **extra)
    )
    gas = _gas_so_far(datetime.fromisoformat(today).date())
    with (
        patch.dict(providers.EXTRACTORS, {supplier: stub}),
        patch(_READ_METER, AsyncMock(return_value=("energy", gas))),
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry, stub


def _assert_network(
    data: Any, prop: float, fixed: float, metering: float, months: list[str]
) -> None:
    """Every month billed, and the year's fixed costs, on these T2 terms
    and metering, the transport as the card prints it."""
    assert [bill.month for bill in data.months] == months
    for bill in data.months:
        assert bill.breakdown.network == pytest.approx(prop + _TRANSPORT), bill.month
    assert data.breakdown.network == pytest.approx(prop + _TRANSPORT)
    assert data.fixed.distribution == pytest.approx(fixed)
    assert data.fixed.metering == pytest.approx(metering)


@pytest.mark.parametrize("case", list(_CASES))
async def test_the_year_after_the_last_card_is_billed_on_the_dso_s_sheet(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, case: str
) -> None:
    """December 2027: every month of the year on the T2 terms and the
    metering of the sheet held for 2027, put on 6 % VAT, the transport the
    card's; the card's own network terms are 2026's."""
    _supplier, _contract, _region, dso = _CASES[case]
    freezer.move_to("2027-12-15 10:00:00+01:00")
    sheet = _hold_sheet(dso, 2027)
    entry, stub = await _set_up(hass, case, "2027-12-15")
    _assert_network(
        entry.runtime_data.data,
        sheet.t2_proportional * _VAT,
        sheet.t2_fixed * _VAT,
        sheet.metering_per_year * _VAT,
        [f"2027-{month:02d}" for month in range(1, 13)],
    )
    assert stub.fetch.await_count == 0
    # The card in hand is the last one as it was read, its network as printed.
    assert entry.runtime_data._snapshot.dsos[dso].tiers[TIER_T2].proportional == _T2_PROP


@pytest.mark.parametrize("case", list(_CASES))
async def test_the_last_card_s_own_year_is_billed_as_printed(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, case: str
) -> None:
    """December 2026, the year of the October card: every month on the card
    as printed, whatever sheet is held for the year."""
    _supplier, _contract, _region, dso = _CASES[case]
    freezer.move_to("2026-12-15 10:00:00+01:00")
    _hold_sheet(dso, 2026)
    entry, _stub = await _set_up(hass, case, "2026-12-15")
    _assert_network(
        entry.runtime_data.data,
        _T2_PROP,
        _T2_FIXED,
        _METERING,
        [f"2026-{month:02d}" for month in range(1, 13)],
    )


@pytest.mark.parametrize("case", list(_CASES))
async def test_with_no_sheet_held_the_card_s_network_stands(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, case: str
) -> None:
    freezer.move_to("2027-03-15 10:00:00+01:00")
    entry, _stub = await _set_up(hass, case, "2027-03-15")
    _assert_network(
        entry.runtime_data.data,
        _T2_PROP,
        _T2_FIXED,
        _METERING,
        ["2027-01", "2027-02", "2027-03"],
    )


async def test_the_tick_reads_the_sheet_only_after_the_card_s_year(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The dispatcher reads the area's sheet of 2027 for the October 2026
    card, none of 2026; the card archive's network table is read in 2027
    alone, for an entry reading the archive."""
    supplier, contract, _region, dso = _CASES["fluvius"]
    card = _last_card(supplier, contract, dso)
    with patch.object(fluvius, "ensure_area_network", AsyncMock()) as area:
        await _ENSURE(None, card, dso, 2026)  # type: ignore[arg-type]
        area.assert_not_awaited()
        await _ENSURE(None, card, dso, 2027)  # type: ignore[arg-type]
        area.assert_awaited_once_with(None, 2027, dso)
    for today, read in (("2026-12-15", False), ("2027-03-15", True)):
        freezer.move_to(f"{today} 10:00:00+01:00")
        reads: Any = network_sheet.ensure_network_sheet
        table: Any = network_archive.ensure_network_archive
        reads.reset_mock()
        table.reset_mock()
        entry, _stub = await _set_up(hass, "fluvius", today, **{CONF_CARD_ARCHIVE: True})
        _session, held, held_dso, year = reads.await_args.args
        assert (held.contract, held_dso, year) == (contract, dso, int(today[:4]))
        assert table.await_count > 0 if read else table.await_count == 0
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_archive_s_network_row_is_kept_over_a_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """The card archive's network table is written to the store for a
    withdrawn contract past its card's year, and bills it after a restart;
    the card is stored as the codec has always written it."""
    freezer.move_to("2027-03-15 10:00:00+01:00")
    row = {
        "t1_fixed": 20.0,
        "t1_proportional": 0.03,
        "t2_fixed": 120.0,
        "t2_proportional": 0.02,
        "metering_per_year": 0.0,
    }
    table = {"months": {f"2027-0{month}": {DSO_ORES: row} for month in (1, 2, 3)}}
    network_archive.hold(table)
    entry, _stub = await _set_up(hass, "ores", "2027-03-15")
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}"]["data"]
    assert stored["network_archive"] == table
    supplier, contract, _region, _dso = _CASES["ores"]
    assert stored["snapshot"] == snapshot_to_json(_last_card(supplier, contract, DSO_ORES))
    assert await hass.config_entries.async_unload(entry.entry_id)
    network_archive._HELD.clear()
    with (
        patch.dict(providers.EXTRACTORS, {supplier: _stub_for("ores")}),
        patch(_READ_METER, AsyncMock(return_value=("energy", _gas_so_far(date(2027, 3, 15))))),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert network_archive.held_table() == table
    _assert_network(
        entry.runtime_data.data,
        0.02 * _VAT,
        120.0 * _VAT,
        0.0,
        ["2027-01", "2027-02", "2027-03"],
    )


async def test_an_older_store_s_card_reads_as_before(
    hass: HomeAssistant, hass_storage: dict[str, Any], freezer: FrozenDateTimeFactory
) -> None:
    """The last card in the store an earlier release wrote, no card of its
    month to be found any more: restored as it was, it prices 2027 off the
    sheet like a card read."""
    freezer.move_to("2027-03-15 10:00:00+01:00")
    supplier, contract, region, dso = _CASES["fluvius"]
    sheet = _hold_sheet(dso, 2027)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Withdrawn", data=_data(supplier, contract, region, dso)
    )
    key = f"{DOMAIN}.{entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "snapshot": snapshot_to_json(_last_card(supplier, contract, dso)),
            "fetched_at": "2026-10-01T00:00:00+00:00",
            "release": "2026.11.11",
        },
    }
    stub = replace(_stub_for("fluvius"), fetch_for_month=AsyncMock(return_value=None))
    with (
        patch.dict(providers.EXTRACTORS, {supplier: stub}),
        patch(_READ_METER, AsyncMock(return_value=("energy", _gas_so_far(date(2027, 3, 15))))),
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.data.card_source == "cache"
    assert not entry.runtime_data._reread_last_card
    _assert_network(
        entry.runtime_data.data,
        sheet.t2_proportional * _VAT,
        sheet.t2_fixed * _VAT,
        sheet.metering_per_year * _VAT,
        ["2027-01", "2027-02", "2027-03"],
    )


async def test_the_history_stamp_moves_when_the_sheet_is_first_read(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Brugel's 2027 sheet read after the history was drawn redraws it."""
    freezer.move_to("2027-03-15 10:00:00+01:00")
    entry, _stub = await _set_up(hass, "sibelga", "2027-03-15")
    coordinator = entry.runtime_data
    before = coordinator.card_months_signature()
    assert coordinator.card_months_signature() == before
    _hold_sheet(DSO_SIBELGA, 2027)
    assert coordinator.card_months_signature() != before


async def test_an_earlier_withdrawn_contract_is_billed_on_the_sheet_too() -> None:
    """Eneco Vast left on 1 March 2027 for another contract: its January and
    February are billed on the October 2026 card with the area's 2027 terms,
    and the fill reads that sheet for the year."""
    supplier, contract, region, dso = _CASES["fluvius"]
    sheet = _hold_sheet(dso, 2027)
    data = record_switch(_data(supplier, contract, region, dso), date(2027, 3, 1))
    session = AsyncMock()
    gas = _gas_so_far(date(2027, 3, 20))
    billing = PeriodBilling(MonthCardCache())
    with patch.dict(providers.EXTRACTORS, {supplier: _stub_for("fluvius")}):
        [earlier], missing = await billing.bill(
            session, data, date(2027, 3, 20), gas, 17_000.0, use_archive=False
        )
    assert missing == []
    assert [bill.month for bill in earlier.months] == ["2027-01", "2027-02"]
    for bill in earlier.months:
        assert bill.breakdown.network == pytest.approx(sheet.t2_proportional * _VAT + _TRANSPORT)
    days = (date(2027, 2, 28) - date(2026, 12, 31)).days
    fixed = sum(bill.fixed_cost for bill in earlier.months)
    expected = (60.0 + (sheet.t2_fixed + sheet.metering_per_year) * _VAT) * days / 365
    assert fixed == pytest.approx(expected)
    reads: Any = contract_periods.ensure_network_sheet
    [(_session, card, held_dso, year)] = [call.args for call in reads.await_args_list]
    assert (card.contract, held_dso, year) == (contract, dso, 2027)
    # And the coordinator's stamp and store see it among the year's sheets.
    [(held_card, held_dso)] = billing.sheet_cards(data, date(2027, 3, 20))
    assert network_sheet.takes_network_sheet(held_card, 2027)
    assert not network_sheet.takes_network_sheet(held_card, 2026)


async def test_the_own_row_of_the_comparison_is_quoted_on_the_sheet() -> None:
    """The household's own card in the ranking of March 2027 bills the
    area's 2027 terms, as a card printing them would."""
    supplier, contract, _region, dso = _CASES["fluvius"]
    sheet = _hold_sheet(dso, 2027)
    card = _last_card(supplier, contract, dso)
    overlay = card.dsos[dso]
    printed = replace(
        card,
        supplier="engie",
        contract="engie_easy_fixed",
        dsos={
            dso: replace(
                overlay,
                tiers={
                    **overlay.tiers,
                    TIER_T2: DsoTier(
                        fixed_per_year=sheet.t2_fixed * _VAT,
                        proportional=sheet.t2_proportional * _VAT,
                    ),
                },
                metering_per_year=sheet.metering_per_year * _VAT,
            )
        },
    )
    household = Household(dso=dso, caliber="q10", annual_kwh=17_000.0)

    async def quote(on: SupplierSnapshot) -> float | None:
        result = await quote_contract(
            AsyncMock(),
            _stub_for("fluvius"),
            contract,
            REGION_FLANDERS,
            household,
            "2027-03",
            IndexCache(),
            use_archive=False,
            card=on,
        )
        return result.annual_cost

    own = await quote(card)
    assert own is not None
    assert own == pytest.approx(await quote(printed))
    assert own != pytest.approx(await quote(replace(card, valid_until=date(2027, 1, 31))))
