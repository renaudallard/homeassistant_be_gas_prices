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

"""Per-supplier extractor protocol and shared dataclasses.

Each supplier exposes a module under ``providers/`` that:

  - declares the contracts it sells (id, label, kind, regions),
  - fetches the current tariff card from the supplier's own publication,
  - parses the energy price plus the distribution, transport and tax
    overlay for every DSO the card lists,
  - optionally fetches the card of a past month and the index values its
    variable prices are resolved against.

No SUPPLIER EUR values live in Python source: every price, fee and
coefficient in :class:`SupplierSnapshot` comes from that supplier's own card.
The only typed-in figures are regulated ones set by law for the whole country,
which live in ``const.py`` with the months they are known to cover.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

import aiohttp

from ._rates import Contract, EnergyRates


@dataclass(frozen=True, kw_only=True)
class DsoTier:
    """One consumption tier of a DSO's distribution tariff."""

    fixed_per_year: float
    proportional: float


@dataclass(frozen=True, kw_only=True)
class DsoOverlay:
    """The regulated network tariff of one DSO, as the card prints it.

    ``tiers`` is keyed by the ``const.TIER_*`` ids. T1 and T2 are on every
    residential card; T3 only on the cards that price large consumers too.
    Proportional terms and transport are EUR/kWh, the fixed terms and the
    metering fee EUR/year. ``metering_per_year`` is the Flemish databeheer or
    the Brussels yearly-reading fee; Wallonia bills none.
    """

    tiers: dict[str, DsoTier]
    transport: float
    metering_per_year: float = 0.0


@dataclass(frozen=True, kw_only=True)
class SheetNetwork:
    """A DSO's network for a household on T1 or T2 as its regulator's tariff
    sheet gives it, excluding VAT: each tier's fixed term a year and its
    proportional rate per kWh with every charge per kWh the sheet prints
    below it added, the way the cards print it, and the metering term a
    year. No sheet prints the transport."""

    t1_fixed: float
    t1_proportional: float
    t2_fixed: float
    t2_proportional: float
    metering_per_year: float


@dataclass(frozen=True, kw_only=True)
class TaxOverlay:
    """Federal and regional levies, on the card's VAT basis.

    ``excise_bands`` is the special excise as ((upper kWh, EUR/kWh), ...)
    ascending, the last upper bound None. It is billed per slice on the
    annual volume, so :func:`_resolve.effective_excise` blends it. A card
    printing a single rate holds one open band, one printing no gas excise
    none: its months are priced on the law's alone.

    ``connection_fee`` is the Walloon "redevance de raccordement" in EUR/kWh,
    which carries no VAT, and 0.0 elsewhere. ``osp_by_caliber`` is the
    Brussels per-meter levy in EUR/year, keyed by ``const.OSP_Q10_*`` and the
    ``const.CALIBER_*`` ids above the smallest caliber; None outside Brussels.

    ``vat_rate`` 0.0 means the values are already VAT inclusive, which every
    residential card is. ``card_vat_rate`` is the rate the card states for
    its customers, or None where it states none.
    """

    excise_bands: tuple[tuple[float | None, float], ...]
    energy_contribution: float = 0.0
    connection_fee: float = 0.0
    osp_by_caliber: dict[str, float] | None = None
    vat_rate: float = 0.0
    card_vat_rate: float | None = None


@dataclass(frozen=True, kw_only=True)
class SupplierSnapshot:
    """Everything read from one supplier's tariff card for one contract."""

    supplier: str
    contract: str
    energy: EnergyRates
    dsos: dict[str, DsoOverlay]
    taxes: TaxOverlay
    source_url: str
    publication_label: str = ""
    # Last calendar day the card's prices apply to, typically the last day
    # of its month. None for a card with no end date: the custom supplier's
    # typed card, or one in force until the supplier replaces it.
    valid_until: date | None = None
    # The months from its start a contract signed on this card pays no
    # supplier fee for; the energy leg's fee is the one billed after them.
    fee_waived_months: int = 0
    # A custom entry's network taken off its DSO's tariff sheet of each
    # delivery year rather than the figures typed (network_sheet.py).
    network_from_sheet: bool = False


