"""Filled economic work must not wait behind a historical unsent-claim backlog."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update
from tests.fixtures.core_m2 import NOW, prepare

from quantbot.domain import IntentState
from quantbot.paper_core import outcomes
from quantbot.paper_core.guardian import guardian_cycle, synchronize
from quantbot.paper_core.store import put, table
from quantbot.paper_core.trader import trader_cycle
from quantbot.paper_core.work import dirty
from quantbot.storage import StorageRepository
from quantbot.storage.database import encode_utc


@pytest.mark.asyncio
@pytest.mark.parametrize("obstacle", [None, "incomplete", "quarantined"])
async def test_filled_lot_runs_before_older_unsent_work_without_bypassing_resolver(
    tmp_path, monkeypatch, obstacle
):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert decision.action == "BUY"
        # Suppress only the scheduled invocation while assembling a real closed lot;
        # the actual resolver runs below with the backlog already present.
        if obstacle != "incomplete":
            broker.now = NOW + timedelta(minutes=30)
            with monkeypatch.context() as setup:
                setup.setattr(outcomes, "process_outcomes", lambda *args, **kwargs: None)
                await guardian_cycle(store, broker, now=broker.now)
        await synchronize(store, broker, now=broker.now)
        assert not store.documents("core_round_trips")
        with db.transaction() as session:
            repo = StorageRepository(session)
            entry = dict(
                session.execute(
                    select(table("core_intent_links")).where(
                        table("core_intent_links").c.lot_id == decision.id,
                        table("core_intent_links").c.role == "ENTRY",
                    )
                )
                .mappings()
                .one()
            )
            original = repo.get_order_intent(entry["intent_id"])
            assert original is not None
            for number in range(20):
                lot = f"historical-unsent-{number:02}"
                intent = original.model_copy(
                    update={
                        "intent_id": lot,
                        "client_order_id": lot,
                        "state": IntentState.ERROR,
                    }
                )
                repo.create_order_intent(intent)
                old_observation = observation.model_copy(update={"raw_data_digest": lot})
                put(session, "core_observations", old_observation.id, old_observation)
                session.execute(
                    table("core_intent_links")
                    .insert()
                    .values(
                        **dict(entry, intent_id=lot, lot_id=lot, observation_id=old_observation.id)
                    )
                )
                repo.record_order_event(
                    lot,
                    event_type="CORE_ENTRY_SEND_REFUSED",
                    occurred_at=NOW,
                    intent_id=lot,
                    detail={
                        "definitively_unsent": True,
                        "state_at_refusal": "ORDER_CREATED",
                        "guard_contract": "core-final-send-v1",
                    },
                )
                dirty(session, (lot,), available_at=NOW, _initial_claim=True)
            session.execute(
                update(table("core_outcome_work"))
                .where(table("core_outcome_work").c.lot_id == decision.id)
                .values(attempted_at=encode_utc(NOW))
            )
            if obstacle == "quarantined":
                repo.create_incident(
                    "source-conflict",
                    severity="ERROR",
                    kind="CORE_EVIDENCE_CONFLICT",
                    message="isolated source conflict",
                    occurred_at=broker.now,
                    detail={"lot_ids": [decision.id]},
                )
        batch = outcomes.process_outcomes(store, cutoff=broker.now)
        with db.transaction() as session:
            attempted = session.execute(
                select(table("core_outcome_work").c.attempted_at).where(
                    table("core_outcome_work").c.lot_id == decision.id
                )
            ).scalar_one()
            assert attempted == encode_utc(broker.now)
        if obstacle is None:
            assert len(batch.outcomes) == 1 and batch.outcomes[0].lot_id == decision.id
            assert batch.outcomes[0].open_quantity == 0
            assert batch.outcomes[0].status == "COSTS_PENDING"
        else:
            assert not batch.outcomes and not store.documents("core_round_trips")
            reason = (
                "SOURCE_EVIDENCE_QUARANTINED"
                if obstacle == "quarantined"
                else "NONEMPTY_OWNED_FLAT_UNAVAILABLE"
            )
            assert f"{decision.id}:{reason}" in batch.reasons
    finally:
        db.close()
