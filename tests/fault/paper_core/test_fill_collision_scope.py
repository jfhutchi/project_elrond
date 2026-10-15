"""A distinct source cannot hide the canonical owner of a reused fill identity."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from tests.fault.paper_core.test_review_repairs import fresh_observation
from tests.fixtures.core_m2 import events, fee_events, prepare

from quantbot.paper_core.contracts import CostItem
from quantbot.paper_core.guardian import guardian_cycle, synchronize
from quantbot.paper_core.outcomes import (
    import_broker_events,
    process_outcomes,
    reconcile_round_trip,
)
from quantbot.paper_core.store import table
from quantbot.paper_core.trader import trader_cycle
from quantbot.storage import StateConflictError, StorageRepository


@pytest.mark.asyncio
async def test_new_source_fill_id_collision_quarantines_both_owners_and_keeps_third_lot(tmp_path):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decisions = []
        for sequence in range(1, 4):
            current = (
                observation
                if sequence == 1
                else fresh_observation(observation, broker.now, sequence)
            )
            decisions.append(
                await trader_cycle(store, broker, current, model, proof, now=broker.now)
            )
            broker.now += timedelta(minutes=30)
            await guardian_cycle(store, broker, now=broker.now)
            known = {e["fill_id"] for e in store.documents("core_cost_items")}
            import_broker_events(
                store, tuple(e for e in fee_events(store, broker) if e.cost.fill_id not in known)
            )
            process_outcomes(store, cutoff=broker.now)
            await synchronize(store, broker, now=broker.now)
            proof = store.latest_proof()
            broker.now += timedelta(seconds=1)
        canonical, claimed, healthy = decisions
        original = broker.fills[0]
        batch = events(store, broker)
        incoming = next(
            e for e in batch if e.fill and e.client_order_id == broker.posts[2].client_order_id
        )
        bad = incoming.model_copy(
            update={
                "event_id": "distinct-new-source-canonical-fill-collision",
                "raw_digest": "distinct-new-collision-document",
                "fill": incoming.fill.model_copy(update={"fill_id": original.fill_id}),
            }
        )
        healthy_source = next(
            e for e in batch if e.order and e.client_order_id == broker.posts[4].client_order_id
        )
        healthy_source = healthy_source.model_copy(
            update={"event_id": "healthy-attempt-before-collision"}
        )
        before = store.documents("core_broker_event_sources")
        assert bad.event_id not in {e["event_id"] for e in before}
        with db.transaction() as session:
            cursor_before = [
                dict(r) for r in session.execute(select(table("core_source_cursors"))).mappings()
            ]
            original_row = dict(
                session.execute(
                    select(table("fills")).where(table("fills").c.fill_id == original.fill_id)
                )
                .mappings()
                .one()
            )
        envelope = (healthy_source, bad)
        with pytest.raises(StateConflictError, match="fill"):
            import_broker_events(store, envelope)
        assert store.documents("core_broker_event_sources") == before
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_fill(original.fill_id) == original
            assert (
                dict(
                    session.execute(
                        select(table("fills")).where(table("fills").c.fill_id == original.fill_id)
                    )
                    .mappings()
                    .one()
                )
                == original_row
            )
            assert [
                dict(r) for r in session.execute(select(table("core_source_cursors"))).mappings()
            ] == cursor_before
            rows = {
                r["lot_id"]: r["status"]
                for r in session.execute(select(table("core_outcome_index"))).mappings()
            }
            assert rows[canonical.id] == rows[claimed.id] == "INTEGRITY_FAILURE"
            assert rows[healthy.id] == "COMPLETE"
            incident = repo.list_incidents(unresolved_only=True)[-1]
            assert incident.detail["lot_ids"] == sorted((canonical.id, claimed.id))
            assert incident.detail["implicated_client_order_ids"] == sorted(
                (broker.posts[0].client_order_id, broker.posts[2].client_order_id)
            )
            assert incident.detail["source_events"] == [e.model_dump(mode="json") for e in envelope]
        result = process_outcomes(store, cutoff=broker.now)
        assert len(result.reasons) == 2
        assert all(healthy.id not in reason for reason in result.reasons)
        assert reconcile_round_trip(store, healthy.id, as_of=broker.now).status == "COMPLETE"
        # The healthy attempted receipt can be replayed alone after the mixed rollback.
        assert import_broker_events(store, (healthy_source,))
        assert not import_broker_events(store, (healthy_source,))
        # New authoritative cost evidence for the innocent lot still produces a
        # new current outcome while the two conflicted owners remain quarantined.
        current_costs = {
            c["fill_id"]: CostItem.model_validate(c).id for c in store.documents("core_cost_items")
        }
        healthy_fill_ids = {f.fill_id for f in broker.fills[-2:]}
        broker.now += timedelta(seconds=1)
        corrections = tuple(
            e
            for e in fee_events(store, broker, amount=".10", revision=2, supersedes=current_costs)
            if e.cost.fill_id in healthy_fill_ids
        )
        import_broker_events(store, corrections)
        progressed = process_outcomes(store, cutoff=broker.now)
        assert len(progressed.reasons) == 2
        assert len(progressed.outcomes) == 1
        updated = progressed.outcomes[0]
        assert updated.lot_id == healthy.id and updated.status == "COMPLETE"
        assert updated.net_pnl == Decimal("-.2")
        print(
            "M2_FILL_COLLISION:"
            + json.dumps(
                {
                    "canonical_lot": canonical.id,
                    "incoming_lot": claimed.id,
                    "healthy_lot": healthy.id,
                    "original_fill": original.model_dump(mode="json"),
                    "rejected_source": bad.model_dump(mode="json"),
                    "incident": incident.detail,
                    "statuses": rows,
                    "outcome_reasons": result.reasons,
                    "healthy_replay_imported": True,
                    "canonical_fill_preserved": True,
                    "atomic_source_cursor_rollback": True,
                    "healthy_updated_outcome": updated.model_dump(mode="json"),
                },
                sort_keys=True,
            )
        )
    finally:
        db.close()
