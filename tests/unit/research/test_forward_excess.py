"""Whether this strategy beat its benchmark by enough to call an edge (#51).

`gross_excess_bps` was a parameter nothing supplied, so #51's capacity ladder reported
`UNASSESSED` forever -- correctly -- and would have gone on doing so after the evidence arrived,
because nothing computed the number. The tenth instance of a control waiting on an input no code
produces.

This measures it. Nearly every test below is a refusal, because nearly every way of computing
this number produces something that would let a capacity verdict rest on nothing.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantbot.research.forward_excess import (
    MINIMUM_SESSIONS,
    ExcessUnavailable,
    measure_forward_excess,
)

START = date(2026, 1, 5)


def series(returns: list[float], *, start: float = 100.0) -> list[tuple[date, Decimal]]:
    """A price path from a list of daily returns, one session apart."""
    value = start
    points = [(START, Decimal(str(round(value, 6))))]
    for index, item in enumerate(returns, start=1):
        value *= 1 + item
        points.append((START + timedelta(days=index), Decimal(str(round(value, 6)))))
    return points


def closes(points: list[tuple[date, Decimal]]) -> dict[date, Decimal]:
    return dict(points)


def measure(strategy: list[float], benchmark: list[float], **kwargs):
    return measure_forward_excess(
        strategy_id="adaptive-momentum",
        benchmark_symbol="SPY",
        equity=series(strategy),
        benchmark_closes=closes(series(benchmark)),
        **kwargs,
    )


# --- the refusals ------------------------------------------------------------------------------


def test_a_short_history_is_refused_rather_than_estimated() -> None:
    """A return measured over a handful of days is noise with a sign.

    This is the state the project is actually in: the durable store holds five days of equity
    history, so the honest answer today is a refusal and the ladder stays UNASSESSED.
    """
    result = measure([0.01] * 10, [0.0] * 10)

    assert not result.measured
    assert result.unavailable is ExcessUnavailable.TOO_FEW_SESSIONS
    assert result.bps is None
    assert str(MINIMUM_SESSIONS) in result.detail


def test_an_excess_indistinguishable_from_zero_is_not_an_edge() -> None:
    """The refusal that stops capacity resolving on noise.

    A strategy that wandered around its benchmark has no edge for capacity to erode. Reporting a
    number anyway would give the ladder something to work with and produce a confident-looking
    AUM figure resting on nothing -- which is exactly the shape of result this project exists to
    refuse.
    """
    wobble = [0.01 if index % 2 else -0.0098 for index in range(120)]
    result = measure(wobble, [0.0] * 120)

    assert not result.measured
    assert result.unavailable is ExcessUnavailable.NOT_DISTINGUISHABLE_FROM_ZERO
    assert result.t_statistic is not None, "measured and insignificant, not unmeasurable"
    assert abs(result.t_statistic) < Decimal("2.0")


def test_a_missing_benchmark_is_refused_rather_than_reported_as_a_raw_return() -> None:
    """"We made 8%" is not evidence of skill in a year the benchmark made 12%.

    Without the benchmark leg the only available number is the strategy's own return, which
    would credit it for market direction it had no view on -- and would do so most generously in
    exactly the rising markets where the mistake is hardest to notice.
    """
    result = measure_forward_excess(
        strategy_id="adaptive-momentum",
        benchmark_symbol="SPY",
        equity=series([0.01] * 120),
        benchmark_closes={},
    )

    assert result.unavailable is ExcessUnavailable.NO_BENCHMARK_RETURNS
    assert result.bps is None


def test_no_equity_history_is_its_own_refusal() -> None:
    """Distinct from a missing benchmark: nobody traded, versus nothing to compare against."""
    result = measure_forward_excess(
        strategy_id="adaptive-momentum",
        benchmark_symbol="SPY",
        equity=[],
        benchmark_closes=closes(series([0.001] * 120)),
    )

    assert result.unavailable is ExcessUnavailable.NO_STRATEGY_RETURNS


def test_a_spread_with_no_variance_cannot_be_tested() -> None:
    """Tracking the benchmark exactly is a real outcome and an untestable one."""
    path = [0.004, -0.002, 0.006] * 40
    result = measure(path, path)

    assert result.unavailable is ExcessUnavailable.NO_VARIANCE
    assert result.bps is None


# --- what it reports when it does answer ----------------------------------------------------------


def test_a_persistent_excess_is_measured_and_annualised() -> None:
    """The case the ladder is waiting for."""
    strategy = [0.001 + (0.002 if index % 3 else -0.0015) for index in range(200)]
    benchmark = [(0.002 if index % 3 else -0.0015) for index in range(200)]

    result = measure(strategy, benchmark)

    assert result.measured
    assert result.bps is not None and result.bps > 0
    assert result.t_statistic is not None and abs(result.t_statistic) >= Decimal("2.0")
    assert result.sessions == 200
    assert result.first_session and result.last_session


def test_the_excess_is_the_spread_not_the_strategys_own_return() -> None:
    """A strategy that merely rode a rising market has no excess.

    Both legs gain 40% over the window. A measure that looked at the strategy alone would report
    a large edge; the paired difference reports none, because there is none.
    """
    path = [0.002] * 170
    result = measure(path, path)

    assert not result.measured, "riding the benchmark is not beating it"


def test_underperformance_is_reported_rather_than_hidden() -> None:
    """A negative excess is a finding. Suppressing it would make the measure one-directional,
    and a capacity ladder that only ever heard good news is not a control.
    """
    strategy = [(0.002 if index % 3 else -0.0015) - 0.001 for index in range(200)]
    benchmark = [(0.002 if index % 3 else -0.0015) for index in range(200)]

    result = measure(strategy, benchmark)

    assert result.measured
    assert result.bps is not None and result.bps < 0


def test_only_sessions_present_on_both_sides_are_paired() -> None:
    """A day with equity and no benchmark bar, or the reverse, cannot contribute a difference."""
    equity = series([0.001] * 200)
    benchmark = closes(series([0.0005] * 200))
    thinned = {day: value for index, (day, value) in enumerate(benchmark.items()) if index % 2}

    result = measure_forward_excess(
        strategy_id="adaptive-momentum",
        benchmark_symbol="SPY",
        equity=equity,
        benchmark_closes=thinned,
    )

    assert result.sessions < 200
    assert result.sessions > 0


def test_a_return_is_keyed_to_the_session_it_ends_on() -> None:
    """Keying by the earlier session would shift both legs by a day.

    Tested against the helper directly, because at the level of the whole measurement it is not
    decidable: shifting *both* legs identically leaves the spread unchanged, so a test using the
    same path on each side passes whichever key is used. My first version did exactly that and
    the mutation came back green.

    The property only has content per-series: a return of +10% between Monday and Tuesday belongs
    to Tuesday, because that is the interval it describes. Getting this wrong is a look-ahead in
    one direction and a lag in the other, and it survives review easily because the number it
    produces looks entirely plausible.
    """
    from quantbot.research.forward_excess import _daily_returns

    monday, tuesday = date(2026, 1, 5), date(2026, 1, 6)
    returns = _daily_returns([(monday, Decimal("100")), (tuesday, Decimal("110"))])

    assert set(returns) == {tuesday}, "the return belongs to the day it ended on"
    assert returns[tuesday] == Decimal("0.1")


def test_identical_paths_leave_no_spread_to_test() -> None:
    """A strategy that tracked its benchmark exactly is a real outcome and an untestable one."""
    path = [0.01, -0.02, 0.015, -0.005] * 50

    assert measure(path, path).unavailable is ExcessUnavailable.NO_VARIANCE


@pytest.mark.parametrize("threshold", [Decimal("2.0"), Decimal("4.0")])
def test_a_higher_bar_refuses_more(threshold: Decimal) -> None:
    """The threshold is a `PROJECT_DEFAULT`, and raising it must only ever refuse more.

    A parameter that could let a weaker result through when tightened would mean the direction
    of the control was not what it appeared.
    """
    strategy = [0.0004 + (0.002 if index % 3 else -0.0015) for index in range(200)]
    benchmark = [(0.002 if index % 3 else -0.0015) for index in range(200)]

    result = measure(strategy, benchmark, minimum_abs_t=threshold)

    if result.measured:
        assert result.t_statistic is not None and abs(result.t_statistic) >= threshold
