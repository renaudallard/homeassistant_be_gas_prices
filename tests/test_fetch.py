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

"""The shared HTTP helpers, against a local server."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.be_gas_prices import month_cards
from custom_components.be_gas_prices.archive_table import ArchiveTable
from custom_components.be_gas_prices.providers import _pdf
from custom_components.be_gas_prices.providers._pdf import (
    fetch_bytes,
    fetch_pdf_bytes,
    fetch_text,
    guarded,
    head_freshness_key,
    head_or_raise,
    is_missing_card_error,
    is_transient_fetch_error,
)
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests.local_https import device, https_site, malformed_status_line, raw_site


def _app(handler: Any, path: str = "/") -> web.Application:
    app = web.Application()
    app.router.add_get(path, handler)
    return app


async def test_a_page_in_another_charset_is_read_not_raised(
    socket_enabled: None, tmp_path: Path
) -> None:
    """A maintenance page in Latin-1 sent without a charset: the decode
    must not escape as anything but text the parser then refuses."""

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            body="Maintenance planifiée".encode("latin-1"), content_type="text/html"
        )

    async with https_site(_app(page), tmp_path) as site:
        text = await fetch_text(site.session, site.url("/"))
    assert text.startswith("Maintenance planifi")


_PUNYCODE_BOMB = b"-" + b"a" * 200_000


async def test_a_charset_off_the_list_is_read_as_utf8(socket_enabled: None, tmp_path: Path) -> None:
    """200 kB that name punycode, whose decoder takes seconds on them: the
    decode runs on the event loop, so the codec a server names is not
    trusted. Read as punycode they would be other text."""

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            body=_PUNYCODE_BOMB, headers={"Content-Type": "text/html; charset=punycode"}
        )

    async with https_site(_app(page), tmp_path) as site:
        text = await fetch_text(site.session, site.url("/"))
    assert text == _PUNYCODE_BOMB.decode("ascii")


def test_a_body_that_names_punycode_decodes_in_well_under_a_second() -> None:
    start = time.monotonic()
    _PUNYCODE_BOMB.decode(_pdf._charset("punycode"), errors="replace")
    assert time.monotonic() - start < 0.5


@pytest.mark.parametrize(
    ("declared", "body", "text"),
    [
        # Justel's page, which declares it.
        ("ISO-8859-1", "planifiée".encode("latin-1"), "planifiée"),
        ("windows-1252", "5 €".encode("cp1252"), "5 €"),
        ("US-ASCII", b"5 EUR", "5 EUR"),
        ("UTF8", "planifiée".encode(), "planifiée"),
        (None, "planifiée".encode(), "planifiée"),
        # Unknown to Python, or a codec off the list: UTF-8.
        ("utf8mb4", "planifiée".encode(), "planifiée"),
        ("idna", "planifiée".encode(), "planifiée"),
        ("utf-16", "planifiée".encode(), "planifiée"),
    ],
)
def test_a_declared_charset_is_read_in_its_codec_when_listed(
    declared: str | None, body: bytes, text: str
) -> None:
    assert body.decode(_pdf._charset(declared), errors="replace") == text


async def _streamed(request: web.Request) -> web.StreamResponse:
    """A body sent in chunks with no Content-Length, as a chunked answer is."""
    resp = web.StreamResponse()
    resp.content_type = "application/pdf"
    await resp.prepare(request)
    await resp.write(b"%PDF-1.7\n")
    for _ in range(8):
        await resp.write(b" " * 1024)
    await resp.write_eof()
    return resp


@pytest.mark.parametrize(
    ("fetch", "cap"), [(fetch_text, "MAX_TEXT_BYTES"), (fetch_pdf_bytes, "MAX_RESPONSE_BYTES")]
)
async def test_a_body_past_the_cap_is_refused_as_it_streams(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fetch: Any, cap: str
) -> None:
    monkeypatch.setattr(_pdf, cap, 4096)
    async with https_site(_app(_streamed), tmp_path) as site:
        with pytest.raises(ExtractorError, match="more than 4096 bytes"):
            await fetch(site.session, site.url("/"))


async def test_a_body_declared_past_the_cap_is_refused_unread(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_pdf, "MAX_RESPONSE_BYTES", 4096)

    async def large(request: web.Request) -> web.Response:
        return web.Response(body=b"%PDF" + b" " * 8192, content_type="application/pdf")

    async with https_site(_app(large), tmp_path) as site:
        with pytest.raises(ExtractorError, match="declared 8196 bytes"):
            await fetch_pdf_bytes(site.session, site.url("/"))


def test_a_text_is_held_to_a_tighter_cap_than_a_card() -> None:
    assert _pdf.MAX_TEXT_BYTES < _pdf.MAX_RESPONSE_BYTES


async def test_a_caller_sizes_the_cap_of_its_own_text(socket_enabled: None, tmp_path: Path) -> None:
    async with https_site(_app(_streamed), tmp_path) as site:
        with pytest.raises(ExtractorError, match="more than 4096 bytes"):
            await fetch_text(site.session, site.url("/"), limit=4096)
        assert len(await fetch_text(site.session, site.url("/"), limit=9 + 8 * 1024)) == 9 + 8192


async def test_an_archive_row_past_its_cap_is_not_parsed(
    socket_enabled: None, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rows are a few kB. A larger one is refused as it streams, before
    json.loads would hold the event loop on it."""

    async def row(request: web.Request) -> web.Response:
        return web.json_response([{}] * 100_000)

    app = web.Application()
    app.router.add_get("/rows/{tail:.*}", row)
    async with https_site(app, tmp_path) as site:
        assert await _archived_row(site.session, site.url("/card")) is None
    assert "declared 400000 bytes (limit 262144)" in caplog.text


