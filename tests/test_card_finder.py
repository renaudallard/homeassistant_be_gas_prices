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

"""The card finder, on the moves of November 2026.

Each case is a card that moved that month, with the real page that listed
its new address and the real card found there: did the search find it?
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from custom_components.be_gas_prices.providers import _moved, _pdf, engie, luminus
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import FIXTURES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
import card_finder as cf  # type: ignore[import-not-found]
import live_check as lc  # type: ignore[import-not-found]

NOVEMBER = date(2026, 11, 1)
PAGES = FIXTURES / "card_finder"


def _serve(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str], cards: dict[str, bytes]) -> None:
    """Answer each page and card from the fixtures, by the part of its
    address given, and anything else as the 404 it would be."""

    def _match(table: dict[str, Any], url: str) -> Any:
        for part, value in table.items():
            if part in url:
                return value
        raise ExtractorError(f"HTTP 404 fetching {url}")

    async def fetch_text(_session: Any, url: str, **_kw: Any) -> str:
        return str(_match(pages, url))

    async def fetch_pdf_text(_session: Any, url: str, **_kw: Any) -> str:
        payload = _match(cards, url)
        if not _pdf.is_pdf_payload(payload):
            # As fetch_pdf_bytes words it.
            got = (
                "a web page"
                if payload.lstrip(b" \t\r\n").startswith(b"<")
                else f"{len(payload)} bytes of something else"
            )
            raise ExtractorError(f"expected a PDF at {url}, got {got}")
        return _pdf.extract_pdf_text(_pdf.strip_pdf_prefix(payload))

    # The candidates read their pages through their own import of it.
    monkeypatch.setattr(_moved, "fetch_text", fetch_text)
    monkeypatch.setattr(cf, "fetch_pdf_text", fetch_pdf_text)
    # Luminus reads its product list through its own import of the helper.
    monkeypatch.setattr(luminus, "fetch_text", fetch_text)


async def test_elegant_is_found_under_the_name_its_listing_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Elegant renamed its files for November 2026, "FlexGas" to "Flex Gas",
    and its listing linked the new name. The ComfortFlex card beside it and
    the Flex electricity card are other products."""
    flex = "/Current/Flex%20Gas_Residential.pdf"
    _serve(
        monkeypatch,
        {"elegant.be/tariefkaarten": (PAGES / "elegant_tariefkaarten.html").read_text()},
        {flex: (FIXTURES / "elegant" / "Flex Gas_Residential_2026-11.pdf").read_bytes()},
    )
    found = await cf.find(None, "elegant", "elegant_flex", "flanders", NOVEMBER)
    assert found == [
        (f"https://cdn.elegant.be/Pricing/TariffCharts{flex}", "Elegant's listing", None)
    ]


async def test_engie_is_found_under_the_code_its_prices_page_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Engie gave Empty House a new document code for November 2026, "_36_"
    for "_00_", which its prices page named. The registry is put back as it
    was before the fix, since the search must not lean on the code it is
    there to find, and the Dutch page fails, as a page may."""
    before = replace(
        engie._CONTRACTS_BY_ID["engie_empty_house"],
        months_per_region={"V": "00", "W": "00", "B": "00"},
        renumbered=None,
    )
    monkeypatch.setitem(engie._CONTRACTS_BY_ID, "engie_empty_house", before)
    code = "G_EMPTYHOUSE_R_GREY_C_I_36_W_F"
    _serve(
        monkeypatch,
        {"/fr/energie/": (PAGES / "engie_prix_conditions.html").read_text()},
        {code: (FIXTURES / "engie" / f"{code}_202611.pdf").read_bytes()},
    )
    found = await cf.find(None, "engie", "engie_empty_house", "wallonia", NOVEMBER)
    assert [(url.split("document=")[1].split("&")[0], why) for url, _, why in found] == [
        (code, None)
    ]


async def test_energyvision_is_found_where_its_card_page_links_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EnergyVision's tariefkaart page links its November 2026 variable gas
    card in ``inline-files``; the upload folders are asked as well."""
    name = "EV-1126-GSG-nl.pdf"
    _serve(
        monkeypatch,
        {"/nl-be/tariefkaart-2": (PAGES / "energyvision_tariefkaart_2.html").read_text()},
        {f"/inline-files/{name}": (FIXTURES / "energyvision" / name).read_bytes()},
    )
    found = await cf.find(None, "energyvision", "energyvision_gas", "flanders", NOVEMBER)
    files = "https://www.energyvision.be/sites/default/files"
    by_url = {url: (where, why) for url, where, why in found}
    assert by_url[f"{files}/inline-files/{name}"] == ("its card page", None)
    assert all(
        why is not None for url, (_where, why) in by_url.items() if "/inline-files/" not in url
    )


