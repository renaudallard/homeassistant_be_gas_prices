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


"""The card archive's law table: written from what the integration bills
each month on from the law (``scripts/archive_cards.py``), read back by an
installation (``law_archive.py``), and billed only for a month its own
readings do not give."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.be_gas_prices import archive_table, excise_law, law_archive, tarbel
from custom_components.be_gas_prices.const import ENERGY_CONTRIBUTION_RESIDENTIAL_HTVA
from custom_components.be_gas_prices.law_archive import ensure_law_archive
from custom_components.be_gas_prices.providers._resolve import (
    law_contribution,
    law_excise,
    law_protected_excise,
)
from custom_components.be_gas_prices.providers.base import ExtractorError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import archive_cards as ac  # type: ignore[import-not-found]

SEP = date(2026, 9, 1)
KEPT = {"excise": [0.0105, 0.0114], "protected": 0.0031, "contribution": 0.0005}


def _readings_gone() -> None:
    """Neither FPS Finance's tariff nor Justel read."""
    tarbel._HELD.clear()
    tarbel._PROTECTED_HELD.clear()
    tarbel._CONTRIBUTION_HELD.clear()
    excise_law._HELD.clear()
    excise_law._PROTECTED_HELD.clear()
    excise_law._CONTRIBUTION_HELD.clear()


# The reader.


def test_only_figures_that_read_are_held() -> None:
    law_archive.hold(
        {
            "residential": {
                "2026-09": KEPT,
                "2026-10": {"excise": [0.0105], "protected": -0.1, "contribution": 0.0},
                "2026-11": {"excise": [0.0105, 0.5], "protected": False},
                "2026-13": KEPT,
                "next": KEPT,
            }
        }
    )
    assert law_archive.excise_for(SEP) == (0.0105, 0.0114)
    assert law_archive.protected_excise_for(SEP) == 0.0031
    assert law_archive.contribution_for(SEP) == 0.0005
    # Each figure on its own: October keeps its contribution alone.
    october = date(2026, 10, 1)
    assert law_archive.excise_for(october) is None
    assert law_archive.protected_excise_for(october) is None
    assert law_archive.contribution_for(october) == 0.0
    assert law_archive.held_table() == {
        "residential": {"2026-09": KEPT, "2026-10": {"contribution": 0.0}}
    }
    # A month the table does not hold is no month before or after it.
    assert law_archive.excise_for(date(2026, 8, 1)) is None


def test_every_month_of_the_archives_table_is_held() -> None:
    """law.json as the card archive published it on 6 November 2026, January
    2025 to November 2026, figure for figure."""
    table = json.loads((Path(__file__).parent / "fixtures/card_archive/law.json").read_text())
    law_archive.hold(table)
    assert law_archive.held_table() == table
    held = law_archive.held_table()["residential"]
    assert (min(held), max(held), len(held)) == ("2025-01", "2026-11", 23)


@freeze_time("2026-11-06 12:00:00+01:00")
def test_only_a_month_from_2000_to_next_year_is_held() -> None:
    """A table is written to every entry's Store on each tick, and 600,000
    months made up by a tampered archive made a 49 MiB one."""
    made_up = {f"{year}-01": KEPT for year in range(2028, 22028)}
    law_archive.hold(
        {"residential": {**made_up, "1999-12": KEPT, "2026-09": KEPT, "2027-12": KEPT}}
    )
    assert law_archive.held_table() == {"residential": {"2026-09": KEPT, "2027-12": KEPT}}


def test_an_empty_table_does_not_wipe_a_held_one() -> None:
    law_archive.hold({"residential": {"2026-09": KEPT}})
    law_archive.hold({"residential": {}})
    law_archive.hold({"other": {}})
    assert law_archive.excise_for(SEP) == (0.0105, 0.0114)


def test_the_store_round_trips_and_never_replaces_a_read() -> None:
    law_archive.restore({"residential": {"2026-09": KEPT}})
    stored = law_archive.held_table()
    law_archive.restore({"residential": {"2026-09": {"protected": 0.009}}})
    assert law_archive.held_table() == stored
    law_archive._HELD.clear()
    law_archive.restore(stored)
    assert law_archive.held_table() == stored
    law_archive._HELD.clear()
    law_archive.restore(None)
    law_archive.restore({"residential": "no months"})
    assert law_archive.held_table() == {"residential": {}}


