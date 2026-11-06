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

"""Shared helpers for fetching and reading PDF tariff cards."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import pkgutil
import re
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from ssl import SSLContext
from typing import Any

import aiohttp
import pypdf
from yarl import URL

from ._parse import (
    to_float,
)
from ._pdf_render import extract_pdf_text, extract_pdf_text_layout
from .base import (
    ExtractorError,
)

_LOGGER = logging.getLogger(__name__)


# pypdf logs at WARNING what it skips or repairs while it reads a card: a
# ToUnicode entry it cannot decode (every energie.be card from November
# 2026 on), an object pointer it has to look for elsewhere, its advice to
# install fontTools, with whole font dictionaries, several times per Engie
# card. The text comes out as the tests pin it either way and nothing there
# is for the user to act on, so while a render of ours runs, its pypdf
# records go to this module's debug log instead. pypdf reads made by
# anything else in Home Assistant log as before.
_RENDERING = threading.local()


class _PypdfToDebug(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(_RENDERING, "active", False):
            return True
        _LOGGER.debug("%s: %.300s", record.name, record.getMessage())
        return False


# pypdf logs to the logger of the module that met the problem.
_TO_DEBUG = _PypdfToDebug()
for _module in pkgutil.walk_packages(pypdf.__path__, "pypdf."):
    logging.getLogger(_module.name).addFilter(_TO_DEBUG)


def _render_quietly(render: Callable[[bytes], str], payload: bytes) -> str:
    _RENDERING.active = True
    try:
        return render(payload)
    finally:
        _RENDERING.active = False


async def render_in_thread(render: Callable[[bytes], str], payload: bytes) -> str:
    """Run ``render`` on ``payload`` in a worker thread, the pypdf records
    it causes logged at debug."""
    return await asyncio.to_thread(_render_quietly, render, payload)


def _read_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


USER_AGENT = f"Home Assistant be_gas_prices/{_read_version()}"


def is_transient_fetch_error(message: str) -> bool:
    """Whether an ExtractorError message describes a transient fetch
    failure a later refresh is likely to recover, rather than a permanent
    one (parse error, 404, non-PDF payload) that needs a code fix.

    The fetch helpers in this module wrap aiohttp surface errors with three
    stable prefixes: ``network error fetching`` (timeout / reset / DNS),
    ``storage error fetching`` (the object store behind the url refused the
    read) and ``HTTP <status>``. A bare ``network error`` is always
    transient. Among HTTP statuses, 5xx plus 408 / 429 / 403 are transient
    (the Cloudflare-fronted suppliers intermittently answer an otherwise
    healthy resource with a 403 anti-bot challenge or a 429 that succeeds
    on retry); 404 / 410 mean the card was renamed or withdrawn and must
    fail fast.
    """
    if message.startswith("network error fetching"):
        return True
    if message.startswith("storage error fetching"):
        return True
    if message.startswith("HTTP "):
        head = message[len("HTTP ") :].split(None, 1)[0]
        if head.isdigit():
            status = int(head)
            return status >= 500 or status in (403, 408, 429)
    return False


# The "expected a PDF" message fetch_pdf_bytes writes when the address
# served a web page: the repr of a payload whose first byte, past any
# leading whitespace, opens a tag.
_WEB_PAGE_INSTEAD_OF_CARD = re.compile(
    r"expected a PDF at .*, payload starts with b['\"](?:\\[nrt]|\s)*<"
)


def is_missing_card_error(message: str) -> bool:
    """Whether an ExtractorError message says there is no card at the
    address.

    Permanent for :func:`is_transient_fetch_error`, since a retry will not
    bring the card back, but not a layout change either: the supplier has
    not published the month's card yet, withdrew the product or moved its
    cards. That is a 404 or a 410, read off the ``HTTP <status>`` prefix
    the fetch helpers write, or a web page served in the card's place, as
    TotalEnergies answers for a card it does not publish.
    """
    if message.startswith("HTTP "):
        return message[len("HTTP ") :].split(None, 1)[0] in ("404", "410")
    return _WEB_PAGE_INSTEAD_OF_CARD.match(message) is not None


def error_text(err: BaseException) -> str:
    """The exception's message, or its class name when it carries none.

    aiohttp raises its timeouts argless, and ``str()`` of an argless
    exception is ``""``. Interpolated into a message that ends in ``": "``
    that produced a user-facing sentence trailing off after the colon, on
    all three surfaces that show ``last_error``: the ``snapshot_stale``
    Repairs card, the ``current_price`` sensor attribute, and diagnostics.
    Naming the class is the smallest thing that stays informative: the
    caller's own prefix already says what was being attempted.
    """
    return str(err) or type(err).__name__


async def guarded[T](label: str, call: Awaitable[T]) -> T:
    """Await an extractor's ``call``, an error none raises on purpose
    turned into an ExtractorError.

    Such an error is a parser meeting a page it does not expect. Let out,
    it escaped the tick and made every entity unavailable while the held
    card could still price them. It is now failed like a card the parser
    refuses, and its traceback logged for the bug report.
    """
    try:
        return await call
    except ExtractorError:
        raise
    except Exception as err:
        _LOGGER.warning("%s: unexpected error", label, exc_info=True)
        name = type(err).__name__
        raise ExtractorError(f"{label}: {name}: {err}" if str(err) else f"{label}: {name}") from err


# 64 MiB: far above any real tariff card or page, so it never trips on a
# legitimate one while bounding what a broken or hostile server can pull
# into Home Assistant's memory in one fetch.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


async def read_capped(resp: aiohttp.ClientResponse, url: str) -> bytes:
    """Read a response body, refusing one past :data:`MAX_RESPONSE_BYTES`.

    Content-Length is only a hint: a chunked answer carries none and a
    server can lie, so the cap holds on the bytes read as well. The bytes
    counted are the decoded ones, so a compressed answer is held to it too.
    """
    declared = resp.content_length
    if declared is not None and declared > MAX_RESPONSE_BYTES:
        raise ExtractorError(
            f"refusing {url}: declared {declared} bytes (limit {MAX_RESPONSE_BYTES})"
        )
    payload = bytearray()
    async for chunk in resp.content.iter_chunked(65536):
        payload.extend(chunk)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ExtractorError(f"refusing {url}: more than {MAX_RESPONSE_BYTES} bytes")
    return bytes(payload)


async def read_text_capped(resp: aiohttp.ClientResponse, url: str) -> str:
    """Read a text body under the cap, in the charset it declares or UTF-8.

    Not strict: a page in another charset than it declares (a maintenance
    page in Latin-1) is then refused by its parser as an ExtractorError
    rather than raised as a UnicodeDecodeError.
    """
    payload = await read_capped(resp, url)
    try:
        return payload.decode(resp.charset or "utf-8", errors="replace")
    except (LookupError, UnicodeError):
        # A charset Python does not know ("utf8mb4"), or a codec with no
        # replace handler (idna).
        return payload.decode("utf-8", errors="replace")


# Redirects followed in a row at most. On 6 November 2026 none of the 200
# addresses the integration reads took more than one: TotalEnergies and
# energie.be send their index publications to their storage, OCTA+ its short
# link to the file, and a few sites their root to a language folder.
MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# NAT64's well-known prefix: a translator on the host's network carries an
# address under it to the IPv4 address in its last 32 bits.
_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address ``host`` spells, or None for a name.

    Every spelling a resolver reads as an address counts: getaddrinfo reads
    "127.1", "0x7f000001", "2130706433" and "0177.0.0.1" as 127.0.0.1, as
    inet_aton does, and a trailing dot or an IPv6 zone changes nothing.
    """
    bare = host.rstrip(".").partition("%")[0]
    try:
        return ipaddress.ip_address(bare)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(bare))
    except OSError:
        return None


