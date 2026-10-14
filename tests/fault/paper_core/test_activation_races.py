"""Stale approval, ABA, unresolved exposure and corrupted digests must fence entries."""

from datetime import timedelta

import pytest
from tests.fixtures.core_m5 import accepted


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_terminal_status_requires_recovered_complete_activities_at_switch_cutoff(
    tmp_path, rollback
):
    from decimal import Decimal

    from quantbot.domain import BrokerOrder, IntentState
    from quantbot.paper_core.activation import (
        activate_model,
        initialize_fixture_model,
        rollback_model,
    )
    from quantbot.paper_core.contracts import BrokerEvent, ModelArtifact, ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, terminal_evidence
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
    from quantbot.storage import StateConflictError, StorageRepository

    db, store, broker, parent, candidate, receipt, observation = accepted(tmp_path)
    try:
        current = None
        if rollback:
            current = initialize_fixture_model(store, parent, at=broker.now)
            current = activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=commissioned(store, candidate, receipt, current, broker.now),
                at=broker.now,
            )

        def switch(at):
            if rollback:
                return rollback_model(
                    store,
                    expected_model_id=candidate.id,
                    target_id=parent.id,
                    expected_activation_id=current.id,
                    reason="recovered terminal fixture",
                    at=at,
                )
            return activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=at,
            )

        await guardian_cycle(store, broker, now=broker.now)
        record_observation(store, observation)
        decision = evaluate_entry(
            store.config,
            observation,
            candidate if rollback else ModelArtifact(feature_version=observation.feature_version),
            store.latest_proof(),
            activation=current if rollback else None,
        )
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        if current is None:
            current = initialize_fixture_model(store, parent, at=broker.now)
        authority = commissioned(store, candidate, receipt, current, broker.now)
        intent = entry.intent
        order = BrokerOrder(
            broker_order_id="boundary-" + intent.client_order_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            quantity=intent.quantity,
            filled_quantity=Decimal(0),
            status="canceled",
            submitted_at=broker.now,
        )
        with db.transaction() as session:
            repo = StorageRepository(session)
            for state, before in (
                (IntentState.ORDER_CREATED, IntentState.RISK_APPROVED),
                (IntentState.SUBMITTING, IntentState.ORDER_CREATED),
                (IntentState.SUBMISSION_UNKNOWN, IntentState.SUBMITTING),
            ):
                repo.transition_order_intent(intent.intent_id, state, expected_current=before)
            repo.save_broker_order(order)
        with pytest.raises(StateConflictError, match="ACTIVATION_UNRESOLVED_SUBMISSION"):
            switch(broker.now)
        assert len(store.links()) == 81 and not broker.posts
        incomplete = BrokerEvent(
            event_id="boundary-incomplete",
            raw_digest="explicit-boundary-fixture",
            account_id=store.config.account_id,
            endpoint="orders",
            client_order_id=intent.client_order_id,
            occurred_at=broker.now,
            recorded_at_utc=broker.now,
            order=order,
        )
        import_broker_events(store, (incomplete,))
        with db.transaction() as session:
            assert StorageRepository(session).get_order_intent(intent.intent_id).state == (
                IntentState.CANCELLED
            )
            assert terminal_evidence(session, order, cutoff=broker.now) is None
        with pytest.raises(StateConflictError, match="ACTIVATION_UNRESOLVED_CLAIM"):
            switch(broker.now)
        later = broker.now + timedelta(seconds=1)
        complete = incomplete.model_copy(
            update={
                "event_id": "boundary-complete",
                "recorded_at_utc": later,
                "activity_complete": True,
                "activity_watermark": later,
            }
        )
        import_broker_events(store, (complete,))
        with pytest.raises(StateConflictError, match="ACTIVATION_UNRESOLVED_CLAIM"):
            switch(broker.now)
        with db.transaction() as session:
            assert terminal_evidence(session, order, cutoff=later) is not None
        result = switch(later)
        assert result.generation == current.generation + 1
        assert len(store.links()) == 81 and len(store.documents("core_round_trips")) == 40
    finally:
        db.close()


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_mismatched_canonical_permanent_charge_cannot_activate(tmp_path, missing):
    import sqlite3

    from quantbot.paper_core.activation import activate_model, initialize_fixture_model
    from quantbot.storage import StateConflictError

    db, store, broker, parent, candidate, receipt, _ = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        authority = commissioned(store, candidate, receipt, genesis, broker.now)
        # A disposable fixture simulates externally corrupted canonical charge state.
        with sqlite3.connect(db.path) as connection:
            if missing:
                connection.execute(
                    "DELETE FROM search_runs WHERE search_id=?", (receipt.trial_charge_id,)
                )
            else:
                connection.execute(
                    "UPDATE search_runs SET configuration_hash='wrong-contract' WHERE search_id=?",
                    (receipt.trial_charge_id,),
                )
        with pytest.raises(StateConflictError, match="FIXED_VALIDATION"):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=broker.now,
            )
        assert len(store.documents("core_activations")) == 1
    finally:
        db.close()


