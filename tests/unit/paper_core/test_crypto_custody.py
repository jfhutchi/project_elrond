"""Protective custody bounds never confer reconciliation or new-entry authority."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.brokers.base import BrokerAccountSnapshot
from quantbot.domain import Account, BrokerOrder, OrderSide, OrderType, Position, TimeInForce
from quantbot.storage import StateConflictError

NOW = datetime(2026, 11, 6, 16, tzinfo=UTC)


def fixture_basis(**changes):
    from quantbot.paper_core import crypto_custody

    fields = dict(
        account_id="fixture",
        store_identity="store",
        config_id="config",
        release_id="release",
        owner="owner",
        evidence_lane="SYNTHETIC_TEST",
        lot_id="lot",
        symbol="BTC/USD",
        baseline_id="baseline",
        ledger_state_id="ledger",
        ledger_generation=1,
        activity_document_id="activities",
        activity_watermark=NOW,
        gross_remaining=Decimal(".01"),
        reserved_exits=Decimal(0),
        expected_account_quantity=Decimal(".01"),
        quantity_step=Decimal(".00000001"),
        minimum_quantity=Decimal(".00000001"),
        asset_eligibility_id="asset-source",
        reservation_state_id="reservations",
        source_refs=("entry-fill", "settlement"),
        open_exit_orders=(),
    )
    return crypto_custody.CryptoLotBasis(**(fields | changes))


def account():
    return BrokerAccountSnapshot(
        account=Account(
            account_id="fixture", cash=100, buying_power=100, equity=600, currency="USD"
        ),
        account_number="SYNTHETIC",
        status="ACTIVE",
        order_entry_allowed=True,
        restrictions=(),
    )


def positions(quantity=".009975"):
    return (
        ()
        if Decimal(quantity) == 0
        else (
            Position(
                symbol="BTC/USD",
                quantity=Decimal(quantity),
                market_price=50000,
                market_value=Decimal(quantity) * 50000,
                average_entry_price=50000,
            ),
        )
    )


def build(basis=None, **changes):
    from quantbot.paper_core import crypto_custody

    fields = dict(
        basis=basis or fixture_basis(),
        funds_evidence_id="funds",
        first_account=account(),
        second_account=account(),
        first_positions=positions(),
        second_positions=positions(),
        first_open_orders=(),
        second_open_orders=(),
        capture_started=NOW - timedelta(seconds=1),
        captured_at=NOW,
        elapsed_seconds=1.0,
    )
    return crypto_custody.build_crypto_custody(**(fields | changes))


def test_fee_reduced_custody_bounds_exit_without_certifying_a_fee_or_reconciliation():
    from quantbot.paper_core import crypto_custody

    evidence = build()
    assert crypto_custody.crypto_custody_limit(evidence, fixture_basis(), now=NOW) == Decimal(
        ".009975"
    )
    assert evidence.unexplained_quantity_shortfall == Decimal(".000025")
    assert evidence.authority == "PROTECTIVE_SELL_UPPER_BOUND_ONLY"
    payload = evidence.model_dump()
    assert "actual_fees" not in payload and "net_pnl" not in payload and "complete" not in payload


def test_reserved_exits_reduce_custody_and_gross_bounds_together():
    from quantbot.paper_core import crypto_custody

    basis = fixture_basis(reserved_exits=Decimal(".005"))
    evidence = build(basis)
    assert crypto_custody.crypto_custody_limit(evidence, basis, now=NOW) == Decimal(".004975")
    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_QUANTITY_LIMIT"):
        crypto_custody.crypto_custody_limit(
            evidence, basis, now=NOW, requested_quantity=Decimal(".005")
        )


def test_proven_fees_reduce_owned_authority_without_rewriting_gross_fills():
    from quantbot.paper_core import crypto_custody

    basis = fixture_basis(
        proven_asset_fee_quantity=Decimal(".000025"),
        fee_allocation_ids=("allocation",),
        fee_activity_ids=("fee",),
        fee_source_ids=("source",),
        fee_external_event_ids=("event",),
        expected_account_quantity=Decimal(".009975"),
        reserved_exits=Decimal(".005"),
    )
    assert basis.gross_remaining == Decimal(".01")
    assert basis.owned_remaining == Decimal(".009975")
    evidence = build(basis)
    assert crypto_custody.crypto_custody_limit(evidence, basis, now=NOW) == Decimal(".004975")
    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_UNOWNED_QUANTITY"):
        build(basis, first_positions=positions(".01"), second_positions=positions(".01"))


def test_zero_allocated_fees_preserve_original_custody_document_identity():
    from quantbot.paper_core.crypto_custody import CryptoLotBasis

    basis = fixture_basis()
    explicit = CryptoLotBasis(
        **(
            basis.model_dump()
            | dict(
                proven_asset_fee_quantity=Decimal(0),
                fee_allocation_ids=(),
                fee_activity_ids=(),
                fee_source_ids=(),
                fee_external_event_ids=(),
            )
        )
    )
    assert explicit.id == basis.id
    assert "proven_asset_fee_quantity" not in explicit.model_dump()


@pytest.mark.parametrize("quantity,ids", [(".000025", False), (".02", True)])
def test_proven_fee_quantity_requires_sources_and_cannot_exceed_remaining_gross(quantity, ids):
    basis = fixture_basis(
        proven_asset_fee_quantity=Decimal(quantity),
        fee_allocation_ids=("allocation",) if ids else (),
        fee_activity_ids=("fee",) if ids else (),
        fee_source_ids=("source",) if ids else (),
        fee_external_event_ids=("event",) if ids else (),
    )
    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_FEE_AUTHORITY_INVALID"):
        build(basis)


@pytest.mark.parametrize(
    "change",
    [
        {"ledger_state_id": "changed"},
        {"ledger_generation": 2},
        {"store_identity": "other"},
        {"config_id": "other"},
        {"account_id": "other"},
        {"lot_id": "other"},
        {"reservation_state_id": "new-exit"},
        {"reserved_exits": Decimal(".001")},
    ],
)
def test_changed_ownership_or_source_binding_rejects_old_custody(change):
    from quantbot.paper_core import crypto_custody

    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_BASIS_CHANGED"):
        crypto_custody.crypto_custody_limit(build(), fixture_basis(**change), now=NOW)


def test_stale_custody_or_future_clock_cannot_authorize_exit():
    from quantbot.paper_core import crypto_custody

    for when in (NOW + timedelta(seconds=6), NOW - timedelta(microseconds=1)):
        with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_STALE"):
            crypto_custody.crypto_custody_limit(build(), fixture_basis(), now=when)


@pytest.mark.parametrize(
    "changes",
    [
        {"second_positions": positions(".0099")},
        {"elapsed_seconds": 5.01},
        {"capture_started": NOW - timedelta(seconds=6)},
        {"first_positions": positions(".0101"), "second_positions": positions(".0101")},
        {"first_positions": positions("-.01"), "second_positions": positions("-.01")},
    ],
)
def test_unstable_or_unowned_custody_cannot_create_receipt(changes):
    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_"):
        build(**changes)


def test_unknown_foreign_open_order_refuses_protective_custody():
    order = BrokerOrder(
        broker_order_id="foreign",
        client_order_id="manual",
        symbol="BTC/USD",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal(".001"),
        filled_quantity=0,
        status="accepted",
        submitted_at=NOW,
    )
    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_FOREIGN_OR_CHANGED_ORDER"):
        build(first_open_orders=(order,), second_open_orders=(order,))


def test_zero_custody_retains_shortfall_without_authorizing_nonzero_exit():
    from quantbot.paper_core import crypto_custody

    evidence = build(first_positions=(), second_positions=())
    assert evidence.unexplained_quantity_shortfall == Decimal(".01")
    assert crypto_custody.crypto_custody_limit(evidence, fixture_basis(), now=NOW) == 0
    with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_QUANTITY_LIMIT"):
        crypto_custody.crypto_custody_limit(
            evidence, fixture_basis(), now=NOW, requested_quantity=Decimal(".0001")
        )


def test_protective_quantity_rounds_down_and_retains_dust_as_nonflat():
    from quantbot.paper_core import crypto_custody

    basis = fixture_basis(quantity_step=Decimal(".0001"), minimum_quantity=Decimal(".0001"))
    evidence = build(basis)
    assert crypto_custody.crypto_custody_limit(evidence, basis, now=NOW) == Decimal(".0099")
    tiny = build(basis, first_positions=positions(".000025"), second_positions=positions(".000025"))
    assert crypto_custody.crypto_custody_limit(tiny, basis, now=NOW) == 0
    assert tiny.exit_reason == "BELOW_BROKER_MINIMUM_DUST"
    assert tiny.custody_quantity == Decimal(".000025")


def test_final_requested_quantity_must_obey_retained_precision_and_minimum():
    from quantbot.paper_core import crypto_custody

    basis = fixture_basis(quantity_step=Decimal(".0001"), minimum_quantity=Decimal(".0002"))
    evidence = build(basis)
    for quantity in (Decimal(".0001"), Decimal(".00025")):
        with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_QUANTITY_LIMIT"):
            crypto_custody.crypto_custody_limit(
                evidence, basis, now=NOW, requested_quantity=quantity
            )


async def owned_fixture(tmp_path, *, partial=False):
    from tests.fixtures.core_m2 import JournalBroker, events
    from tests.integration.paper_core.test_offline_asset_capabilities import (
        capabilities,
        observation,
    )

    from quantbot.paper_core.contracts import CoreConfig, ModelArtifact
    from quantbot.paper_core.outcomes import import_broker_events
    from quantbot.paper_core.runtime import initialize_fixture
    from quantbot.paper_core.store import CoreStore, put, table
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
    from quantbot.storage import Database

    capability = capabilities()[1]
    config = CoreConfig(
        account_id="synthetic-account", symbol="BTC/USD", asset_capabilities=(capability,)
    )
    broker = JournalBroker(tmp_path / "broker.json")
    broker.now = NOW
    broker.partial = Decimal(".004") if partial else None
    broker.set_mark("BTC/USD", Decimal(50000), as_of=NOW)
    database = Database.create(tmp_path / "custody.db")
    store = CoreStore(database, config)
    proof = initialize_fixture(store, broker, now=NOW, entry_enabled=True)
    decision = await trader_cycle(
        store, broker, observation(capability, NOW, 1), ModelArtifact(), proof, now=NOW
    )
    assert decision.action == "BUY"
    broker.now += timedelta(seconds=1)
    import_broker_events(store, events(store, broker))
    document = dict(
        account_id=config.account_id,
        release_id=config.release_id,
        endpoint="synthetic/activities",
        until=broker.now.isoformat(),
        rows=[
            dict(activity_type="FILL", id=f.fill_id, order_id=f.broker_order_id, symbol=f.symbol)
            for f in broker.fills
        ],
    )
    source_id = content_id(document)
    with database.transaction() as session:
        put(session, "core_provider_acquisitions", source_id, document)
        session.execute(
            table("core_provider_progress")
            .insert()
            .values(
                account_id=config.account_id,
                watermark=broker.now.isoformat(),
                document_id=source_id,
            )
        )
    return database, store, broker, decision, source_id


@pytest.mark.asyncio
async def test_durable_basis_replays_after_restart_without_losing_owned_fills(tmp_path):
    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.store import CoreStore

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    try:
        with database.transaction() as session:
            first = crypto_custody.read_crypto_lot_basis(
                store,
                session,
                decision.id,
                now=broker.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        reopened = CoreStore(database, store.config)
        with database.transaction() as session:
            second = crypto_custody.read_crypto_lot_basis(
                reopened,
                session,
                decision.id,
                now=broker.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        assert first.id == second.id and first.gross_remaining == Decimal(".01")
        assert first.reserved_exits == 0 and first.source_refs
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "source", "allocation_index"])
async def test_delayed_allocated_fee_changes_only_net_owned_bound_and_replays_after_restart(
    tmp_path, monkeypatch, missing
):
    from sqlalchemy import select
    from tests.contract.test_alpaca_paper_broker import response
    from tests.unit.paper_core.test_crypto_costs import fee_row
    from tests.unit.paper_core.test_crypto_fee_allocation import fixture

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.crypto_fee_allocation import (
        ALLOCATION_INDEX,
        acquire_order_fee_sources,
        retain_order_fee_allocations,
    )
    from quantbot.paper_core.store import CoreStore, table

    row = fee_row()
    database, store, simulated, decision, broker = await fixture(
        tmp_path, monkeypatch, [response(200, [row])]
    )
    try:
        with database.transaction() as session:
            source = session.execute(
                select(table("core_provider_progress").c.document_id)
            ).scalar_one()
            original = crypto_custody.read_crypto_lot_basis(
                store,
                session,
                decision.id,
                now=simulated.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        captures = await acquire_order_fee_sources(broker, store, simulated.now, rows=(row,))
        with database.transaction() as session:
            retain_order_fee_allocations(store, session, captures, recorded_at=simulated.now)
            first = crypto_custody.read_crypto_lot_basis(
                store,
                session,
                decision.id,
                now=simulated.now,
                capture_started=NOW,
                activity_document_id=source,
            )
            retain_order_fee_allocations(store, session, captures, recorded_at=simulated.now)
        reopened = CoreStore(database, store.config)
        with database.transaction() as session:
            replayed = crypto_custody.read_crypto_lot_basis(
                reopened,
                session,
                decision.id,
                now=simulated.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        assert first.id == replayed.id and first.id != original.id
        assert first.gross_remaining == original.gross_remaining == Decimal(".01")
        assert first.proven_asset_fee_quantity == Decimal(".000025")
        assert first.owned_remaining == first.expected_account_quantity == Decimal(".009975")
        assert first.fee_allocation_ids and first.fee_external_event_ids and first.fee_source_ids
        if missing is not None:
            with database.transaction() as session:
                target = (
                    table("core_document_index")
                    if missing == "allocation_index"
                    else table("core_provider_acquisitions")
                )
                condition = (
                    (target.c.kind == ALLOCATION_INDEX)
                    & (target.c.id == first.fee_allocation_ids[0])
                    if missing == "allocation_index"
                    else target.c.id == captures[0].id
                )
                session.execute(target.delete().where(condition))
                with pytest.raises(
                    StateConflictError,
                    match="CRYPTO_(FEE_ALLOCATION_UNVERIFIED|CUSTODY_FEE_ALLOCATION_MISSING)",
                ):
                    crypto_custody.read_crypto_lot_basis(
                        reopened,
                        session,
                        decision.id,
                        now=simulated.now,
                        capture_started=NOW,
                        activity_document_id=source,
                    )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_inherited_crypto_baseline_cannot_be_claimed_by_current_lot(tmp_path):
    import json

    from sqlalchemy import select

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.store import table
    from quantbot.research.manifest import canonical_result_json, content_id

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    try:
        with database.transaction() as session:
            target = table("core_fixture_baselines")
            baseline = json.loads(session.execute(select(target.c.payload)).scalar_one())
            baseline["quantities"].append(["BTC/USD", ".001"])
            session.execute(
                target.update().values(
                    id=content_id(baseline), payload=canonical_result_json(baseline)
                )
            )
            with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_BASELINE_NOT_ZERO"):
                crypto_custody.read_crypto_lot_basis(
                    store,
                    session,
                    decision.id,
                    now=broker.now,
                    capture_started=NOW,
                    activity_document_id=source,
                )
    finally:
        database.close()


@pytest.mark.asyncio
async def test_timestamped_account_fee_does_not_become_a_lot_fee(tmp_path):
    from tests.unit.paper_core.test_crypto_costs import fee_row

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.crypto_costs import parse_fee_activity, retain_fee_activity
    from quantbot.paper_core.store import put
    from quantbot.research.manifest import content_id

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    raw = fee_row(transaction_time=broker.now.isoformat())
    raw_id = content_id(raw)
    try:
        with database.transaction() as session:
            put(session, "core_provider_acquisitions", raw_id, raw)
            fee = parse_fee_activity(
                raw, symbol_aliases={"BTCUSD": "BTC/USD"}, source_document_id=raw_id
            )
            retain_fee_activity(
                session, fee, account_id=store.config.account_id, received_at=broker.now
            )
            event = fee.external_quantity_event(account_id=store.config.account_id, baseline_at=NOW)
            assert event is not None
            AlpacaCoreBroker.record_external(session, event)
            basis = crypto_custody.read_crypto_lot_basis(
                store,
                session,
                decision.id,
                now=broker.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        assert basis.expected_account_quantity == Decimal(".009975")
        assert basis.proven_asset_fee_quantity == 0 and basis.fee_allocation_ids == ()
        assert basis.owned_remaining == basis.gross_remaining == Decimal(".01")
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [False, True])
async def test_unsettled_exit_keeps_full_remaining_reservation(tmp_path, acknowledged):
    from quantbot.domain import IntentState
    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.store import table
    from quantbot.research.manifest import canonical_result_json
    from quantbot.storage import StorageRepository

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    try:
        with database.transaction() as session:
            link = store.links()[0]
            repo = StorageRepository(session)
            original = repo.get_order_intent(link["intent_id"])
            unknown = original.model_copy(
                update=dict(
                    intent_id="unknown-exit",
                    client_order_id="unknown-exit",
                    side=OrderSide.SELL,
                    quantity=Decimal(".005"),
                    state=IntentState.SUBMISSION_UNKNOWN,
                )
            )
            repo.create_order_intent(unknown)
            if acknowledged:
                repo.save_broker_order(
                    BrokerOrder(
                        broker_order_id="exit-ack",
                        client_order_id=unknown.client_order_id,
                        symbol=unknown.symbol,
                        side=OrderSide.SELL,
                        order_type=OrderType.MARKET,
                        time_in_force=TimeInForce.GTC,
                        quantity=unknown.quantity,
                        filled_quantity=0,
                        status="accepted",
                        submitted_at=NOW,
                    )
                )
            session.execute(
                table("core_intent_links")
                .insert()
                .values(
                    **(
                        link
                        | {
                            "intent_id": unknown.intent_id,
                            "role": "EXIT",
                            "payload": canonical_result_json(unknown),
                        }
                    )
                )
            )
            basis = crypto_custody.read_crypto_lot_basis(
                store,
                session,
                decision.id,
                now=broker.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        assert basis.gross_remaining == Decimal(".01") and basis.reserved_exits == Decimal(".005")
        assert len(basis.open_exit_orders) == int(acknowledged)
    finally:
        database.close()


@pytest.mark.asyncio
async def test_live_partial_entry_refuses_until_cancellation_is_canonically_settled(tmp_path):
    from tests.fixtures.core_m2 import events

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.outcomes import import_broker_events

    database, store, broker, decision, source = await owned_fixture(tmp_path, partial=True)
    try:
        with database.transaction() as session:
            with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_ENTRY_NOT_SETTLED"):
                crypto_custody.read_crypto_lot_basis(
                    store,
                    session,
                    decision.id,
                    now=broker.now,
                    capture_started=NOW,
                    activity_document_id=source,
                )
        key, order = next(iter(broker.orders.items()))
        broker.orders[key] = order.model_copy(update={"status": "canceled"})
        import_broker_events(store, events(store, broker))
        with database.transaction() as session:
            basis = crypto_custody.read_crypto_lot_basis(
                store,
                session,
                decision.id,
                now=broker.now,
                capture_started=NOW,
                activity_document_id=source,
            )
        assert basis.gross_remaining == Decimal(".004")
    finally:
        database.close()


@pytest.mark.asyncio
async def test_foreign_symbol_intent_prevents_exclusive_custody_claim(tmp_path):
    from quantbot.paper_core import crypto_custody
    from quantbot.storage import StorageRepository

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    try:
        with database.transaction() as session:
            repo = StorageRepository(session)
            original = repo.get_order_intent(store.links()[0]["intent_id"])
            repo.create_order_intent(
                original.model_copy(
                    update=dict(
                        intent_id="foreign", client_order_id="foreign", strategy_id="manual-owner"
                    )
                )
            )
            with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_FOREIGN_OWNERSHIP"):
                crypto_custody.read_crypto_lot_basis(
                    store,
                    session,
                    decision.id,
                    now=broker.now,
                    capture_started=NOW,
                    activity_document_id=source,
                )
    finally:
        database.close()


@pytest.mark.parametrize(
    "fault", [None, "missing_receipt", "stale", "funds_binding", "ledger_changed"]
)
def test_scoped_proof_requires_persisted_matching_custody_and_funds(tmp_path, monkeypatch, fault):
    from types import SimpleNamespace

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.contracts import CoreConfig, ReconciliationProof
    from quantbot.paper_core.funds import PaperFundsEvidence
    from quantbot.paper_core.store import CoreStore, put
    from quantbot.storage import Database

    database = Database.create(tmp_path / "scope.db")
    store = CoreStore(
        database, CoreConfig(account_id="fixture", symbol="BTC/USD", evidence_lane="BROKER_PAPER")
    )
    try:
        basis = fixture_basis(
            store_identity=database.store_identity,
            config_id=store.config_id,
            release_id=store.config.release_id,
            owner=store.config.owner,
            evidence_lane="BROKER_PAPER",
        )
        funds = PaperFundsEvidence(
            recorded_at_utc=NOW,
            evidence_lane="BROKER_PAPER",
            release_id=store.config.release_id,
            endpoint="https://paper-api.alpaca.markets",
            account_id="fixture",
            store_identity=database.store_identity,
            config_id=store.config_id,
            commission_id="commission",
            process_id="fixture",
            capture_started=NOW - timedelta(seconds=1),
            elapsed_seconds=1,
            first_account=account(),
            second_account=account(),
            first_positions=positions(),
            second_positions=positions(),
            first_open_orders=(),
            second_open_orders=(),
        )
        receipt = build(basis, funds_evidence_id="wrong" if fault == "funds_binding" else funds.id)
        proof = ReconciliationProof(
            recorded_at_utc=NOW,
            evidence_lane="BROKER_PAPER",
            release_id=store.config.release_id,
            account=account().account,
            baseline_id="baseline",
            generation=1,
            fill_ids=("entry-fill",),
            expected_quantities=(("BTC/USD", Decimal(".01")),),
            actual_quantities=(("BTC/USD", Decimal(".009975")),),
            positions=positions(),
            expected_cash=None,
            cash_complete=False,
            complete=False,
            acquisition_id=funds.id,
            ledger_state_id="ledger",
            funds_authority="BROKER_PAPER_BALANCE_V1",
            quarantined_symbols=("BTC/USD",),
            custody_evidence_ids=(receipt.id,),
        )
        monkeypatch.setattr(
            crypto_custody,
            "read_crypto_lot_basis",
            lambda *args, **kwargs: (
                basis.model_copy(update={"ledger_state_id": "changed"})
                if fault == "ledger_changed"
                else basis
            ),
        )
        monkeypatch.setattr(
            "quantbot.paper_core.authority.commission_in_session",
            lambda *_: SimpleNamespace(id="commission"),
        )
        with database.transaction() as session:
            put(session, "core_provider_acquisitions", funds.id, funds)
            if fault != "missing_receipt":
                put(session, "core_provider_acquisitions", receipt.id, receipt)
            put(session, "core_reconciliation_proofs", proof.id, proof)
            reasons = crypto_custody.scoped_quantity_objections(
                store, session, proof, now=NOW + timedelta(seconds=121 if fault == "stale" else 20)
            )
        if fault is None:
            assert reasons == (), "20-second scoped entry proof is valid without granting an exit"
        else:
            assert reasons and all("CRYPTO_CUSTODY" in r for r in reasons)
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("crossed_send", [False, True])
async def test_fresh_exit_capture_persists_separate_authority_and_rejects_crossed_send(
    tmp_path, monkeypatch, crossed_send
):
    from types import SimpleNamespace

    from tests.contract.test_alpaca_paper_broker import (
        ScriptedBrokerTransport,
        account_payload,
        adapter,
        response,
    )

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.store import CoreStore
    from quantbot.storage import Database

    database = Database.create(tmp_path / "fresh-custody.db")
    store = CoreStore(
        database, CoreConfig(account_id="fixture", symbol="BTC/USD", evidence_lane="BROKER_PAPER")
    )
    clocks = SimpleNamespace(sample=lambda: (NOW, 1.0), process_id="fixture")
    store.clocks = clocks
    balance = account_payload(id="fixture", cash="100", equity="600", buying_power="100")
    custody = [
        dict(
            symbol="BTC/USD",
            qty=".009975",
            side="long",
            current_price="50000",
            market_value="498.75",
            avg_entry_price="50000",
        )
    ]
    transport = ScriptedBrokerTransport(
        [
            response(200, balance),
            response(200, custody),
            response(200, []),
            response(200, balance),
            response(200, custody),
            response(200, []),
        ]
    )
    broker = AlpacaCoreBroker(
        adapter(transport),
        None,
        None,
        SimpleNamespace(cost_contract=None),
        clocks,
        account_id="fixture",
    )
    basis = fixture_basis(
        store_identity=database.store_identity,
        config_id=store.config_id,
        release_id=store.config.release_id,
        owner=store.config.owner,
        evidence_lane="BROKER_PAPER",
        ledger_generation=0,
    )

    async def recovered(*_):
        return SimpleNamespace(source_refs=("activities",))

    monkeypatch.setattr("quantbot.paper_core.outcomes.recover_intents", recovered)
    monkeypatch.setattr(crypto_custody, "read_crypto_lot_basis", lambda *a, **kw: basis)
    monkeypatch.setattr(
        "quantbot.paper_core.authority.commission_in_session",
        lambda *_: SimpleNamespace(id="commission"),
    )
    monkeypatch.setattr(
        "quantbot.paper_core.guardian._capture_crossing_sends",
        lambda *a, **kw: ("crossed",) if crossed_send else (),
    )
    try:
        if crossed_send:
            with pytest.raises(StateConflictError, match="CRYPTO_CUSTODY_CAPTURE_CROSSED_SEND"):
                await crypto_custody.capture_crypto_custody(store, broker, "lot")
            assert not store.documents("core_provider_acquisitions")
        else:
            evidence = await crypto_custody.capture_crypto_custody(store, broker, "lot")
            assert evidence.bounded_quantity == Decimal(".009975")
            assert {d.get("contract") for d in store.documents("core_provider_acquisitions")} == {
                "BROKER_PAPER_BALANCE_V1",
                "CRYPTO_PROTECTIVE_CUSTODY_V1",
            }
        assert not store.documents("core_reconciliation_proofs")
        assert all(call["method"] == "GET" for call in transport.calls)
    finally:
        database.close()
