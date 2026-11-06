"""Lot closure accounts for proven coin fees without double counting USD cash."""

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.fixtures.core_m2 import events
from tests.unit.paper_core.test_crypto_custody import owned_fixture

from quantbot.domain import IntentState, OrderSide
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.crypto_fee_allocation import LotAssetFees
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.outcomes import (
    OutcomeIncomplete,
    import_broker_events,
    reconcile_round_trip,
)
from quantbot.paper_core.store import table
from quantbot.research.manifest import canonical_result_json
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("proven", [True, False])
async def test_closed_crypto_quantity_requires_attributed_fee_and_keeps_cash_costs_pending(
    tmp_path, monkeypatch, proven
):
    db, store, broker, decision, _ = await owned_fixture(tmp_path)
    try:
        # The allocation layer has independent source-validation regressions. This
        # consumer fixture supplies exact retained fee quantity; never a USD valuation.
        fee = Decimal(".000025")
        monkeypatch.setattr(
            "quantbot.paper_core.crypto_fee_allocation.lot_asset_fees",
            lambda *a, **kw: LotAssetFees(
                quantity=fee if proven else 0,
                allocation_ids=("allocation",) if proven else (),
                fee_ids=("fee",) if proven else (),
                source_ids=("query",) if proven else (),
                external_event_ids=("fee-event",) if proven else (),
            ),
        )
        broker.now += timedelta(minutes=30)
        broker.quantities["BTC/USD"] -= fee
        broker.set_mark("BTC/USD", Decimal(51000), as_of=broker.now)
        link = store.links()[0]
        with db.transaction() as session:
            repo = StorageRepository(session)
            entry = repo.get_order_intent(link["intent_id"])
            exit_intent = entry.model_copy(
                update={
                    "intent_id": "fee-aware-exit",
                    "client_order_id": "fee-aware-exit",
                    "side": OrderSide.SELL,
                    "quantity": Decimal(".009975"),
                    "state": IntentState.SUBMITTING,
                    "created_at": broker.now,
                }
            )
            repo.create_order_intent(exit_intent)
            session.execute(
                table("core_intent_links")
                .insert()
                .values(
                    **(
                        link
                        | {
                            "intent_id": exit_intent.intent_id,
                            "role": "EXIT",
                            "payload": canonical_result_json(exit_intent),
                        }
                    )
                )
            )
            AlpacaCoreBroker.record_external(
                session,
                dict(
                    account_id=store.config.account_id,
                    event_id="fee",
                    owner="ACCOUNT_CRYPTO_FEE",
                    symbol="BTC/USD",
                    quantity_delta=str(-fee),
                    net_cash="0",
                    source_document="fixture-query",
                    occurred_at=None,
                ),
            )
        await broker.submit_order(exit_intent)
        import_broker_events(store, events(store, broker))
        await synchronize(store, broker, now=broker.now)
        if not proven:
            with pytest.raises(OutcomeIncomplete, match="NONEMPTY_OWNED_FLAT_UNAVAILABLE"):
                reconcile_round_trip(store, decision.id, as_of=broker.now)
            return
        trip = reconcile_round_trip(store, decision.id, as_of=broker.now)
        assert trip.open_quantity == 0
        assert trip.asset_fee_quantity == fee
        assert trip.asset_fee_cost_basis == Decimal("1.25")
        assert trip.cashflow_pnl == Decimal("8.725")
        assert trip.gross_pnl == Decimal("9.975")
        assert trip.net_pnl is None and trip.actual_fees is None
        assert (
            trip.status == "COSTS_PENDING"
            and trip.cost_reason == "CRYPTO_USD_COST_FINALITY_PENDING"
        )
        assert "allocation" in trip.source_refs and "query" in trip.source_refs
        assert trip.gross_pnl - trip.asset_fee_cost_basis == trip.cashflow_pnl
    finally:
        db.close()
