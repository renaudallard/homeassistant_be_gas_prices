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
sheets and FPS Finance's excise tariff. Read with the standard library's zip
reader and defusedxml; the unpacked size is capped before anything is read
out of the archive, which refuses a zip bomb, and a damaged archive raises
one of :data:`UNREADABLE`.
"""

from __future__ import annotations

import io
import zipfile
import zlib
from typing import Final

from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOC_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_FIRST_SHEET: Final = "xl/worksheets/sheet1.xml"

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


def _sheet_member(z: zipfile.ZipFile, name: str) -> str:
    """The archive member of the worksheet named ``name``, through the
    workbook's relationships; KeyError when it has none."""
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rid = next(
        (
            sheet.get(_DOC_NS + "id")
            for sheet in workbook.iter(_NS + "sheet")
            if sheet.get("name") == name
        ),
        None,
    )
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
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
    payload that is not a readable workbook."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            if sum(info.file_size for info in z.infolist()) > max_unpacked:
                raise ValueError("workbook unpacks past the size cap")
            try:
                shared = ET.fromstring(z.read("xl/sharedStrings.xml"))
            except KeyError:
                strings: list[str] = []
            else:
                strings = [
                    "".join(t.text or "" for t in si.iter(_NS + "t"))
                    for si in shared.iter(_NS + "si")
                ]
            member = _FIRST_SHEET if sheet is None else _sheet_member(z, sheet)
            tree = ET.fromstring(z.read(member))
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