def commissioned(store, candidate, receipt, previous, at):
    from quantbot.paper_core.activation import ActivationAuthority, commission_fixture_authority

    authority = ActivationAuthority.fixture(
        store,
        candidate_id=candidate.id,
        receipt_id=receipt.id,
        expected_activation_id=previous.id,
        at=at,
    )
    commission_fixture_authority(store, authority)
    return authority


@pytest.mark.asyncio
async def test_active_predictor_with_missing_feature_abstains_without_post(tmp_path):
    from quantbot.paper_core.activation import activate_model, initialize_fixture_model
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, parent, candidate, receipt, observation = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=commissioned(store, candidate, receipt, genesis, broker.now),
            at=broker.now,
        )
        await guardian_cycle(store, broker, now=broker.now)
        missing = observation.model_copy(update={"features": observation.features[:-1]})
        decision = await trader_cycle(
            store, broker, missing, parent, store.latest_proof(), now=broker.now
        )
        assert decision.action == "WAIT" and "PREDICT_FEATURES_INVALID" in decision.gates
        assert not broker.posts
    finally:
        db.close()


def test_competing_activation_and_aba_never_revive_old_approval(tmp_path):
    from quantbot.paper_core.activation import (
        activate_model,
        initialize_fixture_model,
        read_active_model,
        rollback_model,
    )
    from quantbot.storage import StateConflictError

    db, store, broker, parent, candidate, receipt, _ = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        authority = commissioned(store, candidate, receipt, genesis, broker.now)
        current = activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=authority,
            at=broker.now,
        )
        with pytest.raises(StateConflictError, match="STALE_ACTIVATION"):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=broker.now,
            )
        back = rollback_model(
            store,
            expected_model_id=candidate.id,
            target_id=parent.id,
            expected_activation_id=current.id,
            reason="reviewed local rollback",
            at=broker.now,
        )
        assert back.generation == 3 and read_active_model(store).id == parent.id
        with pytest.raises(StateConflictError, match="STALE_ACTIVATION"):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=broker.now,
            )
        fresh = commissioned(store, candidate, receipt, back, broker.now)
        assert (
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=fresh,
                at=broker.now,
            ).generation
            == 4
        )
    finally:
        db.close()


@pytest.mark.parametrize(
    "edge", ["before_event", "after_event", "after_pointer", "before_commit", "after_commit"]
)
def test_process_death_at_activation_commit_edges_reloads_only_committed_model(tmp_path, edge):
    import subprocess
    import sys
    from pathlib import Path

    from quantbot.paper_core.activation import initialize_fixture_model, read_active_model
    from quantbot.paper_core.store import CoreStore
    from quantbot.storage import Database

    db, store, broker, parent, candidate, receipt, _ = accepted(tmp_path)
    genesis = initialize_fixture_model(store, parent, at=broker.now)
    authority = commissioned(store, candidate, receipt, genesis, broker.now)
    config, path = store.config, db.path
    db.close()
    child = Path(__file__).parents[2] / "fixtures/core_m5_crash.py"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(child),
            str(path),
            config.model_dump_json(),
            authority.model_dump_json(),
            parent.id,
            candidate.id,
            edge,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 91, result.stderr
    with Database(path) as restarted:
        resumed = CoreStore(restarted, config)
        selected = candidate.id if edge == "after_commit" else parent.id
        assert read_active_model(resumed).id == selected
        assert len(resumed.documents("core_activations")) == (2 if edge == "after_commit" else 1)
        assert len(resumed.documents("core_round_trips")) == 40


