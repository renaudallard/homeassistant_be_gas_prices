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

"""A button that fetches the tariff card again, whatever its age."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import MIN_FETCH_INTERVAL, GasCoordinator
from .sensor import device_info

_REFRESH = ButtonEntityDescription(
    key="refresh",
    translation_key="refresh",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([RefreshButton(entry.runtime_data)])


class RefreshButton(CoordinatorEntity[GasCoordinator], ButtonEntity):
    """Fetch the card, the index values and the calorific values again, by
    the tick running if it is a full one yet to check its card, else by the
    next one."""

    _attr_has_entity_name = True
    entity_description = _REFRESH

    def __init__(self, coordinator: GasCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_refresh"
        self._attr_device_info = device_info(coordinator.entry, coordinator.extractor.label)

    @property
    def available(self) -> bool:
        # Pressing it is how an entry whose card cannot be read is retried.
        return True

    async def async_press(self) -> None:
        # Any user allowed to press a button can press this one, and every
        # press fetched the card, the index values and the calorific values
        # again past their age, so pressing it in a loop fetched all three in
        # a loop. A press within the coordinator's MIN_FETCH_INTERVAL of the
        # last full tick, or while a forced fetch is on its way, is refused
        # and says when the button can fetch again.
        if held := self.coordinator.fetch_held_for():
            ready = dt_util.utcnow() + held
            # Up to the minute, so a press at the time it gives fetches.
            shown = ready.replace(second=0, microsecond=0)
            if shown < ready:
                shown += timedelta(minutes=1)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="refresh_pressed_recently",
                translation_placeholders={
                    "last": dt_util.as_local(ready - MIN_FETCH_INTERVAL).strftime("%H:%M"),
                    "next": dt_util.as_local(shown).strftime("%H:%M"),
                },
            )
        # Counted as a fetch from here, before anything is awaited, so
        # presses arriving meanwhile are refused rather than queued.
        await self.coordinator.async_force_refresh()
