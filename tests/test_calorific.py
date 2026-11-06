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

"""Reading Atrias's monthly calorific value files."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from aiohttp import web

from custom_components.be_gas_prices import calorific
from custom_components.be_gas_prices.calorific import CalorificError, Station, parse_gcv_file
from tests.local_https import client_context, device, https_site

# The August 2026 file's shape: a byte order mark, quoted decimal commas, a
# station out of use listed at zero.
_FILE = (
    "﻿GCVMonth,ARSName,ARSEanGSRN,GCVValue\r\n"
    '2026-08,GOS FLUVIUS - AALST,541454827090000155,"11,5192589960"\r\n'
    '2026-08,GOS FLUVIUS - GENT MANUPORT,541454827090000100,"0,0000000000"\r\n'
    "2026-08,GOS FLUVIUS - GENT,541454827090000087,\r\n"
)


def test_a_station_out_of_use_or_without_a_value_is_left_out() -> None:
    assert parse_gcv_file(_FILE.encode()) == {
        Station(ean="541454827090000155", name="GOS FLUVIUS - AALST"): pytest.approx(11.519258996)
    }


def test_a_value_that_is_not_a_figure_is_a_calorific_error() -> None:
    """Not the parsers' ExtractorError, which no caller of this catches."""
    broken = _FILE + "2026-08,GOS FLUVIUS - BEVEREN,541454827090000056,n/a\r\n"
    with pytest.raises(CalorificError, match="bad value"):
        parse_gcv_file(broken.encode())


def test_a_file_that_is_no_csv_is_a_calorific_error() -> None:
    """A quote that never closes runs past the csv field limit."""
    payload = b'ARSName,ARSEanGSRN,GCVValue\n"X' + b"x" * 140_000 + b"\n"
    with pytest.raises(CalorificError, match="not CSV"):
        parse_gcv_file(payload)


async def test_a_month_file_past_the_cap_is_refused(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A month's file is 4 kB. A larger one is refused as it streams rather
    than parsed, at about 1,6 s a MiB on a Raspberry Pi."""
    big = (_FILE + '2026-08,GOS X,541454827090000001,"11,1"\r\n' * 8000).encode()

    async def answer(request: web.Request) -> web.Response:
        return web.Response(body=big)

    app = web.Application()
    app.router.add_get("/download/{path:.+}", answer)
    async with https_site(app, tmp_path) as site:
        monkeypatch.setattr(calorific, "ATRIAS_API_URL", site.url(""))
        with pytest.raises(CalorificError, match=r"\(limit 262144\)"):
            await calorific.fetch_month(site.session, client_context(), "0123abcd", "x.txt")


async def test_a_month_file_is_parsed_off_the_event_loop(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    threads: list[threading.Thread] = []
    parse = calorific.parse_gcv_file

    def parse_where(payload: bytes) -> dict[Station, float]:
        threads.append(threading.current_thread())
        return parse(payload)

    async def answer(request: web.Request) -> web.Response:
        return web.Response(body=_FILE.encode())

    app = web.Application()
    app.router.add_get("/download/{path:.+}", answer)
    monkeypatch.setattr(calorific, "parse_gcv_file", parse_where)
    async with https_site(app, tmp_path) as site:
        monkeypatch.setattr(calorific, "ATRIAS_API_URL", site.url(""))
        values = await calorific.fetch_month(site.session, client_context(), "0123abcd", "x.txt")
    assert values == {
        Station(ean="541454827090000155", name="GOS FLUVIUS - AALST"): pytest.approx(11.519258996)
    }
    assert threads and threads[0] is not threading.main_thread()


async def test_the_key_goes_in_a_header_and_stays_out_of_errors(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """aiohttp puts the request URL in some of its error messages, too many
    redirects here, a connect timeout in the field."""
    key = "0123abcd"
    listing = [{"name": "2026", "children": [{"name": "GCV202608.txt", "url": "2026/GCV.txt"}]}]
    queries: list[str] = []

    async def folder_list(request: web.Request) -> web.Response:
        queries.append(request.query_string)
        if request.headers.get("subscription-key") != key:
            raise web.HTTPUnauthorized
        return web.json_response(listing)

    async def loop(request: web.Request) -> web.Response:
        queries.append(request.query_string)
        raise web.HTTPFound(request.rel_url)

    app = web.Application()
    app.router.add_get("/folder/list", folder_list)
    app.router.add_get("/download/{path:.+}", loop)
    async with https_site(app, tmp_path) as site:
        monkeypatch.setattr(calorific, "ATRIAS_API_URL", site.url(""))
        months = await calorific.list_months(site.session, client_context(), key)
        assert months == {"2026-08": "2026/GCV.txt"}
        with pytest.raises(CalorificError, match="network error fetching") as err:
            await calorific.fetch_month(site.session, client_context(), key, months["2026-08"])
    assert key not in str(err.value)
    assert queries and all(key not in query for query in queries)


async def test_a_redirect_from_atrias_is_checked_before_it_is_followed(
    socket_enabled: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both of Atrias's hosts, its site with the key and its API, answering
    with a redirect to an appliance on the household's network."""
    async with device() as lan:

        async def away(request: web.Request) -> web.Response:
            raise web.HTTPFound(f"{lan.url}/admin/delete_all")

        app = web.Application()
        app.router.add_get("/{tail:.*}", away)
        async with https_site(app, tmp_path) as site:
            monkeypatch.setattr(calorific, "ATRIAS_CONFIG_URL", site.url("/runtime-config.js"))
            monkeypatch.setattr(calorific, "ATRIAS_API_URL", site.url(""))
            with pytest.raises(CalorificError, match="refusing to follow it"):
                await calorific.subscription_key(site.session)
            with pytest.raises(CalorificError, match="refusing to follow it"):
                await calorific.list_months(site.session, client_context(), "0123abcd")
            with pytest.raises(CalorificError, match="refusing to follow it"):
                await calorific.fetch_month(site.session, client_context(), "0123abcd", "x.txt")
    assert lan.asked == []
    assert len(site.asked) == 3
