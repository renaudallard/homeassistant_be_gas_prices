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

"""A card that left its usual address, read where its supplier lists it.

Each case is a move of November 2026, replayed on the registry as it stood
before the fix, the real page that listed the new address and the real card
there."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices.const import REGION_FLANDERS, REGION_WALLONIA
from custom_components.be_gas_prices.providers import (
    EXTRACTORS,
    _moved,
    belvus,
    dots,
    elegant,
    eneco,
    energyvision,
    engie,
    luminus,
)
from custom_components.be_gas_prices.providers.base import (
    CardNotListedError,
    ExtractorError,
    SupplierSnapshot,
)
from tests import FIXTURES, fixture_text

PAGES = FIXTURES / "card_finder"
# A day November 2026's cards stood at their new addresses.
NOVEMBER_3 = datetime(2026, 11, 3, 8)
_DOTS_PAGE = (PAGES / "dots_tariefkaarten.html").read_text()
_DOTS_NOVEMBER = "Tariefkaart NG Dots Connect - Digital 11_2026.pdf"
# The Odoo attachment the product page linked before Dots rebuilt its site
# between 3 and 4 November 2026; it now leads to the home page.
_DOTS_ODOO = (
    "https://www.dotsenergy.be/web/content/98918"
    "?unique=adb0e23a7ee37894023a0016f9738428b50941fb&download=true"
)
Fetch = Callable[..., Awaitable[str]]


def _serve(answers: dict[str, str | ExtractorError], asked: list[str] | None = None) -> Fetch:
    """A fetch helper answering by the part of the address given, raising
    an answer that is an error, and anything else as the 404 it would be."""

    async def fetch(_session: Any, url: str, **_kw: Any) -> str:
        if asked is not None:
            asked.append(url)
        for part, answer in answers.items():
            if part in url:
                if isinstance(answer, ExtractorError):
                    raise answer
                return answer
        raise ExtractorError(f"HTTP 404 fetching {url}")

    return fetch


def _pages(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]) -> list[str]:
    """Serve the supplier pages the candidates read. Returns what was asked."""
    asked: list[str] = []
    monkeypatch.setattr(_moved, "fetch_text", _serve(dict(pages), asked))
    return asked


def _elegant_before_the_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Elegant's Flex gas card under the name the registry asked before
    November 2026's rename, and the listing of 4 November that names it
    "Flex Gas" to the extractor and the candidates alike."""
    contract = elegant._CONTRACTS_BY_ID["elegant_flex"]
    monkeypatch.setitem(elegant._CONTRACTS_BY_ID, "elegant_flex", replace(contract, file="FlexGas"))
    listing = (PAGES / "elegant_tariefkaarten.html").read_text()
    monkeypatch.setattr(elegant, "fetch_text", _serve({"elegant.be/tariefkaarten": listing}))
    _pages(monkeypatch, {"elegant.be/tariefkaarten": listing})


async def test_elegant_is_read_under_the_name_its_listing_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On 2 November 2026 Elegant renamed its files, "FlexGas" to "Flex Gas",
    and the extractor refused every card as missing from the listing. The
    registry reads the card where the listing names it; the module's own
    fetch, which the live check asks, still refuses it."""
    _elegant_before_the_fix(monkeypatch)
    card = fixture_text("elegant", "Flex Gas_Residential_2026-11.pdf", "layout")
    monkeypatch.setattr(
        elegant, "fetch_pdf_text_layout", _serve({"/Flex%20Gas_Residential.pdf": card})
    )
    with freeze_time(NOVEMBER_3):
        with pytest.raises(CardNotListedError):
            await elegant.fetch(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
        snap = await EXTRACTORS["elegant"].fetch(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert snap.publication_label == "2026-11"
    assert snap.source_url.endswith("/Current/Flex%20Gas_Residential.pdf")


async def test_engie_is_read_under_the_code_its_prices_page_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty House's November 2026 card answered 404 under "_00_" and its
    prices page named "_36_". The registry asks "_00_", as before the fix,
    and only the French page answers."""
    contract = engie._CONTRACTS_BY_ID["engie_empty_house"]
    monkeypatch.setitem(
        engie._CONTRACTS_BY_ID,
        "engie_empty_house",
        replace(contract, months_per_region={"V": "00", "W": "00", "B": "00"}, renumbered=None),
    )
    _pages(monkeypatch, {"/fr/energie/": (PAGES / "engie_prix_conditions.html").read_text()})
    code = "G_EMPTYHOUSE_R_GREY_C_I_36_W_F"
    card = fixture_text("engie", f"{code}_202611.pdf")
    monkeypatch.setattr(engie, "fetch_pdf_text", _serve({f"{code}&": card}))
    with freeze_time(NOVEMBER_3):
        snap = await EXTRACTORS["engie"].fetch(None, "engie_empty_house", REGION_WALLONIA)  # type: ignore[arg-type]
    assert snap.publication_label == "2026-11"
    assert code in snap.source_url