async def test_an_archive_table_past_its_cap_is_not_held(
    socket_enabled: None, tmp_path: Path
) -> None:
    """vat.json, law.json and network.json are tens of kB at most."""
    table = {
        "residential": {
            f"{year}-{month:02d}": 0.06 for year in range(2025, 2027) for month in range(1, 13)
        }
    }

    async def answer(request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(table) + " " * (1024 * 1024), content_type="application/json"
        )

    held: list[Any] = []
    async with https_site(_app(answer, "/vat.json"), tmp_path) as site:
        reader = ArchiveTable(site.url("/vat.json"), held.append)
        await reader.ensure(site.session)
    assert held == []
    assert reader.failed_at is not None


async def test_a_body_under_the_cap_is_read_whole(socket_enabled: None, tmp_path: Path) -> None:
    async with https_site(_app(_streamed), tmp_path) as site:
        payload = await fetch_pdf_bytes(site.session, site.url("/"))
    assert payload.startswith(b"%PDF-1.7") and len(payload) == 9 + 8 * 1024


@pytest.mark.parametrize(
    "fetch",
    [fetch_text, fetch_bytes, fetch_pdf_bytes, head_or_raise, head_freshness_key],
)
@pytest.mark.parametrize(
    "where",
    [
        "{device}/card",
        "https://127.0.0.1:{port}/card",
        "https://[::ffff:127.0.0.1]:{port}/card",
        "https://localhost:{port}/card",
    ],
)
async def test_an_address_off_https_or_on_the_household_network_is_never_asked(
    socket_enabled: None, tmp_path: Path, fetch: Any, where: str
) -> None:
    """A link read off a page that names a plain http address, or an https
    one only the Home Assistant host can reach."""
    async with device() as lan, https_site(_app(_streamed, "/card"), tmp_path) as site:
        url = where.format(device=lan.url, port=site.port)
        try:
            answer = await fetch(site.session, url)
        except ExtractorError as err:
            assert str(err).startswith(f"refusing {_pdf.shown_url(url)}: not a")
        else:
            # The probe, which answers None for what it cannot read.
            assert answer is None
    assert lan.asked == []
    assert site.asked == []


