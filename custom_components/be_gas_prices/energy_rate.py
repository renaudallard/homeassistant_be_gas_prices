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

"""What a kWh of the contract's energy costs, month by month.

For the energy_rate action, which a supplier's coming yearly bill is
projected with: the energy leg the contract is billed on, VAT included, each
month on its own card and index as the running costs price it. A month the
index has not reached yet is priced at the latest value published, and a
month after this one on the card in hand; both are marked provisional unless
the price is fixed. Network, levies and fixed costs are left out: the bill
being projected prices those itself.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType

from .bill import bill_month, month_key
from .bill_cost import _failed, _invalid, earliest_bill_day
from .contract_periods import belgian_today, card_key_due
from .pricing import PricingError
from .providers._pdf import shown_text
from .providers._rates import FixedRates
from .running_costs import days_in_year, months_between

if TYPE_CHECKING:
    from .coordinator import GasCoordinator
    from .providers.base import SupplierSnapshot

# How far past this month a projection may reach: a yearly bill's period
# runs a year and a few days from its last reading.
MONTHS_AHEAD = 14


def _months_after(month: str, count: int) -> str:
    year, number = (int(part) for part in month.split("-"))
    number += count
    return f"{year + (number - 1) // 12}-{(number - 1) % 12 + 1:02d}"


def check_months(coordinator: GasCoordinator, start: date, end: date, today: date) -> None:
    """Refuse months the cards do not reach back to, too far ahead, or of
    an earlier contract."""
    if end < start:
        raise _invalid("window_reversed")
    earliest = earliest_bill_day(today)
    if start < earliest:
        raise _invalid("bill_start_too_early", earliest=earliest.isoformat())
    latest = _months_after(month_key(today), MONTHS_AHEAD)
    if month_key(end) > latest:
        raise _invalid("rate_end_too_late", latest=latest)
    switched = coordinator.switch_day()
    if switched is not None and start < switched:
        raise _invalid("window_before_switch", switched=switched.isoformat())


async def energy_rates(
    coordinator: GasCoordinator, start: date, end: date
) -> dict[str, JsonValueType]:
    """The energy price of every month from ``start``'s to ``end``'s, in EUR
    per kWh VAT included, with whether it is final and whether the month
    was priced on the card in hand rather than its own."""
    today = dt_util.now().date()
    check_months(coordinator, start, end, today)
    snapshot = coordinator._snapshot
    household = coordinator.household
    if snapshot is None or household is None:
        raise _failed("nothing_priced_yet")
    months = months_between(start, end)
    due = belgian_today()
    await coordinator.async_fill_month_cards(
        [month for month in months if card_key_due(month, due)]
    )
    current = month_key(today)
    rates: list[JsonValueType] = []
    for month in months:
        own = None if month >= current else coordinator._month_card(month)
        cards: list[SupplierSnapshot] = [] if own is None else [own]
        cards.append(snapshot)
        error: PricingError | None = None
        for card in cards:
            try:
                bill = bill_month(
                    month=month,
                    card=card,
                    energy_for=coordinator._energy_for,
                    table=coordinator._index_table,
                    dso=household.dso,
                    annual_kwh=household.annual_kwh,
                    caliber=household.caliber,
                    kwh=1.0,
                    days=1,
                    days_in_year=days_in_year(int(month[:4])),
                )
            except PricingError as err:
                # A month's own card that cannot price the household gives
                # way to the card in hand, as in the running costs.
                error = err
                continue
            break
        else:
            raise _failed("window_not_priced", error=shown_text(str(error)))
        energy = coordinator._energy_for(card, 0.0)
        rates.append(
            {
                "month": month,
                "eur_per_kwh": round(bill.breakdown.energy, 6),
                "provisional": bill.provisional
                or (month > current and not isinstance(energy, FixedRates)),
                "stand_in_card": card is snapshot and month < current,
            }
        )
    return {"start": start.isoformat(), "end": end.isoformat(), "months": rates}
