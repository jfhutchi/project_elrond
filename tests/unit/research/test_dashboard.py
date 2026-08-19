"""What the operator view must show, and the comparison it must never blur."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantbot.research.dashboard import (
    BudgetPanel,
    EvidencePanel,
    EvidenceState,
    Metric,
    Provenance,
    active_count,
    attention,
    luck_bar_at,
    pipeline_counts,
)
from quantbot.research.director import ResearchTask, TaskState

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def task(task_id: str, state: TaskState) -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        state=state,
        question=f"question for {task_id}",
        family_id="trend-following",
        created_at=NOW,
        updated_at=NOW,
    )


def budget(**overrides: object) -> BudgetPanel:
    fields: dict[str, object] = {
        "budget_key": "dataset:sip-us-equities-daily",
        "trials_spent": 68,
        "trials_cap": 100,
    }
    fields.update(overrides)
    return BudgetPanel(**fields)


def test_forward_evidence_always_carries_its_denominator() -> None:
    """One trading day against a thirty-day window, and the view cannot hide the thirty."""
    panel = EvidencePanel(
        forward_days=1,
        forward_trades=0,
        research_cycles=17,
        hypotheses_registered=24,
        hypotheses_refuted=24,
        hypotheses_underpowered=0,
        hypotheses_survived=0,
    )
    assert panel.qualification_fraction == "1 of 30 days, 0 of 30 trades"
    assert not panel.qualified

    # Seventeen cycles is a bigger number than one day, and the panel refuses to let that
    # read as progress: the two live in separate fields with separate evidence states.
    forward, research, _ = panel.metrics()
    assert forward.state is EvidenceState.OBSERVED
    assert research.state is EvidenceState.BACKTEST
    assert "of 30 days" in forward.value
    assert not hasattr(panel, "combined")
    assert not hasattr(panel, "total_evidence")


def test_a_window_that_has_not_happened_is_labelled_not_yet_observed() -> None:
    empty = EvidencePanel(
        forward_days=0,
        forward_trades=0,
        research_cycles=17,
        hypotheses_registered=24,
        hypotheses_refuted=24,
        hypotheses_underpowered=0,
        hypotheses_survived=0,
    )
    assert empty.metrics()[0].state is EvidenceState.NOT_YET_OBSERVED


def test_a_generated_number_can_never_be_displayed_as_a_measurement() -> None:
    """No metric shown as paper evidence may be sourced only from an LLM narrative."""
    for state in (EvidenceState.OBSERVED, EvidenceState.BACKTEST):
        with pytest.raises(ValueError, match="not a measurement"):
            Metric(
                label="estimated Sharpe",
                value="1.4",
                state=state,
                provenance=Provenance.MODEL_NARRATIVE,
            )

    # It may still be shown, labelled as what it is.
    inferred = Metric(
        label="agent commentary",
        value="the rotation looks promising",
        state=EvidenceState.INFERRED,
        provenance=Provenance.MODEL_NARRATIVE,
    )
    assert inferred.state is EvidenceState.INFERRED


def test_observed_evidence_has_exactly_one_source() -> None:
    with pytest.raises(ValueError, match="exactly one source"):
        Metric(
            label="forward days",
            value="30",
            state=EvidenceState.OBSERVED,
            provenance=Provenance.EXPERIMENT_MANIFEST,
        )


def test_the_statistical_budget_shows_what_spending_it_would_cost() -> None:
    """The number worth seeing early, not after it has been spent."""
    panel = budget(trials_spent=68, trials_cap=1000)
    assert panel.luck_bar == Decimal("2.90")
    assert panel.bar_at_cap == Decimal("3.72")
    assert panel.trials_remaining == 932
    assert not panel.exhausted

    # SPY buy-and-hold scores 0.90 on this window; a bar of 3.72 needs a Sharpe above it.
    assert panel.bar_at_cap > panel.luck_bar

    spent = budget(trials_spent=100, trials_cap=100)
    assert spent.exhausted
    assert spent.trials_remaining == 0


def test_the_luck_bar_matches_the_figures_the_project_already_quotes() -> None:
    assert luck_bar_at(24) == Decimal("2.52")
    assert luck_bar_at(100) == Decimal("3.03")
    assert luck_bar_at(1000) == Decimal("3.72")
    assert luck_bar_at(1) == Decimal("1.96")


def test_the_default_view_is_what_is_blocked_not_what_ran() -> None:
    """A research loop generates uninteresting activity by design; that is scrollback."""
    tasks = [
        task("T-1", TaskState.EXPERIMENTING),
        task("T-2", TaskState.CRITIQUE),
        task("T-3", TaskState.BLOCKED),
        task("T-4", TaskState.SURVIVED),
        task("T-5", TaskState.REFUTED),
    ]
    alerts = attention(tasks, [budget()])

    kinds = [alert.kind for alert in alerts]
    assert kinds == ["BLOCKED", "SURVIVED"]
    # Routine work is dropped entirely rather than ranked below the signal.
    assert not any("T-1" in alert.detail for alert in alerts)
    assert not any("T-5" in alert.detail for alert in alerts)


def test_a_halt_or_a_failed_reconciliation_outranks_everything() -> None:
    alerts = attention(
        [task("T-3", TaskState.BLOCKED)],
        [budget(trials_spent=100, trials_cap=100)],
        stuck=[task("T-9", TaskState.EXPERIMENTING)],
        kill_switch_engaged=True,
        reconciliation_failed=True,
    )
    # Both are urgency 0: a halt and a failed reconciliation are equally the operator's
    # problem, so neither is ranked above the other.
    assert {alert.kind for alert in alerts[:2]} == {"FAILED", "HALTED"}
    assert {alert.urgency for alert in alerts[:2]} == {0}
    assert "EXHAUSTED" in [alert.kind for alert in alerts]
    assert "STUCK" in [alert.kind for alert in alerts]
    assert any("no amount of compute buys it back" in alert.detail for alert in alerts)


def test_a_quiet_system_produces_no_alerts_at_all() -> None:
    """Nothing to report is a valid answer, and better than a wall of activity."""
    assert attention([task("T-1", TaskState.EXPERIMENTING)], [budget()]) == ()


def test_the_pipeline_census_shows_zero_as_zero() -> None:
    counts = pipeline_counts([task("T-1", TaskState.EXPERIMENTING)])
    assert counts["EXPERIMENTING"] == 1
    assert counts["SURVIVED"] == 0
    assert counts["UNDERPOWERED"] == 0
    assert set(counts) == {state.value for state in TaskState}
    assert active_count([task("T-1", TaskState.EXPERIMENTING), task("T-2", TaskState.REFUTED)]) == 1