@pytest.mark.parametrize(
    ("url", "on"),
    [
        ("https://www.brusol.be/sites/default/files/x.pdf", True),
        # A path on Brusol's site, however it is spelt.
        ("https://www.brusol.be//www.example.com/x.pdf", True),
        ("https://www.brusol.be", False),
        ("https://www.brusol.be.example.com/x.pdf", False),
        ("https://www.brusol.be@example.com/x.pdf", False),
        ("https://www.brusol.be\\@example.com/x.pdf", False),
        ("https://www.brusol.be:8443/x.pdf", False),
        ("http://www.brusol.be/x.pdf", False),
        ("//www.brusol.be/x.pdf", False),
    ],
)
def test_a_link_is_on_a_site_by_its_scheme_host_and_port(url: str, on: bool) -> None:
    assert _pdf.on_site(url, "https://www.brusol.be") is on


_ADMIN = "/cgi-bin/reboot?confirm=1"
_CARD = b"%PDF-1.7\n"


def _redirecting(target: str, status: int = 302) -> web.Application:
    """A site whose /card, and every address under /rows/, redirects to
    ``target``, where "{port}" is the site's own port."""

    async def card(request: web.Request) -> web.Response:
        port = request.url.port
        return web.Response(status=status, headers={"Location": target.format(port=port)})

    app = web.Application()
    app.router.add_route("*", "/card", card)
    app.router.add_route("*", "/rows/{tail:.*}", card)
    return app


async def _archived_row(session: aiohttp.ClientSession, url: str) -> Any:
    """The card archive's row read through month_cards, the archive's address
    being ``url``."""
    with patch.object(month_cards, "CARD_ARCHIVE_URL", url.removesuffix("/card") + "/rows"):
        return await month_cards.fetch_archived_row(session, "acme", "fix", "wallonia", "2026-11")


@pytest.mark.parametrize(
    "target",
    [
        "{device}" + _ADMIN,
        "https://127.0.0.1:{port}" + _ADMIN,
        "https://[::ffff:127.0.0.1]:{port}" + _ADMIN,
        "https://0x7f000001:{port}" + _ADMIN,
        "https://localhost:{port}" + _ADMIN,
        "https://169.254.169.254/latest/meta-data/",
        "https://[fe80::1]/",
        "//10.0.0.1/admin",
    ],
)
async def test_a_redirect_is_checked_before_it_is_followed(
    socket_enabled: None, tmp_path: Path, target: str
) -> None:
    """A site that answers with a redirect to an address only the Home
    Assistant host can reach: the address is never asked, and does not
    reach the error the user is shown."""
    async with device() as lan:
        app = _redirecting(target.replace("{device}", lan.url))
        async with https_site(app, tmp_path) as site:
            with pytest.raises(ExtractorError, match="refusing to follow it") as err:
                await fetch_text(site.session, site.url("/card"))
    assert lan.asked == []
    assert site.asked == ["GET /card"]
    assert "reboot" not in str(err.value)
    assert "127.0.0.1" not in str(err.value)


@pytest.mark.parametrize(
    "fetch",
    [fetch_text, fetch_bytes, fetch_pdf_bytes, head_or_raise, head_freshness_key, _archived_row],
)
async def test_every_fetch_checks_a_redirect_before_following_it(
    socket_enabled: None, tmp_path: Path, fetch: Any
) -> None:
    async with device() as lan, https_site(_redirecting(lan.url + _ADMIN), tmp_path) as site:
        try:
            answer = await fetch(site.session, site.url("/card"))
        except ExtractorError as err:
            assert "refusing to follow it" in str(err)
        else:
            # The two that answer None for what they cannot read.
            assert answer is None
    assert lan.asked == []
    assert len(site.asked) == 1


