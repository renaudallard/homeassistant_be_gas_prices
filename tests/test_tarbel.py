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

"""tarbel.py: the household gas excise off FPS Finance's excise tariff."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch
from xml.sax.saxutils import escape

import pytest

from custom_components.be_gas_prices import tarbel
from custom_components.be_gas_prices.const import GAS_EXCISE_RESIDENTIAL_HTVA
from custom_components.be_gas_prices.providers.base import ExtractorError
from custom_components.be_gas_prices.tarbel import ensure_tariff

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_HEADER = [
    "Omschrijving van de GN-code",
    "Vak 47 (1)/ (hoofd)rubriek maandstaat",
    "Vak 47 (3)",
    "Maatstaf van Heffing codes 100, 200, 300 en 400",
]
_GAS = (
    "Aardgas gebruikt als verwarmingsbrandstof voor niet-zakelijk gebruik, m.u.v. de "
    "beschermde residentiële afnemer\nvoor een jaarlijks verbruik {slice}\n"
    "van toepassing vanaf {start}"
)


def _row(
    slice_: str, start: str, end: str | None, rate: str, unit: str = "EUR per MWh"
) -> list[str]:
    text = _GAS.format(slice=slice_, start=start)
    if end is not None:
        text += f" tot en met {end}"
    return [text, "100\n200\n300", f"0\n{rate}\n0", unit]


def _workbook(rows: list[list[str]], sheet: str = "Q") -> bytes:
    """A workbook of one sheet named ``sheet``, the header row then
    ``rows``, as inline strings."""
    lines = []
    for number, cells in enumerate([_HEADER, *rows], start=1):
        body = "".join(
            f'<c r="{col}{number}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
            for col, text in zip("PQRS", cells, strict=True)
        )
        lines.append(f'<row r="{number}">{body}</row>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{_NS}" xmlns:r="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships"><sheets><sheet name="{sheet}" sheetId="1" '
            'r:id="rId1"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{_NS}"><sheetData>{"".join(lines)}</sheetData></worksheet>',
        )
    return out.getvalue()


_QUARTER = [
    _row("van 0 tot 12 MWh", "01/01/2027", "31/03/2027", "11,39"),
    _row("vanaf 12 MWh", "01/01/2027", "31/03/2027", "11,39"),
]


def test_the_published_tariff_gives_each_quarter_s_two_slices(
    excise_tariff_periods: tuple[tarbel.Period, ...],
) -> None:
    """Annex 7 as served on 2026-10-19: April 2023 to December 2026."""
    periods = excise_tariff_periods
    assert periods[0][:2] == (date(2023, 4, 1), date(2023, 6, 30))
    assert periods[-1] == (
        date(2026, 10, 1),
        date(2026, 12, 31),
        pytest.approx(0.01031),
        pytest.approx(0.01116),
    )
    assert (date(2026, 8, 1), date(2026, 9, 30)) in [p[:2] for p in periods]
    # The protected customer's 3,09 and the energy contribution's codes are
    # no household slice.
    assert all(0.004 < low < 0.011 and 0.008 < high < 0.012 for *_d, low, high in periods)


def test_the_published_tariff_gives_the_protected_customer_s_periods(
    protected_tariff_periods: tuple[tarbel.ProtectedPeriod, ...],
) -> None:
    """Exempt from April to June 2023, 2,77 EUR/MWh from July 2023 and 3,09
    from August 2026, the rows Q554, Q555 and Q586 of annex 7."""
    assert protected_tariff_periods == (
        (date(2023, 4, 1), date(2023, 6, 30), 0.0),
        (date(2023, 7, 1), date(2026, 7, 31), pytest.approx(0.00277)),
        (date(2026, 8, 1), None, pytest.approx(0.00309)),
    )
    assert tarbel.protected_excise_for(date(2026, 7, 15)) == pytest.approx(0.00277)
    assert tarbel.protected_excise_for(date(2026, 8, 1)) == pytest.approx(0.00309)
    assert tarbel.protected_excise_for(date(2023, 3, 1)) is None


def test_an_open_period_ends_with_its_year(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q586 lists 3,09 "vanaf 01/08/2026" with no end, but article 419 sets
    3,42 from 2027: past December the open period leaves the month to
    Justel, or to no rate at all."""
    assert tarbel.protected_excise_for(date(2026, 12, 1)) == pytest.approx(0.00309)
    assert tarbel.protected_excise_for(date(2027, 1, 1)) is None
    monkeypatch.setattr(tarbel, "_HELD", {})
    slices = ("van 0 tot 12 MWh", "vanaf 12 MWh")
    tarbel.hold(tarbel.parse(_workbook([_row(s, "01/10/2026", None, "11,16") for s in slices])))
    assert tarbel.excise_for(date(2026, 12, 1)) == (pytest.approx(0.01116),) * 2
    assert tarbel.excise_for(date(2027, 1, 1)) is None