@pytest.mark.parametrize("old", ["web page", "older card"])
async def test_dots_is_read_where_its_product_page_links_it(
    monkeypatch: pytest.MonkeyPatch, old: str
) -> None:
    """Dots rebuilt its site between 3 and 4 November 2026: the attachment the
    extractor read before leads to the home page, which is no card, and the
    product page embeds the card at a new address. An old address that
    still served September's card, as it once did, is no error, and sends
    the search to the product page as well."""
    monkeypatch.setattr(dots, "_CARD_URL", _DOTS_ODOO)
    if old == "web page":
        home = _DOTS_PAGE.encode()
        stale: str | ExtractorError = ExtractorError(
            f"expected a PDF at {_DOTS_ODOO}, payload starts with {home[:80]!r}"
        )
    else:
        stale = fixture_text("dots", "Tariefkaart NG Dots Connect - Digital 09_2026.pdf")
    asked: list[str] = []
    monkeypatch.setattr(
        dots,
        "fetch_pdf_text",
        _serve(
            {
                "/web/content/98918": stale,
                "/api/tariefkaarten/dots-connect-gas-digital": fixture_text("dots", _DOTS_NOVEMBER),
            },
            asked,
        ),
    )
    _pages(monkeypatch, {"dotsenergy.be/": _DOTS_PAGE})
    with freeze_time(NOVEMBER_3):
        snap = await EXTRACTORS["dots"].fetch(None, "dots_connect_digital", REGION_FLANDERS)  # type: ignore[arg-type]
    assert snap.publication_label == "2026-11"
    assert snap.source_url == "https://www.dotsenergy.be/api/tariefkaarten/dots-connect-gas-digital"
    assert asked == [_DOTS_ODOO, snap.source_url]


async def test_dots_reads_only_the_links_on_its_own_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """A product page that also links card addresses on other hosts, plain
    http and the household's network among them, as a page someone else
    wrote could: only the address on Dots's own site is asked."""
    monkeypatch.setattr(dots, "_CARD_URL", _DOTS_ODOO)
    elsewhere = (
        '<a href="https://www.dotsenergy.be.example.com/web/content/1">a</a>'
        '<a href="https://www.dotsenergy.be@example.com/web/content/2">b</a>'
        '<a href="http://www.dotsenergy.be/web/content/3">c</a>'
        '<a href="http://192.168.1.1/cgi-bin/luci/admin?x=/web/content/4">d</a>'
        '<img src="//10.0.0.1/api/tariefkaarten/dots-connect-gas-digital">'
    )
    asked: list[str] = []
    monkeypatch.setattr(
        dots,
        "fetch_pdf_text",
        _serve(
            {
                "/web/content/98918": ExtractorError(f"HTTP 404 fetching {_DOTS_ODOO}"),
                "/api/tariefkaarten/dots-connect-gas-digital": fixture_text("dots", _DOTS_NOVEMBER),
            },
            asked,
        ),
    )
    _pages(monkeypatch, {"dotsenergy.be/": elsewhere + _DOTS_PAGE})
    with freeze_time(NOVEMBER_3):
        snap = await EXTRACTORS["dots"].fetch(None, "dots_connect_digital", REGION_FLANDERS)  # type: ignore[arg-type]
    assert snap.source_url == "https://www.dotsenergy.be/api/tariefkaarten/dots-connect-gas-digital"
    assert asked == [_DOTS_ODOO, snap.source_url]


