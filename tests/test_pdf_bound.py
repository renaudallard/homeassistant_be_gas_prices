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

"""What a card's readers are held to while they read it
(``providers/_pdf_bound.py`` and ``providers/_pdf_bound_pdfminer.py``),
each bound shown refusing a crafted card past it and reading one under it
as before."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest

from custom_components.be_gas_prices.providers import _pdf_bound, _pdf_bound_pdfminer
from custom_components.be_gas_prices.providers._pdf_render import (
    extract_pdf_text,
    extract_pdf_text_layout,
    extract_pdf_text_rows,
)
from custom_components.be_gas_prices.providers.base import (
    CardNotReadableError,
    ExtractorError,
)


def _pdf_from(objects: Sequence[bytes], *, sections: int = 1) -> bytes:
    """A PDF of ``objects``, numbered from 1, the first its catalog, with
    its last object listed again in each of ``sections`` - 1 later
    cross-reference sections chained by /Prev."""
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = 0
    for section in range(sections):
        previous, xref = xref, len(out)
        if section == 0:
            out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
            out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        else:
            out += b"xref\n%d 1\n%010d 00000 n \n" % (len(objects), offsets[-1])
        out += b"trailer<</Size %d/Root 1 0 R" % (len(objects) + 1)
        if section:
            out += b"/Prev %d" % previous
        out += b">>\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


# A font for the page to name: pypdf reads nothing of a page without
# resources.
_FONT_RESOURCES = b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>"


def _pdf_of(
    stream: bytes,
    *extra: bytes,
    attrs: bytes = b"/Filter/FlateDecode",
    pages: int = 1,
    contents: bytes = b"3 0 R",
    tree: bool = True,
) -> bytes:
    """A PDF of ``pages`` pages that all draw ``stream``, with ``attrs`` in
    its dictionary, as their content, named ``contents``, and name a font,
    followed by the
    ``extra`` objects, which nothing refers to. Without ``tree`` the catalog
    names no page tree."""
    kids = b" ".join(b"%d 0 R" % (4 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>" if tree else b"<</Type/Catalog>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<<%s/Length %d>>\nstream\n" % (attrs, len(stream)) + stream + b"\nendstream",
            *(
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents %s%s>>"
                % (contents, _FONT_RESOURCES),
            )
            * pages,
            *extra,
        )
    )


def _comment(size: int) -> bytes:
    """A content stream of comments ``size`` + 2 bytes long, which inflates
    from a few kB, each line shorter than a token pdfminer may gather."""
    return ((b"%" + b"A" * 62 + b"\n") * (size // 64 + 1))[: size + 1] + b"\n"


def _inflating_pdf(
    size: int,
    *extra: bytes,
    attrs: bytes = b"/Filter/FlateDecode",
    pages: int = 1,
    contents: bytes = b"3 0 R",
    tree: bool = True,
) -> bytes:
    """A PDF whose content stream, of comments, inflates to ``size`` bytes."""
    import zlib

    return _pdf_of(
        zlib.compress(_comment(size), 9),
        *extra,
        attrs=attrs,
        pages=pages,
        contents=contents,
        tree=tree,
    )


_EVERY_READER = [extract_pdf_text, extract_pdf_text_layout, extract_pdf_text_rows]


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_streams_that_inflate_past_the_cap_are_refused_before_the_readers(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer decompresses a stream with no limit, in a worker thread the
    fetch timeout cannot cancel: a 20 kB file inflating to 20 MB took 26 s
    of CPU on a Raspberry Pi, and the download cap does not see it. pypdf
    limits each stream but not their total."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    payload = _inflating_pdf(2 << 20)
    assert len(payload) < 10_000
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than 1048576 bytes"):
        extract(payload)
    # Under the cap the same document goes on to the reader as before.
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000))


def test_pypdf_reaching_its_own_limit_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under pypdf's own words for the limit: every one was reported as the
    decode bound, whatever pypdf had stopped at."""
    import pypdf.filters
    from pypdf.errors import LimitReachedError

    def limited(_data: bytes) -> bytes:
        raise LimitReachedError("Limit reached while decompressing.")

    monkeypatch.setattr(pypdf.filters, "decompress", limited)
    with pytest.raises(
        ExtractorError,
        match=r"^PDF reaches a limit of pypdf \(Limit reached while decompressing\.\)",
    ):
        extract_pdf_text(_inflating_pdf(1000))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_content_stream_counts_whatever_its_subtype(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Images were left out by their /Subtype, and a page's content stream
    tagged /Image escaped the count while pdfminer inflated it all the
    same. An image no page draws as its content is still left out."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    image = b"/Subtype/Image/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(2 << 20, attrs=image))
    picture = zlib.compress(b"\0" * (2 << 20), 9)
    unread = b"<<%s/Length %d>>\nstream\n" % (image, len(picture))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000, unread + picture + b"\nendstream"))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_content_stream_counts_once_for_each_page_drawing_it(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Each page interprets its content again: a 2,7 kB file whose one
    stream served 16 pages kept pdfplumber busy for 100 s, and the stream
    was counted once."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(300_000, pages=4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(300_000, pages=3))


def _written_into_pages_pdf(stream: bytes, pages: int) -> bytes:
    """``pages`` pages, each with ``stream``, a FlateDecode stream, written
    into it as its content rather than referred to."""
    content = b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(stream)
    content += stream + b"\nendstream"
    kids = b" ".join(b"%d 0 R" % (3 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            *(
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents %s%s>>"
                % (content, _FONT_RESOURCES),
            )
            * pages,
        )
    )


def test_a_content_stream_written_into_its_page_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counted by the objects the cross-reference table lists, a stream
    written into its page counted nothing: a 175 kB file had pypdf decode
    60 MB for its text. pdfminer does not read one."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    stream = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text(_written_into_pages_pdf(stream, 4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text(_written_into_pages_pdf(stream, 3))


def _form_pdf(
    form: bytes, draws: int, fonts: int = 0, attrs: bytes = b"/Filter/FlateDecode"
) -> bytes:
    """One page drawing ``draws`` times a form, ``form`` a FlateDecode
    stream unless ``attrs`` says otherwise, whose resources name one font
    ``fonts`` times."""
    names = b"".join(b"/F%d 5 0 R" % name for name in range(fonts))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[4 0 R]/Count 1>>",
            b"<</Length %d>>\nstream\n%s\nendstream"
            % (len(b"/X0 Do\n" * draws), b"/X0 Do\n" * draws),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R"
            b"/Resources<</XObject<</X0 6 0 R>>>>>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
            b"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]"
            b"/Resources<</Font<<%s>>>>%s/Length %d>>\nstream\n"
            % (names, attrs, len(form))
            + form
            + b"\nendstream",
        )
    )


@pytest.mark.parametrize("filtered", [True, False])
@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_form_counts_once_for_each_time_it_is_drawn(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str], filtered: bool
) -> None:
    """pypdf interprets a form again each time a page draws it, and it was
    counted once: a 934 B file kept pypdf busy for 77 s. A refusal in a
    form, which pypdf logs and goes on from, still refuses. A form no
    filter packs counts as much."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    form = _comment(300_000)
    attrs = b""
    if filtered:
        form, attrs = zlib.compress(form, 9), b"/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_form_pdf(form, 4, attrs=attrs))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_form_pdf(form, 3, attrs=attrs))


def _fonts_pdf(pages: int, to_unicode: bytes) -> bytes:
    """``pages`` pages, each with a font of its own, all of them with
    ``to_unicode``, a FlateDecode stream, as their ToUnicode map."""
    kids = b" ".join(b"%d 0 R" % (5 + 2 * page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<</Length 35>>\nstream\nBT /F1 12 Tf 10 50 Td (Hello) Tj ET\nendstream",
            b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(to_unicode)
            + to_unicode
            + b"\nendstream",
            *(
                part
                for page in range(pages)
                for part in (
                    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                    b"/Contents 3 0 R/Resources<</Font<</F1 %d 0 R>>>>>>" % (6 + 2 * page),
                    b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/ToUnicode 4 0 R>>",
                )
            ),
        )
    )


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_tounicode_map_counts_each_time_a_font_is_loaded_from_it(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A ToUnicode map the fonts of several pages share was counted once and
    parsed for each of them."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    to_unicode = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_fonts_pdf(4, to_unicode))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_fonts_pdf(3, to_unicode))


def test_each_hex_string_of_a_tounicode_map_pypdf_reads_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf goes through every hex string of a ToUnicode map before it
    builds anything of it, and a map of empty ones builds nothing: 2
    million took 1,9 s on a Raspberry Pi with nothing counting them. Each
    counts as a font entry."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_FONT_ENTRIES", 10_000)
    with pytest.raises(ExtractorError, match=r"^PDF has a reader build more than 10000"):
        extract_pdf_text(_fonts_pdf(1, zlib.compress(b"<>" * 20_000)))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text(_fonts_pdf(1, zlib.compress(b"<>" * 5_000)))


def test_a_font_counts_each_time_pypdf_loads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf loads every font a form names each time the form is drawn,
    which no stream holds: a 3 kB file naming one font 200 times in a form
    drawn 200 times took 10,8 s, and counted nothing. A load counts, and
    the same dictionary is read to a font once."""
    import zlib

    _font = _pdf_bound.pypdf_font
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    form = zlib.compress(b" ")
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text(_form_pdf(form, 11, fonts=100))
    encode = _font.get_encoding
    loads: list[object] = []

    def counted(font: object) -> object:
        loads.append(font)
        return encode(font)

    monkeypatch.setattr(_font, "get_encoding", counted)
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text(_form_pdf(form, 9, fonts=100))
    assert len(loads) == 1


