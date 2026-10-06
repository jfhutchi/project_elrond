"""Deterministic accounting; synthetic labels never become real PAPER profits."""

from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.experiment_store import ExperimentStore
from quantbot.operations.daily_performance import DailyPerformance
from quantbot.strategy import XNYSSession, XNYSSessionSequence
from tests.fixtures.adaptive_paper import OPEN, manifest, snapshot


def module():
    return import_module("quantbot.learner.experiment_evaluation")


@pytest.fixture
def context(tmp_path):
    store = ExperimentStore(tmp_path / "workflow.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    return store


def calendar(count):
    # Deliberately authoritative supplied sessions, including no inferred weekdays.
    return XNYSSessionSequence(
        calendar="XNYS",
        sessions=tuple(
            XNYSSession(
                session_date=(OPEN + timedelta(days=n)).date(),
                open_at=OPEN + timedelta(days=n),
                close_at=OPEN + timedelta(days=n, hours=6, minutes=30),
            )
            for n in range(count)
        ),
    )


def report(store, *, count, days=()):
    now = OPEN + timedelta(days=count - 1, hours=7)
    return module().evaluate_experiment(
        store,
        manifest(),
        now=now,
        sessions=calendar(count),
        calendar_coverage=(OPEN.date(), now.date()),
        account_days=days,
    )


def test_flat_days_in_denominator_and_account_not_learner_profit(context):
    days = tuple(
        DailyPerformance((OPEN + timedelta(days=n)).date(), "FINAL", Decimal(pnl))
        for n, pnl in enumerate(("10", "-3", "0"))
    )
    result = report(context, count=3, days=days)
    assert result.account_net_pnl == Decimal(7)
    assert result.profitable_days == result.losing_days == result.flat_days == 1
    assert result.profitable_day_percent == Decimal(100) / 3
    assert result.valid_performance_days == 3
    assert result.learner_realized_pnl is None
    assert not result.assessment_ready
    assert not result.live_authority


def test_missing_reset_deposit_invalid_and_duplicates_not_profit(context):
    days = (
        DailyPerformance(OPEN.date(), "MISSING", None, "no baseline"),
        DailyPerformance((OPEN + timedelta(days=1)).date(), "INVALID", None, "reset"),
        DailyPerformance((OPEN + timedelta(days=2)).date(), "FINAL", Decimal(1000)),
        DailyPerformance((OPEN + timedelta(days=2)).date(), "FINAL", Decimal(1000)),
    )
    result = report(context, count=3, days=days)
    assert result.valid_performance_days == 0
    assert result.account_net_pnl is None
    assert result.profitable_day_percent is None


def test_five_sessions_zero_eligibility_is_owned_review_not_new_approval(context):
    result = report(context, count=5)
    assert result.zero_eligibility_review_required
    assert result.next_owner == "DEVELOPMENT_OPERATOR"
    assert not result.requires_model_approval


def test_30_scheduled_sessions_stop_without_reset_or_protection_mutation(context):
    result = report(context, count=30)
    assert result.scheduled_sessions == 30
    assert not result.new_research_allowed
    assert not result.new_entries_allowed
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert context.state(manifest().experiment_id).state == "REGISTERED"
    assert result.position_protection_required


def test_unknown_calendar_cannot_infer_30_days_or_performance(context):
    result = module().evaluate_experiment(
        context, manifest(), now=OPEN + timedelta(days=100)
    )
    assert result.scheduled_sessions is None
    assert result.status == "CALENDAR_COVERAGE_REQUIRED"
    assert not result.new_entries_allowed
    assert not result.new_research_allowed


def test_forecast_gain_is_not_broker_profit_and_six_symbols_not_six_sessions(context):
    from quantbot.learner.experiment_contract import DecisionRecord
    from quantbot.learner.experiment_coordinator import ResolvedMarketLabel, feature_key
    from quantbot.learner.experiment_observation import (
        build_observation_batch,
        resolve_batch_labels,
    )

    source = snapshot()
    observation = build_observation_batch(manifest(), source, now=source.received_at)
    context.append(observation, idempotency_key="observation")
    for row in observation.features:
        context.append(
            DecisionRecord(
                experiment_id=manifest().experiment_id,
                recorded_at=source.received_at,
                model_id="a" * 64,
                features=row,
                score=0.05,
                action="BUY",
                reason="SYNTHETIC_SHADOW_SCORE",
                synthetic=True,
            ),
            idempotency_key="decision:" + feature_key(row),
        )
    target = snapshot(65)
    for label in resolve_batch_labels(observation, target, now=target.received_at):
        context.append(
            ResolvedMarketLabel(
                experiment_id=manifest().experiment_id,
                recorded_at=target.received_at,
                label=label,
                synthetic=True,
            ),
            idempotency_key="label:" + feature_key(label.features),
        )
    result = report(context, count=2)
    assert result.forecasts[0].labeled_rows == 6
    assert result.forecasts[0].independent_sessions == 1
    assert result.forecasts[0].nonoverlapping_time_blocks == 1
    assert result.account_net_pnl is result.learner_realized_pnl is None
    assert result.reference_simulated_pnl is None
    assert result.synthetic


def test_stored_calendar_gap_stays_unknown_and_conflicts_require_review(context):
    evidence = module().EvaluationCalendarEvidence(
        experiment_id=manifest().experiment_id,
        recorded_at=manifest().registered_at,
        coverage_start=OPEN.date(),
        coverage_end=(OPEN + timedelta(days=1)).date(),
        sessions=calendar(2), source_hash="a" * 64, synthetic=True,
    )
    context.append(evidence, idempotency_key="calendar-first")
    now = OPEN + timedelta(days=2, hours=7)
    result = module().evaluate_experiment(context, manifest(), now=now)
    assert result.status == "CALENDAR_COVERAGE_REQUIRED"
    changed = evidence.model_copy(update={
        "sessions": XNYSSessionSequence(calendar="XNYS", sessions=(
            evidence.sessions.sessions[0].model_copy(update={
                "close_at": evidence.sessions.sessions[0].close_at - timedelta(hours=1)
            }),
            evidence.sessions.sessions[1],
        )),
        "source_hash": "b" * 64,
    })
    context.append(changed, idempotency_key="calendar-conflict")
    with pytest.raises(ValueError, match="CALENDAR_CONFLICT"):
        module().evaluate_experiment(context, manifest(), now=now)


async def test_coordinator_stops_new_decisions_and_worker_queue_at_limit(context):
    from quantbot.learner.experiment_coordinator import coordinator_tick

    context.append(module().EvaluationCalendarEvidence(
        experiment_id=manifest().experiment_id, recorded_at=manifest().registered_at,
        coverage_start=OPEN.date(), coverage_end=(OPEN + timedelta(days=30)).date(),
        sessions=calendar(31), source_hash="a" * 64, synthetic=True,
    ), idempotency_key="whole-calendar")
    result = await coordinator_tick(
        context, manifest(), now=OPEN + timedelta(days=29, hours=7),
        observation=None, execution=None, clock_verified=True,
    )
    assert result.reason == "EVALUATION_WINDOW_COMPLETE"
    assert result.next_expected_event == "FINAL_EVIDENCE_REVIEW"
    assert result.decisions_recorded == result.intents == 0
    assert result.fit_request_ids == ()
