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

"""excise_law.py: the residential gas excise read from article 419."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from homeassistant.util import dt as dt_util

from custom_components.be_gas_prices import excise_law
from custom_components.be_gas_prices.excise_law import ensure_excise_law
from custom_components.be_gas_prices.providers.base import ExtractorError

_PAGE = (
    (Path(__file__).parent / "fixtures" / "justel" / "loi_programme_2004_fr.html")
    .read_bytes()
    .decode("iso-8859-1")
)


@pytest.fixture(autouse=True)
def _nothing_held() -> None:
    """conftest holds the published article; these tests read it."""
    excise_law._HELD.clear()
    excise_law._PROTECTED_HELD.clear()
    excise_law._fetched_at = None
    excise_law._failed_at = None


def test_the_two_slices_are_read_with_their_steps() -> None:
    """The rate in force dated by the amendment that wrote it, 1 August
    2026, and the steps the same law voted."""
    assert excise_law.parse(_PAGE) == (
        (date(2026, 8, 1), 0.01031, 0.01116),
        (date(2027, 1, 1), 0.01139, pytest.approx(0.01224)),
        (date(2028, 1, 1), 0.01247, 0.01332),
        (date(2029, 1, 1), 0.0136, 0.01445),
    )


def test_the_protected_customer_s_rate_is_read_with_its_steps() -> None:
    """3,09 EUR/MWh from 1 August 2026, the same law's wording, and its own
    steps after it."""
    assert excise_law.parse_protected(_PAGE) == (
        (date(2026, 8, 1), 0.00309),
        (date(2027, 1, 1), 0.00342),
        (date(2028, 1, 1), pytest.approx(0.00374)),
        (date(2029, 1, 1), 0.00408),
    )
    excise_law.hold_protected(excise_law.parse_protected(_PAGE))
    assert excise_law.protected_excise_for(date(2026, 7, 1)) is None
    assert excise_law.protected_excise_for(date(2026, 12, 31)) == 0.00309
    assert excise_law.protected_excise_for(date(2027, 1, 1)) == 0.00342


async def test_a_protected_rate_that_cannot_be_read_leaves_the_household_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _PAGE.replace("client protégé résidentiel", "client résidentiel visé")

    async def fetch_text(_session: Any, _url: str, **_kwargs: Any) -> str:
        return page

    monkeypatch.setattr(excise_law, "fetch_text", fetch_text)
    await ensure_excise_law(None)  # type: ignore[arg-type]
    assert excise_law.excise_for(date(2026, 8, 1)) == (0.01031, 0.01116)
    assert excise_law.protected_excise_for(date(2026, 8, 1)) is None
    assert excise_law._failed_at is None


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        ("the band moved", "tranche de 0 à 12 MWh", "tranche de 0 à 15 MWh"),
        ("a unit slipped", "spécial: 10,31 euros", "spécial: 103,1 euros"),
        ("no gas paragraph", "gaz naturel relevant", "gaz relevant"),
    ],
)
def test_an_article_that_is_not_as_expected_is_refused(label: str, old: str, new: str) -> None:
    assert _PAGE.count(old) == 1, label
    with pytest.raises(excise_law.ExciseLawError):
        excise_law.parse(_PAGE.replace(old, new))


def test_a_month_takes_the_step_in_force_on_its_first_day() -> None:
    excise_law.hold(excise_law.parse(_PAGE))
    assert excise_law.excise_for(date(2026, 7, 31)) is None
    assert excise_law.excise_for(date(2026, 8, 15)) == (0.01031, 0.01116)
    assert excise_law.excise_for(date(2027, 1, 1)) == (0.01139, pytest.approx(0.01224))
    assert excise_law.excise_for(date(2031, 6, 1)) == (0.0136, 0.01445)


def test_a_later_wording_keeps_the_steps_before_it() -> None:
    """An amendment rewriting the article drops what it replaced from the
    text: the steps read before stay billed up to its first day."""
    excise_law.hold(excise_law.parse(_PAGE))
    excise_law.hold(((date(2027, 4, 1), 0.012, 0.013),))
    assert excise_law.excise_for(date(2027, 2, 1)) == (0.01139, pytest.approx(0.01224))
    assert excise_law.excise_for(date(2027, 4, 1)) == (0.012, 0.013)
    assert excise_law.excise_for(date(2028, 6, 1)) == (0.012, 0.013)


def test_the_store_round_trips_and_never_replaces_a_read() -> None:
    excise_law.hold(excise_law.parse(_PAGE))
    stored = excise_law.held_table()
    excise_law._HELD.clear()
    excise_law.restore(stored)
    assert excise_law.held_table() == stored
    excise_law.restore([["2026-08-01", 0.5, 0.5]])
    assert excise_law.held_table() == stored
    excise_law._HELD.clear()
    for broken in (None, [["2026-08-01", 0.5, 0.5]], [["not a day", 0.01, 0.01]], [[1]]):
        excise_law.restore(broken)
        assert excise_law._HELD == [], broken
    excise_law.hold_protected(excise_law.parse_protected(_PAGE))
    protected = excise_law.protected_held_table()
    excise_law._PROTECTED_HELD.clear()
    excise_law.restore_protected(protected)
    assert excise_law.protected_held_table() == protected
    excise_law.restore_protected([["2026-08-01", 0.5]])
    assert excise_law.protected_held_table() == protected


async def test_the_law_is_read_once_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    async def fetch_text(_session: Any, url: str, **_kwargs: Any) -> str:
        asked.append(url)
        return _PAGE

    monkeypatch.setattr(excise_law, "fetch_text", fetch_text)
    await ensure_excise_law(None)  # type: ignore[arg-type]
    await ensure_excise_law(None)  # type: ignore[arg-type]
    assert len(asked) == 1
    assert excise_law.excise_for(date(2027, 1, 1)) == (0.01139, pytest.approx(0.01224))
    excise_law._fetched_at = dt_util.utcnow() - timedelta(days=2)
    await ensure_excise_law(None)  # type: ignore[arg-type]
    assert len(asked) == 2


@pytest.mark.parametrize(
    "failure",
    [
        ExtractorError("network error fetching https://www.ejustice.just.fgov.be: timeout"),
        "<html>maintenance</html>",
        _PAGE.replace("1er janvier 2027", "31 février 2027"),
    ],
)
async def test_a_failure_never_raises_and_waits_before_asking_again(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | str
) -> None:
    """The caller is the coordinator tick: nothing may escape it, and a
    failure is not asked again every hour."""
    asked: list[str] = []

    async def fetch_text(_session: Any, url: str, **_kwargs: Any) -> str:
        asked.append(url)
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(excise_law, "fetch_text", fetch_text)
    await ensure_excise_law(None)  # type: ignore[arg-type]
    await ensure_excise_law(None)  # type: ignore[arg-type]
    assert len(asked) == 1
    assert excise_law._HELD == []


async def test_a_failure_with_nothing_held_names_the_months_left_on_the_card(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """const.py's quarters bill up to July 2026 whatever Justel answers, so
    only the months from August 2026 a tariff read does not list fall to
    the card's own excise."""

    async def fetch_text(_session: Any, url: str, **_kwargs: Any) -> str:
        raise ExtractorError(f"network error fetching {url}: timeout")

    monkeypatch.setattr(excise_law, "fetch_text", fetch_text)
    await ensure_excise_law(None)  # type: ignore[arg-type]
    assert "a month from 2026-08 on" in caplog.text
