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

"""The cells of an XLSX workbook, as text.

For the regulated figures published as spreadsheets: Fluvius's gas tariff
sheets, the CWaPE's grids and FPS Finance's excise tariff. Read with the
standard library's zip reader and defusedxml, and a damaged archive raises
one of :data:`UNREADABLE`.

A workbook is remote content, so nothing in it is taken on trust: the zip's
directory is bounded before the zip reader parses it, and every member is
inflated no further than the cap on what the workbook unpacks to, counting
what each read gives rather than the sizes the directory declares.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from typing import Final

from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOC_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_FIRST_SHEET: Final = "xl/worksheets/sheet1.xml"

# The record that ends a zip, as the zip reader takes it when it is the last
# bytes with no comment, and the signature of the ZIP64 locator that would
# stand before it and give the directory's size instead. Every workbook the
# integration reads ends that way, with no ZIP64 record.
_END: Final = struct.Struct("<4s4H2LH")
_END_SIGNATURE: Final = b"PK\x05\x06"
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE: Final = 20
# The zip reader builds an object for every entry of the directory before
# anything can be checked, and decodes each entry's extra field in time
# growing with the square of its length: a 64 MiB directory of 1,4 million
# entries took 27,7 s and 690 MB, and forty entries with a 64 kB extra field
# each took 3,6 s. Of the workbooks read here, FPS Finance's excise tariff
# had the largest directory on 6 November 2026, 4 kB for 53 entries.
_MAX_DIRECTORY_BYTES: Final = 64 * 1024
# Stored and deflated, as every workbook read here is. The zip reader of
# Python 3.13 inflates the other methods it knows without a bound on a read.
_METHODS: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})

# What a payload that is not a readable workbook raises: not a zip, damaged
# or cut short, a part missing, or XML that does not parse or is refused.
UNREADABLE: Final = (
    ValueError,
    KeyError,
    IndexError,
    zipfile.BadZipFile,
    zlib.error,
    EOFError,
    ET.ParseError,
    DefusedXmlException,
)


def _check_directory(payload: bytes) -> None:
    """Refuse a payload whose zip directory is not a workbook's: one the zip
    reader would take from anywhere but a record ending the payload, or one
    past :data:`_MAX_DIRECTORY_BYTES`."""
    end = len(payload) - _END.size
    if end < 0:
        raise zipfile.BadZipFile("too short to be a zip")
    signature, _disk, _start, _here, _total, size, _offset, comment = _END.unpack_from(payload, end)
    if signature != _END_SIGNATURE or comment:
        raise zipfile.BadZipFile("no zip end record at the end of the payload")
    if end >= _ZIP64_LOCATOR_SIZE and payload.startswith(_ZIP64_LOCATOR, end - _ZIP64_LOCATOR_SIZE):
        raise zipfile.BadZipFile("a ZIP64 directory, which no workbook has")
    if size > _MAX_DIRECTORY_BYTES:
        raise ValueError(f"zip directory of {size} bytes, past the size cap")


class _Members:
    """The members of one workbook, read no further than ``cap`` bytes in
    all.

    Each is read through ``ZipFile.open``, asking for what is left of the
    cap and one byte more to tell a member past it, which is as far as zlib
    inflates it: ``ZipFile.read`` inflates a whole member before it finds
    that its data does not match the size the directory declares, and a
    204 KB workbook declaring 1,000 bytes inflated 200 MiB that way. What
    each read gives is counted against the cap, whatever the directory
    declares.
    """

    def __init__(self, z: zipfile.ZipFile, cap: int) -> None:
        self._z = z
        self._left = cap

    def read(self, name: str) -> bytes:
        """Member ``name``; KeyError when the workbook has none."""
        info = self._z.getinfo(name)
        if info.compress_type not in _METHODS:
            raise zipfile.BadZipFile(f"{name} is compressed with method {info.compress_type}")
        with self._z.open(info) as member:
            data = member.read(self._left + 1)
        if len(data) > self._left:
            raise ValueError("workbook unpacks past the size cap")
        self._left -= len(data)
        return data


def _sheet_member(members: _Members, name: str) -> str:
    """The archive member of the worksheet named ``name``, through the
    workbook's relationships; KeyError when it has none."""
    workbook = ET.fromstring(members.read("xl/workbook.xml"))
    rid = next(
        (
            sheet.get(_DOC_NS + "id")
            for sheet in workbook.iter(_NS + "sheet")
            if sheet.get("name") == name
        ),
        None,
    )
    rels = ET.fromstring(members.read("xl/_rels/workbook.xml.rels"))
    target = next(
        (rel.get("Target") for rel in rels.iter(_PKG_NS + "Relationship") if rel.get("Id") == rid),
        None,
    )
    if rid is None or not target:
        raise KeyError(f"no worksheet named {name!r}")
    return target.lstrip("/") if target.startswith("/") else f"xl/{target}"


def rows(payload: bytes, *, max_unpacked: int, sheet: str | None = None) -> list[dict[str, str]]:
    """The worksheet named ``sheet``, or the first sheet file, as text: one
    ``{column: text}`` per row. Raises one of :data:`UNREADABLE` on a
    payload that is not a readable workbook, or one that unpacks past
    ``max_unpacked`` bytes."""
    _check_directory(payload)
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            # What the directory declares refuses an honest zip bomb before
            # anything is inflated; _Members holds a dishonest one.
            if sum(info.file_size for info in z.infolist()) > max_unpacked:
                raise ValueError("workbook unpacks past the size cap")
            members = _Members(z, max_unpacked)
            try:
                shared = ET.fromstring(members.read("xl/sharedStrings.xml"))
            except KeyError:
                strings: list[str] = []
            else:
                strings = [
                    "".join(t.text or "" for t in si.iter(_NS + "t"))
                    for si in shared.iter(_NS + "si")
                ]
            member = _FIRST_SHEET if sheet is None else _sheet_member(members, sheet)
            tree = ET.fromstring(members.read(member))
    except (NotImplementedError, RuntimeError) as err:
        # A damaged directory entry reads as an encrypted member or as a
        # method or zip version the module does not know.
        raise zipfile.BadZipFile(str(err)) from err
    out: list[dict[str, str]] = []
    for row in tree.iter(_NS + "row"):
        cells: dict[str, str] = {}
        for cell in row.iter(_NS + "c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(_NS + "t"))
            else:
                value = cell.find(_NS + "v")
                if value is None or value.text is None:
                    continue
                text = strings[int(value.text)] if kind == "s" else value.text
            cells[cell.get("r", "").rstrip("0123456789")] = text.strip()
        out.append(cells)
    return out