def test_two_writers_have_one_atomic_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from quantbot.paper_core.activation import activate_model, initialize_fixture_model
    from quantbot.storage import StateConflictError

    db, store, broker, parent, candidate, receipt, _ = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        authority = commissioned(store, candidate, receipt, genesis, broker.now)

        def contender():
            try:
                return activate_model(
                    store,
                    expected_model_id=parent.id,
                    candidate_id=candidate.id,
                    authority=authority,
                    at=broker.now,
                ).model_id
            except StateConflictError as error:
                return str(error)

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: contender(), range(2)))
        assert sorted(results) == sorted([candidate.id, "STALE_ACTIVATION"])
        assert len(store.documents("core_activations")) == 2
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_unresolved_claim_and_unknown_submission_remain_reserved(tmp_path, unknown):
    from quantbot.domain import IntentState
    from quantbot.paper_core.activation import activate_model, initialize_fixture_model
    from quantbot.paper_core.contracts import ModelArtifact, ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
    from quantbot.storage import StateConflictError, StorageRepository

    db, store, broker, parent, candidate, receipt, observation = accepted(tmp_path)
    try:
        await guardian_cycle(store, broker, now=broker.now)
        record_observation(store, observation)
        decision = evaluate_entry(
            store.config,
            observation,
            ModelArtifact(feature_version=observation.feature_version),
            store.latest_proof(),
        )
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        authority = commissioned(store, candidate, receipt, genesis, broker.now)
        if unknown:
            with db.transaction() as session:
                repo = StorageRepository(session)
                for state, before in (
                    (IntentState.ORDER_CREATED, IntentState.RISK_APPROVED),
                    (IntentState.SUBMITTING, IntentState.ORDER_CREATED),
                    (IntentState.SUBMISSION_UNKNOWN, IntentState.SUBMITTING),
                ):
                    repo.transition_order_intent(
                        entry.intent.intent_id, state, expected_current=before
                    )
        with pytest.raises(StateConflictError, match="ACTIVATION_UNRESOLVED"):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=broker.now,
            )
        assert not broker.posts and len(store.documents("core_activations")) == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    "fault", ["wrong_digest", "stale", "uncommissioned", "future_receipt", "wrong_receipt"]
)
def test_invalid_approval_cannot_change_pointer(tmp_path, fault):
    from sqlalchemy import text

    from quantbot.paper_core.activation import (
        activate_model,
        initialize_fixture_model,
        read_active_model,
    )
    from quantbot.storage import StateConflictError

    db, store, broker, parent, candidate, receipt, _ = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        authority = commissioned(store, candidate, receipt, genesis, broker.now)
        at = broker.now
        if fault == "wrong_digest":
            with db.transaction() as session:
                session.execute(
                    text("UPDATE core_models SET payload=:payload WHERE id=:id"),
                    {
                        "payload": candidate.model_copy(
                            update={"description": "corrupted"}
                        ).model_dump_json(),
                        "id": candidate.id,
                    },
                )
        elif fault == "stale":
            at = authority.expires_at
        elif fault == "uncommissioned":
            authority = authority.model_copy(update={"reviewer": "unapproved"})
        elif fault == "future_receipt":
            at = receipt.available_at - timedelta(microseconds=1)
        elif fault == "wrong_receipt":
            authority = authority.model_copy(update={"receipt_id": "missing"})
            from quantbot.paper_core.activation import commission_fixture_authority

            commission_fixture_authority(store, authority)
        with pytest.raises(StateConflictError):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=at,
            )
        assert read_active_model(store).id == parent.id
        assert len(store.documents("core_activations")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_corrupt_pointer_fences_entries_but_guardian_keeps_working(tmp_path):
    from sqlalchemy import text

    from quantbot.paper_core.activation import initialize_fixture_model
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, parent, _, _, observation = accepted(tmp_path)
    try:
        initialize_fixture_model(store, parent, at=broker.now)
        with db.transaction() as session:
            session.execute(text("DELETE FROM core_active_model"))
        await guardian_cycle(store, broker, now=broker.now)
        decision = await trader_cycle(
            store, broker, observation, parent, store.latest_proof(), now=broker.now
        )
        assert decision.action == "WAIT" and "CORE_MODEL_BINDING_INVALID" in decision.gates
        assert not broker.posts
    finally:
        db.close()


@pytest.mark.parametrize("change", ["failed", "missing_completion", "bad_scores", "lane"])
def test_exact_receipt_requires_fixed_qualifying_result_and_lane(tmp_path, change):
    from sqlalchemy import update

    from quantbot.paper_core.activation import activate_model, initialize_fixture_model
    from quantbot.paper_core.store import table
    from quantbot.storage import StateConflictError

    db, store, broker, parent, candidate, receipt, _ = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        updates = {
            "failed": {"status": "FIT_FAILED"},
            "missing_completion": {"available_at": None, "completed_at": None},
            "bad_scores": {
                "metrics": tuple(
                    (key, 800.0 if key == "candidate_mse" else value)
                    for key, value in receipt.metrics
                )
            },
            "lane": {"evidence_lane": "LOCAL_SIM_REAL_DATA"},
        }
        altered = receipt.model_copy(update=updates[change])
        store.record("core_learning_runs", altered)
        # Deliberately corrupt the immutable mapping to exercise defensive validation.
        with db.transaction() as session:
            mapping = table("core_learning_considerations")
            session.execute(
                update(mapping)
                .where(mapping.c.run_id == receipt.run_id)
                .values(receipt_id=altered.id)
            )
        authority = commissioned(store, candidate, altered, genesis, broker.now)
        with pytest.raises(StateConflictError):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=authority,
                at=broker.now,
            )
        assert len(store.documents("core_activations")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_aba_model_hash_does_not_resurrect_stale_claim(tmp_path):
    from quantbot.paper_core.activation import (
        activate_model,
        initialize_fixture_model,
        rollback_model,
    )
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation

    db, store, broker, parent, candidate, receipt, observation = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        first = activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=commissioned(store, candidate, receipt, genesis, broker.now),
            at=broker.now,
        )
        await guardian_cycle(store, broker, now=broker.now)
        record_observation(store, observation)
        stale = evaluate_entry(
            store.config, observation, candidate, store.latest_proof(), activation=first
        )
        back = rollback_model(
            store,
            expected_model_id=candidate.id,
            target_id=parent.id,
            expected_activation_id=first.id,
            reason="fixture ABA",
            at=broker.now,
        )
        current = activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=commissioned(store, candidate, receipt, back, broker.now),
            at=broker.now,
        )
        assert current.model_id == stale.model_id and current.id != stale.activation_id
        assert claim_entry(store, stale, ProtectionPlan.for_decision(stale, store.config)) is None
        assert not broker.posts and len(store.links()) == 80
    finally:
        db.close()


