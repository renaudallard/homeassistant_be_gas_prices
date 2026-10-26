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

import asyncio
import os
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from custom_components.be_gas_prices import excise_law, fluvius, tarbel
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
from card_texts import in_daemon_thread  # type: ignore[import-not-found]

TODAY = date(2026, 9, 29)
Fetch = Callable[[Any, str, str], Awaitable[SupplierSnapshot]]


@pytest.fixture(autouse=True)
def _synthetic_fluvius_fee() -> None:
    """The sheet's fee the synthetic cards print, so the fleet is measured
    against it as against the real one."""
    fluvius._cache.clear()
    fluvius._cache[2026] = 18.5 / 1.06


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
    # Any date: only a card without one is checked differently.
    valid_until: date | None = date(2026, 9, 30),
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
        valid_until=valid_until,
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


def test_a_dso_row_without_t2_fails_unless_documented() -> None:
    """Most households are on T2: a row the parser could not give one leaves
    them unpriced, which no fleet figure would show."""
    snapshot = _snapshot("dots")
    row = snapshot.dsos["fluvius_kempen"]
    cut = replace(
        snapshot,
        dsos={"fluvius_kempen": replace(row, tiers={"t1": row.tiers["t1"]})},
    )
    today = date(2026, 10, 19)
    assert lc._tiers("dots/x/flanders", _extractor("dots"), snapshot, today).status == "ok"
    check = lc._tiers("dots/x/flanders", _extractor("dots"), cut, today)
    assert (check.status, check.detail) == ("fail", "fluvius_kempen: no T2 term read")
    limburg = replace(cut, dsos={"fluvius_limburg": cut.dsos["fluvius_kempen"]})
    assert lc._tiers("dots/x/flanders", _extractor("dots"), limburg, today).status == "notice"
    lapsed = lc._tiers("dots/x/flanders", _extractor("dots"), limburg, date(2027, 1, 1))
    assert lapsed.status == "fail"
    assert lc._tiers("x/x/flanders", _extractor("acme"), limburg, today).status == "fail"


def test_a_card_printing_no_gas_excise_gives_no_excise_figure() -> None:
    """EnergyVision's Walloon fixed card prints the electricity excise in
    place of the gas one, which is not read: nothing to measure."""
    snapshot = _snapshot("gamma")
    card = _card(replace(snapshot, taxes=replace(snapshot.taxes, excise_bands=())))
    names = {figure.name for figure in lc.figures(card)}
    assert "transport" in names
    assert not any("excise" in name for name in names)


def test_the_social_tariff_gives_the_fleet_no_figure() -> None:
    """Its network part is the CREG's flat one and its excise the protected
    customer's: counted with the operators' terms, every one would be a
    departure."""
    assert list(lc.figures(_card(_snapshot("social")))) == []


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


def test_a_coarse_figure_neither_hides_a_stale_one_nor_fails_the_law() -> None:
    """July 2026's excise above 12 000 kWh is the law's 0,989139 c EUR/kWh.
    Two cards print 0,9891, two round it to 0,99 and one still prints the
    second quarter's 0,9864: the 0,99 agrees with both, and taken for the
    consensus it passed the stale card and failed the law. Nor does one
    card's own digits make the consensus: EBEM's 0,105764 for the 0,1057668
    the others round failed the law's energy contribution."""

    def card(sid: str, high: float, contribution: float) -> Any:
        return _card(
            _snapshot(sid, label="2026-07", excise=(0.0087238, high), contribution=contribution)
        )

    checks = lc.consensus(
        [
            card("alpha", 0.009891, 0.0010577),
            card("beta", 0.009891, 0.0010577),
            card("gamma", 0.0099, 0.001058),
            card("delta", 0.0099, 0.001058),
            card("epsilon", 0.009864, 0.00105764),
        ]
    )
    assert _failing(checks) == {}
    assert [c.label for c in checks if c.status == "notice"] == [
        "epsilon: federal excise above 12000 kWh for 2026-07"
    ]