async def test_a_redirect_over_https_to_another_site_is_followed(
    socket_enabled: None, tmp_path: Path
) -> None:
    """energie.be's document key answers with its storage on another host,
    and TotalEnergies' index publication with its CDN. A relative address
    is the answering site's, and the query goes with the first request
    only, as aiohttp sends them."""

    async def start(request: web.Request) -> web.Response:
        port = request.url.port
        raise web.HTTPFound(f"https://store.test:{port}/assets/key")

    async def key(request: web.Request) -> web.Response:
        raise web.HTTPSeeOther("/assets/card.pdf")

    async def card(request: web.Request) -> web.Response:
        return web.Response(body=_CARD, headers={"Last-Modified": "Thu, 05 Nov 2026 08:00:00 GMT"})

    app = web.Application()
    app.router.add_get("/document", start)
    app.router.add_get("/assets/key", key)
    app.router.add_get("/assets/card.pdf", card)
    async with https_site(app, tmp_path) as site:
        url = site.url("/document")
        assert await fetch_pdf_bytes(site.session, url) == _CARD
        assert await fetch_text(site.session, url, params={"key": "Indexation"}) == "%PDF-1.7\n"
        assert await head_or_raise(site.session, url) == "Thu, 05 Nov 2026 08:00:00 GMT"
    assert site.asked == [
        *("GET /document", "GET /assets/key", "GET /assets/card.pdf"),
        *("GET /document?key=Indexation", "GET /assets/key", "GET /assets/card.pdf"),
        *("HEAD /document", "HEAD /assets/key", "HEAD /assets/card.pdf"),
    ]


@pytest.mark.parametrize("headers", [{}, {"Location": ""}])
async def test_a_redirect_without_a_target_is_the_answer(
    socket_enabled: None, tmp_path: Path, headers: dict[str, str]
) -> None:
    """As aiohttp has it: a redirect status with no Location, or an empty one,
    is handed over as the answer."""

    async def moved(request: web.Request) -> web.Response:
        return web.Response(status=302, headers=headers, text="moved")

    app = web.Application()
    app.router.add_get("/card", moved)
    async with https_site(app, tmp_path) as site:
        assert await fetch_text(site.session, site.url("/card")) == "moved"
    assert site.asked == ["GET /card"]


async def test_a_redirect_loop_is_a_network_error(socket_enabled: None, tmp_path: Path) -> None:
    async with https_site(_redirecting("/card"), tmp_path) as site:
        with pytest.raises(ExtractorError, match=r"^network error fetching") as err:
            await fetch_text(site.session, site.url("/card"))
    assert is_transient_fetch_error(str(err.value))
    assert len(site.asked) == _pdf.MAX_REDIRECTS + 1


async def test_the_timeout_covers_every_redirect(socket_enabled: None, tmp_path: Path) -> None:
    """Three hops of 0,4 s each are past a total of one second, though no
    single one is."""

    async def hop(request: web.Request) -> web.Response:
        await asyncio.sleep(0.4)
        step = int(request.match_info["step"])
        if step == 3:
            return web.Response(text="end")
        raise web.HTTPFound(f"/hop/{step + 1}")

    app = web.Application()
    app.router.add_get("/hop/{step}", hop)
    async with https_site(app, tmp_path) as site:
        with pytest.raises(ExtractorError, match=r"^network error fetching .*TimeoutError"):
            await fetch_text(site.session, site.url("/hop/0"), timeout=1)
    assert len(site.asked) <= 3


