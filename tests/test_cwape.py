"""cwape.py: the CWaPE's approved Walloon gas grids."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from custom_components.be_gas_prices import cwape
from custom_components.be_gas_prices.const import DSO_ORES, DSO_RESA

_FIXTURES = Path(__file__).parent / "fixtures" / "cwape"
_LISTING = (_FIXTURES / "documents.json").read_text(encoding="utf-8")
_ORES_2026 = (_FIXTURES / "ores_gaz_2026.xlsx").read_bytes()
_RESA_2026 = (_FIXTURES / "resa_gaz_2026.xlsx").read_bytes()
_ORES_2025 = (_FIXTURES / "ores_gaz_2025.xlsx").read_bytes()


def test_each_operators_2026_grid_is_what_the_cards_print() -> None:
    """The fixed term and the rate of gas carried by pipe, with the public
    service, levy and regulatory balance rates added, excluding VAT: with 6%
    VAT the 31,91 and 140,93 EUR a year and 4,29 and 2,21 c/kWh every card of
    October 2026 prints on ORES. Wallonia bills no metering term."""
    grid = cwape.parse_grid(_ORES_2026)
    assert grid is not None
    assert (grid.valid_from, grid.valid_to) == (date(2026, 1, 1), date(2026, 12, 31))
    ores = grid.network
    assert ores.t1_fixed == 30.1
    assert ores.t1_proportional == pytest.approx(
        0.0302172 + 0.0038412 + 0.00191 + 0.0024917 + 0.0000357 + 0.0019706
    )
    assert ores.t2_fixed == 132.95
    assert round(ores.t1_proportional * 106, 2) == 4.29
    assert round(ores.t2_proportional * 106, 2) == 2.21
    assert ores.metering_per_year == 0.0


def test_gas_carried_by_road_is_left_out() -> None:
    """RESA bills a supplement only where the gas is carried by road; every
    card's rate leaves it out: 4,64 c/kWh with VAT on T1."""
    grid = cwape.parse_grid(_RESA_2026)
    assert grid is not None
    assert grid.network.t1_fixed == 32.63
    assert round(grid.network.t1_proportional * 106, 2) == 4.64
    assert round(grid.network.t2_proportional * 106, 2) == 2.53


def test_the_2025_grid_reads_the_same_way() -> None:
    grid = cwape.parse_grid(_ORES_2025)
    assert grid is not None and grid.valid_to == date(2025, 12, 31)
    assert grid.network.t1_fixed == 29.11


def test_the_listing_gives_each_grid_its_year_operator_and_address() -> None:
    documents = cwape.parse_listing(_LISTING)
    ores = cwape.grid_documents(documents, 2026, cwape._OPERATORS[DSO_ORES])
    resa = cwape.grid_documents(documents, 2026, cwape._OPERATORS[DSO_RESA])
    assert [doc.url.rsplit("/", 1)[-1][:8] for doc in ores] == ["ORES%20G"]
    assert [doc.url.rsplit("/", 1)[-1][:8] for doc in resa] == ["RESA%20G"]
    assert all(doc.url.endswith(".xlsx") for doc in ores + resa)
    # An id that came to mean another operator is not read for it.
    assert cwape.grid_documents(documents, 2026, ("453", "RESA")) == []


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
    """The listing, and ORES's and RESA's 2026 grids, by address."""

    def __init__(self) -> None:
        self.pages: dict[str, bytes] = {cwape._LISTING_URL: _LISTING.encode()}
        for doc in cwape.parse_listing(_LISTING):
            if doc.year == 2026 and doc.url.endswith(".xlsx"):
                self.pages[doc.url] = _ORES_2026 if doc.operator == "453" else _RESA_2026
        self.asked: list[str] = []

    def get(self, url: str, *_a: object, **_k: object) -> _Response:
        self.asked.append(url)
        body = self.pages.get(url)
        return _Response(b"", 404) if body is None else _Response(body)


async def test_a_year_is_read_once_and_the_year_before_stands_in() -> None:
    """Read off one listing and kept; a year whose grid is not listed yet
    reads the year before's, which stands in as the latest begun."""
    session: Any = _Session()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked[0] == cwape._LISTING_URL and len(session.asked) == 2
    held = cwape.cached_network(date(2026, 11, 1), DSO_ORES)
    assert held == cwape.parse_grid(_ORES_2026).network  # type: ignore[union-attr]
    session.asked.clear()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked == [], "a year held was read again"
    await cwape.ensure_network(session, 2028, DSO_RESA)
    assert cwape.cached_network(date(2028, 3, 1), DSO_RESA) is None
    await cwape.ensure_network(session, 2027, DSO_RESA)
    assert cwape.cached_network(date(2027, 3, 1), DSO_RESA) is not None
    assert cwape.cached_network(date(2025, 12, 1), DSO_ORES) is None
    await cwape.ensure_network(session, 2026, "sibelga")


async def test_a_listing_that_does_not_answer_never_raises() -> None:
    session: Any = _Session()
    session.pages[cwape._LISTING_URL] = b"<html>not json</html>"
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert cwape.cached_network(date(2026, 11, 1), DSO_ORES) is None
    session.asked.clear()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked == [], "a listing that failed was asked again at once"
