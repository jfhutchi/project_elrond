"""Scoped immutable input replay for the amended fixed development procedure."""

from datetime import timedelta

import pytest
from tests.fault.paper_core.test_learning_temporal import replay_inputs
from tests.fixtures.core_m4 import ExecutionClock

from quantbot.paper_core.contracts import LearningCalendar
from quantbot.paper_core.learning import consider_learning
from quantbot.strategy import XNYSSession, XNYSSessionSequence


def daily_inputs(tmp_path, *, gross=False):
    db, store, old, cutoff = replay_inputs(tmp_path)
    policy = old.model_copy(
        update={"procedure_version": "daily-all-history-walk-forward-v2", "max_sessions": None}
    )
    if gross:
        from quantbot.paper_core.contracts import LearningPolicy, ModelArtifact

        policy = LearningPolicy(
            parent_model=ModelArtifact(feature_version="five-minute-price-alpha-v1"),
            target_contract="owned-executed-paper-gross-return-v1",
            procedure_version="daily-paper-exploration-walk-forward-v3",
        )
    from sqlalchemy import select

    from quantbot.paper_core.contracts import Observation
    from quantbot.paper_core.store import table

    with db.transaction() as session:
        observations = tuple(
            Observation.model_validate_json(p)
            for p in session.execute(select(table("core_observations").c.payload)).scalars()
        )
    closes = sorted({o.session_close for o in observations})
    calendar = LearningCalendar(
        sequence=XNYSSessionSequence(
            calendar="XNYS",
            sessions=tuple(
                XNYSSession(
                    session_date=c.date(), open_at=c - timedelta(hours=6, minutes=30), close_at=c
                )
                for c in closes
            ),
        )
    )
    return db, store, policy, cutoff, calendar


def test_daily_procedure_five_past_only_folds_then_all_history_fit(tmp_path):
    db, store, policy, cutoff, calendar = daily_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    try:
        receipt = consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
        )
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert receipt.reason == "PASSED_FIXED_PROCEDURE_GATES"
        assert len(receipt.final_input_ids) == 40
        assert len(receipt.fold_results) == 5
        assert [len(f.training_ids) for f in receipt.fold_results] == [30, 32, 34, 36, 38]
        assert sum(len(f.validation_ids) for f in receipt.fold_results) == 10
        assert all(
            all(t <= f.cutoff for t in f.scoring_available_at) is False
            for f in receipt.fold_results
        )
        assert all(f.scores.incumbent_bps == (0, 0) for f in receipt.fold_results)
        assert receipt.final_validation_absent_reason == "FINAL_REFIT_HAS_NO_INDEPENDENT_HOLDOUT"
        assert receipt.activation_at is None
        print("M4_DAILY_SCOPED:" + receipt.model_dump_json())
    finally:
        db.close()


def test_daily_policy_requires_calendar_and_ten_completed_sessions(tmp_path):
    db, store, policy, cutoff, calendar = daily_inputs(tmp_path)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    try:
        absent = consider_learning(store, policy, cutoff=cutoff, execution_clock=clock)
        assert (
            absent.reason == "MISSING_VERIFIED_SESSION_SEQUENCE" and absent.trial_charge_id is None
        )
        early = consider_learning(
            store,
            policy,
            cutoff=calendar.sequence.sessions[-2].close_at,
            calendar=calendar,
            execution_clock=clock,
        )
        assert early.reason == "TEN_COMPLETED_SESSION_WARMUP" and early.trial_charge_id is None
    finally:
        db.close()


@pytest.mark.parametrize("asof", [False, True])
def test_later_lot_conflict_excludes_stale_complete_pointer_metadata_only(
    tmp_path, monkeypatch, asof
):
    import json

    from sqlalchemy import select

    from quantbot.paper_core import learning
    from quantbot.paper_core.store import table
    from quantbot.storage.schema import incidents

    db, store, policy, cutoff, calendar = daily_inputs(tmp_path)
    try:
        with db.transaction() as session:
            row = session.execute(select(table("core_outcome_index"))).mappings().first()
            session.execute(
                incidents.insert().values(
                    incident_id="scoped-later-conflict",
                    severity="ERROR",
                    kind="CORE_EVIDENCE_CONFLICT",
                    message="synthetic source conflict",
                    occurred_at=cutoff.isoformat(),
                    detail_json=json.dumps({"lot_ids": [row["lot_id"]]}),
                )
            )
            assert row["status"] == "COMPLETE"
            selected, excluded, _ = learning._select(
                session,
                policy,
                calendar.sequence.sessions[-1].open_at if asof else cutoff,
                execution_now=cutoff + timedelta(days=2),
            )
            assert row["outcome_id"] not in {r["outcome_id"] for r in selected}
            assert (row["outcome_id"], "LOT_EVIDENCE_QUARANTINED") in excluded
            assert len(selected) == (37 if asof else 39)
    finally:
        db.close()


