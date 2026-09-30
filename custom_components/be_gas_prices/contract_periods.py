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

"""The contracts a household held earlier in the year.

When a household changes supplier, the options flow records the settings it
had and the last day they applied, so the year's running cost bills each day
on the contract that supplied it. Each earlier contract is priced on its own
supplier's cards, month by month, the way the current one is.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

import aiohttp

from .bill import month_key
from .const import (
    CONF_CALIBER,
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_DSO,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_SUPPLIER,
    CONF_TARIFF_CARD_DATE,
    CONF_YTD_FROM_CONTRACT_START,
    DEFAULT_CALIBER,
    MANUAL_RATE_KEYS,
)
from .month_cards import ArchiveUnavailable, MonthCardCache, current_card
from .pricing import PricingError
from .providers.base import ExtractorError, IndexTable, SupplierExtractor, SupplierSnapshot
from .running_costs import Household, RunningCosts, running_costs
from .running_costs import months_between as _months_between

_LOGGER = logging.getLogger(__name__)

# What an earlier contract keeps of the entry: enough to price it, nothing
# about the meter, which stays the household's.
PERIOD_KEYS = (
    CONF_SUPPLIER,
    CONF_CONTRACT,
    CONF_REGION,
    CONF_DSO,
    CONF_CALIBER,
    CONF_CONTRACT_START_DATE,
    CONF_TARIFF_CARD_DATE,
)


def previous_contracts(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The recorded earlier contracts, oldest first, each with a valid
    ``until`` date; anything malformed is left out rather than guessed."""
    out: list[dict[str, Any]] = []
    for period in data.get(CONF_PREVIOUS_CONTRACTS) or []:
        if not isinstance(period, dict):
            continue
        try:
            date.fromisoformat(str(period["until"]))
        except (KeyError, ValueError):
            continue
        if period.get(CONF_SUPPLIER) and period.get(CONF_CONTRACT) and period.get(CONF_DSO):
            out.append(period)
    return sorted(out, key=lambda period: str(period["until"]))


def record_switch(data: dict[str, Any], switched: date) -> dict[str, Any]:
    """The entry's data with its current contract moved into the earlier
    contracts, ending the day before ``switched``, and the new contract
    starting on it. The caller then asks for the new contract's settings."""
    period = {key: data[key] for key in PERIOD_KEYS if key in data}
    period["until"] = (switched - timedelta(days=1)).isoformat()
    new = dict(data)
    new[CONF_PREVIOUS_CONTRACTS] = [*previous_contracts(data), period]
    new[CONF_CONTRACT_START_DATE] = switched.isoformat()
    # The old contract's card month, end date and typed figures are not the
    # new one's: left in, the next steps would offer them for it.
    for key in (CONF_TARIFF_CARD_DATE, CONF_CONTRACT_END_DATE, *MANUAL_RATE_KEYS):
        new.pop(key, None)
    # The year now starts with the earlier contract, so counting it from the
    # new contract's start would drop the days the earlier one supplied.
    new.pop(CONF_YTD_FROM_CONTRACT_START, None)
    return new


def periods_this_year(data: dict[str, Any], today: date) -> list[tuple[dict[str, Any], date, date]]:
    """Each earlier contract that supplied part of this year, with the first
    and last day of it that falls in the year."""
    year_start = date(today.year, 1, 1)
    out: list[tuple[dict[str, Any], date, date]] = []
    start = year_start
    for period in previous_contracts(data):
        until = date.fromisoformat(str(period["until"]))
        if until >= year_start and until >= start:
            out.append((period, start, min(until, today)))
        start = max(start, until + timedelta(days=1))
    return out


def current_period_start(data: dict[str, Any], default: date, today: date) -> date:
    """The first day the current contract supplied this year: the day after
    the last earlier contract ended, or ``default``."""
    periods = periods_this_year(data, today)
    if not periods:
        return default
    return max(default, periods[-1][2] + timedelta(days=1))


