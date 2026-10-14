"""Final-send authority must outlive acquisition, without weakening unknown exposure."""

from datetime import timedelta

import pytest
from tests.integration.paper_core.test_thin_loop import NOW, prepare

from quantbot.brokers import SubmissionUncertainError
from quantbot.domain import IntentState
from quantbot.execution import OrderIntentNotResumable, OrderSubmissionBlocked
from quantbot.paper_core.contracts import GuardianReceipt, ProtectionPlan
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.runtime import coordinator
from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation, trader_cycle
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository


def delay_final_fetch(broker, method, *, at, store=None, receipt_expiry=None):
    original = getattr(broker, method)
    calls = 0

    async def delayed():
        nonlocal calls
        calls += 1
        result = await original()
        if calls == 2:
            broker.now = at
            if store is not None:
                plans = store.documents("core_protection_plans")
                plan = ProtectionPlan.model_validate(plans[0])
                store.record(
                    "core_guardian_receipts",
                    GuardianReceipt(
                        recorded_at_utc=NOW + timedelta(microseconds=1),
                        owner=plan.owner,
                        generation=plan.generation,
                        valid_until=receipt_expiry,
                        protection_ids=(plan.id,),
                    ),
                )
        return result

    setattr(broker, method, delayed)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get_account", "get_positions"])
async def test_final_acquisition_delay_refuses_expired_entry_and_keeps_unsent_evidence(
    tmp_path, method
):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        delay_final_fetch(broker, method, at=NOW + timedelta(minutes=31))
        with pytest.raises(OrderSubmissionBlocked) as error:
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert "CORE_ENTRY_DECISION_EXPIRED" in error.value.decision.reasons
        assert "CORE_PROTECTION_DEADLINE_DUE" in error.value.decision.reasons
        assert not broker.posts
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.get_order_intent(store.links()[0]["intent_id"])
            assert intent.state is IntentState.ERROR
            events = [
                e
                for e in repo.list_order_events(intent_id=intent.intent_id)
                if e.event_type == "CORE_ENTRY_SEND_REFUSED"
            ]
            assert len(events) == 1
            assert events[0].detail["definitively_unsent"] is True
            assert not repo.get_kill_switch_state().engaged
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("microseconds,allowed", [(119999999, True), (120000000, False)])
async def test_decision_expiry_is_exclusive_at_actual_send(tmp_path, microseconds, allowed):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        delay_final_fetch(broker, "get_account", at=NOW + timedelta(microseconds=microseconds))
        if allowed:
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        else:
            with pytest.raises(OrderSubmissionBlocked):
                await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert len(broker.posts) == int(allowed)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("microseconds,allowed", [(59999999, True), (60000000, False)])
async def test_latest_guardian_ack_expiry_is_exclusive_even_with_older_valid_ack(
    tmp_path, microseconds, allowed
):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        delay_final_fetch(
            broker,
            "get_account",
            at=NOW + timedelta(microseconds=microseconds),
            store=store,
            receipt_expiry=NOW + timedelta(seconds=60),
        )
        if allowed:
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        else:
            with pytest.raises(OrderSubmissionBlocked) as error:
                await trader_cycle(store, broker, observation, model, proof, now=NOW)
            assert "CORE_GUARDIAN_RECEIPT_EXPIRED" in error.value.decision.reasons
        assert len(broker.posts) == int(allowed)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("microseconds,allowed", [(59999999, True), (60000000, False)])
