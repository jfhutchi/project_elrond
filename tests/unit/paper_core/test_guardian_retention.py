"""Local SQLite regressions for protection retention and acknowledgement growth."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from tests.fixtures.core_m2 import NOW, prepare

from quantbot.domain import BrokerOrder, Fill, IntentState
from quantbot.paper_core.contracts import ProtectionPlan
from quantbot.paper_core.guardian import acknowledge_handoffs
from quantbot.paper_core.send_guard import record_pre_send_refusal
from quantbot.paper_core.store import table
from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
from quantbot.paper_core.work import current_receipts, historical_receipts
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository


def claimed(tmp_path):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    store.clocks = SimpleNamespace(sample=lambda: (NOW, 1.0))
    record_observation(store, observation)
    decision = evaluate_entry(store.config, observation, model, proof)
    plan = ProtectionPlan.for_decision(decision, store.config)
    entry = claim_entry(store, decision, plan)
    assert entry is not None
    store.runtime_role = "guardian"
    return db, store, entry, plan


@pytest.mark.parametrize(
    ("case", "retained"),
    [
        ("refused", False),
        ("rejected_local", False),
        ("rejected_only", True),
        ("error_only", True),
        ("unknown", True),
        ("refused_with_order", True),
        ("held", True),
    ],
)
def test_retire_only_durably_unsent_entries(tmp_path, case, retained):
    db, store, entry, plan = claimed(tmp_path)
    try:
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            if case.startswith("refused"):
                record_pre_send_refusal(
                    session,
                    intent,
                    OrderPurpose.ENTRY,
                    ("EXPIRED",),
                    clock=lambda: NOW,
                )
            if case in {"refused_with_order", "held"}:
                repo.save_broker_order(
                    BrokerOrder(
                        broker_order_id="offline-order",
                        client_order_id=intent.client_order_id,
                        symbol=intent.symbol,
                        side=intent.side,
                        order_type=intent.order_type,
                        time_in_force=intent.time_in_force,
                        quantity=intent.quantity,
                        filled_quantity=intent.quantity if case == "held" else 0,
                        status="filled" if case == "held" else "accepted",
                        submitted_at=NOW,
                    )
                )
                if case == "held":
                    repo.record_fill(
                        Fill(
                            fill_id="offline-owned-fill",
                            broker_order_id="offline-order",
                            symbol=intent.symbol,
                            side=intent.side,
                            quantity=intent.quantity,
                            price=100,
                            occurred_at=NOW,
                        )
                    )
            target = IntentState.ERROR
            if case in {"unknown", "held", "rejected_local", "rejected_only"}:
                repo.transition_order_intent(
                    intent.intent_id,
                    IntentState.SUBMITTING,
                    expected_current=IntentState.ORDER_CREATED,
                )
                if case == "unknown":
                    target = IntentState.SUBMISSION_UNKNOWN
                elif case.startswith("rejected"):
                    target = IntentState.REJECTED
                    if case == "rejected_local":
                        repo.record_order_event(
                            "offline-local-budget-refusal",
                            event_type="CORE_LOCAL_SEND_NOT_ATTEMPTED",
                            occurred_at=NOW,
                            intent_id=intent.intent_id,
                            detail={
                                "contract": "ACCOUNT_BUDGET_BEFORE_IO_V1",
                                "client_order_id": intent.client_order_id,
                                "definitively_unsent": True,
                            },
                        )
                current = IntentState.SUBMITTING
            else:
                current = IntentState.ORDER_CREATED
            repo.transition_order_intent(intent.intent_id, target, expected_current=current)
        receipt = acknowledge_handoffs(store, now=NOW + timedelta(seconds=1))
        assert (plan.id in receipt.protection_ids) is retained
        assert not store.documents("core_round_trips")
        assert len(store.documents("core_protection_plans")) == 1
    finally:
        db.close()


def test_unchanged_acknowledgements_reuse_memberships_then_renew_readiness(tmp_path):
    db, store, entry, plan = claimed(tmp_path)
    try:
        first = acknowledge_handoffs(store, now=NOW + timedelta(seconds=1))
        for second in range(2, 61):
            receipt = acknowledge_handoffs(store, now=NOW + timedelta(seconds=second))
            assert receipt.id == first.id
            assert receipt.valid_until > NOW + timedelta(seconds=second)
        with db.transaction() as session:
            assert (
                session.scalar(select(func.count()).select_from(table("core_guardian_memberships")))
                == 1
            )
            assert current_receipts(session, plan_id=plan.id)[0].id == first.id
        renewed = acknowledge_handoffs(store, now=NOW + timedelta(seconds=62))
        assert renewed.id != first.id
        assert renewed.valid_until > first.valid_until
        with db.transaction() as session:
            assert {r.id for r in historical_receipts(session, plan.id)} == {
                first.id,
                renewed.id,
            }
        # A changed live set must publish immediately, inside the reuse interval.
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            record_pre_send_refusal(
                session,
                intent,
                OrderPurpose.ENTRY,
                ("EXPIRED",),
                clock=lambda: NOW,
            )
            repo.transition_order_intent(
                intent.intent_id,
                IntentState.ERROR,
                expected_current=IntentState.ORDER_CREATED,
            )
        changed = acknowledge_handoffs(store, now=NOW + timedelta(seconds=63))
        assert changed.id != renewed.id
        assert changed.protection_ids == ()
        with db.transaction() as session:
            assert current_receipts(session, plan_id=plan.id) == []
            assert len(historical_receipts(session, plan.id)) == 2
    finally:
        db.close()
