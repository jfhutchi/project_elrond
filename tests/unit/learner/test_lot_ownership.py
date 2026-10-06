"""Synthetic operational ownership; no credentials, provider access or orders."""

from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.brokers import BrokerAccountSnapshot
from quantbot.domain import Account, Fill, OrderSide, Position
from quantbot.execution.reconciliation import ReconciliationSnapshot
from quantbot.execution.snapshot_capture import Attempt, Capture
from quantbot.storage import StorageRepository
from tests.unit.learner.test_paper import NOW
from tests.unit.learner.test_trade_learning import episode
from tests.unit.learner.test_trade_learning import store as _store


@pytest.fixture
def store(tmp_path):
    yield from _store.__wrapped__(tmp_path)


def module():
    return import_module("quantbot.storage.lot_ownership")


def capture(*, held=False):
    account = BrokerAccountSnapshot(
        account=Account(
            account_id="paper", cash=1000, buying_power=1000, equity=1000, currency="USD"
        ),
        account_number="synthetic",
        status="ACTIVE",
        order_entry_allowed=True,
        restrictions=(),
    )
    positions = (
        ()
        if not held
        else (
            Position(
                symbol="DIA",
                quantity=1,
                market_price=100,
                market_value=100,
                average_entry_price=100,
            ),
        )
    )
    attempt = Attempt(NOW, NOW, account, account, positions, (NOW, NOW, NOW))
    return Capture(
        "ACQUIRED",
        ReconciliationSnapshot(account=account.account, positions=positions),
        NOW,
        (attempt,),
    )


def register(session, record):
    return module().register_new_lot(
        session,
        record,
        capture(),
        run_id="run",
        experiment_id="synthetic-experiment",
        decision_id="b" * 64,
        now=NOW,
        clock_healthy=True,
        synthetic=True,
    )


def test_registration_precedes_entry_and_preserves_handoff_hash(store):
    database, _, _ = store
    record = episode(store, before_orders=register)
    with database.transaction() as session:
        registrations = module().registrations(session, symbol="DIA", now=NOW)
    assert len(registrations) == 1
    assert registrations[0]["handoff_id"] == record.handoff_id
    assert registrations[0]["position_before_entry"] == "0"


def test_registration_after_submission_refused(store):
    database, _, _ = store
    record = episode(store)
    with (
        database.transaction() as session,
        pytest.raises(ValueError, match="ENTRY_ALREADY_DURABLE"),
    ):
        register(session, record)


def test_registration_cannot_adopt_legacy_position(store):
    def invalid(session, record):
        with pytest.raises(ValueError, match="EXISTING_POSITION"):
            module().register_new_lot(
                session,
                record,
                capture(held=True),
                run_id="run",
                experiment_id="synthetic-experiment",
                decision_id="b" * 64,
                now=NOW,
                clock_healthy=True,
                synthetic=True,
            )

    episode(store, before_orders=invalid)


def test_foreign_or_flat_position_not_attributed(store):
    database, _, _ = store
    record = episode(store, before_orders=register)
    with database.transaction() as session:
        intent = StorageRepository(session).get_order_intent("exit")
        assert (
            module().link_protection_exit(
                session,
                intent,
                account_id="other",
                position_quantity=Decimal(1),
                reconciled=True,
                now=record.deadline_at,
            )
            is None
        )
        assert (
            module().link_protection_exit(
                session,
                intent,
                account_id="paper",
                position_quantity=Decimal(0),
                reconciled=True,
                now=record.deadline_at,
            )
            is None
        )


def test_unknown_registration_does_not_block_normal_protection(store):
    database, _, _ = store
    record = episode(store)
    with database.transaction() as session:
        intent = StorageRepository(session).get_order_intent("exit")
        assert (
            module().link_protection_exit(
                session,
                intent,
                account_id="paper",
                position_quantity=Decimal(1),
                reconciled=True,
                now=record.deadline_at,
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_registration", [False, True])
async def test_actual_protection_action_is_owned_and_economically_attributed(
    store,
    corrupt_registration,
):
    from sqlalchemy import select, update

    from quantbot.learner.paper_deadlines import CLOSED_EVENT
    from quantbot.learner.trade_learning import read_trade_episodes
    from quantbot.operations.metrics import OperationalMetrics
    from quantbot.operations.protection import PositionProtectionSupervisor
    from quantbot.risk.protection import ProtectionPolicy
    from quantbot.storage.schema import order_events, position_protection_actions, signals
    from tests.integration.test_execution_coordinator import _accepted
    from tests.unit.operations.test_position_protection_supervisor import IDENTITY, FakeBroker

    database, _, _ = store
    record = episode(store, before_orders=register, entry_only=True)
    held = (
        Position(
            symbol="DIA", quantity=1, market_price=90, market_value=90, average_entry_price=100
        ),
    )
    initial = capture().attempts[0].before
    snapshot = initial.model_copy(
        update={"account": initial.account.model_copy(update={"cash": Decimal(910)})}
    )
    with database.transaction() as session:
        repo = StorageRepository(session)
        repo.save_strategy_deployment(IDENTITY)
        repo.save_account_snapshot("owned-local", snapshot.account, held, captured_at=NOW)
        if corrupt_registration:
            session.execute(
                update(signals)
                .where(signals.c.signal_id.like("adaptive-lot-%"))
                .values(payload_json='{"kind":"ADAPTIVE_PAPER_NEW_LOT_V1","document":null}')
            )
    protection = PositionProtectionSupervisor(
        database=database,
        broker=FakeBroker(snapshot, held),
        expected_account_id="paper",
        strategy_identity=IDENTITY,
        metrics=OperationalMetrics(),
        policy=ProtectionPolicy(hard_stop_bps=Decimal(100)),
    )
    await protection.evaluate(now=NOW + timedelta(minutes=1))
    with database.transaction() as session:
        repo = StorageRepository(session)
        action = session.execute(select(position_protection_actions)).mappings().one()
        link = (
            session.execute(
                select(order_events).where(order_events.c.event_type == module().PROTECTIVE_EXIT)
            )
            .mappings()
            .one_or_none()
        )
        if corrupt_registration:
            assert link is None
        else:
            assert link["intent_id"] == action["intent_id"]
        protective = repo.get_order_intent(action["intent_id"])
        assert protective.strategy_id != record.identity.strategy_id
        order = _accepted(protective).model_copy(
            update={
                "status": "filled",
                "filled_quantity": Decimal(1),
                "filled_average_price": Decimal(90),
            }
        )
        repo.save_broker_order(order)
        repo.record_fill(
            Fill(
                fill_id="protective-fill",
                broker_order_id=order.broker_order_id,
                symbol="DIA",
                side=OrderSide.SELL,
                quantity=1,
                price=90,
                fee=Decimal(".20"),
                occurred_at=NOW + timedelta(minutes=2),
            )
        )
        repo.record_order_event(
            "owned-close",
            event_type=CLOSED_EVENT,
            occurred_at=NOW + timedelta(minutes=3),
            intent_id=record.entry_intent.intent_id,
            detail={"handoff_id": record.handoff_id},
        )
    (result,) = read_trade_episodes(database.path, now=NOW + timedelta(hours=1))
    if corrupt_registration:
        assert result.status == "INCOMPLETE_FILL_ATTRIBUTION"
        assert result.realized_net_pnl is None
    else:
        assert result.status == "CLOSED"
        assert result.realized_net_pnl == Decimal("-10.30")
        assert result.exit_fill_ids == ("protective-fill",)
