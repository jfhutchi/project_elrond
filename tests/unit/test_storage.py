from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from quantbot.domain import (
    Account,
    Bar,
    BrokerOrder,
    Fill,
    IntentState,
    InvalidOrderTransition,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    ReconciliationStatus,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.storage import (
    AlreadyLockedError,
    Database,
    StateConflictError,
    StorageRepository,
    UnsupportedSchemaVersionError,
    decode_decimal,
    decode_utc,
    encode_decimal,
    encode_utc,
)
from quantbot.storage.schema import (
    SCHEMA_VERSION,
    kill_switch_state,
    metadata,
    schema_version,
)

NOW = datetime(2026, 8, 13, 14, 30, tzinfo=UTC)


def make_identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_id="mean-reversion",
        version="1.2.3",
        git_commit="abc1234",
        configuration_hash="def5678",
        deployment_timestamp=NOW,
    )


def make_bar(*, close: str = "105") -> Bar:
    return Bar(
        symbol="AAPL",
        timestamp=NOW,
        open="100",
        high="110",
        low="95",
        close=close,
        volume="1000",
        adjustment="1",
    )


def make_intent(
    *,
    intent_id: str = "intent-1",
    client_order_id: str = "client-1",
    state: IntentState = IntentState.RISK_APPROVED,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        client_order_id=client_order_id,
        strategy_id="mean-reversion",
        symbol="AAPL",
        signal_date=date(2026, 8, 13),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        quantity="5.2500",
        limit_price="100.25",
        created_at=NOW,
        state=state,
    )


def make_broker_order(
    *, status: str = "accepted", client_order_id: str = "client-1"
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-1",
        client_order_id=client_order_id,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        quantity="5.25",
        filled_quantity="0",
        status=status,
        submitted_at=NOW,
    )


def make_fill(*, price: str = "100.25") -> Fill:
    return Fill(
        fill_id="fill-1",
        broker_order_id="broker-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity="1.25",
        price=price,
        occurred_at=NOW + timedelta(minutes=1),
        fee="0.01",
    )


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "quantbot.db")
    yield db
    db.close()


def test_decimal_and_utc_codecs_are_canonical_and_strict() -> None:
    assert encode_decimal(Decimal("12.3400")) == "12.34"
    assert encode_decimal(Decimal("-0.000")) == "0"
    assert decode_decimal("12.34") == Decimal("12.34")
    assert encode_utc(NOW) == "2026-08-13T14:30:00Z"
    assert decode_utc("2026-08-13T10:30:00-04:00") == NOW
    assert decode_utc("2026-08-13T14:30:00Z").tzinfo is UTC

    with pytest.raises(ValueError, match="finite"):
        encode_decimal(Decimal("NaN"))
    with pytest.raises(ValueError, match="timezone-aware"):
        encode_utc(datetime(2026, 8, 13, 14, 30))
    with pytest.raises(ValueError, match="timezone-aware"):
        decode_utc("2026-08-13T14:30:00")


def test_database_initializes_versioned_schema_and_connection_pragmas(database: Database) -> None:
    expected_tables = {
        "account_snapshots",
        "bars",
        "broker_orders",
        "equity_snapshots",
        "fills",
        "incidents",
        "kill_switch_state",
        "order_events",
        "order_intents",
        "positions",
        "qualification_days",
        "reconciliation_diffs",
        "reconciliation_runs",
        "runs",
        "schema_version",
        "signals",
        "strategy_deployments",
    }

    assert expected_tables <= set(metadata.tables)
    with database.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
        assert (
            connection.exec_driver_sql("SELECT version FROM schema_version").scalar_one()
            == SCHEMA_VERSION
        )

        table_sql = " ".join(
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table'"
            )
            if row[0] is not None
        ).upper()
        assert " REAL" not in table_sql
        for credential_name in ("API_KEY", "API_SECRET", "PASSWORD", "AUTHORIZATION"):
            assert credential_name not in table_sql


def test_database_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.db"
    db = Database(path)
    with db.engine.begin() as connection:
        connection.exec_driver_sql("UPDATE schema_version SET version = 999 WHERE id = 1")
    db.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="999"):
        Database(path)


