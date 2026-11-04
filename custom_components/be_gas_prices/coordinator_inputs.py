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

"""What the coordinator prices the card with: the supplier's index values,
the calorific values a cubic metre is converted with, the past months'
cards and the energy leg the contract is billed on.
"""

from __future__ import annotations

import logging
import ssl
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import calorific
from .bill import bill_month, contract_leg, fee_waiver, merged_index, month_key
from .const import (
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT_START_DATE,
    CONF_CONVERSION_FACTOR,
    CONF_CONVERSION_MODE,
    CONF_STATION,
    CONVERSION_MANUAL,
    DEFAULT_CARD_ARCHIVE,
    DEFAULT_CONVERSION_MODE,
    SUPPLIER_CUSTOM,
)
from .contract_periods import card_key_due, parse_date, signing_keys
from .coordinator_data import Conversion
from .gas_meter import MeterKind
from .month_cards import ArchiveUnavailable, MonthCardCache
from .pricing import PriceBreakdown, PricingError
from .providers._pdf import guarded, is_transient_fetch_error, memoise_text_fetches
from .providers._rates import EnergyRates
from .providers.base import ExtractorError, IndexTable, SupplierExtractor, SupplierSnapshot
from .running_costs import Household

_LOGGER = logging.getLogger(__name__)

# The index values and the calorific values move monthly; once a day catches
# a publication within a day.
INDEX_TTL = timedelta(hours=12)
CALORIFIC_TTL = timedelta(hours=24)


