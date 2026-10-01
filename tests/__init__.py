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

import hashlib
import os
from functools import cache
from importlib.metadata import version
from pathlib import Path
from typing import Any, cast

import pytest

from custom_components.be_gas_prices.providers._pdf import (
    extract_pdf_text,
    extract_pdf_text_layout,
)

FIXTURES = Path(__file__).parent / "fixtures"
_ROOT = Path(__file__).resolve().parent.parent

_READERS = {
    "plain": extract_pdf_text,
    "layout": extract_pdf_text_layout,
}


def _text_cache_dir() -> Path:
    """Where ``fixture_text`` keeps what it read, across runs.

    One directory per reader code: the digest covers ``providers/_pdf.py``
    and the pypdf and pdfplumber versions, so a change to either reads every
    card afresh instead of serving text the current code would not produce.
    Under ``tmp/`` by default, which git ignores; ``BE_FIXTURE_TEXT_CACHE``
    moves it, which the gate does so its throwaway worktree reuses the main
    checkout's.
    """
    readers = hashlib.sha256(
        (_ROOT / "custom_components" / "be_gas_prices" / "providers" / "_pdf.py").read_bytes()
    )
    for dist in ("pypdf", "pdfplumber"):
        readers.update(f"{dist} {version(dist)}".encode())
    base = os.environ.get("BE_FIXTURE_TEXT_CACHE") or _ROOT / "tmp" / "fixture_text"
    return Path(base).resolve() / readers.hexdigest()[:16]


_TEXT_CACHE = _text_cache_dir()


@cache
def fixture_text(supplier: str, name: str, reader: str = "plain") -> str:
    """The text of a fixture card, read the way the extractor reads it.

    Reading the cards is most of what the suite spends: a Frank or Bolt card
    takes 40 to 50 seconds of pdfplumber on a Raspberry Pi. The text is kept
    on disk (``_text_cache_dir``), keyed on the card's own digest and the
    reader, so a run after the first reads none of them again, and a card
    that fails to read raises as before and leaves nothing behind. Stored as
    bytes, so the text comes back exactly, carriage returns included.

    Also cached in memory for the life of the process, so a worker reads a
    file off the disk once. Tests must not mutate the returned string.
    """
    payload = (FIXTURES / supplier / name).read_bytes()
    kept = _TEXT_CACHE / f"{hashlib.sha256(payload).hexdigest()}.{reader}.txt"
    try:
        return kept.read_bytes().decode("utf-8", "surrogatepass")
    except FileNotFoundError:
        pass
    text = _READERS[reader](payload)
    # Written aside and renamed into place: the xdist workers read and write
    # the same directory, and a reader must never see half a file.
    kept.parent.mkdir(parents=True, exist_ok=True)
    partial = kept.with_name(f"{kept.name}.{os.getpid()}")
    partial.write_bytes(text.encode("utf-8", "surrogatepass"))
    os.replace(partial, kept)
    return text


def fixture_page(supplier: str, name: str) -> str:
    """A fixture HTML or JSON page as text."""
    return (FIXTURES / supplier / name).read_text(encoding="utf-8")


def approx(expected: float, **tolerance: Any) -> float:
    """``pytest.approx`` typed as the float it stands in for, so a dataclass
    built to compare against type-checks."""
    return cast(float, pytest.approx(expected, **tolerance))
