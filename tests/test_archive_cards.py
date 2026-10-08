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

"""scripts/archive_cards.py: the daily writer of the gas card archive.

The supplier here is synthetic: a listing page and a PDF whose text carries
the card month and a price, parsed through the integration's own readers, so
the memo, the render hook and the digests are the real ones.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import yaml  # type: ignore[import-untyped]
from homeassistant.util import dt as dt_util

from custom_components.be_gas_prices.const import CARD_ARCHIVE_URL
from custom_components.be_gas_prices.month_cards import fetch_archived_card, fetch_archived_row
from custom_components.be_gas_prices.providers import _pdf
from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._rates import Contract, FixedRates
from custom_components.be_gas_prices.providers._validity import end_of_month
from custom_components.be_gas_prices.providers.base import (
    CardNotReadableError,
    DsoOverlay,
    DsoTier,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)
from custom_components.be_gas_prices.snapshot_codec import snapshot_from_json, snapshot_to_json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
import archive_cards as ac  # type: ignore[import-not-found]
import card_texts  # type: ignore[import-not-found]

NOW = datetime(2026, 9, 11, 6, 0, tzinfo=UTC)
LISTING_URL = "https://acme.test/tariffs"
CARD_URL = "https://acme.test/card.pdf"
ROW = "cards/acme/acme_fix/wallonia"


def _card(month: str, price: str) -> bytes:
    return f"%PDF-1.4 month {month} price {price}".encode("ascii")


class _Body:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _size: int) -> AsyncIterator[bytes]:
        yield self._body


class _Response:
    content_length = None
    charset = None
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.content = _Body(body)

    async def text(self, errors: str = "strict") -> str:
        return self._body.decode("utf-8", errors=errors)

    async def read(self) -> bytes:
        return self._body

    async def json(self, content_type: str | None = None) -> Any:
        return json.loads(self._body)

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    """Just enough of aiohttp for the readers: one canned body per URL."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    def get(self, url: str, **_kw: Any) -> _Response:
        self.asked.append(url)
        if url not in self.pages:
            raise aiohttp.ClientConnectionError(url)
        return _Response(self.pages[url])

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> _Session:
    """The session the archiver opens, serving the synthetic supplier."""
    session = _Session({LISTING_URL: b"listing", CARD_URL: _card("2026-09", "0.08")})
    monkeypatch.setattr(ac.aiohttp, "ClientSession", lambda *_a, **_kw: session)
    return session


def _snapshot(contract: str, label: str, price: float) -> SupplierSnapshot:
    year, month = (int(part) for part in label.split("-"))
    return SupplierSnapshot(
        supplier="acme",
        contract=contract,
        energy=FixedRates(price=price, yearly_fixed_fee=50.0),
        dsos={
            "ores": DsoOverlay(
                tiers={
                    "t1": DsoTier(fixed_per_year=30.0, proportional=0.04),
                    "t2": DsoTier(fixed_per_year=140.0, proportional=0.02),
                },
                transport=0.0016,
            )
        },
        taxes=TaxOverlay(
            excise_bands=excise_bands(0.011, 0.012), connection_fee=0.00008, card_vat_rate=0.06
        ),
        source_url=CARD_URL,
        publication_label=label,
        valid_until=end_of_month(year, month),
    )


def _parse(text: str, factor: float, contract: str) -> SupplierSnapshot:
    month = re.search(r"month (\S+)", text)
    price = re.search(r"price (\S+)", text)
    if month is None or price is None:
        raise ExtractorError("Acme: card not read")
    return _snapshot(contract, month.group(1), float(price.group(1)) * factor)


class _Acme:
    """A supplier whose card is a listing page and a PDF. ``factor`` stands
    for a parser change; ``days`` records the day each parse ran on."""

    regions = frozenset({"wallonia"})

    def __init__(self, factor: float = 1.0) -> None:
        self.factor = factor
        self.renders = 0
        self.days: list[date] = []

    def render(self, payload: bytes) -> str:
        self.renders += 1
        return payload.decode("ascii")

    async def fetch(self, session: Any, contract: str, region: str) -> SupplierSnapshot:
        await _pdf.fetch_text(session, LISTING_URL)
        text = await _pdf.fetch_pdf_rendered(
            session, CARD_URL, variant="plain", timeout=30, render=self.render
        )
        self.days.append(dt_util.now().date())
        return _parse(text, self.factor, contract)

    def extractor(self, **kwargs: Any) -> SupplierExtractor:
        return SupplierExtractor(
            id="acme",
            label="Acme",
            contracts=(
                Contract(id="acme_fix", label="Acme Fix", kind="fixed", regions=self.regions),
            ),
            fetch=self.fetch,
            **kwargs,
        )


async def _no_sleep(_seconds: float) -> None:
    return None


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