class _InputsMixin:
    """Mixed into GasCoordinator."""

    # State GasCoordinator.__init__ creates, as bare annotations: a value
    # here would change what hasattr() and the instance dict see.
    _gcv: dict[str, float]
    _gcv_context: ssl.SSLContext | None
    _gcv_fetched_at: datetime | None
    _index_fetched_at: datetime | None
    _index_table: IndexTable | None
    _months: MonthCardCache
    _session: aiohttp.ClientSession
    _snapshot: SupplierSnapshot | None
    extractor: SupplierExtractor
    hass: HomeAssistant
    household: Household | None

    if TYPE_CHECKING:
        # Defined by GasCoordinator, its other mixins or DataUpdateCoordinator.
        @property
        def _data(self) -> dict[str, Any]: ...
        @property
        def contract(self) -> str: ...
        def period_start(self, today: date) -> date: ...
        @property
        def region(self) -> str: ...

    # ---- index values and calorific values ---------------------------------

    async def _refresh_index(self) -> None:
        fetch = self.extractor.fetch_index
        if fetch is None:
            return
        now = dt_util.utcnow()
        if self._index_fetched_at is not None and now - self._index_fetched_at < INDEX_TTL:
            return
        try:
            fetched = await guarded(self.extractor.label, fetch(self._session))
        except ExtractorError as err:
            _LOGGER.warning("%s: index values not refreshed: %s", self.extractor.label, err)
            if self._index_table is None and is_transient_fetch_error(str(err)):
                # Nothing held: every indexed month waits on it at the card's
                # printed price, so a source that may answer the next time is
                # asked again next tick.
                return
            # A held table keeps pricing, and a source that refuses the
            # read will refuse it next hour too: not worth an hourly retry.
        else:
            self._index_table = merged_index(
                self._index_table, fetched, month_key(dt_util.now().date())
            )
        self._index_fetched_at = now

    async def _refresh_calorific(self) -> None:
        station = self._data.get(CONF_STATION)
        if self._conversion_mode() == CONVERSION_MANUAL or not station:
            return
        now = dt_util.utcnow()
        if self._gcv_fetched_at is not None and now - self._gcv_fetched_at < CALORIFIC_TTL:
            return
        try:
            if self._gcv_context is None:
                self._gcv_context = await self.hass.async_add_executor_job(
                    calorific.build_ssl_context
                )
            key = await calorific.subscription_key(self._session)
            months = await calorific.list_months(self._session, self._gcv_context, key)
            wanted = [m for m in sorted(months) if m >= f"{dt_util.now().year - 1}-01"]
        except calorific.CalorificError as err:
            # Asked again next tick: with no value held for the station,
            # every running cost waits on it.
            _LOGGER.warning("calorific values not refreshed: %s", err)
            return
        failed = False
        for month in wanted:
            if month in self._gcv:
                continue
            # Each month on its own: one file that cannot be read must not
            # keep every later month from being read.
            try:
                values = await calorific.fetch_month(
                    self._session, self._gcv_context, key, months[month]
                )
            except calorific.CalorificError as err:
                _LOGGER.warning("calorific values of %s not read: %s", month, err)
                failed = failed or is_transient_fetch_error(str(err))
                continue
            for held_station, value in values.items():
                if held_station.ean == station:
                    self._gcv[month] = value
        if failed and not self._gcv:
            # Still nothing for the station, and a file a retry may cure:
            # asked again next tick. One the parser refuses waits a day.
            return
        self._gcv_fetched_at = now

    def _conversion_mode(self) -> str:
        return str(self._data.get(CONF_CONVERSION_MODE, DEFAULT_CONVERSION_MODE))

    def _factor_for(self, kind: MeterKind | None) -> Any:
        """The kWh one meter unit is worth in a month, as a function."""

        def factor(month: str) -> float | None:
            if kind == "energy":
                return 1.0
            return self._m3_factor(month)[0]

        return factor

    def _m3_factor(self, month: str) -> tuple[float | None, str | None]:
        """The kWh one cubic metre is worth in ``month``, and the month the
        value belongs to (the latest published before it while ``month``
        itself is not)."""
        if self._conversion_mode() == CONVERSION_MANUAL:
            value = self._data.get(CONF_CONVERSION_FACTOR)
            return (float(value), None) if value else (None, None)
        if month in self._gcv:
            return self._gcv[month], month
        earlier = [key for key in self._gcv if key < month]
        if not earlier:
            return None, None
        latest = max(earlier)
        return self._gcv[latest], latest

    def _conversion(self, month: str) -> Conversion:
        factor, published = self._m3_factor(month)
        if factor is None:
            return Conversion(factor=None, source="none")
        source = "manual" if self._conversion_mode() == CONVERSION_MANUAL else "station"
        return Conversion(factor=factor, source=source, month=published)

    # ---- month cards --------------------------------------------------------

    def _months_needed(self, today: date) -> list[str]:
        if self.extractor.id == SUPPLIER_CUSTOM:
            # Every month bills on the typed card.
            return []
        start = self.period_start(today)
        current = month_key(today)
        months: list[str] = []
        year, month = start.year, start.month
        while f"{year}-{month:02d}" < current:
            months.append(f"{year}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        months += [
            key
            for key in signing_keys(self._data, self.extractor)
            if card_key_due(key, today) and key not in months
        ]
        return months

    async def async_fill_month_cards(self, months: list[str]) -> None:
        if self.extractor.id == SUPPLIER_CUSTOM:
            # Nothing to fetch: the typed card is every month's.
            return
        use_archive = bool(self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE))
        # One fill reads a page or card once for all its months: Bolt's
        # variable months each walk down the same versions of its card.
        with memoise_text_fetches({}):
            for month in months:
                # The cache answers a held month at once, and asks again for
                # one it holds as absent once that answer has aged.
                try:
                    await self._months.card(
                        self._session,
                        self.extractor,
                        self.contract,
                        self.region,
                        month,
                        use_archive=use_archive,
                    )
                except (ExtractorError, ArchiveUnavailable) as err:
                    _LOGGER.debug("%s card for %s not read: %s", self.extractor.label, month, err)

    def _month_card(self, month: str) -> SupplierSnapshot | None:
        if self.extractor.id == SUPPLIER_CUSTOM:
            # The typed card is every month's own.
            return self._snapshot
        row = self._months.get(self.extractor.id, self.contract, self.region, month)
        return None if row is None else row.snapshot

    def _signing_card(self) -> SupplierSnapshot | None:
        """The card the contract was signed on, when the entry names a
        signing month the archive can serve: the one of the day signed where
        the supplier reissues within a month and serves it."""
        keys = signing_keys(self._data, self.extractor)
        signed = self._month_card(keys[0]) if len(keys) > 1 else None
        if signed is None and keys:
            today_month = month_key(dt_util.now().date())
            signing = keys[-1]
            signed = self._snapshot if signing == today_month else self._month_card(signing)
        return signed

    def _energy_for(self, card: SupplierSnapshot, vat_rate: float) -> EnergyRates:
        """The energy leg a card bills this household on at ``vat_rate``, the
        residential rate of the month billed: the signing card's where it is
        held and the contract holds its figures, with the figures the
        household typed from its contract laid over it."""
        signed = self._signing_card()
        return contract_leg(
            card.energy, None if signed is None else signed.energy, self._data, vat_rate
        )

    def _fee_waived(self) -> tuple[date, date] | None:
        """The days the contract pays no supplier fee: the first months the
        card it was signed on waives it for, from its start."""
        return fee_waiver(
            self._signing_card(), parse_date(self._data.get(CONF_CONTRACT_START_DATE))
        )

    def month_price(
        self, month: str, *, own_card: bool = False
    ) -> tuple[PriceBreakdown, float | None] | None:
        """The all-in price of ``month`` and the kWh a cubic metre was worth
        in it, priced the way the running costs price it: the month's own
        card where one is held and prices the household, the current card
        otherwise, or with ``own_card`` not at all. None before the first
        tick has priced the household."""
        snapshot = self._snapshot
        household = self.household
        if snapshot is None or household is None:
            return None
        cards = [snapshot]
        # The custom supplier's typed card prices every month.
        if month != month_key(dt_util.now().date()) and self.extractor.id != SUPPLIER_CUSTOM:
            held = self._month_card(month)
            cards = ([] if held is None else [held]) + ([] if own_card else [snapshot])
        for card in cards:
            try:
                bill = bill_month(
                    month=month,
                    card=card,
                    energy_for=self._energy_for,
                    table=self._index_table,
                    dso=household.dso,
                    annual_kwh=household.annual_kwh,
                    caliber=household.caliber,
                    kwh=0.0,
                    days=0,
                    days_in_year=365,
                )
            except PricingError:
                # A month's own card missing the household's DSO or tier is
                # no better than none, as in the running costs.
                continue
            return bill.breakdown, self._m3_factor(month)[0]
        return None
