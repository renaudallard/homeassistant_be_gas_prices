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

"""Each line of a supplier's bill held against the rate priced here for it.

A line is matched to what it bills by its label, as Eneco prints it on its
French gas bills, since a gas bill carries no line codes. A label not listed
is not compared, never guessed at. The rate is the one the integration bills
the line's months on: each month's card with the energy leg the contract is
billed on, the household's own settings and the law of those months. A line
whose months are billed here at more than one rate is not compared either:
an average would hide where the two part.

A line is off when its unit price, before VAT, is further from the one priced
here than the printing of the two explains:

- a card prints c€/kWh VAT included to two decimals, half a hundredth of
  which is up to 0,0047 c€ before 6% VAT: 0,005 c€/kWh;
- and EUR a year to the cent: 0,005 EUR a year;
- a formula is priced at an index printed to 0,01 EUR/MWh, half of which
  moves the price 0,0005 c€/kWh at the usual factor of 0,1 c€/kWh per
  EUR/MWh, while a bill prints five decimals of a cent: 0,001 c€/kWh, as
  the acceptance tests of Eneco's bills hold it;
- the law's levies are known to the decimals a bill prints: 0,001 c€/kWh,
  the excise being the blend of its slices at the household's annual
  volume, which is how Eneco bills it, on one line.

Pure: no Home Assistant imports.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from .bill import IndexValue
from .providers._resolve import effective_excise, tier_for, vat_factor
from .providers.base import SupplierSnapshot
from .running_costs import months_between

OK: Final = "ok"
OFF: Final = "off"
NOT_COMPARED: Final = "not_compared"

# What a line bills, among what a month is priced on here.
ENERGY: Final = "energy"
SUPPLIER_FEE: Final = "supplier_fee"
DISTRIBUTION: Final = "distribution"
DISTRIBUTION_FIXED: Final = "distribution_fixed"
TRANSPORT: Final = "transport"
EXCISE: Final = "excise"
CONTRIBUTION: Final = "contribution"
CONNECTION_FEE: Final = "connection_fee"

# The units a bill prices a line per, as the Eneco integration names them.
UNIT_KWH: Final = "kWh"
UNIT_YEAR: Final = "year"

# The labels of Eneco's French gas bills, as _normal() leaves them.
_LABELS: Final[dict[str, tuple[str, str]]] = {
    "redevance annuelle": (SUPPLIER_FEE, UNIT_YEAR),
    "consommation": (ENERGY, UNIT_KWH),
    "distribution variable": (DISTRIBUTION, UNIT_KWH),
    "distribution fixe": (DISTRIBUTION_FIXED, UNIT_YEAR),
    "coût de transport": (TRANSPORT, UNIT_KWH),
    "cotisation sur l'énergie": (CONTRIBUTION, UNIT_KWH),
    "accise spéciale": (EXCISE, UNIT_KWH),
    "redevance de raccordement": (CONNECTION_FEE, UNIT_KWH),
}

# How far a billed unit price may be from the one priced here, in EUR per
# unit before VAT, by where the one priced here comes from (see above).
_CARD_KWH: Final = 5e-5
_CARD_YEAR: Final = 5e-3
_INDEXED_KWH: Final = 1e-5
_LAW_KWH: Final = 1e-5


@dataclass(frozen=True)
class Rate:
    """A rate in EUR per unit before VAT, and how far a bill's may be from it."""

    value: float
    tolerance: float


# A month's rates by what they bill, or why one is not priced; or why the
# month itself is not.
type MonthRates = Mapping[str, Rate | str]
type RatesFor = Callable[[str], MonthRates | str]


@dataclass(frozen=True)
class LineCheck:
    """One billed line, its unit price and the one priced here, in EUR per
    ``unit`` before VAT, and whether they agree."""

    label: str
    section: str
    start: str
    end: str
    unit: str | None
    billed: float | None
    priced: float | None
    status: str
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "section": self.section,
            "start": self.start,
            "end": self.end,
            "unit": self.unit,
            "billed": self.billed,
            "priced": self.priced,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BillCheck:
    """A bill's lines, each held against the rate priced here, and the days
    it was issued and covers, ISO dates or None."""

    issued: str | None
    period_start: str | None
    period_end: str | None
    lines: tuple[LineCheck, ...]

    @property
    def off(self) -> int:
        """How many of the lines compared are off."""
        return sum(1 for line in self.lines if line.status == OFF)


