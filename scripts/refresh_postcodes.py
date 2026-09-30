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

"""Rebuild custom_components/be_gas_prices/postcode_map.py from live sources.

bpost's postcode list names every Belgian postcode. Synergrid's "DNB in uw
gemeente" lookup then gives, for each locality it files under a postcode, the
gas distribution system operator. A postcode's entry is the set of those
operators, as the integration's DSO keys, unless OVERRIDES below corrects it.

bpost publishes the list as HTML on bpost2.be and as an .xls linked from its
postcode page. The .xls is read here: bpost2.be did not answer when this was
written, and its one sheet of text and numbers is simple to read in the old
binary Excel format. The script needs the standard library only:

    python3 scripts/refresh_postcodes.py > synergrid.tsv

It writes the module and prints every Synergrid answer on stdout, one tab
separated "postcode, locality, gas DSO" line per locality ("-" for no gas,
"?" when Synergrid gave no answer), for review. Progress and warnings go to
stderr. Nothing is written when a source comes back short.
"""

from __future__ import annotations

import html
import http.client
import json
import re
import struct
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "custom_components" / "be_gas_prices" / "postcode_map.py"

USER_AGENT = "be_gas_prices refresh_postcodes"
BPOST_PAGE = "https://www.bpost.be/fr/outil-de-validation-de-codes-postaux"
# The file name carries a year, so the current one is read off the page.
BPOST_XLS_RE = re.compile(r'href="([^"]*/zipcodes_num_fr_\d{4}\.xls)"')
BPOST_HEADER = ["Code postal", "Localité", "Sous-commune", "Commune principale", "Province"]

