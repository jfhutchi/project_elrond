"""Protective custody is consumed only before a new crypto POST."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from tests.unit.paper_core.test_crypto_custody import NOW, build, fixture_basis

from quantbot.domain import IntentState, OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.paper_core import send_guard
from quantbot.risk import OrderPurpose


def exit_intent(symbol="BTC/USD"):
    return OrderIntent(
        intent_id="exit",
        client_order_id="exit",
        strategy_id="owner",
        signal_date=NOW.date(),
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=Decimal(".009975"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        created_at=NOW,
        state=IntentState.ORDER_CREATED,
    )


def guard_store(monkeypatch):
    monkeypatch.setattr("quantbot.paper_core.authority.runtime_objections", lambda *a, **kw: ())
    monkeypatch.setattr(send_guard, "owned_exit_objections", lambda *a: ())
    return SimpleNamespace(
        config=SimpleNamespace(evidence_lane="BROKER_PAPER"),
        runtime_submission_enabled=True,
    )


def test_new_crypto_send_requires_custody_but_stock_send_does_not(monkeypatch):
    store = guard_store(monkeypatch)
    assert send_guard.final_send_in_session(
        store,
        None,
        exit_intent(),
        OrderPurpose.STRATEGY_EXIT,
        clock=lambda: NOW,
    ) == ("CRYPTO_CUSTODY_RECEIPT_MISSING",)
    assert (
        send_guard.final_send_in_session(
            store,
            None,
            exit_intent("SPY"),
            OrderPurpose.STRATEGY_EXIT,
            clock=lambda: NOW,
        )
        == ()
    )


@pytest.mark.parametrize(
    "age,change,expected",
    [
        (0, None, ()),
        (6, None, ("CRYPTO_CUSTODY_STALE",)),
        (0, "ledger", ("CRYPTO_CUSTODY_BASIS_CHANGED",)),
        (0, "reservation", ("CRYPTO_CUSTODY_BASIS_CHANGED",)),
        (0, "quantity", ("CRYPTO_CUSTODY_QUANTITY_LIMIT",)),
        (0, "receipt", ("CRYPTO_CUSTODY_RECEIPT_NOT_RETAINED",)),
    ],
)
def test_final_crypto_send_consumes_fresh_persisted_exact_basis(
    tmp_path, monkeypatch, age, change, expected
):
    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.funds import PaperFundsEvidence
    from quantbot.paper_core.store import CoreStore, put, table
    from quantbot.storage import Database, StorageRepository

    guard_store(monkeypatch)
    database = Database.create(tmp_path / "crypto-send.db")
    store = CoreStore(
        database, CoreConfig(account_id="fixture", symbol="BTC/USD", evidence_lane="BROKER_PAPER")
    )
    store.runtime_submission_enabled = True
    basis = fixture_basis(
        account_id=store.config.account_id,
        store_identity=database.store_identity,
        config_id=store.config_id,
        release_id=store.config.release_id,
        owner=store.config.owner,
        evidence_lane="BROKER_PAPER",
    )
    original = build(basis)
    funds = PaperFundsEvidence(
        recorded_at_utc=NOW,
        evidence_lane="BROKER_PAPER",
        release_id=store.config.release_id,
        endpoint="https://paper-api.alpaca.markets",
        account_id=store.config.account_id,
        store_identity=database.store_identity,
        config_id=store.config_id,
        commission_id="commission",
        process_id="fixture",
        capture_started=original.capture_started,
        elapsed_seconds=original.elapsed_seconds,
        **{
            name: getattr(original, name)
            for name in (
                "first_account",
                "second_account",
                "first_positions",
                "second_positions",
                "first_open_orders",
                "second_open_orders",
            )
        },
    )
    evidence = original.model_copy(update={"funds_evidence_id": funds.id})
    current_basis = basis.model_copy(
        update={
            "ledger_state_id": "new-ledger" if change == "ledger" else basis.ledger_state_id,
            "reservation_state_id": "new-reservation"
            if change == "reservation"
            else basis.reservation_state_id,
        }
    )
    monkeypatch.setattr(crypto_custody, "read_crypto_lot_basis", lambda *a, **kw: current_basis)
    monkeypatch.setattr(
        "quantbot.paper_core.authority.commission_in_session",
        lambda *a: SimpleNamespace(id="commission"),
    )
    try:
        with database.transaction() as session:
            StorageRepository(session).create_order_intent(exit_intent())
            for kind, identity in (
                ("core_decisions", "decision"),
                ("core_protection_plans", "plan"),
                ("core_observations", "observation"),
            ):
                put(session, kind, identity, {})
            put(session, "core_provider_acquisitions", funds.id, funds)
            if change != "receipt":
                put(session, "core_provider_acquisitions", evidence.id, evidence)
            session.execute(
                table("core_intent_links")
                .insert()
                .values(
                    intent_id="exit",
                    decision_id="decision",
                    protection_id="plan",
                    observation_id="observation",
                    role="EXIT",
                    lot_id="lot",
                    payload="{}",
                )
            )
        with database.transaction() as session:
            reasons = send_guard.final_send_in_session(
                store,
                session,
                exit_intent().model_copy(update={"quantity": Decimal(".01")})
                if change == "quantity"
                else exit_intent(),
                OrderPurpose.STRATEGY_EXIT,
                clock=lambda: NOW + timedelta(seconds=age),
                crypto_custody=lambda: evidence,
            )
        assert reasons == expected
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [".009975", ".000000001", "0"])
async def test_guardian_caps_crypto_outside_transaction_and_keeps_dust_open(
    tmp_path, monkeypatch, quantity
):
    from contextlib import contextmanager

    from tests.unit.paper_core.test_crypto_custody import owned_fixture, positions

    from quantbot.paper_core import crypto_custody, guardian

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    broker.now += timedelta(minutes=31)
    store.config = store.config.model_copy(update={"evidence_lane": "BROKER_PAPER"})
    store.clocks = SimpleNamespace(sample=lambda: (broker.now, 1.0))
    basis = fixture_basis(activity_document_id=source, activity_watermark=broker.now)
    evidence = build(
        basis,
        capture_started=broker.now - timedelta(seconds=1),
        captured_at=broker.now,
        first_positions=positions(quantity),
        second_positions=positions(quantity),
    )
    state = {"transactions": 0, "captures": 0, "submitted": []}
    original_transaction = database.transaction

    @contextmanager
    def tracked_transaction():
        state["transactions"] += 1
        try:
            with original_transaction() as session:
                yield session
        finally:
            state["transactions"] -= 1

    async def capture(*args, **kwargs):
        assert state["transactions"] == 0
        state["captures"] += 1
        return evidence

    def locked_basis(*args, **kwargs):
        assert state["transactions"] > 0
        return basis

    async def synchronize(*args, **kwargs):
        return None

    async def submit(intent, **kwargs):
        state["submitted"].append(intent)

    monkeypatch.setattr(database, "transaction", tracked_transaction)
    monkeypatch.setattr(guardian, "require_runtime_broker", lambda *a: None)
    monkeypatch.setattr(guardian, "acknowledge_handoffs", lambda *a, **kw: None)
    monkeypatch.setattr(guardian, "synchronize", synchronize)
    monkeypatch.setattr(crypto_custody, "capture_crypto_custody", capture)
    monkeypatch.setattr(crypto_custody, "read_crypto_lot_basis", locked_basis)
    monkeypatch.setattr(
        "quantbot.paper_core.runtime.coordinator", lambda *a: SimpleNamespace(submit_durable=submit)
    )
    monkeypatch.setattr("quantbot.paper_core.outcomes.process_outcomes", lambda *a, **kw: None)
    try:
        receipt = await guardian.guardian_cycle(store, broker, now=broker.now)
        assert state["captures"] == 1
        if quantity == ".009975":
            assert len(state["submitted"]) == 1
            assert state["submitted"][0].quantity == Decimal(quantity)
            assert state["submitted"][0].time_in_force is TimeInForce.GTC
        else:
            assert state["submitted"] == []
            assert evidence.exit_reason in receipt.reasons
        assert store.documents("core_round_trips") == []
    finally:
        database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [IntentState.SUBMISSION_UNKNOWN, IntentState.BROKER_ACCEPTED])
async def test_existing_order_recovery_never_demands_new_custody(monkeypatch, state):
    from quantbot.paper_core.runtime import CoreExecutionCoordinator

    intent = exit_intent().model_copy(update={"state": state})
    coordinator = object.__new__(CoreExecutionCoordinator)
    coordinator.store = SimpleNamespace()
    monkeypatch.setattr(coordinator, "_stored_intent", lambda *a: intent)
    from quantbot.domain import BrokerOrder

    order = BrokerOrder(
        broker_order_id="accepted",
        client_order_id=intent.client_order_id,
        symbol=intent.symbol,
        side=intent.side,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        quantity=intent.quantity,
        filled_quantity=0,
        status="accepted",
        submitted_at=NOW,
    )
    if state is IntentState.SUBMISSION_UNKNOWN:

        async def recover(*args):
            return "recovered"

        monkeypatch.setattr(coordinator, "_recover_persisted_submission", recover)
    else:
        monkeypatch.setattr(coordinator, "_stored_order", lambda *a: order)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Accepted/unknown recovery must not acquire or POST")

    monkeypatch.setattr(coordinator, "_acquire_send_constraints", forbidden)
    result = await coordinator.submit_durable(
        intent,
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )
    assert (
        result == "recovered"
        if state is IntentState.SUBMISSION_UNKNOWN
        else result.broker_order is order
    )


@pytest.mark.asyncio
async def test_coordinator_recaptures_after_durable_exit_and_excludes_only_that_intent(
    tmp_path, monkeypatch
):
    from tests.unit.paper_core.test_crypto_custody import owned_fixture

    from quantbot.paper_core import crypto_custody
    from quantbot.paper_core.runtime import CoreExecutionCoordinator
    from quantbot.paper_core.store import table
    from quantbot.research.manifest import canonical_result_json
    from quantbot.storage import StorageRepository

    database, store, broker, decision, source = await owned_fixture(tmp_path)
    link = store.links()[0]
    with database.transaction() as session:
        original = StorageRepository(session).get_order_intent(link["intent_id"])
        intent = original.model_copy(
            update={
                "intent_id": "new-exit",
                "client_order_id": "new-exit",
                "side": OrderSide.SELL,
                "quantity": Decimal(".009975"),
                "state": IntentState.ORDER_CREATED,
            }
        )
        StorageRepository(session).create_order_intent(intent)
        session.execute(
            table("core_intent_links")
            .insert()
            .values(
                **(
                    link
                    | {
                        "intent_id": intent.intent_id,
                        "role": "EXIT",
                        "payload": canonical_result_json(intent),
                    }
                )
            )
        )
    store.config = store.config.model_copy(update={"evidence_lane": "BROKER_PAPER"})
    coordinator = object.__new__(CoreExecutionCoordinator)
    coordinator.store, coordinator._broker = store, broker
    evidence = build()
    captures = []

    async def capture(captured_store, captured_broker, lot_id, *, exclude_intent=None):
        with database.transaction() as session:
            assert StorageRepository(session).get_order_intent(exclude_intent) == intent
        captures.append((captured_store, captured_broker, lot_id, exclude_intent))
        return evidence

    monkeypatch.setattr(crypto_custody, "capture_crypto_custody", capture)
    try:
        await coordinator._acquire_send_constraints(intent, OrderPurpose.STRATEGY_EXIT)
        assert captures == [(store, broker, decision.id, intent.intent_id)]
        assert coordinator._exit_custody is evidence
    finally:
        database.close()
