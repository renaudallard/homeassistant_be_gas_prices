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

"""The network sheets the card archive kept, by delivery month and DSO.

A custom entry can take its network off its DSO's tariff sheet
(network_sheet.py). The card archive reads the same sheets every day through
the same readers and keeps, for each month and DSO, each tier's terms and
the metering the sheet of the month's own year gives, excluding VAT, in
``network.json`` (``scripts/archive_cards.py``): a month still read is
written again, one no longer read keeps what was written.

It answers only for a month an entry's own reading of the sheets does not
give (network_sheet.held_network), ahead of the figures typed. Read daily by
an entry with a contract taking its network from a sheet that lets the card
archive be read, held for the process, and kept in such an entry's store so a
restart without the network still knows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, fields
from datetime import date
from typing import Any, Final

import aiohttp

from .archive_table import ArchiveTable
from .const import NETWORK_TABLE_URL
from .law_archive import table_months
from .providers.base import SheetNetwork

# A figure outside these is a misread, not a tariff, excluding VAT: EUR a
# year for the fixed terms and the metering, EUR/kWh for the rates.
_MAX_EUR_PER_YEAR: Final = 1000.0
_MAX_EUR_PER_KWH: Final = 0.2
_PER_KWH: Final = frozenset({"t1_proportional", "t2_proportional"})
_NAMES: Final = frozenset(field.name for field in fields(SheetNetwork))

# (year, month) -> DSO key -> the network the sheet gave.
_HELD: dict[tuple[int, int], dict[str, SheetNetwork]] = {}


def network_for(month: date, dso: str) -> SheetNetwork | None:
    """The network the archive kept for ``dso``'s sheet in ``month``, or
    ``None``."""
    return _HELD.get((month.year, month.month), {}).get(dso)


def _network(raw: Any) -> SheetNetwork | None:
    """One DSO's month, or ``None`` unless every figure reads: half of it
    would bill a network short of a charge."""
    if not isinstance(raw, Mapping) or set(raw) != _NAMES:
        return None
    figures: dict[str, float] = {}
    for name, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        bound = _MAX_EUR_PER_KWH if name in _PER_KWH else _MAX_EUR_PER_YEAR
        if not 0.0 <= float(value) < bound:
            return None
        figures[str(name)] = float(value)
    return SheetNetwork(**figures)


def _months(section: Any) -> dict[tuple[int, int], dict[str, SheetNetwork]]:
    """The months of the table that hold a DSO that reads, from 2000 to next
    year alone (law_archive.table_months)."""
    out: dict[tuple[int, int], dict[str, SheetNetwork]] = {}
    for key, raw in table_months(section):
        if not isinstance(raw, Mapping):
            continue
        dsos = {str(dso): network for dso, item in raw.items() if (network := _network(item))}
        if dsos:
            out[key] = dsos
    return out


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape of ``network.json``), replacing what was
    held, unless it carries no month while something is held."""
    if "months" not in table:
        return
    months = _months(table["months"])
    if months or not _HELD:
        _HELD.clear()
        _HELD.update(months)


def restore(table: Any) -> None:
    """Hold ``table`` from an entry's store, when nothing is held yet, for the
    reason law_archive.py gives."""
    if not _HELD and isinstance(table, Mapping) and "months" in table:
        _HELD.update(_months(table["months"]))


def held_table() -> dict[str, dict[str, dict[str, dict[str, float]]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    return {
        "months": {
            f"{year:04d}-{month:02d}": {dso: asdict(network) for dso, network in dsos.items()}
            for (year, month), dsos in sorted(_HELD.items())
        }
    }


_TABLE: Final = ArchiveTable(NETWORK_TABLE_URL, hold)


async def ensure_network_archive(session: aiohttp.ClientSession) -> None:
    """Refresh the table from the card archive once a day (archive_table.py)."""
    await _TABLE.ensure(session)