async def test_a_sibling_product_is_never_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engie's prices page lists Easy Fixe beside Easy Variable under one
    family code. With Easy Variable's card missing everywhere, its own code
    is asked and its fixed sibling's never is: the 404 stands."""

    async def fetch(_session: Any, contract_id: str, region: str) -> SupplierSnapshot:
        raise ExtractorError("HTTP 404 fetching G_EASY_R_GREY_C_I_12_W_F")

    _pages(monkeypatch, {"/fr/energie/": (PAGES / "engie_prix_conditions.html").read_text()})
    asked: list[str] = []
    monkeypatch.setattr(engie, "fetch_pdf_text", _serve({}, asked))
    fetch_or_moved = _moved._with_moved_fallback("engie", fetch)
    with freeze_time(NOVEMBER_3), pytest.raises(ExtractorError, match="HTTP 404"):
        await fetch_or_moved(None, "engie_easy_variable", REGION_WALLONIA)  # type: ignore[arg-type]
    assert [url.split("document=")[1].split("&")[0] for url in asked] == [
        "G_EASY_R_GREY_C_I_12_W_F"
    ]


async def test_a_card_of_another_month_at_the_new_address_is_not_taken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing's card is read only when it is the running month's: a
    March card there, which parses, leaves the refusal standing."""
    _elegant_before_the_fix(monkeypatch)
    march = fixture_text("elegant", "archive_FlexGas_20260301.pdf", "layout")
    assert elegant.parse_snapshot("elegant_flex", REGION_FLANDERS, march, source_url="x")
    asked: list[str] = []
    monkeypatch.setattr(
        elegant, "fetch_pdf_text_layout", _serve({"/Flex%20Gas_Residential.pdf": march}, asked)
    )
    with freeze_time(NOVEMBER_3), pytest.raises(CardNotListedError):
        await EXTRACTORS["elegant"].fetch(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert [url.rsplit("/", 1)[1] for url in asked] == ["Flex%20Gas_Residential.pdf"]


@pytest.mark.parametrize(
    "error",
    ["Elegant: energy price not found", "HTTP 503 fetching x", "network error fetching x"],
)
async def test_a_card_that_fails_otherwise_is_not_searched_for(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    """A layout the parser does not know or a network failure says nothing
    of the card's address: the error stands and no page is read."""

    async def fetch(_session: Any, contract_id: str, region: str) -> SupplierSnapshot:
        raise ExtractorError(error)

    asked = _pages(monkeypatch, {})
    fetch_or_moved = _moved._with_moved_fallback("elegant", fetch)
    with freeze_time(NOVEMBER_3), pytest.raises(ExtractorError, match=error):
        await fetch_or_moved(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert asked == []


def test_only_the_registry_falls_back() -> None:
    """Home Assistant and the card archive read the registry, and fall back;
    the live check unwraps it to ask each module's own fetch. The wrapper
    keeps the module's name, which tells the readers apart."""
    for supplier, module in (("elegant", elegant), ("engie", engie), ("dots", dots)):
        wrapped = EXTRACTORS[supplier].fetch
        assert module.EXTRACTOR.fetch is module.fetch
        assert wrapped is not module.fetch
        assert inspect.unwrap(wrapped) is module.fetch
        assert inspect.getmodule(wrapped) is module
    for supplier, module in (
        ("belvus", belvus),
        ("energyvision", energyvision),
        ("luminus", luminus),
        ("eneco", eneco),
    ):
        assert EXTRACTORS[supplier].fetch is module.fetch