async def test_a_row_is_what_the_integration_reads_for_a_past_month(
    tmp_path: Path, web: _Session
) -> None:
    """The row is the codec's snapshot with the archive's own keys beside
    it, at the path month_cards asks for, and the reader ignores the extra
    keys and hands back the very snapshot the parse produced."""
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    path = out / ROW / "2026-09.json"
    row = json.loads(path.read_text(encoding="utf-8"))

    assert row["_seen_on"] == "2026-09-11"
    assert row["_via"] == "live"
    assert [(s["url"], s["variant"]) for s in row["_sources"]] == [
        (LISTING_URL, "text"),
        (CARD_URL, "plain"),
    ]
    assert "pdf" not in row["_sources"][0]
    assert row["_sources"][1]["pdf"] == _digest(web.pages[CARD_URL])
    assert (out / row["_sources"][1]["text"]).read_bytes() == web.pages[CARD_URL]
    assert (out / row["_sources"][0]["text"]).read_text() == "listing"

    expected = _snapshot("acme_fix", "2026-09", 0.08)
    assert snapshot_from_json(row) == expected
    assert CARD_ARCHIVE_URL.endswith(f"/gas/{ac.ROWS}")
    reader = _Session(
        {f"{CARD_ARCHIVE_URL}/acme/acme_fix/wallonia/2026-09.json": path.read_bytes()}
    )
    assert await fetch_archived_card(reader, "acme", "acme_fix", "wallonia", "2026-09") == expected  # type: ignore[arg-type]


async def test_a_row_with_a_short_tier_pair_is_no_card() -> None:
    row = snapshot_to_json(_snapshot("acme_fix", "2026-09", 0.08))
    dso = next(iter(row["dsos"].values()))
    tier = next(iter(dso["tiers"]))
    dso["tiers"][tier] = dso["tiers"][tier][:1]
    reader = _Session(
        {f"{CARD_ARCHIVE_URL}/acme/acme_fix/wallonia/2026-09.json": json.dumps(row).encode()}
    )
    assert await fetch_archived_card(reader, "acme", "acme_fix", "wallonia", "2026-09") is None  # type: ignore[arg-type]


async def test_a_card_is_filed_under_the_month_its_label_names(
    tmp_path: Path, web: _Session
) -> None:
    """October's card published on 28 September is October's row."""
    web.pages[CARD_URL] = _card("2026-10", "0.08")
    out = tmp_path / "gas"
    await ac.archive(
        out,
        extractors=[_Acme().extractor()],
        now=datetime(2026, 9, 28, 6, tzinfo=UTC),
        sleep=_no_sleep,
    )
    assert [p.name for p in (out / ROW).iterdir()] == ["2026-10.json"]


async def test_a_day_on_which_nothing_changed_writes_nothing(tmp_path: Path, web: _Session) -> None:
    """The listing carries a nonce and the run is a day later, but the card
    is the same: not a byte of the archive moves, the PDF is not kept twice
    and the card, its bytes unchanged, is not rendered again."""
    web.pages[LISTING_URL] = b"listing nonce=1"
    out = tmp_path / "gas"
    acme = _Acme()
    first = await ac.archive(
        out, extractors=[acme.extractor()], now=NOW, sleep=_no_sleep, pdf_dir=tmp_path / "pdfs1"
    )
    assert first.stored == 1
    digest = _digest(web.pages[CARD_URL])
    kept = tmp_path / "pdfs1" / "gas-2026-09" / f"{digest}.pdf"
    assert kept.read_bytes() == web.pages[CARD_URL]
    # What the workflow does between two runs: upload it and record where.
    (out / "pdfs.json").write_text(json.dumps({digest: f"gas-2026-09/{digest}.pdf"}))
    before = _tree(out)

    web.pages[LISTING_URL] = b"listing nonce=2"
    second = await ac.archive(
        out,
        extractors=[acme.extractor()],
        now=NOW + timedelta(days=1),
        sleep=_no_sleep,
        pdf_dir=tmp_path / "pdfs2",
    )
    assert (second.stored, second.unchanged) == (0, 1)
    assert _tree(out) == before
    assert not (tmp_path / "pdfs2").exists()
    assert acme.renders == 1


async def test_a_changed_card_rewrites_its_month(tmp_path: Path, web: _Session) -> None:
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    web.pages[CARD_URL] = _card("2026-09", "0.09")
    later = datetime(2026, 9, 20, 6, tzinfo=UTC)
    summary = await ac.archive(out, extractors=[_Acme().extractor()], now=later, sleep=_no_sleep)
    row = json.loads((out / ROW / "2026-09.json").read_text())
    assert summary.stored == 1
    assert (row["energy"]["price"], row["_seen_on"]) == (0.09, "2026-09-20")
    # The first capture's card text is named by no row any more.
    assert len(list((out / "texts").rglob("*.txt"))) == 2


def test_months_older_than_three_years_go_with_the_texts_nothing_names(tmp_path: Path) -> None:
    out = tmp_path
    for rel in ("texts/2023-08/old.txt", "texts/2026-09/kept.txt", "texts/2026-09/nonce.txt"):
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(rel)
    for month, text in (
        ("2023-08", "texts/2023-08/old.txt"),
        ("2023-09", "texts/2026-09/kept.txt"),
    ):
        path = out / ROW / f"{month}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"_sources": [{"url": LISTING_URL, "variant": "text", "text": text}]})
        )
    (out / "pdfs.json").write_text(json.dumps({"a": "gas-2023-08/a.pdf", "b": "gas-2023-09/b.pdf"}))
    (out / "indices").mkdir()
    (out / "indices" / "acme.json").write_text(
        json.dumps({"IDX": {"2023-08": 1.0, "2023-09": 2.0}, "GONE": {"2020-01": 3.0}})
    )

    today = date(2026, 9, 29)
    assert ac._prune(out, 36, today) == 3
    assert ac._drop_unnamed_texts(out) == 2

    assert sorted(_tree(out)) == [
        f"{ROW}/2023-09.json",
        "indices/acme.json",
        "pdfs.json",
        "texts/2026-09/kept.txt",
    ]
    assert json.loads((out / "pdfs.json").read_text()) == {"b": "gas-2023-09/b.pdf"}
    assert json.loads((out / "indices" / "acme.json").read_text()) == {"IDX": {"2023-09": 2.0}}


