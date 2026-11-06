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

"""The HTML table reader."""

from __future__ import annotations

import time

import pytest

from custom_components.be_gas_prices.providers._parse import html_rows


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
