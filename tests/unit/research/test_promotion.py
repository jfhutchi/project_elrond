"""What a strategy has to earn, and the step this system cannot take at all."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.research.builder import OutcomeVerdict
from quantbot.research.manifest import ExecutionPath, StatisticalTest
from quantbot.research.promotion import (
    ALLOWED_PROMOTIONS,
    MINIMUM_FORWARD_DAYS,
    ForwardObservation,
    PromotionRefused,
    PromotionState,
    Stage,
    count_forward_days,
    demote,
    forward_progress,
    material_change,
    promote,
    survivor_objections,
)
from quantbot.research.registry import DataRole
from tests.unit.research.test_builder import (  # reuse the compiled plan and outcome fixtures
    outcome,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def state(stage: Stage = Stage.REGISTERED, **overrides: object) -> PromotionState:
    fields: dict[str, object] = {
        "strategy_id": "adaptive-momentum",
        "stage": stage,
        "strategy_version": "1.3.0",
        "configuration_hash": "cfg-abc",
        "reason": "fixture",
        "actor": "director",
        "updated_at": NOW,
    }
    fields.update(overrides)
    return PromotionState(**fields)


def observations(
    days: int, *, trades_each: int = 1, version: str = "1.3.0", config: str = "cfg-abc"
) -> list[ForwardObservation]:
    return [
        ForwardObservation(
            strategy_id="adaptive-momentum",
            trading_date=date(2026, 8, 19) + timedelta(days=index),
            trades=trades_each,
            role=DataRole.FORWARD_PAPER,
            strategy_version=version,
            configuration_hash=config,
        )
        for index in range(days)
    ]


def test_there_is_no_transition_into_live_trading() -> None:
    """Implemented by the absence of a destination, not by a permission check."""
    assert "LIVE" not in {stage.value for stage in Stage}
    assert ALLOWED_PROMOTIONS[Stage.LIVE_REVIEW_ELIGIBLE] == frozenset()

    eligible = state(Stage.LIVE_REVIEW_ELIGIBLE)
    assert eligible.eligible_for_human_review
    for stage in Stage:
        with pytest.raises(PromotionRefused, match="does not lead to"):
            promote(eligible, stage, actor="director", reason="go live", now=NOW)


def test_a_stage_cannot_be_skipped() -> None:
    with pytest.raises(PromotionRefused, match="does not lead to"):
        promote(state(Stage.CANDIDATE), Stage.SHADOW, actor="director", reason="x", now=NOW)
    with pytest.raises(PromotionRefused, match="does not lead to"):
        promote(
            state(Stage.RESEARCH_SURVIVOR),
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="x",
            now=NOW,
        )


def test_passing_a_pre_registered_test_is_not_enough_to_survive() -> None:
    """Cycle 11's two candidates passed their stated tests and were both wrong.

    The flaw was in the statistic, not the protocol, so robustness and regime diagnostics
    would not have caught either.
    """
    # An independent-samples test on paired data: the overnight-effect failure.
    wrong_statistic = outcome(
        plan=outcome().plan.model_copy(
            update={
                "statistics": outcome().plan.statistics.model_copy(
                    update={"test": StatisticalTest.ONE_SAMPLE_T}
                )
            }
        )
    )
    objections = survivor_objections(wrong_statistic)
    assert any("not valid for PAIRED data" in item for item in objections)
    with pytest.raises(PromotionRefused, match="not valid for PAIRED"):
        promote(
            state(),
            Stage.RESEARCH_SURVIVOR,
            actor="director",
            reason="clears the bar",
            now=NOW,
            outcome=wrong_statistic,
        )

    # A probe that objected: the cross-asset generality failure that killed vol targeting.
    probed = outcome(verdict=OutcomeVerdict.REFUTED, probes_failed=("cross-asset-generality",))
    assert any("cross-asset-generality" in item for item in survivor_objections(probed))

    # And a clean result does survive, so the gate is not simply refusing everything.
    promoted = promote(
        state(),
        Stage.RESEARCH_SURVIVOR,
        actor="director",
        reason="cleared every probe",
        now=NOW,
        outcome=outcome(),
    )
    assert promoted.stage is Stage.RESEARCH_SURVIVOR


def test_a_standalone_study_cannot_make_a_survivor() -> None:
    standalone = outcome(
        plan=outcome().plan.model_copy(
            update={"execution_path": ExecutionPath.RESEARCH_SCRIPT}
        ),
        verdict=OutcomeVerdict.INCONCLUSIVE,
    )
    assert any("standalone model" in item for item in survivor_objections(standalone))


def test_a_backtest_can_never_increment_the_forward_counter() -> None:
    """The exact failure the project goal document was written to prevent."""
    for role in (DataRole.DISCOVERY, DataRole.VALIDATION, DataRole.PROTECTED_EVALUATION):
        with pytest.raises(ValueError, match="cannot increment this counter"):
            ForwardObservation(
                strategy_id="adaptive-momentum",
                trading_date=date(2026, 8, 19),
                trades=1,
                role=role,
                strategy_version="1.3.0",
                configuration_hash="cfg-abc",
            )


def test_paper_qualification_needs_the_full_authentic_window() -> None:
    """The account holds one forward day against a thirty-day window."""
    one_day = state(Stage.PAPER_OBSERVATION)
    with pytest.raises(PromotionRefused, match="1 authentic forward days") as error:
        promote(
            one_day,
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="seventeen research cycles",
            now=NOW,
            observations=observations(1),
        )
    assert "30 and 30 required" in str(error.value)

    qualified = promote(
        one_day,
        Stage.PAPER_QUALIFIED,
        actor="director",
        reason="window complete",
        now=NOW,
        observations=observations(MINIMUM_FORWARD_DAYS),
    )
    assert qualified.stage is Stage.PAPER_QUALIFIED

    # Enough days but too few trades is still not qualified.
    with pytest.raises(PromotionRefused, match="30 authentic forward days and 0 trades"):
        promote(
            one_day,
            Stage.PAPER_QUALIFIED,
            actor="director",
            reason="days but no trading",
            now=NOW,
            observations=observations(MINIMUM_FORWARD_DAYS, trades_each=0),
        )


def test_a_material_change_restarts_the_window_by_excluding_the_old_days() -> None:
    """The observations were of a different strategy, so they are not observations of this one."""
    collected = observations(MINIMUM_FORWARD_DAYS)
    days, trades = count_forward_days(
        collected, strategy_version="1.3.0", configuration_hash="cfg-abc"
    )
    assert (days, trades) == (30, 30)

    after, _ = count_forward_days(
        collected, strategy_version="1.4.0", configuration_hash="cfg-abc"
    )
    assert after == 0

    changed = material_change(
        state(Stage.PAPER_QUALIFIED),
        strategy_version="1.4.0",
        configuration_hash="cfg-def",
        now=NOW,
    )
    assert changed.stage is Stage.RESEARCH_SURVIVOR
    assert "restarted" in changed.reason
    # An unchanged identity leaves the state exactly as it was.
    assert material_change(
        state(Stage.PAPER_QUALIFIED),
        strategy_version="1.3.0",
        configuration_hash="cfg-abc",
        now=NOW,
    ).stage is Stage.PAPER_QUALIFIED


def test_an_unresolved_integrity_violation_blocks_every_promotion() -> None:
    with pytest.raises(PromotionRefused, match="evidence-integrity violation"):
        promote(
            state(Stage.RESEARCH_SURVIVOR),
            Stage.SHADOW,
            actor="director",
            reason="looks fine",
            now=NOW,
            integrity_clear=False,
        )


def test_demotion_is_not_gated() -> None:
    """A system slow to stop trusting something is worse than one occasionally too quick."""
    dropped = demote(
        state(Stage.PAPER_QUALIFIED),
        Stage.CANDIDATE,
        actor="supervisor",
        reason="reconciliation defect found in the observation window",
        now=NOW,
    )
    assert dropped.stage is Stage.CANDIDATE

    with pytest.raises(PromotionRefused, match="is not below"):
        demote(
            state(Stage.SHADOW),
            Stage.LIVE_REVIEW_ELIGIBLE,
            actor="agent",
            reason="promote via demote",
            now=NOW,
        )


def test_forward_progress_reports_the_honest_distance() -> None:
    progress = forward_progress(
        observations(1), strategy_version="1.3.0", configuration_hash="cfg-abc"
    )
    assert progress["forward_days"] == Decimal("1")
    assert progress["days_required"] == Decimal("30")
    assert progress["forward_days"] < progress["days_required"]