def test_no_text_goes_while_a_row_cannot_be_read(tmp_path: Path) -> None:
    (tmp_path / ROW).mkdir(parents=True)
    (tmp_path / ROW / "2026-09.json").write_text("{not json")
    (tmp_path / "texts" / "2026-09").mkdir(parents=True)
    (tmp_path / "texts" / "2026-09" / "a.txt").write_text("a")
    assert ac._drop_unnamed_texts(tmp_path) == 0
    assert (tmp_path / "texts" / "2026-09" / "a.txt").exists()


async def test_the_index_publication_is_kept_and_merged(tmp_path: Path, web: _Session) -> None:
    """Today's values go on top of what the archive held, a revised month
    takes its new figure, a month the page no longer lists stays, and a month
    past the retention goes."""
    out = tmp_path / "gas"
    index = out / "indices" / "acme.json"
    index.parent.mkdir(parents=True)
    index.write_text(
        json.dumps({"IDX": {"2026-07": 29.0, "2026-08": 28.0}, "OLD": {"2023-01": 50.0}})
    )

    async def fetch_index(_session: Any) -> dict[str, dict[str, float]]:
        return {"IDX": {"2026-08": 30.0, "2026-09": 31.0}}

    extractor = _Acme().extractor(fetch_index=fetch_index)
    summary = await ac.archive(out, extractors=[extractor], now=NOW, sleep=_no_sleep)
    assert json.loads(index.read_text()) == {
        "IDX": {"2026-07": 29.0, "2026-08": 30.0, "2026-09": 31.0}
    }
    assert summary.indices == 1

    again = await ac.archive(out, extractors=[extractor], now=NOW, sleep=_no_sleep)
    assert again.indices == 0

    async def broken(_session: Any) -> dict[str, dict[str, float]]:
        raise ExtractorError("Acme: indexation table not found")

    held = index.read_bytes()
    failed = await ac.archive(
        out, extractors=[_Acme().extractor(fetch_index=broken)], now=NOW, sleep=_no_sleep
    )
    assert index.read_bytes() == held
    assert [line.split(":")[0] for line in failed.failed] == ["acme index"]


async def test_backfill_mirrors_the_supplier_archive_for_months_not_held(
    tmp_path: Path, web: _Session
) -> None:
    asked: list[date] = []

    async def fetch_for_month(
        _session: Any, contract: str, _region: str, month: date
    ) -> SupplierSnapshot | None:
        asked.append(month)
        if month == date(2026, 7, 1):
            return None
        return _snapshot(contract, f"{month:%Y-%m}", 0.07)

    out = tmp_path / "gas"
    held = out / ROW / "2026-06.json"
    held.parent.mkdir(parents=True)
    held.write_text("{}")
    summary = await ac.archive(
        out,
        extractors=[_Acme().extractor(fetch_for_month=fetch_for_month)],
        now=NOW,
        sleep=_no_sleep,
        backfill_months=3,
    )
    assert asked == [date(2026, 8, 1), date(2026, 7, 1)]
    assert (summary.backfilled, summary.absent) == (1, 1)
    assert json.loads((out / ROW / "2026-08.json").read_text())["_via"] == "archive"
    assert not (out / ROW / "2026-07.json").exists()
    assert held.read_text() == "{}"


async def test_backfill_files_a_card_under_the_month_it_names(
    tmp_path: Path, web: _Session
) -> None:
    """A card in force from July still answers for August. It is filed under
    July once, the month a replay asks it for again, and a card from before
    the retention is not stored only to be pruned."""
    asked: list[date] = []

    async def fetch_for_month(
        _session: Any, contract: str, _region: str, month: date
    ) -> SupplierSnapshot | None:
        asked.append(month)
        label = "2026-07" if month >= date(2026, 7, 1) else "2025-01"
        return _snapshot(contract, label, 0.07)

    out = tmp_path / "gas"
    summary = await ac.archive(
        out,
        extractors=[_Acme().extractor(fetch_for_month=fetch_for_month)],
        now=NOW,
        sleep=_no_sleep,
        keep_months=12,
        backfill_months=3,
    )
    assert asked == [date(2026, 8, 1), date(2026, 6, 1)]
    assert summary.backfilled == 1
    row = json.loads((out / ROW / "2026-07.json").read_text())
    assert (row["_via"], row["publication_label"]) == ("archive", "2026-07")
    assert not (out / ROW / "2026-08.json").exists()
    assert not (out / ROW / "2025-01.json").exists()


