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

"""The two helpers a formula reader checks its figures with: the unit its
card states for them, and the price its formula gives back. And the HTML
table reader."""

from __future__ import annotations

import re
import time

import pytest

from custom_components.be_gas_prices.providers._parse import (
    finite_number,
    formula_gives,
    html_rows,
    stated_unit,
    to_float,
)
from custom_components.be_gas_prices.providers.base import ExtractorError


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,5", 1.5),
        ("1.5", 1.5),
        ("1.001,55", 1001.55),
        ("1 250,5", 1250.5),
        ("1\u202f250,5", 1250.5),
        (" 0,1654 ", 0.1654),
        ("-0,5", -0.5),
        ("+5", 5.0),
        ("12,", 12.0),
        (",5", 0.5),
        ("0", 0.0),
    ],
)
def test_a_figure_as_cards_print_it(text: str, value: float) -> None:
    assert to_float(text) == value


@pytest.mark.parametrize(
    "text",
    [
        "inf",
        "-inf",
        "Infinity",
        "nan",
        "1e3",
        "1E-3",
        "1_000",
        "0x10",
        "١٢,٥",
        "１２",
        pytest.param("9" * 400, id="400 digits"),
        "1.234.567",
        "1,2,3",
        "5-",
        "−0,5",
        ".",
        "",
        " ",
    ],
)
def test_what_is_no_plain_decimal_figure_is_refused(text: str) -> None:
    """float() reads an infinity, a NaN, an exponent, an underscore and the
    digits of other scripts, and makes an infinity of a figure too long for
    it: none is a figure a card prints."""
    with pytest.raises(ExtractorError, match="not a figure"):
        to_float(text)