def test_pypdf_stops_inflating_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf inflates a stream up to 75 MB before anything could count it.
    Its inflate stops at what is left of the bound."""
    import tracemalloc

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    payload = _inflating_pdf(32 << 20)
    tracemalloc.start()
    try:
        with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
            extract_pdf_text(payload)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 << 20


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_page_naming_its_content_by_another_generation_counts(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer resolves a reference by its number whatever generation it
    names. Counted by generation and number, a page naming its content
    ``3 1 R`` drew nothing: a content stream tagged /Image escaped the
    count, 200 MiB inflated from a 204 kB file, and a stream four pages
    shared counted once."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    image = b"/Subtype/Image/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(2 << 20, attrs=image, contents=b"3 1 R"))
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(300_000, pages=4, contents=b"3 1 R"))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(300_000, pages=3, contents=b"3 1 R"))


def _orphan_page_pdf(stream: bytes, attrs: bytes) -> bytes:
    """A tree pypdf walks to one page and pdfminer to none, its nodes being
    untyped, so pdfminer reads the one object typed as a page instead, which
    draws ``stream``."""
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Kids[3 0 R]/Count 1>>",
            b"<</Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
            b"<</Length 22>>\nstream\nBT /F1 12 Tf (hi) Tj ET\nendstream",
            b"<<%s/Length %d>>\nstream\n" % (attrs, len(stream)) + stream + b"\nendstream",
            b"<</Type/Page/MediaBox[0 0 612 792]/Contents 5 0 R>>",
        )
    )


def _listed_page_pdf(stream: bytes, sections: int) -> bytes:
    """No page tree, and the one page, drawing ``stream``, listed in
    ``sections`` cross-reference sections, each of which pdfminer reads it
    from."""
    return _pdf_from(
        (
            b"<</Type/Catalog>>",
            b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<</Type/Page/MediaBox[0 0 612 792]/Contents 2 0 R>>",
        ),
        sections=sections,
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_pdfplumber_is_bounded_on_the_pages_pdfminer_lists(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer finds pages its own way: with no page tree, or none it can
    walk, every object typed as a page, once for each cross-reference
    section listing it. Imitated off pypdf's walk, a page pdfminer found
    and pypdf did not escaped the count: an image-tagged stream it drew
    inflated 48 MiB, and one page listed in forty sections cost 43 s."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    image = b"/Subtype/Image/Filter/FlateDecode"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_orphan_page_pdf(zlib.compress(_comment(2 << 20), 9), image))
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_inflating_pdf(300_000, pages=4, tree=False))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(300_000, pages=3, tree=False))
    content = zlib.compress(_comment(300_000), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_listed_page_pdf(content, 4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_listed_page_pdf(content, 3))


def _hidden_stream_pdf(hidden: bytes, shape: str) -> bytes:
    """A one-page PDF whose content, object 4, pypdf reads as a few bytes of
    text and pdfminer as ``hidden``, a FlateDecode stream it finds elsewhere:
    ``rebuilt``, a table header spaced twice that pdfminer rebuilds by
    scanning the file, the later definition winning; ``fallthrough``, the
    newest table naming a mislabelled object pypdf takes and pdfminer skips
    for the older table's entry, which pypdf's own repair cannot find."""
    text = b"BT /F1 12 Tf 10 50 Td (Hello) Tj ET"
    out = bytearray(b"%PDF-1.4\n")
    at = {}

    def put(key: object, head: bytes, body: bytes) -> None:
        at[key] = len(out)
        out.extend(head + body + b"\nendobj\n")

    put(1, b"1 0 obj\n", b"<</Type/Catalog/Pages 2 0 R>>")
    put(2, b"2 0 obj\n", b"<</Type/Pages/Kids[3 0 R]/Count 1>>")
    put(
        3,
        b"3 0 obj\n",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
    )
    small = b"<</Length %d>>\nstream\n" % len(text) + text + b"\nendstream"
    big = b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(hidden) + hidden
    big += b"\nendstream"

    def rows(*keys: object) -> bytes:
        return b"".join(b"%010d 00000 n \n" % at[k] for k in keys)

    if shape == "rebuilt":
        put(4, b"4 0 obj\n", small)
        put("hidden", b"4 0 obj\n", big)
        xref = len(out)
        out += b"xref\n0  5\n0000000000 65535 f \n" + rows(1, 2, 3, 4)
        out += b"trailer<</Size 5/Root 1 0 R>>\n"
    else:
        put("hidden", b"4 0 %c\nobj\n", big)
        put(4, b"5 0 obj\n", small)
        older = len(out)
        out += b"xref\n0 5\n0000000000 65535 f \n" + rows(1, 2, 3, "hidden")
        out += b"trailer<</Size 5/Root 1 0 R>>\n"
        xref = len(out)
        out += b"xref\n4 1\n" + rows(4)
        out += b"trailer<</Size 5/Root 1 0 R/Prev %d>>\n" % older
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


@pytest.mark.parametrize("shape", ["rebuilt", "fallthrough"])
@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_pdfplumber_is_bounded_on_what_pdfminer_decodes(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str], shape: str
) -> None:
    """Counted on pypdf's reading of the file, every way of having the two
    readers find different objects let pdfminer decode what pypdf never
    counted: two tables, an entry listed twice, a shifted pointer, a table
    rebuilt by scanning, an entry pdfminer skips for an older one, each 40
    MiB and more. What pdfminer decodes is measured on pdfminer itself."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    hidden = zlib.compress(_comment(2 << 20), 9)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_hidden_stream_pdf(hidden, shape))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_hidden_stream_pdf(zlib.compress(_comment(1000), 9), shape))


def _object_stream_pdf(padding: int) -> bytes:
    """A PDF whose catalog, page tree and page sit in an object stream padded
    with ``padding`` blanks, behind a cross-reference stream, so pdfminer
    inflates it opening the file."""
    import struct
    import zlib

    objects = (
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
    )
    body = b""
    offsets = []
    for obj in objects:
        offsets.append(len(body))
        body += obj + b"\n"
    header = b" ".join(b"%d %d" % (n, o) for n, o in enumerate(offsets, 1))
    packed = zlib.compress(header + b"\n" + body + b" " * padding, 9)
    text = b"BT /F1 12 Tf 72 720 Td (Hello) Tj ET"
    out = bytearray(b"%PDF-1.5\n")
    at = {4: len(out)}
    out += b"4 0 obj\n<</Length %d>>\nstream\n%s\nendstream\nendobj\n" % (
        len(text),
        text,
    )
    at[5] = len(out)
    out += b"5 0 obj\n<</Type/ObjStm/N 3/First %d/Filter/FlateDecode/Length %d>>" % (
        len(header) + 1,
        len(packed),
    )
    out += b"\nstream\n" + packed + b"\nendstream\nendobj\n"
    at[6] = len(out)
    rows = struct.pack(">BIH", 0, 0, 65535)
    rows += b"".join(struct.pack(">BIH", 2, 5, i) for i in range(3))
    rows += b"".join(struct.pack(">BIH", 1, at[n], 0) for n in (4, 5, 6))
    out += b"6 0 obj\n<</Type/XRef/Size 7/W[1 4 2]/Root 1 0 R/Length %d>>" % len(rows)
    out += b"\nstream\n" + rows + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % at[6]
    return bytes(out)


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_what_pdfminer_inflates_opening_a_file_is_measured(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer inflates the cross-reference and object streams as it opens
    a file, before anything could be counted: 300 MB from a 306 kB file. Its
    decoding is measured as it goes, so it stops at the bound."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_object_stream_pdf(2 << 20))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_object_stream_pdf(1000))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_ccitt_stream_outside_an_image_is_refused(
    extract: Callable[[bytes], str],
) -> None:
    """pdfminer decodes CCITT fax in pure Python: 400 bytes took 3,4 s on a
    Raspberry Pi. No card carries one, and an image may."""
    ccitt = b"/Filter/CCITTFaxDecode/DecodeParms<</K -1/Columns 100>>"
    with pytest.raises(ExtractorError, match=r"^PDF carries a CCITT"):
        extract(_pdf_of(b"\xff" * 20, attrs=ccitt))
    fax = b"<</Subtype/Image/Width 100/Height 1%s/Length 20>>\nstream\n" % ccitt
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000, fax + b"\xff" * 20 + b"\nendstream"))


def _damaged(size: int, *, how: str) -> bytes:
    """A PDF whose content stream, ``size`` bytes of comment that compress
    poorly, is cut short, inside an ASCII85 wrapper too (``wrapped``),
    whole but failing its checksum (``checksum``), broken halfway
    (``broken``), or cut short under a gzip header (``gzip``)."""
    import base64
    import gzip
    import random
    import zlib

    letters = random.Random(0).choices(b"abcdefghijklmnopqrstuvwxyz", k=size)
    whole = zlib.compress(b"%" + bytes(letters) + b"\n", 9)
    if how == "gzip":
        return _pdf_of(gzip.compress(b"%" + bytes(letters) + b"\n")[:-4])
    if how == "checksum":
        return _pdf_of(whole[:-1] + bytes([whole[-1] ^ 1]))
    if how == "broken":
        half = len(whole) // 2
        return _pdf_of(whole[:half] + b"\xff" * 8 + whole[half + 8 :])
    if how == "cut":
        return _pdf_of(whole[:-4])
    return _pdf_of(
        base64.a85encode(whole[:-4]) + b"~>",
        attrs=b"/Filter[/ASCII85Decode/FlateDecode]",
    )


