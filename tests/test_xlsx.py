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

"""xlsx.py: a workbook's cells, its zip held to what a workbook's is."""

from __future__ import annotations

import io
import struct
import time
import tracemalloc
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from custom_components.be_gas_prices import xlsx

_FLUVIUS = (Path(__file__).parent / "fixtures" / "fluvius" / "gas_antwerpen_2026.xlsx").read_bytes()
_SHEET = "xl/worksheets/sheet1.xml"
# Fluvius's cap on what its sheets unpack to.
_CAP = 4 * 1024 * 1024


def _rewritten(workbook: bytes, sheet: bytes, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    """``workbook`` with its first sheet's member holding ``sheet``, written
    with ``method``."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(workbook)) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for info in src.infolist():
            if info.filename == _SHEET:
                dst.writestr(_SHEET, sheet, compress_type=method)
            else:
                dst.writestr(info, src.read(info))
    return out.getvalue()


def _declaring(workbook: bytes, size: int) -> bytes:
    """``workbook`` with its first sheet's unpacked size declared as
    ``size``, in its local header and in the directory."""
    with zipfile.ZipFile(io.BytesIO(workbook)) as z:
        offset = z.getinfo(_SHEET).header_offset
    data = bytearray(workbook)
    struct.pack_into("<L", data, offset + 22, size)
    name = _SHEET.encode()
    entry = data.find(b"PK\x01\x02")
    while data[entry + 46 : entry + 46 + len(name)] != name:
        entry = data.find(b"PK\x01\x02", entry + 4)
    struct.pack_into("<L", data, entry + 24, size)
    return bytes(data)


def _directory(entries: int, extra: int) -> bytes:
    """A zip directory of ``entries`` entries, each with an ``extra`` byte
    long extra field of empty records, and the record ending it."""
    one = struct.pack(
        "<4s4B4HL2L5H2L", b"PK\x01\x02", 20, 0, 20, 0, 0, 0, 0, 0, 0, 0, 0, 1, extra, 0, 0, 0, 0, 0
    )
    directory = (one + b"a" + b"\x99\x99\x00\x00" * (extra // 4)) * entries
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, entries, entries, len(directory), 0, 0)
    return directory + end


def _peak(call: Callable[[], Any]) -> int:
    """The most memory ``call`` held at once, in bytes."""
    tracemalloc.start()
    try:
        call()
    finally:
        _now, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return peak


def test_a_real_workbook_reads() -> None:
    assert any(
        "Jaaropname" in text
        for row in xlsx.rows(_FLUVIUS, max_unpacked=_CAP)
        for text in row.values()
    )


def test_a_member_that_inflates_past_what_it_declares_is_held_to_the_cap() -> None:
    """A member declaring 1,000 bytes whose data inflates to 64 MiB: read
    whole, as ZipFile.read does before it finds the sizes differ, it held
    141 MiB at once. A read asks zlib for the cap and one byte, which its
    output may hold twice while it is joined."""
    lying = _declaring(_rewritten(_FLUVIUS, b" " * (64 * 1024 * 1024)), 1000)

    def read() -> None:
        with pytest.raises(xlsx.UNREADABLE):
            xlsx.rows(lying, max_unpacked=_CAP)

    assert _peak(read) < 4 * _CAP


@pytest.mark.parametrize(
    ("entries", "extra"),
    [(40, 65532), (2000, 0)],
    ids=["extra fields of 64 kB", "many entries"],
)
def test_a_directory_past_a_workbooks_is_refused_before_it_is_read(
    entries: int, extra: int
) -> None:
    """The zip reader builds every entry of the directory before anything
    can be checked, and decodes an extra field in time growing with the
    square of its length: forty 64 kB ones took 3,6 s."""
    payload = _directory(entries, extra)
    started = time.monotonic()
    with pytest.raises(ValueError, match="zip directory of"):
        xlsx.rows(payload, max_unpacked=_CAP)
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    "payload",
    [
        _FLUVIUS[:-2] + b"\x05\x00" + b"hello",
        _FLUVIUS[:-22] + b"PK\x06\x07" + bytes(16) + _FLUVIUS[-22:],
    ],
    ids=["a comment", "a ZIP64 locator"],
)
def test_a_zip_that_does_not_end_as_a_workbooks_is_refused(payload: bytes) -> None:
    """The zip reader would take the directory's size from elsewhere than
    the record ending the payload, which no workbook read here does."""
    with pytest.raises(zipfile.BadZipFile, match=r"zip end record|ZIP64"):
        xlsx.rows(payload, max_unpacked=_CAP)


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA], ids=["bzip2", "lzma"])
def test_a_member_compressed_as_no_workbook_is_refused(method: int) -> None:
    """Python 3.13 inflates bzip2 and LZMA members without a bound on one
    read; every workbook read here is stored or deflated."""
    with zipfile.ZipFile(io.BytesIO(_FLUVIUS)) as z:
        sheet = z.read(_SHEET)
    with pytest.raises(zipfile.BadZipFile, match="compressed with method"):
        xlsx.rows(_rewritten(_FLUVIUS, sheet, method), max_unpacked=_CAP)


def test_what_the_members_inflate_to_is_counted_against_one_cap() -> None:
    """Whatever the directory declares: once the shared strings are read,
    the sheet past what is left of the cap is refused."""
    with zipfile.ZipFile(io.BytesIO(_FLUVIUS)) as z:
        shared = len(z.read("xl/sharedStrings.xml"))
        members = xlsx._Members(z, shared + 100)
        assert len(members.read("xl/sharedStrings.xml")) == shared
        with pytest.raises(ValueError, match="past the size cap"):
            members.read(_SHEET)
