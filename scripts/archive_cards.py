#!/usr/bin/env python3
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

"""Store today's gas tariff cards in the card archive.

Walks every registered (supplier, contract, region), fetches the card the
integration would price on right now and writes what it parsed to
``<out>/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json``, filed under the
month the card's own publication label names. A row is the snapshot exactly as
``snapshot_codec.snapshot_to_json`` writes it, which is what
``month_cards.fetch_archived_card`` reads for a month a supplier's own archive
cannot serve, plus three keys of the archive's that the reader ignores:

  - ``_seen_on``: the day the card was captured;
  - ``_sources``: every page and PDF the parse read, each with its text under
    ``<out>/texts/<YYYY-MM>/<sha256>.txt`` and, for a PDF, its digest and
    what read it: the PDF readers' versions with a digest of the render
    code, or the OCR engine's version;
  - ``_via``: ``live`` for the card that was current, ``archive`` for one
    mirrored from the supplier's own archive.

Run daily by .github/workflows/archive_cards.yml into the gas/ directory of
the be_price_cards repository, which the electricity and water integrations
share. A row is rewritten only when the parse changed, so a quiet day leaves
nothing to commit, and months more than ``--keep-months`` before this one are
removed with the texts no row names any more.

With ``--pdfs DIR`` the PDF behind every row that the archive has not recorded
yet is written to ``DIR/gas-<YYYY-MM>/<sha256>.pdf``, the month being the
card's, and the workflow uploads each directory as the assets of the release
of that name; ``<out>/pdfs.json`` then says which release holds each digest.
The digest is also what keeps a daily run cheap: a card whose bytes the
archive already holds is served its stored text instead of being rendered
again, by the same readers and render code only.

Every supplier's index publication (its ``fetch_index``) is kept as
``<out>/indices/<supplier>.json``, {index: {"YYYY-MM": EUR/MWh}}, merged into
what earlier runs kept, so a month the supplier's page stopped listing stays.

A parser fix reaches the stored months by itself. When the parser sources,
the PDF readers or the OCR engine changed since the rows were last replayed
(their digest is stamped in ``parser.txt``), or with ``--reparse``, every row
is parsed again from the texts it names, the clock pinned to the day it was
captured and no supplier contacted, and rewritten where the parse came out
differently. A card whose text other readers, render code or OCR engine made
is read again from its kept bytes, so a render fix, a reader upgrade and a
new glyph library reach them as well.

``--backfill N`` also asks every supplier that keeps an archive of its own for
the N closed months before this one, through the ``fetch_for_month`` the
integration uses, and stores each card not held yet, under the month it names
like a live row: insurance against a supplier dropping its archive.

Exits 0 when at least one card was stored or found unchanged and 1 when none
was. That is the runner's problem rather than a supplier's; the live check is
what reports the suppliers.

Usage:
    python scripts/archive_cards.py --out tmp/archive [--pdfs tmp/pdfs]
        [--only engie ...] [--backfill 12] [--reparse] [--index-only]
        [--pdf-base-url https://github.com/<owner>/be_price_cards/releases/download]
        [--archive-base-url https://github.com/<owner>/be_price_cards/blob/main/gas]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import functools
import hashlib
import json
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiohttp
from freezegun import freeze_time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# scripts/ is not a package; the lines above put it on sys.path so the render
# cache is the one the live check reads too, and the retry, the patience with a
# supplier that does not answer, the card month and the target list are the
# live check's own.
from card_texts import (  # type: ignore[import-not-found]  # noqa: E402
    PARSER_STAMP,
    ROWS,
    ROWS_GLOB,
    StoredTexts,
    engine_version,
    in_daemon_thread,
    read_stamp,
    read_text,
    readers_line,
)
from homeassistant.util import dt as dt_util  # noqa: E402
from live_check import (  # type: ignore[import-not-found]  # noqa: E402
    BRUSSELS,
    GIVE_UP_AFTER,
    Patience,
    fetch_with_retry,
    is_transient,
    label_month,
    targets,
)

from custom_components.be_gas_prices.providers import all_extractors  # noqa: E402
from custom_components.be_gas_prices.providers._pdf import (  # noqa: E402
    MIN_TEXT_LAYER_CHARS,
    extract_pdf_text,
    extract_pdf_text_layout,
    fetch_pdf_bytes,
    memoise_text_fetches,
    render_through,
)
from custom_components.be_gas_prices.providers.base import (  # noqa: E402
    CardNotReadableError,
    ExtractorError,
    IndexTable,
    SupplierExtractor,
    SupplierSnapshot,
)
from custom_components.be_gas_prices.snapshot_codec import snapshot_to_json  # noqa: E402

# This integration's namespace in the cards repository: its releases are
# gas-<YYYY-MM>, one per month of cards.
_RELEASE_PREFIX = "gas"
_TEXTS = "texts"
_INDICES = "indices"
_MANIFEST = "pdfs.json"
_COVERAGE = "coverage.md"
_COVERAGE_DIR = "coverage"
# The running month and the twelve before it, the same retention the other
# two namespaces keep.
_KEEP_MONTHS = 12
# What a parse depends on: the extractors, the shared readers and dataclasses
# beside them, the constants they key on and the codec the rows are written
# with.
_PKG = ROOT / "custom_components" / "be_gas_prices"
_PARSER_SOURCES = ("providers/*.py", "const.py", "snapshot_codec.py")
# The renderer behind each reader variant a row's card is read with, so a
# replay can render a card again. Eneco's and Luminus's own renderers read
# only their index tables, which no row names.
_RENDERERS: dict[str, Callable[[bytes], str]] = {
    "plain": extract_pdf_text,
    "layout": extract_pdf_text_layout,
}
_LEGEND = (
    "Each month links to what it was parsed from and to what came out of it: `pdf` is the",
    "card itself, in the releases of this repository, `page` the text of a page as it was",
    "read, and `json` the card as the integration parsed it. A month marked `(mirror)` was",
    "copied from the supplier's own archive rather than captured while it was current; a",
    "blank cell is a month the archive does not hold.",
)


class _RecordingMemo(dict[str, str]):
    """The text memo the fetch helpers consult, noting what one fetch touched.

    One memo spans the run so a listing page, or a card two products share, is
    downloaded and parsed once. Each row still names only what its own parse
    read, so every read and write lands in ``touched``, which the caller
    clears before each fetch. The helpers test membership and then index, so
    a hit is a read here and a miss is a write.
    """

    def __init__(self) -> None:
        super().__init__()
        self.touched: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.touched.add(key)
        return super().__getitem__(key)

    def __setitem__(self, key: str, value: str) -> None:
        self.touched.add(key)
        super().__setitem__(key, value)


def _ocr_text(payload: bytes) -> str:
    """What the OCR engine reads off a card that carries no text layer.

    The last thing tried, and only here: an installation never runs the
    engine, it reads the row this walk writes. ``ocr_price_cards`` returns
    text shaped like pdfplumber's, so the supplier's own extractor parses it
    as it would a text layer.

    Taken from the engine's trusted text: a line on which it refused a mark
    is left out, so a figure that is there was read whole and one it could
    not read is missing, which fails the parse of a mandatory figure rather
    than pricing without it. A reading that holds too little of the card is
    held to the floor a text layer is held to.
    """
    try:
        from ocr_price_cards import read_pdf
    except ImportError as err:
        raise CardNotReadableError(
            "card has no text layer and ocr_price_cards is not installed"
        ) from err
    try:
        text = str(read_pdf(payload, strict=False).trusted_text)
    except Exception as err:  # the engine's own errors, whatever they are
        raise CardNotReadableError(f"OCR could not read the card: {err}") from err
    if len(text.strip()) < MIN_TEXT_LAYER_CHARS:
        raise CardNotReadableError(
            f"OCR read only {len(text.strip())} characters it was sure of, "
            "which is too little of a card to price on"
        )
    return text


def _ocr_failure(cards: _Cards, memo: _RecordingMemo, err: BaseException) -> bool:
    """Whether a failed fetch is the OCR engine's: it could not read the
    card, or what it read of the card failed the parse. The reading may have
    been made for this fetch or served from the memo, as a card two regions
    share is."""
    if isinstance(err, CardNotReadableError):
        return True
    if is_transient(err):
        return False
    read = {(variant, digest) for variant, _url, digest, _text in cards.calls}
    for key in memo.touched:
        variant, _, url = key.partition("\0")
        if url and url in cards.digests:
            read.add((variant, cards.digests[url]))
    return any(key in cards.ocr for key in read)


class _Cards(StoredTexts):
    """The render cache, plus where the card bytes are kept.

    ``pdfs.json`` says which digests are uploaded already. Bytes the archive
    has not recorded are held until a row names them and then written under
    ``pdf_dir`` in the directory of that row's month; bytes no row names, a
    card whose parse failed, are dropped with the run.

    A card the text readers refuse as page images is read by the OCR engine.
    A stored reading is served only to the engine that made it: a new glyph
    library reads more, or reads otherwise, so its cards are read again.
    """

    def __init__(self, out: Path, pdf_dir: Path | None) -> None:
        super().__init__(out)
        engine = engine_version()
        for key, read_with in list(self.ocr.items()):
            if read_with != engine:
                self.texts.pop(key, None)
                del self.ocr[key]
        # (variant, digest) -> the readers a replayed row names on a source,
        # whose text the replay parses again. A text served or rendered in
        # this run is the installed readers'.
        self.readers: dict[tuple[str, str], str] = {}
        self.pdf_dir = pdf_dir
        manifest = _read_json(out / _MANIFEST)
        self.kept: dict[str, str] = manifest if isinstance(manifest, dict) else {}
        self.saved: dict[str, str] = {}
        self.pending: dict[str, bytes] = {}

    async def render(
        self, variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        try:
            text = await super().render(variant, url, payload, renderer)
        except CardNotReadableError:
            pass
        else:
            self.readers.pop((variant, self.digests[url]), None)
            return text
        digest = hashlib.sha256(payload).hexdigest()
        text = await in_daemon_thread(_ocr_text, payload)
        self.rendered += 1
        self.fresh[(variant, digest)] = text
        self.ocr[(variant, digest)] = engine_version()
        self.calls.append((variant, url, digest, text))
        return text

    def keep(self, digest: str, payload: bytes) -> None:
        if self.pdf_dir is None or digest in self.kept or digest in self.saved:
            return
        self.pending[digest] = payload

    def knows(self, digest: str) -> bool:
        """Whether this card is kept, or about to be: only then may a text
        that embeds it be folded down to a reference."""
        return digest in self.kept or digest in self.saved or digest in self.pending

    def file(self, month: str, digests: Iterable[str]) -> None:
        """Write the pending bytes a row read under that row's month."""
        for digest in digests:
            payload = self.pending.pop(digest, None)
            if payload is None or self.pdf_dir is None:
                continue
            rel = f"{_RELEASE_PREFIX}-{month}/{digest}.pdf"
            path = self.pdf_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.saved[digest] = rel


