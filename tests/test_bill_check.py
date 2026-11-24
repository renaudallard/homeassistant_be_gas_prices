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

"""The bill check: each line of an Eneco bill held against the rate priced
here for its own period.

The bill is Eneco's yearly gas bill of December 2025 to a Walloon household
on ORES, on Flex signed on the October 2022 card: its labels, periods and
unit prices as printed, its quantities made up. Its months are priced on
the card archive's rows of Eneco's Flex cards for Wallonia, the energy on
the October 2022 card's formula at the index Eneco publishes, and the
levies on FPS Finance's tariff and the law as conftest holds them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from custom_components.be_gas_prices.bill import contract_leg, is_provisional, priced_month
from custom_components.be_gas_prices.bill_check import (
    NOT_COMPARED,
    OFF,
    OK,
    BillCheck,
    RatesFor,
    check_bill,
    month_rates,
)
from custom_components.be_gas_prices.const import (
    DSO_ORES,
    REGION_WALLONIA,
)
from custom_components.be_gas_prices.providers import eneco
from custom_components.be_gas_prices.providers.base import (
    IndexTable,
    SupplierSnapshot,
)
from custom_components.be_gas_prices.snapshot_codec import snapshot_from_json
from tests import FIXTURES, fixture_text
from tests.test_eneco import _fetched_index

_ROWS = FIXTURES / "card_archive" / "eneco" / "eneco_aardgas_flex" / "wallonia"
_CDN = "https://cdn.eneco.be/downloads/nl/general/tk"
_SIGNED = "BC_032_012210_NL_ENECO_GAS_FLEX.pdf"
_CURRENT = "BC_032_012611_NL_ENECO_GAS_FLEX.pdf"
_BILL_ENTITY = "sensor.gas_last_bill"
# Made up, above the 12 000 kWh the excise's first slice ends at.
_ANNUAL_KWH = 15_000.0

_SECTIONS = {
    "Redevance annuelle": "energy",
    "Consommation": "energy",
    "Distribution variable": "network",
    "Distribution fixe": "network",
    "Coût de transport": "network",
    "Cotisation sur l'énergie": "taxes",
    "Accise spéciale": "taxes",
    "Redevance de raccordement": "taxes",
}


def _line(
    label: str, start: str, end: str, unit: str | None, price: float | None, vat: float = 0.06
) -> dict[str, Any]:
    """A line as the Eneco integration's last bill sensor shows it."""
    quantity = 0.95 if unit == "year" else 1000.0
    return {
        "section": _SECTIONS.get(label, "energy"),
        "label": label,
        "code": None,
        "start": start,
        "end": end,
        "quantity": None if price is None else quantity,
        "unit": unit,
        "unit_price": price,
        "vat": vat,
        "total": 12.34 if price is None else round(quantity * price, 2),
    }


_FROM, _TO = "2025-01-06", "2025-12-16"
_Q1, _Q2, _Q3, _Q4 = (
    (_FROM, "2025-03-31"),
    ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"),
    ("2025-10-01", _TO),
)
# EUR a year and EUR/kWh before VAT, as printed (c€ on the bill).
BILL = [
    _line("Redevance annuelle", _FROM, _TO, "year", 49.58710),
    _line("Consommation", *_Q1, "kWh", 0.0581542),
    _line("Consommation", *_Q2, "kWh", 0.0459959),
    _line("Consommation", *_Q3, "kWh", 0.0430706),
    _line("Consommation", *_Q4, "kWh", 0.0405168),
    _line("Distribution variable", _FROM, _TO, "kWh", 0.0192676),
    _line("Distribution fixe", _FROM, _TO, "year", 127.75),
    _line("Coût de transport", _FROM, _TO, "kWh", 0.0014061),
    _line("Cotisation sur l'énergie", _FROM, _TO, "kWh", 0.0009978),
    _line("Accise spéciale", *_Q1, "kWh", 0.0085418),
    _line("Accise spéciale", *_Q2, "kWh", 0.0084477),
    _line("Accise spéciale", *_Q3, "kWh", 0.0082548),
    _line("Accise spéciale", *_Q4, "kWh", 0.0085226),
    _line("Redevance de raccordement", _FROM, _TO, "kWh", 0.000075, vat=0.0),
]


