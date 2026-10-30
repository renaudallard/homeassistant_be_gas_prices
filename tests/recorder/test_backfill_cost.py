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

"""The running year's cost written into a real recorder.

The rows have to join what the recorder compiled from the live sensor, and
whether they do lies in how Home Assistant compiles against what was
imported, so these run on the real recorder.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_gas_prices.backfill_cost import _import_cost_rows, backfill_cost
from tests.recorder.test_gas_meter import _import, _setup_entry, _zone

SID = "sensor.gas_current_year_cost"
OLD = datetime(2025, 12, 31, 23, tzinfo=UTC)  # 1 January 2026, Brussels
NEW = datetime(2026, 12, 31, 23, tzinfo=UTC)  # 1 January 2027, Brussels


async def _live(
    hass: HomeAssistant, freezer: Any, when: datetime, value: float, reset: datetime
) -> None:
    """The sensor reports ``value`` at ``when`` and its five minutes compile."""
    freezer.move_to(when)
    hass.states.async_set(
        SID,
        str(value),
        {
            "device_class": "monetary",
            "state_class": "total",
            "unit_of_measurement": "EUR",
            "last_reset": reset.isoformat(),
        },
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=when.replace(minute=when.minute - when.minute % 5, second=0))
    await async_wait_recording_done(hass)


def _rows(
    hass: HomeAssistant, start: datetime, period: Literal["hour", "month"] = "hour"
) -> list[tuple[Any, ...]]:
    got = statistics_during_period(
        hass, start, None, {SID}, period, None, {"sum", "state", "change"}
    )
    return [(r.get("state"), r.get("sum"), r.get("change")) for r in got.get(SID, [])]


async def _sensor(hass: HomeAssistant) -> None:
    # Brussels, where 1 January starts a month: a read by month that ends
    # there runs on to the end of January.
    await _zone(hass)
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()


async def test_a_fresh_sensor_counts_the_months_before_its_install(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """Issue #7: installed on 10 October with 800 billed, the sensor's own
    statistics start at that figure and count nothing before it. Written
    from 1 January, the months add up to the sensor's figure, none of them
    below zero, and the live compile carries on from the imported total."""
    await _sensor(hass)
    h = datetime(2026, 10, 10, 8, tzinfo=UTC)
    await _live(hass, freezer, h + timedelta(minutes=1), 800.0, OLD)
    await _live(hass, freezer, h + timedelta(minutes=56), 800.5, OLD)
    await _live(hass, freezer, h + timedelta(hours=1, minutes=1), 800.6, OLD)
    # Up to h + 1, the hour the run began in: 9 months at 80, then 80.5 by
    # the end of h and 80.6 in the next.
    totals = [(OLD + timedelta(days=31 * n), 80.0 * (n + 1)) for n in range(9)]
    totals += [(h - timedelta(hours=1), 799.9), (h, 800.5)]
    await _import_cost_rows(hass, SID, totals, OLD)
    await async_wait_recording_done(hass)
    await _live(hass, freezer, h + timedelta(hours=1, minutes=56), 801.0, OLD)
    months = await hass.async_add_executor_job(_rows, hass, OLD - timedelta(days=1), "month")
    changes = [change for _state, _sum, change in months]
    assert min(changes) >= 0.0, months
    assert sum(changes) == pytest.approx(801.0), months
    hours = await hass.async_add_executor_job(_rows, hass, h)
    assert [r[1] for r in hours] == pytest.approx([800.5, 801.0]), hours


async def test_a_new_year_continues_last_years_total(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """The live total holds last year's bill at 31 December; the new year's
    imported hours add to it rather than start below it."""
    await _sensor(hass)
    last = NEW - timedelta(hours=2)
    await _live(hass, freezer, last + timedelta(minutes=1), 1000.0, OLD)
    await _live(hass, freezer, last + timedelta(minutes=56), 1200.0, OLD)
    chain = (await hass.async_add_executor_job(_rows, hass, last))[-1][1]
    await _live(hass, freezer, NEW + timedelta(minutes=1), 3.0, NEW)
    await _live(hass, freezer, NEW + timedelta(hours=1, minutes=56), 5.0, NEW)
    freezer.move_to(NEW + timedelta(hours=2, minutes=1))
    await _import_cost_rows(hass, SID, [(NEW, 3.0), (NEW + timedelta(hours=1), 5.0)], NEW)
    await async_wait_recording_done(hass)
    got = await hass.async_add_executor_job(_rows, hass, last)
    assert [r[2] for r in got[1:]] == pytest.approx([3.0, 2.0]), got
    assert got[-1][1] == pytest.approx(chain + 5.0), got
    await _live(hass, freezer, NEW + timedelta(hours=2, minutes=56), 5.5, NEW)
    got = await hass.async_add_executor_job(_rows, hass, last)
    assert got[-1][2] == pytest.approx(0.5), got


async def test_a_run_past_the_next_hour_moves_that_hours_row(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """On a slow host the hour after the window can close before the import:
    its row, compiled on the sensor's own total, is moved onto the imported
    one."""
    await _sensor(hass)
    h = datetime(2026, 10, 10, 8, tzinfo=UTC)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=1), 500.2, OLD)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=56), 500.3, OLD)
    await _live(hass, freezer, h + timedelta(hours=3, minutes=1), 500.35, OLD)
    await _import_cost_rows(hass, SID, [(h, 499.9), (h + timedelta(hours=1), 500.0)], OLD)
    await async_wait_recording_done(hass)
    await _live(hass, freezer, h + timedelta(hours=3, minutes=56), 500.4, OLD)
    got = await hass.async_add_executor_job(_rows, hass, h)
    assert [r[1] for r in got] == pytest.approx([499.9, 500.0, 500.3, 500.4]), got
    assert [r[2] for r in got[1:]] == pytest.approx([0.1, 0.3, 0.1]), got


async def test_nothing_is_imported_when_the_recorder_does_not_answer(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    await _sensor(hass)
    with patch(
        "homeassistant.components.recorder.statistics.statistics_during_period",
        side_effect=RuntimeError("busy"),
    ):
        assert not await _import_cost_rows(hass, SID, [(OLD, 1.0)], OLD)
    await async_wait_recording_done(hass)
    assert await hass.async_add_executor_job(_rows, hass, OLD) == []


@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_the_year_is_written_from_the_meter(
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None
) -> None:
    """Gas from 1 September: January to August hold their share of the fixed
    costs, September its gas as well, and the last hour is the sensor's
    figure."""
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    _import(hass, "m³", start, 14 * 24 + 10, 1.0)
    await async_wait_recording_done(hass)
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data
    sid = "sensor.engie_flow_current_year_cost"
    counts = await backfill_cost(hass, coordinator)
    jan1 = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 1, 1)))
    assert counts == {sid: int((dt_util.utcnow().replace(minute=0) - jan1).total_seconds()) // 3600}
    await async_wait_recording_done(hass)

    def months() -> list[Any]:
        got = statistics_during_period(hass, jan1, None, {sid}, "month", None, {"state", "change"})
        return got[sid]

    rows = await get_instance(hass).async_add_executor_job(months)
    data = coordinator.data
    assert len(rows) == 9
    assert rows[-1]["state"] == pytest.approx(data.current_year_cost, abs=1e-3)
    assert sum(row["change"] for row in rows) == pytest.approx(data.current_year_cost, abs=1e-3)
    january = data.months[0]
    assert rows[0]["change"] == pytest.approx(january.total, abs=1e-3)
    assert january.kwh == 0.0
    # The live compile resumes from the seed: its total moves by no more
    # than the rounding of the sensor's figure.
    do_adhoc_statistics(hass, start=dt_util.utcnow().replace(second=0, microsecond=0))
    await async_wait_recording_done(hass)

    def short_term() -> list[Any]:
        got = statistics_during_period(
            hass, dt_util.utcnow() - timedelta(hours=2), None, {sid}, "5minute", None, {"sum"}
        )
        return got[sid]

    # The seed at the last imported hour, then the compiled five minutes.
    got = await get_instance(hass).async_add_executor_job(short_term)
    assert len(got) == 2, got
    assert got[-1]["sum"] == pytest.approx(got[0]["sum"], abs=0.006), got
