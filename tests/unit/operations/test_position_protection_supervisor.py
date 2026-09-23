"""Independent after-entry protection runtime (#157).

These tests define the independently schedulable authority that covers every broker-open paper
position with durable, attributable state. An EXIT_REQUIRED decision is persisted before the
same-identity exit is routed through the authoritative execution coordinator.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select

from quantbot.brokers import BrokerAccountSnapshot, BrokerAdapter, BrokerRequestError
from quantbot.config import Settings
from quantbot.domain import (
    Account,
    BrokerOrder,
    IntentState,
    MarketClock,
    OrderIntent,
    OrderSide,
    Position,
    StrategyIdentity,
)
from quantbot.execution import ExecutionCoordinator
from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.protection import (
    FILLED_POSITION_CONFIRMATION_TIMEOUT,
    PositionProtectionDaemonRunner,
    PositionProtectionSupervisor,
    ProtectionCycleOutcome,
)
from quantbot.risk.protection import ProtectionOutcome
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import (
    position_protection_action_evaluations,
    position_protection_actions,
    position_protection_cycles,
    position_protection_evaluations,
)

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
ACCOUNT_ID = "paper-account-1"


def position(
    symbol: str,
    *,
    quantity: str = "1",
    price: str = "100",
    entry: str = "100",
) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal(quantity),
        market_price=Decimal(price),
        market_value=Decimal(quantity) * Decimal(price),
        average_entry_price=Decimal(entry),
    )


def account(equity: str = "100000", *, account_id: str = ACCOUNT_ID) -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        account=Account(
            account_id=account_id,
            cash=Decimal(equity),
            buying_power=Decimal(equity),
            equity=Decimal(equity),
            currency="USD",
        ),
        account_number="PA-1",
        status="ACTIVE",
        order_entry_allowed=True,
        restrictions=(),
    )


IDENTITY = StrategyIdentity(
    strategy_id="adaptive-momentum",
    version="1.0.0",
    git_commit="abc123",
    configuration_hash="cfg-123",
    deployment_timestamp=NOW - timedelta(days=30),
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    with Database.create(tmp_path / "protection.db") as store:
        yield store


@dataclass
class FakeBroker:
    snapshot: BrokerAccountSnapshot
    positions: tuple[Position, ...]
    account_error: BrokerRequestError | None = None
    position_error: BrokerRequestError | None = None

    async def get_account(self) -> BrokerAccountSnapshot:
        if self.account_error is not None:
            raise self.account_error
        return self.snapshot

    async def get_positions(self) -> tuple[Position, ...]:
        if self.position_error is not None:
            raise self.position_error
        return self.positions


def seed_local(
    database: Database,
    positions: tuple[Position, ...],
    *,
    equity: str = "100000",
) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_strategy_deployment(IDENTITY)
        repository.save_account_snapshot(
            "local-snapshot",
            account(equity).account,
            positions,
            captured_at=NOW - timedelta(minutes=5),
        )


def supervisor(database: Database, broker: FakeBroker) -> PositionProtectionSupervisor:
    return PositionProtectionSupervisor(
        database=database,
        broker=broker,
        expected_account_id=ACCOUNT_ID,
        strategy_identity=IDENTITY,
        metrics=OperationalMetrics(),
    )


def execution_settings() -> Settings:
    return Settings(
        EXPECTED_ACCOUNT_ID=ACCOUNT_ID,
        ALPACA_PAPER_API_KEY="paper-key",
        ALPACA_PAPER_API_SECRET="paper-secret",
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=True,
    )


class ActionBroker:
    def __init__(
        self,
        database: Database,
        positions: tuple[Position, ...],
        *,
        order_status: str = "accepted",
        filled_quantity: str = "0",
    ) -> None:
        self.database = database
        self.snapshot = account()
        self.positions = positions
        self.order_status = order_status
        self.filled_quantity = Decimal(filled_quantity)
        self.submitted: list[OrderIntent] = []
        self.lookups: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        return self.snapshot

    async def get_positions(self) -> tuple[Position, ...]:
        return self.positions

    async def get_open_orders(self) -> tuple[BrokerOrder, ...]:
        if self.order_status in {"filled", "rejected", "canceled", "cancelled", "expired"}:
            return ()
        return tuple(self._order(intent) for intent in self.submitted)

    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        with self.database.transaction() as session:
            stored = StorageRepository(session).get_order_intent(intent.intent_id)
            actions = list(session.execute(select(position_protection_actions)).mappings())
        assert stored is not None
        assert stored.state is IntentState.SUBMITTING
        assert len(actions) == 1, "the protection action must exist before broker mutation"
        self.submitted.append(intent)
        return self._order(intent)

    def _order(self, intent: OrderIntent) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=f"broker-{intent.client_order_id}",
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            quantity=intent.quantity,
            filled_quantity=self.filled_quantity,
            status=self.order_status,
            submitted_at=NOW,
        )

    async def get_order_by_client_id(
        self,
        client_order_id: str,
        *,
        expected: OrderIntent | None = None,
    ) -> BrokerOrder | None:
        assert expected is not None
        assert expected.client_order_id == client_order_id
        self.lookups.append(client_order_id)
        return self._order(expected)


class CrashBeforeSubmission:
    async def submit_durable(self, intent: OrderIntent, **_kwargs: object) -> object:
        raise RuntimeError(f"crash before submitting {intent.client_order_id}")


class ConflictingAckBroker(ActionBroker):
    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        accepted = self._order(intent)
        with self.database.transaction() as session:
            StorageRepository(session).save_broker_order(accepted)
        self.submitted.append(intent)
        return accepted.model_copy(
            update={
                "status": "filled",
                "filled_quantity": intent.quantity,
                "filled_average_price": Decimal("75"),
            }
        )


def action_supervisor(
    database: Database,
    broker: ActionBroker,
    *,
    submitter: object | None = None,
) -> PositionProtectionSupervisor:
    coordinator = ExecutionCoordinator(
        database,
        cast(BrokerAdapter, broker),
        execution_settings(),
    )
    return PositionProtectionSupervisor(
        database=database,
        broker=broker,
        expected_account_id=ACCOUNT_ID,
        strategy_identity=IDENTITY,
        metrics=OperationalMetrics(),
        submitter=coordinator if submitter is None else submitter,
    )


def stored_cycles(database: Database) -> list[object]:
    with database.transaction() as session:
        return list(
            session.execute(
                select(position_protection_cycles).order_by(
                    position_protection_cycles.c.observed_at
                )
            ).mappings()
        )


def stored_evaluations(database: Database) -> list[object]:
    with database.transaction() as session:
        return list(
            session.execute(
                select(position_protection_evaluations).order_by(
                    position_protection_evaluations.c.symbol
                )
            ).mappings()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("price", ["125", "75"])
async def test_short_protection_routes_only_a_losing_short_to_a_buy_exit(database, price) -> None:
    positions = (position("XLE", quantity="-2", price=price),)
    seed_local(database, positions)
    broker = ActionBroker(database, positions)
    result = await action_supervisor(database, broker).evaluate(now=NOW)
    if price == "125":
        assert result.exit_required_count == 1
        assert len(broker.submitted) == 1
        assert broker.submitted[0].side is OrderSide.BUY
        assert broker.submitted[0].quantity == Decimal("2")
    else:
        assert result.outcome is ProtectionCycleOutcome.HEALTHY
        assert broker.submitted == []
    with database.transaction() as session:
        versions = (
            session.execute(select(position_protection_cycles.c.policy_version)).scalars().all()
        )
    assert versions == ["position-protection-v2"]


@pytest.mark.asyncio
async def test_zero_broker_quantity_records_an_incident_without_crashing_or_submitting(
    database,
) -> None:
    positions = (position("XLE", quantity="0", price="75"),)
    seed_local(database, positions)
    broker = ActionBroker(database, positions)
    result = await action_supervisor(database, broker).evaluate(now=NOW)
    assert result.outcome is ProtectionCycleOutcome.DEGRADED
    assert result.unprotected_count == 1
    assert broker.submitted == []
    with database.transaction() as session:
        outcomes = (
            session.execute(select(position_protection_evaluations.c.outcome)).scalars().all()
        )
        actions = session.execute(select(position_protection_actions.c.action_id)).scalars().all()
    assert outcomes == ["INCIDENT"]
    assert actions == []


@pytest.mark.asyncio
async def test_every_broker_position_gets_one_durable_decision(database: Database) -> None:
    broker_positions = (
        position("XLK", price="99"),
        position("XLE", price="75"),
    )
    seed_local(database, broker_positions)

    result = await supervisor(
        database,
        FakeBroker(snapshot=account(), positions=broker_positions),
    ).evaluate(now=NOW)

    assert result.position_count == result.evaluated_count == 2
    assert result.outcome is ProtectionCycleOutcome.ACTION_REQUIRED
    rows = stored_evaluations(database)
    assert [row["symbol"] for row in rows] == ["XLE", "XLK"]
    assert {row["symbol"]: row["outcome"] for row in rows} == {
        "XLE": ProtectionOutcome.EXIT_REQUIRED.value,
        "XLK": ProtectionOutcome.NO_ACTION.value,
    }
    assert stored_cycles(database)[0]["strategy_id"] == IDENTITY.strategy_id
    with database.transaction() as session:
        incidents = StorageRepository(session).list_incidents()
    assert [item.kind for item in incidents] == ["PROTECTIVE_EXIT_REQUIRED"]


@pytest.mark.asyncio
async def test_local_broker_disagreement_is_recorded_and_never_assumed_safe(
    database: Database,
) -> None:
    broker_positions = (position("XLE", quantity="2", price="75"),)
    seed_local(database, (position("XLE", quantity="1", price="75"),))

    result = await supervisor(
        database,
        FakeBroker(snapshot=account(), positions=broker_positions),
    ).evaluate(now=NOW)

    assert result.outcome is ProtectionCycleOutcome.DEGRADED
    assert result.unprotected_count == 1
    rows = stored_evaluations(database)
    assert [row["outcome"] for row in rows] == [ProtectionOutcome.BLOCKED_STATE.value]
    with database.transaction() as session:
        incidents = StorageRepository(session).list_incidents()
    assert [item.kind for item in incidents] == ["POSITION_STATE_UNRECONCILED"]


@pytest.mark.asyncio
async def test_broker_outage_becomes_a_typed_durable_incident(database: Database) -> None:
    seed_local(database, (position("XLE"),))
    failure = BrokerRequestError("secret-bearing provider response", status_code=503)

    result = await supervisor(
        database,
        FakeBroker(snapshot=account(), positions=(), account_error=failure),
    ).evaluate(now=NOW)

    assert result.outcome is ProtectionCycleOutcome.INCIDENT
    assert result.evaluated_count == 0
    cycle = stored_cycles(database)[0]
    assert cycle["reason"] == "BROKER_STATE_UNAVAILABLE"
    assert cycle["account_id"] == ACCOUNT_ID
    assert "secret-bearing" not in cycle["detail"]
    with database.transaction() as session:
        incidents = StorageRepository(session).list_incidents()
    assert [item.kind for item in incidents] == ["BROKER_STATE_UNAVAILABLE"]
    assert "secret-bearing" not in str(incidents[0].detail)


@pytest.mark.asyncio
async def test_expected_account_mismatch_never_reads_positions_as_ours(
    database: Database,
) -> None:
    seed_local(database, (position("XLE"),))
    broker = FakeBroker(
        snapshot=account(account_id="somebody-elses-paper-account"),
        positions=(position("XLE", price="75"),),
    )

    result = await supervisor(database, broker).evaluate(now=NOW)

    assert result.outcome is ProtectionCycleOutcome.INCIDENT
    assert result.reason == "BROKER_ACCOUNT_MISMATCH"
    cycle = stored_cycles(database)[0]
    assert cycle["account_id"] == ACCOUNT_ID
    assert "somebody-elses-paper-account" not in cycle["detail"]
    assert "reported_account_id_hash" in cycle["detail"]
    assert stored_evaluations(database) == []


@pytest.mark.asyncio
async def test_replaying_the_same_cycle_is_idempotent(database: Database) -> None:
    broker_positions = (position("XLE", quantity="2", price="75"),)
    seed_local(database, (position("XLE", quantity="1", price="75"),))
    subject = supervisor(
        database,
        FakeBroker(snapshot=account(), positions=broker_positions),
    )

    first = await subject.evaluate(now=NOW)
    second = await subject.evaluate(now=NOW)

    assert first == second
    assert len(stored_cycles(database)) == 1
    assert len(stored_evaluations(database)) == 1
    with database.transaction() as session:
        assert len(StorageRepository(session).list_incidents()) == 1


@pytest.mark.asyncio
async def test_same_instant_under_a_new_strategy_identity_is_a_distinct_cycle(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = FakeBroker(snapshot=account(), positions=broker_positions)

    first = await supervisor(database, broker).evaluate(now=NOW)
    alternate_identity = StrategyIdentity(
        strategy_id=IDENTITY.strategy_id,
        version="1.0.1",
        git_commit="def456",
        configuration_hash="cfg-456",
        deployment_timestamp=NOW - timedelta(days=1),
    )
    second = await PositionProtectionSupervisor(
        database=database,
        broker=broker,
        expected_account_id=ACCOUNT_ID,
        strategy_identity=alternate_identity,
        metrics=OperationalMetrics(),
    ).evaluate(now=NOW)

    assert first.cycle_id != second.cycle_id
    assert len(stored_cycles(database)) == 2
    assert len(stored_evaluations(database)) == 2


@pytest.mark.asyncio
async def test_exit_required_persists_one_action_and_intent_before_submission(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)

    await action_supervisor(database, broker).evaluate(now=NOW)

    assert len(broker.submitted) == 1
    with database.transaction() as session:
        actions = list(session.execute(select(position_protection_actions)).mappings())
        links = list(session.execute(select(position_protection_action_evaluations)).mappings())
        intent = StorageRepository(session).get_order_intent(str(actions[0]["intent_id"]))
    assert len(actions) == 1
    assert actions[0]["status"] == "WORKING"
    assert actions[0]["symbol"] == "XLE"
    assert actions[0]["quantity"] == "1"
    assert len(links) == 1
    assert links[0]["action_id"] == actions[0]["action_id"]
    assert intent is not None
    assert intent.state is IntentState.BROKER_ACCEPTED
    assert intent.client_order_id == actions[0]["client_order_id"]


@pytest.mark.asyncio
async def test_a_later_cycle_reuses_the_working_exit_instead_of_submitting_twice(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    await subject.evaluate(now=NOW + timedelta(minutes=1))

    assert len(broker.submitted) == 1
    assert broker.lookups == [broker.submitted[0].client_order_id]
    with database.transaction() as session:
        actions = list(session.execute(select(position_protection_actions)).mappings())
        links = list(session.execute(select(position_protection_action_evaluations)).mappings())
    assert len(actions) == 1
    assert len(links) == 2


@pytest.mark.asyncio
async def test_a_crash_before_broker_mutation_resumes_the_same_durable_action(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)

    with pytest.raises(RuntimeError, match="crash before submitting"):
        await action_supervisor(
            database,
            broker,
            submitter=CrashBeforeSubmission(),
        ).evaluate(now=NOW)

    with database.transaction() as session:
        actions_before = list(session.execute(select(position_protection_actions)).mappings())
        intent_before = StorageRepository(session).get_order_intent(
            str(actions_before[0]["intent_id"])
        )
    assert len(actions_before) == 1
    assert actions_before[0]["status"] == "PENDING"
    assert intent_before is not None
    assert intent_before.state is IntentState.RISK_APPROVED

    await action_supervisor(database, broker).evaluate(now=NOW)

    with database.transaction() as session:
        actions_after = list(session.execute(select(position_protection_actions)).mappings())
    assert [item.client_order_id for item in broker.submitted] == [
        actions_before[0]["client_order_id"]
    ]
    assert len(actions_after) == 1
    assert actions_after[0]["action_id"] == actions_before[0]["action_id"]
    assert actions_after[0]["status"] == "WORKING"


@pytest.mark.asyncio
async def test_a_partial_fill_remains_the_only_active_protective_exit(
    database: Database,
) -> None:
    broker_positions = (position("XLE", quantity="2", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(
        database,
        broker_positions,
        order_status="partially_filled",
        filled_quantity="1",
    )
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    await subject.evaluate(now=NOW + timedelta(minutes=1))

    assert len(broker.submitted) == 1
    with database.transaction() as session:
        actions = list(session.execute(select(position_protection_actions)).mappings())
        intent = StorageRepository(session).get_order_intent(str(actions[0]["intent_id"]))
    assert len(actions) == 1
    assert actions[0]["status"] == "WORKING"
    assert intent is not None
    assert intent.state is IntentState.PARTIALLY_FILLED


@pytest.mark.asyncio
async def test_a_rejected_exit_fails_closed_and_is_not_retried_blindly(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions, order_status="rejected")
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    await subject.evaluate(now=NOW + timedelta(minutes=1))

    assert len(broker.submitted) == 1
    with database.transaction() as session:
        actions = list(session.execute(select(position_protection_actions)).mappings())
        intent = StorageRepository(session).get_order_intent(str(actions[0]["intent_id"]))
    assert len(actions) == 1
    assert actions[0]["status"] == "FAILED"
    assert intent is not None
    assert intent.state is IntentState.REJECTED
    with database.transaction() as session:
        kinds = {item.kind for item in StorageRepository(session).list_incidents()}
    assert "PROTECTIVE_EXIT_TERMINAL" in kinds


@pytest.mark.asyncio
async def test_a_filled_exit_completes_the_action_and_a_flat_account_stays_flat(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(
        database,
        broker_positions,
        order_status="filled",
        filled_quantity="1",
    )
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    await subject.evaluate(now=NOW + timedelta(minutes=1))
    assert len(broker.submitted) == 1
    with database.transaction() as session:
        pending_flat_confirmation = list(
            session.execute(select(position_protection_actions)).mappings()
        )
    assert pending_flat_confirmation[0]["status"] == "FILLED_UNVERIFIED"

    broker.positions = ()
    await subject.evaluate(now=NOW + timedelta(minutes=2))

    assert len(broker.submitted) == 1
    with database.transaction() as session:
        actions = list(session.execute(select(position_protection_actions)).mappings())
    assert len(actions) == 1
    assert actions[0]["status"] == "COMPLETED"
    assert actions[0]["completed_at"] is not None
    assert actions[0]["flat_confirmation_snapshot_id"] is not None
    assert actions[0]["flat_confirmed_at"] is not None
    with database.transaction() as session:
        confirmations = {
            snapshot.snapshot_id: snapshot
            for snapshot in StorageRepository(session).list_account_snapshots(account_id=ACCOUNT_ID)
        }
        confirmation = confirmations.get(str(actions[0]["flat_confirmation_snapshot_id"]))
    assert confirmation is not None
    assert confirmation.positions == ()


@pytest.mark.asyncio
async def test_a_working_exit_reconciles_a_later_fill_without_resubmitting(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    broker.order_status = "filled"
    broker.filled_quantity = Decimal("1")
    await subject.evaluate(now=NOW + timedelta(minutes=1))

    assert len(broker.submitted) == 1
    assert broker.lookups == [broker.submitted[0].client_order_id]
    with database.transaction() as session:
        action = session.execute(select(position_protection_actions)).mappings().one()
        intent = StorageRepository(session).get_order_intent(str(action["intent_id"]))
    assert action["status"] == "FILLED_UNVERIFIED"
    assert intent is not None
    assert intent.state is IntentState.FILLED

    broker.positions = ()
    await subject.evaluate(now=NOW + timedelta(minutes=2))

    with database.transaction() as session:
        completed = session.execute(select(position_protection_actions)).mappings().one()
    assert completed["status"] == "COMPLETED"
    assert len(broker.submitted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("order_status", "expected_state"),
    [
        ("rejected", IntentState.REJECTED),
        ("canceled", IntentState.CANCELLED),
        ("expired", IntentState.EXPIRED),
    ],
)
async def test_a_working_exit_reconciles_terminal_failure_and_fails_closed(
    database: Database,
    order_status: str,
    expected_state: IntentState,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    broker.order_status = order_status
    await subject.evaluate(now=NOW + timedelta(minutes=1))
    await subject.evaluate(now=NOW + timedelta(minutes=2))

    assert len(broker.submitted) == 1
    assert len(broker.lookups) == 1
    with database.transaction() as session:
        action = session.execute(select(position_protection_actions)).mappings().one()
        intent = StorageRepository(session).get_order_intent(str(action["intent_id"]))
        kinds = {item.kind for item in StorageRepository(session).list_incidents()}
    assert action["status"] == "FAILED"
    assert intent is not None
    assert intent.state is expected_state
    assert "PROTECTIVE_EXIT_TERMINAL" in kinds


@pytest.mark.asyncio
async def test_a_working_exit_reconciles_a_later_partial_fill_without_resubmitting(
    database: Database,
) -> None:
    broker_positions = (position("XLE", quantity="2", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    broker.order_status = "partially_filled"
    broker.filled_quantity = Decimal("1")
    await subject.evaluate(now=NOW + timedelta(minutes=1))
    await subject.evaluate(now=NOW + timedelta(minutes=2))

    assert len(broker.submitted) == 1
    assert len(broker.lookups) == 2
    with database.transaction() as session:
        action = session.execute(select(position_protection_actions)).mappings().one()
        intent = StorageRepository(session).get_order_intent(str(action["intent_id"]))
    assert action["status"] == "WORKING"
    assert intent is not None
    assert intent.state is IntentState.PARTIALLY_FILLED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_status", "expected_state"),
    [
        ("canceled", IntentState.CANCELLED),
        ("expired", IntentState.EXPIRED),
    ],
)
async def test_a_partially_filled_exit_reconciles_later_terminal_status(
    database: Database,
    terminal_status: str,
    expected_state: IntentState,
) -> None:
    broker_positions = (position("XLE", quantity="2", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(database, broker_positions)
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    broker.order_status = "partially_filled"
    broker.filled_quantity = Decimal("1")
    await subject.evaluate(now=NOW + timedelta(minutes=1))
    broker.order_status = terminal_status
    await subject.evaluate(now=NOW + timedelta(minutes=2))

    assert len(broker.submitted) == 1
    assert len(broker.lookups) == 2
    with database.transaction() as session:
        action = session.execute(select(position_protection_actions)).mappings().one()
        intent = StorageRepository(session).get_order_intent(str(action["intent_id"]))
        kinds = {item.kind for item in StorageRepository(session).list_incidents()}
    assert action["status"] == "FAILED"
    assert intent is not None
    assert intent.state is expected_state
    assert "PROTECTIVE_EXIT_TERMINAL" in kinds


@pytest.mark.asyncio
async def test_a_stale_filled_exit_raises_one_incident_until_flatness_is_proven(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ActionBroker(
        database,
        broker_positions,
        order_status="filled",
        filled_quantity="1",
    )
    subject = action_supervisor(database, broker)

    await subject.evaluate(now=NOW)
    deadline = NOW + FILLED_POSITION_CONFIRMATION_TIMEOUT
    await subject.evaluate(now=deadline)
    await subject.evaluate(now=deadline + timedelta(minutes=1))

    with database.transaction() as session:
        incidents = [
            incident
            for incident in StorageRepository(session).list_incidents()
            if incident.kind == "PROTECTIVE_FILL_POSITION_NOT_FLAT"
        ]
    assert len(incidents) == 1
    assert incidents[0].resolved_at is None

    broker.positions = ()
    await subject.evaluate(now=deadline + timedelta(minutes=2))

    with database.transaction() as session:
        action = session.execute(select(position_protection_actions)).mappings().one()
        resolved = [
            incident
            for incident in StorageRepository(session).list_incidents()
            if incident.kind == "PROTECTIVE_FILL_POSITION_NOT_FLAT"
        ]
    assert action["status"] == "COMPLETED"
    assert resolved[0].resolved_at is not None


@pytest.mark.asyncio
async def test_a_durable_acknowledgement_conflict_fails_the_action_with_an_incident(
    database: Database,
) -> None:
    broker_positions = (position("XLE", price="75"),)
    seed_local(database, broker_positions)
    broker = ConflictingAckBroker(database, broker_positions)

    await action_supervisor(database, broker).evaluate(now=NOW)

    with database.transaction() as session:
        action = session.execute(select(position_protection_actions)).mappings().one()
        intent = StorageRepository(session).get_order_intent(str(action["intent_id"]))
        kinds = {item.kind for item in StorageRepository(session).list_incidents()}
    assert action["status"] == "FAILED"
    assert intent is not None
    assert intent.state is IntentState.ERROR
    assert "PROTECTIVE_EXIT_BROKER_ERROR" in kinds


def test_the_supervisor_import_graph_has_no_strategy_model_or_research_path() -> None:
    source = Path("src/quantbot/operations/protection.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    forbidden = sorted(
        name
        for name in imported
        if name.startswith(
            (
                "quantbot.strategy",
                "quantbot.research",
                "quantbot.anomaly",
                "quantbot.forecasting",
            )
        )
    )
    assert not forbidden
    assert "quantbot.risk.protection" in imported


class StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped


@dataclass
class FakeClock:
    open: bool
    next_open: datetime

    async def get_market_clock(self) -> MarketClock:
        return MarketClock(
            timestamp=NOW,
            is_open=self.open,
            next_open=self.next_open,
            next_close=NOW + timedelta(hours=6),
        )


@pytest.mark.asyncio
async def test_open_market_is_evaluated_immediately_in_the_independent_loop(
    tmp_path: Path,
) -> None:
    stop = StopFlag()
    calls: list[str] = []

    async def evaluate(moment: datetime) -> object:
        assert moment == NOW
        calls.append("evaluate")
        stop.stopped = True
        return object()

    async def sleeper(_delay: Decimal) -> None:
        calls.append("sleep")

    result = await PositionProtectionDaemonRunner(
        clock_provider=FakeClock(open=True, next_open=NOW),
        evaluate=evaluate,
        sleeper=sleeper,
        metrics=OperationalMetrics(),
        lock_path=tmp_path / "protection.lock",
    ).run(stop, now=lambda: NOW)

    assert calls == ["evaluate"]
    assert result.cycles_completed == 1


@pytest.mark.asyncio
async def test_closed_market_waits_for_open_instead_of_treating_an_old_mark_as_current(
    tmp_path: Path,
) -> None:
    stop = StopFlag()
    calls: list[str] = []

    async def evaluate(_moment: datetime) -> object:
        calls.append("evaluate")
        return object()

    async def sleeper(delay: Decimal) -> None:
        assert delay == Decimal("3600.0")
        calls.append("sleep")
        stop.stopped = True

    result = await PositionProtectionDaemonRunner(
        clock_provider=FakeClock(open=False, next_open=NOW + timedelta(hours=1)),
        evaluate=evaluate,
        sleeper=sleeper,
        metrics=OperationalMetrics(),
        lock_path=tmp_path / "protection.lock",
    ).run(stop, now=lambda: NOW)

    assert calls == ["sleep"]
    assert result.cycles_completed == 0


def test_the_independent_loop_has_no_strategy_or_order_submitter() -> None:
    fields = set(PositionProtectionDaemonRunner.__dataclass_fields__)
    assert "strategy" not in fields
    assert "submitter" not in fields
    assert "execution" not in fields