def _is_internal(host: str) -> bool:
    """Whether ``host`` is the Home Assistant host itself or an address only
    it can reach: a private, loopback, link-local, shared, reserved,
    multicast or unspecified one, an IPv4 one written as IPv6 included.

    Any other name is taken as public: one that resolves to such an address
    is left to TLS, since a device on the household's network has no
    certificate for a public name that the client trusts.
    """
    name = host.rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    address = _ip_literal(name)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            # A dual-stack socket connects to ::ffff:a.b.c.d over IPv4.
            address = address.ipv4_mapped
        elif address in _NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address.is_site_local:
            return True
    # is_global is false for the private, loopback, link-local and
    # unspecified ranges and for 100.64.0.0/10, which carriers and Tailscale
    # hand out, but true for multicast and IPv6's reserved ranges.
    return not address.is_global or address.is_multicast or address.is_reserved


def _refusal(url: URL) -> str | None:
    """Why ``url`` is not requested, or None when it may be."""
    if url.scheme != "https" or not url.raw_host:
        return "not an https address"
    if _is_internal(url.raw_host):
        return "not a public address"
    return None


def on_site(url: str, site: str) -> bool:
    """Whether ``url`` is an address on ``site``, an origin such as
    ``https://www.brusol.be``: its scheme, host and port, and no user.

    For the links an extractor reads off a page, which name a card on the
    supplier's own site. A link to another host is not that card, and
    taking it would let whoever wrote the page choose what Home Assistant
    asks.
    """
    return url.startswith(f"{site}/")