def _attributes(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """The last bill sensor's attributes for a bill of ``lines``."""
    return {
        "invoice_type": "YearlyBill",
        "issued": "2025-12-22",
        "period_start": min(line["start"] for line in lines),
        "period_end": max(line["end"] for line in lines),
        "lines": lines,
        "last_error": None,
    }


def _row(month: str) -> SupplierSnapshot:
    return snapshot_from_json(json.loads((_ROWS / f"{month}.json").read_text()))


def _signed() -> SupplierSnapshot:
    return eneco.parse_snapshot(
        "eneco_aardgas_flex", REGION_WALLONIA, fixture_text("eneco", _SIGNED), f"{_CDN}/{_SIGNED}"
    )


def _rates_for(
    table: IndexTable, months: tuple[str, ...] | None = None, annual_kwh: float = _ANNUAL_KWH
) -> RatesFor:
    """Each month priced as the coordinator prices it for the check, on the
    archive's row of the month, of ``months`` alone where given."""
    signed = _signed().energy
    held = sorted(path.stem for path in _ROWS.glob("*.json"))

    def rates_for(month: str) -> Any:
        if month not in (held if months is None else months):
            return f"no card held for {month}"
        card = _row(month)
        priced, price, index = priced_month(
            card, lambda c, vat: contract_leg(c.energy, signed, {}, vat), table, month
        )
        return month_rates(
            priced,
            price,
            index,
            is_provisional(card, priced.energy, index, month),
            month=month,
            dso=DSO_ORES,
            annual_kwh=annual_kwh,
        )

    return rates_for


async def _check(lines: list[dict[str, Any]], **kwargs: Any) -> BillCheck:
    check = check_bill(_attributes(lines), _rates_for(await _fetched_index(), **kwargs))
    assert check is not None
    return check


async def test_the_bill_of_december_2025() -> None:
    """Every line but the transport and the excise is billed as priced here.
    The cards print the transport Fluxys estimated for the year, 0,165 c€
    VAT included; the bill charges 0,14061 before VAT. The excise is the
    blend of its slices at the household's annual volume, which here is not
    the one the bill was drawn on."""
    check = await _check(BILL)
    # (0,1 x TTFDAW-RLP + 1,07) c€ at 47,45, 35,29, 32,37 and 29,82.
    energy = [0.05815, 0.04599, 0.04307, 0.04052]
    # 12 000 kWh at 0,823 c€ and 3 000 at each quarter's upper rate.
    excise = [
        (12_000 * 0.00823 + 3_000 * upper) / _ANNUAL_KWH
        for upper in (0.0089393, 0.0087266, 0.0082752, 0.0088971)
    ]
    assert [(line.status, line.priced) for line in check.lines] == [
        (OK, pytest.approx(52.56 / 1.06, abs=1e-7)),
        *((OK, pytest.approx(value, abs=1e-7)) for value in energy),
        (OK, pytest.approx(0.0204 / 1.06, abs=1e-7)),
        (OK, pytest.approx(135.42 / 1.06, abs=1e-7)),
        (OFF, pytest.approx(0.00165 / 1.06, abs=1e-7)),
        (OK, pytest.approx(0.0009978, abs=1e-7)),
        *((OFF, pytest.approx(value, abs=1e-7)) for value in excise),
        (OK, pytest.approx(0.000075, abs=1e-7)),
    ]
    assert check.off == 5
    assert (check.issued, check.period_start, check.period_end) == ("2025-12-22", _FROM, _TO)
    first = check.lines[0].as_dict()
    assert first == {
        "label": "Redevance annuelle",
        "section": "energy",
        "start": _FROM,
        "end": _TO,
        "unit": "year",
        "billed": 49.5871,
        "priced": pytest.approx(49.5849057),
        "status": OK,
        "reason": None,
    }


@pytest.mark.parametrize(
    ("label", "start", "end", "unit", "billed", "status"),
    [
        # The card prints 135,42 VAT included: 127,75 rounds to it, 127,74
        # does not.
        ("Distribution fixe", _FROM, _TO, "year", 127.75, OK),
        ("Distribution fixe", _FROM, _TO, "year", 127.744, OFF),
        # And 2,04 c€: 1,92676 rounds to it, 1,93 does not.
        ("Distribution variable", _FROM, _TO, "kWh", 0.0192676, OK),
        ("Distribution variable", _FROM, _TO, "kWh", 0.0193, OFF),
        # The index prints two decimals: a hundredth of it at the formula's
        # 0,1 is 0,001 c€/kWh, which the bill's five decimals stay within.
        ("Consommation", *_Q1, "kWh", 0.0581542, OK),
        ("Consommation", *_Q1, "kWh", 0.0581620, OFF),
        ("Cotisation sur l'énergie", _FROM, _TO, "kWh", 0.0010080, OFF),
    ],
)
async def test_a_line_is_off_past_what_the_printing_explains(
    label: str, start: str, end: str, unit: str, billed: float, status: str
) -> None:
    check = await _check([_line(label, start, end, unit, billed)])
    assert check.lines[0].status == status
    assert check.off == (status == OFF)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        # The network moves on 1 January: 2,21 c€ VAT included in 2026.
        (
            _line("Distribution variable", "2025-12-01", "2026-01-31", "kWh", 0.0192676),
            "the rate priced here changes in 2026-01",
        ),
        # The excise moves every quarter.
        (
            _line("Accise spéciale", _FROM, _TO, "kWh", 0.0085),
            "the rate priced here changes in 2025-04",
        ),
        # Not a label of Eneco's French gas bills: never guessed at.
        (
            _line("Verbruik", *_Q1, "kWh", 0.0581542),
            "no rate priced here under that label",
        ),
        (_line("Frais de rappel", _FROM, _TO, None, None), "no rate priced here under that label"),
        (
            _line("Redevance annuelle", _FROM, _TO, "kWh", 49.5871),
            "billed per kWh, not per year",
        ),
        (_line("Redevance annuelle", _FROM, _TO, "year", None), "a flat amount"),
        (_line("Consommation", "2025-03-31", _FROM, "kWh", 0.0581542), "no period"),
        (
            _line("Consommation", "2024-12-01", "2025-01-31", "kWh", 0.0581542),
            "no card held for 2024-12",
        ),
    ],
)
async def test_a_line_not_compared_says_why(line: dict[str, Any], reason: str) -> None:
    check = await _check([line])
    assert (check.lines[0].status, check.lines[0].priced) == (NOT_COMPARED, None)
    assert check.lines[0].reason == reason
    assert check.off == 0


