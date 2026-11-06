"""Fresh Guardian inventory can recover its own acquisition errors without strategy bars."""

from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import prepare

from quantbot.paper_core.guardian import guardian_cycle, synchronize
from quantbot.paper_core.health import active_health, report
from quantbot.paper_core.time_evidence import TimeEvidence
from quantbot.storage import StorageRepository


def active_ids(store):
    with store.database.transaction() as session:
        return {item.id for item in active_health(session)}


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["GUARDIAN", "TRADER"])
@pytest.mark.parametrize(
    "reason",
    [
        "INVENTORY_PROTOCOL_INVALID",
        "OWNED_FILL_ACTIVITY_PENDING",
        "RECONCILIATION_AUTHORITY_CHANGED",
    ],
)
async def test_guardian_recovers_inventory_without_observations(tmp_path, role, reason):
    db, store, broker, _, clocks = await prepare(
        tmp_path, initial_wall=datetime(2026, 10, 14, 13, 29, tzinfo=UTC), warmup_seconds=0
    )
    try:
        failure = report(
            store,
            role=role,
            state="PROTOCOL_FAILURE",
            reasons=(reason,),
            now=clocks.wall,
            scope="ACCOUNT",
            scope_id=store.config.account_id,
        )
        unrelated = report(
            store,
            role="GUARDIAN",
            state="RETRYABLE_FAILURE",
            reasons=("OWNED_EXIT_RETRY_PENDING",),
            now=clocks.wall,
            scope="LOT",
            scope_id="owned-lot",
        )
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert receipt.proof_id
        assert failure not in active_ids(store)
        assert unrelated in active_ids(store)
        assert not store.documents("core_observations")
        recoveries = [
            r for r in store.documents("core_health_events") if failure in r["recovered_from"]
        ]
        assert len(recoveries) == 1
        assert recoveries[0]["new_proof_id"] == receipt.proof_id
        assert store.runtime_time_evidence_id in recoveries[0]["evidence_ids"]
        await guardian_cycle(store, broker, now=clocks.wall)
        assert (
            len(
                [r for r in store.documents("core_health_events") if failure in r["recovered_from"]]
            )
            == 1
        )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "obstacle",
    [
        "same_generation",
        "stale",
        "forged_time",
        "manual",
        "integrity",
        "unrelated",
        "mixed",
        "protection_blocked",
    ],
)
@pytest.mark.parametrize(
    "reason",
    [
        "INVENTORY_ACQUISITION_RETRYABLE",
        "OWNED_FILL_ACTIVITY_PENDING",
        "RECONCILIATION_AUTHORITY_CHANGED",
    ],
)
async def test_inventory_recovery_cannot_clear_without_exact_current_authority(
    tmp_path, obstacle, reason
):
    from quantbot.paper_core.health import recover_inventory_health

    db, store, broker, _, clocks = await prepare(tmp_path)
    try:
        store.runtime_role = "guardian"
        await guardian_cycle(store, broker, now=clocks.wall)
        failure = report(
            store,
            role="GUARDIAN",
            state="RETRYABLE_FAILURE",
            reasons=("OTHER_PROTOCOL_FAILURE",)
            if obstacle == "unrelated"
            else (reason, "TIME_ACQUISITION_RETRYABLE")
            if obstacle == "mixed"
            else (reason,),
            now=clocks.wall,
            scope="ACCOUNT",
            scope_id=store.config.account_id,
            protection_blocked=obstacle == "protection_blocked",
        )
        timing = store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence)
        if obstacle != "same_generation":
            await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        if obstacle == "stale":
            clocks.advance(121)
        elif obstacle == "forged_time":
            timing = timing.model_copy(update={"release_id": "forged-source"})
        elif obstacle == "manual":
            with db.transaction() as session:
                StorageRepository(session).set_kill_switch(
                    True, reason="test", updated_at=clocks.wall
                )
        elif obstacle == "integrity":
            report(
                store,
                role="GUARDIAN",
                state="INTEGRITY_FAILURE",
                reasons=("INVENTORY_MISMATCH",),
                now=clocks.wall,
                scope="ACCOUNT",
                scope_id=store.config.account_id,
            )
        recover_inventory_health(store, proof, timing, now=clocks.wall)
        assert failure in active_ids(store)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_guardian_clears_pending_fill_only_after_canonical_activity_and_matching_proof(
    tmp_path,
):
    from tests.integration.paper_core.test_daily_paper_loop import DailyProvider
    from tests.integration.paper_core.test_paper_funds import enter

    class DelayedFillProvider(DailyProvider):
        def __init__(self, clocks):
            super().__init__(clocks)
            self.activity_lag_seconds = 60

    db, store, broker, _, clocks = await prepare(tmp_path, provider_factory=DelayedFillProvider)
    try:
        decision, reasons = await enter(store, broker)
        assert decision.action == "BUY" and not reasons
        store.runtime_role = "guardian"
        with pytest.raises(TimeoutError, match="OWNED_FILL_ACTIVITY_PENDING"):
            await synchronize(store, broker, now=clocks.wall)
        with db.transaction() as session:
            pending = next(
                event
                for event in active_health(session)
                if event.objections == ("OWNED_FILL_ACTIVITY_PENDING",)
            )
            assert not StorageRepository(session).list_fills()
        await guardian_cycle(store, broker, now=clocks.wall)
        assert pending.id in active_ids(store)
        with db.transaction() as session:
            assert not StorageRepository(session).list_fills()
        observation_ids = {
            document["recorded_at_utc"] for document in store.documents("core_observations")
        }
        clocks.advance(60)
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        assert receipt.proof_id == proof.id
        assert proof.complete and proof.actual_quantities == proof.expected_quantities
        with db.transaction() as session:
            fills = StorageRepository(session).list_fills()
            assert fills and set(proof.fill_ids) == {fill.fill_id for fill in fills}
        assert pending.id not in active_ids(store)
        assert {
            document["recorded_at_utc"] for document in store.documents("core_observations")
        } == observation_ids
        recovery = next(
            event
            for event in store.documents("core_health_events")
            if pending.id in event["recovered_from"]
        )
        assert recovery["new_proof_id"] == proof.id
        assert store.runtime_time_evidence_id in recovery["evidence_ids"]
    finally:
        db.close()