class PeriodBilling:
    """Prices the earlier contracts of the year on their own suppliers' cards.

    Their current cards and index tables are fetched at most once a day and
    kept here; their past months come from the entry's month card cache,
    which is keyed by supplier and contract already.
    """

    def __init__(self, months: MonthCardCache) -> None:
        self._months = months
        self._cards: dict[tuple[str, str, str], tuple[date, SupplierSnapshot]] = {}
        self._tables: dict[str, tuple[date, IndexTable | None]] = {}

    async def _current_card(
        self,
        session: aiohttp.ClientSession,
        extractor: SupplierExtractor,
        contract: str,
        region: str,
        today: date,
        use_archive: bool,
    ) -> SupplierSnapshot | None:
        key = (extractor.id, contract, region)
        held = self._cards.get(key)
        if held is not None and held[0] == today:
            return held[1]
        try:
            card, _source = await current_card(
                session, extractor, contract, region, f"{today:%Y-%m}", use_archive=use_archive
            )
        except ExtractorError as err:
            _LOGGER.debug("%s card for an earlier contract not read: %s", extractor.label, err)
            return None if held is None else held[1]
        self._cards[key] = (today, card)
        return card

    async def _table(
        self, session: aiohttp.ClientSession, extractor: SupplierExtractor, today: date
    ) -> IndexTable | None:
        held = self._tables.get(extractor.id)
        if held is not None and held[0] == today:
            return held[1]
        table = None if held is None else held[1]
        if extractor.fetch_index is not None:
            try:
                table = await extractor.fetch_index(session)
            except ExtractorError as err:
                _LOGGER.debug("%s index values not read: %s", extractor.label, err)
        self._tables[extractor.id] = (today, table)
        return table

    async def bill(
        self,
        session: aiohttp.ClientSession,
        data: dict[str, Any],
        today: date,
        kwh_days: Mapping[date, float],
        annual_kwh: float,
        *,
        use_archive: bool,
    ) -> tuple[list[RunningCosts], list[str]]:
        """What each earlier contract of the year cost, and the ones that
        could not be priced (their supplier is gone or its card unreadable)."""
        from .providers import get as get_extractor

        costs: list[RunningCosts] = []
        missing: list[str] = []
        current = month_key(today)
        for period, start, end in periods_this_year(data, today):
            try:
                extractor = get_extractor(str(period[CONF_SUPPLIER]))
            except ExtractorError:
                missing.append(str(period[CONF_SUPPLIER]))
                continue
            contract = str(period[CONF_CONTRACT])
            region = str(period[CONF_REGION])
            for month in _months_between(start, end):
                if month < current:
                    try:
                        await self._months.card(
                            session, extractor, contract, region, month, use_archive=use_archive
                        )
                    except (ExtractorError, ArchiveUnavailable) as err:
                        _LOGGER.debug("%s card for %s not read: %s", extractor.label, month, err)

            def month_card(
                month: str, _e: str = extractor.id, _c: str = contract, _r: str = region
            ) -> SupplierSnapshot | None:
                row = self._months.get(_e, _c, _r, month)
                return None if row is None else row.snapshot

            end_card = month_card(month_key(end)) if month_key(end) < current else None
            if end_card is None:
                end_card = await self._current_card(
                    session, extractor, contract, region, today, use_archive
                )
            if end_card is None:
                missing.append(extractor.label)
                continue
            household = Household(
                dso=str(period[CONF_DSO]),
                caliber=str(period.get(CONF_CALIBER, DEFAULT_CALIBER)),
                annual_kwh=annual_kwh,
            )
            try:
                costs.append(
                    running_costs(
                        kwh_days=kwh_days,
                        today=end,
                        window_start=start,
                        household=household,
                        current_card=end_card,
                        month_card=month_card,
                        energy_for=lambda card: card.energy,
                        table=await self._table(session, extractor, today),
                    )
                )
            except PricingError as err:
                _LOGGER.warning("%s: earlier contract not priced: %s", extractor.label, err)
                missing.append(extractor.label)
        return costs, missing