@pytest.mark.asyncio
async def test_final_send_refuses_ambiguous_pointer_and_definitive_unsent_frees_activation(
    tmp_path,
):
    from sqlalchemy import text

    from quantbot.domain import IntentState
    from quantbot.paper_core.activation import activate_model, initialize_fixture_model
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.send_guard import final_send_objections
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
    from quantbot.risk import OrderPurpose
    from quantbot.storage import StateConflictError, StorageRepository

    db, store, broker, parent, candidate, receipt, observation = accepted(tmp_path)
    try:
        genesis = initialize_fixture_model(store, parent, at=broker.now)
        selected = activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=commissioned(store, candidate, receipt, genesis, broker.now),
            at=broker.now,
        )
        await guardian_cycle(store, broker, now=broker.now)
        record_observation(store, observation)
        decision = evaluate_entry(
            store.config, observation, candidate, store.latest_proof(), activation=selected
        )
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        from quantbot.paper_core.guardian import acknowledge_handoffs

        acknowledge_handoffs(store, now=broker.now)
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            session.execute(
                text(
                    "UPDATE core_active_model SET activation_id=:id,model_id=:model,"
                    "generation=1,receipt_id=NULL"
                ),
                {"id": genesis.id, "model": parent.id},
            )
        reasons = final_send_objections(store, intent, OrderPurpose.ENTRY, clock=lambda: broker.now)
        assert "CORE_MODEL_BINDING_INVALID" in reasons and not broker.posts
        with db.transaction() as session:
            repo = StorageRepository(session)
            repo.transition_order_intent(
                intent.intent_id, IntentState.ERROR, expected_current=IntentState.ORDER_CREATED
            )
            session.execute(
                text(
                    "UPDATE core_active_model SET activation_id=:id,model_id=:model,"
                    "generation=2,receipt_id=:receipt"
                ),
                {"id": selected.id, "model": candidate.id, "receipt": receipt.id},
            )
        from quantbot.paper_core.activation import rollback_model

        back = rollback_model(
            store,
            expected_model_id=candidate.id,
            target_id=parent.id,
            expected_activation_id=selected.id,
            reason="safe after proven unsent",
            at=broker.now,
        )
        assert back.generation == 3
        with pytest.raises(StateConflictError, match="STALE_ACTIVATION"):
            activate_model(
                store,
                expected_model_id=parent.id,
                candidate_id=candidate.id,
                authority=commissioned(store, candidate, receipt, genesis, broker.now),
                at=broker.now,
            )
    finally:
        db.close()
