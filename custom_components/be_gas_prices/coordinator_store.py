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

"""The coordinator's store: what an entry keeps across a restart, and the
digests that say whether a figure held for it still stands.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from . import excise_law, fluvius, law_archive, network_archive, tarbel, vat_rates
from .const import CONF_DSO, CONF_STATION, DOMAIN
from .contract_periods import PeriodBilling, periods_this_year
from .daily_ranking import DailyRanking
from .month_cards import MonthCardCache
from .network_sheet import held_network, takes_network_sheet
from .providers._resolve import LAW_FIGURES, tier_for
from .providers.base import IndexTable, SupplierExtractor, SupplierSnapshot
from .running_costs import Household
from .snapshot_codec import SnapshotDecodeError, snapshot_from_json, snapshot_to_json

_LOGGER = logging.getLogger(__name__)


def _digest(value: Any) -> str:
    """A short digest of what JSON makes of value."""
    blob = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _card_digest(card: SupplierSnapshot) -> str:
    """A card's figures, digested."""
    return _digest(snapshot_to_json(card))


class _StoreMixin:
    """Mixed into GasCoordinator."""

    # State GasCoordinator.__init__ creates, as bare annotations: a value
    # here would change what hasattr() and the instance dict see.
    _card_source: str
    _failing_since: date | None
    _failures: int
    _fetched_at: datetime | None
    _gcv: dict[str, float]
    _held: dict[str, Any] | None
    _index_table: IndexTable | None
    _months: MonthCardCache
    _periods: PeriodBilling
    _release: str | None
    _snapshot: SupplierSnapshot | None
    _stand_in: bool
    _store: Store[dict[str, Any]]
    backfill_stamp: str | None
    card_missing: bool
    card_read_by_ocr: bool
    card_unreadable: bool
    cost_backfill_stamp: str | None
    daily_ranking: DailyRanking | None
    extractor: SupplierExtractor
    hass: HomeAssistant
    household: Household | None

    if TYPE_CHECKING:
        # Defined by GasCoordinator, its other mixins or DataUpdateCoordinator.
        @property
        def _data(self) -> dict[str, Any]: ...
        def _month_card(self, month: str) -> SupplierSnapshot | None: ...
        def _months_needed(self, today: date) -> list[str]: ...
        def _set_up(self) -> bool: ...
        @property
        def contract(self) -> str: ...
        @property
        def failing_since(self) -> date | None: ...
        @property
        def region(self) -> str: ...

    # ---- persistence ------------------------------------------------------

    async def async_load_persistent(self) -> None:
        """Restore the last card and the caches, so a restart while the
        supplier is down still prices on something real."""
        self._release = str((await async_get_integration(self.hass, DOMAIN)).version)
        blob = await self._store.async_load()
        if not isinstance(blob, dict):
            return
        try:
            snapshot = snapshot_from_json(blob.get("snapshot"))
            fetched_at = datetime.fromisoformat(blob["fetched_at"])
        except (SnapshotDecodeError, KeyError, TypeError, ValueError):
            snapshot = None
            fetched_at = None
        # Only a card for this contract that prices this household's DSO: a
        # move to another region keeps the supplier and the contract, and a
        # card of the old region would stand in for the new one's.
        restored = (
            snapshot is not None
            and (snapshot.supplier, snapshot.contract) == (self.extractor.id, self.contract)
            and str(self._data.get(CONF_DSO)) in snapshot.dsos
        )
        if restored:
            self._snapshot = snapshot
            self._fetched_at = fetched_at
            self._card_source = "cache"
            self._stand_in = True
            # The archive's OCR reading, as the store says: stored as such
            # again whatever the next check finds, and waited on like one.
            self.card_read_by_ocr = blob.get("read_by_ocr") is True
        # The fetches of this card that failed in a row, and whether it was
        # found unreadable, with or without a card in hand: counted on from
        # there, the first tick after a restart does not delete a Repairs
        # card the user ignored for the next to raise it afresh, and setup
        # retries with no card raise it at all.
        if restored or blob.get("failures_for") == self._card_key():
            failures = blob.get("failures")
            self._failures = failures if isinstance(failures, int) else 0
            since = blob.get("failing_since")
            try:
                self._failing_since = date.fromisoformat(since) if since else None
            except (TypeError, ValueError):
                self._failing_since = None
            self.card_unreadable = blob.get("unreadable") is True
            self.card_missing = blob.get("missing") is True
        # A price is only resolved against its own supplier's publication:
        # a table another supplier published, before a change of supplier,
        # is not restored, even under an index name the two share.
        table = blob.get("index")
        if isinstance(table, dict) and blob.get("index_supplier") == self.extractor.id:
            self._index_table = {
                str(name): {str(k): float(v) for k, v in values.items()}
                for name, values in table.items()
                if isinstance(values, dict)
            }
        excise_law.restore(blob.get("excise_law"))
        excise_law.restore_protected(blob.get("excise_law_protected"))
        excise_law.restore_contribution(blob.get("excise_law_contribution"))
        tarbel.restore(blob.get("tarbel"))
        tarbel.restore_protected(blob.get("tarbel_protected"))
        tarbel.restore_contribution(blob.get("tarbel_contribution"))
        fluvius.restore(blob.get("fluvius"))
        vat_rates.restore(blob.get("vat_rates"))
        law_archive.restore(blob.get("law_archive"))
        network_archive.restore(blob.get("network_archive"))
        gcv = blob.get("gcv")
        if isinstance(gcv, dict) and gcv.get("station") == self._data.get(CONF_STATION):
            self._gcv = {str(k): float(v) for k, v in (gcv.get("values") or {}).items()}
        self._months.load_json(blob.get("months"), reread=blob.get("release") != self._release)
        stamp = blob.get("backfill")
        self.backfill_stamp = stamp if isinstance(stamp, str) else None
        stamp = blob.get("cost_backfill")
        self.cost_backfill_stamp = stamp if isinstance(stamp, str) else None
        held = blob.get("held")
        self._held = held if isinstance(held, dict) else None
        ranking = DailyRanking.from_json(blob.get("ranking"))
        # A ranking made for another contract prices a saving against a
        # contract the household left, and one made under other settings
        # (volume, DSO, caliber, typed figures) for another household: the
        # day is ranked again instead.
        if (
            ranking is not None
            and ranking.own == (self.extractor.id, self.contract)
            and blob.get("ranking_for") == self._settings_digest()
        ):
            self.daily_ranking = ranking

    async def _save_persistent(self) -> None:
        if not self._set_up():
            return
        since = self.failing_since
        payload: dict[str, Any] = {
            "snapshot": None if self._snapshot is None else snapshot_to_json(self._snapshot),
            "fetched_at": None if self._fetched_at is None else self._fetched_at.isoformat(),
            "read_by_ocr": self.card_read_by_ocr,
            "failures": self._failures,
            "failing_since": None if since is None else since.isoformat(),
            "unreadable": self.card_unreadable,
            "missing": self.card_missing,
            "failures_for": self._card_key(),
            "index": self._index_table,
            "index_supplier": self.extractor.id,
            "gcv": {"station": self._data.get(CONF_STATION), "values": self._gcv},
            "excise_law": excise_law.held_table(),
            "excise_law_protected": excise_law.protected_held_table(),
            "excise_law_contribution": excise_law.contribution_held_table(),
            "tarbel": tarbel.held_table(),
            "tarbel_protected": tarbel.protected_held_table(),
            "tarbel_contribution": tarbel.contribution_held_table(),
            "fluvius": fluvius.held_table(),
            "vat_rates": vat_rates.held_table(),
            "law_archive": law_archive.held_table(),
            "months": self._months.to_json(),
            "release": self._release,
            "backfill": self.backfill_stamp,
            "cost_backfill": self.cost_backfill_stamp,
            "ranking": None if self.daily_ranking is None else self.daily_ranking.to_json(),
            "ranking_for": self._settings_digest(),
            "held": self._held,
        }
        if self._sheet_contracts(dt_util.now().date()):
            # What the card archive kept of the network sheets, only where a
            # contract of the year takes its network from one.
            payload["network_archive"] = network_archive.held_table()
        await self._store.async_save(payload)

    def _card_key(self) -> list[str]:
        """The supplier, contract and region the card is fetched for: what
        the stored count of failed fetches belongs to."""
        return [self.extractor.id, self.contract, self.region]

    def _settings_digest(self) -> str:
        """The entry's settings, digested: what a stored ranking was made
        under."""
        return _digest(self._data)

    async def async_save(self) -> None:
        """Write the store now, outside a tick."""
        await self._save_persistent()

    def _sheet_contracts(self, today: date) -> list[dict[str, Any]]:
        """The contracts of the year that take their network from a sheet:
        the entry's own and the earlier ones recorded at a switch."""
        contracts = [
            self._data,
            *(period for period, _first, _last in periods_this_year(self._data, today)),
        ]
        return [contract for contract in contracts if takes_network_sheet(contract)]

    def _network_sheets_held(self, today: date) -> list[Any]:
        """What the network sheets held give each month of the year so far,
        for each contract of the year that takes its network from one."""
        days = [date(today.year, month, 1) for month in range(1, today.month + 1)]
        return [
            [
                None
                if (held := held_network(day, str(contract.get(CONF_DSO, "")))) is None
                else asdict(held)
                for day in days
            ]
            for contract in self._sheet_contracts(today)
        ]

    def earlier_contracts_signature(self) -> str:
        """What the earlier contracts of the year are priced from, digested,
        or "" without one a card prices (``PeriodBilling.priced_from``). The
        cost history bills their days, so it is drawn again when a card or
        an index value of theirs lands; the price history starts at the
        last change of contract and has no use for it."""
        held = self._periods.priced_from(self._data, dt_util.now().date())
        return _digest(held) if held else ""

    def card_months_signature(self) -> str:
        """What the year's past months are priced from: the entry's
        settings, each past month's own card and the card in hand as read,
        the index values, the calorific values, the figures of the law and
        the tier the measured volume falls in, and the network a custom
        contract takes off its DSO's tariff sheet. It moves when any of them
        changes or lands, a card read again after an update, the value of
        an index that lags the others or a sheet read after the history was
        drawn included, which is when the price history is worth drawing
        again. The excise blend within a tier, which moves the price a few
        hundredths of a cent, does not move it."""
        today = dt_util.now().date()
        own = [
            f"{month}:{_card_digest(card)}"
            for month in self._months_needed(today)
            if (card := self._month_card(month)) is not None
        ]
        index = _digest(self._index_table)
        gcv = _digest(self._gcv)
        current = "" if self._snapshot is None else _card_digest(self._snapshot)
        figures: list[Any] = [
            LAW_FIGURES,
            excise_law.held_table(),
            excise_law.protected_held_table(),
            excise_law.contribution_held_table(),
            tarbel.held_table(),
            tarbel.protected_held_table(),
            tarbel.contribution_held_table(),
            fluvius.held_table(),
            vat_rates.held_table(),
            law_archive.held_table(),
        ]
        # Left out where no contract takes a sheet, so every other entry's
        # stamp stays what it was.
        sheets = self._network_sheets_held(today)
        if sheets:
            figures.append(sheets)
        law = _digest(figures)
        tier = "" if self.household is None else tier_for(self.household.annual_kwh)
        return f"{self._settings_digest()}|{','.join(own)}|{index}|{gcv}|{current}|{law}|{tier}"