async def test_a_card_not_published_anywhere_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EnergyVision's one-year fixed gas card was still October's on 4
    November 2026: no page linked a November one and no folder held it,
    which tells a late card from a moved one."""
    _serve(
        monkeypatch,
        {"/nl-be/tariefkaart-2": (PAGES / "energyvision_tariefkaart_2.html").read_text()},
        {},
    )
    found = await cf.find(
        None, "energyvision", "energyvision_gas_1_jaar_vast", "flanders", NOVEMBER
    )
    assert found and all("404" in str(why) for _url, _where, why in found)


async def test_luminus_names_the_product_id_each_card_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On 1 November 2026 Luminus listed BasicFlex gas under a new product id
    that had no card yet, and its October id served a card titled November
    (#147 in the electricity repository). The search reports that card and
    says it came by the month before's id: naming the month does not make it
    the month's card, which is why the search only reports."""
    _serve(
        monkeypatch,
        {
            "signing=2026-11": (PAGES / "luminus_products_2026-11.json").read_text(),
            "signing=2026-10": (PAGES / "luminus_products_2026-10.json").read_text(),
        },
        {
            "productId=a1paa000002QbZ5AAK": b'"Pdf not available."',
            "productId=a1p0800000CEbfNAAT": (
                PAGES / "luminus_basicflex_old_id_2026-11.pdf"
            ).read_bytes(),
        },
    )
    found = await cf.find(None, "luminus", "luminus_basicflex", "flanders", NOVEMBER)
    assert [(where, why is None) for _url, where, why in found] == [
        ("the product id of 2026-11", False),
        ("the product id of 2026-10", True),
    ]


async def test_belvus_is_found_on_its_services_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belvus linked its November 2026 gas cards from its services page while
    its history page, the one the extractor read, still ended earlier. The
    professional card beside it is another product."""
    card = "/public/tariefkaarten/2026-11/Tariefkaart_FlexOnline_GAS.pdf"
    _serve(
        monkeypatch,
        {
            "belvus.be/diensten": (FIXTURES / "belvus" / "diensten.html").read_text(),
            "belvus.be/historische-tariefkaarten": (
                FIXTURES / "belvus" / "historische-tariefkaarten.html"
            ).read_text(),
        },
        {card: (FIXTURES / "belvus" / "Tariefkaart_FlexOnline_GAS_2026-11.pdf").read_bytes()},
    )
    found = await cf.find(None, "belvus", "belvus_flex_online", "flanders", NOVEMBER)
    assert found == [(f"https://www.belvus.be{card}", "Belvus's services page", None)]


async def test_dots_is_found_on_its_new_product_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dots rebuilt its site between 3 and 4 November 2026: the product page
    moved, and serves the card at one address where it linked an Odoo
    attachment. The plain address and its download one are one card."""
    page = (PAGES / "dots_tariefkaarten.html").read_text()
    _serve(
        monkeypatch,
        {"dotsenergy.be/": page},
        {
            "/api/tariefkaarten/dots-connect-gas-digital": (
                FIXTURES / "dots" / "Tariefkaart NG Dots Connect - Digital 11_2026.pdf"
            ).read_bytes()
        },
    )
    found = await cf.find(None, "dots", "dots_connect_digital", "flanders", NOVEMBER)
    assert found == [
        (
            "https://www.dotsenergy.be/api/tariefkaarten/dots-connect-gas-digital",
            "Dots's product page",
            None,
        )
    ]


async def test_only_failing_rows_of_a_covered_card_are_annotated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that passed, one that failed on the network, a supplier the
    search does not cover and a check that says nothing of the card's
    address are left as they were, and no row's status changes."""
    asked: list[tuple[str, str, str]] = []

    async def describe(
        _session: Any, supplier: str, contract: str, region: str, month: date
    ) -> str:
        asked.append((supplier, contract, region))
        assert month == NOVEMBER
        return "card finder: found"

    monkeypatch.setattr(cf, "describe", describe)
    checks = [
        lc.Check("dots/dots_connect_digital/flanders: fetch", "fail", "links 0 cards"),
        lc.Check("dots/dots_connect_digital/flanders: card month", "fail", ""),
        lc.Check("belvus/belvus_flex_online/flanders: fetch", "ok"),
        lc.Check("elegant/elegant_flex/flanders: fetch", "transient", "timeout"),
        lc.Check("eneco/eneco_flex/flanders: fetch", "fail", "HTTP 404"),
        lc.Check("dots/dots_connect_digital/flanders: T2 rows", "fail", "missing"),
    ]
    out = await cf.annotate(checks, None, date(2026, 11, 6))
    assert asked == [("dots", "dots_connect_digital", "flanders")]
    assert [c.detail for c in out] == [
        "links 0 cards; card finder: found",
        "card finder: found",
        "",
        "timeout",
        "HTTP 404",
        "missing",
    ]
    assert [c.status for c in out] == [c.status for c in checks]


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Tarifs de novembre 2026", True),
        ("Tariefkaart November 2026", True),
        ("geldig 11/2026", True),
        ("octobre 2026", False),
        ("novembre 2025", False),
    ],
)
def test_the_month_is_read_as_cards_print_it(text: str, named: bool) -> None:
    assert (cf._month_re(NOVEMBER).search(text) is not None) is named