def test_modern_default_and_legacy_new_fit_refusal(tmp_path):
    from quantbot.paper_core.contracts import LearningPolicy

    assert LearningPolicy().procedure_version == "daily-all-history-walk-forward-v2"
    assert LearningPolicy().max_sessions is None
    db, store, old, cutoff = replay_inputs(tmp_path)
    try:
        receipt = consider_learning(
            store, old, cutoff=cutoff, execution_clock=ExecutionClock(cutoff)
        )
        assert receipt.reason == "LEGACY_POLICY_REPLAY_ONLY" and receipt.trial_charge_id is None
    finally:
        db.close()


def test_asof_fold_uses_original_cost_revision_final_refit_uses_late_revision(tmp_path):
    from decimal import Decimal

    from sqlalchemy import select

    from quantbot.paper_core.contracts import RoundTrip
    from quantbot.paper_core.store import put, table

    db, store, policy, cutoff, calendar = daily_inputs(tmp_path)
    try:
        with db.transaction() as session:
            row = (
                session.execute(
                    select(table("core_outcome_index")).order_by(
                        table("core_outcome_index").c.source_start
                    )
                )
                .mappings()
                .first()
            )
            old = RoundTrip.model_validate_json(
                session.execute(
                    select(table("core_outcome_revisions").c.payload).where(
                        table("core_outcome_revisions").c.id == row["outcome_id"]
                    )
                ).scalar_one()
            )
            revised = old.model_copy(
                update={
                    "recorded_at_utc": cutoff,
                    "available_at": cutoff,
                    "revision": 2,
                    "supersedes": old.id,
                    "net_pnl": old.net_pnl - Decimal(".01"),
                    "actual_fees": Decimal(".01"),
                }
            )
            put(session, "core_outcome_revisions", revised.id, revised)
            session.execute(
                table("core_outcome_index")
                .update()
                .where(table("core_outcome_index").c.lot_id == old.lot_id)
                .values(outcome_id=revised.id, available_at=cutoff.isoformat())
            )
        receipt = consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=ExecutionClock(cutoff)
        )
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert all(
            old.id in f.training_ids and revised.id not in f.training_ids
            for f in receipt.fold_results
        )
        assert revised.id in receipt.final_input_ids and old.id not in receipt.final_input_ids
        assert len(receipt.final_input_ids) == 40
        print("M4_DAILY_ASOF:" + receipt.model_dump_json())
    finally:
        db.close()


def test_quiet_completed_session_is_unchanged_without_second_charge(tmp_path, monkeypatch):
    from sqlalchemy import select
    from tests.fixtures.core_m4 import learning_calendar

    from quantbot.paper_core.store import table

    db, store, policy, cutoff, _ = daily_inputs(tmp_path)
    calendar = learning_calendar()
    try:
        first = consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=ExecutionClock(cutoff)
        )
        quiet = calendar.sequence.sessions[10].close_at
        second = consider_learning(
            store, policy, cutoff=quiet, calendar=calendar, execution_clock=ExecutionClock(quiet)
        )
        assert first.status == "AWAITING_ACTIVATION"
        assert second.status == "NO_CHANGE" and second.reason == "SESSION_BUDGET_OR_INPUT_REUSED"
        assert second.completed_session == calendar.sequence.sessions[10].session_date
        assert second.trial_charge_id is None
        with db.transaction() as session:
            assert len(session.execute(select(table("core_learning_attempts"))).all()) == 1
        print("M4_DAILY_QUIET:" + second.model_dump_json())
    finally:
        db.close()