@pytest.mark.parametrize("how", ["cut", "wrapped", "checksum"])
@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_damaged_flate_stream_counts_many_times_over(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str], how: str
) -> None:
    """zlib refuses a FlateDecode stream cut short, and pdfminer then
    inflates it again a byte at a time, in a time quadratic in its size.
    A small one, like those on some Bolt and Frank cards, still reads."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    monkeypatch.setattr(_pdf_bound, "_DAMAGED_FLATE_UNIT", 4096)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_damaged(128 << 10, how=how))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_damaged(1000, how=how))


def test_pypdf_counts_again_a_flate_stream_zlib_fails_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf reads a stream cut short as far as it goes, at once, and goes
    on past a failure, or a gzip header, a byte at a time to the end of the
    stream. Counted again like pdfminer's, the 216 kB stream Bolt's
    professional Plenty fixed card of June 2026 cuts short counted 8,8 MB
    more for nothing."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    monkeypatch.setattr(_pdf_bound, "_DAMAGED_FLATE_UNIT", 4096)
    for how in ("broken", "checksum", "gzip"):
        with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
            extract_pdf_text(_damaged(128 << 10, how=how))
    for how in ("cut", "wrapped"):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract_pdf_text(_damaged(128 << 10, how=how))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_the_bound_is_the_only_reason_pdfplumber_is_refused(
    extract: Callable[[bytes], str],
) -> None:
    """A stream with a filter no reader knows used to be refused as a parse
    error before pdfplumber, which read such cards before the check, saw
    them, and a file pdfminer cannot open is its own parse error."""
    unknown_filter = b"<</Length 4/Filter/NoSuchDecode>>\nstream\nabcd\nendstream"
    # pdfplumber reads the page, which carries no text: the card reached it.
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_inflating_pdf(1000, unknown_filter))
    with pytest.raises(ExtractorError, match="parse error") as unreadable:
        extract(b"%PDF-1.4\nnothing a reader can open\n")
    assert "refusing" not in str(unreadable.value)


def _font_pdf(pages: int, to_unicode: bytes, attrs: bytes = b"") -> bytes:
    """``pages`` pages sharing resources whose font is written into them,
    with ``to_unicode``, a FlateDecode stream with ``attrs``, as its
    ToUnicode map."""
    kids = b" ".join(b"%d 0 R" % (6 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<</Length 35>>\nstream\nBT /F1 12 Tf 10 50 Td (Hello) Tj ET\nendstream",
            b"<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica/ToUnicode 5 0 R>>>>>>",
            b"<<%s/Filter/FlateDecode/Length %d>>\nstream\n" % (attrs, len(to_unicode))
            + to_unicode
            + b"\nendstream",
            *(b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R/Resources 4 0 R>>",)
            * pages,
        )
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_pdfminer_is_held_to_the_bound_on_every_stream_it_reads(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A check made on a reading of the file beforehand missed what pdfminer
    itself reads: a font's ToUnicode map tagged as an image, a FlateDecode
    step after DCTDecode, a second inflate behind a predictor, each 256 MiB
    from a file of a few hundred kB. It is held to the bound as it reads."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    big = zlib.compress(_comment(2 << 20), 9)
    small = zlib.compress(_comment(1000), 9)
    for stream in (big, small):
        refused = stream is big
        rows = b"".join(
            b"\0" + stream[i : i + 100].ljust(100, b"\0") for i in range(0, len(stream), 100)
        )
        shapes = (
            _font_pdf(1, stream, b"/Subtype/Image"),
            _pdf_of(stream, attrs=b"/Filter[/DCTDecode/FlateDecode]"),
            _pdf_of(
                zlib.compress(rows, 9),
                attrs=b"/Filter[/FlateDecode/FlateDecode]"
                b"/DecodeParms[<</Predictor 10/Columns 100>>null]",
            ),
        )
        for payload in shapes:
            if refused:
                with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
                    extract(payload)
            else:
                with pytest.raises(CardNotReadableError, match="no text layer"):
                    extract(payload)


def test_pdfminer_stops_inflating_at_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused once inflated, a stream would already be held: 300 MB from a
    306 kB file. pdfminer's inflate stops at what is left of the bound."""
    from pdfminer.pdftypes import PDFStream

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    decode = PDFStream.decode
    held: list[int] = []

    def decoded(stream: PDFStream) -> None:
        decode(stream)
        held.append(len(stream.data or b""))

    monkeypatch.setattr(PDFStream, "decode", decoded)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text_layout(_object_stream_pdf(8 << 20))
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract_pdf_text_layout(_inflating_pdf(8 << 20))
    assert max(held, default=0) <= 1 << 20


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_font_written_into_shared_resources_is_read_once(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer keeps a font only when it is referenced, so one written into
    the resources pages share had its ToUnicode map parsed again on every
    page: a 1 MiB map on ten pages took 130 s. It is built once a read."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    to_unicode = zlib.compress(_comment(300_000), 9)
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_font_pdf(10, to_unicode))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_pdfminer_filters_decoded_in_pure_python_are_refused(
    extract: Callable[[bytes], str],
) -> None:
    """pdfminer decodes LZW and run length in pure Python with no limit; no
    card carries either outside an image, which pdfminer never decodes."""
    lzw = b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"
    for name, stream in ((b"LZWDecode", lzw), (b"RunLengthDecode", b"\x00A\x80")):
        with pytest.raises(
            ExtractorError, match=r"^PDF carries a [A-Za-z -]* stream outside an image"
        ):
            extract(_pdf_of(stream, attrs=b"/Filter/" + name))
        image = b"<</Subtype/Image/Width 1/Height 1/Filter/%s/Length %d>>\nstream\n" % (
            name,
            len(stream),
        )
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(_inflating_pdf(1000, image + stream + b"\nendstream"))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_each_inflate_of_a_chain_counts(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A stream inflated over and over, each step as large as the last,
    counted only its final output: 51 inflates of 4 MiB from a 7,6 kB file
    counted 4 MiB."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)

    def chained(steps: int) -> bytes:
        stream = _comment(300_000)
        for _ in range(steps):
            stream = zlib.compress(stream, 0)
        return _pdf_of(stream, attrs=b"/Filter[%s]" % (b"/FlateDecode" * steps))

    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(chained(4))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(chained(3))


def test_an_rc4_decryption_counts_before_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pdfminer decrypts RC4 a byte at a time, appending each to what it
    holds: 1 MiB took 365 s, and twenty 64 kB strings 8 s, none counted.
    Each counts its length times itself, before it runs. Every encrypted
    card is AES."""
    from pdfminer.arcfour import Arcfour

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with _pdf_bound_pdfminer.bounded_pdfminer():
        for _ in range(3):
            assert len(Arcfour(b"key").decrypt(b"\0" * (64 << 10))) == 64 << 10
    with (
        pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"),
        _pdf_bound_pdfminer.bounded_pdfminer(),
    ):
        for _ in range(5):
            Arcfour(b"key").decrypt(b"\0" * (64 << 10))
    big = 2 << 20
    assert len(Arcfour(b"key").decrypt(b"\0" * (64 << 10))) == 64 << 10
    with (
        pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"),
        _pdf_bound_pdfminer.bounded_pdfminer(),
    ):
        Arcfour(b"key").decrypt(b"\0" * big)


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_large_ascii85_stream_is_refused(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Decoding ASCII85 takes about 90 bytes of memory for each: 16 MiB of
    it peaked at 1,7 GiB, before anything was counted. The largest a card
    carries is 2,2 kB."""
    import base64

    monkeypatch.setattr(_pdf_bound, "_MAX_ASCII85_BYTES", 4096)
    a85 = b"/Filter/ASCII85Decode"
    with pytest.raises(ExtractorError, match=r"^PDF carries an ASCII85 stream"):
        extract(_pdf_of(base64.a85encode(_comment(4096)) + b"~>", attrs=a85))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(base64.a85encode(_comment(1000)) + b"~>", attrs=a85))


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_ascii85_behind_another_filter_is_held_to_what_it_decodes(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Behind an inflate, an ASCII85 step decodes what the inflate made of a
    few bytes, which no size of the stream itself bounds: it is held to the
    ASCII85 it is handed."""
    import base64
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_ASCII85_BYTES", 4096)
    attrs = b"/Filter[/FlateDecode/ASCII85Decode]"
    big = zlib.compress(base64.a85encode(_comment(4096)) + b"~>")
    with pytest.raises(ExtractorError, match=r"^PDF carries an ASCII85 stream"):
        extract(_pdf_of(big, attrs=attrs))
    small = zlib.compress(base64.a85encode(_comment(1000)) + b"~>")
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(small, attrs=attrs))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_what_a_decode_hands_back_past_its_last_inflate_counts(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """ASCII85 behind an inflate makes four bytes of each ``z``: a 38 kB file
    had pdfminer handed 125,8 MB with 31,5 MB counted."""
    import zlib

    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    attrs = b"/Filter[/FlateDecode/ASCII85Decode]"
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_pdf_of(zlib.compress(b"z" * 300_000 + b"~>"), attrs=attrs))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(zlib.compress(b"z" * 200_000 + b"~>"), attrs=attrs))


def _packed_pdf(
    objects: Sequence[bytes], packed: bytes, *, filter: bytes = b"/FlateDecode"
) -> bytes:
    """A PDF of ``objects``, numbered from 1, the first its catalog, and of
    ``packed``, numbered next and kept in an object stream under ``filter``,
    behind a cross-reference stream."""
    import struct
    import zlib

    number = len(objects) + 1
    head = b"%d 0 " % number
    body = head + packed
    if filter == b"/FlateDecode":
        body = zlib.compress(body)
    out = bytearray(b"%PDF-1.5\n")
    rows = struct.pack(">BIH", 0, 0, 65535)
    for at, obj in enumerate(objects, 1):
        rows += struct.pack(">BIH", 1, len(out), 0)
        out += b"%d 0 obj\n" % at + obj + b"\nendobj\n"
    rows += struct.pack(">BIH", 2, number + 1, 0)
    rows += struct.pack(">BIH", 1, len(out), 0)
    out += b"%d 0 obj\n<</Type/ObjStm/N 1/First %d/Filter%s/Length %d>>" % (
        number + 1,
        len(head),
        filter,
        len(body),
    )
    out += b"\nstream\n" + body + b"\nendstream\nendobj\n"
    xref = len(out)
    rows += struct.pack(">BIH", 1, xref, 0)
    out += b"%d 0 obj\n<</Type/XRef/Size %d/W[1 4 2]/Root 1 0 R/Length %d>>" % (
        number + 2,
        number + 3,
        len(rows),
    )
    out += b"\nstream\n" + rows + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def _filter_in_object_stream_pdf(stream: bytes) -> bytes:
    """A PDF whose one page draws ``stream``, unfiltered, through a /Filter
    kept in an object stream, which pdfminer inflates in the middle of
    decoding ``stream``."""
    return _packed_pdf(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R>>",
            b"<</Filter 5 0 R/Length %d>>\nstream\n" % len(stream) + stream + b"\nendstream",
        ),
        b"[]",
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_stream_counts_whatever_its_filter_inflates_to_resolve(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A stream whose /Filter sits in an object stream not yet read has
    pdfminer inflate that one while decoding it: 40 MB of content counted
    202 bytes."""
    monkeypatch.setattr(_pdf_bound, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    with pytest.raises(ExtractorError, match=r"^PDF streams decode to more than"):
        extract(_filter_in_object_stream_pdf(_comment(1 << 20)))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_filter_in_object_stream_pdf(_comment(1000)))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_predictor_row_wider_than_its_stream_is_refused(
    extract: Callable[[bytes], str],
) -> None:
    """pdfminer holds a PNG predictor row of the width named before it reads
    any: a 468-byte file naming 64 Mi columns took 581 MiB. A row as wide as
    its stream, or no wider than 16 kB, is read."""
    import zlib

    def predicted(row: int, columns: int) -> bytes:
        return _pdf_of(
            zlib.compress(b"\x00" + b" " * row),
            attrs=b"/Filter/FlateDecode/DecodeParms<</Predictor 12/Columns %d>>" % columns,
        )

    with pytest.raises(ExtractorError, match=r"^PDF names a predictor row wider"):
        extract(predicted(20, 1 << 20))
    for row, columns in ((20, 1000), (20_000, 20_000)):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(predicted(row, columns))


def _swallowed_refusal_pdf() -> bytes:
    """A card whose text reads, with an /Info title in an LZW object stream:
    pdfplumber reads the metadata and logs what fails there instead of
    raising it."""
    import struct

    text = b"BT /F1 12 Tf 72 720 Td " + b"(Hello world, a card with text) Tj 0 -14 Td " * 30
    text += b"ET"
    lzw = b"\x80\x0b\x60\x50\x22\x0c\x0c\x85\x01"
    objects = {
        1: b"<</Type/Catalog/Pages 2 0 R>>",
        2: b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        3: b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>>>>>>>",
        4: b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text),
        5: b"<</Type/ObjStm/N 1/First 4/Filter/LZWDecode/Length 9>>\nstream\n"
        + lzw
        + b"\nendstream",
        7: b"<</Title 8 0 R>>",
    }
    out = bytearray(b"%PDF-1.5\n")
    at = {}
    for number, body in objects.items():
        at[number] = len(out)
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    at[6] = len(out)
    rows = struct.pack(">BIH", 0, 0, 65535)
    for number in range(1, 9):
        if number == 8:
            rows += struct.pack(">BIH", 2, 5, 0)
        else:
            rows += struct.pack(">BIH", 1, at.get(number, 0), 0)
    out += b"6 0 obj\n<</Type/XRef/Size 9/W[1 4 2]/Root 1 0 R/Info 7 0 R/Length %d>>" % (len(rows))
    out += b"\nstream\n" + rows + b"\nendstream\nendobj\n"
    out += b"startxref\n%d\n%%%%EOF\n" % at[6]
    return bytes(out)


