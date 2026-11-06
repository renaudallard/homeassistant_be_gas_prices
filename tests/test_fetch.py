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
from pathlib import Path
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.be_gas_prices import month_cards
from custom_components.be_gas_prices.providers import _pdf
from custom_components.be_gas_prices.providers._pdf import (
    fetch_bytes,
    fetch_pdf_bytes,
    fetch_text,
    head_freshness_key,
    head_or_raise,
    is_missing_card_error,
    is_transient_fetch_error,
)
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests.local_https import device, https_site


async def test_a_page_in_another_charset_is_read_not_raised(socket_enabled: None) -> None:
    """A maintenance page in Latin-1 sent without a charset: the decode
    must not escape as anything but text the parser then refuses."""

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            body="Maintenance planifiée".encode("latin-1"), content_type="text/html"
        )

    app = web.Application()
    app.router.add_get("/", page)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        text = await fetch_text(session, str(server.make_url("/")))
    assert text.startswith("Maintenance planifi")


async def _serve(handler: Any, path: str = "/") -> tuple[TestServer, str]:
    app = web.Application()
    app.router.add_get(path, handler)
    server = TestServer(app)
    await server.start_server()
    return server, str(server.make_url(path))


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


@pytest.mark.parametrize("fetch", [fetch_text, fetch_pdf_bytes])
async def test_a_body_past_the_cap_is_refused_as_it_streams(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, fetch: Any
) -> None:
    monkeypatch.setattr(_pdf, "MAX_RESPONSE_BYTES", 4096)
    server, url = await _serve(_streamed)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ExtractorError, match="more than 4096 bytes"):
                await fetch(session, url)
    finally:
        await server.close()


async def test_a_body_declared_past_the_cap_is_refused_unread(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_pdf, "MAX_RESPONSE_BYTES", 4096)

    async def large(request: web.Request) -> web.Response:
        return web.Response(body=b"%PDF" + b" " * 8192, content_type="application/pdf")

    server, url = await _serve(large)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(ExtractorError, match="declared 8196 bytes"):
                await fetch_pdf_bytes(session, url)
    finally:
        await server.close()


async def test_a_body_under_the_cap_is_read_whole(socket_enabled: None) -> None:
    server, url = await _serve(_streamed)
    try:
        async with aiohttp.ClientSession() as session:
            payload = await fetch_pdf_bytes(session, url)
    finally:
        await server.close()
    assert payload.startswith(b"%PDF-1.7") and len(payload) == 9 + 8 * 1024


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
    socket_enabled: None,
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
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        url = str(server.make_url("/card.pdf"))
        assert await head_or_raise(session, url) == "Thu, 01 Oct 2026 07:34:44 GMT"
        with pytest.raises(ExtractorError) as missing:
            await head_or_raise(session, str(server.make_url("/card_0.pdf")))
        with pytest.raises(ExtractorError) as down:
            await head_or_raise(session, str(server.make_url("/busy.pdf")))
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ExtractorError) as gone:
            await head_or_raise(session, url)
    assert is_missing_card_error(str(missing.value))
    assert not is_transient_fetch_error(str(missing.value))
    assert is_transient_fetch_error(str(down.value))
    assert not is_missing_card_error(str(down.value))
    # The server has closed.
    assert str(gone.value).startswith("network error fetching")


async def _card_behind(prefix: bytes) -> bytes:
    async def card(request: web.Request) -> web.Response:
        return web.Response(body=prefix + b"%PDF-1.7\n", content_type="application/pdf")

    server, url = await _serve(card)
    try:
        async with aiohttp.ClientSession() as session:
            return await fetch_pdf_bytes(session, url)
    finally:
        await server.close()


@pytest.mark.parametrize("prefix", [b"", b"\xef\xbb\xbf", b"\r\n", b"\xef\xbb\xbf\n \t"])
async def test_a_pdf_behind_a_bom_or_blank_lines_is_read_from_its_signature(
    socket_enabled: None, prefix: bytes
) -> None:
    assert await _card_behind(prefix) == b"%PDF-1.7\n"


async def test_a_bom_after_a_blank_line_is_no_bom(socket_enabled: None) -> None:
    with pytest.raises(ExtractorError, match="expected a PDF"):
        await _card_behind(b"\n\xef\xbb\xbf")