def test_a_coarse_consensus_over_finer_figures_that_disagree_is_no_verdict() -> None:
    """Three cards print Sibelga's T2 as 1,45, one as 1,447 and one misprints
    1,457: the 1,45 agrees with both and cannot tell which is right, so the
    misprint is reported rather than passed."""
    cards = [
        _card(_snapshot(sid, t1_prop=t1_prop))
        for sid, t1_prop in (
            ("alpha", 0.0145),
            ("beta", 0.0145),
            ("gamma", 0.0145),
            ("delta", 0.01447),
            ("epsilon", 0.01457),
        )
    ]
    checks = lc.consensus(cards)
    assert _failing(checks) == {}
    assert {c.label: c.detail for c in checks if c.status == "notice"} == {
        "fleet: fluvius_kempen T1 proportional for 2026-09": (
            "no majority: 1.447 c EUR/kWh on delta; 1.45 c EUR/kWh on alpha, beta, gamma; "
            "1.457 c EUR/kWh on epsilon"
        )
    }


def test_two_cards_alike_do_not_outvote_three_printing_another_figure_differently() -> None:
    """Three cards print Kempen's T1 to five, four and two decimals, two
    still print 2025's alike: the three agree, and the two depart."""
    cards = [
        _card(_snapshot(sid, t1_prop=t1_prop))
        for sid, t1_prop in (
            ("alpha", 0.0227489),
            ("beta", 0.022749),
            ("gamma", 0.0227),
            ("delta", 0.0212),
            ("epsilon", 0.0212),
        )
    ]
    assert sorted(_failing(lc.consensus(cards))) == [
        "delta: fluvius_kempen T1 proportional for 2026-09",
        "epsilon: fluvius_kempen T1 proportional for 2026-09",
    ]


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
    """The law read from Justel sets September 2026's excise, so a stale
    excise is not billed and does not fail. With the law unread, February
    2027 is past const.py's table, and the card's figure would be billed."""

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
    excise_law._HELD.clear()
    outside = lc.consensus(fleet("2027-02"))
    assert sorted(_failing(outside)) == [
        "gamma: federal excise above 12000 kWh for 2027-02",
        "gamma: federal excise up to 12000 kWh for 2027-02",
    ]
    # The contribution stays zero by law from August 2026 on.
    assert [c.status for c in outside if "contribution" in c.label] == ["notice"]


def test_a_fleet_printing_an_energy_contribution_again_fails_against_the_law() -> None:
    """The integration bills none from August 2026: a levy brought back
    would go unbilled. Before August the fleet's 0,10577 is the law's."""

    def fleet(label: str, contribution: float) -> list[Any]:
        return [
            _card(_snapshot(supplier, label=label, contribution=contribution))
            for supplier in ("alpha", "beta", "gamma")
        ]

    assert _failing(lc.consensus(fleet("2026-11", 0.0010577))) == {
        "federal: energy contribution for 2026-11 against the law": (
            "alpha, beta, gamma print 0.10577 c EUR/kWh where the integration bills 0 c EUR/kWh"
        )
    }
    # The synthetic excise is August's, so July's fails on its own.
    july = _failing(lc.consensus(fleet("2026-07", 0.0010577)))
    assert not any("contribution" in label for label in july)
    assert _failing(lc.consensus(fleet("2026-11", 0.0))) == {}


def test_a_fluvius_card_without_data_management_waits_on_a_sheet_that_did_not_answer() -> None:
    """Fluvius did not answer, so no fee is held: a row printing none is
    transient, never filed, as on 2026-10-19 when the runner's DNS timed out
    on www.fluvius.be. A row printing another fee still fails."""
    fluvius._cache.clear()

    def fleet(metering: float) -> list[Any]:
        return [
            _card(_snapshot("alpha", label="2026-10")),
            _card(_snapshot("beta", label="2026-10")),
            _card(_snapshot("gamma", label="2026-10", metering=metering)),
        ]

    excise_law._HELD.clear()
    tarbel._HELD.clear()
    unread = lc.consensus(fleet(0.0), date(2026, 10, 19), sheet_unread=True)
    [check] = [c for c in unread if "metering" in c.label]
    assert (check.label, check.status) == (
        "gamma: fluvius_kempen metering for 2026-10",
        "transient",
    )
    assert check.detail.endswith("; the Fluvius sheet did not answer")
    assert list(_failing(lc.consensus(fleet(0.0), date(2026, 10, 19)))) == [
        "gamma: fluvius_kempen metering for 2026-10"
    ]
    assert list(_failing(lc.consensus(fleet(17.0), date(2026, 10, 19), sheet_unread=True))) == [
        "gamma: fluvius_kempen metering for 2026-10"
    ]