def test_a_refusal_pdfplumber_swallows_still_refuses() -> None:
    """pdfplumber logs what fails while it reads the metadata and goes on,
    and the card's text came back as if the refusal had not been made."""
    with pytest.raises(ExtractorError, match=r"^PDF carries a LZW stream"):
        extract_pdf_text_layout(_swallowed_refusal_pdf())


def test_a_refusal_ends_the_read_where_it_is_made(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """pypdf catches any error a form raises, logs it and goes on to the
    form's next draw: a file drawing a form 100 000 times logged 99 984
    lines after the refusal, each draw refused again."""
    import logging

    content = b"/X0 Do\n" * 200
    payload = _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[5 0 R]/Count 1>>",
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
            b"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Filter/LZWDecode"
            b"%s/Length 1>>\nstream\n\x80\nendstream" % _FONT_RESOURCES,
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R"
            b"/Resources<</XObject<</X0 4 0 R>>>>>>",
        )
    )
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(ExtractorError, match=r"^PDF carries a LZW stream"),
    ):
        extract_pdf_text(payload)
    assert not [r for r in caplog.records if "XFormObject" in r.getMessage()]


def _glyphs_pdf(pages: int, glyphs: int, *, draws: int = 0) -> bytes:
    """``pages`` pages sharing one content stream that shows ``glyphs``
    glyphs, each where no other is, and draws an empty form ``draws``
    times."""
    shown = b"".join(
        b"1 0 0 1 %d %d Tm (%c) Tj\n"
        % (10 + glyph % 50 * 11, 10 + glyph // 50 * 9, 65 + glyph % 26)
        for glyph in range(glyphs)
    )
    content = b"BT /F1 9 Tf\n" + shown + b"ET\n" + b"/X0 Do\n" * draws
    kids = b" ".join(b"%d 0 R" % (5 + page) for page in range(pages))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[%s]/Count %d>>" % (kids, pages),
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
            b"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Length 0>>\nstream\n\nendstream",
            *(
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 9999]/Contents 3 0 R"
                b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1"
                b"/BaseFont/Helvetica>>>>/XObject<</X0 4 0 R>>>>>>",
            )
            * pages,
        )
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_what_pdfminer_lays_out_is_held_to_a_page_and_a_card(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Each glyph pdfplumber holds weighs about 3 kB, and the decode bound
    counted the bytes that drew them: a 6,5 kB card drawing a page of 488
    000 glyphs held 1,1 GB. pdfminer is held to what it lays out, on a page,
    forms drawn included, and on the whole card."""
    monkeypatch.setattr(_pdf_bound, "_MAX_PAGE_OBJECTS", 1000)
    monkeypatch.setattr(_pdf_bound, "_MAX_CARD_OBJECTS", 2000)
    refused = r"^PDF lays out more than 1000 objects on a page or 2000 in all"
    for payload in (_glyphs_pdf(1, 1001), _glyphs_pdf(1, 900, draws=101)):
        with pytest.raises(ExtractorError, match=refused):
            extract(payload)
    with pytest.raises(ExtractorError, match=refused):
        extract(_glyphs_pdf(3, 700))
    assert extract(_glyphs_pdf(2, 900, draws=100)).count("A") >= 2 * 900 // 26


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_what_pdfminer_holds_at_once_is_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer keeps operands waiting for their operator, graphics states
    saved, the pieces of an array being read and the segments of a path
    not yet painted, each growing with the bytes drawing it: 2 MB of saved
    states held 552 MB, and 400 kB of operands left waiting, each operator
    then copying the rest, took 157 s."""
    monkeypatch.setattr(_pdf_bound_pdfminer, "_MAX_STACK", 50)
    monkeypatch.setattr(_pdf_bound_pdfminer, "_MAX_NESTING", 3)
    monkeypatch.setattr(_pdf_bound_pdfminer, "_MAX_PATH_SEGMENTS", 100)

    def shapes(n: int, depth: int) -> tuple[bytes, ...]:
        return (
            b"0 " * n + b"Tz",
            b"q " * n,
            b"[" + b"0 " * n + b"] 0 d",
            b"[ " * depth + b"0 " + b"] " * depth + b"0 d",
            b"0 0 m " + b"1 1 l " * (2 * n) + b"S",
            b"0 0 m " + b"h " * (2 * n) + b"n",
        )

    for content in shapes(60, 4):
        with pytest.raises(ExtractorError, match=r"^PDF has pdfminer hold more"):
            extract(_pdf_of(content, attrs=b""))
    for content in shapes(40, 3):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(_pdf_of(content, attrs=b""))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_what_pdfminer_gathers_of_one_token_is_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer grows a token, a comment included, and the data of an inline
    image a byte at a time, copying what it holds each time, while the parse
    count sees each as one object at most: 1 MB of inline image data took
    352 s. The check is made
    as it reads through each buffer of 4 kB."""
    monkeypatch.setattr(_pdf_bound, "_MAX_TOKEN_BYTES", 10_000)

    def shapes(n: int) -> tuple[bytes, ...]:
        return (
            b"/T " + b"(" * n + b")" * n + b" DP",
            b"/T (" + b"\\(" * n + b") DP",
            b"%" + b"A" * 2 * n + b"\n",
            b"q BI /W 1 /H 1 ID " + b"E" * 2 * n + b" EI Q",
        )

    for content in shapes(15_000):
        with pytest.raises(ExtractorError, match=r"^PDF has pdfminer gather a token"):
            extract(_pdf_of(content, attrs=b""))
    for content in shapes(4000):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(_pdf_of(content, attrs=b""))


def _lined_pdf(stream: bytes, tail: bytes) -> bytes:
    """A page of text and of a stream saying it is empty but carrying
    ``stream`` before its end, the file followed by ``tail``."""
    text = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % (b"tariff card text " * 60)
    return (
        _pdf_from(
            (
                b"<</Type/Catalog/Pages 2 0 R>>",
                b"<</Type/Pages/Kids[5 0 R]/Count 1>>",
                b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text),
                b"<</Length 0>>\nstream\n%sendstream" % stream,
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
                b"/Contents[3 0 R 4 0 R]" + _FONT_RESOURCES + b">>",
            )
        )
        + tail
    )


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_what_pdfminer_gathers_of_one_line_is_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer grows a line a buffer at a time, copying what it holds each
    time, reading it forwards, as it looks for the end of a stream whose
    length is wrong, or backwards, from the end of the file: 16 MB with no
    line end took 46,8 s on a Raspberry Pi. A line read forwards is held to the longest token;
    one read backwards is found in the file whole, and counts as parsed:
    2 MB of line ends there took 4,7 s."""
    monkeypatch.setattr(_pdf_bound, "_MAX_TOKEN_BYTES", 10_000)
    monkeypatch.setattr(_pdf_bound, "_MAX_PARSED", 5000)
    with pytest.raises(ExtractorError, match=r"^PDF has pdfminer gather a token"):
        extract(_lined_pdf(b"x" * 15_000, b""))
    with pytest.raises(ExtractorError, match=r"^PDF has a reader parse more"):
        extract(_lined_pdf(b"", b"\n" * 6000))
    for stream, tail in ((b"x" * 4000, b"x" * 15_000), (b"", b"\n" * 1000)):
        assert "tariff card" in extract(_lined_pdf(stream, tail))


def test_the_lines_read_backwards_are_pdfminer_s() -> None:
    """Found in the file whole, the lines pdfminer reads backwards are the
    ones it found a buffer at a time."""
    import io

    from pdfminer.psparser import PSBaseParser

    _pdf_bound_pdfminer._hold_pdfminer_to_the_bound()
    for data in (
        b"",
        b"a",
        b"a\n",
        b"\n\n",
        b"a\r\nb\rc\n\r",
        b"a\rb\rc",
        b"\n\r" * 3 + b"x",
        b"x" * 9000 + b"\r\n" + b"y" * 5000 + b"\n" * 3,
    ):
        expected = list(PSBaseParser(io.BytesIO(data)).revreadlines())
        with _pdf_bound_pdfminer.bounded_pdfminer():
            assert list(PSBaseParser(io.BytesIO(data)).revreadlines()) == expected


def test_the_lines_read_backwards_are_found_in_a_time_linear_in_the_file() -> None:
    """Each line end was looked for afresh for every line, an LF not there
    through the whole file each time: 256 kB of CR line ends took 2,4 s on
    a Raspberry Pi and 1 MB 55 s. What is looked through is counted here,
    no more than twice the file whatever its line ends."""
    import io

    from pdfminer.psparser import PSBaseParser

    looked = 0

    class Looked(bytes):
        def rfind(self, sub: Any, start: Any = 0, end: Any = None) -> int:  # type: ignore[override]
            nonlocal looked
            end = len(self) if end is None else end
            found = super().rfind(sub, start, end)
            looked += end - max(found, start)
            return found

    class File(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return Looked(super().read(size))

    for line in (b"a\r", b"a\n", b"a\r\n", b"\n\r"):
        data = line * 20_000
        looked = 0
        with _pdf_bound_pdfminer.bounded_pdfminer():
            lines = list(PSBaseParser(File(data)).revreadlines())
        assert lines == list(PSBaseParser(io.BytesIO(data)).revreadlines())
        assert looked <= 2 * len(data), (line, looked)


def test_the_lines_pypdf_reads_backwards_are_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pypdf reads a block of 8 kB for each line it reads backwards from the
    end of the file, looking for its end marker: a file of short lines with
    no end marker took about 9 s a MB on a Raspberry Pi, nothing counting
    it. Each line counts."""
    monkeypatch.setattr(_pdf_bound, "_MAX_LINES_BACK", 100)
    with pytest.raises(ExtractorError, match=r"^PDF has pypdf read more than 100"):
        extract_pdf_text(_lined_pdf(b"", b"x\n" * 150))
    assert "tariff card" in extract_pdf_text(_lined_pdf(b"", b"x\n" * 50))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_the_cmaps_pdfminer_loads_of_its_own_are_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer keeps each CMap of its own it loads for good: a 3 kB card
    whose ToUnicode map named all 144 took 2,9 s and kept 135 MB. Each one
    named counts, loaded already or not."""
    monkeypatch.setattr(_pdf_bound_pdfminer, "_MAX_CMAPS", 2)
    text = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % (b"tariff card text " * 60)

    def card(*cmaps: bytes) -> bytes:
        used = b"".join(b"/%s usecmap\n" % name for name in cmaps)
        unicode = b"begincmap\n%sendcmap\n" % used
        return _pdf_from(
            (
                b"<</Type/Catalog/Pages 2 0 R>>",
                b"<</Type/Pages/Kids[5 0 R]/Count 1>>",
                b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text),
                b"<</Length %d>>\nstream\n%s\nendstream" % (len(unicode), unicode),
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R"
                b"/Resources<</Font<</F1<</Type/Font/Subtype/Type1"
                b"/BaseFont/Helvetica/ToUnicode 4 0 R>>>>>>>>",
            )
        )

    with pytest.raises(ExtractorError, match=r"^PDF has pdfminer load more than 2"):
        extract(card(b"UniJIS-UCS2-H", b"UniGB-UCS2-H", b"UniKS-UCS2-H"))
    assert "tariff card" in extract(card(b"UniJIS-UCS2-H", b"UniGB-UCS2-H"))


@pytest.mark.parametrize("most", [4, 1])
def test_a_read_leaves_no_cmap_of_pdfminer_s_behind(
    monkeypatch: pytest.MonkeyPatch, most: int
) -> None:
    """pdfminer keeps each CMap of its own it loads under the name the card
    gives, which it reads the same file for whatever NUL bytes it carries,
    so each read of a card naming new ones kept 40 MB more. What a read
    added to its caches is dropped once it ends, refused or not."""
    from pdfminer.cmapdb import CMapDB

    monkeypatch.setattr(_pdf_bound_pdfminer, "_MAX_CMAPS", most)
    text = b"BT /F0 12 Tf 72 720 Td (%s) Tj ET" % (b"tariff card text " * 60)
    unicode = b"begincmap\n/UniJIS-UCS2-H#00 usecmap\nendcmap\n"
    payload = _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[5 0 R]/Count 1>>",
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text),
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(unicode), unicode),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R"
            b"/Resources<</Font<</F0<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"
            b"/F1<</Type/Font/Subtype/Type1/BaseFont/Helvetica/ToUnicode 4 0 R>>"
            b"/F2<</Type/Font/Subtype/Type0/BaseFont/X/Encoding/Identity-H"
            b"/DescendantFonts[<</Type/Font/Subtype/CIDFontType2/BaseFont/X"
            b"/CIDSystemInfo<</Registry(Adobe)/Ordering(Japan1\\000)/Supplement 0>>"
            b">>]>>>>>>>>",
        )
    )
    kept = (set(CMapDB._cmap_cache), set(CMapDB._umap_cache))
    load = CMapDB._load_data.__func__  # type: ignore[attr-defined]
    loads: list[str] = []

    def counted(cls: type[CMapDB], name: str) -> Any:
        loads.append(name)
        return load(cls, name)

    monkeypatch.setattr(CMapDB, "_load_data", classmethod(counted))
    if most > 1:
        assert "tariff card" in extract_pdf_text_layout(payload)
    else:
        with pytest.raises(ExtractorError, match=r"^PDF has pdfminer load more than 1"):
            extract_pdf_text_layout(payload)
    assert loads == ["UniJIS-UCS2-H\0", "to-unicode-Adobe-Japan1\0"]
    assert (set(CMapDB._cmap_cache), set(CMapDB._umap_cache)) == kept


