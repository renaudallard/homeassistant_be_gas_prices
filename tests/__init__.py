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

"""Shared test helpers."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest

from custom_components.be_gas_prices.providers._pdf import (
    extract_pdf_text,
    extract_pdf_text_layout,
)

FIXTURES = Path(__file__).parent / "fixtures"

_READERS = {
    "plain": extract_pdf_text,
    "layout": extract_pdf_text_layout,
}


@cache
def fixture_text(supplier: str, name: str, reader: str = "plain") -> str:
    """The text of a fixture card, read the way the extractor reads it.

    Cached because pdfplumber takes seconds per card on a Raspberry Pi and
    several tests read the same one.
    """
    return _READERS[reader]((FIXTURES / supplier / name).read_bytes())


def fixture_page(supplier: str, name: str) -> str:
    """A fixture HTML or JSON page as text."""
    return (FIXTURES / supplier / name).read_text(encoding="utf-8")


def approx(expected: float, **tolerance: Any) -> float:
    """``pytest.approx`` typed as the float it stands in for, so a dataclass
    built to compare against type-checks."""
    return cast(float, pytest.approx(expected, **tolerance))