def _protected_row(start: str, end: str | None, rate: str) -> list[str]:
    text = (
        "Aardgas gebruikt als verwarmingsbrandstof voor niet-zakelijk gebruik, "
        f"beschermde residentiële afnemer\nvan toepassing vanaf {start}"
    )
    if end is not None:
        text += f" tot en met {end}"
    return [text, "100\n200\n300", f"0\n{rate}\n0", "EUR per MWh"]


def test_a_synthetic_protected_period_reads() -> None:
    payload = _workbook([*_QUARTER, _protected_row("01/01/2027", None, "3,42")])
    assert tarbel.parse_protected(payload) == ((date(2027, 1, 1), None, pytest.approx(0.00342)),)
    # The household slices are read past it.
    assert tarbel.parse(payload)[0][:2] == (date(2027, 1, 1), date(2027, 3, 31))
    overlapping = _workbook(
        [_protected_row("01/01/2027", None, "3,42"), _protected_row("01/04/2027", None, "3,5")]
    )
    with pytest.raises(tarbel.TarbelError, match="overlap"):
        tarbel.parse_protected(overlapping)


async def test_a_workbook_without_protected_rows_still_bills_the_household() -> None:
    """Only the social tariff bills the protected rate: its rows missing
    from the workbook keep the held ones and leave the household slices
    read."""
    held = tarbel.protected_held_table()
    tarbel._failed_at = tarbel._fetched_at = None
    with patch.object(tarbel, "fetch_bytes", AsyncMock(return_value=_workbook(_QUARTER))):
        await ensure_tariff(AsyncMock())
    assert tarbel._failed_at is None
    assert tarbel.excise_for(date(2027, 2, 1)) is not None
    assert tarbel.protected_held_table() == held
    tarbel._fetched_at = None


def test_the_quarters_typed_in_const_are_the_tariff_s() -> None:
    for (year, month), low, high in GAS_EXCISE_RESIDENTIAL_HTVA:
        assert tarbel.excise_for(date(year, month, 1)) == (pytest.approx(low), pytest.approx(high))


def test_a_month_is_priced_on_the_period_it_falls_in() -> None:
    assert tarbel.excise_for(date(2026, 11, 20)) == (pytest.approx(0.01031), pytest.approx(0.01116))
    assert tarbel.excise_for(date(2027, 1, 1)) is None
    assert tarbel.excise_for(date(2023, 3, 1)) is None


def test_a_synthetic_quarter_reads() -> None:
    assert tarbel.parse(_workbook(_QUARTER)) == (
        (date(2027, 1, 1), date(2027, 3, 31), pytest.approx(0.01139), pytest.approx(0.01139)),
    )


