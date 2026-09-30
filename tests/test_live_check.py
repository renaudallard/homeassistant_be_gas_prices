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

"""scripts/live_check.py: the daily check of every registered card.

The snapshots are synthetic and their figures round numbers, chosen to show
one rule each; none of them is a regulator's figure.
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from custom_components.be_gas_prices.providers._network import excise_bands
from custom_components.be_gas_prices.providers._rates import Contract, FixedRates, IndexedRates
from custom_components.be_gas_prices.providers.base import (
    CardNotReadableError,
    DsoOverlay,
    DsoTier,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
import live_check as lc  # type: ignore[import-not-found]

TODAY = date(2026, 9, 29)
Fetch = Callable[[Any, str, str], Awaitable[SupplierSnapshot]]


def _snapshot(
    supplier: str,
    *,
    label: str = "2026-09",
    t1_prop: float = 0.0227,
    transport: float = 0.00165,
    metering: float = 18.5,
    excise: tuple[float, float | None] = (0.0109, 0.0118),
    contribution: float = 0.0,
    connection_fee: float = 0.0,
    osp: dict[str, float] | None = None,
    energy: Any = None,
) -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier=supplier,
        contract=f"{supplier}_fix",
        energy=energy or FixedRates(price=0.08, yearly_fixed_fee=50.0),
        dsos={
            "fluvius_kempen": DsoOverlay(
                tiers={
                    "t1": DsoTier(fixed_per_year=16.0, proportional=t1_prop),
                    "t2": DsoTier(fixed_per_year=86.0, proportional=0.0088),
                },
                transport=transport,
                metering_per_year=metering,
            )
        },
        taxes=TaxOverlay(
            excise_bands=excise_bands(*excise),
            energy_contribution=contribution,
            connection_fee=connection_fee,
            osp_by_caliber=osp,
        ),
        source_url=f"https://{supplier}.test/card.pdf",
        publication_label=label,
    )


def _extractor(
    sid: str,
    fetch: Fetch | None = None,
    *,
    module: str | None = None,
    regions: frozenset[str] = frozenset({"flanders"}),
    **kwargs: Any,
) -> SupplierExtractor:
    """A supplier whose fetch lives in its own module, as each real one does;
    ``module`` puts several on one, as the brands of one platform are."""

    async def default(_session: Any, _contract: str, _region: str) -> SupplierSnapshot:
        return _snapshot(sid)

    chosen = fetch or default

    async def fetched(session: Any, contract: str, region: str) -> SupplierSnapshot:
        return await chosen(session, contract, region)

    fetched.__module__ = module or f"providers.{sid}"
    return SupplierExtractor(
        id=sid,
        label=sid,
        contracts=(Contract(id=f"{sid}_fix", label=sid, kind="fixed", regions=regions),),
        fetch=fetched,
        **kwargs,
    )


def _card(snapshot: SupplierSnapshot, region: str = "flanders", module: str | None = None) -> Any:
    return lc.Card(
        _extractor(snapshot.supplier, module=module), f"{snapshot.supplier}_fix", region, snapshot
    )


def _failing(checks: list[Any]) -> dict[str, str]:
    return {c.label: c.detail for c in checks if c.status == "fail"}


def test_a_figure_rounded_to_fewer_digits_is_the_same_figure() -> None:
    kwh, year = "EUR/kWh", "EUR/year"
    assert lc.agree(0.0227, 0.0227489, kwh)
    assert lc.agree(0.02275, 0.0227489, kwh)
    assert lc.agree(0.00165, 0.0016536, kwh)
    assert not lc.agree(0.0212, 0.0227489, kwh)
    assert not lc.agree(12.54, 12.59, year)
    assert not lc.agree(0.000075, 0.00075, kwh)
    # Zero is exact: a levy left at zero is not a levy rounded away.
    assert not lc.agree(0.0, 0.000075, kwh)
    assert lc.agree(0.0, 0.0, kwh)


def test_a_trailing_zero_lost_in_reading_does_not_widen_a_figure() -> None:
    """ "2,20" reads back as 0.022, which looks like a figure printed to a
    tenth of a cent; it was printed to a hundredth, and 2,115 is not it."""
    assert lc.agree(0.022, 0.02206, "EUR/kWh")
    assert not lc.agree(0.022, 0.02115, "EUR/kWh")
    assert not lc.agree(18.0, 18.92, "EUR/year")
    cards = [
        _card(_snapshot("alpha", t1_prop=0.0220)),
        _card(_snapshot("beta", t1_prop=0.02206)),
        _card(_snapshot("gamma", t1_prop=0.0221)),
        _card(_snapshot("delta", t1_prop=0.02115)),
    ]
    assert list(_failing(lc.consensus(cards))) == [
        "delta: fluvius_kempen T1 proportional for 2026-09"
    ]


async def _no_sleep(_seconds: float) -> None:
    return None


def test_the_card_that_departs_from_the_fleet_is_reported() -> None:
    cards = [
        _card(_snapshot("alpha", t1_prop=0.0227489)),
        _card(_snapshot("beta", t1_prop=0.0227)),
        _card(_snapshot("gamma", t1_prop=0.02275)),
        _card(_snapshot("delta", t1_prop=0.0212)),
    ]
    failing = _failing(lc.consensus(cards))
    assert list(failing) == ["delta: fluvius_kempen T1 proportional for 2026-09"]
    assert failing["delta: fluvius_kempen T1 proportional for 2026-09"] == (
        "prints 2.12 c EUR/kWh on delta_fix/flanders where alpha, beta, gamma print "
        "2.27489 c EUR/kWh"
    )


def test_two_against_two_is_no_verdict() -> None:
    cards = [
        _card(_snapshot("alpha", metering=18.5)),
        _card(_snapshot("beta", metering=18.5)),
        _card(_snapshot("gamma", metering=19.5)),
        _card(_snapshot("delta", metering=19.5)),
    ]
    checks = lc.consensus(cards)
    assert _failing(checks) == {}
    notices = {c.label: c.detail for c in checks if c.status == "notice"}
    assert notices == {
        "fleet: fluvius_kempen metering for 2026-09": (
            "no majority: 18.5 EUR/year on alpha, beta; 19.5 EUR/year on delta, gamma"
        )
    }


def test_brands_on_one_template_count_once() -> None:
    """Three brands read by one module print one wrong figure; the two
    suppliers reading their own cards are the majority."""
    cards = [
        _card(_snapshot("alpha")),
        _card(_snapshot("beta")),
        *(
            _card(_snapshot(brand, metering=17.0), module="providers.platform")
            for brand in ("b1", "b2", "b3")
        ),
    ]
    assert sorted(_failing(lc.consensus(cards))) == [
        f"{brand}: fluvius_kempen metering for 2026-09" for brand in ("b1", "b2", "b3")
    ]


def test_a_federal_levy_the_law_sets_for_the_month_is_a_notice() -> None:
    """September 2026 is inside the excise window of const.py, so a stale
    excise is not billed and does not fail; February 2027 is past it, and
    the card's figure would be billed."""

    def fleet(label: str) -> list[Any]:
        return [
            _card(_snapshot("alpha", label=label)),
            _card(_snapshot("beta", label=label)),
            _card(_snapshot("gamma", label=label, excise=(0.0087, 0.0095), contribution=0.0011)),
        ]

    inside = lc.consensus(fleet("2026-09"))
    assert _failing(inside) == {}
    assert {c.label for c in inside if c.status == "notice"} == {
        "gamma: federal excise up to 12000 kWh for 2026-09",
        "gamma: federal excise above 12000 kWh for 2026-09",
        "gamma: federal energy contribution for 2026-09",
    }
    outside = lc.consensus(fleet("2027-02"))
    assert sorted(_failing(outside)) == [
        "gamma: federal excise above 12000 kWh for 2027-02",
        "gamma: federal excise up to 12000 kWh for 2027-02",
    ]
    # The contribution stays zero by law from August 2026 on.
    assert [c.status for c in outside if "contribution" in c.label] == ["notice"]