@pytest.mark.parametrize(
    "variant",
    ["valid", "fit-future", "valid-no-change", "ack-future", "ack-wrong-model", "target-mismatch"],
)
def test_exact_historical_predictive_availability_and_acknowledgement(tmp_path, variant):
    from sqlalchemy import select

    from quantbot.paper_core import learning
    from quantbot.paper_core.contracts import Decision, LearningReceipt
    from quantbot.paper_core.store import put, table

    db, store, policy, cutoff, calendar = daily_inputs(tmp_path)
    try:
        with db.transaction() as session:
            rows, _, _ = learning._select(session, policy, cutoff, execution_now=cutoff)
            training = learning._materialize(session, rows[:30])
            row = dict(rows[30])
            original = Decision.model_validate_json(
                session.execute(
                    select(table("core_decisions").c.payload).where(
                        table("core_decisions").c.id == row["lot_id"]
                    )
                ).scalar_one()
            )
            model = learning.fit_candidate(
                training,
                policy.parent_model,
                policy,
                run_id="scoped-historical-fit",
                sample_weights=(2,) * 30,
            )
            available = original.recorded_at_utc + timedelta(
                seconds=1 if variant == "fit-future" else -60
            )
            fit = LearningReceipt(
                recorded_at_utc=available,
                execution_clock_domain=ExecutionClock.execution_domain,
                started_at=available,
                completed_at=available,
                available_at=available,
                run_id=model.learning_receipt_id,
                cutoff=calendar.sequence.sessions[4].close_at,
                parent_model_id=policy.parent_model.id,
                resulting_model_id=model.id,
                candidate_model_id=model.id,
                policy=policy,
                considered_ids=model.input_ids,
                exclusions=(),
                training_ids=model.input_ids,
                split_digest="scoped-historical-fit",
                status="AWAITING_ACTIVATION",
                reason="SCOPED_SYNTHETIC_PREDICTIVE_PROVENANCE",
            )
            put(session, "core_models", model.id, model)
            learning._save_receipt(session, fit)
            ack = fit
            if "ack-" in variant or variant == "valid-no-change":
                ack_at = original.recorded_at_utc + timedelta(
                    seconds=1 if variant == "ack-future" else -30
                )
                ack = LearningReceipt(
                    recorded_at_utc=ack_at,
                    execution_clock_domain=ExecutionClock.execution_domain,
                    started_at=ack_at,
                    completed_at=ack_at,
                    available_at=ack_at,
                    run_id="scoped-historical-ack",
                    cutoff=fit.cutoff,
                    parent_model_id=model.id,
                    resulting_model_id=policy.parent_model.id
                    if variant == "ack-wrong-model"
                    else model.id,
                    policy=policy.model_copy(update={"parent_model": model}),
                    considered_ids=model.input_ids,
                    exclusions=(),
                    split_digest="scoped-ack",
                    status="NO_CHANGE",
                    reason="SCOPED_UNCHANGED",
                )
                learning._save_receipt(session, ack)
            decision = original.model_copy(
                update={"model_id": model.id, "learning_receipt_id": ack.id}
            )
            put(session, "core_decisions", decision.id, decision)
            row["lot_id"] = decision.id
            if variant == "target-mismatch":
                with pytest.raises(ValueError, match="INCOMPATIBLE_HISTORICAL_TARGET"):
                    learning._historical_comparator(
                        session,
                        row,
                        ExecutionClock.execution_domain,
                        "owned-executed-paper-gross-return-v1",
                    )
            elif variant in ("valid", "valid-no-change"):
                result = learning._historical_comparator(
                    session, row, ExecutionClock.execution_domain
                )
                assert result[0].id == model.id
                assert (
                    learning.predict_bps(result[0], (dict(training[0].observation.features),))[0]
                    > 10
                )
                assert result[2] == fit.id and result[3] == ack.id
                assert result[5] == fit.model_dump_json() and result[6] == ack.model_dump_json()
            else:
                with pytest.raises(
                    ValueError,
                    match="HISTORICAL_(MODEL_UNAVAILABLE|ACKNOWLEDGEMENT_UNAVAILABLE|ACKNOWLEDGEMENT)",
                ):
                    learning._historical_comparator(session, row, ExecutionClock.execution_domain)
    finally:
        db.close()