def test_a_fluvius_card_without_data_management_is_a_notice_in_its_year() -> None:
    """The integration bills the regulated fee where a Fluvius row prints
    none, for the tariff year const.py knows; a wrong fee that is printed is
    billed, and so is a missing one past that year."""
    # The synthetic excise is not 2027's: measured against the law here, it
    # would fail on its own.
    excise_law._HELD.clear()

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


def test_a_fluvius_fleet_printing_another_fee_than_the_sheet_fails() -> None:
    """The fee a card leaves out is billed from the sheet: most cards
    printing another one means the sheet was misread or changed."""
    cards = [_card(_snapshot(sid, metering=19.5)) for sid in ("alpha", "beta")]
    assert list(_failing(lc.consensus(cards))) == [
        "fluvius_kempen: metering for 2026-09 against the Fluvius sheet"
    ]


@pytest.mark.parametrize(
    ("fee", "status"),
    [
        (17.85, "ok"),
        (None, "fail"),
        (ExtractorError("HTTP 503 fetching https://www.fluvius.be"), "transient"),
    ],
)
async def test_this_year_s_fluvius_sheet_is_read_before_the_fleet(
    monkeypatch: pytest.MonkeyPatch, fee: float | Exception | None, status: str
) -> None:
    """Held for the fleet when it reads; no sheet or no fee fails, since a
    Flemish card's missing fee then goes unbilled; the network is transient."""

    async def fetch_fee(*_args: Any, **_kwargs: Any) -> float | None:
        if isinstance(fee, Exception):
            raise fee
        return fee

    monkeypatch.setattr(fluvius, "fetch_fee", fetch_fee)
    fluvius._cache.clear()
    check = await lc.check_fluvius_sheet(None, date(2026, 10, 19), sleep=_no_sleep)
    assert (check.label, check.status) == ("fluvius: 2026 gas tariff sheet", status)
    assert fluvius.data_management_fee(2026) == (17.85 if status == "ok" else None)


def _stating(snapshot: SupplierSnapshot, rate: float) -> SupplierSnapshot:
    return replace(snapshot, taxes=replace(snapshot.taxes, card_vat_rate=rate))


def test_the_vat_rate_the_cards_state_is_measured_against_the_law() -> None:
    """A card stating another rate is billed on it, so it fails; the fleet
    stating another rate than const.py's 6 % fails against the law, since
    every card that states none is billed on that. A card that states no
    rate does not vote."""
    fleet = [_card(_stating(_snapshot(sid), 0.06)) for sid in ("alpha", "beta")]
    assert _failing(lc.consensus([*fleet, _card(_snapshot("delta"))])) == {}
    odd = _card(_stating(_snapshot("gamma"), 0.12))
    assert list(_failing(lc.consensus([*fleet, odd]))) == ["gamma: federal VAT rate for 2026-09"]
    raised = [_card(_stating(_snapshot(sid), 0.09)) for sid in ("alpha", "beta")]
    [(label, detail)] = _failing(lc.consensus(raised)).items()
    assert label == "federal: VAT rate for 2026-09 against the law"
    assert detail == "alpha, beta print 9 % where the integration bills 6 %"


