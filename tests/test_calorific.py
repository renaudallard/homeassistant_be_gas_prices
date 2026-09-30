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

"""Reading Atrias's monthly calorific value files."""

from __future__ import annotations

import pytest

from custom_components.be_gas_prices.calorific import CalorificError, Station, parse_gcv_file

# The August 2026 file's shape: a byte order mark, quoted decimal commas, a
# station out of use listed at zero.
_FILE = (
    "﻿GCVMonth,ARSName,ARSEanGSRN,GCVValue\r\n"
    '2026-08,GOS FLUVIUS - AALST,541454827090000155,"11,5192589960"\r\n'
    '2026-08,GOS FLUVIUS - GENT MANUPORT,541454827090000100,"0,0000000000"\r\n'
    "2026-08,GOS FLUVIUS - GENT,541454827090000087,\r\n"
)


def test_a_station_out_of_use_or_without_a_value_is_left_out() -> None:
    assert parse_gcv_file(_FILE.encode()) == {
        Station(ean="541454827090000155", name="GOS FLUVIUS - AALST"): pytest.approx(11.519258996)
    }


def test_a_value_that_is_not_a_figure_is_a_calorific_error() -> None:
    """Not the parsers' ExtractorError, which no caller of this catches."""
    broken = _FILE + "2026-08,GOS FLUVIUS - BEVEREN,541454827090000056,n/a\r\n"
    with pytest.raises(CalorificError, match="bad value"):
        parse_gcv_file(broken.encode())