@pytest.mark.parametrize(
    "text", ["9" * 200_000 + "x", "9" * 200_000 + "." + "9" * 200_000 + "x", "9" * 200_000 + ".."]
)
def test_a_long_run_of_digits_is_refused_in_time_linear_in_it(text: str) -> None:
    """A pattern that gave the run back a digit at a time took 1,2 s for
    8 000 digits before anything else, and four times that for twice as many."""
    started = time.perf_counter()
    with pytest.raises(ExtractorError, match="not a figure"):
        to_float(text)
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize(
    ("value", "number"),
    [
        (12, 12.0),
        (0.5, 0.5),
        (-3.25, -3.25),
        (True, None),
        (None, None),
        ("12.5", None),
        ([12.5], None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        pytest.param(10**400, None, id="an integer past any float"),
    ],
)
def test_a_json_number_is_a_finite_one_or_none(value: object, number: float | None) -> None:
    assert finite_number(value) == number


def test_the_unit_a_card_states_is_the_nearest_statement_of_it() -> None:
    """The last statement before the figure wins, else the first after it,
    each within its own reach; a card stating none there answers None."""
    unit = re.compile(r"\((c€/kWh|€/MWh)\)")
    text = "TTF (€/MWh) 86,10 Formule (c€/kWh) 0,1001 x TTF + 0,52 (€/MWh)"
    figure = text.index("0,1001"), text.index("0,1001") + 6
    assert stated_unit(text, unit, figure, before=30) == "c€/kWh"
    assert stated_unit(text, unit, figure, before=5, after=30) == "€/MWh"
    assert stated_unit(text, unit, figure, before=5, after=5) is None
    assert stated_unit(text, unit, figure) is None


def test_of_several_statements_the_nearest_is_taken() -> None:
    """Of several statements within a reach, the last one ahead of the figure
    is taken, or the first one behind it."""
    unit = re.compile(r"\((c€/kWh|€/MWh)\)")
    text = "(€/MWh) (c€/kWh) 0,1001 (€/MWh) (c€/kWh)"
    figure = text.index("0,1001"), text.index("0,1001") + 6
    assert stated_unit(text, unit, figure, before=30) == "c€/kWh"
    assert stated_unit(text, unit, figure, after=30) == "€/MWh"


def test_a_statement_across_the_edge_of_its_reach_is_not_read() -> None:
    """A statement counts only where it lies wholly within its reach: one
    running across the edge of it is none there, never a unit cut short."""
    unit = re.compile(r"exprimé en\s+([^\s.]+)")
    text = "0,1001 x TTF + 0,52 Le résultat est un montant exprimé en c€/kWh."
    figure = 0, text.index(" Le")
    reach = text.index("c€/kWh") + len("c€/kWh") - figure[1]
    assert stated_unit(text, unit, figure, after=reach) == "c€/kWh"
    for cut in range(1, 6):
        assert stated_unit(text, unit, figure, after=reach - cut) is None
    inside = text.index("kWh")
    assert stated_unit(text, unit, (inside, inside + 3), before=60) is None


def test_a_unit_statement_is_looked_for_within_its_reach_only() -> None:
    """A call costs its reach, not the rest of the card: a pattern that may
    start on any blank, after a figure followed by a long run of them, would
    otherwise be tried again from every blank of the run."""
    unit = re.compile(r"[^\S\n]*(\S+)")
    text = "0,1" + " " * 100_000 + "\n0,1"
    started = time.perf_counter()
    assert stated_unit(text, unit, (0, 3), after=12) is None
    assert stated_unit(text, unit, (len(text) - 3, len(text)), before=12) is None
    assert time.perf_counter() - started < 2.0


def test_a_formula_gives_back_the_price_its_card_prints() -> None:
    # Luminus's November 2026 gas card: 0,1001 x TTF DAH M + 0,5279 c€/kWh
    # before VAT, with TTF DAH M at 86,104 EUR/MWh.
    price = 0.1001 * 86.104 + 0.5279
    assert formula_gives(price, 0.1001, 86.104, 0.5279, tolerance=0.001)
    assert not formula_gives(price, 1.001, 86.104, 0.5279, tolerance=0.001)
    assert not formula_gives(price, 0.1001, 86.104, 5.279, tolerance=0.001)


def test_a_table_reads_as_its_patterns_did() -> None:
    """Rows, cells and tags as "<tr[^>]*>(.*?)</tr>", "<t[hd][^>]*>(.*?)</t[hd]>"
    and "<[^>]+>" found them, corners included: an empty "<>" is no tag, a
    tag runs to the first ">" whatever it holds, a heading cell may close as
    a data cell, the ">" of an opening tag may be its row's closing's own,
    and an element never closed is none."""
    page = (
        '<table><tr class="head"><th>Maand</th><th colspan="2">Index <b>ZTP</b></th></tr>\n'
        "<tr><td>Oktober 2026</td><td>1 &amp; 2</td><td>a<>b</td></tr>"
        "<tr><td>a<<b>c</td><th>x</td></tr>"
        "<tr</tr><td>y</td></tr>"
        "<tr><td>never closed"
    )
    assert html_rows(page) == [
        ["Maand", "Index ZTP"],
        ["Oktober 2026", "1 & 2", "a<>b"],
        ["a c", "x"],
        ["y"],
    ]


@pytest.mark.parametrize(
    ("page", "rows"),
    [
        # Each held the event loop for seconds to a minute: openings that no
        # closing follows, and a cell of "<" that no ">" follows.
        ("<tr>" * 32000, []),
        ("<tr" * 32000, []),
        ("<tr>" + "<td>" * 32000 + "</tr>", [[]]),
        ("<tr><td>" + "<" * 128000 + "</td></tr>", [["<" * 128000]]),
    ],
    ids=["openings", "openings unended", "cells", "a cell of <"],
)
def test_a_crafted_table_is_read_in_time_linear_in_it(page: str, rows: list[list[str]]) -> None:
    started = time.monotonic()
    assert html_rows(page) == rows
    assert time.monotonic() - started < 1.0