def _redirect_target(url: str, answer: URL, location: str) -> URL:
    """Where a redirect from ``answer``, on the way from ``url``, points,
    once it is an address the integration requests.

    The target can be a LAN appliance or a captive portal, so it goes to the
    debug log only, not to last_error or the diagnostics.
    """
    try:
        target: URL | None = answer.join(URL(location))
    except ValueError:
        target = None
    reason = "not a valid address" if target is None else _refusal(target)
    if target is None or reason is not None:
        _LOGGER.debug("%s redirected to %s", url, location)
        raise ExtractorError(f"{url} redirected; refusing to follow it: the target is {reason}")
    return target


@asynccontextmanager
async def checked_request(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float,
    head: bool = False,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    ssl: SSLContext | bool = True,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """GET ``url``, or HEAD it, and hand over the answer once it is not a
    redirect. Every fetch the integration makes goes through here.

    ``url`` is held to https and to a public host before it is asked. Most
    addresses are links read off a supplier's page, and a page that linked
    a plain http address, or one only the Home Assistant host can reach,
    would otherwise send Home Assistant there. Every card, page and API the
    integration reads is served over https, so this refuses nothing real.

    aiohttp follows a redirect before its caller sees the answer, so the
    request to wherever it pointed, an address only the Home Assistant host
    can reach included, with the path and query the redirect wrote, was on
    its way before a check on the answer could refuse it. Redirects are
    followed here instead, at most :data:`MAX_REDIRECTS` in a row, and each
    target is held to the same rules before it is requested. A redirect may
    leave the site: energie.be's document key answers with its storage on
    blob.core.windows.net, TotalEnergies' index publication with its CDN.

    ``timeout`` bounds the whole exchange, every redirect and the read of
    the body included, as aiohttp's total timeout did. ``params`` goes with
    the first request only, and the User-Agent with every one.
    """
    try:
        reason = _refusal(URL(url))
    except ValueError:
        # Not a URL yarl reads: aiohttp refuses it below with its own error.
        reason = None
    if reason is not None:
        raise ExtractorError(f"refusing {url}: {reason}")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    send = session.head if head else session.get
    target: str | URL = url
    redirects: list[aiohttp.ClientResponse] = []
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            # A total of zero would be no timeout at all to aiohttp.
            raise TimeoutError
        async with send(
            target,
            params=None if redirects else params,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=aiohttp.ClientTimeout(total=remaining),
            allow_redirects=False,
            ssl=ssl,
        ) as resp:
            location = (
                resp.headers.get(aiohttp.hdrs.LOCATION)
                if resp.status in _REDIRECT_STATUSES
                else None
            )
            if not location:
                # No redirect, or one with no Location or an empty one,
                # which aiohttp also hands over as the answer.
                yield resp
                return
            redirects.append(resp)
            if len(redirects) > MAX_REDIRECTS:
                raise aiohttp.TooManyRedirects(redirects[0].request_info, tuple(redirects))
            target = _redirect_target(url, resp.url, location)


def strip_pdf_prefix(payload: bytes) -> bytes:
    """The bytes without a UTF-8 BOM, then blank space, ahead of ``%PDF``.

    Luminus's pricelist API prepends the BOM, and some servers a newline.
    Either has to come off rather than merely be tolerated: pdfplumber
    fails such a file with "No /Root object! - Is this really a PDF?",
    which reads like a corrupt card rather than a few stray bytes. pypdf
    recovers on its own, so the pdfplumber readers are the ones this
    protects.
    """
    return payload.removeprefix(b"\xef\xbb\xbf").lstrip(b"\r\n\t ")


def is_pdf_payload(payload: bytes) -> bool:
    """Return True if the bytes look like a PDF: ``%PDF`` once
    :func:`strip_pdf_prefix` has taken off what may precede it."""
    return strip_pdf_prefix(payload).startswith(b"%PDF")


# An object store that refuses the read answers the proxy in front of it
# with its own XML error document, and the proxy passes that through as a
# 200. Azure Blob and S3 both shape it as a root <Error> carrying a <Code>.
_STORAGE_ERROR_RE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]*\?>\s*)?<Error>\s*<Code>([^<]{1,64})</Code>",
    re.IGNORECASE,
)


