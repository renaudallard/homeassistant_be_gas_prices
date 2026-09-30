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

"""Postcode resolution against the generated postcode table."""

from __future__ import annotations

import pytest

from custom_components.be_gas_prices.const import (
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_KEMPEN,
    DSO_FLUVIUS_MIDDEN_VLAANDEREN,
    DSO_ORES,
    DSO_RESA,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_DSO_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from custom_components.be_gas_prices.postcode_map import POSTCODES, UNPRICED
from custom_components.be_gas_prices.postcodes import PostcodeMatch, resolve


@pytest.mark.parametrize(
    ("postcode", "match"),
    [
        ("1000", PostcodeMatch(REGION_BRUSSELS, (DSO_SIBELGA,))),
        ("9000", PostcodeMatch(REGION_FLANDERS, (DSO_FLUVIUS_IMEWO,))),
        ("5000", PostcodeMatch(REGION_WALLONIA, (DSO_ORES,))),
        ("4000", PostcodeMatch(REGION_WALLONIA, (DSO_RESA,))),
        # Zwijndrecht left Fluvius Antwerpen on 1 January 2026.
        ("2070", PostcodeMatch(REGION_FLANDERS, (DSO_FLUVIUS_MIDDEN_VLAANDEREN,))),
        # Baarle-Hertog: Zondereigen is Fluvius Kempen, the rest Enexis, a
        # Dutch DSO no card prices.
        ("2387", PostcodeMatch(REGION_FLANDERS, (DSO_FLUVIUS_KEMPEN,), ("Enexis",))),
        # No gas DSO (Voeren, Sankt Vith), so the region comes from the range.
        ("3790", PostcodeMatch(REGION_FLANDERS, ())),
        ("4780", PostcodeMatch(REGION_WALLONIA, ())),
    ],
)
def test_resolve(postcode: str, match: PostcodeMatch) -> None:
    assert resolve(postcode) == match


# An institution's postcode (1005), a number bpost does not give out (9999),
# and strings that are no postcode at all.
@pytest.mark.parametrize("postcode", ["1005", "9999", "0612", "612", "10000", " 1000", "abcd", ""])
def test_resolve_unknown(postcode: str) -> None:
    assert resolve(postcode) is None


def test_table_holds_known_dso_keys_only() -> None:
    known = frozenset[str]().union(*REGION_DSO_KEYS.values())
    for postcode, dsos in POSTCODES.items():
        assert set(dsos) <= known, postcode
        assert list(dsos) == sorted(set(dsos)), postcode


def test_dsos_of_a_postcode_share_its_region() -> None:
    for postcode in POSTCODES:
        match = resolve(postcode)
        assert match is not None
        assert set(match.dsos) <= REGION_DSO_KEYS[match.region], postcode


def test_unpriced_dsos_are_named_on_known_postcodes_only() -> None:
    known = frozenset[str]().union(*REGION_DSO_KEYS.values())
    for postcode, names in UNPRICED.items():
        assert postcode in POSTCODES, postcode
        assert names and not set(names) & known, postcode