def test_a_fluvius_card_without_data_management_is_a_notice_in_its_year() -> None:
    """The integration bills the regulated fee where a Fluvius row prints
    none, for the tariff year const.py knows; a wrong fee that is printed is
    billed, and so is a missing one past that year."""

    def fleet(label: str, metering: float) -> list[Any]:
        return [
            _card(_snapshot("alpha", label=label)),
            _card(_snapshot("beta", label=label)),
            _card(_snapshot("gamma", label=label, metering=metering)),
        ]

    inside = lc.consensus(fleet("2026-09", 0.0))
    assert _failing(inside) == {}
    assert [(c.label, c.status) for c in inside] == [
        ("gamma: fluvius_kempen metering for 2026-09", "notice")
    ]
    assert list(_failing(lc.consensus(fleet("2026-09", 17.0)))) == [
        "gamma: fluvius_kempen metering for 2026-09"
    ]
    assert list(_failing(lc.consensus(fleet("2027-02", 0.0)))) == [
        "gamma: fluvius_kempen metering for 2027-02"
    ]


def test_a_single_excise_rate_stands_for_both_slices() -> None:
    cards = [
        _card(_snapshot("alpha", label="2027-02")),
        _card(_snapshot("beta", label="2027-02")),
        _card(_snapshot("gamma", label="2027-02", excise=(0.0109, None))),
    ]
    assert list(_failing(lc.consensus(cards))) == [
        "gamma: federal excise above 12000 kWh for 2027-02"
    ]