def test_a_documented_card_error_is_a_notice_while_the_card_prints_it() -> None:
    """EBEM's Kempen T1 of 2,12 is documented; another figure from EBEM, or
    the same one from another supplier, is a new departure."""

    def fleet(supplier: str, t1_prop: float) -> list[Any]:
        return [
            _card(_snapshot("alpha", t1_prop=0.0227489)),
            _card(_snapshot("beta", t1_prop=0.0227)),
            _card(_snapshot(supplier, t1_prop=t1_prop)),
        ]

    today = date(2026, 10, 8)
    known = lc.consensus(fleet("ebem", 0.0212), today)
    assert _failing(known) == {}
    [notice] = [c for c in known if c.status == "notice"]
    assert notice.label == "ebem: fluvius_kempen T1 proportional for 2026-09"
    assert notice.detail.endswith(
        "; a known card error (the 2025 figure kept in 2026), see docs/providers/ebem.md"
    )
    assert list(_failing(lc.consensus(fleet("ebem", 0.0200), today))) == [
        "ebem: fluvius_kempen T1 proportional for 2026-09"
    ]
    assert list(_failing(lc.consensus(fleet("gamma", 0.0212), today))) == [
        "gamma: fluvius_kempen T1 proportional for 2026-09"
    ]


@pytest.mark.parametrize(
    ("today", "allowed"), [(date(2026, 12, 31), True), (date(2027, 1, 1), False)]
)
def test_a_known_card_error_is_reported_again_once_its_allowance_lapses(
    today: date, allowed: bool
) -> None:
    fleet = [
        _card(_snapshot("alpha", t1_prop=0.0227489)),
        _card(_snapshot("beta", t1_prop=0.0227)),
        _card(_snapshot("ebem", t1_prop=0.0212)),
    ]
    failing = _failing(lc.consensus(fleet, today))
    assert (failing == {}) is allowed


def test_the_fleet_s_excise_is_measured_against_the_law() -> None:
    """Every card agreeing is not enough: the excise is billed from the law,
    so a fleet printing another one for a month the law sets means the law
    was misread or changed. February 2027 is on the step of 1 January."""

    def fleet(excise: tuple[float, float]) -> list[Any]:
        return [_card(_snapshot(sid, label="2027-02", excise=excise)) for sid in ("alpha", "beta")]

    assert sorted(_failing(lc.consensus(fleet((0.0109, 0.0118))))) == [
        "federal: excise above 12000 kWh for 2027-02 against the law",
        "federal: excise up to 12000 kWh for 2027-02 against the law",
    ]
    assert _failing(lc.consensus(fleet((0.0120734, 0.0129744)))) == {}


@pytest.mark.parametrize(
    ("page", "status"),
    [
        (None, "ok"),
        ("<html>maintenance</html>", "fail"),
        (ExtractorError("HTTP 503 fetching https://www.ejustice.just.fgov.be"), "transient"),
    ],
)
async def test_the_excise_law_is_read_before_the_fleet(
    monkeypatch: pytest.MonkeyPatch, page: str | Exception | None, status: str
) -> None:
    """Read as the integration reads it and held for the fleet; a page that
    is not the article fails, a network failure is transient."""
    published = (
        Path(__file__).parent / "fixtures" / "justel" / "loi_programme_2004_fr.html"
    ).read_bytes()

    async def fetch_text(*_args: Any, **_kwargs: Any) -> str:
        if isinstance(page, Exception):
            raise page
        return published.decode("iso-8859-1") if page is None else page

    monkeypatch.setattr(lc, "fetch_text", fetch_text)
    excise_law._HELD.clear()
    check = await lc.check_excise_law(None, sleep=_no_sleep)
    assert (check.label, check.status) == ("federal: gas excise law", status)
    assert bool(excise_law._HELD) == (status == "ok")


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        (None, "ok"),
        (b"<html>maintenance</html>", "fail"),
        (ExtractorError("HTTP 503 fetching https://eservices.minfin.fgov.be"), "transient"),
    ],
)
async def test_the_excise_tariff_is_read_before_the_fleet(
    monkeypatch: pytest.MonkeyPatch, payload: bytes | Exception | None, status: str
) -> None:
    """FPS Finance's quarters, held for the fleet; a workbook that is not
    annex 7 fails, a network failure is transient."""
    published = (Path(__file__).parent / "fixtures" / "tarbel" / "accijnzen.xlsx").read_bytes()

    async def fetch_bytes(*_args: Any, **_kwargs: Any) -> bytes:
        if isinstance(payload, Exception):
            raise payload
        return published if payload is None else payload

    monkeypatch.setattr(tarbel, "fetch_bytes", fetch_bytes)
    tarbel._HELD.clear()
    check = await lc.check_excise_tariff(None, sleep=_no_sleep)
    assert (check.label, check.status) == ("federal: gas excise tariff", status)
    assert bool(tarbel._HELD) == (status == "ok")


