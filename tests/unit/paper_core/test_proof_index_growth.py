"""Repeated inventory polls must not multiply the historical fill index."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select
from tests.fixtures.core_m2 import NOW, fee_events, prepare

from quantbot.paper_core.contracts import ReconciliationProof
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.storage import StateConflictError


def test_identical_fill_sets_share_index_across_restart(tmp_path):
    db, store, _, proof, _, _ = prepare(tmp_path)
    fills = tuple(f"retained-fill-{i}" for i in range(300))
    try:
        for number in range(12):
            if number == 6:
                store = CoreStore(db, store.config)
            candidate = proof.model_copy(
                update={
                    "fill_ids": fills,
                    "source_refs": fills,
                    "generation": number + 2,
                    "recorded_at_utc": NOW + timedelta(seconds=15 * number),
                }
            )
            store.record("core_reconciliation_proofs", candidate)
            assert (
                store.read("core_reconciliation_proofs", candidate.id, ReconciliationProof)
                == candidate
            )
        with db.transaction() as session:
            assert session.scalar(
                select(func.count()).select_from(table("core_proof_fill_memberships"))
            ) == len(fills)
        changed = candidate.model_copy(update={"fill_ids": (*fills, "new-fill")})
        store.record("core_reconciliation_proofs", changed)
        with db.transaction() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(table("core_proof_fill_memberships"))
                )
                == len(fills) * 2 + 1
            )
        # Replaying a proof is idempotent; changed evidence under its ID is refused.
        store.record("core_reconciliation_proofs", candidate)
        with db.transaction() as session, pytest.raises(StateConflictError):
            put(session, "core_reconciliation_proofs", candidate.id, changed)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_shared_index_never_inherits_anchor_certification_or_cutoff(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle, synchronize
    from quantbot.paper_core.outcomes import OutcomeIncomplete, _resolve, import_broker_events
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, fee_events(store, broker))
        await synchronize(store, broker, now=broker.now)
        old_cutoff = broker.now
        with db.transaction() as session:
            # Isolate cash-certified fallback; the un-certified anchor still indexes fills.
            session.execute(delete(table("core_quantity_certifications")))
            original = _resolve(store, session, decision.id, old_cutoff)
            certs = table("core_inventory_certifications")
            revoked = table("core_cash_authority_revocations")
            # All earlier cash certificates are revoked; their fill indexes remain.
            for snapshot in session.scalars(
                select(certs.c.snapshot_id).where(
                    certs.c.snapshot_id.not_in(select(revoked.c.snapshot_id))
                )
            ):
                session.execute(
                    revoked.insert().values(
                        snapshot_id=snapshot,
                        reason="TEST_CASH_REVOCATION",
                    )
                )
        broker.now += timedelta(seconds=15)
        await synchronize(store, broker, now=broker.now)
        current = store.latest_proof()
        with db.transaction() as session:
            with pytest.raises(OutcomeIncomplete, match="OWNED_FLAT_RECONCILIATION"):
                _resolve(store, session, decision.id, old_cutoff)
            updated = _resolve(store, session, decision.id, broker.now)
        assert updated.reconciliation_id == current.id != original.reconciliation_id
        assert updated.gross_pnl == original.gross_pnl
    finally:
        db.close()