def test_regional_levies_are_compared_among_their_region_s_cards() -> None:
    """The Walloon connection fee is billed from the law in the months
    const.py knows it for, so a departure there is a notice until then."""

    def fleet(label: str) -> list[Any]:
        osp = {"q10_le5000": 3.51, "q10_gt5000": 12.51}
        return [
            _card(_snapshot("alpha", label=label, connection_fee=0.000075), "wallonia"),
            _card(_snapshot("beta", label=label, connection_fee=0.000075), "wallonia"),
            _card(_snapshot("gamma", label=label, connection_fee=0.00075), "wallonia"),
            # A Flemish card carries no connection fee and does not vote.
            _card(_snapshot("delta", label=label)),
            _card(_snapshot("alpha", label=label, osp=osp), "brussels"),
            _card(_snapshot("beta", label=label, osp=osp), "brussels"),
            _card(_snapshot("gamma", label=label, osp={**osp, "q10_gt5000": 12.56}), "brussels"),
        ]

    inside = lc.consensus(fleet("2026-09"))
    assert sorted(_failing(inside)) == ["gamma: brussels levy q10_gt5000 for 2026-09"]
    assert [c.label for c in inside if c.status == "notice"] == [
        "gamma: wallonia connection fee for 2026-09"
    ]
    assert "gamma: wallonia connection fee for 2027-02" in _failing(lc.consensus(fleet("2027-02")))


def test_cards_of_another_month_or_basis_do_not_vote() -> None:
    cards = [
        _card(_snapshot("alpha")),
        _card(_snapshot("beta")),
        _card(_snapshot("gamma", label="2026-10", metering=19.5)),
        _card(
            replace(
                _snapshot("delta", metering=17.0),
                taxes=replace(_snapshot("delta").taxes, vat_rate=0.21),
            )
        ),
    ]
    assert _failing(lc.consensus(cards)) == {}


@pytest.mark.parametrize(
    ("label", "today", "status"),
    [
        ("2026-09", date(2026, 9, 29), "ok"),
        ("2026-10", date(2026, 9, 29), "ok"),
        ("2026-08", date(2026, 9, 5), "ok"),
        ("2026-08", date(2026, 9, 6), "fail"),
        ("2026-07", date(2026, 9, 2), "fail"),
        ("September", date(2026, 9, 29), "fail"),
    ],
)
def test_a_card_must_be_for_the_current_month(label: str, today: date, status: str) -> None:
    """A card still labelled for last month is a supplier publishing late in
    the first five days, and stale after them."""
    check = lc._freshness("acme/acme_fix/flanders", _snapshot("acme", label=label), today)
    assert (check.label, check.status) == ("acme/acme_fix/flanders: card month", status)


def _indexed(index: str, period: str = "month") -> IndexedRates:
    return IndexedRates(factor=0.001, base=0.01, index=index, price=0.05, period=period)  # type: ignore[arg-type]


async def test_the_index_publication_must_list_what_the_cards_are_priced_on() -> None:
    async def fetch(_session: Any, _contract: str, _region: str) -> SupplierSnapshot:
        return _snapshot("acme", energy=_indexed("ZTP M"))

    tables = iter(
        (
            {"ZTP M": {"2026-08": 30.0}, "RETIRED": {"2019-01": 20.0}},
            {"ZTP M": {"2026-06": 30.0}},
            {"TTF M": {"2026-08": 30.0}},
            {},
        )
    )

    async def fetch_index(_session: Any) -> dict[str, dict[str, float]]:
        return next(tables)

    extractor = _extractor("acme", fetch, fetch_index=fetch_index)

    async def index_checks() -> dict[str, tuple[str, str]]:
        checks = await lc.check_fleet(None, [extractor], TODAY, sleep=_no_sleep)
        return {c.label: (c.status, c.detail) for c in checks if "index" in c.label}

    assert await index_checks() == {
        "acme: index publication": ("ok", "RETIRED, ZTP M"),
        "acme: index ZTP M": ("ok", "2026-08: 30 EUR/MWh"),
    }
    assert (await index_checks())["acme: index ZTP M"][0] == "fail"
    assert (await index_checks())["acme: index ZTP M"] == (
        "fail",
        "the cards are priced on ZTP M, which the publication does not list",
    )
    assert (await index_checks())["acme: index publication"] == (
        "fail",
        "the publication lists no value",
    )


