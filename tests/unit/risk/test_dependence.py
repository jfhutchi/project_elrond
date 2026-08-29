"""What moves together, and the ways a correlation limit can be made to say nothing (#58/#59).

Two tests here pin findings that changed the design rather than confirming it.

`test_complete_linkage_does_not_chain_bonds_to_equities` holds the first. The obvious
implementation of "cluster above a threshold" is connected components, and on this universe that
puts Treasuries and equities in one cluster through credit -- a wrong answer that happens to
refuse, which is the hardest kind to spot.

`test_a_shifted_window_is_excluded_rather_than_silently_correlated` holds the second, reported as
#84 against the merged implementation. Eligibility was decided by *counting* observations, and
each symbol then chose its own most recent that many dates, so two symbols could be correlated
across offset calendars with matching vector lengths and no error anywhere.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantbot.domain import Position
from quantbot.risk.dependence import (
    DependenceSnapshot,
    ExclusionReason,
    LinkageMethod,
    MissingDataRule,
    build_dependence_snapshot,
    build_dependence_state,
)

AS_OF = date(2026, 8, 18)
#: Ten contiguous dates ending at `AS_OF`. Contiguity is irrelevant to the code -- the window is
#: whatever the caller's calendar says -- but it keeps the fixtures readable.
WINDOW = tuple(AS_OF - timedelta(days=offset) for offset in range(9, -1, -1))


def snapshot(pairs: dict[str, str], **overrides: object) -> DependenceSnapshot:
    fields: dict[str, object] = {
        "source": "alpaca:sip:split:1Day",
        "session_window": WINDOW,
        "coefficients": {key: Decimal(value) for key, value in pairs.items()},
    }
    fields.update(overrides)
    return DependenceSnapshot(**fields)  # type: ignore[arg-type]


def position(symbol: str, value: str) -> Position:
    amount = Decimal(value)
    return Position(
        symbol=symbol,
        quantity=amount / Decimal("100"),
        market_price=Decimal("100"),
        market_value=amount,
        average_entry_price=Decimal("100"),
    )


def walk(start: int, steps: list[int]) -> dict[date, Decimal]:
    """A price series over `WINDOW`, moving by `steps` between consecutive sessions."""
    prices = [start]
    for step in steps:
        prices.append(prices[-1] + step)
    assert len(prices) == len(WINDOW)
    return dict(zip(WINDOW, (Decimal(price) for price in prices), strict=True))


def pearson_over(left: dict[date, Decimal], right: dict[date, Decimal], days: tuple[date, ...]):
    """An independent correlation over exactly `days`, for comparing against the builder."""

    def returns(series: dict[date, Decimal]) -> list[float]:
        prices = [float(series[day]) for day in days]
        return [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]

    a, b = returns(left), returns(right)
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    spread_a = math.sqrt(sum((v - mean_a) ** 2 for v in a))
    spread_b = math.sqrt(sum((v - mean_b) ** 2 for v in b))
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    return round(covariance / (spread_a * spread_b), 6)


def test_complete_linkage_does_not_chain_bonds_to_equities() -> None:
    """The measurement that decided the method, reproduced from the real coefficients.

    These four numbers are what the stored bars actually give over 120 sessions. Under connected
    components, `TLT` and `SPY` land in one cluster despite correlating at 0.45, because credit
    bridges them: `HYG-SPY` at 0.82 on one side and `LQD-TLT` at 0.90 on the other.

    A concentration limit built on that reports a Treasury position as adding to equity
    concentration. Requiring every pair to clear the threshold is what makes a cluster mean
    "these move together" rather than "a path exists between these".
    """
    measured = snapshot(
        {
            "SPY|TLT": "0.445215",
            "HYG|SPY": "0.816506",
            "LQD|TLT": "0.900391",
            "HYG|LQD": "0.700000",
            "HYG|TLT": "0.500000",
            "LQD|SPY": "0.500000",
        }
    )

    symbols = ["SPY", "HYG", "LQD", "TLT"]
    threshold = Decimal("0.70")
    clusters = measured.clusters(symbols, threshold=threshold)

    # The property that matters: the uncorrelated pair does not share a cluster.
    for cluster in clusters:
        assert not {"SPY", "TLT"} <= set(cluster), (
            f"SPY and TLT correlate at 0.45 and were chained into {cluster}"
        )

    # And the comparison that makes the point, computed rather than asserted from memory:
    # connected components on the same coefficients merges all four into one.
    parent = {name: name for name in symbols}

    def root(name: str) -> str:
        while parent[name] != name:
            name = parent[name]
        return name

    for left in symbols:
        for right in symbols:
            if left < right and (measured.coefficient(left, right) or Decimal("-1")) >= threshold:
                parent[root(left)] = root(right)

    assert len({root(name) for name in symbols}) == 1, (
        "the fixture must actually be one connected component, or this proves nothing"
    )
    assert len(clusters) > 1, "complete linkage must not reproduce that single blob"


def test_a_shifted_window_is_excluded_rather_than_silently_correlated() -> None:
    """#84: same number of observations is not the same sample.

    `SHIFTED` holds ten closes, exactly as many as the window has sessions, but one of them is a
    session before the window and one required session is absent. The superseded implementation
    admitted it on the count, took its own last ten dates, and handed `_pearson` two vectors of
    equal length whose index N referred to different sessions in each.

    The fixture is built so that misalignment is not a subtle distortion: read against its own
    offset dates `SHIFTED` tracks `ANCHOR` almost exactly, and read against the true window it
    does not exist at all. Nothing raised under the old rule, and the coefficient came back to
    six decimal places.
    """
    steps = [3, -2, 5, -4, 6, -1, 2, -3, 4]
    anchor = walk(100, steps)
    partner = walk(200, steps)

    earlier = WINDOW[0] - timedelta(days=1)
    shifted = {earlier: Decimal("97"), **walk(100, steps)}
    del shifted[WINDOW[4]]
    assert len(shifted) == len(WINDOW), "the defect needs a matching observation count"

    built = build_dependence_snapshot(
        closes={"ANCHOR": anchor, "PARTNER": partner, "SHIFTED": shifted},
        session_window=WINDOW,
        source="test",
    )

    assert built is not None
    assert built.excluded == {"SHIFTED": ExclusionReason.INCOMPLETE_WINDOW}
    assert built.coefficient("ANCHOR", "SHIFTED") is None, (
        "a symbol missing a required session was correlated anyway"
    )
    assert built.symbols == {"ANCHOR", "PARTNER"}


def test_a_missing_final_session_is_not_backfilled_by_an_older_one() -> None:
    """The most dangerous shift is the recent one: a stale symbol pretending to be current."""
    steps = [3, -2, 5, -4, 6, -1, 2, -3, 4]
    stale = {WINDOW[0] - timedelta(days=1): Decimal("97"), **walk(100, steps)}
    del stale[WINDOW[-1]]

    built = build_dependence_snapshot(
        closes={"A": walk(100, steps), "B": walk(50, steps), "STALE": stale},
        session_window=WINDOW,
        source="test",
    )

    assert built is not None
    assert built.excluded["STALE"] is ExclusionReason.INCOMPLETE_WINDOW
    assert "STALE" not in built.symbols


def test_every_coefficient_is_computed_over_the_window_the_snapshot_carries() -> None:
    """The alignment property stated positively, against an independently computed number.

    Extra observations outside the window must be ignored rather than absorbed, so a symbol
    with more history correlates identically to one with exactly the window.
    """
    steps = [3, -2, 5, -4, 6, -1, 2, -3, 4]
    a = walk(100, steps)
    b = walk(400, [-1, 4, -3, 2, -5, 3, 1, -2, 6])
    generous = {WINDOW[0] - timedelta(days=offset): Decimal("500") for offset in (1, 2, 3)} | b

    built = build_dependence_snapshot(
        closes={"A": a, "B": generous}, session_window=WINDOW, source="test"
    )

    assert built is not None
    assert built.session_window == WINDOW
    assert built.coefficient("A", "B") == Decimal(str(pearson_over(a, b, WINDOW)))
    assert built.excluded == {}


def test_a_constant_series_is_excluded_by_name_rather_than_vanishing() -> None:
    """#84's ambiguity: "no coefficient" had two meanings and recorded neither.

    Zero would say "independent"; the truth is "unanswerable", and they differ at the veto. But
    a symbol that simply disappeared from `symbols` was indistinguishable from one that was
    never offered, so the reason is now durable.
    """
    steps = [3, -2, 5, -4, 6, -1, 2, -3, 4]
    built = build_dependence_snapshot(
        closes={
            "MOVES": walk(100, steps),
            "ALSO": walk(60, steps),
            "FLAT": dict.fromkeys(WINDOW, Decimal("100")),
        },
        session_window=WINDOW,
        source="test",
    )

    assert built is not None
    assert built.excluded == {"FLAT": ExclusionReason.NON_VARYING}
    assert built.coefficient("FLAT", "MOVES") is None
    assert "FLAT" not in built.symbols


def test_a_cluster_requires_every_pair_not_a_path() -> None:
    """Three symbols where two pairs clear and one does not is not one cluster."""
    chained = snapshot({"A|B": "0.95", "B|C": "0.95", "A|C": "0.10"})

    clusters = chained.clusters(["A", "B", "C"], threshold=Decimal("0.90"))

    assert sorted(len(cluster) for cluster in clusters) == [1, 2]
    for cluster in clusters:
        assert not {"A", "C"} <= set(cluster)


def test_clustering_is_deterministic_regardless_of_input_order() -> None:
    """A rule whose answer depends on argument order cannot be reproduced from its identity."""
    measured = snapshot(
        {
            "A|B": "0.95",
            "A|C": "0.95",
            "B|C": "0.95",
            "A|D": "0.10",
            "B|D": "0.10",
            "C|D": "0.10",
        }
    )

    first = measured.clusters(["D", "C", "B", "A"], threshold=Decimal("0.90"))
    second = measured.clusters(["A", "B", "C", "D"], threshold=Decimal("0.90"))

    assert first == second


def test_an_unmeasured_pair_is_not_an_uncorrelated_pair() -> None:
    """Absent means unknown, and unknown must not join a cluster."""
    partial = snapshot({"A|B": "0.95"})

    assert partial.coefficient("A", "C") is None
    assert partial.coefficient("A", "A") == Decimal("1")

    clusters = partial.clusters(["A", "B", "C"], threshold=Decimal("0.90"))
    for cluster in clusters:
        assert not {"A", "C"} <= set(cluster)


def test_the_identity_covers_everything_that_changes_a_coefficient() -> None:
    """Window, return definition, missing-data rule and method all move the number.

    Sol's requirement on #58/#59: a veto's basis must be nameable, and two studies across any of
    these are two studies. The session window is in there whole rather than as a count and an
    end date, because two different session sets can share both (#84).
    """
    base = snapshot({"A|B": "0.95"})

    assert base.identity.startswith("dependence-v1-")
    for change in (
        {"session_window": WINDOW[2:]},
        {"session_window": tuple(day - timedelta(days=1) for day in WINDOW)},
        {"source": "another-provider"},
        {"method_version": 2},
        {"excluded": {"ZZZ": ExclusionReason.INCOMPLETE_WINDOW}},
    ):
        assert snapshot({"A|B": "0.95"}, **change).identity != base.identity, change

    # Two windows of the same length ending on the same session, differing in the middle.
    swapped = (*WINDOW[:4], WINDOW[4] - timedelta(days=30), *WINDOW[5:])
    assert snapshot({"A|B": "0.95"}, session_window=tuple(sorted(swapped))).identity != (
        base.identity
    ), "same count and same as-of session is not the same sample"

    # And the coefficients themselves.
    assert snapshot({"A|B": "0.94"}).identity != base.identity


def test_derived_window_facts_cannot_disagree_with_the_window() -> None:
    """`as_of_session`, `lookback_sessions` and `observations_used` are read off, not stored."""
    measured = snapshot({"A|B": "0.95"})

    assert measured.as_of_session == AS_OF
    assert measured.lookback_sessions == len(WINDOW)
    assert measured.observations_used == len(WINDOW) - 1


def test_a_pair_key_cannot_carry_two_coefficients() -> None:
    """`B|A` and `A|B` would be one pair with two numbers, and the veto would read whichever."""
    with pytest.raises(ValueError, match="not a sorted symbol pair"):
        snapshot({"B|A": "0.95"})
    with pytest.raises(ValueError, match="not a sorted symbol pair"):
        snapshot({"A|A": "1.0"})


def test_a_window_must_be_ordered_and_distinct() -> None:
    """An unordered window cannot describe the sample its coefficients were computed over."""
    with pytest.raises(ValueError, match="strictly ascending"):
        snapshot({"A|B": "0.95"}, session_window=tuple(reversed(WINDOW)))
    with pytest.raises(ValueError, match="strictly ascending"):
        snapshot({"A|B": "0.95"}, session_window=(*WINDOW, WINDOW[-1]))
    with pytest.raises(ValueError, match="at least two sessions"):
        snapshot({"A|B": "0.95"}, session_window=WINDOW[:1])


def test_a_measured_symbol_missing_a_pair_is_a_construction_error() -> None:
    """The third meaning of "unmeasured" that `excluded` exists to remove."""
    with pytest.raises(ValueError, match="third meaning"):
        snapshot({"A|B": "0.95", "A|C": "0.10"})


def test_a_symbol_cannot_be_both_measured_and_excluded() -> None:
    """Otherwise whether it counts toward a cluster depends on which field is consulted."""
    with pytest.raises(ValueError, match="both measured and excluded"):
        snapshot({"A|B": "0.95"}, excluded={"A": ExclusionReason.NON_VARYING})


def test_a_window_no_symbol_can_fill_is_no_snapshot_at_all() -> None:
    """A short window is different evidence, not weaker evidence of the same kind."""
    closes = {"A": dict.fromkeys(WINDOW[:3], Decimal("100"))}

    assert (
        build_dependence_snapshot(closes=closes, session_window=WINDOW, source="test") is None
    )
    with pytest.raises(ValueError, match="at least two sessions"):
        build_dependence_snapshot(closes=closes, session_window=WINDOW[:1], source="test")


def test_the_declared_rules_are_the_ones_applied() -> None:
    """`REQUIRE_COMPLETE` and `COMPLETE` linkage are not configurable away by construction."""
    steps = [3, -2, 5, -4, 6, -1, 2, -3, 4]
    built = build_dependence_snapshot(
        closes={"A": walk(100, steps), "B": walk(70, [-1, 4, -3, 2, -5, 3, 1, -2, 6])},
        session_window=WINDOW,
        source="test",
    )

    assert built is not None
    assert built.missing_data_rule is MissingDataRule.REQUIRE_COMPLETE
    assert built.linkage is LinkageMethod.COMPLETE


def test_the_state_totals_the_cluster_and_names_what_it_could_not_measure() -> None:
    """The order joins a cluster; what is already in that cluster is what counts against it."""
    measured = snapshot({"A|B": "0.95", "A|C": "0.10", "B|C": "0.10"})

    state = build_dependence_state(
        snapshot=measured,
        symbol="A",
        positions=[position("B", "3000"), position("C", "9000")],
        threshold=Decimal("0.90"),
    )

    assert state.order_cluster == ("A", "B")
    assert state.committed_in_cluster == Decimal("3000"), "C is in a different cluster"
    assert state.unmeasured == ()
    assert state.snapshot_identity == measured.identity


def test_a_short_does_not_net_away_a_correlated_cluster() -> None:
    """Absolute value, as with exposure: a long and a short that move together are two bets."""
    measured = snapshot({"A|B": "0.95"})

    state = build_dependence_state(
        snapshot=measured,
        symbol="A",
        positions=[position("B", "-4000")],
        threshold=Decimal("0.90"),
    )

    assert state.committed_in_cluster == Decimal("4000")


def test_a_held_position_outside_the_snapshot_is_named() -> None:
    """An unmeasured holding makes its cluster's total an understatement, and that must reject.

    The veto turns this into a refusal; the state's job is to say which symbol caused it.
    """
    measured = snapshot({"A|B": "0.95"})

    state = build_dependence_state(
        snapshot=measured,
        symbol="A",
        positions=[position("UNKNOWN", "5000")],
        threshold=Decimal("0.90"),
    )

    assert state.unmeasured == ("UNKNOWN",)
