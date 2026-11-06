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

"""What the manifest asks Home Assistant to install."""

from __future__ import annotations

import json
import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

_ROOT = Path(__file__).resolve().parent.parent


def test_the_pdfplumber_floor_leaves_out_the_cmaps_it_unpickled() -> None:
    """pdfplumber 0.11.0 to 0.11.7 pin a pdfminer.six that unpickles the
    CMap file a card's font names, from any folder the name reaches, and
    0.11.8 one that still unpickles it from its own: Home Assistant keeps
    whichever installed release the manifest allows. 0.11.9 pins one that
    reads them as JSON, and CI runs it."""
    manifest = json.loads(
        (_ROOT / "custom_components" / "be_gas_prices" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    (floor,) = (
        Requirement(line).specifier
        for line in manifest["requirements"]
        if Requirement(line).name == "pdfplumber"
    )
    assert not floor.contains(Version("0.11.8"))
    requirements = (_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    pinned = re.search(r"^pdfplumber==([0-9.]+)$", requirements, re.MULTILINE)
    assert pinned is not None, "requirements-dev.txt no longer pins pdfplumber"
    assert floor.contains(Version(pinned.group(1)))
