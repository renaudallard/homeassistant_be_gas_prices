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

import asyncio
import importlib
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_gas_prices.backfill_cost import _import_cost_rows, backfill_cost
from tests.recorder.test_gas_meter import _import, _setup_entry, _zone

# The module itself: the package exports a function of the same name.
backfill_cost_module = importlib.import_module("custom_components.be_gas_prices.backfill_cost")

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
    imported hours add to it rather than start below it. The live rows
    already carry on from it, so nothing has to move them."""
    await _sensor(hass)
    last = NEW - timedelta(hours=2)
    await _live(hass, freezer, last + timedelta(minutes=1), 1000.0, OLD)
    await _live(hass, freezer, last + timedelta(minutes=56), 1200.0, OLD)
    chain = (await hass.async_add_executor_job(_rows, hass, last))[-1][1]
    await _live(hass, freezer, NEW + timedelta(minutes=1), 3.0, NEW)
    await _live(hass, freezer, NEW + timedelta(hours=1, minutes=56), 5.0, NEW)
    freezer.move_to(NEW + timedelta(hours=2, minutes=1))
    instance = get_instance(hass)
    with patch.object(
        instance, "async_adjust_statistics", wraps=instance.async_adjust_statistics
    ) as adjust:
        await _import_cost_rows(hass, SID, [(NEW, 3.0), (NEW + timedelta(hours=1), 5.0)], NEW)
    adjust.assert_not_called()
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


async def test_a_compile_between_the_read_and_the_import_joins_the_cycle(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorder compiles every five minutes, whatever the backfill is doing.
    A row it compiled after the live rows were read and before the import
    reached its queue stayed on the old total, and the next hour then fell
    by the whole bill: 1.0 after 800.5."""
    await _sensor(hass)
    h = datetime(2026, 10, 10, 8, tzinfo=UTC)
    await _live(hass, freezer, h + timedelta(minutes=1), 800.0, OLD)
    await _live(hass, freezer, h + timedelta(minutes=56), 800.5, OLD)
    await _live(hass, freezer, h + timedelta(hours=1, minutes=1), 800.6, OLD)
    totals = [(OLD + timedelta(days=31 * n), 80.0 * (n + 1)) for n in range(9)]
    totals += [(h - timedelta(hours=1), 799.9), (h, 800.5)]
    read = backfill_cost_module._read
    compiled: list[bool] = []

    async def _then_compile(*args: Any) -> list[Any] | None:
        got = await read(*args)
        if not compiled and args[4] == "hour" and args[2] >= h:
            # The last read of the live rows, then the five minutes at 09:05.
            compiled.append(True)
            await _live(hass, freezer, h + timedelta(hours=1, minutes=6), 800.7, OLD)
        return got

    monkeypatch.setattr(backfill_cost_module, "_read", _then_compile)
    assert await _import_cost_rows(hass, SID, totals, OLD)
    assert compiled
    await async_wait_recording_done(hass)
    await _live(hass, freezer, h + timedelta(hours=1, minutes=56), 801.0, OLD)
    hours = await hass.async_add_executor_job(_rows, hass, h)
    assert [r[1] for r in hours] == pytest.approx([800.5, 801.0]), hours


