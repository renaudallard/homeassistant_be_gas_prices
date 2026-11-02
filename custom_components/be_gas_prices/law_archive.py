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


"""The federal levies on gas the card archive kept, by delivery month.

The integration reads the law itself, FPS Finance's excise tariff
(tarbel.py) and article 419 on Justel (excise_law.py), and neither keeps
every month it once gave: the tariff lists the quarters FPS Finance still
publishes, and Justel's consolidated text only the wording in force. The
card archive reads them the same way every day and keeps what the
integration bills each month from them in ``law.json``
(``scripts/archive_cards.py``): the excise of both slices, the protected
customer's rate and the energy contribution. A month the readings still
give is written again, one they no longer give keeps what was written.

It answers only for a month an entry's own readings do not give
(providers/_resolve.py), ahead of the figures const.py holds. Read daily by
an entry that lets the card archive be read, held for the process, and kept
in every entry's store so a restart without the network still knows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import aiohttp

from .archive_table import ArchiveTable
from .const import LAW_TABLE_URL

# A figure outside these is a misread, not a levy (EUR/kWh excluding VAT).
_MAX_EUR_PER_KWH: Final = 0.2
_RATES: Final = ("protected", "contribution")

# (year, month) -> the figures the archive kept for it.
_HELD: dict[tuple[int, int], dict[str, Any]] = {}


def excise_for(month: date) -> tuple[float, float] | None:
    """The two slices' excise the archive kept for ``month``, EUR/kWh
    excluding VAT, or ``None``."""
    rates: tuple[float, float] | None = _HELD.get((month.year, month.month), {}).get("excise")
    return rates


def protected_excise_for(month: date) -> float | None:
    """The protected customer's excise the archive kept for ``month``,
    EUR/kWh excluding VAT, or ``None``."""
    rate: float | None = _HELD.get((month.year, month.month), {}).get("protected")
    return rate


def contribution_for(month: date) -> float | None:
    """The energy contribution the archive kept for ``month``, EUR/kWh
    excluding VAT, or ``None``."""
    rate: float | None = _HELD.get((month.year, month.month), {}).get("contribution")
    return rate


def _rate(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    rate = float(value)
    return rate if 0.0 <= rate < _MAX_EUR_PER_KWH else None


def _entry(raw: Any) -> dict[str, Any]:
    """The figures of one month that read, each on its own."""
    out: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return out
    excise = raw.get("excise")
    if isinstance(excise, list) and len(excise) == 2:
        low, high = _rate(excise[0]), _rate(excise[1])
        if low is not None and high is not None:
            out["excise"] = (low, high)
    for name in _RATES:
        rate = _rate(raw.get(name))
        if rate is not None:
            out[name] = rate
    return out


def _months(section: Any) -> dict[tuple[int, int], dict[str, Any]]:
    """The months of the table that hold a figure that reads."""
    out: dict[tuple[int, int], dict[str, Any]] = {}
    if not isinstance(section, Mapping):
        return out
    for label, raw in section.items():
        try:
            year, month = (int(part) for part in str(label).split("-"))
        except ValueError:
            continue
        entry = _entry(raw)
        if 1 <= month <= 12 and entry:
            out[(year, month)] = entry
    return out


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape of ``law.json``), replacing what was held,
    unless it carries no month while something is held."""
    if "residential" not in table:
        return
    months = _months(table["residential"])
    if months or not _HELD:
        _HELD.clear()
        _HELD.update(months)


def restore(table: Any) -> None:
    """Hold ``table`` from an entry's store, when nothing is held yet.

    Several entries restore their own copy at start-up and one of them may
    already have fetched a newer table, so a stored copy never replaces one.
    """
    if not _HELD and isinstance(table, Mapping) and "residential" in table:
        _HELD.update(_months(table["residential"]))


def held_table() -> dict[str, dict[str, dict[str, Any]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    months: dict[str, dict[str, Any]] = {}
    for (year, month), entry in sorted(_HELD.items()):
        figures = dict(entry)
        if "excise" in figures:
            figures["excise"] = list(figures["excise"])
        months[f"{year:04d}-{month:02d}"] = figures
    return {"residential": months}


_TABLE: Final = ArchiveTable(LAW_TABLE_URL, hold)


async def ensure_law_archive(session: aiohttp.ClientSession) -> None:
    """Refresh the table from the card archive once a day (archive_table.py)."""
    await _TABLE.ensure(session)
