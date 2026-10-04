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

"""The card archive's texts as a render cache.

A card's bytes hash to a digest, and every row of the card archive names, per
reader variant, the text that digest rendered to. Installed as the readers'
render hook (``providers/_pdf.render_through``), a downloaded card whose
bytes are already known is served that text instead of being rendered again.
The download still happens, so a card that changed is always seen; only the
render, which is most of a walk's time, is skipped when nothing changed.

Shared by the card archiver, which also keeps the bytes it has not seen, and
the live check, which reads a checkout of the archive.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

# The rows sit under cards/ of the archive, one file per supplier, contract,
# region and month.
ROWS = "cards"
ROWS_GLOB = f"{ROWS}/*/*/*/????-??.json"
# The digest of the parser sources the rows were last replayed with.
PARSER_STAMP = "parser.txt"
# The two PDF text readers. pdfplumber pins the pdfminer.six it runs on, so
# its version stands for both. Their versions are recorded on each source
# they rendered, like the OCR engine's.
_READERS = ("pypdf", "pdfplumber")
# The code that drives them: every text a row stores was rendered by
# extract_pdf_text or extract_pdf_text_layout. The extractors' own renderers
# (Eneco's and Luminus's index tables) feed only the index publications,
# which keep no text.
_RENDER_CODE = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "be_gas_prices"
    / "providers"
    / "_pdf.py"
)
# The engine that reads a card published as page images. Its version is
# recorded on each source it read rather than in the stamp, so a new engine
# reads those cards again and leaves every other card's text alone.
OCR_ENGINE = "ocr-price-cards"


def _version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "absent"


def engine_version() -> str:
    """The OCR engine's version, with the commit pip recorded when it was
    installed from git: it is installed from its main branch, where a new
    glyph library does not move the version number. ``absent`` when it is
    not installed."""
    version = _version(OCR_ENGINE)
    if version == "absent":
        return version
    try:
        direct = importlib.metadata.distribution(OCR_ENGINE).read_text("direct_url.json")
        commit = json.loads(direct or "{}").get("vcs_info", {}).get("commit_id")
    except (ValueError, AttributeError):
        commit = None
    return f"{version}+{commit[:12]}" if isinstance(commit, str) else version


def render_digest() -> str:
    """One digest over what turns a card's bytes into text: the render code
    and the reader versions. Also what the tests key their fixture texts on."""
    digest = hashlib.sha256(_RENDER_CODE.read_bytes())
    for name in _READERS:
        digest.update(f"{name} {_version(name)}".encode())
    return digest.hexdigest()


def readers_line() -> str:
    """The reader versions a render runs on and a digest of the code that
    drives them, as a source records them."""
    versions = " ".join(f"{name}=={_version(name)}" for name in _READERS)
    return f"{versions} render={render_digest()[:16]}"


def read_stamp(archive: Path) -> str | None:
    """The parser digest of the archive's stamp, or None for an archive that
    was never stamped."""
    path = archive / PARSER_STAMP
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    return lines[0].strip() if lines else ""


# The name of a render thread, so the tests can wait for one to wind down.
RENDER_THREAD = "card-render"


async def in_daemon_thread[T](func: Callable[[bytes], T], payload: bytes) -> T:
    """``func(payload)`` in a daemon thread of its own.

    The scripts bound each card with a timeout, which abandons a render that
    never returns but cannot stop its thread. asyncio.to_thread's workers
    are then waited for when the event loop closes and again at exit, so a
    single hung render held the job until its own timeout, and a live check
    killed there filed nothing. A daemon thread holds neither.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def settle(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)

    def run() -> None:
        try:
            outcome: tuple[Any, BaseException | None] = (func(payload), None)
        except BaseException as err:  # handed to the awaiting coroutine
            outcome = (None, err)
        # A loop that has closed has nobody waiting for this render any more.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(settle, *outcome)

    threading.Thread(target=run, name=RENDER_THREAD, daemon=True).start()
    return await future


def read_text(path: Path) -> str:
    """A stored text exactly as it was read: no newline translation, so a
    replay hands the parser the very string it parsed the first time."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


class StoredTexts:
    """What the archive already knows about card bytes."""

    def __init__(self, archive: Path) -> None:
        self.archive = archive
        # (variant, digest) -> text path in the archive, from every row.
        self.texts: dict[tuple[str, str], str] = {}
        # (variant, digest) -> the engine version, for the texts the OCR
        # engine read off a card published as page images.
        self.ocr: dict[tuple[str, str], str] = {}
        readers = readers_line()
        for row in archive.glob(ROWS_GLOB):
            try:
                sources = json.loads(row.read_text(encoding="utf-8")).get("_sources", [])
            except (ValueError, AttributeError):
                continue
            for source in sources:
                if not (isinstance(source, dict) and "pdf" in source):
                    continue
                key = (source["variant"], source["pdf"])
                if isinstance(source.get("ocr"), str):
                    self.ocr[key] = source["ocr"]
                elif source.get("readers") != readers:
                    # A text is served only to the readers and render code
                    # that rendered it. A pypdf or pdfplumber release can lay
                    # a card out differently (6.16 and 6.18 did on 2026-09-13
                    # in the electricity archive), as can a fix to the render
                    # code, and a stored text would stand in for them, hiding
                    # what they make of the card for as long as its bytes
                    # stay the same.
                    continue
                self.texts[key] = source["text"]
        # What this run rendered, so a second card on the same bytes is
        # served too.
        self.fresh: dict[tuple[str, str], str] = {}
        # url -> digest of the card last seen behind it.
        self.digests: dict[str, str] = {}
        # Every card handed to the hook, in order, as (variant, url, digest,
        # text). A provider that gets a card some other way than through a
        # reader (OCTA+'s archive, base64 inside JSON) leaves nothing in the
        # text memo, and this is how the archiver still learns what it read.
        # Cleared per fetch by the caller.
        self.calls: list[tuple[str, str, str, str]] = []
        self.rendered = 0
        self.served = 0

    def keep(self, digest: str, payload: bytes) -> None:
        """Called once per card handed over; the archiver keeps unseen bytes."""

    async def render(
        self, variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.digests[url] = digest
        self.keep(digest, payload)
        key = (variant, digest)
        text = self.fresh.get(key)
        if text is None:
            stored = self.texts.get(key)
            if stored is not None and (self.archive / stored).exists():
                text = read_text(self.archive / stored)
        if text is not None:
            self.served += 1
        else:
            text = await in_daemon_thread(renderer, payload)
            self.rendered += 1
            self.fresh[key] = text
        self.calls.append((variant, url, digest, text))
        return text
