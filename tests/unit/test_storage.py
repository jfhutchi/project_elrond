from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
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
from quantbot.storage.database import REVISION_FOR_SCHEMA_VERSION
from quantbot.storage.schema import (
    SCHEMA_VERSION,
    kill_switch_state,
    metadata,
    runs,
    schema_version,
    strategy_deployments,
)

NOW = datetime(2026, 8, 13, 14, 30, tzinfo=UTC)
HEAD_REVISION = REVISION_FOR_SCHEMA_VERSION[SCHEMA_VERSION]


def make_identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_id="mean-reversion",
        version="1.2.3",
        git_commit="abc1234",
        configuration_hash="def5678",
        deployment_timestamp=NOW,
    )


def make_bar(*, close: str = "105", volume: str = "1000") -> Bar:
    return Bar(
        symbol="AAPL",
        timestamp=NOW,
        open="100",
        high="110",
        low="95",
        close=close,
        volume=volume,
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
            assert MigrationContext.configure(connection).get_current_revision() == HEAD_REVISION
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


def test_rejected_legacy_database_preserves_journal_mode_and_contents(tmp_path: Path) -> None:
    path = tmp_path / "legacy-delete-journal.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_orders (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO legacy_orders (id) VALUES (7)")
        connection.commit()
        original_journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()

    assert original_journal_mode == "delete"
    with pytest.raises(UnsupportedSchemaVersionError, match="unversioned"):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == original_journal_mode
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall() == [("legacy_orders",)]
        assert connection.execute("SELECT id FROM legacy_orders").fetchall() == [(7,)]
    finally:
        connection.close()


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
            assert MigrationContext.configure(connection).get_current_revision() == HEAD_REVISION
            assert (
                connection.exec_driver_sql(
                    "SELECT reason FROM kill_switch_state WHERE id = 1"
                ).scalar_one()
                == "pre-alembic V1"
            )
    finally:
        database.close()


