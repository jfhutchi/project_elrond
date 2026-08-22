"""Catching a generated signal that reads the future (#7, #8).

The defect this exists for is real and observed: the first generated experiment, written by
`llama3.1:8b` and run under the WSL sandbox, computed its moving average from the *final* bar of
the series for every date. Every signal in that run knew the whole future, the output was
perfectly well-formed, and nothing caught it.

The honest signal and the cheating signal below are the same shape and differ only in what they
read, which is the point: no inspection of the output can tell them apart. Only re-running can.
"""

from __future__ import annotations

from collections.abc import Mapping

from quantbot.research.causality import (
    check_causality,
    probe_dates,
)

DATES = [f"2026-01-{day:02d}" for day in range(1, 21)]


def honest_signal(visible: list[str]) -> dict[str, dict[str, str]]:
    """Each date's weight depends only on data up to that date. A 3-day mean of the index."""
    signal: dict[str, dict[str, str]] = {}
    for index, day in enumerate(visible):
        if index < 3:
            continue
        window = [float(i) for i in range(index - 3, index)]
        signal[day] = {"SPY": f"{sum(window) / len(window) / 100:.6f}"}
    return signal


def cheating_signal(visible: list[str]) -> dict[str, dict[str, str]]:
    """Every date's weight is computed from the *last* bar available.

    This is the defect the 8B model actually produced, reduced to its essence: the aggregate is
    taken over the whole series rather than over the history each date could see. On the full
    run it looks like any other signal.
    """
    if len(visible) < 4:
        return {}
    final = float(len(visible) - 1)
    return {day: {"SPY": f"{final / 100:.6f}"} for day in visible[3:]}


def runner(kind) -> Mapping[str, Mapping[str, str]]:
    def run_to(cut: str) -> Mapping[str, Mapping[str, str]]:
        return kind(DATES[: DATES.index(cut) + 1])

    return run_to


def test_an_honest_signal_passes_the_probe() -> None:
    """Without this, a probe that failed everything would look like a working detector."""
    report = check_causality(runner(honest_signal), honest_signal(DATES))

    assert report.violations == (), [str(v) for v in report.violations]
    assert report.probed, "a clean report from zero probes is not evidence"
    assert report.clean is True


def test_a_signal_computed_from_the_final_bar_is_caught() -> None:
    """The observed defect. Its output is well-formed, so only re-running can find it."""
    report = check_causality(runner(cheating_signal), cheating_signal(DATES))

    assert report.violations, "a signal that reads the whole series must not pass"
    assert report.clean is False
    # Named precisely enough to act on: which date, which symbol, both values.
    first = report.violations[0]
    assert first.symbol == "SPY"
    assert first.with_full_history != first.with_history_to_date
    assert "with data only up to that date" in str(first)


def test_a_clean_report_from_nothing_probed_is_not_clean() -> None:
    """An empty violation tuple from zero probes and from three probes are different facts.

    This is the failure mode every report in this project guards against: a check that could not
    run looking exactly like a check that passed.
    """

    def never_produces(_cut: str) -> Mapping[str, Mapping[str, str]]:
        return {}

    report = check_causality(never_produces, {day: {"SPY": "0.5"} for day in DATES})

    assert report.violations == ()
    assert report.probed == ()
    assert report.unchecked, "dates that could not be compared must be recorded"
    assert report.clean is False, "nothing probed is not the same as nothing wrong"


def test_probe_dates_never_picks_the_last_date() -> None:
    """Truncating at the last date is not a truncation.

    The run would see the same history and pass trivially -- a probe that cannot fail, which is
    worse than no probe because it contributes a passing check.
    """
    chosen = probe_dates(DATES)

    assert chosen, "some dates must be probed"
    assert DATES[-1] not in chosen
    assert DATES[0] not in chosen, "the warm-up window produces nothing and wastes a run"
    assert len(set(chosen)) == len(chosen), "duplicate cut points waste sandboxed runs"


def test_a_series_too_short_to_truncate_probes_nothing_rather_than_pretending() -> None:
    assert probe_dates(["2026-01-01"]) == ()
    assert probe_dates(["2026-01-01", "2026-01-02"]) == ()


def test_equivalent_decimal_representations_are_not_violations() -> None:
    """"0.5" and "0.50" are the same weight. Treating them as different would cry wolf."""
    full = {day: {"SPY": "0.5"} for day in DATES}

    def padded(cut: str) -> Mapping[str, Mapping[str, str]]:
        return {day: {"SPY": "0.500000"} for day in DATES[: DATES.index(cut) + 1]}

    report = check_causality(padded, full)

    assert report.violations == ()
    assert report.clean is True


def test_a_symbol_appearing_only_in_one_run_is_a_violation() -> None:
    """Taking a position that vanishes when the future is hidden is exactly the defect.

    A weight present with the whole series and absent without it is not a rounding difference --
    it is an entry the signal could not have made on the day.
    """
    full = {day: {"SPY": "0.5", "QQQ": "0.5"} for day in DATES}

    def drops_qqq(cut: str) -> Mapping[str, Mapping[str, str]]:
        return {day: {"SPY": "0.5"} for day in DATES[: DATES.index(cut) + 1]}

    report = check_causality(drops_qqq, full)

    assert any(v.symbol == "QQQ" for v in report.violations), report.violations


def test_a_measurement_reports_only_probes_that_actually_ran() -> None:
    """The defect this closes: `probes_run=plan.probes`, echoed back without running anything.

    `ExperimentRunner._grade` refuses to read a number as evidence unless every required probe
    ran. Reporting the plan's own list back at it defeated that check entirely -- the one
    measurement whose code nobody reviewed was asserting that its own falsification attempts had
    happened.
    """
    import inspect

    from quantbot.research import codegen

    source = inspect.getsource(codegen.measurement_from_signal)
    assert "probes_run=plan.probes" not in source, (
        "the measurement is claiming the plan's probes ran without running them"
    )


def test_a_clean_causality_report_contributes_the_look_ahead_probe() -> None:
    """A probe that ran and passed should count; one that could not run must not."""
    from quantbot.research.codegen import LOOK_AHEAD_PROBE

    clean = check_causality(runner(honest_signal), honest_signal(DATES))
    assert clean.clean is True
    assert clean.probed

    dirty = check_causality(runner(cheating_signal), cheating_signal(DATES))
    assert dirty.clean is False
    assert dirty.violations

    # The name has to be the one the compiler requires, or the grader counts it as missing and
    # the run is INCONCLUSIVE for a reason nobody can see.
    from quantbot.research.builder import REQUIRED_PROBES

    assert LOOK_AHEAD_PROBE in REQUIRED_PROBES
