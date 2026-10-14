"""Fresh recovery cannot use a superseded or disputed authority."""

import json
from datetime import timedelta

import pytest
from tests.fixtures.core_m2 import fee_events
from tests.fixtures.core_m3 import fresh, prepare


@pytest.mark.asyncio
async def test_reconciliation_generation_race_is_retryable_not_integrity(tmp_path, monkeypatch):
    from quantbot.paper_core import guardian
    from quantbot.paper_core.store import certify_inventory
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    original = guardian.certify_inventory

    def competing_certification(store, proof):
        peer = proof.model_copy(update={"acquisition_id": "competing-valid-generation"})
        certify_inventory(store, peer)
        return original(store, proof)

    monkeypatch.setattr(guardian, "certify_inventory", competing_certification)
    try:
        refused = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        assert refused.action == "WAIT" and not broker.posts
        assert not any(
            h["state"] == "INTEGRITY_FAILURE" for h in store.documents("core_health_events")
        )
        monkeypatch.setattr(guardian, "certify_inventory", original)
        clocks.advance(1)
        resumed = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert resumed.action == "BUY" and len(broker.posts) == 1
        print(
            "M3_REPAIR:"
            + json.dumps(
                {
                    "case": "generation-race",
                    "refused": refused.id,
                    "resumed": resumed.id,
                    "posts": len(broker.posts),
                    "health": store.documents("core_health_events"),
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_disputed_current_cash_cannot_borrow_an_older_independent_authority(tmp_path):
    from quantbot.paper_core.contracts import BrokerEvent, CashActivity
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import _source_id, import_broker_events, process_outcomes
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import StorageRepository

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        first = await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        clocks.advance(1801)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, fee_events(store, broker))
        process_outcomes(store, cutoff=broker.now)
        events = []
        for number in range(2):
            clocks.advance(1)
            broker.now += timedelta(seconds=1)
            activity = CashActivity(
                recorded_at_utc=broker.now,
                account_id=store.config.account_id,
                document_id=f"independent-cash-{number}",
                activity_ids=(f"buy-{number}", f"sell-{number}"),
                complete=True,
                watermark=broker.now,
                movements=tuple(
                    (f.fill_id, (-1 if f.side.value == "BUY" else 1) * f.quantity * f.price)
                    for f in broker.fills
                ),
            )
            event = BrokerEvent(
                recorded_at_utc=broker.now,
                occurred_at=broker.now,
                account_id=store.config.account_id,
                endpoint="synthetic/cash",
                event_id=activity.id,
                raw_digest=content_id(activity),
                client_order_id=broker.posts[0].client_order_id,
                cash_activity=activity,
            )
            import_broker_events(store, (event,))
            events.append(event)
        with db.transaction() as session:
            StorageRepository(session).create_incident(
                "disputed-current-cash",
                severity="ERROR",
                kind="CORE_EVIDENCE_CONFLICT",
                message="disputed cash source",
                occurred_at=broker.now,
                detail={
                    "lot_ids": [first.id],
                    "implicated_source_ids": [_source_id(events[-1])],
                    "disputed_authorities": ["CASH"],
                },
            )
        refused = await trader_cycle(
            store, broker, fresh(observation, clocks), model, proof, now=clocks.wall
        )
        assert refused.action == "WAIT" and len(broker.posts) == 2
        clocks.advance(1)
        broker.now += timedelta(seconds=1)
        activity = events[-1].cash_activity.model_copy(
            update={
                "recorded_at_utc": broker.now,
                "watermark": broker.now,
                "document_id": "fresh-independent-cash-third",
                "activity_ids": ("third-buy", "third-sell"),
            }
        )
        current = events[-1].model_copy(
            update={
                "recorded_at_utc": broker.now,
                "occurred_at": broker.now,
                "cash_activity": activity,
                "event_id": activity.id,
                "raw_digest": content_id(activity),
            }
        )
        import_broker_events(store, (current,))
        resumed = await trader_cycle(
            store, broker, fresh(observation, clocks, sequence=3), model, proof, now=clocks.wall
        )
        assert resumed.action == "BUY" and len(broker.posts) == 3
        print(
            "M3_REPAIR:"
            + json.dumps(
                {
                    "case": "current-cash-scope",
                    "old_cash": events[0].cash_activity.id,
                    "disputed_current": events[-1].cash_activity.id,
                    "new_current": activity.id,
                    "refused": refused.id,
                    "resumed": resumed.id,
                    "posts": len(broker.posts),
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_separate_guardian_projects_clock_past_deadline_after_awaited_acquisition(tmp_path):
    from tests.fixtures.core_m2 import NOW
    from tests.fixtures.core_m3 import Clocks

    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.time_evidence import BrokerTime
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model, clocks = prepare(tmp_path)
    try:
        await trader_cycle(store, broker, observation, model, proof, now=clocks.wall)
        independent = Clocks()
        independent.process_id = "guardian-independent-deadline"
        independent.wall = NOW + timedelta(seconds=1798)
        independent.mono = 9999999

        async def sample():
            independent.advance(0.01)
            broker.now = independent.wall
            return BrokerTime(
                timestamp=broker.now,
                account_id=broker.account_id,
                provider=broker.provider,
                session_id="2026-10-14",
                session_open=NOW - timedelta(hours=1),
                session_close=observation.session_close,
                resolution_seconds=0.001,
            )

        broker.get_clock = sample
        original = broker.get_account
        first = True

        async def delayed():
            nonlocal first
            if first:
                independent.advance(3)
                broker.now = independent.wall
                first = False
            return await original()

        broker.get_account = delayed
        peer = CoreStore(db, store.config, clocks=independent)
        assert (await guardian_cycle(peer, broker, now=independent.wall)).exit_intent_ids
        assert len(broker.posts) == 2 and broker.posts[-1].quantity == 5
        assert len(store.documents("core_round_trips")) == 1
        print(
            "M3_REPAIR:"
            + json.dumps(
                {
                    "case": "guardian-delayed-acquisition",
                    "trader_process": clocks.process_id,
                    "guardian_process": independent.process_id,
                    "provider_now": broker.now.isoformat(),
                    "posts": len(broker.posts),
                    "quantity": str(broker.posts[-1].quantity),
                    "outcomes": store.documents("core_round_trips"),
                }
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_legacy_post_entry_preserves_original_reconciliation_exception(tmp_path, monkeypatch):
    from tests.fixtures.core_m2 import NOW
    from tests.fixtures.core_m2 import prepare as legacy_prepare

    from quantbot.paper_core import guardian
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import StateConflictError

    db, store, broker, proof, observation, model = legacy_prepare(tmp_path)

    async def conflict(*args, **kwargs):
        raise StateConflictError("synthetic legacy reconciliation conflict")

    monkeypatch.setattr(guardian, "synchronize", conflict)
    try:
        with pytest.raises(StateConflictError, match="legacy reconciliation"):
            await trader_cycle(store, broker, observation, model, proof, now=NOW)
        assert len(broker.posts) == 1
        print(
            "M3_REPAIR:"
            + json.dumps(
                {
                    "case": "legacy-exception",
                    "exception": "StateConflictError",
                    "posts": len(broker.posts),
                }
            )
        )
    finally:
        db.close()
