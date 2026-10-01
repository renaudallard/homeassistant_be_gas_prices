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

"""A card or link whose month is no month ("0000", "13") is an
ExtractorError, which every caller handles, never a ValueError."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from custom_components.be_gas_prices.providers import (
    ebem,
    eneco,
    engie,
    luminus,
    mega,
    octaplus,
    trevion,
)
from custom_components.be_gas_prices.providers._energy_together import last_known_index
from custom_components.be_gas_prices.providers._parse import month_date
from custom_components.be_gas_prices.providers.base import ExtractorError


def test_month_date() -> None:
    assert month_date("2026", "09", "Acme") == date(2026, 9, 1)
    for year, month in (("0000", "09"), ("2026", "13"), ("2026", "00")):
        with pytest.raises(ExtractorError, match=rf"Acme: {year}-{month} is no month"):
            month_date(year, month, "Acme")


@pytest.mark.parametrize(
    ("read", "text"),
    [
        (engie._card_month, "pour les contrats conclus en Septembre 0000 (durée de 2 ans)"),
        (eneco._card_month, "Tariefkaart september 0000 van Eneco Belgium nv"),
        (mega._card_month, "Prix du mois 09/0000"),
        (octaplus._card_month, "FICHE TARIFAIRE OCTOBRE 0000"),
        (luminus._title, "Luminus ComfyFlex Gaz(février 0000)"),
        (lambda text: trevion._month(*text.split()), "september 0000"),
    ],
)
def test_a_card_printing_year_0000_is_an_extractor_error(
    read: Callable[[str], Any], text: str
) -> None:
    with pytest.raises(ExtractorError, match="is no month"):
        read(text)


def test_an_ebem_link_that_is_no_month_is_skipped() -> None:
    page = "".join(
        f'<a href="/media/x/ebem_tariefkaart-gas-{name}.pdf">'
        for name in ("09-0000", "13-2026", "09-2026")
    )
    assert list(ebem._links(page, ebem._CARD_RE)) == [date(2026, 9, 1)]


def test_an_index_footnote_that_is_no_month_states_no_value() -> None:
    text = "laatst gekende waarde van TTF-DAM 8/0000: €61,729/MWh"
    assert last_known_index(text) is None
