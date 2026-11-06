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

"""Reading a figure off a line of a tariff card.

Belgian cards print numbers in several ways: a comma decimal, a dot decimal,
a thousands separator beside a decimal comma, a dash for an empty cell. These
turn a line of extracted text, or a row of an HTML table, into numbers, or
refuse rather than guess. They know nothing about which supplier printed it.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from difflib import SequenceMatcher

from .base import ExtractorError

# Every Unicode space variant Belgian PDFs use inside a figure, as a
# thousands separator or as padding.
_NUMERIC_SEPARATORS = (
    " ",  # ASCII space
    " ",  # NO-BREAK SPACE
    " ",  # THIN SPACE
    " ",  # NARROW NO-BREAK SPACE, the CLDR French thousands separator
    " ",  # LINE SEPARATOR
)

# Every character a card has used for a minus sign or a dash: hyphen-minus,
# hyphen, non-breaking hyphen, figure dash, en dash, em dash, minus sign.
_DASHES = ("-", "‐", "‑", "‒", "–", "—", "−")
SIGN_CHARS = r"+\-‐‑‒–—−"
"""Drop into a regex character class: ``[`` + SIGN_CHARS + ``]``."""

LINE_BREAK = r"(?>\s*\n\s*)"
"""Drop into a pattern for a line break with the spaces and blank lines
around it, before an item that cannot begin with a space. Taken whole, as
the atomic group does, it reads what ``\\s*\\n\\s*`` reads there, which split
a run of blank lines every way before failing: 32 kB of them after Mega's
"Coût énergie" heading took 28 s."""

NUMBER_START = r"(?<!\d)"
"""Drop into a pattern before the figure it opens on, so that a search for
it starts where a number does. It finds what it found without: a match
after a digit was a match from that digit too, which the search reached
first. Tried from every digit of a long run instead, the figure was read
to the end of the run again each time, in time growing with its square:
32 kB of digits held EBEM's price row for 3,7 s."""

# One table cell: a figure, or a lone dash for an empty cell. A figure may
# carry thousands separators and a decimal part in either convention.
_CELL = re.compile(r"^(?:\d+(?:[.,]\d+)*|[" + "".join(_DASHES) + r"])$")
_FIGURE = re.compile(r"\d+(?:[.,]\d+)*")


def month_date(year: str | int, month: str | int, label: str) -> date:
    """The first day of the month a card names, or an ExtractorError naming
    ``label`` when the figures are no month ("0000", "13")."""
    try:
        return date(int(year), int(month), 1)
    except ValueError:
        raise ExtractorError(f"{label}: {year}-{month} is no month") from None


