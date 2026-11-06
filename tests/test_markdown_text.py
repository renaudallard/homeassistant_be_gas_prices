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


"""escape_markdown: text from a supplier's server as plain characters in markdown.

The Repairs cards, their fix flow and the reason it aborts with are rendered
as markdown, and the frontend lets https links and images through. The cards
quote the text through it (test_ha_setup.py).
"""

from __future__ import annotations

import string

import pytest

from custom_components.be_gas_prices.markdown_text import escape_markdown

# A fetch error quoting what a server answered: the first bytes of a body
# that was not a PDF, and a malformed status line as aiohttp quotes it.
BODY = (
    "expected a PDF at https://card.example/x.pdf, payload starts with "
    "b'![](https://evil.example/p.gif) [Renew your card](https://evil.example/l)'"
)
STATUS = "400, message=\"Invalid status line: '<img src=https://evil.example/a.png>'\""


def _unescape(text: str) -> str:
    """Read ``text`` as CommonMark reads it: a backslash before ASCII
    punctuation shows that character and nothing else, and every other
    punctuation character outside such a pair could begin markdown."""
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            assert text[i + 1] in string.punctuation, text[i:]
            out.append(text[i + 1])
            i += 2
            continue
        assert char not in string.punctuation, (char, text)
        out.append(char)
        i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "text",
    [BODY, STATUS, "www.evil.example *bold* _it_ `code` ~~gone~~ C:\\path\\", "1,09 € été"],
)
def test_every_punctuation_character_is_escaped(text: str) -> None:
    """Nothing in the result can begin a link, an image, an autolink, an
    emphasis, a code span or a tag, and it shows exactly the text given."""
    assert _unescape(escape_markdown(text)) == text


def test_the_text_is_put_on_one_line() -> None:
    escaped = escape_markdown("one\n# two\r\n- three\t\tfour    five\n")
    assert "\n" not in escaped and "\r" not in escaped and "\t" not in escaped
    assert _unescape(escaped) == "one # two - three four five"


def test_each_ascii_punctuation_character_alone() -> None:
    for char in string.punctuation:
        assert escape_markdown(char) == "\\" + char
    assert escape_markdown("abc 123 é €") == "abc 123 é €"