def _storage_error_code(payload: bytes) -> str | None:
    """The error code if ``payload`` is an object-store error document.

    Luminus switched anonymous access off on the storage account behind
    ``api-next/get-pricelist``, which serves its gas cards too, on
    2026-08-10, and every card came back as a 248-byte
    ``PublicAccessNotPermitted`` document under a 200. Read as a plain
    non-PDF payload that is a permanent parse failure asking the user to
    report a layout change that had not happened. Nothing here could fix
    it, which is the definition of transient in this taxonomy.
    """
    match = _STORAGE_ERROR_RE.match(payload)
    if match is None:
        return None
    return match.group(1).decode("ascii", "replace").strip() or None


async def fetch_bytes(session: aiohttp.ClientSession, url: str, *, timeout: int = 30) -> bytes:
    """GET ``url`` and return its body, capped as any download is.

    Catches TimeoutError alongside ClientError: aiohttp's ClientTimeout
    fires asyncio.TimeoutError (== builtins.TimeoutError on 3.11+), which is
    NOT a ClientError subclass, so a slow endpoint would otherwise bubble a
    bare TimeoutError out of discover/fetch and crash the live-check. The
    ``network error fetching`` prefix is load-bearing:
    :func:`is_transient_fetch_error` keys on it to decide whether to retry.
    """
    try:
        async with checked_request(session, url, timeout=timeout) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            return await read_capped(resp, url)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {error_text(err)}") from err


async def fetch_pdf_bytes(session: aiohttp.ClientSession, url: str, *, timeout: int = 30) -> bytes:
    """Download ``url`` and return its bytes once validated as a PDF.

    Shared by the three ``fetch_pdf_text*`` variants.
    """
    payload = await fetch_bytes(session, url, timeout=timeout)
    if not is_pdf_payload(payload):
        # An object store refusing the read is the one non-PDF payload no
        # code change can fix, so it gets its own prefix and lands on the
        # transient side of is_transient_fetch_error rather than asking
        # the user to report a layout change.
        code = _storage_error_code(payload)
        if code is not None:
            raise ExtractorError(f"storage error fetching {url}: {code}")
        # Some CDNs return 200 + text/html for a missing PDF (a 404
        # disguised as success, as TotalEnergies does for a card it does not
        # publish), and Engie's API serves valid ones as octet-stream, so
        # the magic bytes are more reliable than the Content-Type header.
        raise ExtractorError(f"expected a PDF at {url}, payload starts with {payload[:80]!r}")
    return strip_pdf_prefix(payload)


async def fetch_pdf_rendered(
    session: aiohttp.ClientSession,
    url: str,
    *,
    variant: str,
    timeout: int,
    render: Callable[[bytes], str],
) -> str:
    """Download ``url`` and extract its text, or serve both from the memo.

    The ``fetch_pdf_text*`` readers, and an extractor's own pdfplumber
    render, differ only in ``render``, so the memo lookup lives here once.
    Keyed by variant as well as URL: the same document read plain and read
    layout-preserving are different strings, and handing one back for the
    other would feed a parser a shape it has no regexes for.

    Extraction is offloaded to a worker thread: every variant is pure-Python
    parsing, and a multi-page tariff card would otherwise stall Home
    Assistant's event loop.
    """
    memo = _TEXT_MEMO.get()
    key = f"{variant}\0{url}"
    if memo is not None and key in memo:
        return memo[key]
    payload = await fetch_pdf_bytes(session, url, timeout=timeout)
    text = await render_pdf(variant, url, payload, render)
    if memo is not None:
        memo[key] = text
    return text


async def render_pdf(variant: str, url: str, payload: bytes, render: Callable[[bytes], str]) -> str:
    """Turn validated PDF bytes into text, through the render hook when one
    is installed and in a worker thread otherwise.

    The readers call it after their download. A provider that receives a
    card some other way calls it too, such as OCTA+'s archive, which hands
    the card over base64 inside a JSON answer: rendering those bytes
    directly would keep the card archiver, which listens on the hook, from
    ever seeing them. ``url`` is whatever names the card for that provider.
    """
    hook = _RENDER_HOOK.get()
    if hook is None:
        return await render_in_thread(render, payload)
    return await hook(variant, url, payload, render)