def test_a_single_excise_rate_stands_for_both_slices() -> None:
    excise_law._HELD.clear()
    cards = [
        _card(_snapshot("alpha", label="2027-02")),
        _card(_snapshot("beta", label="2027-02")),
        _card(_snapshot("gamma", label="2027-02", excise=(0.0109, None))),
    ]
    assert list(_failing(lc.consensus(cards))) == [
        "gamma: federal excise above 12000 kWh for 2027-02"
    ]


def test_regional_levies_are_compared_among_their_region_s_cards() -> None:
    """The Walloon connection fee is billed from the law, so a departure
    there is a notice in any month."""

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
    assert "gamma: wallonia connection fee for 2027-02" not in _failing(
        lc.consensus(fleet("2027-02"))
    )


def test_a_walloon_fleet_printing_another_connection_fee_fails() -> None:
    """The fee is billed from the order of 2003 with no end: the cards
    following an amendment the integration does not know of is a failure."""
    cards = [_card(_snapshot(sid, connection_fee=0.00015), "wallonia") for sid in ("alpha", "beta")]
    assert list(_failing(lc.consensus(cards))) == [
        "wallonia: connection fee for 2026-09 against the law"
    ]


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


def test_a_card_in_force_until_replaced_is_never_late() -> None:
    """A card with no end date is the one the supplier links today, whatever
    month its title names."""
    snapshot = _snapshot("acme", label="2026-06", valid_until=None)
    check = lc._freshness("acme/acme_var/flanders", snapshot, TODAY)
    assert (check.status, check.detail) == ("ok", "2026-06, in force until replaced")


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


async def test_a_supplier_that_does_not_answer_is_given_up_on_for_the_run() -> None:
    """Every further card of a dead host would cost the same retries and
    pauses: what is left is reported as transient without being fetched."""
    asked: list[str] = []

    async def fetch(_session: Any, contract: str, _region: str) -> SupplierSnapshot:
        asked.append(contract)
        raise ExtractorError("network error fetching https://acme.test/card.pdf: TimeoutError")

    async def fetch_index(_session: Any) -> dict[str, dict[str, float]]:
        asked.append("index")
        return {}

    extractor = SupplierExtractor(
        id="acme",
        label="Acme",
        contracts=tuple(
            Contract(id=f"acme_{n}", label="Acme", kind="fixed", regions=frozenset({"flanders"}))
            for n in range(5)
        ),
        fetch=fetch,
        fetch_index=fetch_index,
    )
    checks = await lc.check_fleet(None, [extractor], TODAY, sleep=_no_sleep)
    assert asked == ["acme_0"] * 3 + ["acme_1"] * 3 + ["acme_2"] * 3
    assert [(c.label, c.status) for c in checks] == [
        *((f"acme/acme_{n}/flanders: fetch", "transient") for n in range(5)),
        ("acme: index publication", "transient"),
    ]
    assert checks[3].detail == "not fetched: gave up on acme after 3 network failures in a row"
    assert lc.exit_code(checks) == 2
    assert lc.render_fingerprint(checks) == ""


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


def test_a_withdrawn_contract_is_a_target_of_the_archive_s_backfill_only() -> None:
    """Its last card stays up and stays stale, so the daily walk and the
    check leave it out; its past months are still mirrored."""
    from custom_components.be_gas_prices.providers import octaplus

    daily = {contract for _, contract, _ in lc.targets([octaplus.EXTRACTOR], TODAY)}
    mirrored = {
        contract for _, contract, _ in lc.targets([octaplus.EXTRACTOR], TODAY, withdrawn=True)
    }
    assert "octaplus_flux" not in daily
    assert "octaplus_boostflex" in daily
    assert mirrored - daily == {
        "octaplus_flux",
        "octaplus_ecoflux",
        "octaplus_fixed",
        "octaplus_ecofixed",
    }