def fold_accents(text: str) -> str:
    """Lowercase and strip Latin diacritics, so an extraction that lost its
    accents ("aout") still matches the spelled one ("août")."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )


def to_float(text: str) -> float:
    """Parse a figure as Belgian cards print it.

    A comma is the decimal separator; so is a lone dot, which Mega and a few
    others print. When both appear the dot groups thousands and the comma is
    the decimal ("1.001,55", EnergyVision's Sibelga T3). Every space variant
    is dropped first, since cards also group thousands with a narrow space.
    """
    cleaned = text.strip()
    for sep in _NUMERIC_SEPARATORS:
        cleaned = cleaned.replace(sep, "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned.replace(",", "."))
    except ValueError as err:
        raise ExtractorError(f"not a figure: {text!r}") from err


def parse_sign(char: str) -> float:
    """-1.0 for any dash or minus variant, +1.0 otherwise."""
    return -1.0 if char in _DASHES else 1.0


def stated_unit(
    text: str,
    pattern: re.Pattern[str],
    figure: tuple[int, int],
    *,
    before: int = 0,
    after: int = 0,
) -> str | None:
    """The unit a card states for the figure at ``figure``, the start and end
    offsets of it in ``text``, or None where it states none there.

    ``pattern`` matches one statement of the unit as the card prints it (a
    column heading, the words after a formula), with the unit as its first
    group. The last statement lying within ``before`` characters ahead of the
    figure is taken, else the first within ``after`` characters behind it. A
    reader compares the answer with the spellings its cards have printed and
    refuses any other, None included: a figure read on a scale its card no
    longer states parses clean ten or a hundred times out, as OCTA+'s formulas
    did in November 2026 (``octaplus.py``).
    """
    start, end = figure
    if before:
        stated = list(pattern.finditer(text, max(0, start - before), start))
        if stated:
            return stated[-1].group(1)
    if after:
        match = pattern.search(text, end, end + after)
        if match:
            return match.group(1)
    return None


def formula_gives(
    printed: float, factor: float, index: float, base: float, *, tolerance: float
) -> bool:
    """Whether ``factor`` x ``index`` + ``base`` gives back the price a card
    prints beside its formula, within ``tolerance`` of it as a share.

    For a card that states no unit for its formula but prints the index its
    price is worked out at: a formula read on the wrong scale, or off the
    wrong row, does not give the printed price back. All four figures in one
    unit and on one VAT basis.
    """
    return abs(factor * index + base - printed) <= tolerance * abs(printed)


def require_contract[T](by_id: Mapping[str, T], contract_id: str, label: str) -> T:
    """The contract definition for ``contract_id``, or raise."""
    try:
        return by_id[contract_id]
    except KeyError:
        raise ExtractorError(f"unknown {label} contract {contract_id!r}") from None


# A page is remote text, and a pattern that scans to the end of it from every
# place it could start takes time growing with the square of the page:
# "<tr[^>]*>(.*?)</tr>" spent a minute on 128 kB of "<tr>". The re module
# holds the GIL while it matches, so not even a worker thread keeps the event
# loop running meanwhile.
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_OPEN = re.compile("<tr")
_ROW_CLOSE = re.compile("</tr>")
_CELL_OPEN = re.compile("<t[hd]")
_CELL_CLOSE = re.compile("</t[hd]>")
_TAG_END = re.compile(">")


def sub_closed(
    pattern: re.Pattern[str],
    repl: str | Callable[[re.Match[str]], str],
    text: str,
    closing: str,
) -> str:
    """``pattern.sub(repl, text)`` in time linear in ``text``, for a
    ``pattern`` that runs from where it starts to the first ``closing``
    after it and matches there.

    Substituted up to the last ``closing`` only, which no match runs past:
    up to there every start finds its closing and the match takes in what
    was scanned, while past it such a pattern scans to the end of the text
    from every place it could start.
    """
    end = text.rfind(closing)
    end = 0 if end < 0 else end + len(closing)
    return pattern.sub(repl, text[:end]) + text[end:]


def strip_tags(text: str) -> str:
    """``text`` with every tag replaced by a space: what ``<[^>]+>``
    matches, which fails, and at once, only on an empty "<>"."""
    return sub_closed(_TAG_RE, " ", text, ">")


def elements(
    text: str, opening: re.Pattern[str], closing: re.Pattern[str], *, attributes: bool = True
) -> Iterator[tuple[re.Match[str], str, re.Match[str]]]:
    """Each element of ``text``, as the match of its ``opening``, what it
    holds and the match of its ``closing``, the first after the opening; the
    next element is looked for after that closing. With ``attributes`` what
    the element holds starts after the first ">" past ``opening``, which ends
    the tag, and without them right after ``opening``.

    These are the matches of ``opening[^>]*>(.*?)closing``, or of
    ``opening(.*?)closing``, found one after the other in time linear in
    ``text``: once no ">" or no ``closing`` follows an opening, none follows
    a later one either. Such a pattern cannot be cut as :func:`sub_closed`
    cuts, since the ">" ending an opening tag can be the closing's own.
    """
    at = 0
    while (opened := opening.search(text, at)) is not None:
        inner = opened.end()
        if attributes:
            inner = text.find(">", inner) + 1
            if not inner:
                return
        closed = closing.search(text, inner)
        if closed is None:
            return
        yield opened, text[inner : closed.start()], closed
        at = closed.end()


def in_tag(
    text: str,
    opening: re.Pattern[str],
    target: re.Pattern[str],
    *,
    end: re.Pattern[str] = _TAG_END,
) -> Iterator[tuple[re.Match[str], re.Match[str]]]:
    """Each ``opening`` with the first ``target`` after it before the tag
    ends, as the two matches, the next looked for after that target: what
    ``opening[^>]*?target`` finds, the tag ending at a ">" or at the first
    match of ``end``, for a ``target`` whose match does not depend on where
    it is searched from. A ``target`` beginning with the tag's end, as
    ``>rest`` does, gives what ``opening[^>]*>rest`` finds.

    In time linear in ``text``: the tag's end and the next ``target`` are
    looked for again only once an opening has passed them, where the pattern
    looked for both from every opening.
    """
    at = 0
    tag_end = -1
    found: re.Match[str] | None = None
    while (opened := opening.search(text, at)) is not None:
        start = opened.end()
        if tag_end < start:
            ended = end.search(text, start)
            tag_end = len(text) if ended is None else ended.start()
        if found is None or found.start() < start:
            found = target.search(text, start)
            if found is None:
                return
        if found.start() <= tag_end:
            yield opened, found
            at = found.end()
        else:
            at = opened.start() + 1


def search_once_per_line(
    text: str, start: re.Pattern[str], rest: re.Pattern[str]
) -> re.Match[str] | None:
    """The match of ``rest`` right after the first ``start`` it follows:
    what ``start`` then ``rest`` searches for. ``rest`` is tried after the
    first ``start`` it would begin on in each line only, for a ``rest`` that
    fails after a later ``start`` on a line once it failed after an earlier
    one there, as one looking for something further on the same line does.

    In time linear in ``text`` for such a ``rest``, where the pattern looked
    to the end of the line again from every ``start`` on it.
    """
    failed_to = -1
    at = 0
    while (begun := start.search(text, at)) is not None:
        here = begun.end()
        if here > failed_to:
            found = rest.match(text, here)
            if found is not None:
                return found
            failed_to = text.find("\n", here)
            if failed_to < 0:
                return None
        at = begun.start() + 1
    return None


def html_cells(row: str) -> list[str]:
    """The text of each cell of one HTML table row, tags dropped, entities
    decoded and whitespace collapsed."""
    return [
        " ".join(html.unescape(strip_tags(cell)).split())
        for _opened, cell, _closed in elements(row, _CELL_OPEN, _CELL_CLOSE)
    ]


def html_rows(page: str) -> list[list[str]]:
    """Every ``<tr>`` of ``page`` as its cells' text."""
    return [html_cells(row) for _opened, row, _closed in elements(page, _ROW_OPEN, _ROW_CLOSE)]


def split_row(line: str) -> tuple[str, list[str]]:
    """A table line as its label and its cells.

    The cells are read from the right: the trailing run of figures and lone
    dashes. Everything before them is the label, so a label that carries a
    dash of its own ("TECTEO - RESA") or a figure in the middle keeps it.
    """
    words = line.split()
    end = len(words)
    while end and _CELL.match(words[end - 1]):
        end -= 1
    return " ".join(words[:end]).rstrip(":").strip(), words[end:]


def cell_value(cell: str) -> float | None:
    """A cell as a float, or None for a dash."""
    if cell in _DASHES:
        return None
    return to_float(cell)


def table_row(
    text: str,
    label: str,
    columns: int | None = None,
    *,
    after: str | None = None,
    before: str | None = None,
    threshold: float = 0.82,
) -> list[str] | None:
    """The cells of the table row whose label reads as ``label``.

    The label is matched by similarity rather than spelling, because a card
    misspells now and then ("Fuvius Halle-Vilvoorde") and pads others
    ("Fluvius (Antwerpen )"). A row's cells are all on ONE line; the only
    line looked past is a label too long for its column that wrapped above
    its own cells, leaving them alone on the next line. Where the caller
    knows the column count from the card's headings it has to match.
    ``after`` and ``before`` bound the search to the block the row belongs
    to.

    Returns the cells as printed, or None when nothing matches well enough.
    A wrong row is worse than no row, so the caller raises where the card
    promises one.
    """
    body = text
    if after is not None:
        start = body.find(after)
        if start < 0:
            return None
        body = body[start + len(after) :]
    if before is not None:
        end = body.find(before)
        if end >= 0:
            body = body[:end]

    best: tuple[float, list[str]] | None = None
    lines = body.splitlines()
    for index, line in enumerate(lines):
        row_label, cells = split_row(line)
        if not cells or not any(_FIGURE.fullmatch(c) for c in cells):
            continue
        if columns is not None and len(cells) != columns:
            continue
        if not row_label and index:
            previous_label, previous_cells = split_row(lines[index - 1])
            if not previous_cells:
                row_label = previous_label
        score = SequenceMatcher(None, row_label.lower(), label.lower()).ratio()
        if score >= threshold and (best is None or score > best[0]):
            best = (score, cells)
    return None if best is None else best[1]