async def test_a_head_tells_a_name_not_there_from_a_site_that_is_down(
    socket_enabled: None, tmp_path: Path
) -> None:
    async def card(request: web.Request) -> web.Response:
        return web.Response(
            body=b"%PDF", headers={"Last-Modified": "Thu, 01 Oct 2026 07:34:44 GMT"}
        )

    async def busy(request: web.Request) -> web.Response:
        return web.Response(status=503)

    app = web.Application()
    app.router.add_get("/card.pdf", card)
    app.router.add_get("/busy.pdf", busy)
    async with https_site(app, tmp_path) as site:
        url = site.url("/card.pdf")
        assert await head_or_raise(site.session, url) == "Thu, 01 Oct 2026 07:34:44 GMT"
        with pytest.raises(ExtractorError) as missing:
            await head_or_raise(site.session, site.url("/card_0.pdf"))
        with pytest.raises(ExtractorError) as down:
            await head_or_raise(site.session, site.url("/busy.pdf"))
        # Nothing listens on port 1: the site is down.
        with pytest.raises(ExtractorError) as gone:
            await head_or_raise(site.session, "https://card.test:1/card.pdf")
    assert is_missing_card_error(str(missing.value))
    assert not is_transient_fetch_error(str(missing.value))
    assert is_transient_fetch_error(str(down.value))
    assert not is_missing_card_error(str(down.value))
    assert str(gone.value).startswith("network error fetching")


@pytest.mark.parametrize(
    "page", [b"<!DOCTYPE html><title>Admin</title>LAN-DEVICE-SECRET", b"\r\n  <html>SECRET"]
)
async def test_a_web_page_in_a_cards_place_is_named_not_quoted(
    socket_enabled: None, tmp_path: Path, caplog: pytest.LogCaptureFixture, page: bytes
) -> None:
    """TotalEnergies answers a card it does not publish with a web page. What
    the page holds stays out of the message, which reaches the Repairs
    cards, the diagnostics and a sensor's attributes: only the debug log
    has it."""

    async def card(request: web.Request) -> web.Response:
        return web.Response(body=page, content_type="text/html")

    caplog.set_level(logging.DEBUG, logger=_pdf.__name__)
    async with https_site(_app(card, "/card.pdf"), tmp_path) as site:
        url = site.url("/card.pdf")
        with pytest.raises(ExtractorError) as err:
            await fetch_pdf_bytes(site.session, url)
    assert str(err.value) == f"expected a PDF at {url}, got a web page"
    assert is_missing_card_error(str(err.value))
    assert "SECRET" in caplog.text


async def test_a_payload_that_is_no_card_is_named_by_its_size(
    socket_enabled: None, tmp_path: Path
) -> None:
    answer = json.dumps({"error": "![x](https://example.com/i.png)"})

    async def card(request: web.Request) -> web.Response:
        return web.Response(text=answer, content_type="application/json")

    async with https_site(_app(card, "/card.pdf"), tmp_path) as site:
        url = site.url("/card.pdf")
        with pytest.raises(ExtractorError) as err:
            await fetch_pdf_bytes(site.session, url)
    assert str(err.value) == f"expected a PDF at {url}, got {len(answer)} bytes of something else"
    assert not is_missing_card_error(str(err.value))
    assert not is_transient_fetch_error(str(err.value))


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("PublicAccessNotPermitted", "storage error fetching {url}: PublicAccessNotPermitted"),
        # Markup is no code: the document is not quoted, and stands as a
        # page in the card's place, as any markup there does.
        ("![x](https://example.com/i.png)", "expected a PDF at {url}, got a web page"),
    ],
)
async def test_a_storage_error_is_named_by_its_code_alone(
    socket_enabled: None, tmp_path: Path, code: str, message: str
) -> None:
    """Luminus's storage answered every card with such a document from
    10 August 2026, under a 200."""
    document = f'<?xml version="1.0" encoding="utf-8"?><Error><Code>{code}</Code></Error>'

    async def card(request: web.Request) -> web.Response:
        return web.Response(text=document, content_type="application/xml")

    async with https_site(_app(card, "/card.pdf"), tmp_path) as site:
        url = site.url("/card.pdf")
        with pytest.raises(ExtractorError) as err:
            await fetch_pdf_bytes(site.session, url)
    assert str(err.value) == message.format(url=url)


