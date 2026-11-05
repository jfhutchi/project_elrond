"""V3 executed gross targets, all history, and exact activation contracts."""

import json

import pytest
from sqlalchemy import select
from tests.fault.paper_core.test_learning_daily import daily_inputs
from tests.fixtures.core_gross import gross_episodes
from tests.fixtures.core_m4 import ExecutionClock, learning_calendar

from quantbot.paper_core import learning
from quantbot.paper_core.activation import (
    ActivationAuthority,
    activate_model,
    commission_fixture_authority,
    initialize_fixture_model,
)
from quantbot.paper_core.contracts import LearningPolicy, ModelArtifact, RoundTrip
from quantbot.paper_core.learning_procedure import fit_candidate, predict_bps
from quantbot.paper_core.store import table
from quantbot.storage import StateConflictError
from quantbot.storage.schema import search_runs

GROSS = "owned-executed-paper-gross-return-v1"
V3 = "daily-paper-exploration-walk-forward-v3"


def gross_policy(**kwargs):
    return LearningPolicy(target_contract=GROSS, procedure_version=V3, **kwargs)


@pytest.mark.parametrize(
    "mode,envelope,allowed",
    [
        ("SIGNAL_OR_EXPLORE_V1", "DAILY_BOUNDED_V3", True),
        ("SIGNAL_OR_EXPLORE_V1", "DAILY_BOUNDED_V2", False),
        ("MODEL_ONLY_V1", "DAILY_BOUNDED_V3", False),
        ("MODEL_ONLY_V1", "DAILY_BOUNDED_V2", True),
        ("SIGNAL_OR_EXPLORE_V1", "REVIEW_EACH_CANDIDATE", True),
    ],
)
def test_runtime_envelope_matches_selected_learning_mode(tmp_path, mode, envelope, allowed):
    from datetime import UTC, datetime

    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.release import RuntimeConfig

    fields = dict(
        repository=str(tmp_path),
        store_path=str(tmp_path / "fixture.db"),
        store_identity="synthetic",
        expected_account_hash="a" * 64,
        holdout_source=str(tmp_path / "canonical.db"),
        learning_origin=datetime(2026, 1, 1, tzinfo=UTC),
        activation_policy=envelope,
        core=CoreConfig(
            account_id="synthetic",
            symbol="XLE",
            release_id="UNBOUND_PREPARED_RELEASE",
            clock_profile="PAPER_TIME_V2",
            selection_policy=mode,
            **({"symbols": ("XLE", "AAPL")} if mode == "SIGNAL_OR_EXPLORE_V1" else {}),
        ),
    )
    if allowed:
        assert RuntimeConfig(**fields).activation_policy == envelope
    else:
        with pytest.raises(ValueError, match="activation.*selection"):
            RuntimeConfig(**fields)


def test_target_procedure_pairs_refuse_silent_net_gross_reinterpretation():
    assert gross_policy().model_dump()["target_contract"] == GROSS
    for kwargs in ({"target_contract": GROSS}, {"procedure_version": V3}):
        with pytest.raises(ValueError, match="target.*procedure"):
            LearningPolicy(**kwargs)


@pytest.mark.parametrize("gross", [False, True])
def test_operational_daily_receipt_and_authority_keep_exact_commissioned_contract(tmp_path, gross):
    import asyncio
    from datetime import UTC, datetime, time, timedelta

    from tests.integration.paper_core.test_final_repair import commissioned

    from quantbot.paper_core.activation import active_in_session, commission_operational_candidate
    from quantbot.paper_core.contracts import LearningCalendar, LearningReceipt
    from quantbot.paper_core.roles import daily_consideration
    from quantbot.strategy import XNYSSession, XNYSSessionSequence

    db, store, release, commission, _ = commissioned(
        tmp_path, gross=gross, learning_origin=datetime.now(UTC) - timedelta(days=3)
    )
    store.clocks.execution_domain = "BROKER_PAPER:HOST_UTC_RUNTIME_V1"
    try:
        now = store.clocks.sample()[0]

        # External calendar boundary only; learner, commission and activation remain real.
        class CalendarProvider:
            async def learning_calendar(self):
                return LearningCalendar(
                    authority="ALPACA_PAPER_XNYS_V1",
                    sequence=XNYSSessionSequence(
                        calendar="XNYS",
                        sessions=tuple(
                            XNYSSession(
                                session_date=(now - timedelta(days=d)).date(),
                                open_at=datetime.combine(
                                    (now - timedelta(days=d)).date(), time(14, 30), UTC
                                ),
                                close_at=datetime.combine(
                                    (now - timedelta(days=d)).date(), time(21), UTC
                                ),
                            )
                            for d in (2, 1)
                        ),
                    ),
                )

        reason = asyncio.run(daily_consideration(store, CalendarProvider(), release.config))
        assert reason[-1] == "TEN_COMPLETED_SESSION_WARMUP"
        with db.transaction() as session:
            parent, active = active_in_session(store, session)
        receipt = store.read("core_learning_runs", active.receipt_id, LearningReceipt)
        assert receipt.status == "NO_CHANGE" and receipt.trial_charge_id is None
        assert receipt.policy.policy_id == store.config.policy_id
        assert receipt.policy.target_contract == (
            GROSS if gross else "owned-executed-net-return-v1"
        )
        assert receipt.policy.procedure_version == (
            V3 if gross else "daily-all-history-walk-forward-v2"
        )
        authority = store.read(
            "core_activation_authorities", active.authority_id, ActivationAuthority
        )
        assert authority.runtime_commission_id == commission.id
        assert authority.target_contract == receipt.policy.target_contract
        assert authority.procedure_version == receipt.policy.procedure_version
        wrong = receipt.model_copy(
            update={
                "policy": (
                    LearningPolicy(parent_model=parent, evidence_lane="BROKER_PAPER")
                    if gross
                    else gross_policy(parent_model=parent, evidence_lane="BROKER_PAPER")
                )
            }
        )
        with pytest.raises(StateConflictError, match="COMMISSION_CONTRACT"):
            commission_operational_candidate(
                store,
                wrong,
                expected_activation_id=active.id,
                at=store.clocks.sample()[0],
                approval_reference="cannot-reinterpret-target",
            )
    finally:
        db.close()


