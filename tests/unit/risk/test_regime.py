"""What kind of market it was, and the ways a regime label manufactures significance (#59).

A regime vocabulary is the easiest place in this project to find an effect that is not there:
enough tags and every result works somewhere. So the tests below are mostly about restraint --
that missing context stays missing, that a redefinition cannot reach backwards, and that the
artifact has no power beyond grouping.
"""

from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.risk.regime import (
    TREND_SESSIONS,
    VOLATILITY_DISTRIBUTION_SESSIONS,
    BreadthState,
    MarketRegimeSnapshot,
    TrendState,
    VolatilityState,
    build_market_regime_snapshot,
)

AS_OF = date(2026, 8, 14)
SOURCE = Path("src/quantbot/risk/regime.py")


def rising(count: int, *, start: float = 100.0, step: float = 0.1) -> list[Decimal]:
    return [Decimal(str(round(start + index * step, 4))) for index in range(count)]


def choppy(count: int, *, start: float = 100.0, swing: float = 0.5) -> list[Decimal]:
    return [
        Decimal(str(round(start + (swing if index % 2 else -swing) + index * 0.01, 4)))
        for index in range(count)
    ]


def build(**overrides: object) -> MarketRegimeSnapshot:
    fields: dict[str, object] = {
        "as_of_session": AS_OF,
        "benchmark_symbol": "SPY",
        "source": "alpaca:sip:all:1day",
        "benchmark_closes": rising(VOLATILITY_DISTRIBUTION_SESSIONS + 10),
        "universe_closes": {
            "AAA": rising(10),
            "BBB": rising(10),
            "CCC": rising(10, step=-0.1),
        },
    }
    fields.update(overrides)
    return build_market_regime_snapshot(**fields)  # type: ignore[arg-type]


# --- the artifact has no authority ---------------------------------------------------------------


