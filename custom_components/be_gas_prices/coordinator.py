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

"""The data coordinator of one config entry.

Ticks hourly. Each tick keeps the supplier's card fresh (a cheap probe where
the supplier offers one, a 24-hour TTL otherwise), refreshes the supplier's
index values, the calorific values and the excise law once a day, reads the
gas meter out of the recorder, and prices the household on all of it.

A card that cannot be fetched is never a reason to stop pricing: the last
good one keeps serving, restored from the entry's store across a restart,
and when there is none or it has gone stale the card archive stands in. The
Repairs cards say when either happens.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import (
    excise_law,
    fluvius,
    law_archive,
    network_archive,
    network_sheet,
    tarbel,
    vat_rates,
)
from .bill import IndexValue, bill_month, month_key
from .compare import OwnContract, rank
from .const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CALIBER,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_DAILY_COMPARE,
    CONF_DSO,
    CONF_GAS_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    CONF_YTD_FROM_CONTRACT_START,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_CALIBER,
    DEFAULT_CARD_ARCHIVE,
    DEFAULT_DAILY_COMPARE,
    DOMAIN,
    REGION_FLANDERS,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from .contract_periods import (
    PeriodBilling,
    current_period_start,
    parse_date,
    periods_this_year,
    switch_day,
)
from .coordinator_card import _CardMixin
from .coordinator_data import CoordinatorData
from .coordinator_inputs import _InputsMixin
from .coordinator_store import _StoreMixin
from .daily_ranking import DailyRanking, ranking_minute
from .gas_meter import (
    MeterKind,
    RecorderUnavailable,
    daily_consumption,
    discover_energy_gas_meter,
    statistic_kind,
)
from .issues import sync_issues
from .month_cards import (
    MonthCardCache,
)
from .pricing import PricingError, fixed_costs
from .providers import get as get_extractor
from .providers._pdf import shown_text
from .providers._resolve import resolve_for_delivery, tier_for
from .providers.base import (
    IndexTable,
    SupplierExtractor,
    SupplierSnapshot,
)
from .running_costs import (
    Household,
    RunningCosts,
    meter_start,
    rolling_year_kwh,
    running_costs,
    to_kwh,
)

_LOGGER = logging.getLogger(__name__)

# The shortest time between two ticks that ask the supplier, Atrias and the
# rest. Home Assistant's update_entity lets any user who may control an
# entity ask for a refresh every ten seconds, and every refresh was a full
# tick: the supplier's probe each time, and the card itself whenever it is
# due, as in the first days of every month or on an entry whose card fails.
# A refresh within this time of the last full tick publishes what it holds
# and asks nobody, and the refresh button refuses a press. The hourly tick
# goes through the same check and is never held: Home Assistant arms it an
# hour after the end of the last refresh, on the loop clock this is counted
# on. A forced fetch (the refresh action, the button, the Repair's retry)
# is not held either.
MIN_FETCH_INTERVAL = timedelta(minutes=15)


class GasCoordinator(_StoreMixin, _CardMixin, _InputsMixin, DataUpdateCoordinator[CoordinatorData]):
    """Fetches the card, reads the meter and prices one household."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, *, defer_meter_reads: bool = False
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.extractor: SupplierExtractor = get_extractor(entry.data[CONF_SUPPLIER])
        self._snapshot: SupplierSnapshot | None = None
        self._fetched_at: datetime | None = None
        self._probe_key: str | None = None
        self._card_source = "live"
        # The card in hand stands in for the one of the month: restored from
        # the Store, the card archive's latest row taken after a failure, or
        # a withdrawn contract's last card.
        # Due at once, unlike a card read for the month, here or by the
        # card archive, which is due when it changes or ages.
        self._stand_in = False
        self._force_refresh = False
        self._failures = 0
        # The day the run of failures _failures counts began.
        self._failing_since: date | None = None
        self.last_error = ""
        # The last error was the card failing to price the household.
        self._pricing_error = ""
        self.card_unreadable = False
        # The last fetch found no card at the address (a 404 or 410, or a
        # web page in its place): no layout change, the supplier has none.
        self.card_missing = False
        # The card in hand is the card archive's OCR reading of a card
        # published as page images.
        self.card_read_by_ocr = False
        self._index_table: IndexTable | None = None
        self._index_fetched_at: datetime | None = None
        self._gcv: dict[str, float] = {}
        self._gcv_fetched_at: datetime | None = None
        self._gcv_context: ssl.SSLContext | None = None
        self._months = MonthCardCache()
        # The release the store was written by: past months' cards read by
        # another release are read again, so a parser fix reaches them, and
        # kept where no one serves them any more.
        self._release: str | None = None
        self._month_fill: asyncio.Task[None] | None = None
        self._first_tick = True
        self.meter: str | None = None
        # None until a tick names the meter: one failing before that, or
        # still waiting for the energy manager, says nothing of how many
        # there are.
        self.meter_count: int | None = None
        self.meter_error = ""
        # The household the last tick priced, measured volume included, for
        # the price history backfill to price past months the same way.
        self.household: Household | None = None
        # Whether the next tick leaves the recorder alone: asked for by setup,
        # whose first refresh Home Assistant waits on with 300 s for every
        # integration together, and a year of meter statistics can take
        # seconds on a database on a NAS. That tick publishes the figures the
        # last one that read the meter left (_held), and setup asks for the
        # one that reads it in the background (meter_reads_pending).
        self._meter_reads_deferred = defer_meter_reads
        self.meter_reads_pending = False
        # Set for a tick that prices again on what is in hand (async_reprice),
        # asking neither the supplier nor Atrias.
        self._reprice_only = False
        # The past months' cards landed, and the last landing a tick that
        # read the meter has priced: the fill's reprice runs only while one
        # has not been (async_reprice).
        self._cards_landed = 0
        self._cards_priced = 0
        # Fetches forced so far (async_force_refresh), and how many of them
        # a full tick has read at its card check, past the probe, and so
        # made: one asked for since is still to be made (_make_forced_fetch).
        self._force_asked = 0
        self._force_made = 0
        # When the last full tick began, on the event loop's clock: what
        # MIN_FETCH_INTERVAL counts from.
        self._last_fetch_at: float | None = None
        self._held: dict[str, Any] | None = None
        # What the price history was last drawn from (backfill.py), kept in
        # the store so a restart does not redraw an unchanged year.
        self.backfill_stamp: str | None = None
        # The same for the running year's cost (backfill_cost.py).
        self.cost_backfill_stamp: str | None = None
        self._periods = PeriodBilling(self._months)
        self.unpriced_periods: list[str] = []
        # Those of them that supplied part of the running month.
        self.unpriced_this_month: list[str] = []
        # The last daily ranking (compare.py), kept across restarts, and the
        # task computing the next one.
        self.daily_ranking: DailyRanking | None = None
        self._ranking_task: asyncio.Task[None] | None = None

    # ---- settings ---------------------------------------------------------

    @property
    def _data(self) -> dict[str, Any]:
        return dict(self.entry.data)

    @property
    def contract(self) -> str:
        return str(self._data[CONF_CONTRACT])

    @property
    def region(self) -> str:
        return str(self._data[CONF_REGION])

    @property
    def withdrawn(self) -> bool:
        """Whether the supplier withdrew the contract: no card of it is
        published any more, so the last one held prices it."""
        return any(
            c.id == self.contract and c.withdrawn is not None for c in self.extractor.contracts
        )

    def window_start(self, today: date) -> date:
        """The first day the running costs cover: 1 January, or the contract
        start when the entry bills from it. With an earlier contract this
        year, the first day it bills, which is its own start where it billed
        from there: recording or removing a switch then leaves the year's
        last_reset where it was, as it leaves the bill."""
        start = date(today.year, 1, 1)
        periods = periods_this_year(self._data, today)
        if periods:
            return periods[0][1]
        contract_start = parse_date(self._data.get(CONF_CONTRACT_START_DATE))
        if self._data.get(CONF_YTD_FROM_CONTRACT_START) and contract_start is not None:
            return max(start, min(contract_start, today))
        return start

    def period_start(self, today: date) -> date:
        """The first day the current contract supplied this year."""
        return current_period_start(self._data, self.window_start(today), today)

    def _billed_in_flanders(self, today: date) -> bool:
        """Whether a contract billed this year is Flemish: the current one,
        or an earlier one of a household that has moved since."""
        return self.region == REGION_FLANDERS or any(
            period.get(CONF_REGION) == REGION_FLANDERS
            for period, _first, _last in periods_this_year(self._data, today)
        )

    def switch_day(self) -> date | None:
        """The day the current contract took over from the last one recorded
        at a switch, whatever its year, None when none is recorded."""
        return switch_day(self._data)

    # ---- the tick -------------------------------------------------------------

    async def async_reprice(self, needed: Callable[[], bool]) -> None:
        """A tick that prices again on the card, index and calorific values in
        hand, the meter read, while ``needed`` says one still is: the one
        setup starts once Home Assistant no longer waits on it, which reads
        the meter its own tick left out, and the one the past months' cards
        ask for once fetched. Whichever gets the lock first does the other's
        work too, so a restart whose past months' cards are all in the store
        reads the meter once; cards fetched again land later and their own
        reprice reads it again to price them. Neither asks the supplier or
        Atrias again seconds after a tick did, which would count a card that
        fails twice for one failure, unless a fetch was forced meanwhile. The
        lock async_refresh takes is taken first, so no other refresh waiting
        on it, a Repairs fix flow's forced fetch say, can take the flag meant
        for this one."""
        async with self._debounced_refresh.async_lock():
            if not needed():
                return
            self._reprice_only = True
            try:
                await self._async_refresh(log_failures=True)
            finally:
                self._reprice_only = False

    async def async_force_refresh(self, *, wait: bool = False) -> None:
        """Fetch the card, the index values and the calorific values again,
        whatever their age: by the tick running if it is a full one yet to
        check its card, else by the next one, made full for it; with
        ``wait`` before returning."""
        self._force_refresh = True
        self._force_asked += 1
        self._index_fetched_at = None
        self._gcv_fetched_at = None
        if wait:
            await self._make_forced_fetch()
        else:
            self.entry.async_create_background_task(
                self.hass, self._make_forced_fetch(), f"{DOMAIN} forced fetch"
            )

    async def _make_forced_fetch(self) -> None:
        """A tick that makes the fetch forced, once the refresh lock is free,
        unless a full tick has checked its card since it was asked and made
        it. Not a requested refresh: Home Assistant drops one whose cooldown
        ends while a long tick holds the lock. And a fetch forced while
        another waits on the lock, a reprice or the fix flow's, is made
        once, so a card that fails is not counted twice for it."""
        asked = self._force_asked
        async with self._debounced_refresh.async_lock():
            if self._force_made >= asked:
                return
            await self._async_refresh(log_failures=True)

    def fetch_held_for(self) -> timedelta:
        """How long a refresh would still ask nobody for
        (``MIN_FETCH_INTERVAL``), zero once the next one asks. A forced
        fetch not yet made counts as one starting now."""
        if self._force_made < self._force_asked:
            return MIN_FETCH_INTERVAL
        if self._last_fetch_at is None:
            return timedelta(0)
        left = self._last_fetch_at + MIN_FETCH_INTERVAL.total_seconds() - self.hass.loop.time()
        return timedelta(seconds=max(left, 0.0))

    @property
    def failures(self) -> int:
        """Consecutive fetches that failed for a reason a retry will not fix."""
        return self._failures

    @property
    def failing_since(self) -> date | None:
        """The day those failures began, or None without any."""
        return self._failing_since if self._failures else None

    @property
    def card_held(self) -> bool:
        """Whether a card prices the entry, fetched, kept or stood in."""
        return self._snapshot is not None

    async def _async_update_data(self) -> CoordinatorData:
        if (
            self.data is not None
            and not self._reprice_only
            and self._force_made >= self._force_asked
            and self.fetch_held_for()
        ):
            # Nothing is priced or read again either: the next tick, at
            # most an hour away, brings the meter's figures on. A tick that
            # failed still stands, or the held refresh would mark the entry
            # recovered with nothing tried.
            _LOGGER.debug("gas tick less than %s after the last one, held", MIN_FETCH_INTERVAL)
            if not self.last_update_success:
                raise UpdateFailed(self.last_error or "the last tick failed")
            return self.data
        try:
            return await self._tick()
        finally:
            if self._set_up():
                sync_issues(self.hass, self)

    def _set_up(self) -> bool:
        """Whether the entry is still set up, or being set up, with this
        coordinator: a refresh that outlives an unload, a removal or a
        reload of the entry leaves no Repairs card or store behind it."""
        entry = self.hass.config_entries.async_get_entry(self.entry.entry_id)
        return (
            entry is not None
            and entry.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
            and getattr(entry, "runtime_data", None) is self
        )

    async def _tick(self) -> CoordinatorData:
        deferred = self._meter_reads_deferred
        self._meter_reads_deferred = False
        if deferred:
            # Set from the start: a setup tick that fails before it has a
            # card read no meter either, and leaves the meter card alone.
            self.meter_reads_pending = True
        # A fetch forced and not yet made (the refresh button, the service)
        # is made by this tick. One made and failed is not: the hourly tick
        # retries it, rather than a reprice seconds after counting it twice.
        reprice_only = self._reprice_only and self._force_made >= self._force_asked
        self._reprice_only = False
        if not reprice_only:
            self._last_fetch_at = self.hass.loop.time()
        try:
            return await self._tick_with(deferred, reprice_only)
        except BaseException:
            if not deferred:
                # Only setup's own tick leaves the meter for later: any
                # other one tried to read it, failing or not, and the
                # Repairs card it may raise and the work waiting on it go
                # ahead.
                self.meter_reads_pending = False
            raise

    async def _tick_with(self, deferred: bool, reprice_only: bool) -> CoordinatorData:
        # Taken before anything is read: an edit saved while this tick runs
        # reloads the entry, and what it reads belongs to the settings it
        # started under.
        digest = self._settings_digest()
        if not reprice_only:
            await self._refresh_snapshot()
        if self._snapshot is None:
            # Written for a setup retry, whose new coordinator counts the
            # failed fetches on from the store.
            await self._save_persistent()
            raise UpdateFailed(self.last_error or "no tariff card available")
        if not reprice_only:
            await self._refresh_index()
            await self._refresh_calorific()
            # The excise and the energy contribution the law sets and the
            # ones FPS Finance's tariff lists, which every residential month
            # is resolved against when it is priced below.
            await excise_law.ensure_excise_law(self._session)
            await tarbel.ensure_tariff(self._session)
            # The residential VAT rate the cards of each month agree on, and
            # what the law billed each month the readings above no longer
            # give, from the card archive: an entry that keeps it out
            # contacts GitHub for nothing and bills on the tables it last read.
            if self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE):
                await vat_rates.ensure_vat_rates(self._session)
                await law_archive.ensure_law_archive(self._session)
                # And what it kept of the network sheets, for a contract of
                # the year taking its network from one.
                if self._sheet_contracts(dt_util.now().date()):
                    await network_archive.ensure_network_archive(self._session)
            if self._billed_in_flanders(dt_util.now().date()):
                # The data management fee a Flemish card may leave out, for
                # this year: the running costs bill no earlier month, and the
                # price history of one is per kWh.
                await fluvius.ensure_tariff_sheet(self._session, dt_util.now().year)
            # The network a custom entry takes off its DSO's tariff sheet, for
            # this year, as the fee above.
            await network_sheet.ensure_network_sheet(self._session, self._data, dt_util.now().year)
        today = dt_util.now().date()
        needed = self._months_needed(today)
        if self._first_tick:
            # The first tick is what setup waits on: fetch the past months'
            # cards, the earlier contracts' too, in the background and price
            # them on the current card until they land.
            self._first_tick = False
            if needed or periods_this_year(self._data, today):
                self._month_fill = self.entry.async_create_background_task(
                    self.hass, self._fill_then_refresh(needed), f"{DOMAIN} month cards"
                )
        elif not self._filling():
            await self.async_fill_month_cards(needed)
        if self._pricing_error and self.last_error == self._pricing_error:
            # Asked again below, before the data the sensors show is built: a
            # card not due for a fetch would keep it.
            self.last_error = ""
        self._pricing_error = ""
        landed = self._cards_landed
        try:
            try:
                data = await self._build(today, deferred=deferred, digest=digest)
            except PricingError:
                if not deferred or self.meter_count is None:
                    # Nothing to read the meter with either while the
                    # energy manager has not named it.
                    raise
                # The typed or held volume falls in a tier the card does not
                # price, where the meter's may not: read it now, or every
                # setup retry would fail on the same volume for good. Read
                # here, it is no longer to come, whatever this build gives.
                deferred = False
                self.meter_reads_pending = False
                data = await self._build(today, digest=digest)
        except PricingError as err:
            # The card cannot price this household: its DSO or its tier is
            # missing, or it gives no gas excise and the law's is not held,
            # for this month or for one the running costs bill.
            message = shown_text(str(err))
            self.last_error = self._pricing_error = message
            # Written as with no card: a setup retry counts on from the
            # count this tick's fetch left, not from an older one.
            await self._save_persistent()
            raise UpdateFailed(message) from err
        # Not left pending by a tick still waiting for the energy manager,
        # which may never load: the ranking and the price history go ahead
        # on the held volume, or the typed one when none is held.
        self.meter_reads_pending = deferred
        if not deferred and self.meter_count is not None:
            # One waiting for the energy manager showed the held figures
            # and priced no card that landed.
            self._cards_priced = landed
        self.maybe_rank(today)
        await self._save_persistent()
        return data

    def maybe_rank(self, today: date) -> None:
        """Start the day's ranking once its minute has come, when the entry
        asked for one. The minute is derived from the entry id so that
        installations do not all fetch every supplier at the same time.

        A listener calls this at the minute itself, and every tick after it
        catches a day the minute was missed on (Home Assistant was down)."""
        if not self._data.get(CONF_DAILY_COMPARE, DEFAULT_DAILY_COMPARE):
            return
        if self.meter_reads_pending:
            # Ranked on the measured volume, once the meter read setup starts
            # after its own tick lands, whether this tick or the listener
            # asks.
            return
        if self.daily_ranking is not None and self.daily_ranking.day == today:
            return
        if self._ranking_task is not None and not self._ranking_task.done():
            return
        now = dt_util.now()
        if now.hour * 60 + now.minute < ranking_minute(self.entry.entry_id):
            return
        household = self.household
        if household is None:
            return
        # Tied to the entry, so an unload, a removal or a failed setup
        # cancels it rather than let it write the entry's store afterwards.
        self._ranking_task = self.entry.async_create_background_task(
            self.hass, self._rank(today, household), f"{DOMAIN} daily ranking"
        )

    async def _rank(self, today: date, household: Household) -> None:
        try:
            quotes, _skipped = await rank(
                self._session,
                self.region,
                household,
                month_key(today),
                use_archive=bool(self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
                own=self.own_contract(),
            )
        except Exception:
            # A background job with nobody watching: log it, and let the next
            # tick try again rather than leave an unretrieved exception.
            _LOGGER.exception("daily ranking failed")
            return
        own = (self.extractor.id, self.contract)
        if not any(q.annual_cost is not None for q in quotes if (q.supplier, q.contract) != own):
            # No other contract could be priced, the network down most
            # likely: not kept as the day's ranking, so the next tick tries
            # again.
            _LOGGER.warning("daily ranking priced no other contract; trying again")
            return
        self.daily_ranking = DailyRanking.from_quotes(
            today, quotes, (self.extractor.id, self.contract)
        )
        await self._save_persistent()
        self.async_update_listeners()

    def own_contract(self) -> OwnContract | None:
        """The household's contract as a comparison quotes it: on the card in
        hand with the energy leg the entry is billed on. None before a card
        is held."""
        snapshot = self._snapshot
        if snapshot is None:
            return None
        return OwnContract(
            self.extractor,
            self.contract,
            replace(
                snapshot,
                energy=self._energy_for(snapshot, vat_rates.residential_vat(dt_util.now().date())),
            ),
            read_by_ocr=self.card_read_by_ocr,
            table=self._index_table,
            fee_waived=self._fee_waived(),
        )

    def _filling(self) -> bool:
        """Whether the first tick's month cards are still being fetched."""
        return self._month_fill is not None and not self._month_fill.done()

    async def _fill_then_refresh(self, months: list[str]) -> None:
        await self.async_fill_month_cards(months)
        await self._periods.fill(
            self._session,
            self._data,
            dt_util.now().date(),
            use_archive=bool(self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
        )
        self._cards_landed += 1
        landed = self._cards_landed
        await self.async_reprice(lambda: self._cards_priced < landed)

    async def _meter(self) -> str | None:
        configured = self._data.get(CONF_GAS_METER)
        if configured:
            self.meter_count = 1
            return str(configured)
        meter, count = await discover_energy_gas_meter(self.hass)
        self.meter_count = count
        if count is None:
            # The energy manager is still loading: the meter the last read
            # named stands until a tick names one.
            held = (self._held or {}).get("meter")
            return held if isinstance(held, str) else None
        return meter

    def _held_for(self, today: date, digest: str) -> dict[str, Any]:
        """The figures the last tick that read the meter left, those that
        still hold: read off the meter in use, priced under the entry's
        settings, the year's for this year and the month's for this month.
        A new year or month, or a setting edited since, leaves the figure
        out rather than show a period it does not cover."""
        held = self._held or {}
        if held.get("inputs") != digest or held.get("meter") != self.meter:
            return {}

        def number(name: str) -> float | None:
            value = held.get(name)
            return float(value) if isinstance(value, (int, float)) else None

        out: dict[str, Any] = {
            "annual_kwh": number("annual_kwh"),
            "rolling_year_kwh": number("rolling_year_kwh"),
        }
        if held.get("year") == today.year:
            out["current_year_cost"] = number("current_year_cost")
            out["ytd_kwh"] = number("ytd_kwh")
            out["projected_year_end_cost"] = number("projected_year_end_cost")
            out["projected_year_kwh"] = number("projected_year_kwh")
            unpriced = held.get("unpriced")
            if isinstance(unpriced, list):
                out["unpriced"] = [str(name) for name in unpriced]
            if held.get("month") == month_key(today):
                out["current_month_cost"] = number("current_month_cost")
                unpriced = held.get("unpriced_month")
                if isinstance(unpriced, list):
                    out["unpriced_month"] = [str(name) for name in unpriced]
        return out

    async def _read_meter(self, today: date) -> tuple[MeterKind | None, dict[date, float]]:
        self.meter_error = ""
        if self.meter is None:
            return None, {}
        try:
            kind = await statistic_kind(self.hass, self.meter)
            if kind is None:
                return None, {}
            days = await daily_consumption(self.hass, self.meter, kind, meter_start(today), today)
        except RecorderUnavailable as err:
            self.meter_error = str(err)
            _LOGGER.warning("gas meter not read: %s", err)
            return None, {}
        return kind, days

    async def _build(
        self, today: date, *, deferred: bool = False, digest: str = ""
    ) -> CoordinatorData:
        snapshot = self._snapshot
        assert snapshot is not None
        household = Household(
            dso=str(self._data[CONF_DSO]),
            caliber=str(self._data.get(CONF_CALIBER, DEFAULT_CALIBER)),
            annual_kwh=float(
                self._data.get(CONF_ANNUAL_CONSUMPTION_KWH, DEFAULT_ANNUAL_CONSUMPTION_KWH)
            ),
        )
        costs: RunningCosts | None = None
        earlier: list[RunningCosts] = []
        # What the meter recorded over the earlier contracts' days, priced
        # or not: a contract no card prices still used its gas.
        earlier_kwh = 0.0
        measured = False
        kwh_days: dict[date, float] | None = None
        held: dict[str, Any] = {}
        # Which meter, from the Energy dashboard's settings and not the
        # recorder, so the several-meters card stands and figures held for
        # another one are not shown.
        self.meter = await self._meter()
        if self.meter_count is None:
            # The energy manager is still loading and has not named the
            # meter: read none, as setup's own tick, and leave the figures
            # held for a tick that does.
            deferred = True
        kind: MeterKind | None = None
        meter_days: dict[date, float] = {}
        if not deferred:
            kind, meter_days = await self._read_meter(today)
            # A recorder that did not answer is no year of no gas: the last
            # figures stand, as on setup's own tick, until a read answers.
            deferred = bool(self.meter_error)
        if deferred:
            # No meter read: the figures the last tick that read it left,
            # those still true (_held_for), and the measured volume they
            # priced the tier on.
            held = self._held_for(today, digest)
            if held.get("annual_kwh") is not None:
                measured = True
                household = Household(
                    dso=household.dso, caliber=household.caliber, annual_kwh=held["annual_kwh"]
                )
            self.unpriced_periods = list(held.get("unpriced", []))
            self.unpriced_this_month = list(held.get("unpriced_month", []))
        else:
            # Named afresh on every tick: last year's earlier contracts are
            # no part of this year's cost.
            self.unpriced_periods = []
            self.unpriced_this_month = []
            kwh_days = (
                to_kwh(meter_days, self._factor_for(kind), date(today.year, 1, 1))
                if meter_days
                else None
            )
        waived = self._fee_waived()
        if kwh_days is not None:
            # A measured year picks the tier and the excise slices; the typed
            # estimate stands in until the meter has one.
            rolling = rolling_year_kwh(kwh_days, today)
            if rolling is not None:
                measured = True
                household = Household(
                    dso=household.dso, caliber=household.caliber, annual_kwh=rolling
                )
            costs = running_costs(
                kwh_days=kwh_days,
                today=today,
                window_start=self.period_start(today),
                household=household,
                current_card=snapshot,
                month_card=self._month_card,
                energy_for=self._energy_for,
                table=self._index_table,
                fee_waived=waived,
            )
            periods = periods_this_year(self._data, today)
            earlier_kwh = sum(
                kwh
                for day, kwh in kwh_days.items()
                if any(first <= day <= last for _period, first, last in periods)
            )
            if periods:
                earlier, unpriced = await self._periods.bill(
                    self._session,
                    self._data,
                    today,
                    kwh_days,
                    household.annual_kwh,
                    use_archive=bool(self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
                    fill=not self._filling(),
                )
                self.unpriced_periods = [u.label for u in unpriced]
                self.unpriced_this_month = [
                    u.label for u in unpriced if month_key(u.until) == month_key(today)
                ]
        month = month_key(today)
        now_bill = bill_month(
            month=month,
            card=snapshot,
            energy_for=self._energy_for,
            table=self._index_table,
            dso=household.dso,
            annual_kwh=household.annual_kwh,
            caliber=household.caliber,
            kwh=0.0,
            days=0,
            days_in_year=365,
        )
        fixed = fixed_costs(
            replace(
                resolve_for_delivery(snapshot, today),
                energy=self._energy_for(snapshot, vat_rates.residential_vat(today)),
            ),
            household.dso,
            household.annual_kwh,
            household.caliber,
        )
        if waived is not None and waived[0] <= today < waived[1]:
            # A year's fixed costs as billed today: none of the fee the
            # signing card waives.
            fixed = replace(fixed, supplier=0.0)
        self.household = household
        index: IndexValue | None = now_bill.index
        age = self.snapshot_age()
        all_bills = tuple(b for period in earlier for b in period.months) + (
            () if costs is None else costs.months
        )
        earlier_cost = sum(period.current_year_cost for period in earlier)
        if deferred:
            rolling = held.get("rolling_year_kwh")
            year_cost = held.get("current_year_cost")
            month_cost = held.get("current_month_cost")
            ytd_kwh = held.get("ytd_kwh")
            year_end_cost = held.get("projected_year_end_cost")
            year_end_kwh = held.get("projected_year_kwh")
        else:
            # Short by a whole contract, a cost would read on the recorder as
            # a large drop and then a rise once that contract is priced: it is
            # unknown instead, the month's only while one that supplied in it
            # is not priced.
            short = bool(self.unpriced_periods)
            rolling = None if costs is None else costs.rolling_year_kwh
            year_cost = None if costs is None or short else costs.current_year_cost + earlier_cost
            month_cost = (
                None
                if costs is None or self.unpriced_this_month
                else sum(b.total for b in all_bills if b.month == month)
            )
            ytd_kwh = None if costs is None else costs.ytd_kwh + earlier_kwh
            year_end_cost = (
                None
                if costs is None or short or costs.projected_year_end_cost is None
                else costs.projected_year_end_cost + earlier_cost
            )
            year_end_kwh = (
                None
                if costs is None or costs.projected_year_kwh is None
                else costs.projected_year_kwh + earlier_kwh
            )
            if not self.meter_error:
                # Held for the next restart's first tick, under the settings
                # this tick started with; a recorder that failed keeps the
                # last figures it gave.
                self._held = {
                    "inputs": digest,
                    "meter": self.meter,
                    "year": today.year,
                    "month": month,
                    "annual_kwh": household.annual_kwh if measured else None,
                    "rolling_year_kwh": rolling,
                    "current_year_cost": year_cost,
                    "current_month_cost": month_cost,
                    "ytd_kwh": ytd_kwh,
                    "projected_year_end_cost": year_end_cost,
                    "projected_year_kwh": year_end_kwh,
                    "unpriced": list(self.unpriced_periods),
                    "unpriced_month": list(self.unpriced_this_month),
                }
        return CoordinatorData(
            snapshot=snapshot,
            card_source=self._card_source,
            breakdown=now_bill.breakdown,
            fixed=fixed,
            index=index,
            price_provisional=now_bill.provisional,
            conversion=self._conversion(month),
            tier=tier_for(household.annual_kwh),
            annual_kwh=household.annual_kwh,
            annual_kwh_measured=measured,
            snapshot_fetched_at=self._fetched_at,
            snapshot_age_hours=None if age is None else age.total_seconds() / 3600,
            snapshot_stale=self.snapshot_stale(),
            last_error=self.last_error,
            day=today,
            current_year_cost=year_cost,
            current_month_cost=month_cost,
            ytd_kwh=ytd_kwh,
            months=all_bills,
            # Every contract's months in order, one shared by two named once.
            months_on_current_card=tuple(
                dict.fromkeys(
                    [m for period in earlier for m in period.months_on_current_card]
                    + ([] if costs is None else list(costs.months_on_current_card))
                )
            ),
            rolling_year_kwh=rolling,
            projected_year_cost=(
                None if rolling is None else rolling * now_bill.breakdown.all_in + fixed.total
            ),
            projected_year_end_cost=year_end_cost,
            projected_year_kwh=year_end_kwh,
            meter=self.meter,
            unpriced_periods=tuple(self.unpriced_periods),
            unpriced_this_month=tuple(self.unpriced_this_month),
        )