def test_alembic_upgrade_head_creates_complete_versioned_schema(tmp_path: Path) -> None:
    path = tmp_path / "alembic.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

            assert set(metadata.tables) <= set(inspect(connection).get_table_names())
            assert MigrationContext.configure(connection).get_current_revision() == "0001"
            assert (
                connection.exec_driver_sql(
                    "SELECT version FROM schema_version WHERE id = 1"
                ).scalar_one()
                == SCHEMA_VERSION
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("with_empty_alembic_table", [False, True])
def test_database_rejects_unversioned_nonempty_database_without_mutation(
    tmp_path: Path,
    with_empty_alembic_table: bool,
) -> None:
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE legacy_orders (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO legacy_orders (id) VALUES (7)")
        if with_empty_alembic_table:
            connection.exec_driver_sql(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"
            )
    engine.dispose()

    with pytest.raises(UnsupportedSchemaVersionError, match="unversioned"):
        Database(path)

    check_engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        with check_engine.connect() as connection:
            expected_tables = (
                ["alembic_version", "legacy_orders"]
                if with_empty_alembic_table
                else ["legacy_orders"]
            )
            assert inspect(connection).get_table_names() == expected_tables
            assert connection.exec_driver_sql("SELECT id FROM legacy_orders").scalar_one() == 7
    finally:
        check_engine.dispose()


def test_database_stamps_supported_pre_alembic_schema_at_head(tmp_path: Path) -> None:
    path = tmp_path / "supported-v1.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(schema_version.insert().values(id=1, version=SCHEMA_VERSION))
        connection.execute(
            kill_switch_state.insert().values(
                id=1,
                engaged=True,
                reason="pre-alembic V1",
                updated_at=encode_utc(NOW),
            )
        )
    engine.dispose()

    database = Database(path)
    try:
        with database.engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == "0001"
            assert (
                connection.exec_driver_sql(
                    "SELECT reason FROM kill_switch_state WHERE id = 1"
                ).scalar_one()
                == "pre-alembic V1"
            )
    finally:
        database.close()


def test_deployments_runs_and_failed_transaction_rollback(database: Database) -> None:
    identity = make_identity()
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.save_strategy_deployment(identity) is True
        assert repository.save_strategy_deployment(identity) is False
        repository.create_run("run-1", identity, started_at=NOW)

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.get_strategy_deployment("mean-reversion", "1.2.3") == identity
        run = repository.get_run("run-1")
        assert run is not None
        assert run.status == "RUNNING"

    with pytest.raises(RuntimeError, match="abort"):
        with database.transaction() as session:
            repository = StorageRepository(session)
            repository.create_run("run-rolled-back", identity, started_at=NOW)
            raise RuntimeError("abort")

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.get_run("run-rolled-back") is None
        repository.finish_run("run-1", finished_at=NOW + timedelta(minutes=3), status="SUCCEEDED")
        assert repository.get_run("run-1").status == "SUCCEEDED"  # type: ignore[union-attr]


def test_bar_observations_are_idempotent_and_conflicts_are_explicit(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert (
            repository.save_bars(
                [make_bar()], provider="polygon", adjustment_metadata={"split": True}
            )
            == 1
        )
        assert (
            repository.save_bars(
                [make_bar()], provider="polygon", adjustment_metadata={"split": True}
            )
            == 0
        )

        restored = repository.list_bars(symbol="AAPL", provider="polygon")
        assert restored == [make_bar()]
        assert restored[0].close == Decimal("105")
        assert restored[0].timestamp.tzinfo is UTC

        with pytest.raises(StateConflictError, match="bar"):
            repository.save_bars(
                [make_bar(close="106")],
                provider="polygon",
                adjustment_metadata={"split": True},
            )


def test_intent_lifecycle_is_atomic_and_optimistic(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.create_order_intent(make_intent()) is True
        assert repository.create_order_intent(make_intent()) is False
        transitioned = repository.transition_order_intent(
            "intent-1",
            IntentState.ORDER_CREATED,
            expected_current=IntentState.RISK_APPROVED,
        )
        assert transitioned.state is IntentState.ORDER_CREATED

        with pytest.raises(InvalidOrderTransition):
            repository.transition_order_intent(
                "intent-1",
                IntentState.FILLED,
                expected_current=IntentState.ORDER_CREATED,
            )
        assert repository.get_order_intent("intent-1").state is IntentState.ORDER_CREATED  # type: ignore[union-attr]

        with pytest.raises(StateConflictError, match="expected"):
            repository.transition_order_intent(
                "intent-1",
                IntentState.SUBMITTING,
                expected_current=IntentState.RISK_APPROVED,
            )
        assert repository.get_order_intent("intent-1").state is IntentState.ORDER_CREATED  # type: ignore[union-attr]


def test_broker_orders_fills_and_events_enforce_stable_identity(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(make_intent())
        assert repository.save_broker_order(make_broker_order()) is True
        assert repository.save_broker_order(make_broker_order()) is False
        with pytest.raises(StateConflictError, match="broker order"):
            repository.save_broker_order(make_broker_order(client_order_id="other-client"))

        assert repository.record_fill(make_fill()) is True
        assert repository.record_fill(make_fill()) is False
        with pytest.raises(StateConflictError, match="fill"):
            repository.record_fill(make_fill(price="100.50"))

        assert (
            repository.record_order_event(
                "event-1",
                event_type="accepted",
                occurred_at=NOW,
                intent_id="intent-1",
                broker_order_id="broker-1",
                detail={"source": "websocket"},
            )
            is True
        )
        assert (
            repository.record_order_event(
                "event-1",
                event_type="accepted",
                occurred_at=NOW,
                intent_id="intent-1",
                broker_order_id="broker-1",
                detail={"source": "websocket"},
            )
            is False
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "partially_filled"},
        {"filled_quantity": Decimal("1.25")},
        {"filled_average_price": Decimal("100.25")},
    ],
)
def test_broker_order_duplicate_payload_conflicts_leave_original_unchanged(
    database: Database,
    changes: dict[str, object],
) -> None:
    original = make_broker_order()
    conflicting = original.model_copy(update=changes)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(make_intent())
        assert repository.save_broker_order(original) is True

        with pytest.raises(StateConflictError, match="broker order"):
            repository.save_broker_order(conflicting)

        assert repository.get_broker_order(original.broker_order_id) == original


def test_snapshots_reconciliation_incidents_and_qualification(database: Database) -> None:
    account = Account(
        account_id="paper-account",
        cash="1000.50",
        buying_power="2001",
        equity="1500.25",
        currency="USD",
    )
    positions = [
        Position(
            symbol="AAPL",
            quantity="2.5",
            market_price="100",
            market_value="250",
            average_entry_price="95",
        )
    ]

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.save_account_snapshot("snapshot-1", account, positions, captured_at=NOW)
        snapshot = repository.get_latest_account_snapshot("paper-account")
        assert snapshot is not None
        assert snapshot.account == account
        assert snapshot.positions == tuple(positions)
        assert snapshot.captured_at.tzinfo is UTC

        assert repository.save_reconciliation(
            "recon-1",
            status=ReconciliationStatus.RECONCILIATION_REQUIRED,
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            diffs=[
                {
                    "diff_id": "diff-1",
                    "category": "position",
                    "key": "AAPL",
                    "expected": {"quantity": "2"},
                    "actual": {"quantity": "2.5"},
                }
            ],
        )
        reconciliation = repository.get_reconciliation("recon-1")
        assert reconciliation is not None
        assert reconciliation.status is ReconciliationStatus.RECONCILIATION_REQUIRED
        assert reconciliation.diffs[0].key == "AAPL"

        repository.create_incident(
            "incident-1",
            severity="HIGH",
            kind="RECONCILIATION",
            message="Position differs",
            occurred_at=NOW,
            detail={"symbol": "AAPL"},
        )
        assert [incident.incident_id for incident in repository.list_incidents()] == ["incident-1"]

        assert (
            repository.record_qualification_day(
                "mean-reversion", date(2026, 8, 13), qualified=True, detail={"reason": "passed"}
            )
            is True
        )
        assert (
            repository.record_qualification_day(
                "mean-reversion", date(2026, 8, 13), qualified=True, detail={"reason": "passed"}
            )
            is False
        )
        assert repository.count_qualification_days("mean-reversion") == 1


def test_signal_fill_event_equity_and_qualification_records_round_trip(database: Database) -> None:
    identity = make_identity()
    account = Account(
        account_id="paper-account",
        cash="1000.50",
        buying_power="2001",
        equity="1500.25",
        currency="USD",
    )
    fill = make_fill()
    signal_payload = {"score": Decimal("0.75"), "window_end": NOW}
    event_detail = {"source": "websocket", "sequence": 12}
    qualification_detail = {"reason": "passed", "equity": Decimal("1500.25")}

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_run("run-1", identity, started_at=NOW)
        repository.create_order_intent(make_intent())
        repository.save_broker_order(make_broker_order())

        assert (
            repository.record_signal(
                "signal-1",
                run_id="run-1",
                strategy_id=identity.strategy_id,
                symbol="AAPL",
                occurred_at=NOW,
                payload=signal_payload,
            )
            is True
        )
        assert (
            repository.record_signal(
                "signal-1",
                run_id="run-1",
                strategy_id=identity.strategy_id,
                symbol="AAPL",
                occurred_at=NOW,
                payload=signal_payload,
            )
            is False
        )
        with pytest.raises(StateConflictError, match="signal"):
            repository.record_signal(
                "signal-1",
                run_id="run-1",
                strategy_id=identity.strategy_id,
                symbol="AAPL",
                occurred_at=NOW,
                payload={"score": Decimal("0.80")},
            )

        repository.record_fill(fill)
        repository.record_order_event(
            "event-1",
            event_type="accepted",
            occurred_at=NOW,
            intent_id="intent-1",
            broker_order_id="broker-1",
            detail=event_detail,
        )
        repository.save_account_snapshot("snapshot-1", account, [], captured_at=NOW)
        repository.record_qualification_day(
            identity.strategy_id,
            date(2026, 8, 13),
            qualified=True,
            detail=qualification_detail,
        )

        signal = repository.get_signal("signal-1")
        assert signal is not None
        assert signal.signal_id == "signal-1"
        assert signal.run_id == "run-1"
        assert signal.strategy_id == identity.strategy_id
        assert signal.symbol == "AAPL"
        assert signal.occurred_at == NOW
        assert signal.payload == {"score": "0.75", "window_end": encode_utc(NOW)}
        assert repository.list_signals(run_id="run-1") == [signal]

        assert repository.get_fill(fill.fill_id) == fill
        assert repository.list_fills(broker_order_id="broker-1") == [fill]

        event = repository.get_order_event("event-1")
        assert event is not None
        assert event.event_id == "event-1"
        assert event.intent_id == "intent-1"
        assert event.broker_order_id == "broker-1"
        assert event.event_type == "accepted"
        assert event.occurred_at == NOW
        assert event.detail == event_detail
        assert repository.list_order_events(intent_id="intent-1") == [event]

        equity = repository.get_equity_snapshot("snapshot-1")
        assert equity is not None
        assert equity.snapshot_id == "snapshot-1"
        assert equity.account_snapshot_id == "snapshot-1"
        assert equity.account_id == account.account_id
        assert equity.captured_at == NOW
        assert equity.equity == Decimal("1500.25")
        assert equity.cash == Decimal("1000.5")
        assert repository.list_equity_snapshots(account_id=account.account_id) == [equity]

        qualification = repository.get_qualification_day(identity.strategy_id, date(2026, 8, 13))
        assert qualification is not None
        assert qualification.strategy_id == identity.strategy_id
        assert qualification.trading_date == date(2026, 8, 13)
        assert qualification.qualified is True
        assert qualification.detail == {"equity": "1500.25", "reason": "passed"}
        assert repository.list_qualification_days(identity.strategy_id) == [qualification]


def test_order_event_duplicate_conflicts(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(make_intent())
        repository.save_broker_order(make_broker_order())
        repository.record_order_event(
            "event-1",
            event_type="accepted",
            occurred_at=NOW,
            intent_id="intent-1",
            broker_order_id="broker-1",
            detail={"source": "websocket"},
        )

        with pytest.raises(StateConflictError, match="order event"):
            repository.record_order_event(
                "event-1",
                event_type="rejected",
                occurred_at=NOW,
                intent_id="intent-1",
                broker_order_id="broker-1",
                detail={"source": "websocket"},
            )


def test_kill_switch_defaults_engaged_and_persists_explicit_state(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        initial = repository.get_kill_switch_state()
        assert initial.engaged is True
        repository.set_kill_switch(False, reason="operator reset", updated_at=NOW)
        saved = repository.get_kill_switch_state()
        assert saved.engaged is False
        assert saved.reason == "operator reset"


def test_single_writer_lock_rejects_second_owner_and_can_be_reacquired(tmp_path: Path) -> None:
    from quantbot.storage import SingleWriterLock

    lock_path = tmp_path / "quantbot.writer.lock"
    first = SingleWriterLock(lock_path)
    second = SingleWriterLock(lock_path)

    first.acquire()
    try:
        with pytest.raises(AlreadyLockedError):
            second.acquire()
    finally:
        first.release()

    with second:
        assert second.is_acquired is True
    assert second.is_acquired is False
