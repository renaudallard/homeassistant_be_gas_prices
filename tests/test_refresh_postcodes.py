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

"""scripts/refresh_postcodes.py: a source that comes back short or fails
writes nothing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import refresh_postcodes as rp  # type: ignore[import-not-found]

KEYS = sorted(set(rp.SYNERGRID_GAS_DSOS.values()))


@pytest.fixture
def output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where the map would be written, and no pause between requests."""
    path = tmp_path / "postcode_map.py"
    monkeypatch.setattr(rp, "OUTPUT", path)
    monkeypatch.setattr(rp.time, "sleep", lambda _s: None)
    return path


def _bpost(count: int) -> dict[str, list[str]]:
    return {f"{1000 + n}": [f"Town {n}"] for n in range(count)}


def _table(count: int) -> dict[str, tuple[str, ...]]:
    """``count`` postcodes, every DSO key served by one of them."""
    return {f"{1000 + n}": (KEYS[n % len(KEYS)],) for n in range(count)}


def test_a_short_bpost_list_writes_nothing(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated file or a changed layout, not a smaller Belgium."""
    monkeypatch.setattr(rp, "bpost_postcodes", lambda: ("https://x/list.xls", _bpost(40)))
    built: list[Any] = []
    monkeypatch.setattr(rp, "build", lambda bpost: built.append(bpost))
    assert rp.main() == 1
    assert "aborting, nothing written: bpost lists 40 postcodes" in capsys.readouterr().err
    assert not built
    assert not output.exists()


def test_a_lookup_that_keeps_failing_writes_nothing(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Synergrid down: each request is tried ATTEMPTS times, then the run is
    given up rather than a map written without the postcodes it lost."""
    monkeypatch.setattr(
        rp, "bpost_postcodes", lambda: ("https://x/list.xls", _bpost(rp.MIN_POSTCODES))
    )
    calls: list[object] = []

    def down(request: object) -> bytes:
        calls.append(request)
        raise TimeoutError("timed out")

    monkeypatch.setattr(rp, "_read", down)
    assert rp.main() == 1
    assert len(calls) == rp.ATTEMPTS
    assert "aborting, nothing written: Synergrid" in capsys.readouterr().err
    assert not output.exists()


def test_a_lookup_answering_garbage_writes_nothing(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        rp, "bpost_postcodes", lambda: ("https://x/list.xls", _bpost(rp.MIN_POSTCODES))
    )
    monkeypatch.setattr(rp, "_read", lambda _request: b'"<html>maintenance</html>"')
    assert rp.main() == 1
    assert "unexpected answer" in capsys.readouterr().err
    assert not output.exists()


def test_a_flaky_lookup_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp.time, "sleep", lambda _s: None)
    answers: list[Any] = [OSError("reset"), ValueError("half a body"), b'{"results": []}']

    def flaky(_request: object) -> bytes:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return bytes(answer)

    monkeypatch.setattr(rp, "_read", flaky)
    assert rp.search("1000") == []
    assert not answers


def test_a_short_map_writes_nothing(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Synergrid answering for too few postcodes is a broken walk."""
    monkeypatch.setattr(
        rp, "bpost_postcodes", lambda: ("https://x/list.xls", _bpost(rp.MIN_POSTCODES))
    )
    monkeypatch.setattr(rp, "build", lambda _bpost: (_table(rp.MIN_POSTCODES - 1), {}))
    assert rp.main() == 1
    assert f"only {rp.MIN_POSTCODES - 1} postcodes" in capsys.readouterr().err
    assert not output.exists()


def test_a_map_missing_a_dso_writes_nothing(
    output: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        rp, "bpost_postcodes", lambda: ("https://x/list.xls", _bpost(rp.MIN_POSTCODES))
    )
    table = {code: keys for code, keys in _table(rp.MIN_POSTCODES).items() if keys != (KEYS[0],)}
    table.update({f"{5000 + n}": (KEYS[1],) for n in range(rp.MIN_POSTCODES)})
    monkeypatch.setattr(rp, "build", lambda _bpost: (table, {}))
    assert rp.main() == 1
    assert f"no postcode for ['{KEYS[0]}']" in capsys.readouterr().err
    assert not output.exists()


def test_a_whole_map_is_written(output: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the same run with nothing short writes the map."""
    monkeypatch.setattr(
        rp, "bpost_postcodes", lambda: ("https://x/list.xls", _bpost(rp.MIN_POSTCODES))
    )
    monkeypatch.setattr(rp, "build", lambda _bpost: (_table(rp.MIN_POSTCODES), {}))
    assert rp.main() == 0
    assert '"1000": (' in output.read_text(encoding="utf-8")
