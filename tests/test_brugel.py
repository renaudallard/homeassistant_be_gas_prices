"""brugel.py: Sibelga's gas network off Brugel's yearly tariff sheet."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.be_gas_prices import brugel
from custom_components.be_gas_prices.providers._pdf_render import extract_pdf_text

_SHEET_2026 = (Path(__file__).parent / "fixtures" / "brugel" / "gaz_2026.pdf").read_bytes()


def _text() -> str:
    return extract_pdf_text(_SHEET_2026)


def test_the_network_is_read_off_the_2026_sheet() -> None:
    """Each tier's fixed term and rate with the four rates per kWh below the
    table, and the yearly-reading meter's term, excluding VAT: with 6% VAT
    the 15,90 and 43,07 EUR a year, 1,99 and 1,45 c/kWh and 24,95 EUR every
    card of October 2026 prints for Sibelga."""
    terms = brugel.parse_network(_text())
    assert terms is not None
    charges = 0.0013227 + 0.0001662 + 0.0015719 + 0.0008964
    assert terms.t1_fixed == 15.0
    assert terms.t1_proportional == pytest.approx(0.0148166 + charges)
    assert terms.t2_fixed == 40.63
    assert terms.t2_proportional == pytest.approx(0.0096911 + charges)
    assert terms.metering_per_year == 23.54
    assert round(terms.t1_proportional * 106, 2) == 1.99
    assert round(terms.t2_proportional * 106, 2) == 1.45
    assert round(terms.metering_per_year * 1.06, 2) == 24.95


@pytest.mark.parametrize("row", ["X = EUR", "Y = EUR", "Comptage YMR", "Redevance de voirie"])
def test_a_sheet_missing_a_row_gives_no_network(row: str) -> None:
    """A term the sheet stops printing refuses it. The rates per kWh below
    the table are summed whatever their labels, which change from one year
    to the next, so one the sheet stops printing is billed as gone."""
    text = "\n".join(line for line in _text().splitlines() if row not in line)
    terms = brugel.parse_network(text)
    if row == "Redevance de voirie":
        assert terms is not None
        assert terms.t1_proportional < brugel.parse_network(_text()).t1_proportional  # type: ignore[union-attr]
    else:
        assert terms is None


def test_a_long_run_of_thousands_groups_is_read_past_in_time() -> None:
    """A line of thousands groups that no comma ends, which the figure
    pattern once tried from every digit of: 80 kB of it held the event loop
    for 86 s. The sheet still reads as it does alone."""
    started = time.monotonic()
    terms = brugel.parse_network("1." + "111." * 20000 + "x\n" + _text())
    assert time.monotonic() - started < 1.0
    assert terms == brugel.parse_network(_text())


def test_a_figure_starts_where_a_number_does() -> None:
    """Never inside one, after a digit or a dot: the pattern read "345,6"
    off "12345,6" before. Every figure a sheet prints stands apart."""
    assert brugel._FIGURE_RE.findall("4.720,80 12345,6 x.5,25 0,0148166") == [
        "4.720,80",
        "0,0148166",
    ]


class _Body:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int) -> Any:
        yield self._body


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self.content_length = None
        self.charset = None
        self.history = ()
        self.url = None
        self.headers: dict[str, str] = {}
        self.content = _Body(body)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Session:
    """Brugel's sheets by year: a 2026 one, none for any other."""

    def __init__(self, body: bytes = _SHEET_2026) -> None:
        self.body = body
        self.asked: list[str] = []

    def get(self, url: str, *_a: object, **_k: object) -> _Response:
        self.asked.append(url)
        if "Gaz-2026" in url:
            return _Response(self.body)
        return _Response(b"", 404)


async def test_the_years_sheet_is_read_and_the_year_before_stands_in() -> None:
    """Read once and kept; a year Brugel does not serve yet reads the year
    before's to stand in, never a later one, and waits out the backoff."""
    session: Any = _Session()
    await brugel.ensure_network(session, 2026)
    held = brugel.cached_network(2026)
    assert held == brugel.parse_network(_text())
    assert session.asked == [
        "https://brugel.brussels/publication/document/notype/2025/fr/Tarif-distribution-Gaz-2026.pdf"
    ]
    session.asked.clear()
    await brugel.ensure_network(session, 2026)
    assert session.asked == [], "a held year was fetched again"
    await brugel.ensure_network(session, 2027)
    assert brugel.cached_network(2027) == held
    assert brugel.cached_network(2025) is None
    session.asked.clear()
    await brugel.ensure_network(session, 2027)
    assert session.asked == [], "the backoff did not hold"


async def test_the_sheet_is_read_off_the_event_loop() -> None:
    """Its text is the sheet's to make as long as it likes."""
    threads: list[threading.Thread] = []
    parse = brugel.parse_network

    def recorded(text: str) -> Any:
        threads.append(threading.current_thread())
        return parse(text)

    with patch.object(brugel, "parse_network", recorded):
        await brugel.ensure_network(_Session(), 2026)  # type: ignore[arg-type]
    assert threads
    assert threading.main_thread() not in threads
    assert brugel.cached_network(2026) == parse(_text())


async def test_a_sheet_that_is_not_a_pdf_never_raises() -> None:
    session: Any = _Session(b"<html>maintenance</html>")
    await brugel.ensure_network(session, 2026)
    assert brugel.cached_network(2026) is None
    assert 2026 in brugel._failed_at