async def fetch_pdf_text(session: aiohttp.ClientSession, url: str, *, timeout: int = 30) -> str:
    """Download ``url`` and return the concatenated extracted text."""
    return await fetch_pdf_rendered(
        session, url, variant="plain", timeout=timeout, render=extract_pdf_text
    )


async def fetch_pdf_text_layout(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> str:
    """Layout-preserving variant of :func:`fetch_pdf_text`.

    Some CDNs return HTTP 200 with ``text/html`` for missing PDFs (404
    pages disguised as success). We treat those as fetch failures so the
    parser never tries to read a PDF that isn't.
    """
    return await fetch_pdf_rendered(
        session, url, variant="layout", timeout=timeout, render=extract_pdf_text_layout
    )


async def head_freshness_key(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefer: tuple[str, ...] = ("Last-Modified", "ETag"),
) -> str | None:
    """HEAD ``url`` and return the first present header from ``prefer``.

    Used as a cheap freshness probe by suppliers whose tariff cards live
    behind a CDN that honours ``If-Modified-Since`` / ``If-None-Match``.
    Returns ``None`` on any 4xx/5xx, network error, or when none of the
    preferred headers are populated; the coordinator treats ``None`` as
    "no signal" and falls back to its time-based TTL.

    A caller whose server flips ``Last-Modified`` on every CDN edge but
    keeps a stable ETag passes ``prefer`` the other way round.
    """
    try:
        async with checked_request(session, url, timeout=10, head=True) as resp:
            if resp.status >= 400:
                return None
            for key in prefer:
                value = resp.headers.get(key)
                if value:
                    return value
            return None
    except (aiohttp.ClientError, TimeoutError, ExtractorError):
        # aiohttp's ClientTimeout fires asyncio.TimeoutError (==
        # builtins.TimeoutError on 3.11+), which is NOT a ClientError;
        # without this a slow HEAD would break the documented
        # None-on-failure contract and bubble out of the probe path. A
        # redirect checked_request refuses is no signal either.
        return None


async def head_or_raise(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 10
) -> str | None:
    """HEAD ``url`` and return its ``Last-Modified``, or None when it sends
    none, raising the way the fetch helpers do when it fails.

    For a lookup that asks for names before it downloads one. ``HTTP
    <status>`` at 400 and above and ``network error fetching`` on a client
    error or a timeout let :func:`is_missing_card_error` tell a name nobody
    uploaded from a supplier that is down, where :func:`head_freshness_key`
    answers None to both.
    """
    try:
        async with checked_request(session, url, timeout=timeout, head=True) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            return resp.headers.get("Last-Modified")
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {error_text(err)}") from err


def printed_vat_rate(text: str, *patterns: str | re.Pattern[str]) -> float | None:
    """The VAT rate a card states, as a fraction, or ``None`` where none of
    ``patterns`` matches.

    For a parser that records the rate on ``TaxOverlay.card_vat_rate``.
    Group 1 of the first pattern that matches is the percentage.
    """
    for pattern in patterns:
        match = re.search(pattern, text) if isinstance(pattern, str) else pattern.search(text)
        if match:
            return to_float(match.group(1)) / 100.0
    return None


# A caller that will read the same document many times in quick succession can
# ask fetch_text and the PDF readers to serve repeats from memory. Off by
# default: every existing caller wants a live read, and a global time-based
# cache would quietly hand a coordinator tick a stale page. This is opt-in,
# explicit, and scoped to the block that entered it.
_TEXT_MEMO: ContextVar[dict[str, str] | None] = ContextVar("_TEXT_MEMO", default=None)


@contextmanager
def memoise_text_fetches(store: dict[str, str]) -> Iterator[None]:
    """Serve repeat reads of one URL from ``store`` inside this block.

    Two kinds of repeat, both of them a sweep pricing several products of one
    supplier:

    Listing pages. A provider that resolves a per-supplier listing inside
    ``fetch()`` and then picks one product out of it would re-download the
    same page once per contract when a whole supplier is priced.

    Tariff cards. Where two products share one document (EBEM prints both
    its gas products on one card) the sweep would parse it twice, and the
    parse is the expensive half, not the download: pdfplumber takes tens of
    seconds on a large card on a Raspberry Pi against well under a second
    to fetch it.

    What is memoised is the EXTRACTED TEXT, not the bytes. It is the parse
    that is worth skipping, and a card's text is a few kB against a couple of
    MB of payload, so a whole sweep's worth stays small enough to hold.

    ``store`` is passed in rather than created here so a caller can share one
    memo across several tasks - an ``asyncio.Task`` copies the context at
    creation, which copies the reference and not the dict, so every candidate
    in a sweep sees what the first one fetched.

    Deliberately not a TTL cache inside the fetchers: the coordinator and the
    one-off quote both want a live read, and the failure mode of guessing a
    TTL for them is a stale card nobody asked for.
    """
    token = _TEXT_MEMO.set(store)
    try:
        yield
    finally:
        _TEXT_MEMO.reset(token)


# How a downloaded card becomes text, when someone other than the readers
# wants a say. The card archiver (scripts/archive_cards.py) downloads every
# card daily but keys the render on the bytes' hash, so a card that has not
# changed since it was last stored is neither rendered nor parsed again, and
# it keeps the bytes it has not seen before. None, the default everywhere in
# Home Assistant, renders in a worker thread as always.
RenderHook = Callable[[str, str, bytes, Callable[[bytes], str]], Awaitable[str]]
_RENDER_HOOK: ContextVar[RenderHook | None] = ContextVar("_RENDER_HOOK", default=None)


@contextmanager
def render_through(hook: RenderHook) -> Iterator[None]:
    """Route every PDF render inside this block through ``hook``.

    The hook receives the reader variant, the URL, the validated bytes and
    the renderer the reader would have used, and returns the text. Scoped
    to the block, like the text memo above, so nothing in the integration
    itself can end up on this path.
    """
    token = _RENDER_HOOK.set(hook)
    try:
        yield
    finally:
        _RENDER_HOOK.reset(token)


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 20,
) -> str:
    """GET ``url`` and return the response body as text.

    Raises :class:`ExtractorError` on any HTTP non-2xx, network error,
    or aiohttp client failure. Use for HTML listing / index pages and
    other plain-text sources; reach for :func:`fetch_pdf_text` (or its
    layout variant) when the body is expected to be a PDF.

    ``params`` is passed straight to :meth:`aiohttp.ClientSession.get`
    for endpoints that carry their query in the URL string (a CMS query,
    an API's filters).

    Callers that prefer a soft None-on-failure can wrap this in a
    ``try / except ExtractorError`` block; concentrating the network /
    HTTP error handling here keeps the ~6 lines of boilerplate out of
    every provider, and routes every fetch through the one
    :func:`is_transient_fetch_error` taxonomy so transient failures
    retry uniformly.
    """
    memo = _TEXT_MEMO.get()
    # Keyed on the full request, not the bare URL: one endpoint answers
    # different questions by query string.
    memo_key = url if not params else f"{url}?{sorted(params.items())}"
    if memo is not None and memo_key in memo:
        return memo[memo_key]
    try:
        async with checked_request(session, url, timeout=timeout, params=params) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            body = await read_text_capped(resp, url)
            # Only a success is memoised. A failure is re-attempted by the
            # next caller, which is what the negative cache one layer up is
            # for; caching it here would give it a second, untracked lifetime.
            if memo is not None:
                memo[memo_key] = body
            return body
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {error_text(err)}") from err


def parse_json(body: str, what: str) -> Any:
    """``body`` decoded, or an :class:`ExtractorError` naming ``what``. Its
    shape is the caller's to check."""
    try:
        return json.loads(body)
    except ValueError as err:
        raise ExtractorError(f"{what} is not JSON: {err}") from err


# Full month names in calendar order (index 0 == January). The single
# source of truth for the per-supplier archive-validity checks, which
# match a card's spelled-out month against ``month_names[month - 1]``.
# Suppliers import the tuple for their card's language rather than
# re-listing the twelve names (and drifting on accents); dict-shaped
# lookups derive from these with ``enumerate(.., 1)``.
NL_MONTHS: tuple[str, ...] = (
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
)


# Month names as cards print them, lowercase, to their number: Dutch, French
# with and without its accents, and the English names that differ from both.
MONTH_NAMES: dict[str, int] = {
    **{name: number for number, name in enumerate(NL_MONTHS, 1)},
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
    # April, September, November and December are spelt as in Dutch.
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
}