def _named_pdf(names: int, tag: bytes) -> bytes:
    """A card of text followed by ``names`` names and as many keywords, each
    new, ``tag`` telling them from another card's."""
    named = b"".join(b" /%s%d %s%d" % (tag, n, tag.lower(), n) for n in range(names))
    text = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % (b"tariff card text " * 60)
    return _pdf_of(text + named, attrs=b"")


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_read_leaves_no_name_of_pdfminer_s_behind(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer keeps each name and keyword it makes for good, so that one
    read twice is the same object: each card naming new ones grew its
    tables, 100 000 of them by about 20 MB, refused or not. Those a read
    makes are kept with the read alone, at most _MAX_SYMBOLS."""
    from pdfminer.psparser import PSKeywordTable, PSLiteralTable

    _pdf_bound_pdfminer._hold_pdfminer_to_the_bound()
    kept = (set(PSLiteralTable.dict), set(PSKeywordTable.dict))
    monkeypatch.setattr(_pdf_bound_pdfminer, "_MAX_SYMBOLS", 1000)
    assert "tariff card" in extract(_named_pdf(300, b"Kept"))
    with pytest.raises(ExtractorError, match=r"^PDF has pdfminer make more than 1000"):
        extract(_named_pdf(600, b"Refused"))
    assert (set(PSLiteralTable.dict), set(PSKeywordTable.dict)) == kept


def test_pdfminer_makes_a_name_once_a_read() -> None:
    """pdfminer compares names and keywords by identity, those it makes as
    it reads with those it makes in its code, as the keyword after a
    file's cross-reference table, so a name a read makes is the same
    object all through it, and made again for the next."""
    from pdfminer.psparser import KWD, LIT, PSLiteralTable

    with _pdf_bound_pdfminer.bounded_pdfminer():
        made = LIT("Made once a read")
        assert LIT("Made once a read") is made
        assert KWD(b"Made once a read") is not LIT(b"Made once a read")
    with _pdf_bound_pdfminer.bounded_pdfminer():
        assert LIT("Made once a read") is not made
    assert "Made once a read" not in PSLiteralTable.dict


def _drawn_pdf(content: bytes, form: bytes, *, fonts: int = 1) -> bytes:
    """A page of ``content`` that may draw the form ``X0`` of ``form``,
    which names no resources of its own, both naming ``fonts`` fonts."""
    named = b"/Font<<%s>>" % b"".join(b"/F%d 3 0 R" % n for n in range(fonts))
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[6 0 R]/Count 1>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
            b"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]/Resources<<%s>>"
            b"/Length %d>>\nstream\n%s\nendstream" % (named, len(form), form),
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 5 0 R"
            b"/Resources<<%s/XObject<</X0 4 0 R>>>>>>" % named,
        )
    )


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_what_a_reader_parses_is_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """What the bytes draw cost even when it builds nothing: 78 kB of
    numbers no operator takes held pypdf 238 s and 1,2 GB, and 3 MB of
    saved and restored states took pdfminer 56 s. Each operand and
    operator counts as the reader parses it, a form's each time it is
    drawn."""
    monkeypatch.setattr(_pdf_bound, "_MAX_PARSED", 5000)
    refused = r"^PDF has a reader parse more than 5000 objects"
    for content, form in (
        (b"q Q " * 3000, b""),
        (b"0 " * 6000, b""),
        (b"/X0 Do " * 30, b"q Q " * 100),
    ):
        with pytest.raises(ExtractorError, match=refused):
            extract(_drawn_pdf(content, form))
    for content, form in (
        (b"q Q " * 1000, b""),
        (b"0 " * 2000, b""),
        (b"/X0 Do " * 10, b"q Q " * 100),
    ):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(_drawn_pdf(content, form))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_the_resources_pdfminer_walks_for_each_draw_count(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer walks a form's resources each time the form is drawn,
    looking up each font: a few bytes drawing a form again walk them all
    again."""
    monkeypatch.setattr(_pdf_bound, "_MAX_PARSED", 5000)
    with pytest.raises(ExtractorError, match=r"^PDF has a reader parse more than"):
        extract(_drawn_pdf(b"/X0 Do " * 20, b"", fonts=300))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_drawn_pdf(b"/X0 Do " * 3, b"", fonts=300))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_only_the_resources_pdfminer_walks_are_counted(
    extract: Callable[[bytes], str],
) -> None:
    """Counting a kind of resources pdfminer never reads, here graphics
    states kept in an object stream it cannot decode, read them, and the
    card failed on them."""
    content = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % (b"tariff card text " * 60)
    payload = _packed_pdf(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
            b"/Resources<</Font<</F1 5 0 R>>/ExtGState 6 0 R>>>>",
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
        ),
        b"<</GS0<</LW 1>>>>",
        filter=b"/FooDecode",
    )
    assert "tariff card" in extract(payload)


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_reference_to_itself_is_not_resolved_for_good(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """An object that is a reference to itself had pdfminer resolve it
    forever, from what it had parsed already, wherever the resources named
    it: a 700-byte card hung the reader for good. Each reference resolved
    counts as parsed."""
    monkeypatch.setattr(_pdf_bound, "_MAX_PARSED", 5000)
    content = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % (b"tariff card text " * 60)
    for resources in (
        b"6 0 R",
        b"<</Font 6 0 R>>",
        b"<</Font<</F1 6 0 R>>>>",
        b"<</Font<</F1 3 0 R>>/XObject 6 0 R>>",
    ):
        payload = _pdf_from(
            (
                b"<</Type/Catalog/Pages 2 0 R>>",
                b"<</Type/Pages/Kids[5 0 R]/Count 1>>",
                b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
                b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
                b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
                b"/Resources %s>>" % resources,
                b"6 0 R",
            )
        )
        with pytest.raises(ExtractorError, match=r"^PDF has a reader parse more"):
            extract(payload)


def _mapped_pdf(last: int) -> bytes:
    """A font whose ToUnicode map gives the codes 0 to ``last`` in a line."""
    import zlib

    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"1 begincodespacerange <0000> <FFFF> endcodespacerange\n"
        b"1 beginbfrange\n<0000> <%04X> <0041>\nendbfrange\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end" % last
    )
    return _font_pdf(1, zlib.compress(cmap))