def test_the_regime_module_cannot_reach_an_execution_path() -> None:
    """Context, never authority. A regime may group an outcome; it may never permit one.

    Asserted structurally rather than by intent, the same way `shadow.py` is: if the module
    cannot import a broker, a coordinator or an order, then no future edit can quietly let a
    calm market relax a veto.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = {"broker", "execution", "coordinator", "OrderIntent", "vetoes", "kill_switch"}
    offenders = [name for name in imported if any(item in name for item in forbidden)]
    assert not offenders, f"a regime snapshot must not be able to reach execution: {offenders}"


# --- redefinition cannot reach backwards ---------------------------------------------------------


def test_changing_the_method_mints_a_new_snapshot_rather_than_relabelling_history() -> None:
    """The rule #84 set for dependence and #85 for veto evidence, applied here.

    Reconstructing an old decision's regime under today's definition answers what the market
    *would* be called now, which is a different question from what it was called then. Pooling
    the two would let a redefinition silently rewrite every result already grouped by regime.
    """
    first = build()
    second = build(method_version=2)

    assert first.identity != second.identity
    assert first.identity.startswith("market-regime-v1-")
    assert second.identity.startswith("market-regime-v2-")
    # The reading itself is unchanged; only the definition moved.
    assert (first.trend, first.volatility, first.breadth) == (
        second.trend,
        second.volatility,
        second.breadth,
    )


def test_the_identity_covers_every_input_that_changes_the_reading() -> None:
    """Two snapshots differing in benchmark, source or session are two snapshots."""
    base = build()

    for change in (
        {"benchmark_symbol": "QQQ"},
        {"source": "another-provider"},
        {"as_of_session": date(2026, 8, 13)},
    ):
        assert build(**change).identity != base.identity, change

    # And the artifacts it was derived alongside.
    assert build(source_snapshot_ids=("dependence-v1-abcd",)).identity != base.identity


# --- unavailable is a state, not a neutral value --------------------------------------------------


def test_short_history_reports_unavailable_rather_than_a_calm_market() -> None:
    """A benchmark nobody has enough history for is not a quiet one.

    This is the failure that would make a regime study find effects in its own gaps: if missing
    context silently read as ordinary context, every early-period result would be conditioned on
    a label that was really "we did not know".
    """
    thin = build(benchmark_closes=rising(30), universe_closes={})

    assert thin.trend is TrendState.UNAVAILABLE
    assert thin.volatility is VolatilityState.UNAVAILABLE
    assert thin.breadth is BreadthState.UNAVAILABLE
    assert thin.realised_volatility is None
    assert thin.participation is None
    assert thin.fully_measured is False
    assert len(thin.unavailable) == 3
    assert any(str(TREND_SESSIONS) in reason for reason in thin.unavailable)


def test_an_unexplained_gap_cannot_be_constructed() -> None:
    """An axis that says UNAVAILABLE without a reason is indistinguishable from a reading."""
    with pytest.raises(ValueError, match="without saying why"):
        MarketRegimeSnapshot(
            as_of_session=AS_OF,
            benchmark_symbol="SPY",
            source="test",
            trend=TrendState.UNAVAILABLE,
            volatility=VolatilityState.NORMAL,
            breadth=BreadthState.BROAD,
            unavailable=(),
        )


def test_an_unavailable_axis_cannot_carry_a_measurement() -> None:
    """Otherwise a reader sees a number beside a label saying the number does not exist."""
    with pytest.raises(ValueError, match="cannot carry a measured volatility"):
        MarketRegimeSnapshot(
            as_of_session=AS_OF,
            benchmark_symbol="SPY",
            source="test",
            trend=TrendState.ABOVE,
            volatility=VolatilityState.UNAVAILABLE,
            breadth=BreadthState.BROAD,
            realised_volatility=Decimal("0.15"),
            unavailable=("volatility: short history",),
        )


def test_a_flat_benchmark_has_no_direction_to_participate_in() -> None:
    """Breadth against a zero move would be an arbitrary tie-break reported as a measurement."""
    flat = [Decimal("100")] * (VOLATILITY_DISTRIBUTION_SESSIONS + 10)
    snapshot = build(benchmark_closes=flat)

    assert snapshot.breadth is BreadthState.UNAVAILABLE
    assert any("did not move" in reason for reason in snapshot.unavailable)


# --- the readings themselves ---------------------------------------------------------------------


def test_trend_is_measured_against_the_benchmarks_own_average() -> None:
    """Above and below its own 200-session mean, not against an absolute level."""
    assert build(benchmark_closes=rising(400)).trend is TrendState.ABOVE
    assert build(benchmark_closes=rising(400, start=200.0, step=-0.2)).trend is TrendState.BELOW


def test_volatility_is_placed_against_its_own_history_not_an_absolute_threshold() -> None:
    """An absolute bps figure would encode one era's volatility as a permanent definition.

    Every regime study spanning a decade would then be measuring the calendar. Terciles of the
    benchmark's own trailing distribution keep the label about the market rather than the year.
    """
    calm = build(benchmark_closes=rising(400, step=0.01))
    assert calm.volatility in {VolatilityState.CALM, VolatilityState.NORMAL}

    # The same series scaled up in absolute price terms must not change the label: only the
    # shape of its returns matters.
    scaled = build(benchmark_closes=[value * 1000 for value in rising(400, step=0.01)])
    assert scaled.volatility is calm.volatility


def test_breadth_reports_the_share_moving_with_the_benchmark() -> None:
    """A narrow advance and a broad one are different facts a single index hides."""
    rising_bench = rising(VOLATILITY_DISTRIBUTION_SESSIONS + 10)
    broad = build(
        benchmark_closes=rising_bench,
        universe_closes={name: rising(10) for name in ("AAA", "BBB", "CCC", "DDD")},
    )
    narrow = build(
        benchmark_closes=rising_bench,
        universe_closes={
            "AAA": rising(10),
            "BBB": rising(10, step=-0.1),
            "CCC": rising(10, step=-0.1),
            "DDD": rising(10, step=-0.1),
        },
    )

    assert broad.breadth is BreadthState.BROAD
    assert broad.participation == Decimal("1.0000")
    assert narrow.breadth is BreadthState.NARROW
    assert narrow.participation == Decimal("0.2500")


def test_the_snapshot_carries_the_artifacts_it_was_derived_alongside() -> None:
    """So a later study can trace the whole context rather than one axis of it."""
    snapshot = build(source_snapshot_ids=("dependence-v1-two", "dependence-v1-one"))

    assert snapshot.source_snapshot_ids == ("dependence-v1-one", "dependence-v1-two")
    row = snapshot.as_row()
    assert row["source_snapshot_ids"] == "dependence-v1-one,dependence-v1-two"
    assert row["snapshot_id"] == snapshot.identity
