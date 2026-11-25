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

"""What the meter's gas over a closed run of days cost, priced here.

For the bill_cost action, which a supplier's bill is held against: the days
it covers are billed the way the running costs bill the year, each month on
its own card with the energy leg the contract is billed on, at the volume
the last tick priced the tier and the excise slices on. A month without a
card of its own is billed on the last month's, or on the card in hand.

Only the current contract is priced: a window reaching back before the last
recorded switch is refused rather than billed on the wrong supplier.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType

from .bill import month_key
from .const import DOMAIN, MEASURED_YEAR_GAP_DAYS
from .contract_periods import belgian_today, card_key_due
from .gas_meter import RecorderUnavailable, consumption, daily_changes, statistic_kind
from .pricing import PricingError
from .providers._pdf import shown_text
from .providers._validity import earliest_priced_month
from .running_costs import RunningCosts, months_between, running_costs, to_kwh

if TYPE_CHECKING:
    from .coordinator import GasCoordinator
    from .providers.base import SupplierSnapshot


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def _failed(key: str, **placeholders: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN, translation_key=key, translation_placeholders=placeholders
    )


def check_window(coordinator: GasCoordinator, start: date, end: date, today: date) -> None:
    """Refuse a window that is not a closed run of days of the current
    contract the cards reach back to."""
    if end < start:
        raise _invalid("window_reversed")
    if end >= today:
        raise _invalid("window_not_closed")
    earliest = earliest_priced_month(today)
    if start < earliest:
        raise _invalid("start_date_too_early", earliest=earliest.isoformat())
    switched = coordinator.switch_day()
    if switched is not None and start < switched:
        raise _invalid("window_before_switch", switched=switched.isoformat())


async def window_cost(
    coordinator: GasCoordinator, start: date, end: date
) -> dict[str, JsonValueType]:
    """What the gas the meter recorded from ``start`` to ``end`` cost, VAT
    included, with the kWh, how much of the window the meter covers and
    which months are priced on another month's card or provisionally."""
    today = dt_util.now().date()
    check_window(coordinator, start, end, today)
    snapshot = coordinator._snapshot
    household = coordinator.household
    if snapshot is None or household is None:
        raise _failed("nothing_priced_yet")
    meter = coordinator.meter
    if meter is None:
        raise _failed("no_meter")
    due = belgian_today()
    await coordinator.async_fill_month_cards(
        [month for month in months_between(start, end) if card_key_due(month, due)]
    )
    try:
        kind = await statistic_kind(coordinator.hass, meter)
        # Read ahead of the window, as the year is: the first day returned
        # is measured against the last total before the read, and may hold
        # an outage's gas.
        ahead = start - timedelta(days=MEASURED_YEAR_GAP_DAYS + 1)
        changes = (
            {} if kind is None else await daily_changes(coordinator.hass, meter, kind, ahead, end)
        )
    except RecorderUnavailable as err:
        raise _failed("meter_not_read", error=str(err)) from err
    kwh_days = to_kwh(consumption(changes), coordinator._factor_for(kind), start)
    if kwh_days is None:
        raise _failed("no_conversion_factor")

    end_month = month_key(end)
    end_card = snapshot if end_month == month_key(today) else coordinator._month_card(end_month)
    stand_in = end_card is None
    costs: RunningCosts | None = None
    error: PricingError | None = None
    cards: list[SupplierSnapshot] = [] if end_card is None else [end_card]
    if end_card is not snapshot:
        cards.append(snapshot)
    for card in cards:
        try:
            costs = running_costs(
                kwh_days=kwh_days,
                today=end,
                window_start=start,
                household=household,
                current_card=card,
                month_card=coordinator._month_card,
                energy_for=coordinator._energy_for,
                table=coordinator._index_table,
                fee_waived=coordinator._fee_waived(),
            )
        except PricingError as err:
            # The last month's own card cannot price the household: the
            # card in hand stands in, as in the running costs.
            error = err
            stand_in = True
            continue
        break
    if costs is None:
        raise _failed("window_not_priced", error=shown_text(str(error)))

    on_record = sorted(day for day in changes if start <= day <= end)
    stand_ins: list[JsonValueType] = list(costs.months_on_current_card)
    if stand_in and end_month not in stand_ins:
        stand_ins.append(end_month)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cost": round(costs.current_year_cost, 2),
        "kwh": round(costs.ytd_kwh, 3),
        # Whether the meter holds the whole window: a day before it, which
        # the first day of it is measured from, and its last day.
        "covered": bool(changes) and min(changes) < start and end in changes,
        "days": (end - start).days + 1,
        "days_on_record": len(on_record),
        "first_day_on_record": on_record[0].isoformat() if on_record else None,
        "annual_kwh": round(household.annual_kwh, 1),
        "months_on_stand_in_card": stand_ins,
        "provisional_months": [bill.month for bill in costs.months if bill.provisional],
        "months": [
            {
                "month": bill.month,
                "kwh": round(bill.kwh, 3),
                "days": bill.days,
                "energy": round(bill.energy_cost, 2),
                "network": round(bill.network_cost, 2),
                "taxes": round(bill.taxes_cost, 2),
                "fixed": round(bill.fixed_cost, 2),
                "total": round(bill.total, 2),
            }
            for bill in costs.months
        ],
    }