def test_gross_fit_accepts_only_fee_pending_and_preserves_original_economics(tmp_path):
    db, store, old, cutoff, _ = daily_inputs(tmp_path)
    try:
        with db.transaction() as session:
            rows, _, _ = learning._select(session, old, cutoff, execution_now=cutoff)
            inputs = learning._materialize(session, rows)
        pending = tuple(
            e.model_copy(
                update={
                    "outcome": e.outcome.model_copy(
                        update={
                            "status": "COSTS_PENDING",
                            "cost_reason": "FEE_UNAVAILABLE",
                            "net_pnl": None,
                            "actual_fees": None,
                        }
                    )
                }
            )
            for e in inputs
        )
        before = tuple(e.outcome.model_dump_json() for e in pending)
        parent = ModelArtifact(feature_version="five-minute-price-alpha-v1")
        candidate = fit_candidate(pending, parent, gross_policy())
        predictions = predict_bps(candidate, tuple(dict(e.observation.features) for e in pending))
        assert min(predictions) < -10 and max(predictions) > 10
        assert tuple(e.outcome.model_dump_json() for e in pending) == before
        assert "before-fee" in candidate.description
        with pytest.raises(ValueError, match="UNKNOWN_COSTS"):
            fit_candidate(pending, parent, old)
        for reason in ("M1_COST_PROVENANCE_UNAVAILABLE", "RECONCILIATION_PENDING"):
            bad = pending[0].model_copy(
                update={"outcome": pending[0].outcome.model_copy(update={"cost_reason": reason})}
            )
            with pytest.raises(ValueError, match="GROSS_TARGET"):
                fit_candidate((bad,), parent, gross_policy())
        flipped = tuple(
            e.model_copy(
                update={"outcome": e.outcome.model_copy(update={"gross_pnl": -e.outcome.gross_pnl})}
            )
            for e in pending
        )
        opposite = fit_candidate(flipped, parent, gross_policy())
        assert predict_bps(
            opposite, tuple(dict(e.observation.features) for e in pending)
        ) == pytest.approx(tuple(-p for p in predictions))
    finally:
        db.close()