@pytest.mark.parametrize(
    "corruption_sql",
    [
        ("ALTER TABLE incidents DROP COLUMN detail_json",),
        ("DROP INDEX ix_broker_orders_status",),
        (
            "PRAGMA foreign_keys=OFF",
            "DROP TABLE incidents",
            """CREATE TABLE incidents (
                incident_id VARCHAR(128) NOT NULL PRIMARY KEY,
                run_id VARCHAR(128),
                severity VARCHAR(32) NOT NULL,
                kind VARCHAR(64) NOT NULL,
                message TEXT NOT NULL,
                occurred_at VARCHAR(40) NOT NULL,
                resolved_at VARCHAR(40),
                detail_json INTEGER NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs (run_id)
            )""",
            "CREATE INDEX ix_incidents_occurred ON incidents (occurred_at)",
        ),
        (
            "PRAGMA foreign_keys=OFF",
            "DROP TABLE incidents",
            """CREATE TABLE incidents (
                incident_id VARCHAR(128) NOT NULL PRIMARY KEY,
                run_id VARCHAR(128),
                severity VARCHAR(32) NOT NULL,
                kind VARCHAR(64) NOT NULL,
                message TEXT NOT NULL,
                occurred_at VARCHAR(40) NOT NULL,
                resolved_at VARCHAR(40),
                detail_json TEXT NOT NULL
            )""",
            "CREATE INDEX ix_incidents_occurred ON incidents (occurred_at)",
        ),
        (
            "PRAGMA foreign_keys=OFF",
            "DROP TABLE broker_orders",
            """CREATE TABLE broker_orders (
                broker_order_id VARCHAR(128) NOT NULL PRIMARY KEY,
                client_order_id VARCHAR(128) NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                side VARCHAR(8) NOT NULL,
                order_type VARCHAR(16) NOT NULL,
                time_in_force VARCHAR(8) NOT NULL,
                quantity TEXT NOT NULL,
                filled_quantity TEXT NOT NULL,
                status VARCHAR(64) NOT NULL,
                submitted_at VARCHAR(40) NOT NULL,
                filled_average_price TEXT,
                FOREIGN KEY(client_order_id) REFERENCES order_intents (client_order_id)
            )""",
            "CREATE INDEX ix_broker_orders_status ON broker_orders (status)",
        ),
    ],
    ids=["missing-column", "missing-index", "altered-type", "missing-fk", "missing-unique"],
)
def test_database_rejects_corrupt_supported_pre_alembic_schema_without_mutation(
    tmp_path: Path,
    corruption_sql: tuple[str, ...],
) -> None:
    path = tmp_path / "corrupt-supported-v1.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(schema_version.insert().values(id=1, version=SCHEMA_VERSION))
        connection.execute(
            kill_switch_state.insert().values(
                id=1,
                engaged=True,
                reason="must survive rejected preflight",
                updated_at=encode_utc(NOW),
            )
        )
        for statement in corruption_sql:
            connection.exec_driver_sql(statement)
    engine.dispose()

    raw_connection = sqlite3.connect(path)
    try:
        original_journal_mode = raw_connection.execute("PRAGMA journal_mode").fetchone()[0]
        original_schema = raw_connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        original_reason = raw_connection.execute(
            "SELECT reason FROM kill_switch_state WHERE id = 1"
        ).fetchone()
    finally:
        raw_connection.close()

    with pytest.raises(UnsupportedSchemaVersionError, match="incompatible schema"):
        Database(path)

    raw_connection = sqlite3.connect(path)
    try:
        assert raw_connection.execute("PRAGMA journal_mode").fetchone()[0] == original_journal_mode
        assert (
            raw_connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            == original_schema
        )
        assert (
            raw_connection.execute("SELECT reason FROM kill_switch_state WHERE id = 1").fetchone()
            == original_reason
        )
        assert "alembic_version" not in {
            row[0]
            for row in raw_connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        raw_connection.close()


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

        with pytest.raises(StateConflictError, match="close") as conflict:
            repository.save_bars(
                [make_bar(close="106")],
                provider="polygon",
                adjustment_metadata={"split": True},
            )
        # The message has to say which field moved and what both values were. "bar observation
        # conflicts" alone sent an operator to the database to find out what changed.
        assert "105" in str(conflict.value)
        assert "106" in str(conflict.value)


def test_a_revised_volume_is_not_a_conflict_and_does_not_overwrite(database: Database) -> None:
    """Vendors restate consolidated volume after the close; that is not restated history.

    On 2026-08-20 this took the live daemon down. EEM at 2026-08-18 was stored with volume
    2025744 and re-fetched as 2028904 -- 0.16%, with open, high, low, close and the adjustment
    factor all identical -- and the conflict guard exited the process with code 2. A handful of
    trades reporting late is not a restatement and must not stop trading.

    The stored value is kept rather than updated, which matters more than it first appears:
    `strategy/identity.py` hashes volume into the dataset fingerprint, so adopting a revision
    would silently change the hash of results already recorded against the original.
    """
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.save_bars([make_bar(volume="2025744")], provider="alpaca") == 1

        # Same bar, volume revised upward by the vendor. No exception, no new row.
        assert repository.save_bars([make_bar(volume="2028904")], provider="alpaca") == 0

        restored = repository.list_bars(symbol="AAPL", provider="alpaca")
        assert len(restored) == 1
        assert restored[0].volume == Decimal("2025744"), (
            "the first observation is what was knowable"
        )


def test_a_revised_price_is_still_a_conflict(database: Database) -> None:
    """The other half: tolerating volume must not tolerate a restated price.

    Without this, `test_a_revised_volume_is_not_a_conflict` could pass with the guard removed
    entirely, and the check that exists to catch rewritten history would be gone.
    """
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_bars([make_bar(close="105", volume="1000")], provider="alpaca")

        for field, revised in (
            ("close", make_bar(close="106", volume="1000")),
            # A price change arriving alongside a volume revision is still a price change.
            ("close", make_bar(close="106", volume="2028904")),
        ):
            with pytest.raises(StateConflictError, match=field):
                repository.save_bars([revised], provider="alpaca")


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


def test_a_v1_database_upgrades_to_head_and_carries_its_ledger_across(tmp_path: Path) -> None:
    """The point of wiring Alembic in: an existing paper store gains tables, loses nothing.

    Pinned to the table list revision 0001 actually creates, so this stays a V1 database even
    after later revisions extend `metadata`.
    """
    v1_migration = import_module("quantbot.storage.migrations.versions.0001_initial_schema")
    v1_tables = [metadata.tables[name] for name in v1_migration.V1_TABLES]

    path = tmp_path / "v1-with-history.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        metadata.create_all(connection, tables=v1_tables)
        connection.execute(schema_version.insert().values(id=1, version=1))
        connection.execute(
            kill_switch_state.insert().values(
                id=1, engaged=True, reason="engaged before the upgrade", updated_at=encode_utc(NOW)
            )
        )
        connection.execute(
            strategy_deployments.insert().values(
                strategy_id="mean-reversion",
                version="1.2.3",
                git_commit="abc1234",
                configuration_hash="def5678",
                deployment_timestamp=encode_utc(NOW),
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-1",
                strategy_id="mean-reversion",
                strategy_version="1.2.3",
                status="COMPLETED",
                started_at=encode_utc(NOW),
            )
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('0001')")
    engine.dispose()

    database = Database(path)
    try:
        with database.engine.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT version FROM schema_version").scalar_one()
                == SCHEMA_VERSION
            )
            assert MigrationContext.configure(connection).get_current_revision() == HEAD_REVISION
            assert set(metadata.tables) <= set(inspect(connection).get_table_names())
            assert (
                connection.exec_driver_sql(
                    "SELECT reason FROM kill_switch_state WHERE id = 1"
                ).scalar_one()
                == "engaged before the upgrade"
            )
            assert [
                tuple(row) for row in connection.exec_driver_sql("SELECT run_id, status FROM runs")
            ] == [("run-1", "COMPLETED")]
    finally:
        database.close()


def test_a_database_one_version_ahead_of_this_build_is_refused(tmp_path: Path) -> None:
    """Older is upgradable, newer is fatal. Asserted at the boundary, not at an absurd value."""
    path = tmp_path / "from-the-future.db"
    db = Database(path)
    with db.engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE schema_version SET version = {SCHEMA_VERSION + 1} WHERE id = 1"
        )
    db.close()

    with pytest.raises(UnsupportedSchemaVersionError, match=str(SCHEMA_VERSION + 1)):
        Database(path)