async def test_an_address_in_a_message_is_escaped_and_cut(
    socket_enabled: None, tmp_path: Path
) -> None:
    """A link a page gave, markup and all, a few kB long."""
    async with https_site(web.Application(), tmp_path) as site:
        url = site.url("/files/![x](https://example.com/i.png)<img src=x>" + "a" * 4000)
        with pytest.raises(ExtractorError, match=r"^HTTP 404 fetching ") as err:
            await fetch_text(site.session, url)
    message = str(err.value)
    assert not {"[", "]", "<", ">"} & set(message)
    assert len(message) == len("HTTP 404 fetching ") + _pdf._SHOWN + len("...")


@pytest.mark.parametrize("fetch", [fetch_text, fetch_bytes, head_or_raise])
async def test_a_malformed_answer_reaches_the_message_cut(
    socket_enabled: None, tmp_path: Path, fetch: Any
) -> None:
    """aiohttp's error for a status line it cannot parse quotes the whole
    chunk the server sent. Before the cut it put 60 kB of a host's choosing
    into last_error and the log."""
    async with raw_site(malformed_status_line, tmp_path) as site:
        url = site.url("/card.pdf")
        with pytest.raises(ExtractorError, match=r"^network error fetching ") as err:
            await fetch(site.session, url)
    prefix = f"network error fetching {url}: "
    assert str(err.value).startswith(prefix)
    assert len(str(err.value)) <= len(prefix) + _pdf._SHOWN + len("...")


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        (
            "https://totalenergies.be/fr/files/Historique-valeurs-param%C3%A8tres-gaz.pdf",
            "https://totalenergies.be/fr/files/Historique-valeurs-param%C3%A8tres-gaz.pdf",
        ),
        ("https://www.energie.be/api/v1/data/document?key=Indexation", None),
        ("https://cdn.elegant.be/Current/Flex Gas_Residential.pdf", None),
        ("https://x.be/a`b`<i>[c](d)", "https://x.be/a%60b%60%3Ci%3E%5Bc%5D(d)"),
    ],
)
def test_an_address_is_quoted_as_a_url_holds_it(url: str, shown: str | None) -> None:
    assert _pdf.shown_url(url) == (shown or url.replace(" ", "%20"))


async def _card_behind(prefix: bytes, folder: Path) -> bytes:
    async def card(request: web.Request) -> web.Response:
        return web.Response(body=prefix + b"%PDF-1.7\n", content_type="application/pdf")

    async with https_site(_app(card), folder) as site:
        return await fetch_pdf_bytes(site.session, site.url("/"))


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf", b"\r\n", b"\xef\xbb\xbf\n \t"])
async def test_a_pdf_behind_a_bom_or_blank_lines_is_read_from_its_signature(
    socket_enabled: None, tmp_path: Path, prefix: bytes
) -> None:
    assert await _card_behind(prefix, tmp_path) == b"%PDF-1.7\n"


async def test_a_bom_after_a_blank_line_is_no_bom(socket_enabled: None, tmp_path: Path) -> None:
    with pytest.raises(ExtractorError, match="expected a PDF"):
        await _card_behind(b"\n\xef\xbb\xbf", tmp_path)


async def test_an_unexpected_parser_error_is_logged_and_raised_cut(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parser that trips over a page names what it read, as long as the
    page makes it: the warning and the message last_error keeps were the
    whole value, 66 kB of a supplier's markup on every failed tick."""

    async def parse() -> None:
        float("![x](https://evil.example/p.png)" * 2000)

    caplog.set_level(logging.WARNING)
    with pytest.raises(ExtractorError) as raised:
        await guarded("Frank", parse())
    assert len(str(raised.value)) < 600
    assert caplog.records
    assert all(record.exc_info is None for record in caplog.records)
    assert max(len(record.getMessage()) for record in caplog.records) < 600