@pytest.mark.parametrize("fault", ["future-validation", "wrong-session", "training-crosses-fold"])
def test_pure_gross_procedure_refuses_noncausal_fold_inputs(tmp_path, fault):
    from datetime import timedelta

    from quantbot.paper_core.contracts import LearningFold
    from quantbot.paper_core.learning_procedure import run_procedure

    db, store, policy, cutoff, calendar = daily_inputs(tmp_path, gross=True)
    try:
        with db.transaction() as session:
            rows, _, _ = learning._select(session, policy, cutoff, execution_now=cutoff)
            inputs = learning._materialize(session, rows)
        folds = []
        for day in calendar.sequence.sessions[-5:]:
            validation = tuple(e for e in inputs if e.session == day.session_date)
            training = tuple(e for e in inputs if e.session < day.session_date)
            folds.append(
                LearningFold(
                    session=day.session_date,
                    cutoff=day.open_at,
                    training=training,
                    validation=validation,
                    historical_models=(policy.parent_model,) * len(validation),
                    historical_model_ids=(policy.parent_model.id,) * len(validation),
                    historical_available_at=(None,) * len(validation),
                    historical_receipt_ids=(None,) * len(validation),
                    historical_acknowledgement_ids=(None,) * len(validation),
                    historical_acknowledgement_available_at=(None,) * len(validation),
                    historical_receipt_payloads=(None,) * len(validation),
                    historical_acknowledgement_payloads=(None,) * len(validation),
                )
            )
        first = folds[0]
        e = first.validation[0]
        if fault == "future-validation":
            e = e.model_copy(
                update={
                    "outcome": e.outcome.model_copy(
                        update={"available_at": cutoff + timedelta(seconds=1)}
                    )
                }
            )
            folds[0] = first.model_copy(update={"validation": (e, *first.validation[1:])})
        elif fault == "wrong-session":
            folds[0] = first.model_copy(update={"validation": (inputs[-1], *first.validation[1:])})
        else:
            folds[0] = first.model_copy(update={"training": (*first.training, e)})
        with pytest.raises(ValueError, match="NONCAUSAL"):
            run_procedure(
                tuple(folds), inputs, policy.parent_model, policy, calendar, cutoff, "bad-fold"
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_day_ten_actual_gross_outcomes_fit_and_activate_with_exact_authority(tmp_path):
    db, store, broker, parent, trips, joins = await gross_episodes(tmp_path)
    try:
        cutoff = broker.now.replace(hour=20, minute=0)
        clock = ExecutionClock(cutoff)
        calendar = learning_calendar()
        policy = gross_policy(parent_model=parent)
        before = {t.id: t.model_dump_json() for t in trips}
        assert {t.gross_pnl > 0 for t in trips} == {True, False}
        with db.transaction() as session:
            selected, excluded, _ = learning._select(session, policy, cutoff, execution_now=cutoff)
            assert len(selected) == 60 and not excluded
            net, net_excluded, _ = learning._select(
                session, LearningPolicy(), cutoff, execution_now=cutoff
            )
            assert not net and {r for _, r in net_excluded} == {"COSTS_PENDING"}
        receipt = learning.consider_learning(
            store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
        )
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert len(receipt.final_input_ids) == 60
        assert [len(f.training_ids) for f in receipt.fold_results] == [30, 36, 42, 48, 54]
        assert [len(f.validation_ids) for f in receipt.fold_results] == [6] * 5
        assert receipt.final_raw_weights == (2.0,) * 60
        for fold in receipt.fold_results:
            assert all(t > fold.cutoff for t in fold.scoring_available_at)
        with db.transaction() as session:
            charge = (
                session.execute(
                    select(search_runs).where(search_runs.c.search_id == receipt.trial_charge_id)
                )
                .mappings()
                .one()
            )
            assert charge["actor_version"] == GROSS
            detail = json.loads(charge["detail_json"])
            assert (
                detail["procedure_version"],
                detail["target_contract"],
                detail["policy_id"],
            ) == (V3, GROSS, policy.policy_id)
        for trip in trips:
            assert (
                store.read("core_outcome_revisions", trip.id, RoundTrip).model_dump_json()
                == before[trip.id]
            )
        initial = initialize_fixture_model(store, parent, at=cutoff)
        authority = ActivationAuthority.fixture(
            store,
            candidate_id=receipt.resulting_model_id,
            receipt_id=receipt.id,
            expected_activation_id=initial.id,
            at=cutoff,
        )
        commission_fixture_authority(store, authority)
        with pytest.raises(StateConflictError, match="CONTRACT"):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=receipt.resulting_model_id,
                authority=authority,
                at=cutoff,
            )
        exact = authority.model_copy(update={"target_contract": GROSS, "procedure_version": V3})
        commission_fixture_authority(store, exact)
        for change in (
            {"procedure_version": "daily-all-history-walk-forward-v2"},
            {"policy_id": "different-holding-policy"},
        ):
            incompatible = exact.model_copy(update=change)
            commission_fixture_authority(store, incompatible)
            with pytest.raises(StateConflictError, match="CONTRACT"):
                activate_model(
                    store,
                    expected_model_id=parent.id,
                    candidate_id=receipt.resulting_model_id,
                    authority=incompatible,
                    at=cutoff,
                )
        for change in (
            {"actor_version": "owned-executed-net-return-v1"},
            {
                "detail_json": json.dumps(
                    {**detail, "procedure_version": "daily-all-history-walk-forward-v2"}
                )
            },
            {"detail_json": json.dumps({**detail, "policy_id": "different-holding-policy"})},
        ):
            with db.transaction() as session:
                session.execute(
                    search_runs.update()
                    .where(search_runs.c.search_id == receipt.trial_charge_id)
                    .values(**change)
                )
            with pytest.raises(StateConflictError, match="FIXED_VALIDATION"):
                activate_model(
                    store,
                    expected_model_id=parent.id,
                    candidate_id=receipt.resulting_model_id,
                    authority=exact,
                    at=cutoff,
                )
            with db.transaction() as session:
                session.execute(
                    search_runs.update()
                    .where(search_runs.c.search_id == receipt.trial_charge_id)
                    .values(actor_version=GROSS, detail_json=charge["detail_json"])
                )
        with db.transaction() as session:
            assert (
                session.execute(select(table("core_active_model").c.model_id)).scalar_one()
                == parent.id
            )
        event = activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=receipt.resulting_model_id,
            authority=exact,
            at=cutoff,
        )
        assert event.model_id == receipt.resulting_model_id
        assert (
            learning.consider_learning(
                store, policy, cutoff=cutoff, calendar=calendar, execution_clock=clock
            )
            == receipt
        )
    finally:
        db.close()