@dataclass
class _Summary:
    stored: int = 0
    unchanged: int = 0
    backfilled: int = 0
    absent: int = 0
    indices: int = 0
    replayed: int = 0
    reparsed: int = 0
    # Set when a kept card could not be downloaded for a replay for a reason
    # that says nothing about the card: the parser stamp then stays as it was,
    # so the next run replays again rather than never.
    download_failed: bool = False
    failed: list[str] = field(default_factory=list)
    # The failures that are the OCR engine's, a subset of ``failed``: this
    # walk is the only reader of such a card, so the workflow files them.
    ocr_failed: list[str] = field(default_factory=list)
    given_up: list[str] = field(default_factory=list)
    unreplayable: list[str] = field(default_factory=list)
    # Rows the parser now refuses outright: a misread an installation would
    # otherwise go on billing, so they are removed.
    refused: list[str] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    """A stored JSON file, or None when there is none or it does not parse."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _read_row(path: Path) -> dict[str, Any] | None:
    row = _read_json(path)
    return row if isinstance(row, dict) else None


def _dump(value: Any) -> str:
    """One key per line, sorted, so a day's diff on the archive is readable."""
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _month_id(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _months_before(today: date, months: int) -> str:
    """The month id ``months`` before ``today``'s month."""
    index = today.year * 12 + today.month - 1 - months
    return _month_id(index // 12, index % 12 + 1)


def _card_month(snapshot: SupplierSnapshot, today: date) -> str:
    """The month to file a card under: the one its label names, else today's."""
    named = label_month(snapshot.publication_label)
    return _month_id(*named) if named is not None else _month_id(today.year, today.month)


# A card handed over inside a document rather than downloaded on its own:
# OCTA+'s archive answers {"TariffSheet":"data:application/pdf;base64,..."}.
# The bytes are kept as a release asset like any other card, so storing the
# base64 as well would keep the same PDF a second time, a third larger. The
# payload is folded to a reference and put back from the kept copy when a
# replay needs it.
_EMBEDDED_CARD = re.compile(r"(data:[\w/+.-]+;base64,)([A-Za-z0-9+/=]{512,})")
_CARD_REF = re.compile(r"\{\{card:([0-9a-f]{64})\}\}")


def _fold_embedded_cards(text: str, cards: _Cards) -> str:
    """Replace every embedded card the archive keeps with a reference to it."""

    def fold(match: re.Match[str]) -> str:
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error):
            return match.group(0)
        digest = hashlib.sha256(payload).hexdigest()
        if not cards.knows(digest):
            # Nothing to put it back from later, so it stays as it came.
            return match.group(0)
        return f"{match.group(1)}{{{{card:{digest}}}}}"

    return _EMBEDDED_CARD.sub(fold, text)


async def _unfold_embedded_cards(text: str, kept_pdf: Callable[[str], Awaitable[bytes]]) -> str:
    """Put the kept cards back into a stored text, for a parse to read."""
    for digest in dict.fromkeys(_CARD_REF.findall(text)):
        payload = base64.b64encode(await kept_pdf(digest)).decode("ascii")
        text = text.replace(f"{{{{card:{digest}}}}}", payload)
    return text


def _write_text(out: Path, month: str, text: str) -> str:
    """Store ``text`` once, content-addressed, and return its path in ``out``."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rel = f"{_TEXTS}/{month}/{digest}.txt"
    path = out / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return rel


def _source_entry(key: str, text: str, cards: _Cards) -> dict[str, str]:
    """One memo entry as a source. The readers key a rendered document
    ``<variant>\\0<url>`` and a text fetch by its URL alone, query included;
    a rendered document also names the digest of its bytes."""
    variant, sep, url = key.partition("\0")
    if not sep:
        return {"url": key, "variant": "text", "text": text}
    entry = {"url": url, "variant": variant, "text": text}
    digest = cards.digests.get(url)
    if digest is not None:
        entry["pdf"] = digest
        _mark_reading(entry, cards)
    return entry


def _mark_reading(entry: dict[str, str], cards: _Cards) -> None:
    """Name what read a PDF source: the OCR engine, which is also how an
    installation learns that its card was read off an image, or the PDF
    readers, whose text is served again to the same versions and render code
    only."""
    key = (entry["variant"], entry["pdf"])
    engine = cards.ocr.get(key)
    if engine is not None:
        entry["ocr"] = engine
    else:
        entry["readers"] = cards.readers.get(key, readers_line())


def _sources_of(memo: _RecordingMemo, cards: _Cards, month: str) -> list[dict[str, str]]:
    """Everything one parse read, each text stored under ``month``: the memo
    entries it touched, and any card the render hook was handed that never
    passed through the memo."""
    out = cards.archive
    sources = [
        _source_entry(key, _write_text(out, month, _fold_embedded_cards(memo[key], cards)), cards)
        for key in sorted(memo.touched)
    ]
    named = {(s["variant"], s["url"]) for s in sources}
    for variant, url, digest, text in cards.calls:
        if (variant, url) in named:
            continue
        named.add((variant, url))
        entry = {
            "url": url,
            "variant": variant,
            "text": _write_text(out, month, text),
            "pdf": digest,
        }
        _mark_reading(entry, cards)
        sources.append(entry)
    return sorted(sources, key=lambda s: (s["variant"] != "text", s["variant"], s["url"]))


def _memo_key(source: dict[str, str]) -> str:
    """The memo key a source was read under, the inverse of _source_entry."""
    if source["variant"] == "text":
        return source["url"]
    return f"{source['variant']}\0{source['url']}"


def _pdfs_of(sources: Iterable[dict[str, str]]) -> list[str]:
    return [s["pdf"] for s in sources if "pdf" in s]


def _row(
    snapshot: SupplierSnapshot, sources: list[dict[str, str]], seen_on: date, via: str
) -> dict[str, Any]:
    row = snapshot_to_json(snapshot)
    row["_seen_on"] = seen_on.isoformat()
    row["_sources"] = sources
    row["_via"] = via
    return row


def _same_card(existing: dict[str, Any], fresh: dict[str, Any]) -> bool:
    """Whether two rows hold the same card.

    The capture day is not the card, and neither is the path of a text a
    source was read from: a page carrying a nonce gives a new text every day
    while the parse, the URL, the reader and the PDF are all the same.
    """

    def settled(row: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in row.items() if k != "_seen_on"}
        out["_sources"] = [
            {k: v for k, v in source.items() if k != "text"}
            for source in row.get("_sources", [])
            if isinstance(source, dict)
        ]
        return out

    return settled(existing) == settled(fresh)


def _write_row(path: Path, row: dict[str, Any]) -> bool:
    """Write a row unless the file already holds the same card; True when
    the file changed."""
    existing = _read_row(path)
    if existing is not None and _same_card(existing, row):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(row), encoding="utf-8")
    return True


def _merge_index(held: Any, table: IndexTable, cutoff: str) -> IndexTable:
    """What the archive held for a supplier's indices with today's values on
    top, a value the supplier revised taking its new figure, and every month
    before ``cutoff`` dropped."""
    merged: IndexTable = {}
    old = held if isinstance(held, dict) else {}
    for name in sorted(set(old) | set(table)):
        months = old.get(name)
        values = {
            **(months if isinstance(months, dict) else {}),
            **table.get(name, {}),
        }
        kept = {m: float(v) for m, v in sorted(values.items()) if m >= cutoff}
        if kept:
            merged[name] = kept
    return merged


def _write_json(path: Path, value: Any) -> bool:
    """Write ``value`` unless the file already holds it; True when it changed."""
    text = _dump(value)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _prune(out: Path, keep_months: int, today: date) -> int:
    """Remove the months more than ``keep_months`` before today's: rows,
    index values and manifest entries. Returns how many went."""
    cutoff = _months_before(today, keep_months)
    removed = 0
    for path in out.glob(ROWS_GLOB):
        if path.stem < cutoff:
            path.unlink()
            removed += 1
    for path in out.glob(f"{_INDICES}/*.json"):
        held = _read_json(path)
        # A file that cannot be read is left for a person to look at.
        if not isinstance(held, dict):
            continue
        merged = _merge_index(held, {}, cutoff)
        if merged == held:
            continue
        removed += 1
        if merged:
            _write_json(path, merged)
        else:
            path.unlink()
    kept = _read_json(out / _MANIFEST)
    if isinstance(kept, dict):
        # A release path is gas-<YYYY-MM>/<digest>.pdf, and the workflow
        # deletes the release itself on the same cutoff.
        current = {
            digest: rel
            for digest, rel in kept.items()
            if str(rel).split("/")[0].removeprefix(f"{_RELEASE_PREFIX}-") >= cutoff
        }
        if len(current) != len(kept):
            removed += len(kept) - len(current)
            _write_json(out / _MANIFEST, current)
    # Deepest first, so a contract directory emptied above goes too.
    for top in (ROWS, _TEXTS):
        for folder in sorted(
            (p for p in (out / top).rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)
        ):
            if not any(folder.iterdir()):
                folder.rmdir()
    return removed


def _drop_unnamed_texts(out: Path) -> int:
    """Remove every stored text no row names; count them.

    Everything that reads a text reaches it through a row's sources, so one no
    row names is dead weight: a page that carries a nonce is stored under a new
    digest every day while the unchanged row keeps naming the first one, a
    rewritten row leaves its old texts, and a pruned row all of its own. A row
    that cannot be read could be naming anything, so then nothing goes.
    """
    named: set[str] = set()
    for path in out.glob(ROWS_GLOB):
        row = _read_row(path)
        if row is None:
            return 0
        for source in row.get("_sources", []):
            if isinstance(source, dict) and isinstance(source.get("text"), str):
                named.add(source["text"])
    removed = 0
    for text in out.glob(f"{_TEXTS}/????-??/*.txt"):
        if text.relative_to(out).as_posix() not in named:
            text.unlink()
            removed += 1
    for folder in out.glob(f"{_TEXTS}/????-??"):
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    return removed


# ---- the coverage sheets ----------------------------------------------------


@dataclass
class _Held:
    """One stored month, as a coverage cell needs it."""

    via: str
    pdfs: list[str]
    page: str | None
    path: str


def _held_rows(out: Path) -> dict[str, dict[tuple[str, str], dict[str, _Held]]]:
    """Every row on disk by supplier, then (contract, region), then month."""
    held: dict[str, dict[tuple[str, str], dict[str, _Held]]] = {}
    for path in sorted(out.glob(ROWS_GLOB)):
        row = _read_row(path)
        if row is None:
            continue
        supplier, contract, region = path.parts[-4:-1]
        sources = [s for s in row.get("_sources", []) if isinstance(s, dict)]
        pages = [s["text"] for s in sources if s.get("variant") == "text"]
        held.setdefault(supplier, {}).setdefault((contract, region), {})[path.stem] = _Held(
            via=str(row.get("_via", "live")),
            pdfs=_pdfs_of(sources),
            page=pages[0] if pages else None,
            path=path.relative_to(out).as_posix(),
        )
    return held


def _link(label: str, base: str | None, rel: str) -> str:
    """A link into the archive, or the bare label with nowhere to link to."""
    return f"[{label}]({base}/{rel})" if base else label


def _cell(
    held: _Held | None,
    kept: dict[str, str],
    pdf_base_url: str | None,
    archive_base_url: str | None,
) -> str:
    """What a month links to: the card when one was read (once it is
    uploaded), the page it was parsed from otherwise, and the JSON the parse
    produced; a mirrored month says so."""
    if held is None:
        return ""
    uploaded = [kept[d] for d in held.pdfs if d in kept]
    parts: list[str] = []
    if uploaded and pdf_base_url:
        parts.append(f"[pdf]({pdf_base_url}/{uploaded[0]})")
    elif held.pdfs:
        parts.append("pdf")
    elif held.page is not None:
        parts.append(_link("page", archive_base_url, held.page))
    parts.append(_link("json", archive_base_url, held.path))
    if held.via == "archive":
        parts.append("(mirror)")
    return " ".join(parts)


def _write_coverage(
    out: Path, pdf_base_url: str | None = None, archive_base_url: str | None = None
) -> None:
    """Rewrite the coverage sheets from the rows on disk: one per supplier
    under ``coverage/`` and an index naming them.

    Deterministic, so a day that changed nothing rewrites them to the same
    bytes. A sheet whose supplier has no rows any more is removed.
    """
    held = _held_rows(out)
    manifest = _read_json(out / _MANIFEST)
    kept: dict[str, str] = manifest if isinstance(manifest, dict) else {}
    folder = out / _COVERAGE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    index = [
        "# Coverage",
        "",
        "One sheet per supplier, each a table with a row per contract and region and a",
        "column per month the archive holds.",
        *_LEGEND,
        "",
    ]
    for supplier, rows in sorted(held.items()):
        months = sorted({m for have in rows.values() for m in have})
        lines = [
            f"# {supplier}",
            "",
            "One row per contract and region, one column per month the archive holds.",
            *_LEGEND,
            "",
            "| contract | region | " + " | ".join(months) + " |",
            "| --- | --- | " + " | ".join("---" for _ in months) + " |",
        ]
        for (contract, region), have in sorted(rows.items()):
            cells = [_cell(have.get(m), kept, pdf_base_url, archive_base_url) for m in months]
            lines.append(f"| {contract} | {region} | " + " | ".join(cells) + " |")
        lines.append("")
        (folder / f"{supplier}.md").write_text("\n".join(lines), encoding="utf-8")
        count = f"{len(rows)} row" + ("" if len(rows) == 1 else "s")
        index.append(
            f"- [{supplier}]({_COVERAGE_DIR}/{supplier}.md): {count}, {months[0]} to {months[-1]}"
        )
    for stale in folder.glob("*.md"):
        if stale.stem not in held:
            stale.unlink()
    index.append("")
    (out / _COVERAGE).write_text("\n".join(index), encoding="utf-8")


# ---- replaying the stored rows through the current parser -------------------


def _parser_digest() -> str:
    """One digest over every source a parse depends on, and what reads the
    cards: the PDF readers and the OCR engine, so a new release of either
    replays the months the walk no longer downloads."""
    digest = hashlib.sha256(f"{readers_line()} {engine_version()}".encode())
    for pattern in _PARSER_SOURCES:
        for path in sorted(_PKG.glob(pattern)):
            digest.update(path.relative_to(_PKG).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


class _Offline:
    """The session a replayed parse runs on: it contacts nobody.

    Every text the row read is in the memo before the parse starts, so the
    readers never reach this for those. Anything else the parse asks for is
    refused as a network error, which the readers wrap the way they wrap a
    real one, and the row is left as it was and reported.
    """

    def get(self, url: str, **_kw: Any) -> Any:
        raise aiohttp.ClientConnectionError(f"a replay is offline: {url}")

    head = get
    post = get


async def _kept_pdf(
    digest: str,
    cards: _Cards,
    session: aiohttp.ClientSession,
    pdf_base_url: str | None,
) -> bytes:
    """The bytes of a card the archive keeps: from this run when it wrote
    them, else from the release that holds them."""
    if digest in cards.pending:
        return cards.pending[digest]
    if cards.pdf_dir is not None:
        local = next(cards.pdf_dir.glob(f"*/{digest}.pdf"), None)
        if local is not None:
            return local.read_bytes()
    rel = cards.kept.get(digest)
    if rel is None or pdf_base_url is None:
        raise ExtractorError(f"no kept copy of card {digest}")
    payload = await fetch_with_retry(
        functools.partial(fetch_pdf_bytes, session, f"{pdf_base_url}/{rel}", timeout=60)
    )
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ExtractorError(f"the kept copy of card {digest} does not match its digest")
    return payload


async def _replay_row(
    path: Path,
    registry: dict[str, SupplierExtractor],
    cards: _Cards,
    kept_pdf: Callable[[str], Awaitable[bytes]],
    summary: _Summary,
) -> None:
    """Parse one stored row again from the texts it names, offline, and
    rewrite it when the parse came out differently. A card whose text other
    readers, render code or OCR engine made is read again from its kept
    bytes. The caller pins the clock to the row's capture day."""
    out = cards.archive
    supplier, contract, region = path.parts[-4:-1]
    label = f"{supplier}/{contract}/{region}/{path.stem}"
    row = _read_row(path)
    extractor = registry.get(supplier)
    if row is None or extractor is None:
        summary.unreplayable.append(f"{label}: no readable row or no extractor registered")
        return
    if not any(c.id == contract and region in c.regions for c in extractor.contracts):
        # A contract the supplier withdrew, or no longer sells in the region:
        # its rows still price the months an earlier contract on it supplied,
        # so they stay as they are.
        summary.unreplayable.append(f"{label}: the contract is no longer sold there")
        return
    try:
        seen_on = date.fromisoformat(row["_seen_on"])
        sources = [s for s in row["_sources"] if isinstance(s, dict)]
        via = str(row["_via"])
    except (KeyError, TypeError, ValueError):
        summary.unreplayable.append(f"{label}: no capture day or sources")
        return
    memo = _RecordingMemo()
    readers = readers_line()
    engine = engine_version()
    rendered: set[tuple[str, str]] = set()
    for source in sources:
        text_path = out / source["text"]
        if not text_path.exists():
            summary.unreplayable.append(f"{label}: {source['text']} is missing")
            return
        renderer = _RENDERERS.get(source["variant"])
        if "pdf" not in source:
            stale = False
        elif "ocr" in source:
            stale = source["ocr"] != engine
        else:
            stale = source.get("readers") != readers
        try:
            if stale and renderer is not None:
                payload = await kept_pdf(source["pdf"])
                text = await cards.render(source["variant"], source["url"], payload, renderer)
                rendered.add((source["variant"], source["pdf"]))
            else:
                text = await _unfold_embedded_cards(read_text(text_path), kept_pdf)
        except Exception as err:  # a card not read back is a row not replayed
            summary.download_failed |= is_transient(err)
            summary.unreplayable.append(f"{label}: {type(err).__name__}: {err}")
            return
        # Seeded, not touched: only what the parse reads counts as read.
        dict.__setitem__(memo, _memo_key(source), text)
    cards.digests.update({s["url"]: s["pdf"] for s in sources if "pdf" in s})
    # A text the replay did not read again from its card goes on naming what
    # read it: an installation learns from the engine that the card was read
    # off an image.
    for source in sources:
        if "pdf" not in source:
            continue
        key = (source["variant"], source["pdf"])
        if key in rendered:
            continue
        if "ocr" in source:
            cards.ocr[key] = source["ocr"]
        else:
            cards.readers[key] = source.get("readers", "")
    cards.calls.clear()
    offline: Any = _Offline()
    with memoise_text_fetches(memo), render_through(cards.render):
        try:
            if via == "archive":
                if extractor.fetch_for_month is None:
                    summary.unreplayable.append(f"{label}: the supplier keeps no archive now")
                    return
                first = date(int(path.stem[:4]), int(path.stem[5:]), 1)
                snapshot = await extractor.fetch_for_month(offline, contract, region, first)
            else:
                snapshot = await extractor.fetch(offline, contract, region)
        except ExtractorError as err:
            if is_transient(err):
                # Something the row never read, asked of the offline
                # session: the row stays as it was.
                summary.unreplayable.append(f"{label}: {type(err).__name__}: {err}")
            else:
                path.unlink()
                summary.refused.append(f"{label}: {err}")
            return
        except Exception as err:  # reported, and the row stays as it was
            summary.unreplayable.append(f"{label}: {type(err).__name__}: {err}")
            return
    if snapshot is None or _card_month(snapshot, seen_on) != path.stem:
        path.unlink()
        summary.refused.append(f"{label}: the parse no longer gives this month's card")
        return
    summary.replayed += 1
    fresh = _row(
        snapshot, _sources_of(memo, cards, _month_id(seen_on.year, seen_on.month)), seen_on, via
    )
    cards.file(path.stem, _pdfs_of(fresh["_sources"]))
    if _write_row(path, fresh):
        summary.reparsed += 1


async def _replay_all(
    registry: dict[str, SupplierExtractor],
    cards: _Cards,
    kept_pdf: Callable[[str], Awaitable[bytes]],
    summary: _Summary,
) -> None:
    """Every stored row, grouped by capture day so the clock is pinned once
    per day rather than once per row."""
    by_day: dict[str, list[Path]] = {}
    for path in sorted(cards.archive.glob(ROWS_GLOB)):
        row = _read_row(path) or {}
        by_day.setdefault(str(row.get("_seen_on", "")), []).append(path)
    for day, paths in sorted(by_day.items()):
        try:
            date.fromisoformat(day)
        except ValueError:
            summary.unreplayable.extend(f"{path}: no capture day" for path in paths)
            continue
        # Ticking, so the loop's own timers and the render threads keep
        # working; the date stays the capture day for the seconds this takes.
        with freeze_time(f"{day}T12:00:00+02:00", tick=True):
            for path in paths:
                await _replay_row(path, registry, cards, kept_pdf, summary)


# ---- the walk -----------------------------------------------------------------


async def archive(
    out: Path,
    *,
    only: frozenset[str] = frozenset(),
    keep_months: int = _KEEP_MONTHS,
    backfill_months: int = 0,
    pdf_dir: Path | None = None,
    pdf_base_url: str | None = None,
    archive_base_url: str | None = None,
    reparse: bool = False,
    extractors: Iterable[SupplierExtractor] | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> _Summary:
    """Fetch every card, store what changed, keep the index publications,
    replay the rows if the parser changed, prune the old, report."""
    now = now or datetime.now(UTC)
    today = now.astimezone(BRUSSELS).date()
    seen_month = _month_id(today.year, today.month)
    cutoff = _months_before(today, keep_months)
    out.mkdir(parents=True, exist_ok=True)
    summary = _Summary()
    registry = tuple(all_extractors() if extractors is None else extractors)
    wanted = targets(registry, today, only)
    parser = _parser_digest()
    stamp = read_stamp(out)
    cards = _Cards(out, pdf_dir)
    memo = _RecordingMemo()
    patience = Patience()

    async def walk(ex: SupplierExtractor, fetch: Callable[[], Awaitable[Any]], label: str) -> Any:
        """One fetch, retried; None when it failed, which is recorded."""
        memo.touched.clear()
        cards.calls.clear()
        try:
            got = await fetch_with_retry(fetch, sleep=sleep)
        except Exception as err:  # one card must not stop the walk
            line = f"{label}: {type(err).__name__}: {err}"
            summary.failed.append(line)
            if _ocr_failure(cards, memo, err):
                summary.ocr_failed.append(line)
            if patience.note(ex.id, err):
                summary.given_up.append(ex.id)
            return None
        patience.ok(ex.id)
        return got

    async with aiohttp.ClientSession() as session:
        with memoise_text_fetches(memo), render_through(cards.render):
            for ex, contract, region in wanted:
                if ex.id in patience.given_up:
                    continue
                label = f"{ex.id}/{contract}/{region}"
                snapshot = await walk(
                    ex, functools.partial(ex.fetch, session, contract, region), label
                )
                if snapshot is None:
                    continue
                month = _card_month(snapshot, today)
                sources = _sources_of(memo, cards, seen_month)
                path = out / ROWS / ex.id / contract / region / f"{month}.json"
                if _write_row(path, _row(snapshot, sources, today, "live")):
                    summary.stored += 1
                else:
                    summary.unchanged += 1
                cards.file(month, _pdfs_of(sources))
            for ex, contract, region in wanted:
                if ex.fetch_for_month is None:
                    continue
                # Past the retention a month would be stored only to be pruned.
                for back in range(1, min(backfill_months, keep_months) + 1):
                    month = _months_before(today, back)
                    path = out / ROWS / ex.id / contract / region / f"{month}.json"
                    if ex.id in patience.given_up or path.exists():
                        continue
                    first = date(int(month[:4]), int(month[5:]), 1)
                    past = await walk(
                        ex,
                        functools.partial(ex.fetch_for_month, session, contract, region, first),
                        f"{ex.id}/{contract}/{region}/{month}",
                    )
                    if past is None:
                        # A month the supplier does not serve, or a failure
                        # already recorded: either way left for a later
                        # backfill.
                        summary.absent += 1
                        continue
                    # Filed under the month the card names, as a live row
                    # is: a card in force over several months (Bolt's
                    # variable ones) names the first, and a replay refuses
                    # a row whose card names another month than its file.
                    named = _card_month(past, today)
                    path = out / ROWS / ex.id / contract / region / f"{named}.json"
                    if named < cutoff or path.exists():
                        continue
                    sources = _sources_of(memo, cards, seen_month)
                    _write_row(path, _row(past, sources, today, "archive"))
                    summary.backfilled += 1
                    cards.file(named, _pdfs_of(sources))
            # Inside the memo: a supplier reading its index off its own cards
            # is served them rather than downloading them again.
            for ex in registry:
                if ex.fetch_index is None or ex.id in patience.given_up:
                    continue
                if not any(target[0] is ex for target in wanted):
                    continue
                table = await walk(ex, functools.partial(ex.fetch_index, session), f"{ex.id} index")
                if table is not None:
                    index_path = out / _INDICES / f"{ex.id}.json"
                    merged = _merge_index(_read_json(index_path), table, cutoff)
                    if merged and _write_json(index_path, merged):
                        summary.indices += 1
        # A fresh archive holds nothing older than this parser, so the first
        # run only stamps it.
        if reparse or (stamp is not None and stamp != parser):
            await _replay_all(
                {ex.id: ex for ex in registry},
                cards,
                functools.partial(
                    _kept_pdf, cards=cards, session=session, pdf_base_url=pdf_base_url
                ),
                summary,
            )
    if summary.download_failed and stamp is not None:
        # Stamping now would call the rows it missed replayed, and no later
        # run would look at them again until an unrelated parser change.
        parser = stamp
        print("a kept card could not be downloaded; the next run replays the rows again")
    (out / PARSER_STAMP).write_text(f"{parser}\n", encoding="utf-8")
    removed = _prune(out, keep_months, today) + _drop_unnamed_texts(out)
    _write_coverage(out, pdf_base_url, archive_base_url)
    print(
        f"{summary.stored} stored, {summary.unchanged} unchanged, "
        f"{summary.backfilled} backfilled, {summary.absent} absent, "
        f"{len(summary.failed)} failed, {summary.indices} index tables changed, "
        f"{removed} pruned, {len(wanted)} cards asked; {cards.rendered} rendered, "
        f"{cards.served} served from stored text, {len(cards.saved)} new PDFs kept; "
        f"{summary.replayed} replayed, {summary.reparsed} reparsed, "
        f"{len(summary.unreplayable)} not replayable, {len(summary.refused)} refused and removed"
    )
    for line in summary.failed:
        print(f"  failed {line[:300]}")
    for supplier in summary.given_up:
        print(f"  gave up on {supplier} after {GIVE_UP_AFTER} network failures in a row")
    for line in summary.unreplayable:
        print(f"  not replayable {line[:300]}")
    for line in summary.refused:
        print(f"  refused and removed {line[:300]}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="the archive directory")
    parser.add_argument("--only", action="append", default=[], help="restrict to a supplier id")
    parser.add_argument("--keep-months", type=int, default=_KEEP_MONTHS)
    parser.add_argument(
        "--pdfs",
        type=Path,
        default=None,
        metavar="DIR",
        help="write every PDF the archive has not recorded yet under DIR",
    )
    parser.add_argument(
        "--pdf-base-url",
        default=None,
        metavar="URL",
        help="where the kept PDFs are downloaded from, for the links and for a replay",
    )
    parser.add_argument(
        "--archive-base-url",
        default=None,
        metavar="URL",
        help="where the archive is browsed, for the coverage sheets' links",
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="replay every stored row through the parser even if it did not change",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="N",
        help="also mirror the N closed months before this one from the supplier archives",
    )
    parser.add_argument(
        "--ocr-failures",
        type=Path,
        default=None,
        metavar="FILE",
        help="write the cards the OCR engine could not read to FILE, one a line",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="only rewrite the coverage sheets from what is on disk; no fetch",
    )
    args = parser.parse_args()
    if args.index_only:
        # After the workflow's upload has extended the manifest, so the links
        # the walk wrote before it point at files that now exist.
        _write_coverage(args.out, args.pdf_base_url, args.archive_base_url)
        return 0
    # The providers take the month off Home Assistant's clock, which an
    # installation sets to its own zone, and a Belgian one is in Brussels.
    dt_util.set_default_time_zone(BRUSSELS)
    summary = asyncio.run(
        archive(
            args.out,
            only=frozenset(args.only),
            keep_months=args.keep_months,
            backfill_months=args.backfill,
            pdf_dir=args.pdfs,
            pdf_base_url=args.pdf_base_url,
            archive_base_url=args.archive_base_url,
            reparse=args.reparse,
        )
    )
    if args.ocr_failures is not None:
        args.ocr_failures.write_text(
            "".join(" ".join(line.split()) + "\n" for line in summary.ocr_failed),
            encoding="utf-8",
        )
    return 0 if summary.stored or summary.unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
