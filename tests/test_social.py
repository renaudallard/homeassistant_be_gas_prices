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

"""The social tariff, off the CREG's quarterly cards."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time

from custom_components.be_gas_prices import excise_law, tarbel
from custom_components.be_gas_prices.compare import candidates
from custom_components.be_gas_prices.const import (
    CALIBER_Q10,
    DSO_FLUVIUS_IMEWO,
    DSO_ORES,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    REGIONS,
    SOCIAL_CONTRACT,
    SUPPLIER_SOCIAL,
)
from custom_components.be_gas_prices.pricing import (
    PricingError,
    compute_breakdown,
    fixed_costs,
)
from custom_components.be_gas_prices.providers import social
from custom_components.be_gas_prices.providers._resolve import resolve_for_delivery
from custom_components.be_gas_prices.providers.base import ExtractorError
from tests import fixture_text

Q4_2026 = "G-TSS-FR-2026-Q4.pdf"
# The fetch is patched: no session is used.
_SESSION: Any = None


def _card(name: str = Q4_2026) -> social.CregCard:
    return social.parse_creg(fixture_text("social", name))


@pytest.mark.parametrize(
    ("name", "quarter", "total", "distribution", "transport"),
    [
        ("G-TSS-FR-2022-Q4.pdf", date(2022, 10, 1), 3.400, 0.766, 0.141),
        ("G-TSS-FR-2025-Q1.pdf", date(2025, 1, 1), 5.894, 0.745, 0.162),
        (Q4_2026, date(2026, 10, 1), 6.082, 0.882, 0.165),
    ],
)
def test_each_quarter_s_card_is_read(
    name: str, quarter: date, total: float, distribution: float, transport: float
) -> None:
    """The VAT-inclusive total and network components, in c/kWh; the Q4
    2022 card spells its footnote apart and says the protected customer was
    exempt from the excise then."""
    assert _card(name) == social.CregCard(
        quarter=quarter, vat=0.06, total=total, distribution=distribution, transport=transport
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("6,082", "6,182"),  # a total the components do not reach
        ("5,035", "5,135"),  # a VAT-inclusive figure off its ex-VAT one
        ("Composante transport", "Composante autre"),  # a row missing
        ("trimestre 2026", "semestre 2026"),  # no quarter named
    ],
)
def test_a_card_not_as_expected_is_refused(old: str, new: str) -> None:
    text = fixture_text("social", Q4_2026)
    assert old in text
    with pytest.raises(ExtractorError):
        social.parse_creg(text.replace(old, new))


def _billed(region: str, dso: str, delivery: date) -> tuple[float, float]:
    """The all-in c/kWh and the yearly fixed costs a protected household of
    12 000 kWh pays in ``delivery``'s month."""
    snapshot = resolve_for_delivery(social.build_snapshot(region, _card(), delivery), delivery)
    price = compute_breakdown(snapshot, dso, 12_000.0, snapshot.energy.price)
    fixed = fixed_costs(snapshot, dso, 12_000.0, CALIBER_Q10)
    return price.all_in * 100.0, fixed.total


@pytest.mark.parametrize(
    ("region", "dso", "fee"),
    [
        (REGION_FLANDERS, DSO_FLUVIUS_IMEWO, 0.0),
        (REGION_WALLONIA, DSO_ORES, 0.0075),
        (REGION_BRUSSELS, DSO_SIBELGA, 0.0),
    ],
)
def test_the_card_bills_what_the_suppliers_social_cards_print(
    region: str, dso: str, fee: float
) -> None:
    """October 2026: the CREG's 6,082 c/kWh, the protected excise of 3,09
    EUR/MWh with VAT (0,32754, as Engie's and Luminus's social cards print
    it) and the Walloon connection fee, which carries none. No fixed cost:
    no Fluvius data management fee, no Brussels levy."""
    price, fixed = _billed(region, dso, date(2026, 10, 1))
    assert price == pytest.approx(6.082 + 0.32754 + fee)
    assert fixed == 0.0


def test_a_month_bills_the_protected_rate_in_force_then() -> None:
    """July 2026 on FPS Finance's 2,77 EUR/MWh, with no energy contribution
    though the household rate still carried one; January 2027 on the law's
    voted 3,42."""
    july, _ = _billed(REGION_FLANDERS, DSO_FLUVIUS_IMEWO, date(2026, 7, 1))
    assert july == pytest.approx(6.082 + 0.277 * 1.06)
    january, _ = _billed(REGION_FLANDERS, DSO_FLUVIUS_IMEWO, date(2027, 1, 1))
    assert january == pytest.approx(6.082 + 0.342 * 1.06)


def test_a_month_with_no_protected_rate_read_cannot_be_priced() -> None:
    excise_law._PROTECTED_HELD.clear()
    tarbel._PROTECTED_HELD.clear()
    delivery = date(2026, 10, 1)
    snapshot = resolve_for_delivery(
        social.build_snapshot(REGION_FLANDERS, _card(), delivery), delivery
    )
    with pytest.raises(PricingError, match="none of the law's is held"):
        compute_breakdown(snapshot, DSO_FLUVIUS_IMEWO, 12_000.0, snapshot.energy.price)


@pytest.mark.parametrize("region", REGIONS)
def test_every_operator_of_the_region_is_priced(region: str) -> None:
    snapshot = social.build_snapshot(region, _card(), date(2026, 10, 1))
    assert snapshot.supplier == SUPPLIER_SOCIAL
    assert snapshot.publication_label == "2026-10"
    assert snapshot.valid_until == date(2026, 10, 31)
    assert len({id(overlay) for overlay in snapshot.dsos.values()}) == 1


async def test_a_month_is_read_off_its_quarter_s_card() -> None:
    text = fixture_text("social", Q4_2026)
    pdf = AsyncMock(return_value=text)
    with freeze_time(datetime(2026, 11, 20, 12)), patch.object(social, "fetch_pdf_text", pdf):
        card = await social.fetch_for_month(
            _SESSION, SOCIAL_CONTRACT, REGION_WALLONIA, date(2026, 11, 1)
        )
        assert card is not None and card.publication_label == "2026-11"
        assert pdf.await_args is not None
        assert pdf.await_args.args[1].endswith("/G-TSS-FR-2026-Q4.pdf")
        # A card naming another quarter is no card for the month.
        assert (
            await social.fetch_for_month(
                _SESSION, SOCIAL_CONTRACT, REGION_WALLONIA, date(2026, 7, 1)
            )
            is None
        )
        # Before the first quarter the CREG still publishes.
        assert (
            await social.fetch_for_month(
                _SESSION, SOCIAL_CONTRACT, REGION_WALLONIA, date(2022, 9, 1)
            )
            is None
        )


async def test_a_network_failure_is_asked_again() -> None:
    down = AsyncMock(side_effect=ExtractorError("network error fetching x: timeout"))
    with (
        freeze_time(datetime(2026, 11, 20, 12)),
        patch.object(social, "fetch_pdf_text", down),
        pytest.raises(ExtractorError),
    ):
        await social.fetch_for_month(_SESSION, SOCIAL_CONTRACT, REGION_WALLONIA, date(2026, 10, 1))


@pytest.mark.parametrize("region", REGIONS)
def test_the_social_tariff_is_never_ranked(region: str) -> None:
    assert all(extractor.id != SUPPLIER_SOCIAL for extractor, _contract in candidates(region))