@pytest.mark.parametrize(
    ("label", "rows", "sheet"),
    [
        ("one slice only", _QUARTER[:1], "Q"),
        (
            "a unit changed",
            [_QUARTER[0], _row("vanaf 12 MWh", "01/01/2027", "31/03/2027", "1", "EUR per kWh")],
            "Q",
        ),
        ("no gas row", [], "Q"),
        (
            "periods that overlap",
            [
                *_QUARTER,
                *(
                    _row(s, "01/03/2027", "30/06/2027", "11,39")
                    for s in ("van 0 tot 12 MWh", "vanaf 12 MWh")
                ),
            ],
            "Q",
        ),
        (
            "an implausible rate",
            [_QUARTER[0], _row("vanaf 12 MWh", "01/01/2027", "31/03/2027", "113900")],
            "Q",
        ),
        (
            "one slice open, the other closed, from the same day",
            [_row("van 0 tot 12 MWh", "01/01/2027", None, "11,39"), _QUARTER[1]],
            "Q",
        ),
    ],
)
def test_a_workbook_not_as_expected_is_refused(
    label: str, rows: list[list[str]], sheet: str
) -> None:
    with pytest.raises(tarbel.TarbelError):
        tarbel.parse(_workbook(rows, sheet))


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"<html>maintenance</html>", id="a maintenance page answered with 200"),
        pytest.param(_workbook(_QUARTER, sheet="R"), id="a workbook without sheet Q"),
    ],
)
async def test_a_payload_that_is_not_the_tariff_never_raises(payload: bytes) -> None:
    """The read runs inside the coordinator tick, where anything escaping
    would fail the refresh; what is held goes on billing."""
    held = tarbel.held_table()
    tarbel._failed_at = tarbel._fetched_at = None
    with patch.object(tarbel, "fetch_bytes", AsyncMock(return_value=payload)):
        await ensure_tariff(AsyncMock())
    assert tarbel._failed_at is not None
    assert tarbel.held_table() == held
    tarbel._failed_at = None


async def test_a_fetch_failure_waits_and_a_read_holds() -> None:
    tarbel._failed_at = tarbel._fetched_at = None
    down = AsyncMock(side_effect=ExtractorError("network error fetching x: timeout"))
    with patch.object(tarbel, "fetch_bytes", down):
        await ensure_tariff(AsyncMock())
        await ensure_tariff(AsyncMock())
    assert down.await_count == 1, "the backoff did not hold"
    tarbel._failed_at = None
    with patch.object(tarbel, "fetch_bytes", AsyncMock(return_value=_workbook(_QUARTER))):
        await ensure_tariff(AsyncMock())
    assert tarbel.excise_for(date(2027, 2, 1)) == (pytest.approx(0.01139), pytest.approx(0.01139))
    # A period the new read no longer lists stays held.
    assert tarbel.excise_for(date(2026, 11, 1)) is not None
    tarbel._fetched_at = None


async def test_entries_set_up_together_read_the_workbook_once() -> None:
    tarbel._failed_at = tarbel._fetched_at = None
    download = AsyncMock(return_value=_workbook(_QUARTER))
    with patch.object(tarbel, "fetch_bytes", download):
        await asyncio.gather(*(ensure_tariff(AsyncMock()) for _ in range(3)))
    assert download.await_count == 1
    tarbel._fetched_at = None


def test_the_store_round_trips_and_never_replaces_a_read() -> None:
    stored: Any = tarbel.held_table()
    tarbel._HELD.clear()
    tarbel.restore(stored)
    assert tarbel.held_table() == stored
    tarbel.restore([["2027-01-01", None, 0.5, 0.5]])
    assert tarbel.held_table() == stored
    tarbel._HELD.clear()
    tarbel.restore([["2027-01-01", "bad", 0.01, 0.01]])
    assert tarbel.held_table() == []
    protected: Any = tarbel.protected_held_table()
    assert protected
    tarbel._PROTECTED_HELD.clear()
    tarbel.restore_protected(protected)
    assert tarbel.protected_held_table() == protected
    tarbel.restore_protected([["2027-01-01", None, 0.5]])
    assert tarbel.protected_held_table() == protected