async def test_the_five_minute_rows_carry_on_from_the_cycle(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """The run after a tick writes that tick's bill, while the last hour it
    writes ended before the tick, so its five-minute rows hold the tick
    before. A short-term row written there with the new bill had them fall
    back by an hour's cost; they are the sensor's own now, moved, and climb
    as it did."""
    await _sensor(hass)
    h = datetime(2026, 10, 10, 8, tzinfo=UTC)
    for k in range(12):
        await _live(hass, freezer, h + timedelta(minutes=5 * k + 4), 800.0, OLD)
    await _live(hass, freezer, h + timedelta(hours=1, minutes=4), 801.0, OLD)
    totals = [(OLD + timedelta(days=31 * n), 80.0 * (n + 1)) for n in range(9)] + [(h, 801.0)]
    assert await _import_cost_rows(hass, SID, totals, OLD)
    await async_wait_recording_done(hass)

    def short() -> list[tuple[Any, ...]]:
        got = statistics_during_period(
            hass, h, None, {SID}, "5minute", None, {"sum", "state", "change"}
        )
        return [(r["state"], r["sum"], r["change"]) for r in got.get(SID, [])]

    rows = await hass.async_add_executor_job(short)
    assert min(change for _state, _sum, change in rows) >= 0.0, rows
    assert rows[-1][1] == pytest.approx(801.0), rows
    months = await hass.async_add_executor_job(_rows, hass, OLD - timedelta(days=1), "month")
    assert sum(change for _state, _sum, change in months) == pytest.approx(801.0), months


@dataclass(slots=True)
class _Held(RecorderTask):
    """Holds the recorder's queue until ``gate`` opens, as a busy database
    does."""

    gate: threading.Event

    def run(self, instance: Recorder) -> None:
        self.gate.wait(10)


async def test_two_joins_at_once_move_the_live_rows_once(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """A tick's backfill and a service call's can join the same cycle at
    once. Each moves the live rows by what it read lies between the two
    totals, so two that read before either was written moved them twice: on
    a busy recorder the next hour read 1000.5 and a change of 500.5. One join
    at a time, each reading what the last one wrote, moves them once."""
    await _sensor(hass)
    h = datetime(2026, 10, 10, 8, tzinfo=UTC)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=1), 500.2, OLD)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=56), 500.3, OLD)
    totals = [(h, 499.9), (h + timedelta(hours=1), 500.0)]
    gate = threading.Event()
    get_instance(hass).queue_task(_Held(gate))
    opener = threading.Timer(0.5, gate.set)
    opener.start()
    try:
        got = await asyncio.gather(
            _import_cost_rows(hass, SID, totals, OLD), _import_cost_rows(hass, SID, totals, OLD)
        )
    finally:
        opener.cancel()
        gate.set()
    await async_wait_recording_done(hass)
    assert got == [True, True]
    await _live(hass, freezer, h + timedelta(hours=3, minutes=56), 500.4, OLD)
    rows = await hass.async_add_executor_job(_rows, hass, h)
    assert [r[1] for r in rows] == pytest.approx([499.9, 500.0, 500.3, 500.4]), rows
    assert [r[2] for r in rows[1:]] == pytest.approx([0.1, 0.3, 0.1]), rows


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
    recorder_mock: Any, hass: HomeAssistant, enable_custom_integrations: None, freezer: Any
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
    # Nothing compiled for the sensor yet: there is no total to join the
    # cycle to, so nothing is written and the next tick asks again.
    assert await backfill_cost(hass, coordinator) == {}
    # The sensor's first five minutes, which the cycle is joined to.
    do_adhoc_statistics(hass, start=dt_util.utcnow().replace(second=0, microsecond=0))
    await async_wait_recording_done(hass)
    # The year's bills are spread off the event loop.
    loop_thread = threading.get_ident()
    spread_on: list[int] = []
    cost_rows = backfill_cost_module.cost_rows

    def _spread(*args: Any) -> Any:
        spread_on.append(threading.get_ident())
        return cost_rows(*args)

    with patch.object(backfill_cost_module, "cost_rows", _spread):
        counts = await backfill_cost(hass, coordinator)
    assert spread_on and loop_thread not in spread_on
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
    # The live compile resumes from its own five minutes, moved onto the
    # imported total: its total moves by no more than the rounding of the
    # sensor's figure.
    freezer.tick(timedelta(minutes=5))
    do_adhoc_statistics(hass, start=dt_util.utcnow().replace(second=0, microsecond=0))
    await async_wait_recording_done(hass)

    def short_term() -> list[Any]:
        got = statistics_during_period(
            hass, dt_util.utcnow() - timedelta(hours=2), None, {sid}, "5minute", None, {"sum"}
        )
        return got[sid]

    # The five minutes before the backfill, moved, then the next ones.
    got = await get_instance(hass).async_add_executor_job(short_term)
    assert len(got) == 2, got
    assert got[-1]["sum"] == pytest.approx(got[0]["sum"], abs=0.006), got


@pytest.mark.parametrize("why", ["disabled", "excluded"])
@pytest.mark.freeze_time("2026-09-15 10:30:00+02:00")
async def test_a_cost_sensor_the_recorder_does_not_compile_is_left_alone(
    recorder_mock: Any,
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    why: str,
) -> None:
    """A disabled cost sensor, or one the recorder excludes, never compiles
    a row for the cycle to join, so its year is not written, and no tick
    reads the meter's year for it again."""
    await _zone(hass)
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 9, 1)))
    _import(hass, "m³", start, 14 * 24 + 10, 1.0)
    await async_wait_recording_done(hass)
    entry = await _setup_entry(hass)
    coordinator = entry.runtime_data
    sid = "sensor.engie_flow_current_year_cost"
    do_adhoc_statistics(hass, start=dt_util.utcnow().replace(second=0, microsecond=0))
    await async_wait_recording_done(hass)
    if why == "disabled":
        er.async_get(hass).async_update_entity(sid, disabled_by=er.RegistryEntryDisabler.USER)
    else:
        monkeypatch.setattr(get_instance(hass), "entity_filter", lambda entity_id: entity_id != sid)
    reads = AsyncMock(side_effect=backfill_cost_module.hourly_changes)
    with patch.object(backfill_cost_module, "hourly_changes", reads):
        assert await backfill_cost(hass, coordinator) == {}
    reads.assert_not_awaited()
    await async_wait_recording_done(hass)
    jan1 = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 1, 1)))

    def hours() -> list[Any]:
        return statistics_during_period(hass, jan1, None, {sid}, "hour", None, {"sum"}).get(sid, [])

    assert await get_instance(hass).async_add_executor_job(hours) == []
