"""Explicit economic amendment CAS preserves the base ledger and refuses ambiguous lots."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from tests.integration.paper_core.test_paper_funds import candidate, prepare

from quantbot.domain import IntentState
from quantbot.paper_core.activation import active_in_session
from quantbot.paper_core.authority import (
    commission_in_session,
    executable_in_session,
    fence_entries,
    transition_release,
)
from quantbot.paper_core.contracts import ProtectionPlan
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.release import prepare_release, read_status
from quantbot.paper_core.store import CoreStore, table
from quantbot.paper_core.trader import claim_entry
from quantbot.storage import StateConflictError, StorageRepository


def amendment(store, broker):
    policy = EntryPolicy(symbols=("AAPL", "MSFT", "F"))
    successor = prepare_release(broker.config.model_copy(update={"entry_policy": policy}))
    with store.database.transaction() as session:
        commission = commission_in_session(store, session)
        binding = executable_in_session(session)
        active = active_in_session(store, session)[1]
    fence = fence_entries(store, reason="OFFLINE_EXPLICIT_AMENDMENT", evidence_ids=("FIXTURE",))
    approval = dict(
        commission_id=commission.id,
        expected_binding_id=binding.id,
        old_release_id=binding.release.id,
        new_release_id=successor.id,
        expected_fence_id=fence,
        expected_activation_id=active.id,
        entry_policy_id=policy.id,
        approved_at=store.clocks.sample()[0].isoformat(),
        approval_reference="OFFLINE_USER_APPROVED_ONE_SHARE",
        old_writer_fence_evidence_ids=("FIXTURE",),
    )
    return successor, approval


def immutable_snapshot(db):
    with db.transaction() as session:
        return {
            name: session.execute(select(table(name)).order_by(table(name).c[0])).all()
            for name in (
                "core_identities",
                "core_runtime_commissions",
                "core_fixture_baselines",
                "core_active_model",
                "kill_switch_state",
                "core_learning_runs",
            )
        }


@pytest.mark.asyncio
async def test_explicit_amendment_preserves_old_identity_model_stop_and_rejects_old_decision(
    tmp_path,
):
    from quantbot.paper_core.__main__ import commissioned_core
    from quantbot.paper_core.authority import transition_entry_policy

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        old_decision = await candidate(store, broker)
        old_binding = read_status(db.path)["executable_binding"]
        snapshot = immutable_snapshot(db)
        successor, approval = amendment(store, broker)
        with pytest.raises(StateConflictError, match="CODE_ONLY"):
            transition_release(store, successor, approval)
        identity = transition_entry_policy(store, successor, approval)
        assert identity
        assert snapshot == immutable_snapshot(db)
        status = read_status(db.path)
        core = commissioned_core(successor, status)
        assert core == store.config
        assert status["executable_binding"]["kind"] == "ENTRY_POLICY_EXECUTABLE_BINDING_V1"
        current = CoreStore(db, core, clocks=clocks, runtime_release_id=successor.id)
        assert current.entry_symbols == ("AAPL", "MSFT", "F")
        assert current.entry_policy.id == successor.config.entry_policy.id
        assert (
            claim_entry(current, old_decision, ProtectionPlan.for_decision(old_decision, core))
            is None
        )
        with pytest.raises(ValueError, match="BINDING_MISMATCH"):
            from quantbot.paper_core.release import PreparedRelease

            commissioned_core(PreparedRelease.model_validate(old_binding["release"]), status)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_explicit_amendment_can_return_to_prior_entry_rules_without_erasing_history(tmp_path):
    from quantbot.paper_core.authority import transition_entry_policy

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        prior = prepare_release(broker.config)
        snapshot = immutable_snapshot(db)
        successor, approval = amendment(store, broker)
        binding_id = transition_entry_policy(store, successor, approval)
        current = CoreStore(db, store.config, clocks=clocks, runtime_release_id=successor.id)
        from quantbot.paper_core.authority import current_runtime_event

        with db.transaction() as session:
            fence = current_runtime_event(session)
        rollback = {
            **approval,
            "expected_binding_id": binding_id,
            "old_release_id": successor.id,
            "new_release_id": prior.id,
            "entry_policy_id": None,
            "expected_fence_id": fence.id,
            "approval_reference": "OFFLINE_EXPLICIT_RETURN_TO_PRIOR_ENTRY_RULES",
        }
        transition_entry_policy(current, prior, rollback)
        restored = CoreStore(db, store.config, clocks=clocks, runtime_release_id=prior.id)
        assert restored.entry_policy is None
        assert restored.entry_symbols == store.config.entry_symbols
        assert snapshot == immutable_snapshot(db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_amendment_does_not_reset_daily_reservations(tmp_path):
    from tests.integration.paper_core.test_paper_funds import close, enter

    from quantbot.paper_core.authority import transition_entry_policy
    from quantbot.paper_core.selection import reservation_state

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision, reasons = await enter(store, broker)
        assert not reasons
        await close(store, broker, clocks)
        successor, approval = amendment(store, broker)
        transition_entry_policy(store, successor, approval)
        current = CoreStore(db, store.config, clocks=clocks, runtime_release_id=successor.id)
        broker.config = successor.config
        broker.store = current
        cohort = await broker.observation_cohort()
        with db.transaction() as session:
            daily, _, _ = reservation_state(current, session, cohort)
        assert dict(daily) == {decision.symbol: 1}
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        IntentState.RISK_APPROVED,
        IntentState.ORDER_CREATED,
        IntentState.SUBMITTING,
        IntentState.SUBMISSION_UNKNOWN,
        IntentState.ERROR,
    ],
)
async def test_amendment_refuses_prepared_or_possibly_sent_lot(tmp_path, state):
    from quantbot.paper_core.authority import transition_entry_policy

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry
        path = [IntentState.RISK_APPROVED, IntentState.ORDER_CREATED, IntentState.SUBMITTING]
        if state not in path:
            path.append(state)
        with db.transaction() as session:
            repo = StorageRepository(session)
            previous = IntentState.RISK_APPROVED
            for target in path[1 : path.index(state) + 1]:
                repo.transition_order_intent(
                    entry.intent.intent_id, target, expected_current=previous
                )
                previous = target
        successor, approval = amendment(store, broker)
        with pytest.raises(StateConflictError, match="CORE_NOT_FLAT"):
            transition_entry_policy(store, successor, approval)
        assert not provider.orders
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["other_configuration", "stale_approval", "stale_binding"])
async def test_amendment_requires_exact_fresh_policy_only_approval(tmp_path, fault):
    from quantbot.paper_core.authority import transition_entry_policy

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        successor, approval = amendment(store, broker)
        if fault == "other_configuration":
            successor = prepare_release(
                successor.config.model_copy(update={"entry_poll_seconds": 14})
            )
            approval["new_release_id"] = successor.id
        elif fault == "stale_approval":
            approval["approved_at"] = (clocks.wall - timedelta(seconds=121)).isoformat()
        else:
            approval["expected_binding_id"] = "stale"
        with pytest.raises(StateConflictError, match="EXACT_APPROVAL"):
            transition_entry_policy(store, successor, approval)
    finally:
        db.close()