def _widths_pdf(last: int) -> bytes:
    """A page naming a font whose widths give the codes 0 to ``last`` in a
    range."""
    content = b"BT /F1 9 Tf 10 10 Td <0041> Tj ET"
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[4 0 R]/Count 1>>",
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R"
            b"/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Type/Font/Subtype/Type0/BaseFont/F/Encoding/Identity-H/DescendantFonts[6 0 R]>>",
            b"<</Type/Font/Subtype/CIDFontType2/BaseFont/F/FontDescriptor 7 0 R"
            b"/CIDSystemInfo<</Registry(Adobe)/Ordering(Identity)/Supplement 0>>"
            b"/W[0 %d 500]>>" % last,
            b"<</Type/FontDescriptor/FontName/F/Flags 4/FontBBox[0 0 1000 1000]"
            b"/ItalicAngle 0/Ascent 800/Descent -200/CapHeight 700/StemV 80>>",
        )
    )


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_what_a_reader_builds_of_fonts_is_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A font's map or widths give a range of codes in a line, and the
    readers build each: 2,8 kB mapping 2^32 codes ran pdfminer out of
    memory after 52 s, and pypdf held 970 MB for 48 fonts of the 100 000
    codes it maps at most for one. Each code counts before the range is
    gone through."""
    monkeypatch.setattr(_pdf_bound, "_MAX_FONT_ENTRIES", 10_000)
    refused = r"^PDF has a reader build more than 10000 font entries"
    for payload in (_mapped_pdf(0xFFFF), _widths_pdf(60_000)):
        with pytest.raises(ExtractorError, match=refused):
            extract(payload)
    for payload in (_mapped_pdf(0xFF), _widths_pdf(255)):
        with pytest.raises(CardNotReadableError, match="no text layer"):
            extract(payload)


def _ranged_pdf(codes: int, characters: int) -> bytes:
    """A font whose ToUnicode map gives ``codes`` codes in a range, below
    the ones the page shows, ``characters`` characters each."""
    import zlib

    cmap = (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        b"1 beginbfrange\n<00> <%02X> <%s>\nendbfrange\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
        % (codes - 1, ("A" * characters).encode("utf-16-be").hex().encode())
    )
    return _font_pdf(1, zlib.compress(cmap))


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_map_entry_counts_each_character_it_maps_to(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """A range gives each of its codes the characters its line names, up to
    4 000 under pdfminer: a 1 kB card holding one font of 65 536 codes so
    mapped held 500 MB, and the codes alone let 15 such fonts through. An
    entry counts once more for each character past its first. pypdf takes
    no more than 8 characters for a code of a range, and refuses the line
    under its own words."""
    monkeypatch.setattr(_pdf_bound, "_MAX_FONT_ENTRIES", 10_000)
    with pytest.raises(
        ExtractorError, match=r"^PDF has a reader build more than 10000 font entries"
    ):
        extract(_ranged_pdf(10, 2000))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_ranged_pdf(4, 2000))


def _lines_pdf(fonts: bytes, lines: Sequence[bytes], *extra: bytes) -> bytes:
    """A page showing each of ``lines``, a string, on a line of its own in
    its font ``/F1``, its resources naming ``fonts``, then the ``extra``
    objects, numbered from 5."""
    content = (
        b"BT /F1 5 Tf 6 TL 10 780 Td " + b" T* ".join(b"(%s) Tj" % line for line in lines) + b" ET"
    )
    return _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
            b"/Resources<</Font<<%s>>>>>>" % fonts,
            b"<</Length %d>>\nstream\n%s\nendstream" % (len(content), content),
            *extra,
        )
    )


def _glyphs_of_one_code(glyphs: int) -> list[bytes]:
    """``glyphs`` glyphs of the code 1, a hundred to a line. Their font is
    none of the standard ones, whose own widths pdfminer would take over
    the card's, setting each glyph on the one before."""
    return [b"\x01" * min(100, glyphs - at) for at in range(0, glyphs, 100)]