async def test_the_custom_and_withdrawn_suppliers_are_not_checked() -> None:
    withdrawn = _extractor("old", deprecated_until=date(2026, 8, 31))
    custom = _extractor("custom")
    assert lc.targets([withdrawn, custom], TODAY) == []
    assert await lc.check_fleet(None, [withdrawn, custom], TODAY, sleep=_no_sleep) == []


def test_main_writes_the_fingerprint_and_returns_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fleets: list[dict[str, Any]] = []

    async def check_fleet(*_args: Any, **kwargs: Any) -> list[Any]:
        fleets.append(kwargs)
        return [lc.Check("gone/gone_fix/flanders: fetch", "fail", "HTTP 404")]

    async def check_excise_law(*_args: Any, **_kwargs: Any) -> Any:
        return lc.Check("federal: gas excise law", "ok")

    async def check_excise_tariff(*_args: Any, **_kwargs: Any) -> Any:
        return lc.Check("federal: gas excise tariff", "ok")

    async def check_fluvius_sheet(*_args: Any, **_kwargs: Any) -> Any:
        return lc.Check("fluvius: 2026 gas tariff sheet", "ok")

    monkeypatch.setattr(lc, "check_excise_law", check_excise_law)
    monkeypatch.setattr(lc, "check_excise_tariff", check_excise_tariff)
    monkeypatch.setattr(lc, "check_fluvius_sheet", check_fluvius_sheet)
    monkeypatch.setattr(lc, "check_fleet", check_fleet)
    fingerprint = tmp_path / "failures.txt"
    monkeypatch.setattr(sys, "argv", ["live_check.py", "--fingerprint", str(fingerprint)])
    assert lc.main() == 1
    assert fingerprint.read_text() == "gone/gone_fix/flanders: fetch\n"
    assert capsys.readouterr().out.startswith(
        "# Live check: 3 pass, 1 fail, 0 transient, 0 notices"
    )
    assert fleets[-1]["sheet_unread"] is False

    async def sheet_down(*_args: Any, **_kwargs: Any) -> Any:
        return lc.Check("fluvius: 2026 gas tariff sheet", "transient", "network error")

    monkeypatch.setattr(lc, "check_fluvius_sheet", sheet_down)
    assert lc.main() == 1
    assert fleets[-1]["sheet_unread"] is True
    capsys.readouterr()

    async def crash(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("a bug in the check")

    monkeypatch.setattr(lc, "check_fleet", crash)
    assert lc.main() == 3


@pytest.mark.parametrize(("fingerprint", "rc"), [(False, "3"), (True, "1")])
def test_the_workflow_tells_a_crash_from_a_failing_card(
    tmp_path: Path, fingerprint: bool, rc: str
) -> None:
    """An import that fails exits 1 before the check writes its fingerprint:
    the workflow reads that as the crash it is, not as a card to file."""
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/live_check.yml").read_text()
    )
    step = next(s for s in workflow["jobs"]["check"]["steps"] if s.get("id") == "check")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    write = "touch failures.txt\n" if fingerprint else ""
    (stubs / "python").write_text(f"#!/bin/sh\n{write}exit 1\n")
    (stubs / "python").chmod(0o755)
    output = tmp_path / "output"
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output)}
    subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, check=True)
    assert output.read_text() == f"rc={rc}\n"


def test_a_render_that_never_returns_does_not_hold_the_exit() -> None:
    """The per-card timeout abandons the render; its thread must not keep
    the event loop, or the process, from ending."""
    hang = threading.Event()

    async def main() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(in_daemon_thread(lambda _: hang.wait(), b""), 0.1)

    started = time.monotonic()
    asyncio.run(main())
    assert time.monotonic() - started < 5.0
    hang.set()
