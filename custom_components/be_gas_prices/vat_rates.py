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

"""The Belgian residential VAT rate on gas, by delivery month.

A card that states its own rate keeps it for everything on its own basis;
this answers for everything else: a figure the law sets excluding VAT, put on
a card that states none, a formula a parser grossed on an assumed rate, and
the figures a household typed from its contract.

The table is the card archive's ``vat.json``: per month, the rate the cards
agree on, three suppliers or more stating one rate and none another
(``scripts/archive_cards.py``). A month they disagree on is not in it. Keyed
by the month billed and never applied backwards: a month the table does not
hold takes the last month before it that it does, then the rate in
``const.py``. Held for the process, refreshed daily, and kept in every
entry's store so a restart without the network still knows the last one.
Only an entry that lets the card archive be read refreshes it; one that
keeps the archive out bills on the table last read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import aiohttp

from .archive_table import ArchiveTable
from .const import VAT_RATE_REDUCED, VAT_TABLE_URL

# A rate outside these is a misread, not a rate.
_MIN_RATE: Final = 0.0
_MAX_RATE: Final = 0.5

# (year, month) -> rate, as held.
_RESIDENTIAL: dict[tuple[int, int], float] = {}


def residential_vat(month: date) -> float:
    """The rate a household pays on gas delivered in ``month``."""
    key = (month.year, month.month)
    if key in _RESIDENTIAL:
        return _RESIDENTIAL[key]
    earlier = [k for k in _RESIDENTIAL if k < key]
    return _RESIDENTIAL[max(earlier)] if earlier else VAT_RATE_REDUCED


def _months(section: Any) -> dict[tuple[int, int], float]:
    """The agreed months of the table, bounded."""
    out: dict[tuple[int, int], float] = {}
    if not isinstance(section, Mapping):
        return out
    for label, entry in section.items():
        rate = entry.get("rate") if isinstance(entry, Mapping) else None
        try:
            year, month = (int(part) for part in str(label).split("-"))
            rate = float(rate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if 1 <= month <= 12 and _MIN_RATE <= rate < _MAX_RATE:
            out[(year, month)] = rate
    return out


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape of ``vat.json``), replacing what was held,
    unless it carries no agreed month while something is held."""
    if "residential" not in table:
        return
    months = _months(table["residential"])
    if months or not _RESIDENTIAL:
        _RESIDENTIAL.clear()
        _RESIDENTIAL.update(months)


def restore(table: Any) -> None:
    """Hold ``table`` from an entry's store, when nothing is held yet.

    Several entries restore their own copy at start-up and one of them may
    already have fetched a newer table, so a stored copy never replaces one.
    """
    if not _RESIDENTIAL and isinstance(table, Mapping) and "residential" in table:
        _RESIDENTIAL.update(_months(table["residential"]))


def held_table() -> dict[str, dict[str, dict[str, float]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    return {
        "residential": {
            f"{y:04d}-{m:02d}": {"rate": rate} for (y, m), rate in sorted(_RESIDENTIAL.items())
        }
    }


_TABLE: Final = ArchiveTable(VAT_TABLE_URL, hold)


async def ensure_vat_rates(session: aiohttp.ClientSession) -> None:
    """Refresh the table from the card archive once a day (archive_table.py)."""
    await _TABLE.ensure(session)
