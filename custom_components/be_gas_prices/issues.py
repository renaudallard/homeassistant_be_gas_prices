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

"""The Repairs cards an entry raises and clears.

Each card is raised from the state the tick left behind and deleted as soon
as that state clears, so none outlives the problem it names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .gas_meter import recorder_loaded
from .markdown_text import escape_markdown

if TYPE_CHECKING:
    from .coordinator import GasCoordinator

# A failed fetch is usually a transient outage the next tick recovers; the
# card saying the layout may have changed waits for this many in a row.
EXTRACTOR_ISSUE_THRESHOLD = 2

# From the 1st to the 5th of a month a card that is missing or does not
# parse raises no Repairs card while the entry still prices on a card, if
# its run of failures began that month. Suppliers are often late then and
# change their cards then, and the daily live check files such a failure
# on its next run, so a fix can be out before the 6th. A run that began
# earlier keeps its card, and an entry with no card raises it at once,
# since nothing prices it.
MONTH_START_HOLD_DAYS = 5

ISSUES = (
    "snapshot_stale",
    "extractor_failed",
    "card_missing",
    "card_unreadable",
    "card_read_by_ocr",
    "meter_unit",
    "several_meters",
    "contract_withdrawn",
    "recorder_missing",
)


def issue_id(entry_id: str, name: str) -> str:
    return f"{name}_{entry_id}"


def _sync(
    hass: HomeAssistant,
    entry_id: str,
    name: str,
    active: bool,
    placeholders: dict[str, str],
    *,
    fixable: bool = False,
) -> None:
    key = issue_id(entry_id, name)
    if not active:
        ir.async_delete_issue(hass, DOMAIN, key)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        key,
        is_fixable=fixable,
        severity=ir.IssueSeverity.WARNING,
        translation_key=name,
        translation_placeholders=placeholders,
        data={"entry_id": entry_id},
    )


def _held_at_month_start(coordinator: GasCoordinator) -> bool:
    since = coordinator.failing_since
    today = dt_util.now().date()
    return (
        coordinator.card_held
        and since is not None
        and since >= today.replace(day=1)
        and today.day <= MONTH_START_HOLD_DAYS
    )


def sync_issues(hass: HomeAssistant, coordinator: GasCoordinator) -> None:
    """Raise or clear every card of the coordinator's entry."""
    entry = coordinator.entry
    base = {"entry": entry.title, "supplier": coordinator.extractor.label}
    # The cards are markdown, and a fetch error quotes what the supplier's
    # server answered, which may have put a link or an image there.
    fetch_error = escape_markdown(coordinator.last_error or "-")
    stale = coordinator.snapshot_stale()
    _sync(
        hass,
        entry.entry_id,
        "snapshot_stale",
        stale,
        {**base, "error": fetch_error},
        fixable=True,
    )
    failed = (
        coordinator.failures >= EXTRACTOR_ISSUE_THRESHOLD
        and not coordinator.card_unreadable
        and not _held_at_month_start(coordinator)
    )
    # No card at the address is a card not published yet, a product
    # withdrawn or a card moved, which a layout report fits none of.
    _sync(
        hass,
        entry.entry_id,
        "extractor_failed",
        failed and not coordinator.card_missing,
        {**base, "error": fetch_error},
    )
    _sync(
        hass,
        entry.entry_id,
        "card_missing",
        failed and coordinator.card_missing,
        {**base, "error": fetch_error},
    )
    _sync(hass, entry.entry_id, "card_unreadable", coordinator.card_unreadable, base)
    _sync(
        hass,
        entry.entry_id,
        "contract_withdrawn",
        coordinator.withdrawn,
        {
            **base,
            "contract": coordinator.extractor.contract_label(coordinator.contract)
            or coordinator.contract,
        },
    )
    # The card cannot be read here either way, but while the archive's OCR
    # reading prices the entry, that is what the user needs to know.
    _sync(hass, entry.entry_id, "card_read_by_ocr", coordinator.card_read_by_ocr, base)
    if not coordinator.meter_reads_pending and coordinator.meter_count is not None:
        # Setup's own tick reads no meter and says nothing of it, nor does
        # one still waiting for the energy manager: deleting the card there
        # and creating it again once the meter is read would lose a user's
        # choice to ignore it.
        _sync(
            hass,
            entry.entry_id,
            "meter_unit",
            bool(coordinator.meter_error),
            # Quotes the unit another integration gave the meter, or the
            # recorder's own error.
            {**base, "error": escape_markdown(coordinator.meter_error or "-")},
        )
    # Every cost is priced from the meter's history the recorder keeps, so
    # without it they stay unknown for good, which reads like a frozen
    # sensor unless something says why.
    _sync(hass, entry.entry_id, "recorder_missing", not recorder_loaded(hass), base)
    if coordinator.meter_count is not None:
        # Left alone by a tick that failed before it looked for the meter,
        # like the meter card above.
        _sync(
            hass,
            entry.entry_id,
            "several_meters",
            coordinator.meter_count > 1,
            {**base, "count": str(coordinator.meter_count)},
        )


def clear_issues(hass: HomeAssistant, entry_id: str) -> None:
    for name in ISSUES:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry_id, name))