async def test_a_month_whose_card_is_not_held_is_not_priced_on_another() -> None:
    """With the first quarter's cards alone, its lines are compared and
    every other is not, on the first month of it with no card."""
    check = await _check(BILL, months=("2025-01", "2025-02", "2025-03"))
    compared = {1: OK, 9: OFF}
    for number, line in enumerate(check.lines):
        if number in compared:
            assert line.status == compared[number]
            continue
        first = max(line.start[:7], "2025-04")
        assert (line.status, line.reason) == (NOT_COMPARED, f"no card held for {first}")


async def test_an_energy_price_not_yet_final_is_not_compared() -> None:
    """Without Q4 2025's index the quarter is priced at Q3's, an estimate."""
    table = await _fetched_index()
    table = {
        **table,
        "TTFDAW-RLP": {k: v for k, v in table["TTFDAW-RLP"].items() if k < "2025-10"},
    }
    check = check_bill(_attributes(BILL), _rates_for(table))
    assert check is not None
    assert [line.status for line in check.lines[1:5]] == [OK, OK, OK, NOT_COMPARED]
    assert check.lines[4].reason == "the energy price of 2025-10 is not final"


def test_a_sensor_showing_no_bill_checks_nothing() -> None:
    def no_rates(month: str) -> str:
        raise AssertionError(month)

    assert check_bill({"lines": []}, no_rates) is None
    assert check_bill({}, no_rates) is None
    assert check_bill({"lines": "x"}, no_rates) is None


async def test_unreadable_lines_are_not_compared() -> None:
    lines = [{"label": 3}, "x", _line("Consommation", *_Q1, "kWh", float("nan"))]
    check = check_bill({**_attributes(BILL), "lines": lines}, _rates_for(await _fetched_index()))
    assert check is not None
    assert [line.status for line in check.lines] == [NOT_COMPARED] * 3