# The values of the indices a supplier resolves its variable prices against,
# by index name and then by month ("YYYY-MM"), in EUR/MWh.
IndexTable = dict[str, dict[str, float]]

_STAND_IN = " stand-in"


def stand_in_name(index: str) -> str:
    """The series a table carries beside ``index`` for the months the
    supplier's own publication has not settled: the same index as another
    supplier publishes it, for the months after the supplier's own newest
    value (energie.be's TTF_RLP from Trevion's), or the supplier's own
    value while it can still move (Bolt's month just closed, on the first
    days of the next). A month priced on it stays provisional."""
    return f"{index}{_STAND_IN}"


def is_stand_in(name: str) -> bool:
    """Whether a table's series ``name`` is a stand-in (:func:`stand_in_name`)."""
    return name.endswith(_STAND_IN)


SnapshotFetcher = Callable[[aiohttp.ClientSession, str, str], Awaitable[SupplierSnapshot]]

# Cheap-probe contract: the same return value across calls means the card is
# unchanged; a different one means fetch again. None means no signal, and the
# time-based TTL takes over.
SnapshotProbe = Callable[[aiohttp.ClientSession, str, str], Awaitable[str | None]]

# The card the supplier published for one past month, or None where it keeps
# no archive reaching it; the month cache then asks the card archive.
ArchivedSnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str, date], Awaitable[SupplierSnapshot | None]
]

IndexFetcher = Callable[[aiohttp.ClientSession], Awaitable[IndexTable]]

# The products the supplier lists as sold today: a registered contract by its
# id, anything else by an id naming it. Raises ExtractorError when the list
# cannot be read.
CatalogFetcher = Callable[[aiohttp.ClientSession], Awaitable[set[str]]]


@dataclass(frozen=True, kw_only=True)
class SupplierExtractor:
    """Registry entry for one supplier."""

    id: str
    label: str
    contracts: tuple[Contract, ...]
    fetch: SnapshotFetcher
    probe: SnapshotProbe | None = None
    fetch_for_month: ArchivedSnapshotFetcher | None = None
    # The card in force on one day, for a supplier that reissues a card within
    # its month: the signing card of a household that names the day it
    # signed. None where a month has one card.
    fetch_on_day: ArchivedSnapshotFetcher | None = None
    # The supplier's own publication of the index values its indexed prices
    # settle on. None for a supplier selling fixed prices only.
    fetch_index: IndexFetcher | None = None
    # The supplier's own list of the products it sells, which the live check
    # holds against ``contracts``. None where it publishes none.
    discover: CatalogFetcher | None = None
    # Set when the supplier has announced it is leaving the residential
    # market: the config flow stops offering it and the comparisons leave it
    # out. Nothing compares these to the clock, and nothing reads the
    # successor yet.
    deprecated_until: date | None = None
    deprecated_successor: str | None = None
    # Roughly what one card costs to fetch and parse, in seconds, on the
    # slowest hardware this runs on, so the comparison sweep can spend its
    # time budget on many cheap rows before a few expensive ones.
    sweep_cost_s: float = 5.0
    # The supplier publishes its cards as page images, which only the card
    # archive's reading prices: an entry on it needs the archive.
    images_only: bool = False

    def regions(self) -> frozenset[str]:
        """Union of regions across this supplier's contracts."""
        out: set[str] = set()
        for contract in self.contracts:
            out |= contract.regions
        return frozenset(out)

    def contract_label(self, contract: str) -> str | None:
        """The label of ``contract``, a withdrawn one's included."""
        return next((c.label for c in self.contracts if c.id == contract), None)


class ExtractorError(Exception):
    """Raised when a supplier's source cannot be fetched or parsed."""


class CardNotListedError(ExtractorError):
    """The supplier's own listing names no card for the product.

    No card's layout changed: the listing has none under the name or month
    the extractor looks for, which is a card not published yet or one that
    moved. ``providers/_moved.py`` looks for a moved card on this as on a
    404.
    """


class CardNotReadableError(ExtractorError):
    """The card downloaded fine but carries no text layer to read.

    Derived per fetch rather than declared per supplier, so it clears by
    itself the moment a supplier publishes text again.
    """