def _mapped_glyphs_pdf(glyphs: int, characters: int) -> bytes:
    """A page showing ``glyphs`` glyphs of one code, which its font's
    ToUnicode map gives ``characters`` characters."""
    import zlib

    cmap = zlib.compress(
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        b"1 beginbfchar\n<01> <%s>\nendbfchar\n"
        b"endcmap CMapName currentdict /CMap defineresource pop end end"
        % ("A" * characters).encode("utf-16-be").hex().encode()
    )
    return _lines_pdf(
        b"/F1<</Type/Font/Subtype/Type1/BaseFont/CardFont/FirstChar 1"
        b"/LastChar 1/Widths[600]/ToUnicode 5 0 R>>",
        _glyphs_of_one_code(glyphs),
        b"<</Filter/FlateDecode/Length %d>>\nstream\n" % len(cmap) + cmap + b"\nendstream",
    )


def _named_glyphs_pdf(glyphs: int, name: bytes) -> bytes:
    """A page showing ``glyphs`` glyphs of one code, which its font's
    encoding names ``name``."""
    return _lines_pdf(
        b"/F1<</Type/Font/Subtype/Type1/BaseFont/CardFont/FirstChar 1"
        b"/LastChar 1/Widths[600]/Encoding<</Type/Encoding/Differences[1/%s]>>>>" % name,
        _glyphs_of_one_code(glyphs),
    )


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_glyph_counts_each_character_its_font_maps_it_to(
    extract: Callable[[bytes], str],
) -> None:
    """The bounds counted glyphs, not the characters a glyph shows: a
    ToUnicode map gives a code up to 4 000 of them under pdfminer and 256
    under pypdf, and an encoding gives it a glyph name as long as the card
    writes it, which pypdf takes for the text and pdfminer reads as the
    characters it names. A 4,8 kB card had pypdf make 1 024 000 000
    characters and hold 1,1 GB, and a 1 kB card had pdfplumber lay out
    32 000 000 and hold 2,7 GB. Each character counts as it is made, pypdf
    joining what a string's codes give only once it has looked them all
    up."""
    refused = r"^PDF has a reader make more than 100000 characters of text"
    name = b"uni" + b"0041" * 1000
    for payload in (_mapped_glyphs_pdf(600, 200), _named_glyphs_pdf(120, name)):
        with pytest.raises(ExtractorError, match=refused):
            extract(payload)
    for payload in (_mapped_glyphs_pdf(400, 200), _named_glyphs_pdf(10, name)):
        assert len(extract(payload)) >= 10_000


def test_a_font_pypdf_stands_in_for_counts_what_it_shows() -> None:
    """pypdf shows the codes of a font the page names but lacks through one
    it makes for it, a character for each."""
    other = b"/F2<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"
    lines = [b"a" * 1000] * 101
    with pytest.raises(ExtractorError, match=r"^PDF has a reader make more than 100000 characters"):
        extract_pdf_text(_lines_pdf(other, lines))
    assert len(extract_pdf_text(_lines_pdf(other, lines[:99]))) >= 99_000