def month_rates(
    priced: SupplierSnapshot,
    price: float,
    index: IndexValue | None,
    provisional: bool,
    *,
    month: str,
    dso: str,
    annual_kwh: float,
) -> dict[str, Rate | str]:
    """What ``month`` is billed on here, before VAT: ``priced``, the card as
    it bills the month (``bill.priced_month``), its energy leg's ``price``
    on the card's VAT basis, priced at ``index`` and ``provisional`` or not.
    The tier and the excise slices follow ``annual_kwh``."""
    year, number = (int(part) for part in month.split("-"))
    taxes = priced.taxes
    # A residential card prints VAT included, at the rate the law's
    # figures are put on (vat_factor); a professional one excluding it.
    vat = 1.0 if taxes.vat_rate else vat_factor(taxes, date(year, number, 1))
    rates: dict[str, Rate | str] = {
        ENERGY: (
            f"the energy price of {month} is not final"
            if provisional
            else Rate(price / vat, _CARD_KWH if index is None else _INDEXED_KWH)
        ),
        SUPPLIER_FEE: Rate(priced.energy.yearly_fixed_fee / vat, _CARD_YEAR),
        CONTRIBUTION: Rate(taxes.energy_contribution / vat, _LAW_KWH),
        # Carries no VAT.
        CONNECTION_FEE: Rate(taxes.connection_fee, _LAW_KWH),
        EXCISE: (
            Rate(effective_excise(taxes.excise_bands, annual_kwh) / vat, _LAW_KWH)
            if taxes.excise_bands
            else f"no gas excise held for {month}"
        ),
    }
    overlay = priced.dsos.get(dso)
    tier = tier_for(annual_kwh)
    terms = None if overlay is None else overlay.tiers.get(tier)
    if overlay is None:
        rates[TRANSPORT] = f"the card of {month} has no {dso} tariff"
    else:
        rates[TRANSPORT] = Rate(overlay.transport / vat, _CARD_KWH)
    if terms is None:
        missing = f"the card of {month} has no {dso} {tier.upper()}"
        rates[DISTRIBUTION] = rates[DISTRIBUTION_FIXED] = missing
    else:
        rates[DISTRIBUTION] = Rate(terms.proportional / vat, _CARD_KWH)
        rates[DISTRIBUTION_FIXED] = Rate(terms.fixed_per_year / vat, _CARD_YEAR)
    return rates


def _normal(label: str) -> str:
    """``label`` as _LABELS keys it: case, blanks and apostrophes aside."""
    text = unicodedata.normalize("NFKC", label).replace("’", "'")
    return " ".join(text.casefold().split())


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(_text(value))
    except ValueError:
        return None


def bill_period(attributes: Mapping[str, Any]) -> tuple[date, date] | None:
    """The first and last day a bill's lines cover, or None without both."""
    start = _day(attributes.get("period_start"))
    end = _day(attributes.get("period_end"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _check_line(raw: Any, rates_for: RatesFor) -> LineCheck:
    line: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    unit = line.get("unit")
    unit = unit if isinstance(unit, str) else None
    billed = _number(line.get("unit_price"))
    start, end = _day(line.get("start")), _day(line.get("end"))

    def result(status: str, priced: float | None = None, reason: str | None = None) -> LineCheck:
        return LineCheck(
            label=_text(line.get("label")),
            section=_text(line.get("section")),
            start=_text(line.get("start")),
            end=_text(line.get("end")),
            unit=unit,
            billed=billed,
            priced=priced,
            status=status,
            reason=reason,
        )

    spec = _LABELS.get(_normal(_text(line.get("label"))))
    if spec is None:
        return result(NOT_COMPARED, reason="no rate priced here under that label")
    what, wanted = spec
    if unit != wanted:
        return result(NOT_COMPARED, reason=f"billed per {unit or 'nothing'}, not per {wanted}")
    if billed is None:
        return result(NOT_COMPARED, reason="a flat amount")
    if start is None or end is None or end < start:
        return result(NOT_COMPARED, reason="no period")
    rates: list[tuple[str, Rate]] = []
    for month in months_between(start, end):
        held = rates_for(month)
        rate = held if isinstance(held, str) else held.get(what, "not priced here")
        if isinstance(rate, str):
            return result(NOT_COMPARED, reason=rate)
        rates.append((month, rate))
    first = rates[0][1]
    for month, rate in rates[1:]:
        if not math.isclose(rate.value, first.value, rel_tol=1e-9, abs_tol=1e-12):
            return result(NOT_COMPARED, reason=f"the rate priced here changes in {month}")
    status = OFF if abs(billed - first.value) > first.tolerance else OK
    return result(status, priced=round(first.value, 7))


def check_bill(attributes: Mapping[str, Any], rates_for: RatesFor) -> BillCheck | None:
    """Every line of a bill as the Eneco integration's last bill sensor
    shows it (``attributes``), held against ``rates_for``; None for a
    sensor showing no bill. Each month is asked for once."""
    lines = attributes.get("lines")
    if not isinstance(lines, list) or not lines:
        return None
    asked: dict[str, MonthRates | str] = {}

    def cached(month: str) -> MonthRates | str:
        if month not in asked:
            asked[month] = rates_for(month)
        return asked[month]

    issued = _day(attributes.get("issued"))
    period = bill_period(attributes)
    return BillCheck(
        issued=None if issued is None else issued.isoformat(),
        period_start=None if period is None else period[0].isoformat(),
        period_end=None if period is None else period[1].isoformat(),
        lines=tuple(_check_line(line, cached) for line in lines),
    )
