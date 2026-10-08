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

"""The daily ranking behind the potential-saving sensor.

Once a day, when the entry asks for it, every contract sold in the region is
quoted against the household (compare.py) and the result kept here, across
restarts, until the next day's replaces it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from .compare import Quote

_MINUTES_A_DAY = 24 * 60


def ranking_minute(entry_id: str) -> int:
    """The minute of the day an entry ranks at, spread by its id so that
    installations do not all fetch every supplier at the same moment, and
    stable so a failure can be reasoned about."""
    return int(hashlib.sha256(entry_id.encode()).hexdigest(), 16) % _MINUTES_A_DAY


@dataclass(frozen=True)
class RankedRow:
    supplier: str
    contract: str
    label: str
    annual_cost: float
    # What the comparison table shows besides the cost. None in a ranking a
    # release before them stored, which the table is not drawn from.
    all_in: float | None = None
    provisional: bool = False
    read_by_ocr: bool = False

    def quote(self) -> Quote:
        """The row as the comparison table takes it."""
        return Quote(
            supplier=self.supplier,
            contract=self.contract,
            label=self.label,
            annual_cost=self.annual_cost,
            all_in=self.all_in,
            fixed=None,
            indexed=False,
            provisional=self.provisional,
            read_by_ocr=self.read_by_ocr,
        )


@dataclass(frozen=True)
class DailyRanking:
    """The quoted contracts cheapest first, and the household's own."""

    day: date
    rows: tuple[RankedRow, ...]
    own: tuple[str, str]

    @classmethod
    def from_quotes(cls, day: date, quotes: list[Quote], own: tuple[str, str]) -> DailyRanking:
        rows = sorted(
            (
                RankedRow(
                    q.supplier,
                    q.contract,
                    q.label,
                    q.annual_cost,
                    q.all_in,
                    q.provisional,
                    q.read_by_ocr,
                )
                for q in quotes
                if q.annual_cost is not None
            ),
            key=lambda row: (row.annual_cost, row.label),
        )
        return cls(day=day, rows=tuple(rows), own=own)

    @property
    def tabled(self) -> bool:
        """Whether the rows carry what the comparison table shows."""
        return all(row.all_in is not None for row in self.rows)

    @property
    def own_cost(self) -> float | None:
        for row in self.rows:
            if (row.supplier, row.contract) == self.own:
                return row.annual_cost
        return None

    @property
    def saving(self) -> float | None:
        """What the cheapest contract would save a year against the
        household's own, which is among the rows: zero when nothing beats it.
        None when the own contract could not be quoted."""
        own = self.own_cost
        if own is None or not self.rows:
            return None
        return own - self.rows[0].annual_cost

    def to_json(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "own": list(self.own),
            "rows": [
                [
                    r.supplier,
                    r.contract,
                    r.label,
                    r.annual_cost,
                    r.all_in,
                    r.provisional,
                    r.read_by_ocr,
                ]
                for r in self.rows
            ],
        }

    @classmethod
    def from_json(cls, blob: Any) -> DailyRanking | None:
        if not isinstance(blob, dict):
            return None
        try:
            return cls(
                day=date.fromisoformat(blob["day"]),
                own=(str(blob["own"][0]), str(blob["own"][1])),
                rows=tuple(_row_from_json(row) for row in blob["rows"]),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None


def _row_from_json(row: list[Any]) -> RankedRow:
    """A stored row: the four fields an older release stored, or all seven."""
    supplier, contract, label, cost = row[:4]
    ranked = RankedRow(str(supplier), str(contract), str(label), float(cost))
    if len(row) == 4:
        return ranked
    all_in, provisional, read_by_ocr = row[4:]
    return replace(
        ranked,
        all_in=None if all_in is None else float(all_in),
        provisional=provisional is True,
        read_by_ocr=read_by_ocr is True,
    )
