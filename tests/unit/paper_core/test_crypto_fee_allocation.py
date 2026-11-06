"""Order-scoped provider fixtures prove coin debits without inventing fee time or USD."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from tests.contract.test_alpaca_paper_broker import ScriptedBrokerTransport, adapter, response
from tests.unit.paper_core.test_crypto_costs import ASSET, fee_row
from tests.unit.paper_core.test_crypto_custody import NOW, owned_fixture

from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker


async def fixture(tmp_path, monkeypatch, responses):
    db, store, simulated, decision, _ = await owned_fixture(tmp_path)
    native = adapter(ScriptedBrokerTransport(responses))
    native.register_crypto_asset(ASSET)
    broker = AlpacaCoreBroker(
        native,
        None,
        None,
        SimpleNamespace(cost_contract=None),
        SimpleNamespace(sample=lambda: (simulated.now, 1.0)),
        account_id=store.config.account_id,
    )
    monkeypatch.setattr(
        "quantbot.paper_core.authority.commission_in_session",
        lambda *_: SimpleNamespace(approved_at=NOW - timedelta(seconds=1), id="fixture-commission"),
    )
    return db, store, simulated, decision, broker


async def test_order_filter_allocates_date_only_fee_once_without_making_time_or_usd(
    tmp_path, monkeypatch
):
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        lot_asset_fees,
        retain_order_fee_allocations,
    )

    row = fee_row()
    db, store, simulated, decision, broker = await fixture(
        tmp_path, monkeypatch, [response(200, [row])]
    )
    try:
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
        assert len(sources) == 1
        assert sources[0].pages[0].rows[0] == row
        with db.transaction() as session:
            retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
            retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
            fees = lot_asset_fees(store, session, decision.id, as_of=simulated.now)
            quantities, _, _ = store.ledger(session)
            earlier = lot_asset_fees(store, session, decision.id, as_of=NOW)
        assert fees.quantity == Decimal(".000025") and len(fees.allocation_ids) == 1
        assert earlier.quantity == 0
        assert quantities["BTC/USD"] == Decimal(".009975")
        events = store.documents("core_external_owner_events")
        assert len(events) == 1 and events[0]["occurred_at"] is None
        assert events[0]["baseline_binding"] == "ORDER_FILTER_POST_BASELINE"
        assert not store.documents("core_cost_items")
        assert not await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
    finally:
        db.close()


async def test_empty_query_reuses_audit_document_after_backoff_and_restart(tmp_path, monkeypatch):
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        retain_order_fee_allocations,
    )
    from quantbot.paper_core.store import CoreStore

    row = fee_row()
    db, store, simulated, _, broker = await fixture(
        tmp_path, monkeypatch, [response(200, []), response(200, [])]
    )
    try:
        for _ in range(2):
            sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
            with db.transaction() as session:
                assert not retain_order_fee_allocations(
                    store, session, sources, recorded_at=simulated.now
                )
            assert not await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
            simulated.now += timedelta(minutes=6)
            store = CoreStore(db, store.config)
        queries = [
            d
            for d in store.documents("core_provider_acquisitions")
            if d.get("contract") == "ALPACA_ORDER_FILTER_FEE_QUERY_V1"
        ]
        assert len(queries) == 1 and queries[0]["protocol_complete"]
        assert not store.documents("core_external_owner_events")
    finally:
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"qty": ".000025"},
        {"status": "pending"},
        {"net_amount": "1"},
        {"symbol": "ETHUSD"},
        {"order_id": "foreign-order"},
        {"transaction_time": (NOW + timedelta(days=1)).isoformat()},
    ],
)
async def test_nonqualifying_order_fee_never_debits_lot(tmp_path, monkeypatch, change):
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        lot_asset_fees,
        retain_order_fee_allocations,
    )

    db, store, simulated, decision, broker = await fixture(
        tmp_path, monkeypatch, [response(200, [{**fee_row(), **change}])]
    )
    try:
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(fee_row(),))
        with db.transaction() as session:
            retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
            assert lot_asset_fees(store, session, decision.id, as_of=simulated.now).quantity == 0
        assert not store.documents("core_external_owner_events")
    finally:
        db.close()


async def test_existing_precise_account_debit_is_not_debited_again_by_lot_allocation(
    tmp_path, monkeypatch
):
    from quantbot.paper_core.crypto_costs import parse_fee_activity, retain_fee_activity
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        lot_asset_fees,
        retain_order_fee_allocations,
    )

    row = fee_row(transaction_time=(NOW + timedelta(seconds=1)).isoformat())
    db, store, simulated, decision, broker = await fixture(
        tmp_path, monkeypatch, [response(200, [row])]
    )
    try:
        with db.transaction() as session:
            fee = retain_fee_activity(
                session,
                parse_fee_activity(
                    row,
                    symbol_aliases=broker.adapter.crypto_symbol_aliases,
                    source_document_id="original-source",
                ),
                account_id=store.config.account_id,
                received_at=simulated.now,
            )
            AlpacaCoreBroker.record_external(
                session,
                fee.external_quantity_event(
                    account_id=store.config.account_id, baseline_at=NOW - timedelta(seconds=1)
                ),
            )
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
        with db.transaction() as session:
            retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
            assert lot_asset_fees(
                store, session, decision.id, as_of=simulated.now
            ).quantity == Decimal(".000025")
            assert store.ledger(session)[0]["BTC/USD"] == Decimal(".009975")
        assert len(store.documents("core_external_owner_events")) == 1
        assert (
            store.documents("core_external_owner_events")[0]["occurred_at"]
            == row["transaction_time"]
        )
    finally:
        db.close()


async def test_fee_exceeding_owned_buy_rejects_without_debit(tmp_path, monkeypatch):
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        retain_order_fee_allocations,
    )
    from quantbot.storage import StateConflictError

    row = {**fee_row(), "qty": "-.02"}
    db, store, simulated, _, broker = await fixture(tmp_path, monkeypatch, [response(200, [row])])
    try:
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
        with pytest.raises(StateConflictError, match="CRYPTO_FEE_QUANTITY_EXCEEDS"):
            with db.transaction() as session:
                retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
        assert not store.documents("core_external_owner_events")
    finally:
        db.close()


async def test_equity_zero_fee_contract_is_not_called_for_registered_crypto_fill(
    tmp_path, monkeypatch
):
    from tests.fixtures.core_m2 import events

    db, store, simulated, _, broker = await fixture(tmp_path, monkeypatch, [])
    try:
        fill = simulated.fills[0]
        row = dict(
            activity_type="FILL",
            id=fill.fill_id,
            order_id=fill.broker_order_id,
            symbol="BTCUSD",
            side="buy",
            qty=str(fill.quantity),
            price=str(fill.price),
            transaction_time=fill.occurred_at.isoformat(),
            cum_qty=str(fill.quantity),
        )
        broker.adapter._transport = ScriptedBrokerTransport([response(200, [row])])

        def wrong_fee(*args, **kwargs):
            raise AssertionError("equity cost contract applied to crypto")

        broker.config.cost_contract = SimpleNamespace(id="equity-contract", fee=wrong_fee)
        monkeypatch.setattr(broker, "_settled_order_event", lambda *_: events(store, simulated)[0])
        assert all(e.cost is None for e in await broker.owned_events(store))
    finally:
        db.close()


async def test_duplicate_pagination_keeps_incomplete_source_without_allocating(
    tmp_path, monkeypatch
):
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        retain_order_fee_allocations,
    )

    page = [{**fee_row(), "id": f"fee-{i}"} for i in range(100)]
    db, store, simulated, _, broker = await fixture(
        tmp_path, monkeypatch, [response(200, page), response(200, [page[-1]])]
    )
    try:
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(fee_row(),))
        assert not sources[0].protocol_complete and sources[0].objections
        assert sources[0].pages[1].page_token == "fee-99"
        with db.transaction() as session:
            assert not retain_order_fee_allocations(
                store, session, sources, recorded_at=simulated.now
            )
        assert not store.documents("core_external_owner_events")
    finally:
        db.close()


async def test_foreign_owner_prevents_order_query_and_allocation(tmp_path, monkeypatch):
    from quantbot.domain import IntentState
    from quantbot.paper_core.crypto_fee_allocation import acquire_order_fee_sources
    from quantbot.storage import StorageRepository

    db, store, simulated, _, broker = await fixture(tmp_path, monkeypatch, [])
    try:
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.get_order_intent(store.links()[0]["intent_id"])
            repo.create_order_intent(
                intent.model_copy(
                    update=dict(
                        intent_id="foreign",
                        client_order_id="foreign",
                        strategy_id="foreign-owner",
                        state=IntentState.SUBMISSION_UNKNOWN,
                    )
                )
            )
        assert not await acquire_order_fee_sources(broker, store, simulated.now, rows=(fee_row(),))
    finally:
        db.close()


@pytest.mark.parametrize("fault", ["binding", "orphan"])
async def test_retained_allocation_rejects_changed_relative_binding_or_orphan(
    tmp_path, monkeypatch, fault
):
    import json

    from sqlalchemy import select

    from quantbot.paper_core.crypto_fee_allocation import (
        ALLOCATION_INDEX,
        acquire_order_fee_sources,
        lot_asset_fees,
        retain_order_fee_allocations,
    )
    from quantbot.paper_core.store import table
    from quantbot.research.manifest import canonical_result_json
    from quantbot.storage import StateConflictError

    row = fee_row()
    db, store, simulated, decision, broker = await fixture(
        tmp_path, monkeypatch, [response(200, [row])]
    )
    try:
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
        with db.transaction() as session:
            retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
        with db.transaction() as session:
            if fault == "orphan":
                session.execute(
                    table("core_document_index")
                    .delete()
                    .where(table("core_document_index").c.kind == ALLOCATION_INDEX)
                )
            else:
                target = table("core_external_owner_events")
                payload = json.loads(session.execute(select(target.c.payload)).scalar_one())
                payload["order_id"] = "wrong"
                session.execute(target.update().values(payload=canonical_result_json(payload)))
        with db.transaction() as session:
            with pytest.raises(StateConflictError, match="CRYPTO_FEE_ALLOCATION_UNVERIFIED"):
                lot_asset_fees(store, session, decision.id, as_of=simulated.now)
    finally:
        db.close()


async def closed_fixture(tmp_path, monkeypatch, responses):
    from tests.fixtures.core_m2 import events

    from quantbot.paper_core.contracts import RoundTrip
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes

    db, store, simulated, decision, broker = await fixture(tmp_path, monkeypatch, responses)
    simulated.now += timedelta(minutes=30)
    simulated.set_mark("BTC/USD", Decimal(50000), as_of=simulated.now)
    receipt = await guardian_cycle(store, simulated, now=simulated.now)
    import_broker_events(store, events(store, simulated))
    batch = process_outcomes(store, cutoff=simulated.now)
    trips = tuple(RoundTrip.model_validate(d) for d in store.documents("core_round_trips"))
    assert len(trips) == 1, (receipt, batch)
    return db, store, simulated, decision, broker, trips[0]


@pytest.mark.parametrize("fee_quantity", ["-.000025", "-.02"])
async def test_new_fee_after_closed_lot_preserves_history_and_quarantines_outcome(
    tmp_path, monkeypatch, fee_quantity
):
    from sqlalchemy import select

    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        retain_order_fee_allocations,
    )
    from quantbot.paper_core.outcomes import process_outcomes, quarantined_lots
    from quantbot.paper_core.store import table

    row = {**fee_row(), "qty": fee_quantity}
    db, store, simulated, decision, broker, trip = await closed_fixture(
        tmp_path, monkeypatch, [response(200, [row])]
    )
    try:
        simulated.now += timedelta(seconds=1)
        sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
        assert len(sources) == 1
        with db.transaction() as session:
            retain_order_fee_allocations(store, session, sources, recorded_at=simulated.now)
            assert decision.id in quarantined_lots(session)
            assert (
                session.execute(select(table("core_outcome_index").c.status)).scalar_one()
                == "INTEGRITY_FAILURE"
            )
        from quantbot.paper_core.contracts import RoundTrip

        assert trip.id in {
            RoundTrip.model_validate(d).id for d in store.documents("core_outcome_revisions")
        }
        assert not process_outcomes(store, cutoff=simulated.now).outcomes
    finally:
        db.close()


async def test_old_closed_order_fee_stays_unallocated_with_deduplicated_gap(tmp_path, monkeypatch):
    from quantbot.paper_core.crypto_fee_allocation import (
        acquire_order_fee_sources,
        retain_order_fee_allocations,
    )

    row = fee_row()
    db, store, simulated, _, broker, _ = await closed_fixture(tmp_path, monkeypatch, [])
    try:
        simulated.now += timedelta(days=3)
        for _ in range(2):
            sources = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
            assert not sources
            with db.transaction() as session:
                retain_order_fee_allocations(
                    store, session, sources, recorded_at=simulated.now, observed_rows=(row,)
                )
            simulated.now += timedelta(seconds=1)
        gaps = [
            d
            for d in store.documents("core_provider_acquisitions")
            if d.get("contract") == "CRYPTO_FEE_ALLOCATION_GAP_V1"
        ]
        assert len(gaps) == 1 and gaps[0]["reason"] == "OUTSIDE_BOUNDED_ORDER_SEARCH_UNALLOCATED"
        assert not store.documents("core_external_owner_events")
    finally:
        db.close()
