"""Reuse immediate intent reads without weakening definitive no-send evidence."""

from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.integration.paper_core.test_resource_loop_recovery import ResourceProvider
from tests.unit.test_storage import NOW, make_broker_order, make_intent

from quantbot.domain import IntentState
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.portfolio import portfolio_budget
from quantbot.paper_core.settlement import definitively_unsent, definitively_unsent_many
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.storage import Database, StorageRepository


def test_batch_settlement_preserves_broker_evidence_and_ambiguous_states(tmp_path):
    db = Database.create(tmp_path / "settlement.db")
    cases = (
        ("error_refused", IntentState.ERROR, True, False, True),
        ("error_no_evidence", IntentState.ERROR, False, False, False),
        ("error_with_order", IntentState.ERROR, True, True, False),
        ("local_rejected", IntentState.REJECTED, True, False, True),
        ("rejected_no_evidence", IntentState.REJECTED, False, False, False),
        ("rejected_with_order", IntentState.REJECTED, True, True, False),
        ("submitting", IntentState.SUBMITTING, True, False, False),
        ("unknown", IntentState.SUBMISSION_UNKNOWN, True, False, False),
        ("prepared", IntentState.ORDER_CREATED, True, False, False),
    )
    try:
        with db.transaction() as session:
            repo = StorageRepository(session)
            for identity, state, evidence, broker_order, _ in cases:
                intent = make_intent(intent_id=identity, client_order_id=identity, state=state)
                repo.create_order_intent(intent)
                if evidence:
                    rejected = state is IntentState.REJECTED
                    repo.record_order_event(
                        "event-" + identity,
                        event_type="CORE_LOCAL_SEND_NOT_ATTEMPTED"
                        if rejected
                        else "CORE_ENTRY_SEND_REFUSED",
                        occurred_at=NOW,
                        intent_id=identity,
                        detail={
                            "definitively_unsent": True,
                            "contract": "ACCOUNT_BUDGET_BEFORE_IO_V1",
                            "client_order_id": identity,
                            "guard_contract": "core-final-send-v1",
                            "state_at_refusal": "ORDER_CREATED",
                        },
                    )
                if broker_order:
                    repo.save_broker_order(
                        make_broker_order(client_order_id=identity).model_copy(
                            update={"broker_order_id": "broker-" + identity}
                        )
                    )
        expected = {identity: result for identity, _, _, _, result in cases}
        expected["missing"] = False
        with db.transaction() as session:
            repo = StorageRepository(session)
            identities = [*expected, "error_refused"]
            assert definitively_unsent_many(repo, identities) == expected
            assert {
                identity: definitively_unsent(repo, identity) for identity in expected
            } == expected
            # A later transaction must observe a broker acknowledgement, never a cached verdict.
            repo.save_broker_order(
                make_broker_order(client_order_id="error_refused").model_copy(
                    update={"broker_order_id": "late-ack"}
                )
            )
        with db.transaction() as session:
            assert not definitively_unsent_many(StorageRepository(session), ["error_refused"])[
                "error_refused"
            ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_portfolio_does_not_hydrate_loaded_intents_again_for_settlement(
    tmp_path, monkeypatch
):
    db, store, broker, _, clocks = await prepare(
        tmp_path,
        provider_factory=ResourceProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        assert not (await guardian_cycle(store, broker, now=clocks.wall)).reasons
        clocks.advance(2100)
        ready(store)
        cohort = await broker.observation_cohort()
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert any(decision.action == "BUY" for decision in decisions)
        proof = store.latest_proof()
        calls = []
        original = StorageRepository.get_order_intents

        def tracked(self, identities):
            calls.append(tuple(identities))
            return original(self, identities)

        monkeypatch.setattr(StorageRepository, "get_order_intents", tracked)
        with db.transaction() as session:
            result = portfolio_budget(store, session, proof, now=clocks.wall)
        assert not result.objections, result.objections
        assert result.session_reservations == len(decisions)
        # One hydration validates the ledger; one computes the current portfolio.
        # Settlement must use the second set immediately in the same transaction.
        assert len(calls) == 2, calls
        # SQL queries without ORDER BY may choose different indexes. The
        # performance invariant is identical membership with no extra hydration.
        assert set(calls[0]) == set(calls[1])
    finally:
        db.close()
