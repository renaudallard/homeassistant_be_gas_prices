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

"""The social tariff, for protected customers.

A household granted protected status pays the social tariff whoever supplies
it: one price per kWh for the whole country, set each quarter by the CREG
under the ministerial decree of 30 March 2007. The CREG publishes it as one
PDF per quarter (G-TSS-FR-<year>-Q<n>.pdf), its energy, distribution and
transport components before and after VAT. The network part replaces the
operator's tariffs altogether, so there is no fixed fee, no fixed term, no
data management fee and no Brussels levy: Engie's and Luminus's social cards
print none. On top of it the household pays only the special excise at the
protected rate, which the law sets (``_resolve.law_protected_excise``), and
in Wallonia the connection fee; it is exempt from the energy contribution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    REGION_DSO_KEYS,
    SOCIAL_CONTRACT,
    SUPPLIER_SOCIAL,
    TIER_T1,
    TIER_T2,
    TIER_T3,
)
from ._parse import to_float
from ._pdf import fetch_pdf_text, is_transient_fetch_error
from ._rates import Contract, VariableRates
from ._validity import end_of_month
from .base import (
    DsoOverlay,
    DsoTier,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_CREG_URL = (
    "https://www.creg.be/sites/default/files/assets/Tarifs/Social/G-TSS-FR-{year}-Q{quarter}.pdf"
)
# The first quarter the CREG still publishes, which is as far back as a month
# can be billed on its own card.
FIRST_QUARTER = date(2022, 10, 1)

_NUM = r"(\d+,\d+)"
# Each row of the table, ex-VAT then VAT-inclusive, in c/kWh. A footnote
# mark of one or two asterisks may follow the unit or the label.
_ROW_RE = re.compile(
    rf"(Composante (?:énergie|distribution|transport)|Total)\s*\(€cent/kWh\)\**\s+{_NUM}\s+{_NUM}"
)
_VAT_RE = re.compile(r"TVA\s+(\d+)\s*%\s+comprise")
_QUARTER_RE = re.compile(r"(\d)(?:er|e|ème)\s+trimestre\s+(\d{4})")
_ROWS = ("Composante énergie", "Composante distribution", "Composante transport", "Total")
# The rounding the card states its figures at, in c/kWh, twice: a component
# sum and a VAT gross-up may each be a unit off in the last place.
_TOLERANCE = 0.0021


@dataclass(frozen=True)
class CregCard:
    """One quarter's social price, VAT-inclusive, in c/kWh."""

    quarter: date
    vat: float
    total: float
    distribution: float
    transport: float


def parse_creg(text: str) -> CregCard:
    """Read one quarter's CREG card. Raises on anything else.

    The rows have to be the energy, distribution and transport components
    and their total, adding up to it on both sides of VAT, and each
    VAT-inclusive figure has to be its ex-VAT one at the card's rate.
    """
    vat_match = _VAT_RE.search(text)
    quarter = _QUARTER_RE.search(text)
    if vat_match is None or quarter is None:
        raise ExtractorError("CREG social tariff: not a quarter's card")
    number, year = int(quarter.group(1)), int(quarter.group(2))
    if not 1 <= number <= 4:
        raise ExtractorError(f"CREG social tariff: quarter {number}")
    vat = int(vat_match.group(1)) / 100.0
    found = _ROW_RE.findall(text)
    if tuple(label for label, _ex, _inc in found) != _ROWS:
        raise ExtractorError("CREG social tariff: not the four rows of one table")
    rows = [(to_float(ex), to_float(inc)) for _label, ex, inc in found]
    *parts, total = rows
    for side in (0, 1):
        if abs(sum(part[side] for part in parts) - total[side]) > _TOLERANCE * len(parts):
            raise ExtractorError("CREG social tariff: components do not add up")
    if any(abs(ex * (1.0 + vat) - inc) > _TOLERANCE for ex, inc in rows):
        raise ExtractorError("CREG social tariff: VAT does not add up")
    return CregCard(
        quarter=date(year, 3 * number - 2, 1),
        vat=vat,
        total=total[1],
        distribution=parts[1][1],
        transport=parts[2][1],
    )


def _quarter_of(month: date) -> date:
    return date(month.year, 3 * ((month.month - 1) // 3) + 1, 1)


def _creg_url(quarter: date) -> str:
    return _CREG_URL.format(year=quarter.year, quarter=(quarter.month + 2) // 3)


def build_snapshot(region: str, card: CregCard, month: date) -> SupplierSnapshot:
    """The snapshot a protected household in ``region`` is billed on for
    ``month``, from its quarter's CREG card.

    The card prints the total the household is billed, so the energy price
    is that total less the network components, rather than the energy row,
    which is rounded on its own. No excise is printed: the resolver puts the
    protected rate of the delivery month on it, and a month the law has not
    been read for cannot be priced (``pricing.compute_breakdown``).
    """
    if region not in REGION_DSO_KEYS:
        raise ExtractorError(f"social tariff: unknown region {region!r}")

    def _eur(c_per_kwh: float) -> float:
        return c_per_kwh / 100.0

    distribution = DsoTier(fixed_per_year=0.0, proportional=_eur(card.distribution))
    overlay = DsoOverlay(
        tiers=dict.fromkeys((TIER_T1, TIER_T2, TIER_T3), distribution),
        transport=_eur(card.transport),
    )
    return SupplierSnapshot(
        supplier=SUPPLIER_SOCIAL,
        contract=SOCIAL_CONTRACT,
        energy=VariableRates(price=_eur(card.total - card.distribution - card.transport)),
        dsos=dict.fromkeys(REGION_DSO_KEYS[region], overlay),
        taxes=TaxOverlay(excise_bands=(), card_vat_rate=card.vat),
        source_url=_creg_url(card.quarter),
        # The quarter's card, billed per month: the excise changes at a month
        # boundary, and the month cache files it by month.
        publication_label=f"{month:%Y-%m}",
        valid_until=end_of_month(month.year, month.month),
    )


async def _for_month(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> SupplierSnapshot:
    if contract_id != SOCIAL_CONTRACT:
        raise ExtractorError(f"unknown social tariff contract {contract_id!r}")
    quarter = _quarter_of(month)
    card = parse_creg(await fetch_pdf_text(session, _creg_url(quarter)))
    if card.quarter != quarter:
        raise ExtractorError(f"CREG social tariff: the card for {quarter} prices {card.quarter}")
    return build_snapshot(region, card, month)


async def fetch(session: aiohttp.ClientSession, contract_id: str, region: str) -> SupplierSnapshot:
    """This month's social tariff."""
    return await _for_month(session, contract_id, region, dt_util.now().date().replace(day=1))


async def fetch_for_month(
    session: aiohttp.ClientSession, contract_id: str, region: str, year_month: date
) -> SupplierSnapshot | None:
    """A past month's social tariff, from its own quarter's CREG card, or
    ``None`` before the first quarter the CREG still publishes."""
    first = year_month.replace(day=1)
    if first < FIRST_QUARTER or first > dt_util.now().date():
        return None
    try:
        return await _for_month(session, contract_id, region, first)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raised,
        # so the month cache asks again instead of filing it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None


EXTRACTOR = SupplierExtractor(
    id=SUPPLIER_SOCIAL,
    label="Social tariff (CREG, protected customers only)",
    contracts=(Contract(id=SOCIAL_CONTRACT, label="Social tariff", kind="variable"),),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
)
