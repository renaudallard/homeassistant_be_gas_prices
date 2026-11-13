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

"""The network a custom entry takes off its DSO's published tariff sheet.

A custom entry types its DSO's figures, or ticks the box that takes them off
the regulator's sheet of each delivery year instead: Fluvius's sheet for its
area in Flanders (fluvius.py), Brugel's in Brussels (brugel.py) or its
operator's grid as the CWaPE publishes it in Wallonia (cwape.py). For a month
whose own year's sheet is not held, what the card archive kept of that
month's sheet stands in (network_archive.py), then the latest earlier sheet
held, and the typed figures while none of them is.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

import aiohttp

from . import brugel, cwape, fluvius, network_archive
from .const import (
    BRUSSELS_DSO_KEYS,
    CONF_CUSTOM_NETWORK_FROM_SHEET,
    CONF_DSO,
    CONF_SUPPLIER,
    FLUVIUS_KEYS,
    SUPPLIER_CUSTOM,
    WALLONIA_DSO_KEYS,
)
from .providers.base import SheetNetwork


def takes_network_sheet(data: Mapping[str, Any]) -> bool:
    """Whether this entry, or an earlier contract's settings, is a custom
    one taking its network off its DSO's sheet."""
    return data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM and bool(
        data.get(CONF_CUSTOM_NETWORK_FROM_SHEET)
    )


async def ensure_network_sheet(
    session: aiohttp.ClientSession, data: Mapping[str, Any], year: int
) -> None:
    """Read the sheet such an entry takes its network from for ``year``,
    before its card is resolved, which reads it synchronously. Never raises,
    as each reader promises."""
    if not takes_network_sheet(data):
        return
    dso = str(data.get(CONF_DSO, ""))
    if dso in FLUVIUS_KEYS:
        await fluvius.ensure_area_network(session, year, dso)
    elif dso in BRUSSELS_DSO_KEYS:
        await brugel.ensure_network(session, year)
    elif dso in WALLONIA_DSO_KEYS:
        await cwape.ensure_network(session, year, dso)


def _sheet(delivery: date, dso: str, *, stand_in: bool) -> SheetNetwork | None:
    """``dso``'s network off the sheet held for the year of ``delivery``,
    or with ``stand_in`` off the latest earlier one held."""
    if dso in FLUVIUS_KEYS:
        return fluvius.cached_area_network(delivery.year, dso, stand_in=stand_in)
    if dso in BRUSSELS_DSO_KEYS:
        return brugel.cached_network(delivery.year, stand_in=stand_in)
    if dso in WALLONIA_DSO_KEYS:
        return cwape.cached_network(delivery, dso, stand_in=stand_in)
    return None


def held_network(delivery: date, dso: str) -> SheetNetwork | None:
    """``dso``'s network for the month of ``delivery``: off its own year's
    sheet where that is held, else what the card archive kept of that
    month's sheet (network_archive.py), else off the latest earlier sheet
    held, which stands in until the year's is read, or ``None`` while none
    of them is. An earlier year's sheet comes last: the year's own read may
    have failed here while the archive's run read it."""
    return next(
        (
            held
            for held in (
                _sheet(delivery, dso, stand_in=False),
                network_archive.network_for(delivery, dso),
                _sheet(delivery, dso, stand_in=True),
            )
            if held is not None
        ),
        None,
    )
