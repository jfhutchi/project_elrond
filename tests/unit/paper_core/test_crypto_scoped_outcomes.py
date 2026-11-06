"""Unrelated coin uncertainty must not erase independently closed equity outcomes."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from tests.fixtures.core_m2 import NOW, events, prepare

from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.outcomes import (
    OutcomeIncomplete,
    import_broker_events,
    reconcile_round_trip,
)
from quantbot.paper_core.store import put, table
from quantbot.paper_core.trader import trader_cycle


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_valid,affected", [(True, False), (False, False), (True, True)])
async def test_equity_outcome_requires_verified_scope_excluding_its_symbol(
    tmp_path, monkeypatch, scope_valid, affected
):
    db, store, broker, proof, observation, model = prepare(tmp_path)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        import_broker_events(store, events(store, broker))
        original = reconcile_round_trip(store, decision.id, as_of=broker.now)
        current = store.latest_proof()
        symbol = observation.symbol if affected else "BTC/USD"
        partial = current.model_copy(
            update={
                "evidence_lane": "BROKER_PAPER",
                "complete": False,
                "cash_complete": False,
                "expected_cash": None,
                "funds_authority": "BROKER_PAPER_BALANCE_V1",
                "expected_quantities": ((symbol, Decimal(".01")),),
                "actual_quantities": ((symbol, Decimal(".009975")),),
                "quarantined_symbols": (symbol,),
                "custody_evidence_ids": ("receipt",),
            }
        )
        assert partial.quantity_scope_shape_valid
        # Isolate the resolver's consumer boundary; independent tests exercise real
        # certification and persisted scope validation. No production database is used.
        with db.transaction() as session:
            certs = table("core_quantity_certifications")
            row = dict(
                session.execute(select(certs).where(certs.c.proof_id == current.id))
                .mappings()
                .one()
            )
            session.execute(table("core_inventory_certifications").delete())
            session.execute(certs.delete())
            put(session, "core_reconciliation_proofs", partial.id, partial)
            session.execute(certs.insert().values(**(row | {"proof_id": partial.id})))
        checked = []

        def validate(*args, **kwargs):
            checked.append(args[2].id)
            return () if scope_valid else ("CRYPTO_CUSTODY_BASIS_CHANGED",)

        monkeypatch.setattr(
            "quantbot.paper_core.crypto_custody.scoped_quantity_objections", validate
        )
        if scope_valid and not affected:
            resolved = reconcile_round_trip(store, decision.id, as_of=broker.now)
            assert resolved.gross_pnl == original.gross_pnl
            assert resolved.reconciliation_id == partial.id
            assert checked == [partial.id]
        else:
            with pytest.raises(OutcomeIncomplete, match="OWNED_FLAT_RECONCILIATION"):
                reconcile_round_trip(store, decision.id, as_of=broker.now)
    finally:
        db.close()
