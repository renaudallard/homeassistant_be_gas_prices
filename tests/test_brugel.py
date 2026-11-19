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
from custom_components.be_gas_prices.providers.base import SheetNetwork

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


_BALANCES = "3. Tarif pour les soldes régulatoires EUR / kWh"


def _with_balances(cells: str) -> SheetNetwork | None:
    """The 2026 sheet read with ``cells`` on its regulatory balances row,
    which it prints no figure on."""
    text = _text()
    assert f"{_BALANCES}\n" in text
    return brugel.parse_network(text.replace(_BALANCES, f"{_BALANCES} {cells}"))


@pytest.mark.parametrize("minus", ["-", "−", "–"], ids=["hyphen", "minus sign", "en dash"])
def test_a_refund_per_kwh_is_subtracted(minus: str) -> None:
    """A balance the operator gives back is printed with its minus sign,
    which the figure pattern read past: the refund was added."""
    whole = brugel.parse_network(_text())
    terms = _with_balances(" ".join(f"{minus}0,000{tier}000" for tier in range(1, 6)))
    assert whole is not None and terms is not None
    assert terms.t1_proportional == pytest.approx(whole.t1_proportional - 0.0001)
    assert terms.t2_proportional == pytest.approx(whole.t2_proportional - 0.0002)


def test_a_dash_alone_is_a_tier_the_row_does_not_charge() -> None:
    """As Brugel's 2026 electricity sheet prints it, "EUR / kWh HI - - -
    0,0572325 0,0572325": a cell in its place, and no sign of the figure
    after it."""
    whole = brugel.parse_network(_text())
    assert _with_balances("- - - 0,0572325 0,0572325") == whole
    assert _with_balances("- - - - -") == whole
    terms = _with_balances("- 0,0002000 0,0003000 0,0004000 0,0005000")
    assert whole is not None and terms is not None
    assert terms.t1_proportional == whole.t1_proportional
    assert terms.t2_proportional == pytest.approx(whole.t2_proportional + 0.0002)


@pytest.mark.parametrize(
    "cells",
    [
        pytest.param("0,0002000 0,0003000 0,0004000 0,0005000", id="a tier's cell missing"),
        pytest.param("0,0001 0,0002 0,0003 0,0004 0,0005 0,0006", id="a cell too many"),
        pytest.param(
            "- 0,0001 - 0,0002 - 0,0003 - 0,0004 - 0,0005",
            id="minus signs apart from their figures",
        ),
    ],
)
def test_a_row_per_kwh_without_every_tier_s_cell_refuses_the_sheet(cells: str) -> None:
    """Read by their place, its figures were billed to other tiers than
    their own: four cells put T2's rate on T1."""
    assert _with_balances(cells) is None


def test_a_rate_row_without_every_tier_s_cell_refuses_the_sheet() -> None:
    text = _text()
    assert "Y = EUR / kWh 0,0148166 0,0096911" in text
    assert brugel.parse_network(text.replace("kWh 0,0148166 0,0096911", "kWh 0,0096911")) is None


@pytest.mark.parametrize(
    ("printed", "cells"),
    [
        pytest.param("15,00 40,63 944,86", "15 40,63 944,86", id="a term in whole euros"),
        pytest.param("15,00 40,63 944,86", "40,63 944,86", id="a tier's cell missing"),
        pytest.param("15,00 40,63 944,86", "- 15,00 40,63 944,86", id="a cell too many"),
    ],
)
def test_a_fixed_term_row_without_every_tier_s_cell_refuses_the_sheet(
    printed: str, cells: str
) -> None:
    """Read by their place among the figures found, four cells gave T1 the
    40,63 EUR a year of T2 and T2 the 944,86 of T3."""
    text = _text()
    assert f"X = EUR / an  (°) {printed}" in text
    assert brugel.parse_network(text.replace(printed, cells)) is None


def test_a_long_run_of_thousands_groups_is_read_past_in_time() -> None:
    """A line of thousands groups that no comma ends, which the figure
    pattern once tried from every digit of: 80 kB of it held the event loop
    for 86 s. The sheet still reads as it does alone, and with such a run,
    signed, where a row per kWh prints its cells."""
    text = _text()
    run = "1." + "111." * 20000 + "x"
    started = time.monotonic()
    terms = brugel.parse_network(f"{run}\n{text}\nx EUR / kWh -{run}")
    assert time.monotonic() - started < 1.0
    assert terms == brugel.parse_network(text)


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
