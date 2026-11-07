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

"""The coordinator's card: when it is due, fetching it, and what stands in
for it while the supplier cannot be read, a withdrawn contract's last card
or the card archive's.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.util import dt as dt_util

from .const import CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE, SUPPLIER_CUSTOM
from .contract_periods import last_month_sold
from .month_cards import ArchiveUnavailable, MonthCardCache, current_card, fetch_archived_row
from .providers._pdf import is_missing_card_error, is_transient_fetch_error, shown_text
from .providers.base import (
    CardNotReadableError,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)
from .providers.custom import build_snapshot as build_custom_snapshot

_LOGGER = logging.getLogger(__name__)

# A card is fetched again after this long when the supplier offers no probe.
SNAPSHOT_TTL = timedelta(hours=24)

# A card this old, or this long past the last day it prices, is stale: the
# supplier has not been readable for a week, or has not published a new month.
SNAPSHOT_STALE_AGE = timedelta(days=7)
SNAPSHOT_STALE_AFTER_VALIDITY = timedelta(days=7)
# What the card fetch of one tick may take. The slowest path measured on a
# Raspberry Pi 4 under load is Bolt's: its current card in 38 s, and a
# month's card, which walks back through the versions of a variable card,
# in up to 54 s more. Twice that, so a slow day still finishes, while a
# supplier that never finishes answering cannot hold the tick for good.
CARD_FETCH_BUDGET_S = 180
# The card archive is asked back this many months for a card to stand in.
_ARCHIVE_MONTHS_BACK = 12


def _newer(card: SupplierSnapshot, held: SupplierSnapshot) -> bool:
    """Whether ``card`` is a later card than ``held``: by its end date, or by
    the month it names where a card has none, being in force until the next
    one (Bolt's variable cards)."""
    if card.valid_until is None or held.valid_until is None:
        return card.publication_label > held.publication_label
    return card.valid_until > held.valid_until


class _CardMixin:
    """Mixed into GasCoordinator."""

    # State GasCoordinator.__init__ creates, as bare annotations: a value
    # here would change what hasattr() and the instance dict see.
    _card_source: str
    _failing_since: date | None
    _failures: int
    _fetched_at: datetime | None
    _force_asked: int
    _force_made: int
    _force_refresh: bool
    _months: MonthCardCache
    _probe_key: str | None
    _session: aiohttp.ClientSession
    _snapshot: SupplierSnapshot | None
    _stand_in: bool
    card_missing: bool
    card_read_by_ocr: bool
    card_unreadable: bool
    extractor: SupplierExtractor
    last_error: str

    if TYPE_CHECKING:
        # Defined by GasCoordinator, its other mixins or DataUpdateCoordinator.
        @property
        def _data(self) -> dict[str, Any]: ...
        @property
        def contract(self) -> str: ...
        @property
        def region(self) -> str: ...
        @property
        def withdrawn(self) -> bool: ...

    # ---- the card ---------------------------------------------------------

    def snapshot_age(self) -> timedelta | None:
        if self._fetched_at is None:
            return None
        return dt_util.utcnow() - self._fetched_at

    def snapshot_stale(self) -> bool:
        age = self.snapshot_age()
        if self._snapshot is None or age is None or self.withdrawn:
            # A withdrawn contract's last card will not change again.
            return False
        if age > SNAPSHOT_STALE_AGE:
            return True
        valid_until = self._snapshot.valid_until
        return (
            valid_until is not None
            and dt_util.now().date() > valid_until + SNAPSHOT_STALE_AFTER_VALIDITY
        )

    async def _card_is_due(self) -> tuple[bool, str | None]:
        """Whether the card must be fetched, and the probe key seen.

        A card from the Store or a stand-in from the card archive is due at
        once; the archive's reading of an unreadable card is the card of the
        month, and is asked for again when the supplier's card changes and
        once a day besides, since the archive reads a card reissued in the
        month a day after the key moved. The probe is asked either way, so
        a card fetched for another reason is kept against the key it was
        fetched under rather than fetched again on the next tick.
        """
        key = None
        if self.extractor.probe is not None:
            try:
                key = await self.extractor.probe(self._session, self.contract, self.region)
            except Exception:
                # A probe answers None on any failure it expects: one it
                # does not is no signal either, and the card's age decides.
                _LOGGER.warning("%s: probe failed", self.extractor.label, exc_info=True)
        # Read past the probe, which can take seconds: every fetch forced by
        # now is made by this tick, a press during the probe included, and
        # the press's own tick has nothing left to fetch.
        self._force_made = self._force_asked
        if self._snapshot is None or self._force_refresh or self._stand_in:
            return True, key
        # A card whose month is over is asked for again on every tick until
        # its successor is out, probe or not: the successor may have gone up
        # before its month began, under the key the card in hand is kept
        # against, and no later change of key would ever come for it.
        valid_until = self._snapshot.valid_until
        if valid_until is not None and dt_util.now().date() > valid_until:
            return True, key
        if key is not None and (key != self._probe_key or not self.card_read_by_ocr):
            return key != self._probe_key, key
        age = self.snapshot_age()
        return age is None or age >= SNAPSHOT_TTL, key

    async def _refresh_snapshot(self) -> None:
        if self.extractor.id == SUPPLIER_CUSTOM:
            # The household's typed card: nothing to fetch, never stale.
            self._snapshot = build_custom_snapshot(self._data)
            self._fetched_at = dt_util.utcnow()
            self._card_source = "live"
            self._stand_in = False
            self.last_error = ""
            self._force_made = self._force_asked
            return
        if self.withdrawn:
            # Nothing to fetch: the last card held, from the store, else the
            # card of the last month it was sold, keeps pricing the contract.
            self._force_made = self._force_asked
            self._failures = 0
            self.card_unreadable = self.card_missing = False
            if self._snapshot is None:
                await self._adopt_last_card()
            self.last_error = (
                ""
                if self._snapshot is not None
                else f"{self.extractor.label} withdrew this contract and no card of it is held"
            )
            return
        due, key = await self._card_is_due()
        if not due:
            if key is not None and not self.card_read_by_ocr:
                # The supplier still serves the card in hand: as good as a
                # fetch, and what keeps a month's card from ageing into
                # staleness between two publications.
                self._fetched_at = dt_util.utcnow()
            return
        try:
            async with asyncio.timeout(CARD_FETCH_BUDGET_S):
                snapshot, source = await current_card(
                    self._session,
                    self.extractor,
                    self.contract,
                    self.region,
                    f"{dt_util.now().date():%Y-%m}",
                    use_archive=bool(self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
                )
        except CardNotReadableError as err:
            self.card_missing = False
            if self.card_read_by_ocr and self._snapshot is not None:
                # The card archive's reading of last month's card is in hand,
                # read here, restored from the store or taken as a stand-in,
                # and the archive has not read the new month's yet: it reads
                # once a day, so this is a wait, not an unreadable card.
                self.card_unreadable = False
                self.card_read_by_ocr = True
                self._fetch_failed(str(err), transient=True)
            else:
                self.card_unreadable = True
                self.card_read_by_ocr = False
                self._fetch_failed(str(err), transient=False)
        except ExtractorError as err:
            transient = is_transient_fetch_error(str(err))
            # A network failure says nothing about the card: one known to be
            # unreadable or missing stays so, rather than its count of
            # failures being read as a layout change.
            if not transient:
                self.card_unreadable = False
                self.card_missing = is_missing_card_error(str(err))
            self._fetch_failed(str(err), transient=transient)
        except TimeoutError:
            # Like a network failure: it says nothing about the card, and
            # the next tick may well finish.
            self._fetch_failed(
                f"{self.extractor.label}: the card fetch did not finish within "
                f"{CARD_FETCH_BUDGET_S} s",
                transient=True,
            )
        else:
            self._snapshot = snapshot
            self._fetched_at = dt_util.utcnow()
            self._probe_key = key
            self._card_source = source
            self._stand_in = False
            self._force_refresh = False
            self._failures = 0
            self.card_unreadable = False
            self.card_missing = False
            self.card_read_by_ocr = source == "ocr"
            self.last_error = ""
            return
        if self._snapshot is None or self.snapshot_stale():
            await self._adopt_archived_card()

    def _fetch_failed(self, message: str, *, transient: bool) -> None:
        # A parser's message can quote the card or page, as long as they make
        # it; last_error reaches every user's attributes and diagnostics.
        message = shown_text(message)
        self.last_error = message
        if not transient:
            if not self._failures:
                self._failing_since = dt_util.now().date()
            self._failures += 1
        _LOGGER.warning("%s: tariff card not refreshed: %s", self.extractor.label, message)

    async def _adopt_last_card(self) -> None:
        """Stand a withdrawn contract's last card in when none is held: the
        card of the last month it was sold, from the card archive where the
        entry reads it, else from the supplier's own archive."""
        month = last_month_sold(self.extractor, self.contract)
        if month is None:
            return
        try:
            row = await self._months.card(
                self._session,
                self.extractor,
                self.contract,
                self.region,
                month,
                use_archive=bool(self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
            )
        except (ExtractorError, ArchiveUnavailable) as err:
            _LOGGER.debug("%s card for %s not read: %s", self.extractor.label, month, err)
            return
        if row.snapshot is None:
            return
        year, number = (int(part) for part in month.split("-"))
        self._snapshot = row.snapshot
        self._card_source = "archive" if row.source == "archive" else "live"
        self._stand_in = True
        self.card_read_by_ocr = False
        self._fetched_at = dt_util.as_utc(dt_util.start_of_local_day(date(year, number, 1)))

    async def _adopt_archived_card(self) -> None:
        """Stand the card archive's latest row in for a card we cannot read.

        This month's row first, then back a month at a time, stopping at the
        first one that holds a card and as soon as GitHub fails to answer,
        since a month it could not read may still hold one. Taken only if it
        is newer than what is held.
        """
        if not self._data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE):
            return
        today = dt_util.now().date()
        year, month = today.year, today.month
        for _ in range(_ARCHIVE_MONTHS_BACK):
            key = f"{year}-{month:02d}"
            try:
                row = await fetch_archived_row(
                    self._session, self.extractor.id, self.contract, self.region, key
                )
            except ArchiveUnavailable as err:
                _LOGGER.debug("card archive unavailable: %s", err)
                return
            if row is not None:
                card, read_by_ocr = row
                held = self._snapshot
                if held is None or _newer(card, held):
                    self._snapshot = card
                    self._card_source = "archive"
                    self._stand_in = True
                    self.card_read_by_ocr = read_by_ocr
                    if read_by_ocr:
                        # The archive's reading of a card published as
                        # images: what such a card is priced on, as good as
                        # a fetch, so the card found unreadable is no
                        # failure while the archive has not read the newest.
                        self._fetched_at = dt_util.utcnow()
                        if self.card_unreadable:
                            self._failures = 0
                            self.card_unreadable = False
                    else:
                        self._fetched_at = dt_util.as_utc(
                            dt_util.start_of_local_day(date(year, month, 1))
                        )
                return
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
