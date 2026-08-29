"""What moves together, and the ways a correlation limit can be made to say nothing (#58/#59).

The central test here is `test_complete_linkage_does_not_chain_bonds_to_equities`, because it
pins the finding that changed the design. The obvious implementation of "cluster above a
threshold" is connected components, and on this universe that puts Treasuries and equities in one
cluster through credit — a wrong answer that happens to refuse, which is the hardest kind to spot.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantbot.domain import Position
from quantbot.risk.dependence import (
    DependenceSnapshot,
    LinkageMethod,
    MissingDataRule,
    build_dependence_snapshot,
    build_dependence_state,
)

AS_OF = date(2026, 8, 18)


def snapshot(pairs: dict[str, str], **overrides: object) -> DependenceSnapshot:
    fields: dict[str, object] = {
        "source": "alpaca:sip:split:1Day",
        "as_of_session": AS_OF,
        "lookback_sessions": 120,
        "observations_used": 119,
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


def test_complete_linkage_does_not_chain_bonds_to_equities() -> None:
    """The measurement that decided the method, reproduced from the real coefficients.

    These four numbers are what the stored bars actually give over 120 sessions. Under connected
    components, `TLT` and `SPY` land in one cluster despite correlating at 0.45, because credit
    bridges them: `HYG–SPY` at 0.82 on one side and `LQD–TLT` at 0.90 on the other.

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


def test_a_cluster_requires_every_pair_not_a_path() -> None:
    """Three symbols where two pairs clear and one does not is not one cluster."""
    chained = snapshot({"A|B": "0.95", "B|C": "0.95", "A|C": "0.10"})

    clusters = chained.clusters(["A", "B", "C"], threshold=Decimal("0.90"))

    assert sorted(len(cluster) for cluster in clusters) == [1, 2]
    for cluster in clusters:
        assert not {"A", "C"} <= set(cluster)


def test_clustering_is_deterministic_regardless_of_input_order() -> None:
    """A rule whose answer depends on argument order cannot be reproduced from its identity."""
    measured = snapshot({"A|B": "0.95", "A|C": "0.95", "B|C": "0.95", "A|D": "0.10",
                         "B|D": "0.10", "C|D": "0.10"})

    first = measured.clusters(["D", "C", "B", "A"], threshold=Decimal("0.90"))
    second = measured.clusters(["A", "B", "C", "D"], threshold=Decimal("0.90"))

    assert first == second


def test_an_unmeasured_pair_is_not_an_uncorrelated_pair() -> None:
    """Absent means unknown, and unknown must not join a cluster.

    Treating a missing coefficient as zero would silently let an unmeasured symbol into any
    group, which is the permissive direction.
    """
    partial = snapshot({"A|B": "0.95"})

    assert partial.coefficient("A", "C") is None
    assert partial.coefficient("A", "A") == Decimal("1")

    clusters = partial.clusters(["A", "B", "C"], threshold=Decimal("0.90"))
    for cluster in clusters:
        assert not {"A", "C"} <= set(cluster)


def test_the_identity_covers_everything_that_changes_a_coefficient() -> None:
    """Window, return definition, missing-data rule and method all move the number.

    Sol's requirement on #58/#59: a veto's basis must be nameable, and two studies across any of
    these are two studies.
    """
    base = snapshot({"A|B": "0.95"})

    assert base.identity.startswith("dependence-v1-")
    for change in (
        {"lookback_sessions": 60},
        {"observations_used": 59},
        {"source": "another-provider"},
        {"as_of_session": AS_OF - timedelta(days=1)},
        {"method_version": 2},
        {"excluded_symbols": ("ZZZ",)},
    ):
        assert snapshot({"A|B": "0.95"}, **change).identity != base.identity, change

    # And the coefficients themselves.
    assert snapshot({"A|B": "0.94"}).identity != base.identity


def test_a_pair_key_cannot_carry_two_coefficients() -> None:
    """`B|A` and `A|B` would be one pair with two numbers, and the veto would read whichever."""
    with pytest.raises(ValueError, match="not a sorted symbol pair"):
        snapshot({"B|A": "0.95"})
    with pytest.raises(ValueError, match="not a sorted symbol pair"):
        snapshot({"A|A": "1.0"})


def test_a_symbol_without_the_full_window_is_excluded_and_named() -> None:
    """`REQUIRE_COMPLETE`, and the reason `PAIRWISE_COMPLETE` is not offered.

    Pairwise-complete computes each coefficient on whatever sample that pair shares, so one
    cluster can rest on 120 sessions and another on 12 while the matrix presents both the same.
    """
    days = [AS_OF - timedelta(days=offset) for offset in range(9, -1, -1)]
    closes = {
        "A": {day: Decimal(100 + index) for index, day in enumerate(days)},
        "B": {day: Decimal(200 - index) for index, day in enumerate(days)},
        "SHORT": {day: Decimal(50) for day in days[-3:]},
    }

    built = build_dependence_snapshot(
        closes=closes, as_of_session=AS_OF, lookback_sessions=10, source="test"
    )

    assert built is not None
    assert built.excluded_symbols == ("SHORT",)
    assert built.symbols == {"A", "B"}
    assert built.observations_used == 9
    assert built.missing_data_rule is MissingDataRule.REQUIRE_COMPLETE
    assert built.linkage is LinkageMethod.COMPLETE


def test_a_window_that_cannot_be_filled_is_no_snapshot_at_all() -> None:
    """A short window is different evidence, not weaker evidence of the same kind."""
    days = [AS_OF - timedelta(days=offset) for offset in range(4, -1, -1)]
    closes = {"A": {day: Decimal(100) for day in days}}

    assert (
        build_dependence_snapshot(
            closes=closes, as_of_session=AS_OF, lookback_sessions=120, source="test"
        )
        is None
    )


def test_a_constant_series_has_no_correlation_rather_than_a_zero_one() -> None:
    """Zero would say "independent"; the truth is "unanswerable", and they differ at the veto."""
    days = [AS_OF - timedelta(days=offset) for offset in range(4, -1, -1)]
    closes = {
        "MOVES": {day: Decimal(100 + index) for index, day in enumerate(days)},
        "FLAT": {day: Decimal(100) for day in days},
    }

    built = build_dependence_snapshot(
        closes=closes, as_of_session=AS_OF, lookback_sessions=5, source="test"
    )

    assert built is not None
    assert built.coefficient("FLAT", "MOVES") is None


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
