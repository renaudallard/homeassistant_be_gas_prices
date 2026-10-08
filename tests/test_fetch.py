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

from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

from custom_components.be_gas_prices.providers import _pdf
from custom_components.be_gas_prices.providers._pdf import fetch_pdf_bytes, fetch_text
from custom_components.be_gas_prices.providers.base import ExtractorError


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


class _Hop:
    def __init__(self, url: str) -> None:
        self.url = URL(url)


class _Redirected:
    """An answer that came through the redirects ``hops`` name."""

    status = 200
    content_length = None
    charset = None

    def __init__(self, *hops: str) -> None:
        self.history = tuple(_Hop(hop) for hop in hops[:-1])
        self.url = URL(hops[-1])

    async def __aenter__(self) -> _Redirected:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    def __init__(self, resp: _Redirected) -> None:
        self.resp = resp

    def get(self, url: str, **_kw: Any) -> _Redirected:
        return self.resp


@pytest.mark.parametrize(
    "hops",
    [
        ("https://totalenergies.be/a.pdf", "http://192.168.1.1/a.pdf"),
        ("https://a.be/x", "http://a.be/y", "https://a.be/z"),
    ],
)
@pytest.mark.parametrize("fetch", [fetch_text, fetch_pdf_bytes])
async def test_a_redirect_off_https_is_refused(fetch: Any, hops: tuple[str, ...]) -> None:
    session: Any = _Session(_Redirected(*hops))
    with pytest.raises(ExtractorError, match="redirected off https") as err:
        await fetch(session, hops[0])
    # The target stays out of what the user is shown.
    assert "192.168" not in str(err.value)


def test_a_redirect_to_another_site_over_https_is_followed() -> None:
    """TotalEnergies serves its index publication from its storage's host."""
    resp: Any = _Redirected(
        "https://totalenergies.be/fr/files/x.pdf",
        "https://cf.bewebsiteprod.alzp.tgscloud.net/s3fs-public/x.pdf",
    )
    _pdf.guard_redirect("https://totalenergies.be/fr/files/x.pdf", resp)


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