async def test_session_protection_deadline_is_exclusive_at_send(tmp_path, microseconds, allowed):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        observation = observation.model_copy(update={"session_close": NOW + timedelta(seconds=180)})
        delay_final_fetch(broker, "get_positions", at=NOW + timedelta(microseconds=microseconds))
        if allowed:
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        else:
            with pytest.raises(OrderSubmissionBlocked) as error:
                await trader_cycle(store, broker, observation, model, proof, now=NOW)
            assert "CORE_PROTECTION_DEADLINE_DUE" in error.value.decision.reasons
        assert len(broker.posts) == int(allowed)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_unsent_expiry_frees_slot_for_fresh_observation_but_never_replays_old_claim(tmp_path):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        delay_final_fetch(broker, "get_account", at=NOW + timedelta(minutes=31))
        with pytest.raises(OrderSubmissionBlocked):
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        old_link = store.links()[0]
        await guardian_cycle(store, broker, now=broker.now)
        fresh = observation.model_copy(
            update={
                "source_start": broker.now - timedelta(minutes=5),
                "source_end": broker.now,
                "available_at": broker.now,
                "recorded_at_utc": broker.now,
                "dependency_available_at": (broker.now,),
                "sequence": 2,
                "raw_data_digest": "fresh-bar",
            }
        )
        await trader_cycle(store, broker, fresh, model, store.latest_proof(), now=broker.now)
        assert len(broker.posts) == 1
        assert len(store.links()) == 2
        with db.transaction() as session:
            old = StorageRepository(session).get_order_intent(old_link["intent_id"])
        with pytest.raises(OrderIntentNotResumable):
            await coordinator(store, broker).submit_durable(
                old,
                purpose=OrderPurpose.ENTRY,
                market_eligible=True,
                drawdown=None,
                risk_approved=True,
            )
        assert len(broker.posts) == 1
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN])
async def test_expiry_never_releases_submitting_or_unknown_exposure(tmp_path, state):
    from quantbot.risk import OrderPurpose

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        record_observation(store, observation)
        decision = evaluate_entry(store.config, observation, model, proof)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        with db.transaction() as session:
            repo = StorageRepository(session)
            repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.SUBMITTING,
                expected_current=IntentState.ORDER_CREATED,
            )
            if state is IntentState.SUBMISSION_UNKNOWN:
                repo.transition_order_intent(
                    entry.intent.intent_id, state, expected_current=IntentState.SUBMITTING
                )
        broker.now = NOW + timedelta(minutes=31)
        with pytest.raises(SubmissionUncertainError):
            await coordinator(store, broker).submit_durable(
                entry.intent,
                purpose=OrderPurpose.ENTRY,
                market_eligible=True,
                drawdown=None,
                risk_approved=True,
            )
        await guardian_cycle(store, broker, now=broker.now)
        fresh = observation.model_copy(
            update={
                "source_start": broker.now - timedelta(minutes=5),
                "source_end": broker.now,
                "available_at": broker.now,
                "recorded_at_utc": broker.now,
                "dependency_available_at": (broker.now,),
                "sequence": 2,
                "raw_data_digest": "fresh-bar",
            }
        )
        await trader_cycle(store, broker, fresh, model, store.latest_proof(), now=broker.now)
        assert not broker.posts
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert (
                repo.get_order_intent(entry.intent.intent_id).state
                is IntentState.SUBMISSION_UNKNOWN
            )
            assert not any(
                e.event_type == "CORE_ENTRY_SEND_REFUSED" for e in repo.list_order_events()
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_expired_entry_and_guardian_receipt_never_prevent_owned_deadline_exit(tmp_path):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=31)
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="operator pause", updated_at=broker.now
            )
        receipt = await guardian_cycle(store, broker, now=broker.now)
        assert receipt.exit_intent_ids
        assert len(broker.posts) == 2
        assert broker.posts[1].side.value == "SELL"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_protocol_error_after_send_attempt_does_not_free_slot(tmp_path):
    from quantbot.brokers import BrokerProtocolError

    db, store, broker, proof, observation, model = prepare(tmp_path)
    attempts = []

    async def failed_post(intent):
        attempts.append(intent.client_order_id)
        raise BrokerProtocolError("synthetic protocol failure after possible send")

    broker.submit_order = failed_post
    try:
        with pytest.raises(BrokerProtocolError):
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=31)
        await guardian_cycle(store, broker, now=broker.now)
        fresh = observation.model_copy(
            update={
                "source_start": broker.now - timedelta(minutes=5),
                "source_end": broker.now,
                "available_at": broker.now,
                "recorded_at_utc": broker.now,
                "dependency_available_at": (broker.now,),
                "sequence": 2,
                "raw_data_digest": "fresh-bar",
            }
        )
        await trader_cycle(store, broker, fresh, model, store.latest_proof(), now=broker.now)
        assert len(attempts) == 1
        assert len(store.links()) == 1
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent(store.links()[0]["intent_id"]).state is IntentState.ERROR
            assert not any(
                e.event_type == "CORE_ENTRY_SEND_REFUSED" for e in repo.list_order_events()
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_refusal_before_error_commit_latches_against_clock_reversed_retry(
    tmp_path, monkeypatch
):
    from quantbot.execution import ExecutionCoordinator
    from quantbot.paper_core.send_guard import final_send_objections

    db, store, broker, proof, observation, model = prepare(tmp_path)
    original = ExecutionCoordinator._transition

    def crash_before_error(self, intent_id, target, expected):
        if target is IntentState.ERROR:
            raise RuntimeError("crash before ERROR commit")
        return original(self, intent_id, target, expected)

    monkeypatch.setattr(ExecutionCoordinator, "_transition", crash_before_error)
    try:
        delay_final_fetch(broker, "get_account", at=NOW + timedelta(minutes=31))
        with pytest.raises(RuntimeError, match="ERROR commit"):
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.get_order_intent(store.links()[0]["intent_id"])
            assert intent.state is IntentState.ORDER_CREATED
            assert (
                len(
                    [
                        e
                        for e in repo.list_order_events()
                        if e.event_type == "CORE_ENTRY_SEND_REFUSED"
                    ]
                )
                == 1
            )
        broker.now = NOW
        assert final_send_objections(
            store, intent, OrderPurpose.ENTRY, clock=lambda: broker.now
        ) == ("CORE_ENTRY_ALREADY_REFUSED",)
        assert not broker.posts
    finally:
        db.close()
