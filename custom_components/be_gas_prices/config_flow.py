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

"""The setup wizard and the options flow.

Both walk the same steps: where the household is (a postcode resolves the
region and the gas DSO), which supplier and contract it is on, the household
facts the bill needs, and how a cubic metre of its gas becomes kWh. The
options flow starts from the stored entry and writes it back.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

from . import postcodes
from .bill import month_key
from .compare import IndexCache, OwnContract, Quote, quote_contract, rank
from .compare_table import quote_table
from .const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CALIBER,
    CONF_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_DSO,
    CONF_POSTCODE,
    CONF_REGION,
    CONF_SUPPLIER,
    CONF_SWITCH_DATE,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_CALIBER,
    DEFAULT_CARD_ARCHIVE,
    DOMAIN,
    SUPPLIER_CUSTOM,
    SUPPLIER_SOCIAL,
)
from .contract_periods import (
    parse_date,
    previous_contracts,
    record_switch,
    removable_switch,
    remove_last_switch,
)
from .daily_ranking import DailyRanking
from .flow_steps import _contracts_for, _FlowSteps, _select, _suppliers_for
from .providers import get as get_extractor
from .providers.base import ExtractorError
from .providers.custom import build_snapshot as build_custom_snapshot
from .running_costs import Household

# How long the ranked comparison spends fetching before it shows what it has.
COMPARE_BUDGET_S = 120.0

# The box on the stored ranking that quotes every contract again.
_PRICE_AGAIN = "price_again"


def _title(data: dict[str, Any]) -> str:
    """The title the wizard gives an entry: its contract's label, that of a
    contract the supplier withdrew included."""
    extractor = get_extractor(data[CONF_SUPPLIER])
    return extractor.contract_label(data[CONF_CONTRACT]) or extractor.label


class BeGasPricesConfigFlow(_FlowSteps, ConfigFlow, domain=DOMAIN):
    """The setup wizard."""

    VERSION = 1

    def __init__(self) -> None:
        self._data = {}
        self._candidates = ()
        self._stations_cache = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_postcode(user_input)

    async def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title=_title(self._data), data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BeGasPricesOptionsFlow()


class BeGasPricesOptionsFlow(_FlowSteps, OptionsFlow):
    """Edit the settings, compare contracts, or record a supplier switch."""

    def __init__(self) -> None:
        self._candidates = ()
        self._stations_cache = None
        self._compare_supplier: str | None = None
        self._quotes: tuple[Quote, Quote] | None = None
        self._ranking: tuple[list[Quote], int] | None = None
        self._rank_task: asyncio.Task[tuple[list[Quote], int]] | None = None
        self._switch_date: str | None = None
        self._price_again = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        options = ["settings", "compare", "compare_all", "switch"]
        if removable_switch(self.config_entry.data, dt_util.now().date()) is not None:
            options.append("remove_switch")
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._data = dict(self.config_entry.data)
        code = self._data.get(CONF_POSTCODE)
        match = postcodes.resolve(str(code)) if code else None
        self._candidates = () if match is None else match.dsos
        return await self.async_step_postcode()

    async def _async_finish(self) -> ConfigFlowResult:
        entry = self.config_entry
        # The title the wizard gave follows the contract; one the user typed
        # is theirs and stays.
        try:
            made = entry.title == _title(dict(entry.data))
        except ExtractorError:
            made = False
        title = _title(self._data) if made else entry.title
        self.hass.config_entries.async_update_entry(entry, data=self._data, title=title)
        if entry.state is not ConfigEntryState.LOADED:
            # An entry stopped at setup (a contract its supplier withdrew)
            # has no update listener yet to set it up again.
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data={})

    # ---- comparison ----------------------------------------------------------

    def _household(self) -> Household:
        coordinator = getattr(self.config_entry, "runtime_data", None)
        measured = getattr(coordinator, "household", None)
        if isinstance(measured, Household):
            return measured
        data = self.config_entry.data
        return Household(
            dso=str(data[CONF_DSO]),
            caliber=str(data.get(CONF_CALIBER, DEFAULT_CALIBER)),
            annual_kwh=float(data.get(CONF_ANNUAL_CONSUMPTION_KWH, DEFAULT_ANNUAL_CONSUMPTION_KWH)),
        )

    def _own_contract(self) -> OwnContract | None:
        """The household's contract as the comparisons quote it: on the card
        the loaded entry holds, with the energy leg it is billed on, else a
        custom entry's typed card; None leaves it to the card of the month."""
        own_contract = getattr(
            getattr(self.config_entry, "runtime_data", None), "own_contract", None
        )
        held = own_contract() if callable(own_contract) else None
        if isinstance(held, OwnContract):
            return held
        data = self.config_entry.data
        if data[CONF_SUPPLIER] != SUPPLIER_CUSTOM:
            return None
        return OwnContract(
            get_extractor(SUPPLIER_CUSTOM),
            str(data[CONF_CONTRACT]),
            build_custom_snapshot(dict(data)),
        )

    async def async_step_compare(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        region = self.config_entry.data[CONF_REGION]
        if user_input is not None:
            self._compare_supplier = user_input[CONF_SUPPLIER]
            return await self.async_step_compare_contract()
        # The custom supplier has no card of its own to quote, and the social
        # tariff is for protected households only.
        options = [
            o
            for o in _suppliers_for(region)
            if o["value"] not in (SUPPLIER_CUSTOM, SUPPLIER_SOCIAL)
        ]
        schema = vol.Schema({vol.Required(CONF_SUPPLIER): _select(options)})
        return self.async_show_form(step_id="compare", data_schema=schema)

    async def async_step_compare_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        data = self.config_entry.data
        region = data[CONF_REGION]
        supplier = self._compare_supplier or data[CONF_SUPPLIER]
        contracts = _contracts_for(supplier, region)
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            household = self._household()
            month = month_key(dt_util.now().date())
            use_archive = bool(data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE))
            held = self._own_contract()
            indices = IndexCache(held)
            mine = quote_contract(
                session,
                get_extractor(data[CONF_SUPPLIER]),
                data[CONF_CONTRACT],
                region,
                household,
                month,
                indices,
                use_archive=use_archive,
                card=None if held is None else held.card,
                read_by_ocr=held is not None and held.read_by_ocr,
                fee_waived=None if held is None else held.fee_waived,
            )
            if (supplier, user_input[CONF_CONTRACT]) == (data[CONF_SUPPLIER], data[CONF_CONTRACT]):
                # The household's own contract: one quote, on the card it holds.
                own = await mine
                other = own
            else:
                other, own = await asyncio.gather(
                    quote_contract(
                        session,
                        get_extractor(supplier),
                        user_input[CONF_CONTRACT],
                        region,
                        household,
                        month,
                        indices,
                        use_archive=use_archive,
                    ),
                    mine,
                )
            self._quotes = (own, other)
            return await self.async_step_compare_result()
        schema = vol.Schema(
            {
                vol.Required(CONF_CONTRACT): SelectSelector(
                    SelectSelectorConfig(
                        options=[SelectOptionDict(value=c.id, label=c.label) for c in contracts],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="compare_contract", data_schema=schema)

    async def async_step_compare_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None or self._quotes is None:
            return self.async_abort(reason="compare_done")
        own, other = self._quotes
        return self.async_show_form(
            step_id="compare_result",
            data_schema=vol.Schema({}),
            description_placeholders={
                "table": quote_table([own] if other is own else [own, other], own=own),
                "annual_kwh": f"{self._household().annual_kwh:.0f}",
            },
        )

    async def async_step_compare_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._rank_task is None and not self._price_again:
            # An entry that ranks daily has today's answer already: shown
            # rather than making the household wait two minutes for it.
            stored = getattr(
                getattr(self.config_entry, "runtime_data", None), "daily_ranking", None
            )
            if (
                isinstance(stored, DailyRanking)
                and stored.day == dt_util.now().date()
                and stored.rows
                and stored.tabled
            ):
                return await self.async_step_compare_all_stored(ranking=stored)
        if self._rank_task is None:
            data = self.config_entry.data
            self._rank_task = self.hass.async_create_task(
                rank(
                    async_get_clientsession(self.hass),
                    data[CONF_REGION],
                    self._household(),
                    month_key(dt_util.now().date()),
                    use_archive=bool(data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
                    budget_s=COMPARE_BUDGET_S,
                    own=self._own_contract(),
                )
            )
        if not self._rank_task.done():
            return self.async_show_progress(
                step_id="compare_all",
                progress_action="compare_all",
                progress_task=self._rank_task,
            )
        self._ranking = self._rank_task.result()
        return self.async_show_progress_done(next_step_id="compare_all_result")

    async def async_step_compare_all_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None or self._ranking is None:
            return self.async_abort(reason="compare_done")
        quotes, skipped = self._ranking
        data = self.config_entry.data
        own = next(
            (
                q
                for q in quotes
                if (q.supplier, q.contract) == (data[CONF_SUPPLIER], data[CONF_CONTRACT])
            ),
            None,
        )
        return self.async_show_form(
            step_id="compare_all_result",
            data_schema=vol.Schema({}),
            description_placeholders={
                "table": quote_table(quotes, own=own),
                "annual_kwh": f"{self._household().annual_kwh:.0f}",
                "skipped": str(skipped),
            },
        )

    async def async_step_compare_all_stored(
        self, user_input: dict[str, Any] | None = None, *, ranking: DailyRanking | None = None
    ) -> ConfigFlowResult:
        """Today's daily ranking, with a box to quote every contract again."""
        if user_input is not None:
            if user_input.get(_PRICE_AGAIN):
                self._price_again = True
                return await self.async_step_compare_all()
            return self.async_abort(reason="compare_done")
        if ranking is None:
            return self.async_abort(reason="compare_done")
        quotes = [row.quote() for row in ranking.rows]
        own = next((q for q in quotes if (q.supplier, q.contract) == ranking.own), None)
        return self.async_show_form(
            step_id="compare_all_stored",
            data_schema=vol.Schema({vol.Optional(_PRICE_AGAIN, default=False): BooleanSelector()}),
            description_placeholders={
                "table": quote_table(quotes, own=own),
                "annual_kwh": f"{self._household().annual_kwh:.0f}",
                "day": ranking.day.isoformat(),
            },
        )

    # ---- supplier switch -------------------------------------------------------

    async def async_step_switch(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Record that the household changed contract this year: the current
        settings become an earlier contract billed up to the day before the
        switch, and the steps that follow set up the new one."""
        errors: dict[str, str] = {}
        today = dt_util.now().date()
        if user_input is not None:
            switched = date.fromisoformat(str(user_input[CONF_SWITCH_DATE]))
            data = self.config_entry.data
            # The contract the change closes must have supplied a day of this
            # year: from 1 January, the day after the last change recorded,
            # or its own start, whichever comes last.
            began = parse_date(data.get(CONF_CONTRACT_START_DATE))
            first = max(
                [date(today.year, 1, 1)]
                + [
                    date.fromisoformat(p["until"]) + timedelta(days=1)
                    for p in previous_contracts(data)
                ]
                + ([] if began is None else [began])
            )
            if not first < switched <= today:
                errors[CONF_SWITCH_DATE] = "switch_date_invalid"
            else:
                self._data = record_switch(dict(self.config_entry.data), switched)
                self._candidates = ()
                code = self._data.get(CONF_POSTCODE)
                match = postcodes.resolve(str(code)) if code else None
                self._candidates = () if match is None else match.dsos
                return await self.async_step_supplier()
        schema = vol.Schema({vol.Required(CONF_SWITCH_DATE): DateSelector()})
        return self.async_show_form(step_id="switch", data_schema=schema, errors=errors)

    async def async_step_remove_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Undo the last change of contract recorded this year, after saying
        which one it is: the way to correct a mistyped change date, by
        removing it and recording it again."""
        data = dict(self.config_entry.data)
        last = removable_switch(data, dt_util.now().date())
        if last is None:
            return self.async_abort(reason="no_switch_recorded")
        if user_input is not None:
            self._data = remove_last_switch(data)
            return await self._async_finish()
        switched = date.fromisoformat(last["until"]) + timedelta(days=1)
        try:
            contract = _title(last)
        except ExtractorError:
            # A supplier this release no longer knows.
            contract = str(last[CONF_CONTRACT])
        return self.async_show_form(
            step_id="remove_switch",
            description_placeholders={"date": switched.isoformat(), "contract": contract},
        )
