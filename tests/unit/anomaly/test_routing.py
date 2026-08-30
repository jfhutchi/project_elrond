"""The falsification gate, and the ways a routing report flatters the thing it judges (#56).

This module exists to be able to say no. Every test below protects that: a detector that routes
at random must produce a lift of 1, a small sample must refuse to produce a rate at all, and a
proxy with no corpus behind it must never carry a number a reader could quote.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantbot.anomaly.routing import (
    InformationProxy,
    ProxyAvailability,
    ProxyResult,
    RoutingSample,
    RoutingValueReport,
    dependence_units,
    design_effect,
    effective_observations,
    evaluate_routing_value,
    intraclass_correlation,
)

START = date(2026, 1, 5)


def sample(
    symbol: str,
    day_offset: int,
    *,
    selected: bool = True,
    group: str | None = None,
    move: str | None = "1.0",
) -> RoutingSample:
    return RoutingSample(
        symbol=symbol,
        session_date=START + timedelta(days=day_offset),
        selected=selected,
        group_key=group,
        next_absolute_move_sigma=None if move is None else Decimal(move),
    )


def report_for(samples: list[RoutingSample], **overrides: object) -> RoutingValueReport:
    fields: dict[str, object] = {"detector_identity": "anomaly-detector-v1-x", "samples": samples}
    fields.update(overrides)
    return evaluate_routing_value(**fields)  # type: ignore[arg-type]


# --- effective sample size ----------------------------------------------------------------------


def test_symbols_moving_together_on_one_day_are_not_many_observations() -> None:
    """The property Sol required, and the one the first implementation missed.

    Twenty symbols firing on one session with near-identical outcomes is one macro fact. Keying
    on the 0.90 dependence clusters did not catch it -- that threshold yields almost all
    singletons -- so the discount is estimated from the outcomes instead.
    """
    # Several macro days, each moving its whole roster together by a different amount. Multiple
    # sessions are needed for the ICC to have any between-cluster variance to measure.
    macro: list[RoutingSample] = []
    for day, magnitude in enumerate(("3.0", "0.5", "4.5", "1.2", "2.7")):
        macro += [
            sample(f"SYM{index:02d}", day, group=f"cluster-{index:02d}", move=magnitude)
            for index in range(20)
        ]
    units = dependence_units(macro)

    assert len(units) == 100, "distinct clusters, so grouping alone collapses nothing"
    assert intraclass_correlation(units) > Decimal("0.5"), (
        "identical outcomes within each session must register as clustered"
    )
    assert design_effect(units) > Decimal("5")
    assert effective_observations(macro) < 20, (
        "twenty symbols sharing one driver must not count as twenty successes"
    )


def test_a_sample_confined_to_one_session_counts_as_one_observation() -> None:
    """The case the ICC cannot speak to, handled rather than left to return no discount.

    With a single cluster there is no between-cluster variance to estimate and the estimator
    returns zero -- meaning no discount at all for the most obviously clustered sample there is.
    """
    one_day = [
        sample(f"SYM{index:02d}", 0, group=f"cluster-{index:02d}", move="3.0")
        for index in range(20)
    ]

    assert effective_observations(one_day) == 1


def test_unrelated_events_across_many_days_are_barely_discounted() -> None:
    """The other direction: a discount that always fires would understate every detector."""
    spread = [
        sample(f"SYM{index}", index, group=f"cluster-{index}", move=str(1.0 + (index % 7) * 0.4))
        for index in range(40)
    ]
    units = dependence_units(spread)

    assert design_effect(units) == Decimal("1"), "one event per session cannot be clustered"
    assert effective_observations(spread) == 40


def test_a_negative_icc_estimate_never_enlarges_the_sample() -> None:
    """Between-session variance below within-session is evidence of no clustering, not of less."""
    alternating = [
        sample(f"SYM{index}", index // 4, group=f"c{index}", move="5.0" if index % 2 else "0.1")
        for index in range(24)
    ]
    units = dependence_units(alternating)

    assert intraclass_correlation(units) >= Decimal("0")
    assert design_effect(units) >= Decimal("1")
    assert effective_observations(alternating) <= len(units)


def test_a_dependence_group_collapses_before_the_design_effect_is_applied() -> None:
    """Two symbols the matrix put in one cluster on one session are definitionally one unit."""
    paired = [
        sample("SPY", 0, group="cluster-a", move="2.0"),
        sample("QQQ", 0, group="cluster-a", move="2.2"),
        sample("TLT", 1, group="cluster-b", move="1.0"),
    ]

    units = dependence_units(paired)
    assert len(units) == 2, "one cluster on one session is one unit"


def test_an_unmeasured_symbol_is_not_merged_with_unrelated_ones() -> None:
    """"Unmeasured" must not silently become "same group as every other unmeasured symbol"."""
    unplaced = [
        sample("AAA", 0, group=None, move="1.0"),
        sample("BBB", 0, group=None, move="1.0"),
    ]

    assert len(dependence_units(unplaced)) == 2


# --- the verdict --------------------------------------------------------------------------------


def test_a_detector_that_routes_at_random_shows_no_lift() -> None:
    """The negative result must be reachable, or this gate cannot do its job.

    Selected and unselected sessions drawn from the same distribution: the two means coincide
    and the lift is 1. A report that could not produce this would only ever confirm.
    """
    samples: list[RoutingSample] = []
    for index in range(200):
        samples.append(
            sample(
                f"SYM{index % 20}",
                index,
                selected=index % 2 == 0,
                group=f"cluster-{index % 20}",
                move=str(1.0 + (index % 5) * 0.5),
            )
        )

    result = report_for(samples)
    move = next(
        item
        for item in result.proxies
        if item.proxy is InformationProxy.NEXT_SESSION_ABSOLUTE_MOVE
    )

    assert move.availability is ProxyAvailability.MEASURED
    assert move.lift is not None
    assert Decimal("0.9") < move.lift < Decimal("1.1"), (
        f"random routing produced a lift of {move.lift}; the gate cannot return a negative"
    )


def test_a_detector_that_selects_the_interesting_sessions_shows_lift() -> None:
    """And the positive direction, so the test above is not passing on a broken metric."""
    samples = [
        sample(f"SYM{index}", index, selected=True, group=f"c{index}", move="4.0")
        for index in range(40)
    ] + [
        sample(f"OTH{index}", 100 + index, selected=False, group=f"o{index}", move="1.0")
        for index in range(160)
    ]

    move = next(
        item
        for item in report_for(samples).proxies
        if item.proxy is InformationProxy.NEXT_SESSION_ABSOLUTE_MOVE
    )
    assert move.lift is not None and move.lift > Decimal("1.5")


def test_a_small_sample_refuses_to_produce_a_rate() -> None:
    """Below the floor a number would invite tuning a threshold against noise."""
    tiny = [sample(f"SYM{index}", index, group=f"c{index}") for index in range(5)]

    move = next(
        item
        for item in report_for(tiny).proxies
        if item.proxy is InformationProxy.NEXT_SESSION_ABSOLUTE_MOVE
    )
    assert move.availability is ProxyAvailability.UNDERPOWERED
    assert move.lift is None
    assert move.selected_mean is None


def test_catalyst_prevalence_is_reported_absent_rather_than_proxied() -> None:
    """A lift against a corpus nobody validated is the artefact this gate exists to catch.

    The universe is broad-market ETFs, which do not file material 8-Ks the way single issuers
    do, and no news provider is integrated. That is a fact about the project, not about the
    detector, and it stays visible instead of being papered over.
    """
    samples = [sample(f"SYM{index}", index, group=f"c{index}") for index in range(60)]

    catalyst = next(
        item
        for item in report_for(samples).proxies
        if item.proxy is InformationProxy.CATALYST_PREVALENCE
    )
    assert catalyst.availability is ProxyAvailability.CATALYST_CORPUS_ABSENT
    assert catalyst.lift is None


def test_an_unmeasured_proxy_cannot_carry_a_number() -> None:
    """Structural, because a quotable number beside an "unavailable" label gets quoted."""
    with pytest.raises(ValueError, match="must not carry a number"):
        ProxyResult(
            proxy=InformationProxy.CATALYST_PREVALENCE,
            availability=ProxyAvailability.CATALYST_CORPUS_ABSENT,
            lift=Decimal("3.0"),
            detail="a lift with no corpus behind it",
        )


def test_the_search_saving_is_the_one_number_that_needs_no_proxy() -> None:
    """A naive scan enriches everything; that comparison rests on no assumption at all."""
    samples = [
        sample(f"SYM{index}", index, selected=index < 10, group=f"c{index}")
        for index in range(1000)
    ]

    result = report_for(samples)
    assert result.scanned_symbol_sessions == 1000
    assert result.selected_symbol_sessions == 10
    assert result.searches_avoided == 990
    assert result.selection_rate == Decimal("10") / Decimal("1000")


def test_a_report_cannot_claim_more_effective_observations_than_events() -> None:
    """A correlation discount shrinks a sample. Anything else is an arithmetic error."""
    with pytest.raises(ValueError, match="can only shrink it"):
        RoutingValueReport(
            detector_identity="anomaly-detector-v1-x",
            scanned_symbol_sessions=100,
            selected_symbol_sessions=10,
            dependence_units=4,
            intraclass_correlation=Decimal("0"),
            design_effect=Decimal("1"),
            effective_observations=5,
            proxies=(),
        )


def test_the_report_identity_moves_with_the_detector_it_judged() -> None:
    """Two verdicts on two detectors must never be pooled as one."""
    samples = [sample(f"SYM{index}", index, group=f"c{index}") for index in range(60)]

    first = report_for(samples)
    second = report_for(samples, detector_identity="anomaly-detector-v1-other")

    assert first.identity != second.identity
    assert first.identity.startswith("routing-value-v1-")