@pytest.mark.parametrize("gross", [False, True])
def test_old_history_beyond_sixty_sessions_affects_weighted_fit_and_quiet_holiday_recency(
    tmp_path, gross
):
    from datetime import UTC, datetime

    from quantbot.paper_core import learning
    from quantbot.paper_core.learning_procedure import raw_weights

    db, store, policy, cutoff, _ = daily_inputs(tmp_path, gross=gross)
    # Explicit synthetic session list: includes quiet sessions, skips Labor Day and weekends.
    months = {
        7: (30, 31),
        8: (3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 17, 18, 19, 20, 21, 24, 25, 26, 27, 28, 31),
        9: (1, 2, 3, 4, 8, 9, 10, 11, 14, 15, 16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30),
        10: (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30),
    }
    opens = tuple(
        datetime(2026, m, d, 13, 30, tzinfo=UTC) for m, days in months.items() for d in days
    )
    calendar = LearningCalendar(
        sequence=XNYSSessionSequence(
            calendar="XNYS",
            sessions=tuple(
                XNYSSession(
                    session_date=s.date(), open_at=s, close_at=s + timedelta(hours=6, minutes=30)
                )
                for s in opens
            ),
        )
    )
    try:
        with db.transaction() as session:
            rows, _, _ = learning._select(session, policy, cutoff, execution_now=cutoff)
            episodes = learning._materialize(session, rows)
        original = episodes[0]
        delta = calendar.sequence.sessions[0].open_at - calendar.sequence.sessions[-13].open_at
        obs = original.observation.model_copy(
            update={
                "recorded_at_utc": original.observation.recorded_at_utc + delta,
                "source_start": original.observation.source_start + delta,
                "source_end": original.observation.source_end + delta,
                "available_at": original.observation.available_at + delta,
                "dependency_available_at": tuple(
                    t + delta if t else None for t in original.observation.dependency_available_at
                ),
                "session_close": original.observation.session_close + delta,
            }
        )
        old = original.model_copy(
            update={
                "observation": obs,
                "session": opens[0].date(),
                "outcome": original.outcome.model_copy(
                    update={
                        "source_start": obs.source_start,
                        "source_end": original.outcome.source_end + delta,
                        "available_at": original.outcome.available_at + delta,
                        "net_pnl": -original.outcome.net_pnl,
                        "gross_pnl": -original.outcome.gross_pnl,
                    }
                ),
            }
        )
        final = (old, *episodes)
        fit_cutoff = calendar.sequence.sessions[-1].close_at
        weights = raw_weights(final, calendar, fit_cutoff)
        assert len(calendar.sequence.sessions) > 60 and weights[0] == 1
        assert set(weights) == {1, 2} and max(weights) / min(weights) == 2
        candidate = learning.fit_candidate(
            final, policy.parent_model, policy, sample_weights=weights
        )
        recent_only = learning.fit_candidate(
            episodes, policy.parent_model, policy, sample_weights=weights[1:]
        )
        assert old.outcome.id in candidate.input_ids
        assert candidate.estimator.intercept != recent_only.estimator.intercept
        assert sum(candidate.estimator.sample_weights) / len(final) == pytest.approx(1)
        # September7 is absent, so ten-session recency cannot use guessed weekdays.
        september_cutoff = calendar.sequence.sessions[len(months[7]) + len(months[8]) + 4].close_at
        assert september_cutoff.date().isoformat() == "2026-09-08"
        probes = tuple(
            original.model_copy(update={"session": datetime(2026, 8, d, tzinfo=UTC).date()})
            for d in (24, 25)
        )
        assert raw_weights(probes, calendar, september_cutoff) == (1, 2)
        print(
            "M4_DAILY_OLD_HISTORY:"
            + __import__("json").dumps(
                {
                    "calendar": calendar.model_dump(mode="json"),
                    "input": [e.model_dump(mode="json") for e in final],
                    "raw_weights": weights,
                    "candidate": candidate.model_dump(mode="json"),
                    "recent_only": recent_only.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()


@pytest.mark.parametrize("role", ["PROTECTED_EVALUATION", "FORWARD_PAPER", "DECLARED"])
@pytest.mark.parametrize("gross", [False, True])
def test_daily_holdout_canary_covers_folds_and_final_without_materialization(
    tmp_path, monkeypatch, role, gross
):
    import json

    from tests.unit.research.test_registry import make_draft, register, window

    from quantbot.paper_core import learning
    from quantbot.research.registry import DataRole, HypothesisRegistry
    from quantbot.storage.schema import learner_datasets

    db, store, policy, cutoff, calendar = daily_inputs(tmp_path, gross=gross)
    clock = ExecutionClock(cutoff + timedelta(days=2))
    try:
        with db.transaction() as session:
            if role == "DECLARED":
                session.execute(
                    learner_datasets.insert().values(
                        dataset_id="scoped-daily-hold",
                        document_hash="d" * 64,
                        document_json=json.dumps(
                            {
                                "holdout_window": {
                                    "dataset": policy.dataset,
                                    "start": "2026-10-14",
                                    "end": "2026-10-27",
                                }
                            }
                        ),
                    )
                )
            else:
                register(
                    HypothesisRegistry(session),
                    make_draft(
                        windows=(
                            window(
                                DataRole(role), "2026-10-14", "2026-10-27", dataset=policy.dataset
                            ),
                        )
                    ),
                    now=clock.at,
                )

        def forbidden(*args, **kwargs):
            raise AssertionError("DAILY_PROTECTED_LABELS_MATERIALIZED")

        monkeypatch.setattr(learning, "_materialize", forbidden)
        receipt = consider_learning(
            store,
            policy,
            cutoff=cutoff + timedelta(days=365),
            calendar=calendar,
            execution_clock=clock,
        )
        assert receipt.status == "NO_CHANGE" and receipt.trial_charge_id is None
        assert len(receipt.exclusions) == 40
    finally:
        db.close()


def test_new_conflict_at_data_boundary_refuses_without_hydration(tmp_path, monkeypatch):
    import json

    from quantbot.paper_core import learning
    from quantbot.storage.schema import incidents

    db, store, policy, cutoff, calendar = daily_inputs(tmp_path)
    original = learning._select
    calls = 0
    try:

        def conflict_after_final_fold(session, *args, **kwargs):
            nonlocal calls
            selected = original(session, *args, **kwargs)
            calls += 1
            if calls == 6:
                session.execute(
                    incidents.insert().values(
                        incident_id="scoped-boundary-conflict",
                        severity="ERROR",
                        kind="CORE_EVIDENCE_CONFLICT",
                        message="late synthetic conflict",
                        occurred_at=cutoff.isoformat(),
                        detail_json=json.dumps({"lot_ids": [selected[0][0]["lot_id"]]}),
                    )
                )
            return selected

        monkeypatch.setattr(learning, "_select", conflict_after_final_fold)
        monkeypatch.setattr(
            learning,
            "_materialize",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("QUARANTINED_MATERIALIZED")),
        )
        receipt = consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=ExecutionClock(cutoff)
        )
        assert (
            receipt.status == "FIT_FAILED"
            and receipt.reason == "LOT_QUARANTINED_BEFORE_MATERIALIZATION"
        )
        assert receipt.trial_charge_id is not None
    finally:
        db.close()


@pytest.mark.parametrize("gross", [False, True])
def test_future_cutoff_refuses_before_materialization_fit_and_charge(tmp_path, monkeypatch, gross):
    from sqlalchemy import select
    from tests.fixtures.core_m4 import learning_calendar

    from quantbot.paper_core import learning
    from quantbot.paper_core.store import table
    from quantbot.research.registry import HypothesisRegistry

    db, store, policy, executed_at, _ = daily_inputs(tmp_path, gross=gross)
    calendar = learning_calendar()
    requested = calendar.sequence.sessions[-1].close_at
    clock = ExecutionClock(executed_at)
    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("FUTURE_CUTOFF_DATA_OR_FIT_ACCESSED")

    monkeypatch.setattr(learning, "_materialize", forbidden)
    monkeypatch.setattr(learning, "_fit_subprocess", forbidden)
    try:
        receipt = consider_learning(
            store, policy, cutoff=requested, calendar=calendar, execution_clock=clock
        )
        assert (
            receipt.status == "NO_CHANGE"
            and receipt.reason == "FUTURE_CUTOFF_BEYOND_EXECUTION_TIME"
        ), receipt.reason
        assert not touched
        assert receipt.cutoff == requested > executed_at
        assert (
            receipt.recorded_at_utc == receipt.completed_at == receipt.available_at == executed_at
        )
        assert receipt.parent_model_id == receipt.resulting_model_id == policy.parent_model.id
        assert receipt.trial_charge_id is None and receipt.activation_at is None
        clock.advance(60)
        assert (
            consider_learning(
                store, policy, cutoff=requested, calendar=calendar, execution_clock=clock
            )
            == receipt
        )
        with db.transaction() as session:
            assert HypothesisRegistry(session).learner_trials_spent() == 0
            assert not session.execute(select(table("core_learning_inputs"))).all()
        print("M4_FIX2_FUTURE_REFUSAL:" + receipt.model_dump_json())
    finally:
        db.close()


def test_future_refusal_recovers_at_equal_execution_cutoff_without_rewriting_receipt(tmp_path):
    from sqlalchemy import select
    from tests.fixtures.core_m4 import learning_calendar

    from quantbot.paper_core.contracts import LearningReceipt
    from quantbot.paper_core.store import table
    from quantbot.research.registry import HypothesisRegistry

    db, store, policy, executed_at, _ = daily_inputs(tmp_path)
    calendar = learning_calendar()
    requested = calendar.sequence.sessions[-1].close_at
    clock = ExecutionClock(executed_at)
    try:
        refusal = consider_learning(
            store, policy, cutoff=requested, calendar=calendar, execution_clock=clock
        )
        assert refusal.reason == "FUTURE_CUTOFF_BEYOND_EXECUTION_TIME"
        clock.advance((requested - clock.at).total_seconds())
        accepted = consider_learning(
            store, policy, cutoff=requested, calendar=calendar, execution_clock=clock
        )
        assert accepted.status == "AWAITING_ACTIVATION", accepted.reason
        assert accepted.run_id != refusal.run_id and accepted.id != refusal.id
        assert accepted.cutoff == accepted.available_at == requested
        assert accepted.calendar_id == refusal.calendar_id == calendar.id
        assert len(accepted.final_input_ids) == 40
        assert (
            accepted.final_raw_weights.count(1) == 18 and accepted.final_raw_weights.count(2) == 22
        )
        assert store.read("core_learning_runs", refusal.id, LearningReceipt) == refusal
        assert (
            consider_learning(
                store, policy, cutoff=requested, calendar=calendar, execution_clock=clock
            )
            == accepted
        )
        with db.transaction() as session:
            assert HypothesisRegistry(session).learner_trials_spent() == 1
            assert len(session.execute(select(table("core_learning_attempts"))).all()) == 1
        print(
            "M4_FIX2_EQUAL_RECOVERY:"
            + __import__("json").dumps(
                {
                    "refusal": refusal.model_dump(mode="json"),
                    "accepted": accepted.model_dump(mode="json"),
                    "calendar": calendar.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()


@pytest.mark.parametrize("historical", [False, True])
def test_equal_and_historical_cutoffs_keep_completed_calendar_weights(tmp_path, historical):
    import json
    from pathlib import Path

    from tests.fixtures.core_m4 import learning_calendar

    from quantbot.paper_core.contracts import ModelArtifact

    db, store, policy, cutoff, _ = daily_inputs(tmp_path)
    calendar = learning_calendar()
    clock = ExecutionClock(calendar.sequence.sessions[-1].close_at if historical else cutoff)
    try:
        receipt = consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
        )
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert receipt.final_raw_weights == (2,) * 40
        assert receipt.completed_session == calendar.sequence.sessions[9].session_date
        assert receipt.available_at == clock.at
        frozen = json.loads(
            (
                Path(__file__).parents[3] / "docs/evidence/paper-core/m4/fix1/summary.json"
            ).read_text()
        )
        assert receipt.resulting_model_id == ModelArtifact.model_validate(frozen["candidate"]).id
        assert receipt.input_export_id == frozen["input_export_id"]
        assert (
            consider_learning(
                store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
            )
            == receipt
        )
        print(
            "M4_FIX2_ORDINARY:"
            + __import__("json").dumps(
                {
                    "historical": historical,
                    "receipt": receipt.model_dump(mode="json"),
                    "calendar": calendar.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()