def test_what_pypdf_puts_between_strings_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move to the right has pypdf put a space in, and one down end the
    line, with no glyph shown, so moves alone grew a page by a character
    each, every line ended copying the page: 80 000 pairs of them took 15 s
    on a Raspberry Pi. Each counts as a character made."""
    monkeypatch.setattr(_pdf_bound, "_MAX_CHARACTERS", 10_000)

    def moved(pairs: int) -> bytes:
        content = b"BT /F1 9 Tf 10 700 Td (a) Tj\n" + b"20 0 Td 0 -20 Td\n" * pairs
        return _pdf_of(content + b"ET", attrs=b"")

    with pytest.raises(ExtractorError, match=r"^PDF has a reader make more than 10000 characters"):
        extract_pdf_text(moved(6000))
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract_pdf_text(moved(4000))


@pytest.mark.parametrize(
    "name",
    [
        "mega/Mega-FR-NG-B2C-WL-092026-Cosy0809.pdf",
        "luminus/LUMINUS_PL_202609_ZGR2A0_FR_FLA_ComfyFlex_Gas_1_year_Online_Sales_Luminusbe.pdf",
    ],
)
def test_pypdf_checks_a_move_against_the_page_s_last_character(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Checking each move or string shown for the line or word it starts,
    pypdf made the page and the line it was building one string to take
    its last character, copying the page each time: on a page holding
    400 000 characters, 20 000 moves that went nowhere took 6,4 s on a
    Raspberry Pi. It is handed that character alone while a card is read,
    and a real card reads as pypdf reads it with the whole page."""
    import io

    import pypdf
    from pypdf import _text_extraction

    from tests import FIXTURES

    extractor = vars(_pdf_bound.pypdf_extractor)
    assert extractor["crlf_space_check"].__qualname__.startswith("_tail_only.")
    payload = (FIXTURES / name).read_bytes()
    # Outside a read the check is handed the whole page, as pypdf does.
    whole = "\n".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(payload)).pages)
    handed: list[int] = []

    def check(*args: Any) -> Any:
        handed.append(len(args[6]))
        return _text_extraction.crlf_space_check(*args)

    monkeypatch.setitem(extractor, "crlf_space_check", _pdf_bound._tail_only(check))
    assert extract_pdf_text(payload) == whole
    assert handed and max(handed) <= 1


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_read_whose_bound_counts_no_characters_reads_as_before(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """The electricity integration reads through the same wrappers, and a
    read its copy of the bound started may come from a release that counts
    no characters, 0.44.33 and before: the count passes such a read by,
    where it raised."""
    from tests import FIXTURES

    payload = (
        FIXTURES / "sparki" / "Sparki_Tariefkaart_augustus_Particulier_SelfService_Gas_NL.pdf"
    ).read_bytes()
    expected = extract(payload)
    monkeypatch.delattr(_pdf_bound._Reading, "show")
    assert extract(payload) == expected


def test_the_character_count_is_put_over_wrappers_that_lack_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release of either integration from before the count may have put
    the other wrappers in place, which both integrations then read through:
    the count goes in over them, once, and leaves them as they are."""
    from pdfminer import cmapdb
    from pdfminer.layout import LTChar
    from pdfminer.pdftypes import PDFStream
    from pypdf import _text_extraction
    from pypdf.generic import EncodedStreamObject

    _pdf_bound_pdfminer._hold_pdfminer_to_the_bound()
    others = (EncodedStreamObject.get_data, PDFStream.get_data)
    font = _pdf_bound.pypdf_font.Font
    extractor = vars(_pdf_bound.pypdf_extractor)

    def unmarked(method: Any) -> Any:
        def bare(*args: Any, **kwargs: Any) -> Any:
            return method(*args, **kwargs)

        return bare

    for owner, name in (
        (font, "__init__"),
        (LTChar, "__init__"),
        (cmapdb.FileUnicodeMap, "add_cid2unichr"),
    ):
        monkeypatch.setattr(owner, name, unmarked(getattr(owner, name)))
    pypdf_check = _text_extraction.crlf_space_check
    monkeypatch.setitem(extractor, "crlf_space_check", pypdf_check)

    def counting() -> tuple[Any, ...]:
        _pdf_bound._hold_pypdf_to_the_bound()
        _pdf_bound_pdfminer._hold_pdfminer_to_the_bound()
        assert (EncodedStreamObject.get_data, PDFStream.get_data) == others
        return (font.__init__, LTChar.__init__, extractor["crlf_space_check"])

    first = counting()
    assert all(hasattr(method, _pdf_bound._MARK) for method in first[:2])
    assert first[2] is not pypdf_check
    assert first[2].__qualname__.startswith("_tail_only.")
    assert counting() == first


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_font_written_into_a_form_is_built_once_a_read(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer keeps a font it finds by reference, but builds one written
    into the resources again for each draw, its widths with it, which no
    count saw: 100 000 widths drawn 400 times took 46,7 s."""
    from pdfminer import pdffont

    built: list[object] = []
    init = pdffont.PDFType1Font.__init__

    def counted(font: Any, *args: Any) -> None:
        built.append(font)
        init(font, *args)

    monkeypatch.setattr(pdffont.PDFType1Font, "__init__", counted)
    content = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET " % (b"tariff card text " * 60)
    font = b"<</Type/Font/Subtype/Type1/BaseFont/Custom/Widths[%s]>>" % (b"0 " * 500)
    payload = _pdf_from(
        (
            b"<</Type/Catalog/Pages 2 0 R>>",
            b"<</Type/Pages/Kids[6 0 R]/Count 1>>",
            b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
            b"<</Type/XObject/Subtype/Form/BBox[0 0 612 792]"
            b"/Resources<</Font<</F1 %s>>>>/Length 1>>\nstream\nn\nendstream" % font,
            b"<</Length %d>>\nstream\n%s\nendstream"
            % (len(content) + 50 * 7, content + b"/X0 Do " * 50),
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 5 0 R"
            b"/Resources<</Font<</F1 3 0 R>>/XObject<</X0 4 0 R>>>>>>",
        )
    )
    assert "tariff card" in extract(payload)
    assert len(built) == 2


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_a_font_a_page_names_but_lacks_is_built_once_a_read(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer builds a font of a new empty dictionary for each font a
    page names but lacks: naming one cost 146 us a token on a Raspberry Pi,
    four times what a token of a Bolt card costs."""
    from pdfminer import pdffont

    built: list[object] = []
    init = pdffont.PDFType1Font.__init__

    def counted(font: Any, *args: Any) -> None:
        built.append(font)
        init(font, *args)

    monkeypatch.setattr(pdffont.PDFType1Font, "__init__", counted)
    content = b"BT /F1 12 Tf 72 720 Td (%s) Tj %s ET" % (
        b"tariff card text " * 60,
        b"/F9 1 Tf " * 100,
    )
    assert "tariff card" in extract(_pdf_of(content, attrs=b""))
    assert len(built) == 2


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_the_steps_pdfminer_takes_are_held(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfminer takes a step of its own for each NUL byte, lone ">" and
    escape in a string or a name, which no token counts: 2 MB of them took
    7,5 s on a Raspberry Pi, in the file itself held only by the download
    cap. Each step counts."""
    monkeypatch.setattr(_pdf_bound, "_MAX_STEPS", 20_000)
    text = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET " % (b"tariff card text " * 60)

    def shapes(n: int) -> tuple[bytes, ...]:
        return (
            _pdf_of(text + b"\0" * n, attrs=b""),
            _pdf_of(text + b"> " * n, attrs=b""),
            _pdf_of(text + (b"(" + b"\\n" * 1000 + b") pop ") * (n // 1000), attrs=b""),
            _pdf_from(
                (
                    b"<</Type/Catalog/Pages 2 0 R>>",
                    b"<</Type/Pages/Kids[4 0 R]/Count 1>>",
                    b"<</Length %d>>\nstream\n%s\nendstream" % (len(text), text),
                    b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 3 0 R "
                    + b"\0" * n
                    + _FONT_RESOURCES
                    + b">>",
                )
            ),
        )

    for payload in shapes(30_000):
        with pytest.raises(ExtractorError, match=r"^PDF has pdfminer take more than"):
            extract(payload)
    for payload in shapes(5_000):
        assert "tariff card" in extract(payload)


@pytest.mark.parametrize("extract", _EVERY_READER)
def test_a_card_is_held_to_a_page_count(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """Each page costs pdfplumber about 15 ms with nothing on it, and pypdf
    1,4 ms: a 2 MB file of 20 000 empty pages took 302 s. The count stops
    the reader listing them, before it holds them all."""
    from pdfminer.pdfpage import PDFPage
    from pypdf import PageObject

    # Held first, so the counting below wraps the hold, not the reverse.
    _pdf_bound_pdfminer._hold_pdfminer_to_the_bound()
    monkeypatch.setattr(_pdf_bound, "_MAX_PAGES", 3)
    pages: list[object] = []
    page_object = PageObject.__init__

    def listed(page: PageObject, *args: Any, **kwargs: Any) -> None:
        pages.append(page)
        page_object(page, *args, **kwargs)

    create_pages = PDFPage.create_pages

    def created(document: object) -> Iterator[PDFPage]:
        for page in create_pages(document):  # type: ignore[arg-type]
            pages.append(page)
            yield page

    monkeypatch.setattr(PageObject, "__init__", listed)
    monkeypatch.setattr(PDFPage, "create_pages", created)
    with pytest.raises(ExtractorError, match=r"^PDF has more than 3 pages"):
        extract(_pdf_of(b"", attrs=b"", pages=200))
    # pdfplumber lists them a second time to close them.
    assert len(pages) <= 2 * 3
    with pytest.raises(CardNotReadableError, match="no text layer"):
        extract(_pdf_of(b"", attrs=b"", pages=3))


def test_pdfminer_lays_out_no_paths_while_a_card_is_read() -> None:
    """A card's text is read off its glyphs alone, and its paths were most
    of what a page held: 123 000 segments on a page of a Bolt card."""
    import io

    import pdfplumber

    payload = _pdf_of(b"0 0 m 10 10 l S 0 0 10 10 re f", attrs=b"")
    with (
        _pdf_bound_pdfminer.bounded_pdfminer(),
        pdfplumber.open(io.BytesIO(payload)) as pdf,
    ):
        assert not pdf.pages[0].lines and not pdf.pages[0].rects
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        assert pdf.pages[0].lines and pdf.pages[0].rects


def test_a_second_copy_of_the_bound_reads_through_the_same_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live check loads the package again under a name of its own, and
    that copy wrapped the readers again on top of the first, which then
    wrapped them again in turn: a card's charges and objects were counted
    four times over after a few reads. The copy reads through the wrappers
    in place, and is held to its own bound all the same."""
    import importlib.util
    import io
    import sys

    import pdfplumber
    import pypdf
    from pdfminer.pdftypes import PDFStream
    from pdfminer.psparser import PSLiteralTable
    from pypdf.generic import EncodedStreamObject

    _pdf_bound_pdfminer._hold_pdfminer_to_the_bound()
    wrapped = (PDFStream.get_data, EncodedStreamObject.get_data)

    def load(module: Any) -> Any:
        name = f"{module.__name__}_copy"
        spec = importlib.util.spec_from_file_location(name, module.__file__)
        assert spec is not None and spec.loader is not None
        copy = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, copy)
        spec.loader.exec_module(copy)
        return copy

    copy = load(_pdf_bound)
    # The copy of pdfminer's wrappers imports the copy of the bound, as in
    # a package loaded again.
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, _pdf_bound.__name__, copy)
        miner = load(_pdf_bound_pdfminer)
    miner._hold_pdfminer_to_the_bound()
    assert (PDFStream.get_data, EncodedStreamObject.get_data) == wrapped
    assert type(PSLiteralTable.dict) is _pdf_bound_pdfminer._Symbols
    assert PSLiteralTable.klass == PSLiteralTable.dict.made
    assert miner._READING is copy._READING is _pdf_bound._READING
    monkeypatch.setattr(copy, "_MAX_DECODED_STREAM_BYTES", 1 << 20)
    payload = _inflating_pdf(2 << 20)
    refused = r"^PDF streams decode to more than 1048576 bytes"
    with pytest.raises(ExtractorError, match=refused), copy.bounded_pypdf():
        pypdf.PdfReader(io.BytesIO(payload)).pages[0].extract_text()
    with (
        pytest.raises(ExtractorError, match=refused),
        miner.bounded_pdfminer(),
        pdfplumber.open(io.BytesIO(payload)) as pdf,
    ):
        pdf.pages[0].extract_text()


@pytest.mark.parametrize("extract", [extract_pdf_text_layout, extract_pdf_text_rows])
def test_each_page_is_let_go_of_once_read(
    monkeypatch: pytest.MonkeyPatch, extract: Callable[[bytes], str]
) -> None:
    """pdfplumber holds every page it laid out until the document is closed,
    so what a card held grew with each page."""
    from pdfplumber.page import Page

    close = Page.close
    order: list[str] = []

    def closed(page: Page) -> None:
        order.append(f"close {page.page_number}")
        close(page)

    layout = Page.layout

    def laid_out(page: Page) -> object:
        if not hasattr(page, "_layout"):
            order.append(f"layout {page.page_number}")
        return layout.fget(page)  # type: ignore[attr-defined]

    monkeypatch.setattr(Page, "close", closed)
    monkeypatch.setattr(Page, "layout", property(laid_out))
    extract(_glyphs_pdf(3, 700))
    assert order[:6] == [
        "layout 1",
        "close 1",
        "layout 2",
        "close 2",
        "layout 3",
        "close 3",
    ]


class _Glyph(dict[str, object]):
    """A glyph pdfplumber can dedupe that counts how often it is compared."""

    compared = 0

    def __eq__(self, other: object) -> bool:
        _Glyph.compared += 1
        return super().__eq__(other)


def test_dedupe_finds_each_glyph_kept_at_once() -> None:
    """pdfplumber puts the glyphs it keeps back in order by looking each one
    up in the page's list: 40 000 glyphs on a page took 303 s. The order,
    duplicates dropped, is as pdfplumber has it."""
    from pdfplumber import utils

    glyphs: list[dict[str, object]] = [
        _Glyph(
            upright=True,
            text=chr(65 + n % 26),
            fontname="F",
            size=9.0,
            doctop=float(n // 50 * 9),
            x0=float(n % 50 * 11),
        )
        for n in range(2000)
    ]
    glyphs[10:10] = [_Glyph(glyphs[5]), _Glyph(glyphs[700])]
    _Glyph.compared = 0
    expected = utils.dedupe_chars(glyphs)
    assert _Glyph.compared > 1_000_000
    _Glyph.compared = 0
    with _pdf_bound_pdfminer.bounded_pdfminer():
        kept = utils.dedupe_chars(glyphs)
    assert _Glyph.compared < 10_000
    assert len(kept) == 2000
    assert [id(glyph) for glyph in kept] == [id(glyph) for glyph in expected]