def test_a_quarterly_index_may_lag_its_quarter() -> None:
    extractor = _extractor("acme")
    card = lc.Card(
        extractor, "acme_fix", "flanders", _snapshot("acme", energy=_indexed("Q", "quarter"))
    )
    checks = list(lc._index_checks(extractor, {"Q": {"2026-06": 30.0}}, [card], date(2026, 10, 2)))
    assert [c.status for c in checks] == ["ok", "ok"]


async def test_failures_are_told_apart_and_set_the_exit_code(tmp_path: Path) -> None:
    """A card that cannot be read fails the run and is filed; one that did
    not answer is reported and never filed; the fingerprint is the failing
    labels alone."""
    attempts: list[str] = []

    def failing(message: str) -> Fetch:
        async def fetch(_session: Any, contract: str, _region: str) -> SupplierSnapshot:
            attempts.append(contract)
            raise ExtractorError(message)

        return fetch

    healthy = [_extractor("alpha"), _extractor("beta")]
    down = _extractor(
        "slow", failing("network error fetching https://slow.test/card.pdf: TimeoutError")
    )
    gone = _extractor("gone", failing("HTTP 404 fetching https://gone.test/card.pdf"))

    checks = await lc.check_fleet(None, [*healthy, down], TODAY, sleep=_no_sleep)
    assert lc.exit_code(checks) == 2
    assert lc.render_fingerprint(checks) == ""
    assert attempts == ["slow_fix"] * 3

    checks = await lc.check_fleet(None, [*healthy, down, gone], TODAY, sleep=_no_sleep)
    assert lc.exit_code(checks) == 1
    assert sorted(attempts[3:]) == ["gone_fix"] + ["slow_fix"] * 3
    assert lc.render_fingerprint(checks) == "gone/gone_fix/flanders: fetch\n"
    report = lc.render_report(checks)
    assert "## Failures" in report and "## Transient failures" in report
    assert "| `slow/slow_fix/flanders: fetch` | ExtractorError: network error fetching" in report

    assert lc.exit_code(await lc.check_fleet(None, healthy, TODAY, sleep=_no_sleep)) == 0


async def test_a_card_published_as_images_without_a_reading_is_a_notice() -> None:
    """Ecofix's card before the archive has read today's bytes: nothing here
    can read it, so it is reported and not filed."""

    async def images(_session: Any, _contract: str, _region: str) -> SupplierSnapshot:
        raise CardNotReadableError("card has no text layer: 60 characters across 2 page(s)")

    checks = await lc.check_fleet(None, [_extractor("pictures", images)], TODAY, sleep=_no_sleep)
    assert [(c.label, c.status) for c in checks] == [
        ("pictures/pictures_fix/flanders: fetch", "notice")
    ]
    assert lc.exit_code(checks) == 0
    assert lc.render_fingerprint(checks) == ""


async def test_the_custom_and_withdrawn_suppliers_are_not_checked() -> None:
    withdrawn = _extractor("old", deprecated_until=date(2026, 8, 31))
    custom = _extractor("custom")
    assert lc.targets([withdrawn, custom], TODAY) == []
    assert await lc.check_fleet(None, [withdrawn, custom], TODAY, sleep=_no_sleep) == []


def test_main_writes_the_fingerprint_and_returns_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def check_fleet(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [lc.Check("gone/gone_fix/flanders: fetch", "fail", "HTTP 404")]

    monkeypatch.setattr(lc, "check_fleet", check_fleet)
    fingerprint = tmp_path / "failures.txt"
    monkeypatch.setattr(sys, "argv", ["live_check.py", "--fingerprint", str(fingerprint)])
    assert lc.main() == 1
    assert fingerprint.read_text() == "gone/gone_fix/flanders: fetch\n"
    assert capsys.readouterr().out.startswith(
        "# Live check: 0 pass, 1 fail, 0 transient, 0 notices"
    )

    async def crash(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("a bug in the check")

    monkeypatch.setattr(lc, "check_fleet", crash)
    assert lc.main() == 3