async def test_the_table_is_read_once_a_day_and_a_failure_never_raises() -> None:
    fetch = AsyncMock(return_value=json.dumps({"residential": {"2026-09": KEPT}}))
    with patch.object(archive_table, "fetch_text", fetch):
        await ensure_law_archive(None)  # type: ignore[arg-type]
        await ensure_law_archive(None)  # type: ignore[arg-type]
    assert fetch.await_count == 1
    assert fetch.await_args is not None
    assert fetch.await_args.args[1].endswith("/be_price_cards/main/gas/law.json")
    assert law_archive.protected_excise_for(SEP) == 0.0031
    law_archive._TABLE.fetched_at = dt_util.utcnow() - timedelta(days=2)
    for failure in (ExtractorError("timeout"), "not json", "[1, 2]"):
        law_archive._TABLE.failed_at = None
        answer = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else None
        with patch.object(archive_table, "fetch_text", answer or AsyncMock(return_value=failure)):
            await ensure_law_archive(None)  # type: ignore[arg-type]
        assert law_archive._TABLE.failed_at is not None
        # What was read goes on billing.
        assert law_archive.protected_excise_for(SEP) == 0.0031


async def test_a_failure_waits_six_hours_before_the_table_is_asked_again() -> None:
    fetch = AsyncMock(side_effect=ExtractorError("timeout"))
    with patch.object(archive_table, "fetch_text", fetch):
        await ensure_law_archive(None)  # type: ignore[arg-type]
        await ensure_law_archive(None)  # type: ignore[arg-type]
        assert fetch.await_count == 1
        law_archive._TABLE.failed_at = dt_util.utcnow() - timedelta(hours=7)
        await ensure_law_archive(None)  # type: ignore[arg-type]
    assert fetch.await_count == 2


# Billed on it.


def test_the_readings_are_billed_before_the_archive() -> None:
    """FPS Finance's tariff and Justel give September 2026, and Justel
    alone January 2027: what the archive kept of them is not billed."""
    january = date(2027, 1, 1)
    law_archive.hold({"residential": {"2026-09": KEPT, "2027-01": KEPT}})
    assert law_excise(SEP) == pytest.approx((0.01031, 0.01116))
    assert law_protected_excise(SEP) == pytest.approx(0.00309)
    assert law_contribution(SEP) == 0.0
    assert law_excise(january) == pytest.approx((0.01139, 0.01224))
    assert law_protected_excise(january) == pytest.approx(0.00342)
    assert law_contribution(january) == 0.0


def test_a_month_the_readings_no_longer_give_is_billed_on_the_archive() -> None:
    _readings_gone()
    law_archive.hold({"residential": {"2026-09": KEPT, "2026-07": KEPT}})
    assert law_excise(SEP) == (0.0105, 0.0114)
    assert law_protected_excise(SEP) == 0.0031
    assert law_contribution(SEP) == 0.0005
    # Ahead of const.py's contribution, behind its excise quarters, which
    # stand in for the tariff's notices in their window.
    july = date(2026, 7, 1)
    assert law_contribution(july) == 0.0005
    assert law_excise(july) == pytest.approx((0.00823, 0.0093315))
    # Without it, const.py answers as before.
    law_archive._HELD.clear()
    assert law_excise(SEP) is None
    assert law_protected_excise(SEP) is None
    assert law_contribution(SEP) == 0.0
    assert law_contribution(july) == ENERGY_CONTRIBUTION_RESIDENTIAL_HTVA


# Written from the readings.


def test_the_table_is_what_the_integration_bills_each_month_on(tmp_path: Path) -> None:
    held = ac._write_law(tmp_path, date(2026, 11, 2))
    table = json.loads((tmp_path / "law.json").read_text())["residential"]
    assert held == len(table) == 23
    assert min(table) == "2025-01" and max(table) == "2026-11"
    assert table["2025-01"]["excise"] == pytest.approx([0.00823, 0.0089393])
    assert table["2025-01"]["contribution"] == pytest.approx(0.0009978)
    assert table["2025-01"]["protected"] == pytest.approx(0.00277)
    assert table["2026-11"]["excise"] == pytest.approx([0.01031, 0.01116])
    assert table["2026-11"]["contribution"] == 0.0
    assert table["2026-11"]["protected"] == pytest.approx(0.00309)
    before = (tmp_path / "law.json").read_bytes()
    ac._write_law(tmp_path, date(2026, 11, 2))
    assert (tmp_path / "law.json").read_bytes() == before


def test_a_month_the_readings_no_longer_give_keeps_what_was_written(tmp_path: Path) -> None:
    (tmp_path / "law.json").write_text(
        json.dumps({"residential": {"2024-12": KEPT, "2026-09": KEPT}}), encoding="utf-8"
    )
    _readings_gone()
    ac._write_law(tmp_path, date(2026, 11, 2))
    table = json.loads((tmp_path / "law.json").read_text())["residential"]
    # Before the window, and what no reading gives any more, stay. So does
    # the contribution: const.py still gives one, but it is asked last, and
    # only fills a month that holds none.
    assert table["2024-12"] == KEPT
    assert table["2026-09"] == KEPT
    assert table["2026-10"] == {"contribution": 0.0}