SYNERGRID_PAGE = "https://www.synergrid.be/nl/over-de-netten/dnb-in-uw-gemeente"
SYNERGRID_URL = (
    "https://www.synergrid.be/index.php"
    "?option=com_synergrid&task=dnb.display&format=json&tmpl=component"
)
SYNERGRID_LANG = "nl-BE"
# Synergrid's search suggests at most this many localities.
SEARCH_CAP = 10
# The gas answer names the DSO in the paragraphs under its first heading. A
# second heading, when present, names the network operator (Fluvius for every
# Flemish DSO), which is not the DSO.
GAS_DSO_RE = re.compile(r'class="dnb_result g">.*?</h3>(.*?)(?:<h3>|$)', re.DOTALL)
PARAGRAPH_RE = re.compile(r"<p>(.*?)</p>", re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")

# A run is some 4000 requests over about an hour. The retries, 2 s doubling
# up to 32 s, ride out a DNS or server hiccup of a minute.
DELAY_S = 0.25
ATTEMPTS = 6

# bpost lists 1146 postcodes of places in 2026. Far fewer means a truncated
# file or a changed layout, not a smaller Belgium.
MIN_POSTCODES = 1100

# Gas DSO names as Synergrid prints them, and the DSO keys of const.py. The
# keys are spelled out rather than imported: importing const.py runs the
# package's __init__, which needs Home Assistant and the whole integration.
# tests/test_postcodes.py checks that the table holds known keys only. A name
# missing here (Enexis, the Dutch DSO serving most of Baarle-Hertog, which no
# Belgian card prices) is left out of its postcode's keys, reported, and
# written to UNPRICED so the setup flow can warn a household there.
SYNERGRID_GAS_DSOS: dict[str, str] = {
    "Fluvius Antwerpen": "fluvius_antwerpen",
    "Fluvius Halle-Vilvoorde": "fluvius_halle_vilvoorde",
    "Fluvius Imewo": "fluvius_imewo",
    "Fluvius Kempen": "fluvius_kempen",
    "Fluvius Limburg": "fluvius_limburg",
    "Fluvius Midden-Vlaanderen": "fluvius_midden_vlaanderen",
    "Fluvius West": "fluvius_west",
    "Fluvius Zenne-Dijle": "fluvius_zenne_dijle",
    "ORES Assets": "ores",
    "RESA": "resa",
    "Sibelga": "sibelga",
}

# Postcodes Synergrid gets wrong, or cannot answer for, set from the
# regulator's designation and the DSO's own open data. Each is reported when
# applied, so a corrected Synergrid shows up on the next run. Sources: the
# VNR designation decisions (vlaamsenutsregulator.be, "Aanwijzing
# netbeheerders", updated 11 June 2026), Fluvius open data 1_23 (gas DSO per
# locality) and 1_17 (active access points per postcode, August 2026), CWaPE
# (cwape.be/mon-grd/<postcode>, gas DSO per locality) and ORES open data on
# odwb.be (ores-gaz-nombre-de-points-de-prelevement-par-localite-annuel,
# 2024).
OVERRIDES: dict[str, tuple[str, ...]] = {
    # Bever. Synergrid gives Fluvius Halle-Vilvoorde, but the VNR designates
    # Fluvius Midden-Vlaanderen (Intergem, BESL-2015-42), which the 2025
    # decisions left unchanged there, and Fluvius 1_23 agrees.
    "1547": ("fluvius_midden_vlaanderen",),
    # Malderen, in Londerzeel. Synergrid gives Fluvius Zenne-Dijle, but the
    # VNR designates Fluvius Halle-Vilvoorde for all of Londerzeel from 2025
    # (BESL-2024-78), as Synergrid itself does for its other two localities,
    # and Fluvius 1_23 agrees.
    "1840": ("fluvius_halle_vilvoorde",),
    # Holsbeek. Synergrid still gives Fluvius Limburg, whose designation there
    # ended on 1 January 2025 (BESL-2024-78) in favour of Fluvius Zenne-Dijle
    # (BESL-2024-75). Fluvius 1_23 agrees.
    "3220": ("fluvius_zenne_dijle",),
    "3221": ("fluvius_zenne_dijle",),
    # Voeren. Synergrid gives Fluvius Limburg in most 3790 localities (none
    # in the rest of 3790 and in 3791 to 3798), but the VNR designates
    # Fluvius Limburg for Limburg "met uitzondering van de gemeente Voeren"
    # (BESL-2015-47), Fluvius 1_23 names no gas DSO in Voeren, and Fluvius
    # 1_17 counts no gas access point in 3790 to 3798.
    "3790": (),
    # Letterhoutem, in Sint-Lievens-Houtem. Synergrid gives Fluvius
    # Midden-Vlaanderen, but the VNR designates Fluvius Imewo for the commune
    # (BESL-2015-41), as Synergrid itself does for 9520, and Fluvius 1_23
    # agrees.
    "9521": ("fluvius_imewo",),
    # Braine-l'Alleud, whose one locality has an apostrophe in its name, so
    # Synergrid does not answer. CWaPE designates ORES, and ORES counts 9647
    # residential gas access points.
    "1420": ("ores",),
    # Andenne. Synergrid gives ORES for Sclayn, Seilles and Vezin, but CWaPE
    # designates RESA for gas in all ten localities (AIEG for electricity),
    # and ORES counts no gas access point in 5300.
    "5300": ("resa",),
    # Autelbas. Synergrid files it under 6700 and 6704 only, so bpost's 6706
    # gets no answer. CWaPE designates ORES, but ORES counts no gas access
    # point there, which elsewhere Synergrid shows as no gas.
    "6706": (),
    # Synergrid gives no gas in any locality, but CWaPE designates ORES for
    # gas and ORES counts gas access points there, residential ones unless
    # noted, given after each postcode.
    "5022": ("ores",),  # Cognelée, 2 professional
    "5310": ("ores",),  # Eghezée, 498
    "5363": ("ores",),  # Emptinne, 1
    "5640": ("ores",),  # Mettet, 282
    "5651": ("ores",),  # Tarcienne, 8
    "6210": ("ores",),  # Les Bons Villers, 715
    "6211": ("ores",),  # Mellet, 141
    "6531": ("ores",),  # Biesme-sous-Thuin, 1
    "6532": ("ores",),  # Ragnies, 26
    "6533": ("ores",),  # Biercée, 128
    "6690": ("ores",),  # Vielsalm, 76
    "7601": ("ores",),  # Roucourt, 14
    "7812": ("ores",),  # Ligne and Villers-Saint-Amand, 112
    "7870": ("ores",),  # Lens, 167
    "7901": ("ores",),  # Thieulain, 71
    "7904": ("ores",),  # Tourpes, 6
    "7951": ("ores",),  # Tongre-Notre-Dame, 71
}


class SourceError(Exception):
    """A source could not be read, or did not answer in the expected form."""


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def _retry[T](what: str, call: Callable[[], T]) -> T:
    """Run call, retrying a network or decoding failure with a growing pause."""
    attempt = 1
    while True:
        try:
            return call()
        except (OSError, ValueError, http.client.HTTPException) as err:
            if attempt == ATTEMPTS:
                raise SourceError(f"{what}: {err}") from err
            pause = 2.0**attempt
            print(f"{what}: {err}, retrying in {pause:.0f} s", file=sys.stderr)
            time.sleep(pause)
            attempt += 1


def _read(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=30) as response:
        body: bytes = response.read()
    return body


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return _retry(url, lambda: _read(request))


# Compound File Binary (OLE2) container, as far as reading one stream needs.
CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
CFB_END_OF_CHAIN = 0xFFFFFFFE


def cfb_stream(data: bytes, name: str) -> bytes:
    """The named stream of an OLE2 file."""
    if data[:8] != CFB_MAGIC:
        raise SourceError("not an OLE2 file")
    sector = 1 << struct.unpack_from("<H", data, 0x1E)[0]
    fat_sectors, dir_start = struct.unpack_from("<II", data, 0x2C)
    mini_cutoff = struct.unpack_from("<I", data, 0x38)[0]
    difat_next, difat_sectors = struct.unpack_from("<II", data, 0x44)
    per_sector = sector // 4

    def words(index: int) -> tuple[int, ...]:
        offset = (index + 1) * sector
        if offset + sector > len(data):
            raise SourceError(f"OLE2 sector {index} past the end of the file")
        return struct.unpack_from(f"<{per_sector}I", data, offset)

    difat = list(struct.unpack_from("<109I", data, 0x4C))
    for _ in range(difat_sectors):
        entries = words(difat_next)
        difat.extend(entries[:-1])
        difat_next = entries[-1]
    fat = [entry for index in difat[:fat_sectors] for entry in words(index)]

    def chain(start: int) -> bytes:
        out = bytearray()
        index = start
        while index != CFB_END_OF_CHAIN:
            if index >= len(fat) or len(out) > len(data):
                raise SourceError("broken OLE2 sector chain")
            out += data[(index + 1) * sector : (index + 2) * sector]
            index = fat[index]
        return bytes(out)

    directory = chain(dir_start)
    for offset in range(0, len(directory) - 127, 128):
        name_len = struct.unpack_from("<H", directory, offset + 0x40)[0]
        entry = directory[offset : offset + max(name_len - 2, 0)].decode("utf-16-le")
        if entry != name:
            continue
        start, size = struct.unpack_from("<II", directory, offset + 0x74)
        # Streams under the cutoff live in the mini stream. A workbook that
        # small could not hold the postcode list anyway.
        if size < mini_cutoff:
            raise SourceError(f"OLE2 stream {name} is too small")
        return chain(start)[:size]
    raise SourceError(f"no {name} stream in the OLE2 file")


# The few BIFF8 records the bpost sheet uses.
BIFF_BOF = 0x0809
BIFF_EOF = 0x000A
BIFF_SST = 0x00FC
BIFF_CONTINUE = 0x003C
BIFF_LABELSST = 0x00FD
BIFF_NUMBER = 0x0203


class _Pieces:
    """Reads across the SST record and the CONTINUE records that follow it.

    A string's characters may run over into the next record, which then
    starts with a fresh flags byte saying whether they are one or two bytes
    wide. Everything else is read straight across the boundary.
    """

    def __init__(self, pieces: list[bytes]) -> None:
        self.pieces = pieces
        self.index = 0
        self.pos = 0

    def _next(self) -> None:
        self.index += 1
        self.pos = 0
        if self.index == len(self.pieces):
            raise SourceError("xls shared string table ends early")

    def take(self, count: int) -> bytes:
        out = bytearray()
        while count:
            piece = self.pieces[self.index]
            if self.pos == len(piece):
                self._next()
                continue
            chunk = piece[self.pos : self.pos + count]
            out += chunk
            self.pos += len(chunk)
            count -= len(chunk)
        return bytes(out)

    def chars(self, count: int, wide: bool) -> str:
        out = []
        while count:
            piece = self.pieces[self.index]
            if self.pos == len(piece):
                self._next()
                wide = bool(self.take(1)[0] & 1)
                continue
            width = 2 if wide else 1
            fit = min(count, (len(piece) - self.pos) // width)
            if fit == 0:
                raise SourceError("xls string splits a character")
            raw = piece[self.pos : self.pos + fit * width]
            out.append(raw.decode("utf-16-le" if wide else "latin-1"))
            self.pos += fit * width
            count -= fit
        return "".join(out)


def _shared_strings(pieces: list[bytes]) -> list[str]:
    reader = _Pieces(pieces)
    unique = struct.unpack("<4xI", reader.take(8))[0]
    strings = []
    for _ in range(unique):
        count, flags = struct.unpack("<HB", reader.take(3))
        runs = struct.unpack("<H", reader.take(2))[0] if flags & 0x08 else 0
        extra = struct.unpack("<I", reader.take(4))[0] if flags & 0x04 else 0
        strings.append(reader.chars(count, bool(flags & 0x01)))
        reader.take(4 * runs + extra)
    return strings


def xls_rows(data: bytes) -> list[dict[int, str | float]]:
    """The cells of the first sheet of an .xls file, row by row."""
    stream = cfb_stream(data, "Workbook")
    records = []
    pos = 0
    while pos + 4 <= len(stream):
        kind, size = struct.unpack_from("<HH", stream, pos)
        records.append((kind, stream[pos + 4 : pos + 4 + size]))
        pos += 4 + size
    strings: list[str] = []
    cells: dict[int, dict[int, str | float]] = {}
    substream = 0
    for number, (kind, body) in enumerate(records):
        if kind == BIFF_BOF:
            substream += 1
        elif kind == BIFF_EOF and substream == 2:
            break
        elif kind == BIFF_SST:
            pieces = [body]
            for later, more in records[number + 1 :]:
                if later != BIFF_CONTINUE:
                    break
                pieces.append(more)
            strings = _shared_strings(pieces)
        elif kind == BIFF_LABELSST and substream == 2:
            row, col, _, string = struct.unpack_from("<HHHI", body)
            if string >= len(strings):
                raise SourceError("xls cell points past the shared string table")
            cells.setdefault(row, {})[col] = strings[string]
        elif kind == BIFF_NUMBER and substream == 2:
            row, col, _, value = struct.unpack_from("<HHHd", body)
            cells.setdefault(row, {})[col] = value
    return [cells[row] for row in sorted(cells)]


def bpost_postcodes() -> tuple[str, dict[str, list[str]]]:
    """The .xls URL, and the localities bpost files under each postcode."""
    page = _get(BPOST_PAGE).decode("utf-8", "replace")
    links = BPOST_XLS_RE.findall(page)
    if not links:
        raise SourceError(f"no postcode .xls link on {BPOST_PAGE}")
    url = urllib.parse.urljoin(BPOST_PAGE, max(links))
    try:
        rows = xls_rows(_get(url))
    except struct.error as err:
        raise SourceError(f"{url}: truncated xls record: {err}") from err
    if not rows or [rows[0].get(col) for col in range(len(BPOST_HEADER))] != BPOST_HEADER:
        raise SourceError(f"{url}: unexpected header {rows[:1]}")
    postcodes: dict[str, list[str]] = {}
    for row in rows[1:]:
        # Rows without a province are postcodes bpost gives institutions,
        # companies and sorting centres (parliaments, broadcasters, "Brussel
        # X"), not places with gas meters.
        if not row.get(4):
            continue
        code, name = row.get(0), row.get(1)
        if not isinstance(code, float) or not isinstance(name, str) or not 1000 <= code <= 9999:
            raise SourceError(f"{url}: unexpected row {row}")
        postcodes.setdefault(f"{int(code)}", []).append(name)
    return url, postcodes


def _synergrid(form: dict[str, str]) -> object:
    """POST one lookup to Synergrid and decode its JSON answer."""
    request = urllib.request.Request(
        SYNERGRID_URL,
        data=urllib.parse.urlencode(form).encode(),
        # Without a Referer from its own site the endpoint answers an empty body.
        headers={"User-Agent": USER_AGENT, "Referer": SYNERGRID_PAGE},
    )
    time.sleep(DELAY_S)
    return _retry(f"Synergrid {form}", lambda: json.loads(_read(request)))


def search(text: str) -> list[tuple[str, str]]:
    """The (postcode, locality) pairs Synergrid suggests for a search text."""
    answer = _synergrid({"type": "list", "dnb_search": text, "lang": SYNERGRID_LANG})
    if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
        raise SourceError(f"Synergrid search {text!r}: unexpected answer {answer!r}")
    return [(str(hit["netadmin_zip"]), str(hit["netadmin_subcity"])) for hit in answer["results"]]


def _folded(name: str) -> str:
    return re.sub(r"\W", "", name.casefold())


def localities(postcode: str, bpost_names: list[str]) -> list[str]:
    """The localities Synergrid files under a postcode.

    A search on the postcode suggests them alphabetically, cut at SEARCH_CAP.
    When the cut is reached, the bpost locality names not among them are
    searched too, which finds the ones past it. A name Synergrid spells
    otherwise ("Mont-Gauthier", "Montgauthier") is compared without case or
    punctuation, and reported when even that finds nothing.
    """
    found = search(postcode)
    towns = {town for code, town in found if code == postcode}
    if len(found) >= SEARCH_CAP:
        for name in bpost_names:
            if _folded(name) in {_folded(town) for town in towns}:
                continue
            hits = {town for code, town in search(name) if code == postcode}
            # Synergrid misspells a few ("Waret-la-Chassée"), so a miss is
            # searched again on the first word of the name.
            first = re.split(r"[- ]", name, maxsplit=1)[0]
            if not hits and first != name:
                hits = {town for code, town in search(first) if code == postcode}
            if not hits:
                warn(f"{postcode}: bpost locality {name!r} not found by name on Synergrid")
            towns |= hits
    return sorted(towns)


def gas_dsos(postcode: str, town: str) -> list[str] | None:
    """The gas DSO names Synergrid gives for a locality, none when it has no gas.

    None when Synergrid gives no gas answer at all. It answers nothing, on its
    own site too, for every locality with an apostrophe in its name
    ("Braine-l'Alleud"), and only the electricity DSO for a few others (2870
    Lippelo).
    """
    answer = _synergrid(
        {"type": "selection", "zip": postcode, "town": town, "lang": SYNERGRID_LANG}
    )
    if not isinstance(answer, str):
        raise SourceError(f"Synergrid {postcode} {town!r}: unexpected answer {answer!r}")
    match = GAS_DSO_RE.search(answer)
    if match is None:
        return None
    paragraphs = PARAGRAPH_RE.findall(match.group(1))
    if not paragraphs:
        raise SourceError(f"Synergrid {postcode} {town!r}: no gas answer in {answer!r}")
    names = []
    for paragraph in paragraphs:
        # "Fluvius Kempen :+32 78 35 35 34" carries the phone number; "-"
        # stands for no gas DSO.
        name = html.unescape(TAG_RE.sub("", paragraph)).split(":", 1)[0].strip()
        if name != "-":
            names.append(name)
    return names


def build(
    bpost: dict[str, list[str]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """The gas DSO keys per postcode, and per postcode the gas DSOs
    Synergrid names there that have no key."""
    table: dict[str, tuple[str, ...]] = {}
    unpriced: dict[str, tuple[str, ...]] = {}
    for done, postcode in enumerate(sorted(bpost)):
        if done % 50 == 0:
            print(f"{done}/{len(bpost)} postcodes", file=sys.stderr)
        keys: set[str] = set()
        unknown: set[str] = set()
        answered = False
        for town in localities(postcode, bpost[postcode]):
            names = gas_dsos(postcode, town)
            shown = "?" if names is None else (", ".join(names) or "-")
            print(f"{postcode}\t{town}\t{shown}", flush=True)
            if names is None:
                warn(f"{postcode} {town!r}: no Synergrid answer, left out")
                continue
            answered = True
            for name in names:
                if name in SYNERGRID_GAS_DSOS:
                    keys.add(SYNERGRID_GAS_DSOS[name])
                else:
                    unknown.add(name)
                    warn(f"{postcode} {town!r}: gas DSO {name!r} has no key, left out")
        if unknown:
            unpriced[postcode] = tuple(sorted(unknown))
        if answered:
            table[postcode] = tuple(sorted(keys))
        else:
            warn(f"{postcode}: no Synergrid answer for any locality, left out")
    for postcode, forced in OVERRIDES.items():
        if postcode not in bpost:
            warn(f"{postcode}: bpost no longer lists it, drop the override")
            continue
        if table.get(postcode) == forced:
            warn(f"{postcode}: Synergrid now agrees with the override, drop it")
        print(f"{postcode}: Synergrid {table.get(postcode)}, override {forced}", file=sys.stderr)
        table[postcode] = forced
    return table, unpriced


def _license() -> str:
    """This file's BSD header, which the generated module carries as well."""
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    start = next(n for n, line in enumerate(lines) if line.startswith("# Copyright"))
    end = next(n for n, line in enumerate(lines) if line.endswith("POSSIBILITY OF SUCH DAMAGE."))
    return "\n".join(lines[start : end + 1])


def _entry(postcode: str, values: tuple[str, ...]) -> str:
    joined = ", ".join(f'"{value}"' for value in values)
    return f'    "{postcode}": ({joined}{"," if len(values) == 1 else ""}),'


def render(
    table: dict[str, tuple[str, ...]], unpriced: dict[str, tuple[str, ...]], xls_url: str
) -> str:
    lines = [
        _license(),
        "",
        f"# Generated by scripts/refresh_postcodes.py on {date.today().isoformat()}.",
        "# Do not edit: rerun the script. Sources: the bpost postcode list",
        f"# ({xls_url})",
        "# and the Synergrid gas DSO lookup per locality",
        f"# ({SYNERGRID_PAGE}),",
        "# with the overrides listed in the script.",
        "",
        '"""Belgian postcodes and the gas DSO keys serving their localities."""',
        "",
        "POSTCODES: dict[str, tuple[str, ...]] = {",
    ]
    lines.extend(_entry(postcode, table[postcode]) for postcode in sorted(table))
    lines += [
        "}",
        "",
        "# The gas DSOs Synergrid names in a postcode that no Belgian card prices:",
        "# a household there may be on one of them rather than on the postcode's",
        "# keys above.",
        "UNPRICED: dict[str, tuple[str, ...]] = {",
    ]
    lines.extend(_entry(postcode, unpriced[postcode]) for postcode in sorted(unpriced))
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    try:
        xls_url, bpost = bpost_postcodes()
        print(f"bpost: {len(bpost)} postcodes from {xls_url}", file=sys.stderr)
        if len(bpost) < MIN_POSTCODES:
            raise SourceError(f"bpost lists {len(bpost)} postcodes, expected {MIN_POSTCODES}")
        table, unpriced = build(bpost)
    except SourceError as err:
        print(f"aborting, nothing written: {err}", file=sys.stderr)
        return 1
    if len(table) < MIN_POSTCODES:
        print(f"aborting, nothing written: only {len(table)} postcodes", file=sys.stderr)
        return 1
    missing = set(SYNERGRID_GAS_DSOS.values()) - {key for keys in table.values() for key in keys}
    if missing:
        print(f"aborting, nothing written: no postcode for {sorted(missing)}", file=sys.stderr)
        return 1
    OUTPUT.write_text(render(table, unpriced, xls_url), encoding="utf-8")
    print(f"wrote {len(table)} postcodes to {OUTPUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
