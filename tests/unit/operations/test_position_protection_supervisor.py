"""Independent after-entry protection runtime (#157).

These tests define the next slice after the pure evaluator: a separately schedulable process
must cover every broker-open paper position with durable, attributable state. It deliberately
does not submit exits; an EXIT_REQUIRED decision is surfaced as ACTION_REQUIRED.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from quantbot.brokers import BrokerAccountSnapshot, BrokerRequestError
from quantbot.domain import Account, MarketClock, Position, StrategyIdentity
from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.protection import (
    PositionProtectionDaemonRunner,
    PositionProtectionSupervisor,
    ProtectionCycleOutcome,
)
from quantbot.risk.protection import ProtectionOutcome
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import (
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