# A made-up quarter of 2027, for the figures FPS Finance's tariff alone gives.
_QUARTER = (date(2027, 1, 1), date(2027, 3, 31))


def _written(out: Path, today: date) -> dict[str, dict[str, Any]]:
    """The table after one more run on ``today``."""
    ac._write_law(out, today)
    table: dict[str, dict[str, Any]] = json.loads((out / "law.json").read_text())["residential"]
    return table


def test_the_law_s_yearly_step_never_replaces_the_tariff_s_notice(tmp_path: Path) -> None:
    """The tariff lists a notice for the first quarter of 2027 above the
    step the law voted for the year, which is all Justel prints. A run holds
    only what it read: one that read Justel and not the tariff, or neither,
    wrote the step over the notice. The notice stays, the step still fills
    a month that holds no excise, and the tariff, read again, corrects it."""
    notice, step = [0.01139, 0.0125], [0.01139, 0.01224]
    february, april = date(2027, 2, 10), date(2027, 4, 10)
    tarbel.hold(((*_QUARTER, 0.01139, 0.0125),))
    assert _written(tmp_path, february)["2027-01"]["excise"] == notice
    tarbel._HELD.clear()
    assert law_excise(date(2027, 1, 1)) == pytest.approx(step)
    table = _written(tmp_path, april)
    assert table["2027-01"]["excise"] == table["2027-02"]["excise"] == notice
    assert table["2027-03"]["excise"] == table["2027-04"]["excise"] == pytest.approx(step)
    _readings_gone()
    assert _written(tmp_path, april)["2027-01"]["excise"] == notice
    tarbel.hold(((date(2027, 4, 1), date(2027, 6, 30), 0.01139, 0.0126),))
    table = _written(tmp_path, april)
    assert table["2027-04"]["excise"] == [0.01139, 0.0126]
    assert table["2027-03"]["excise"] == pytest.approx(step)


def test_a_typed_quarter_is_written_over_what_was_there(tmp_path: Path) -> None:
    """The quarters const.py types are the tariff's notices, never the
    law's yearly step, so they are written like the tariff's own."""
    (tmp_path / "law.json").write_text(
        json.dumps({"residential": {"2026-07": KEPT}}), encoding="utf-8"
    )
    _readings_gone()
    table = _written(tmp_path, date(2026, 11, 2))
    assert table["2026-07"]["excise"] == pytest.approx([0.00823, 0.0093315])


def test_the_tariff_never_replaces_the_protected_rate_justel_gave(tmp_path: Path) -> None:
    """The protected customer's rate is asked of Justel first. On a day
    Justel is not read, the tariff's figure fills a month that holds none
    and leaves the others, and Justel, read again, corrects it."""
    february, march = date(2027, 2, 10), date(2027, 3, 10)
    assert _written(tmp_path, february)["2027-01"]["protected"] == pytest.approx(0.00342)
    kept = list(excise_law._PROTECTED_HELD)
    excise_law._PROTECTED_HELD.clear()
    tarbel.hold_protected(((*_QUARTER, 0.003),))
    table = _written(tmp_path, march)
    assert table["2027-01"]["protected"] == pytest.approx(0.00342)
    assert table["2027-03"]["protected"] == 0.003
    excise_law._PROTECTED_HELD[:] = kept
    assert _written(tmp_path, march)["2027-03"]["protected"] == pytest.approx(0.00342)


def test_only_the_tariff_replaces_a_contribution_already_written(tmp_path: Path) -> None:
    """The contribution is asked of the tariff first, then of Justel, and
    last of const.py: what either of those gives fills a month that holds
    none and never replaces what the tariff wrote."""
    february, april = date(2027, 2, 10), date(2027, 4, 10)
    tarbel.hold_contribution(((*_QUARTER, 0.0005),))
    assert _written(tmp_path, february)["2027-01"]["contribution"] == 0.0005
    tarbel._CONTRIBUTION_HELD.clear()
    assert law_contribution(date(2027, 1, 1)) == 0.0
    assert _written(tmp_path, february)["2027-01"]["contribution"] == 0.0005
    _readings_gone()
    table = _written(tmp_path, april)
    assert table["2027-01"]["contribution"] == table["2027-02"]["contribution"] == 0.0005
    assert table["2027-04"]["contribution"] == 0.0
    tarbel.hold_contribution(((date(2027, 4, 1), date(2027, 6, 30), 0.0004),))
    assert _written(tmp_path, april)["2027-04"]["contribution"] == 0.0004