async def test_a_parser_change_replays_the_stored_months_on_their_capture_day(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In October a parser fix doubles what the card is read as. September's
    row is parsed again from its own stored texts, on its capture day and
    without a request, and keeps the day it was captured."""
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    web.pages[CARD_URL] = _card("2026-10", "0.09")
    fixed = _Acme(factor=2.0)
    monkeypatch.setattr(ac, "_parser_digest", lambda: "a parser that changed")

    summary = await ac.archive(
        out,
        extractors=[fixed.extractor()],
        now=datetime(2026, 10, 2, 6, tzinfo=UTC),
        sleep=_no_sleep,
    )
    september = json.loads((out / ROW / "2026-09.json").read_text())
    assert september["energy"]["price"] == pytest.approx(0.16)
    assert september["_seen_on"] == "2026-09-11"
    assert (summary.replayed, summary.reparsed) == (2, 1)
    # The live walk on the real clock, then each replay on its row's day.
    assert fixed.days[1:] == [date(2026, 9, 11), date(2026, 10, 2)]
    assert len(web.asked) == 4
    assert (out / "parser.txt").read_text().splitlines()[0] == "a parser that changed"


async def test_a_row_the_parser_now_refuses_is_removed(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser fix that refuses a kind of card it once misread: the row
    stored from the misread is removed, so no installation bills on it."""
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    assert (out / ROW / "2026-09.json").exists()

    class _Stricter(_Acme):
        async def fetch(self, session: Any, contract: str, region: str) -> SupplierSnapshot:
            await super().fetch(session, contract, region)
            raise ExtractorError("Acme: this card prints the network table of 2025")

    monkeypatch.setattr(ac, "_parser_digest", lambda: "a parser that changed")
    summary = await ac.archive(
        out,
        extractors=[_Stricter().extractor()],
        now=datetime(2026, 9, 12, 6, tzinfo=UTC),
        sleep=_no_sleep,
    )
    assert not (out / ROW / "2026-09.json").exists()
    assert summary.refused and summary.refused[0].startswith("acme/acme_fix/wallonia/2026-09")


@pytest.mark.parametrize(
    "contracts",
    [
        (),
        (Contract(id="acme_fix", label="Acme Fix", kind="fixed", regions=frozenset({"flanders"})),),
    ],
)
async def test_a_row_of_a_withdrawn_contract_is_kept(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch, contracts: tuple[Contract, ...]
) -> None:
    """The supplier withdrew the contract, or stopped selling it in the
    region: its rows still price an earlier contract on it, and are not
    taken for rows the parser refuses."""
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)

    async def refuse(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        raise ExtractorError(f"Acme {contract}: not sold in region {region!r}")

    withdrawn = replace(_Acme().extractor(), contracts=contracts, fetch=refuse)
    monkeypatch.setattr(ac, "_parser_digest", lambda: "a parser that changed")
    summary = await ac.archive(
        out, extractors=[withdrawn], now=datetime(2026, 9, 12, 6, tzinfo=UTC), sleep=_no_sleep
    )
    assert (out / ROW / "2026-09.json").exists()
    assert not summary.refused


async def test_a_row_the_parser_cannot_rebuild_offline_is_left_as_it_was(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    before = (out / ROW / "2026-09.json").read_bytes()

    class _Wider(_Acme):
        async def fetch(self, session: Any, contract: str, region: str) -> SupplierSnapshot:
            await _pdf.fetch_text(session, "https://acme.test/a-page-the-row-never-read")
            return await super().fetch(session, contract, region)

    monkeypatch.setattr(ac, "_parser_digest", lambda: "a parser that changed")
    summary = await ac.archive(
        out,
        only=frozenset({"nobody"}),
        extractors=[_Wider(factor=2.0).extractor()],
        now=NOW,
        sleep=_no_sleep,
    )
    assert (out / ROW / "2026-09.json").read_bytes() == before
    assert summary.replayed == 0
    assert len(summary.unreplayable) == 1
    assert "a replay is offline" in summary.unreplayable[0]
    assert not summary.download_failed


async def test_a_card_handed_over_inside_json_is_kept_once_and_put_back_for_a_replay(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card arrives base64 inside a JSON answer, as OCTA+'s archive
    hands it over. The stored text names the kept PDF instead of carrying it
    a second time, and a replay gets the bytes back from the release."""
    payload = _card("2026-09", "0.08") + b" " + b"x" * 600
    sheet_url = "https://acme.test/sheet?name=card"
    envelope = "data:application/pdf;base64," + base64.b64encode(payload).decode("ascii")
    web.pages[sheet_url] = json.dumps({"TariffSheet": envelope}).encode("ascii")

    class _Sheet(_Acme):
        async def fetch(self, session: Any, contract: str, region: str) -> SupplierSnapshot:
            reply = json.loads(await _pdf.fetch_text(session, sheet_url))
            card = base64.b64decode(reply["TariffSheet"].split("base64,", 1)[1])
            text = await _pdf.render_pdf("layout", sheet_url, card, self.render)
            self.days.append(dt_util.now().date())
            return _parse(text, self.factor, contract)

    out = tmp_path / "gas"
    await ac.archive(
        out, extractors=[_Sheet().extractor()], now=NOW, sleep=_no_sleep, pdf_dir=tmp_path / "pdfs"
    )
    digest = _digest(payload)
    row = json.loads((out / ROW / "2026-09.json").read_text())
    by_variant = {s["variant"]: s for s in row["_sources"]}
    assert by_variant["layout"]["pdf"] == digest
    stored = (out / by_variant["text"]["text"]).read_text()
    assert f"{{{{card:{digest}}}}}" in stored
    assert "base64," in stored and len(stored) < 200
    assert (tmp_path / "pdfs" / "gas-2026-09" / f"{digest}.pdf").read_bytes() == payload

    # A later run: the PDF is in its release now, and the parser changed.
    (out / "pdfs.json").write_text(json.dumps({digest: f"gas-2026-09/{digest}.pdf"}))
    release = "https://cards.test/releases/download"
    web.pages[f"{release}/gas-2026-09/{digest}.pdf"] = payload
    monkeypatch.setattr(ac, "_parser_digest", lambda: "a parser that changed")
    summary = await ac.archive(
        out,
        only=frozenset({"nobody"}),
        extractors=[_Sheet(factor=2.0).extractor()],
        now=datetime(2026, 10, 2, 6, tzinfo=UTC),
        sleep=_no_sleep,
        pdf_base_url=release,
    )
    assert summary.unreplayable == []
    assert (summary.replayed, summary.reparsed) == (1, 1)
    assert json.loads((out / ROW / "2026-09.json").read_text())["energy"]["price"] == pytest.approx(
        0.16
    )


async def test_a_supplier_that_does_not_answer_is_given_up_on_for_the_day(
    tmp_path: Path, web: _Session
) -> None:
    asked: list[str] = []

    async def fetch(_session: Any, contract: str, _region: str) -> SupplierSnapshot:
        asked.append(contract)
        raise ExtractorError(f"network error fetching {CARD_URL}: TimeoutError")

    extractor = SupplierExtractor(
        id="acme",
        label="Acme",
        contracts=tuple(
            Contract(id=f"acme_{n}", label="Acme", kind="fixed", regions=frozenset({"wallonia"}))
            for n in range(5)
        ),
        fetch=fetch,
    )
    summary = await ac.archive(tmp_path, extractors=[extractor], now=NOW, sleep=_no_sleep)
    # Three cards of three attempts each, then the rest of the day off.
    assert asked == ["acme_0"] * 3 + ["acme_1"] * 3 + ["acme_2"] * 3
    assert summary.given_up == ["acme"]
    assert summary.stored == summary.unchanged == 0


async def test_the_coverage_sheet_links_each_month_to_its_card_and_row(
    tmp_path: Path, web: _Session
) -> None:
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    digest = _digest(web.pages[CARD_URL])
    (out / "pdfs.json").write_text(json.dumps({digest: f"gas-2026-09/{digest}.pdf"}))
    ac._write_coverage(out, "https://cards.test/download", "https://cards.test/blob/main/gas")
    sheet = (out / "coverage" / "acme.md").read_text()
    assert (
        f"| acme_fix | wallonia | [pdf](https://cards.test/download/gas-2026-09/{digest}.pdf) "
        f"[json](https://cards.test/blob/main/gas/{ROW}/2026-09.json) |"
    ) in sheet
    assert (
        "- [acme](coverage/acme.md): 1 row, 2026-09 to 2026-09" in (out / "coverage.md").read_text()
    )


def test_main_fails_the_run_only_when_nothing_was_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter((ac._Summary(), ac._Summary(unchanged=1)))

    async def archive(_out: Path, **_kwargs: Any) -> Any:
        return next(results)

    monkeypatch.setattr(ac, "archive", archive)
    monkeypatch.setattr(sys, "argv", ["archive_cards.py", "--out", str(tmp_path)])
    assert ac.main() == 1
    assert ac.main() == 0


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _push_race(tmp_path: Path) -> tuple[Path, Path, str]:
    """The cards repository, a job's clone of it with today's gas rows, and
    the workflow's push step. Between the clone and the push, the
    electricity archive pushed a commit that rewrote the README."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("# Price cards\n")
    _git("add", "README.md", cwd=seed)
    _git("commit", "-q", "-m", "start", cwd=seed)
    _git("clone", "-q", "--bare", str(seed), str(origin), cwd=tmp_path)

    work = tmp_path / "work"
    (work / "tmp").mkdir(parents=True)
    _git("clone", "-q", "--depth=1", f"file://{origin}", "tmp/cards", cwd=work)
    other = tmp_path / "electricity"
    _git("clone", "-q", "--depth=1", f"file://{origin}", str(other), cwd=tmp_path)
    (other / "README.md").write_text("# Price cards\n\nelectricity and water\n")
    (other / "electricity").mkdir()
    (other / "electricity" / "row.json").write_text("{}\n")
    _git("add", "-A", cwd=other)
    _git("commit", "-q", "-m", "Cards seen", cwd=other)
    _git("push", "-q", "origin", "HEAD:main", cwd=other)

    cards = work / "tmp" / "cards"
    (cards / "README.md").write_text("# Price cards\n\nelectricity, gas and water\n")
    (cards / "gas").mkdir()
    (cards / "gas" / "row.json").write_text("{}\n")

    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/archive_cards.yml").read_text()
    )
    step = next(s for s in workflow["jobs"]["archive"]["steps"] if s.get("id") == "push")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "sleep").chmod(0o755)
    return origin, work, step["run"]


def _assert_landed(origin: Path) -> None:
    log = _git("log", "--format=%s", "main", cwd=origin).splitlines()
    assert log[:2] == [f"Gas cards seen on {datetime.now(UTC).date().isoformat()}", "Cards seen"]
    assert (
        _git("show", "main:README.md", cwd=origin)
        == "# Price cards\n\nelectricity, gas and water\n"
    )
    assert _git("show", "main:electricity/row.json", cwd=origin) == "{}\n"
    assert _git("show", "main:gas/row.json", cwd=origin) == "{}\n"


def test_the_push_survives_another_archive_rewriting_the_readme(tmp_path: Path) -> None:
    """The electricity and water workflows push to the same main and write
    the root README too, with the same text. Should one of them drift and
    land between this job's clone and its push, README included, the run
    must not be lost: the push step rebases, keeps this job's README, which
    lists every namespace, and lands."""
    origin, work, run = _push_race(tmp_path)
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    done = subprocess.run(
        ["bash", "-e", "-c", run], cwd=work, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    _assert_landed(origin)


def test_the_push_survives_a_pull_that_fails(tmp_path: Path) -> None:
    """A network blip on the first pull is left to the next attempt."""
    origin, work, run = _push_race(tmp_path)
    real = shutil.which("git")
    assert real is not None
    stub = tmp_path / "bin" / "git"
    stub.write_text(
        "#!/bin/sh\n"
        'case " $* " in *" pull "*)\n'
        f'  if [ ! -e "{tmp_path}/pulled" ]; then touch "{tmp_path}/pulled"; exit 128; fi ;;\n'
        "esac\n"
        f'exec "{real}" "$@"\n'
    )
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    done = subprocess.run(
        ["bash", "-e", "-c", run], cwd=work, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "pulled").exists()
    _assert_landed(origin)


class _ImageAcme(_Acme):
    """Acme publishing its card as page images: no text reader reads it."""

    def render(self, payload: bytes) -> str:
        self.renders += 1
        raise CardNotReadableError("card has no text layer")


async def test_a_card_published_as_images_is_read_by_the_ocr_engine(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine reads what the text readers refuse, the row says so, and
    the reading is served again until another engine is installed."""
    out = tmp_path / "gas"
    read = []

    def ocr(payload: bytes) -> str:
        read.append(payload)
        return "month 2026-09 price 0.09"

    monkeypatch.setattr(ac, "_ocr_text", ocr)
    monkeypatch.setattr(ac, "engine_version", lambda: "0.3.0+aaaaaaaaaaaa")
    await ac.archive(out, extractors=[_ImageAcme().extractor()], now=NOW, sleep=_no_sleep)
    path = out / ROW / "2026-09.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    card = next(s for s in row["_sources"] if s["url"] == CARD_URL)
    assert card["ocr"] == "0.3.0+aaaaaaaaaaaa"
    assert snapshot_from_json(row) == _snapshot("acme_fix", "2026-09", 0.09)
    assert len(read) == 1
    # What an installation reads: the row, and that it was read off an image.
    reader = _Session(
        {f"{CARD_ARCHIVE_URL}/acme/acme_fix/wallonia/2026-09.json": path.read_bytes()}
    )
    got = await fetch_archived_row(reader, "acme", "acme_fix", "wallonia", "2026-09")  # type: ignore[arg-type]
    assert got == (_snapshot("acme_fix", "2026-09", 0.09), True)

    # The same bytes the next day: the stored reading, not the engine.
    await ac.archive(out, extractors=[_ImageAcme().extractor()], now=NOW, sleep=_no_sleep)
    assert len(read) == 1
    # A new engine reads the card again.
    monkeypatch.setattr(ac, "engine_version", lambda: "0.3.0+bbbbbbbbbbbb")
    await ac.archive(out, extractors=[_ImageAcme().extractor()], now=NOW, sleep=_no_sleep)
    assert len(read) == 2
    row = json.loads(path.read_text(encoding="utf-8"))
    assert next(s for s in row["_sources"] if s["url"] == CARD_URL)["ocr"] == "0.3.0+bbbbbbbbbbbb"


async def test_a_card_the_ocr_cannot_read_is_listed_for_the_workflow(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reading that misses a figure, or no reading at all: the walk still
    succeeds on the other cards, so these are what the workflow files."""
    out = tmp_path / "gas"
    monkeypatch.setattr(ac, "engine_version", lambda: "0.3.0+aaaaaaaaaaaa")
    # The engine refused the marks of the price line.
    monkeypatch.setattr(ac, "_ocr_text", lambda payload: "month 2026-09")
    summary = await ac.archive(out, extractors=[_ImageAcme().extractor()], now=NOW, sleep=_no_sleep)
    assert summary.ocr_failed == summary.failed
    assert summary.ocr_failed[0].startswith("acme/acme_fix/wallonia: ExtractorError")

    def refuse(payload: bytes) -> str:
        raise ac.CardNotReadableError("OCR could not read the card: no glyph")

    monkeypatch.setattr(ac, "_ocr_text", refuse)
    summary = await ac.archive(out, extractors=[_ImageAcme().extractor()], now=NOW, sleep=_no_sleep)
    assert summary.ocr_failed == summary.failed
    assert "CardNotReadableError" in summary.ocr_failed[0]
    # A card read off its text layer that fails is not the OCR's.
    web.pages[CARD_URL] = b"%PDF-1.4 month 2026-09"
    summary = await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    assert summary.failed and not summary.ocr_failed


class _ImageAcmeTwoRegions(_ImageAcme):
    """One card, read for both regions: the second is served the first's
    reading from the run's memo."""

    regions = frozenset({"wallonia", "flanders"})


async def test_an_ocr_reading_served_from_the_memo_is_listed_too(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ac, "engine_version", lambda: "0.3.0+aaaaaaaaaaaa")
    read: list[bytes] = []

    def ocr(payload: bytes) -> str:
        read.append(payload)
        return "month 2026-09"

    monkeypatch.setattr(ac, "_ocr_text", ocr)
    acme = _ImageAcmeTwoRegions()
    summary = await ac.archive(
        tmp_path / "gas", extractors=[acme.extractor()], now=NOW, sleep=_no_sleep
    )
    assert len(read) == 1
    assert len(summary.failed) == 2
    assert summary.ocr_failed == summary.failed


def test_main_writes_the_ocr_failures_one_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter(
        (
            ac._Summary(stored=1, ocr_failed=["ecofix/flexy/flanders: ExtractorError: a\nb"]),
            ac._Summary(stored=1),
        )
    )

    async def archive(_out: Path, **_kwargs: Any) -> Any:
        return next(results)

    report = tmp_path / "ocr_failed.txt"
    monkeypatch.setattr(ac, "archive", archive)
    argv = ["archive_cards.py", "--out", str(tmp_path), "--ocr-failures", str(report)]
    monkeypatch.setattr(sys, "argv", argv)
    assert ac.main() == 0
    assert report.read_text() == "ecofix/flexy/flanders: ExtractorError: a b\n"
    assert ac.main() == 0
    assert report.read_text() == ""


async def test_a_new_engine_reads_a_month_no_longer_downloaded_again(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In October a new engine is installed and nothing else changed:
    September's card, read off page images, is read again from its kept
    bytes, and the row names the engine that read it."""
    out = tmp_path / "gas"
    pdfs = tmp_path / "pdfs"
    read: list[bytes] = []

    def ocr(misread: str) -> Callable[[bytes], str]:
        def read_card(payload: bytes) -> str:
            read.append(payload)
            return payload.decode("ascii").replace("0.08", misread)

        return read_card

    def no_text_layer(payload: bytes) -> str:
        raise CardNotReadableError("card has no text layer")

    monkeypatch.setitem(ac._RENDERERS, "plain", no_text_layer)
    monkeypatch.setattr(ac, "_ocr_text", ocr("0.03"))
    monkeypatch.setattr(ac, "engine_version", lambda: "0.3.0+aaaaaaaaaaaa")
    await ac.archive(
        out, extractors=[_ImageAcme().extractor()], now=NOW, sleep=_no_sleep, pdf_dir=pdfs
    )
    september = out / ROW / "2026-09.json"
    assert json.loads(september.read_text(encoding="utf-8"))["energy"]["price"] == 0.03
    web.pages[CARD_URL] = _card("2026-10", "0.10")
    monkeypatch.setattr(ac, "_ocr_text", ocr("0.08"))
    monkeypatch.setattr(ac, "engine_version", lambda: "0.3.0+bbbbbbbbbbbb")
    summary = await ac.archive(
        out,
        extractors=[_ImageAcme().extractor()],
        now=datetime(2026, 10, 2, 6, tzinfo=UTC),
        sleep=_no_sleep,
        pdf_dir=pdfs,
    )
    assert (summary.replayed, summary.reparsed) == (2, 1)
    assert read[1:] == [_card("2026-10", "0.10"), _card("2026-09", "0.08")]
    row = json.loads(september.read_text(encoding="utf-8"))
    assert row["energy"]["price"] == 0.08
    card = next(s for s in row["_sources"] if s["url"] == CARD_URL)
    assert card["ocr"] == "0.3.0+bbbbbbbbbbbb"
    # The next run holds both readings and replays nothing.
    summary = await ac.archive(
        out,
        extractors=[_ImageAcme().extractor()],
        now=datetime(2026, 10, 3, 6, tzinfo=UTC),
        sleep=_no_sleep,
        pdf_dir=pdfs,
    )
    assert (summary.replayed, len(read)) == (0, 3)


class _NewReadersAcme(_Acme):
    """Acme read by a pypdf release that lays its card out otherwise."""

    def render(self, payload: bytes) -> str:
        self.renders += 1
        return payload.decode("ascii").replace("price", "p r i c e")


async def test_new_readers_render_the_card_again_on_every_run(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored text is served only to the readers that rendered it, so a
    card the new readers break keeps failing, in the archive and in the live
    check, rather than being seen failing once and then read off the old
    readers' text."""
    out = tmp_path / "gas"

    def readers(line: str) -> None:
        monkeypatch.setattr(card_texts, "readers_line", lambda: line)
        monkeypatch.setattr(ac, "readers_line", lambda: line)

    readers("pypdf==1 pdfplumber==1")
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    row = json.loads((out / ROW / "2026-09.json").read_text(encoding="utf-8"))
    card = next(s for s in row["_sources"] if s["url"] == CARD_URL)
    assert card["readers"] == "pypdf==1 pdfplumber==1"
    assert card_texts.StoredTexts(out).texts
    readers("pypdf==2 pdfplumber==1")
    for _ in range(2):
        acme = _NewReadersAcme()
        summary = await ac.archive(out, extractors=[acme.extractor()], now=NOW, sleep=_no_sleep)
        assert acme.renders == 1
        assert summary.failed
    assert card_texts.StoredTexts(out).texts == {}


async def test_a_reader_upgrade_reaches_a_month_no_longer_downloaded(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pypdf release and nothing else: the replay it starts renders a
    closed month's card again from its kept bytes."""
    out = tmp_path / "gas"
    pdfs = tmp_path / "pdfs"

    def readers(line: str) -> None:
        monkeypatch.setattr(card_texts, "readers_line", lambda: line)
        monkeypatch.setattr(ac, "readers_line", lambda: line)

    readers("pypdf==1 pdfplumber==1")
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep, pdf_dir=pdfs)
    web.pages[CARD_URL] = _card("2026-10", "0.09")
    readers("pypdf==2 pdfplumber==1")
    upgraded = _Acme()
    monkeypatch.setitem(ac._RENDERERS, "plain", upgraded.render)
    summary = await ac.archive(
        out,
        extractors=[_Acme().extractor()],
        now=datetime(2026, 10, 11, 6, 0, tzinfo=UTC),
        sleep=_no_sleep,
        pdf_dir=pdfs,
    )
    row = json.loads((out / ROW / "2026-09.json").read_text())
    assert (summary.replayed, upgraded.renders) == (2, 1)
    assert [s["readers"] for s in row["_sources"] if "pdf" in s] == ["pypdf==2 pdfplumber==1"]


def test_the_engine_moves_with_the_libraries_it_reads_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """numpy and pypdfium2 are installed unpinned beside the engine, so a
    new release of either reads the cards again like a new engine."""

    class _Installed:
        def read_text(self, name: str) -> str:
            return '{"vcs_info": {"commit_id": "a5c8a17ccad7b3e4"}}'

    versions = {"ocr-price-cards": "0.4.2", "pypdfium2": "5.14.0", "numpy": "2.5.3"}
    versions["pdfplumber"] = "0.11.9"
    monkeypatch.setattr(card_texts, "_version", versions.__getitem__)
    monkeypatch.setattr(card_texts.importlib.metadata, "distribution", lambda name: _Installed())
    before = card_texts.engine_version()
    assert before.startswith("0.4.2+a5c8a17ccad7 ")
    versions["numpy"] = "2.6.0"
    assert card_texts.engine_version() != before


async def test_a_render_fix_renders_the_card_again(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored text is served only to the render code that made it, so a
    fix to how a card is rendered reaches a card whose bytes the archive
    already holds, and the next run serves what the fix rendered."""
    out = tmp_path / "gas"
    code = tmp_path / "_pdf.py"
    code.write_text("one render\n", encoding="utf-8")
    monkeypatch.setattr(card_texts, "_RENDER_CODE", code)
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    code.write_text("a fixed render\n", encoding="utf-8")
    for renders in (1, 0):
        acme = _Acme()
        await ac.archive(out, extractors=[acme.extractor()], now=NOW, sleep=_no_sleep)
        assert acme.renders == renders


async def test_a_render_fix_reaches_a_month_no_longer_downloaded(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replay renders a closed month's card again from its kept bytes
    when other render code made its text, and parses what the fix reads."""
    out = tmp_path / "gas"
    code = tmp_path / "_pdf.py"
    code.write_text("one render\n", encoding="utf-8")
    monkeypatch.setattr(card_texts, "_RENDER_CODE", code)
    await ac.archive(
        out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep, pdf_dir=tmp_path / "pdfs"
    )
    # A month on, the render code is fixed to read the price it misread.
    web.pages[CARD_URL] = _card("2026-10", "0.09")
    code.write_text("a fixed render\n", encoding="utf-8")
    fixed = _Acme()
    monkeypatch.setitem(
        ac._RENDERERS, "plain", lambda payload: fixed.render(payload).replace("0.08", "0.07")
    )
    summary = await ac.archive(
        out,
        extractors=[_Acme().extractor()],
        now=datetime(2026, 10, 11, 6, 0, tzinfo=UTC),
        sleep=_no_sleep,
        pdf_dir=tmp_path / "pdfs",
        reparse=True,
    )
    row = json.loads((out / ROW / "2026-09.json").read_text())
    assert (summary.reparsed, fixed.renders) == (1, 1)
    assert row["energy"]["price"] == 0.07
    assert [s["readers"] for s in row["_sources"] if "pdf" in s] == [card_texts.readers_line()]


async def test_a_card_rendered_again_for_a_sibling_row_names_the_current_code(
    tmp_path: Path, web: _Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replayed row whose card another row already holds a current text of
    is served that text, and names the render code that made it from then on
    rather than the code it was first read with."""

    class _TwoRegions(_Acme):
        regions = frozenset({"wallonia", "flanders"})

    out = tmp_path / "gas"
    code = tmp_path / "_pdf.py"
    code.write_text("one render\n", encoding="utf-8")
    monkeypatch.setattr(card_texts, "_RENDER_CODE", code)
    await ac.archive(
        out,
        extractors=[_TwoRegions().extractor()],
        now=NOW,
        sleep=_no_sleep,
        pdf_dir=tmp_path / "pdfs",
    )
    # The flanders row was replayed under the fixed code on a day the
    # wallonia one could not get its card back.
    code.write_text("a fixed render\n", encoding="utf-8")
    flanders = out / "cards/acme/acme_fix/flanders/2026-09.json"
    row = json.loads(flanders.read_text())
    for source in row["_sources"]:
        if "pdf" in source:
            source["readers"] = card_texts.readers_line()
    flanders.write_text(json.dumps(row))
    web.pages[CARD_URL] = _card("2026-10", "0.09")
    await ac.archive(
        out,
        extractors=[_TwoRegions().extractor()],
        now=datetime(2026, 10, 11, 6, 0, tzinfo=UTC),
        sleep=_no_sleep,
        pdf_dir=tmp_path / "pdfs",
        reparse=True,
    )
    row = json.loads((out / ROW / "2026-09.json").read_text())
    assert [s["readers"] for s in row["_sources"] if "pdf" in s] == [card_texts.readers_line()]


async def test_a_card_read_by_its_text_layer_is_not_marked(tmp_path: Path, web: _Session) -> None:
    out = tmp_path / "gas"
    await ac.archive(out, extractors=[_Acme().extractor()], now=NOW, sleep=_no_sleep)
    path = out / ROW / "2026-09.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    assert all("ocr" not in source for source in row["_sources"])
    reader = _Session(
        {f"{CARD_ARCHIVE_URL}/acme/acme_fix/wallonia/2026-09.json": path.read_bytes()}
    )
    got = await fetch_archived_row(reader, "acme", "acme_fix", "wallonia", "2026-09")  # type: ignore[arg-type]
    assert got is not None and got[1] is False