def test_no_migration_imports_the_live_schema_module() -> None:
    """A revision must describe the schema it created, not the one that exists today.

    Pinning table *names* in 0001 was not enough: `metadata.tables[name]` still took each table's
    *shape* from `schema.py`, so the day a later revision added a column or changed a constraint on
    a V1 table, revision 0001 would silently begin creating the new shape. A fresh database
    replaying the chain would then diverge from one that actually lived through it.

    This is the assertion that keeps it fixed. It fails if any revision reaches back into the live
    schema module for anything at all — which is stricter than strictly necessary for a table like
    `schema_version`, and deliberately so: the rule is only enforceable if it has no exceptions to
    argue about.
    """
    import ast

    root = Path(__file__).parents[2] / "src" / "quantbot" / "storage" / "migrations"
    versions = root / "versions"
    offenders: list[str] = []
    for source in sorted(versions.glob("[0-9]*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "quantbot.storage.schema"
            ):
                offenders.append(f"{source.name} imports {node.module}")
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{source.name} imports {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("quantbot.storage.schema")
                )
    assert offenders == [], (
        "migrations must declare their own tables literally, not import live metadata: "
        + "; ".join(offenders)
    )


def test_replaying_every_migration_reproduces_the_head_schema_column_for_column(
    tmp_path: Path,
) -> None:
    """The chain and `schema.py` must agree on shape, not merely on table names.

    The pre-existing upgrade test asserts `set(metadata.tables) <= get_table_names()`, which passes
    even if every column in every table is wrong. This compares the reflected result of replaying
    0001 -> head against the live metadata, so drift in either direction fails here rather than
    surfacing as a confusing failure inside a later migration on a fresh install.
    """
    path = tmp_path / "chain.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        inspector = inspect(engine)
        for name, table in sorted(metadata.tables.items()):
            reflected = {
                column["name"]: (str(column["type"]), bool(column["nullable"]))
                for column in inspector.get_columns(name)
            }
            declared = {
                column.name: (str(column.type), bool(column.nullable)) for column in table.columns
            }
            assert reflected == declared, (
                f"{name} differs between the migration chain and schema.py"
            )
            assert set(inspector.get_pk_constraint(name)["constrained_columns"]) == {
                column.name for column in table.primary_key.columns
            }, f"{name} primary key differs between the migration chain and schema.py"
    finally:
        engine.dispose()
