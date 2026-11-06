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

"""Turning a tariff card's bytes into text.

Every stored text of the card archive was rendered by one of these readers,
and each is stamped with a digest of this module and of the decode bounds
(``scripts/card_texts.py``), so a change here renders every kept card again.
Downloading, validating and memoising a card stay in ``_pdf.py``, where a
change renders nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

import pypdf

from ._pdf_bound import bounded_pypdf
from ._pdf_bound_pdfminer import bounded_pdfminer
from .base import CardNotReadableError, ExtractorError

_LOGGER = logging.getLogger(__name__)


# A tariff card that carries a text layer is never anywhere near this small:
# across the 152 gas cards collected while building this integration the least
# texty readable one holds 2049 characters (Sparki), while Ecofix's rasterized
# cards yield 60. Anything in between separates the two cleanly, and 600
# leaves a wide margin on both sides. Raising here only changes WHICH error the user is
# shown: a card with no text layer was already going to fail its parse.
MIN_TEXT_LAYER_CHARS = 600


def _require_text_layer(text: str, pages: int) -> None:
    """Raise :class:`CardNotReadableError` for an image-only PDF."""
    if pages and len(text.strip()) < MIN_TEXT_LAYER_CHARS:
        raise CardNotReadableError(
            f"card has no text layer: {len(text.strip())} characters across "
            f"{pages} page(s), so it is published as page images"
        )


def extract_pdf_text(payload: bytes) -> str:
    try:
        chunks: list[str] = []
        failures = 0
        # Held to the bound from opening the file, which inflates the
        # cross-reference and object streams.
        with bounded_pypdf():
            pages = list(pypdf.PdfReader(BytesIO(payload)).pages)
            for idx, page in enumerate(pages):
                text = page.extract_text()
                if text is None:
                    # pypdf returns None when a page cannot be decoded (e.g.
                    # an unsupported font). The caller would otherwise see a
                    # corrupt snapshot with regex misses on whatever was on
                    # that page; log so the failure is visible in HA logs.
                    _LOGGER.warning("pypdf returned None for page %d/%d", idx + 1, len(pages))
                    failures += 1
                    continue
                chunks.append(text)
        if pages and failures == len(pages):
            raise ExtractorError("PDF parse error: every page failed to decode")
        text = "\n".join(chunks)
        _require_text_layer(text, len(pages))
        return text
    except ExtractorError:
        raise
    except Exception as err:
        raise ExtractorError(f"PDF parse error: {err}") from err


def pdfplumber_text(payload: bytes, kind: str, render: Callable[[Any], str]) -> str:
    """Open ``payload`` with pdfplumber, reconstruct text via ``render``,
    and rewrap failures uniformly.

    ``render`` receives the open pdfplumber document and returns the
    reconstructed text; ``kind`` only shapes the :class:`ExtractorError`
    message. A PDF with pages but no decodable
    text fails loud rather than returning "" and letting every downstream
    regex miss silently (only mandatory fields fail loud; nullable ones
    zero), matching the pypdf path's all-pages guard.
    """
    try:
        import pdfplumber

        with bounded_pdfminer(), pdfplumber.open(BytesIO(payload)) as pdf:
            text = render(pdf)
            # The text-layer check lives on both readers, or whether a card
            # counts as unreadable would depend on which reader its
            # extractor happens to use.
            _require_text_layer(text, len(pdf.pages))
            return text
    except ExtractorError:
        raise
    except Exception as err:
        raise ExtractorError(f"PDF {kind} parse error: {err}") from err


def _each_page(pdf: Any) -> Iterator[Any]:
    """``pdf``'s pages, each let go of once read: pdfplumber holds every
    page it laid out until the document is closed."""
    for page in pdf.pages:
        yield page
        page.close()


def word_rows(words: list[dict[str, Any]], tolerance: float = 3.0) -> list[list[dict[str, Any]]]:
    """pdfplumber ``words`` grouped by line, left to right: a word whose top
    is within ``tolerance`` points of a line's first word joins that line."""
    rows: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: float(w["top"])):
        if rows and float(word["top"]) - float(rows[-1][0]["top"]) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: float(w["x0"])) for row in rows]


def word_centre(word: dict[str, Any]) -> float:
    """The horizontal centre of a pdfplumber word."""
    return (float(word["x0"]) + float(word["x1"])) / 2.0


def extract_pdf_text_layout(payload: bytes) -> str:
    """Extract PDF text via pdfplumber, preserving table layout.

    pdfplumber walks the character stream and reassembles rows from glyph
    coordinates, so each DSO row comes out as one line with its columns in
    order, where pypdf follows the content stream and can split a table.

    Pages are passed through ``dedupe_chars()`` first, which drops glyphs
    a publisher stacked twice at the same coordinates; read as they are,
    "5,09" comes out as "55,,09".
    """
    return pdfplumber_text(
        payload,
        "layout",
        lambda pdf: "\n".join(
            (page.dedupe_chars().extract_text() or "") for page in _each_page(pdf)
        ),
    )


def extract_pdf_text_rows(payload: bytes) -> str:
    """Extract PDF text via pdfplumber's words, one line per row of words
    left to right (:func:`word_rows`).

    For a card whose glyphs pdfplumber's layout text spaces out and reads
    column by column, where the words still sit on their lines, each label
    beside its figure.
    """
    return pdfplumber_text(
        payload,
        "rows",
        lambda pdf: "\n".join(
            " ".join(word["text"] for word in row)
            for page in _each_page(pdf)
            for row in word_rows(page.dedupe_chars().extract_words())
        ),
    )
