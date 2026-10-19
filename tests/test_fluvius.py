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

"""fluvius.py: the data management fee off Fluvius's yearly gas tariff sheet."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from custom_components.be_gas_prices import fluvius
from custom_components.be_gas_prices.fluvius import ensure_tariff_sheet
from custom_components.be_gas_prices.providers.base import ExtractorError

_FIXTURES = Path(__file__).parent / "fixtures" / "fluvius"
# Fluvius Antwerpen's sheets as served: 2026's, and 2024's, whose title is set
# in capitals.
_SHEET_2026 = (_FIXTURES / "gas_antwerpen_2026.xlsx").read_bytes()
_SHEET_2024 = (_FIXTURES / "gas_antwerpen_2024.xlsx").read_bytes()

_PUBLICATION = (
    "/nl/publicatie/fluvius-antwerpen-distributienettarief-afname-aardgas-01012026-31122026"
)
_WORKBOOK = (
    "/sites/fluvius/files/2025-11/"
    "distributienettarieven-gas-afname-fluvius-antwerpen-01012026-31122026.xlsx"
)


def _flagged(workbook: bytes, offset: int, value: int) -> bytes:
    """``workbook`` with one byte of the worksheet's central directory entry
    set: offset 8 the encryption flag, 6 the zip version to extract."""
    name = b"xl/worksheets/sheet1.xml"
    start = workbook.find(b"PK\x01\x02")
    while workbook[start + 46 : start + 46 + len(name)] != name:
        start = workbook.find(b"PK\x01\x02", start + 4)
    data = bytearray(workbook)
    data[start + offset] |= value
    return bytes(data)


def _damaged(workbook: bytes) -> bytes:
    """``workbook`` with bytes flipped inside the worksheet's compressed
    data: the zip directory still reads, the inflate does not."""
    with zipfile.ZipFile(io.BytesIO(workbook)) as z:
        info = z.getinfo("xl/worksheets/sheet1.xml")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra) + 50
    data = bytearray(workbook)
    for i in range(start, start + 30):
        data[i] ^= 0xFF
    return bytes(data)


@pytest.fixture(autouse=True)
def _nothing_held() -> None:
    """conftest holds the published fees; these tests read them."""
    fluvius._cache.clear()
    fluvius._failed_at.clear()


def test_the_fee_is_read_off_the_years_sheet() -> None:
    """17,850000000000001 in the workbook, the tariff in cents."""
    assert fluvius.parse_sheet(_SHEET_2026, 2026) == 17.85
    assert fluvius.parse_sheet(_SHEET_2024, 2024) == 13.16
    # Another year's sheet is not this year's.
    assert fluvius.parse_sheet(_SHEET_2026, 2027) is None


def test_a_fee_outside_the_bounds_is_refused(tmp_path: Path) -> None:
    """A unit slip is not a fee: the same sheet printing a hundred times the
    figure yields nothing rather than a fee a hundred times too high."""
    out = tmp_path / "slipped.xlsx"
    with (
        zipfile.ZipFile(_FIXTURES / "gas_antwerpen_2026.xlsx") as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                assert data.count(b"<v>17.850000000000001</v>") == 1
                data = data.replace(b"<v>17.850000000000001</v>", b"<v>1785</v>")
            dst.writestr(info, data)
    assert fluvius.parse_sheet(out.read_bytes(), 2026) is None


class _Body:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int) -> Any:
        yield self._body


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self.content_length = None
        self.charset = "utf-8"
        self.history = ()
        self.url = None
        self.content = _Body(body)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise ExtractorError(f"HTTP {self.status}")

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Session:
    """Fluvius's listing, publication page and workbook, by address."""

    def __init__(self, workbook: bytes = _SHEET_2026) -> None:
        self.pages = {
            fluvius.LISTING_URL: f'<a href="{_PUBLICATION}">Antwerpen</a>'.encode(),
            fluvius._SITE + _PUBLICATION: f'<a href="{_WORKBOOK}">XLSX</a>'.encode(),
            fluvius._SITE + _WORKBOOK: workbook,
        }
        self.asked: list[str] = []

    def get(self, url: str, *_a: object, **_k: object) -> _Response:
        self.asked.append(url)
        body = self.pages.get(url)
        return _Response(b"", 404) if body is None else _Response(body)


async def test_the_sheet_is_found_from_the_listing_and_kept() -> None:
    session: Any = _Session()
    await ensure_tariff_sheet(session, 2026)
    assert session.asked == [
        fluvius.LISTING_URL,
        fluvius._SITE + _PUBLICATION,
        fluvius._SITE + _WORKBOOK,
    ]
    assert fluvius.data_management_fee(2026) == 17.85
    session.asked.clear()
    await ensure_tariff_sheet(session, 2026)
    assert session.asked == [], "a held year was fetched again"


async def test_a_year_not_listed_yet_yields_nothing_until_the_backoff() -> None:
    session: Any = _Session()
    await ensure_tariff_sheet(session, 2027)
    assert fluvius.data_management_fee(2027) is None
    session.asked.clear()
    await ensure_tariff_sheet(session, 2027)
    assert session.asked == [], "the backoff did not hold"


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("a maintenance page answered with 200", b"<html>maintenance</html>"),
        ("an empty body", b""),
        ("a truncated workbook", _SHEET_2026[:4000]),
        ("a workbook with damaged compressed data", _damaged(_SHEET_2026)),
        ("a directory entry flagged encrypted", _flagged(_SHEET_2026, 8, 0x01)),
        ("a directory entry of a zip version to come", _flagged(_SHEET_2026, 6, 199)),
    ],
)
async def test_a_body_that_is_not_a_sheet_never_raises(label: str, body: bytes) -> None:
    """The fetch runs inside the coordinator tick, where anything escaping
    would fail the refresh."""
    session: Any = _Session(body)
    await ensure_tariff_sheet(session, 2026)
    assert fluvius.data_management_fee(2026) is None, label
    assert 2026 in fluvius._failed_at


def test_the_store_round_trips_and_never_replaces_a_read() -> None:
    fluvius.hold(2026, 17.85)
    stored = fluvius.held_table()
    assert stored == {"2026": 17.85}
    fluvius._cache.clear()
    fluvius.restore(stored)
    assert fluvius.data_management_fee(2026) == 17.85
    fluvius.restore({"2026": 18.0, "2025": 1785.0, "x": 1.0, "2024": "n/a"})
    assert fluvius.held_table() == {"2026": 17.85}
    fluvius.restore(None)
    assert fluvius.held_table() == {"2026": 17.85}
