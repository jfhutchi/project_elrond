"""Shared storage contract on real engines; PostgreSQL is opt-in test infrastructure only."""

from __future__ import annotations

import hashlib
import os
from argparse import Namespace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from quantbot.domain import Account, Bar, StrategyIdentity
from quantbot.research.memory import (
    MemoryError_,
    RecordKind,
    ResearchMemory,
    ResearchRecord,
    Verdict,
)
from quantbot.storage import StateConflictError, StorageRepository, encode_decimal, encode_utc
from quantbot.storage.migrations.metadata import metadata_for_dialect
from quantbot.storage.schema import (
    SCHEMA_VERSION,
    bars,
    ingestion_cursors,
    ingestion_sources,
    metadata,
    research_records,
    schema_version,
)
from quantbot.storage.schema import (
    position_protection_actions as actions,
)
from quantbot.storage.schema import (
    position_protection_cycles as cycles,
)
from quantbot.storage.schema import (
    position_protection_evaluations as evaluations,
)

NOW = datetime(2026, 9, 6, 14, 0, 0, 123456, tzinfo=UTC)
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _test_database_url(url, name):
    return url.set(database=name).difference_update_query(["dbname"])


def test_libpq_database_query_cannot_override_the_isolated_database_name():
    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg

    url = make_url("postgresql+psycopg://test@localhost/postgres?dbname=postgres")
    _, parameters = PGDialect_psycopg().create_connect_args(
        _test_database_url(url, "elrond_test_123")
    )
    assert parameters["dbname"] == "elrond_test_123"


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def engine(request, tmp_path_factory):
    if request.param == "sqlite":
        instance = create_engine(f"sqlite:///{tmp_path_factory.mktemp('parity') / 'test.db'}")

        @event.listens_for(instance, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        yield instance
        instance.dispose()
        return
    raw_url = os.environ.get("QUANTBOT_TEST_POSTGRES_ADMIN_URL")
    if not raw_url:
        pytest.skip("set QUANTBOT_TEST_POSTGRES_ADMIN_URL for disposable PostgreSQL parity tests")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        pytest.fail("parity admin URL must select PostgreSQL")
    # Only the fresh random database created here is migrated and dropped.
    name = f"elrond_test_{uuid4().hex}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    instance = create_engine(_test_database_url(url, name))
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        try:
            with instance.connect() as connection:
                if connection.exec_driver_sql("SELECT current_database()").scalar_one() != name:
                    raise RuntimeError("test connection did not select the newly created database")
            yield instance
        finally:
            instance.dispose()
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{name}"')
    finally:
        admin.dispose()


@pytest.fixture(scope="module", params=["fresh", "migrated"])
def prepared(request, engine):
    with engine.begin() as connection:
        assert not inspect(connection).get_table_names()
        metadata.create_all(connection)
        expected_checks = _check_definitions(connection)
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        if request.param == "fresh":
            connection.execute(
                schema_version.insert().values(
                    id=1, version=SCHEMA_VERSION, store_identity=str(uuid4())
                )
            )
            command.stamp(config, "head")
        else:
            metadata.drop_all(connection)
            command.upgrade(config, "head")
    yield engine, expected_checks
    with engine.begin() as connection:
        metadata.drop_all(connection)
        connection.exec_driver_sql("DROP TABLE alembic_version")


@pytest.fixture
def connection(prepared):
    engine, expected_checks = prepared
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.info["parity_expected_checks"] = expected_checks
            yield connection
        finally:
            transaction.rollback()


def _check_definitions(connection):
    inspector = inspect(connection)
    return {
        table: {
            check["name"]: " ".join(check["sqltext"].split())
            for check in inspector.get_check_constraints(table)
        }
        for table in inspector.get_table_names()
        if table != "alembic_version"
    }


def _assert_check_definitions(connection, expected):
    assert _check_definitions(connection) == expected


def test_head_layout_and_all_logical_constraints_match(connection):
    assert (
        compare_metadata(
            MigrationContext.configure(connection), metadata_for_dialect(connection.dialect)
        )
        == []
    )
    inspector = inspect(connection)
    assert set(inspector.get_table_names()) == set(metadata.tables) | {"alembic_version"}
    for table in metadata.sorted_tables:
        expected = [item for item in table.constraints if isinstance(item, CheckConstraint)]
        assert len(inspector.get_check_constraints(table.name)) == len(expected), table.name
    _assert_check_definitions(connection, connection.info["parity_expected_checks"])
    assert connection.execute(select(schema_version.c.version)).scalar_one() == SCHEMA_VERSION
    assert MigrationContext.configure(connection).get_current_revision() == f"{SCHEMA_VERSION:04}"
    config = Config(str(ALEMBIC_INI))
    config.attributes["connection"] = connection
    command.check(config)


def test_check_comparison_detects_changed_expression_with_the_same_name():
    # Alembic compares CHECK names, not definitions. A real altered schema must fail our guard.
    instance = create_engine("sqlite://")
    try:
        with instance.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE probe (version INTEGER, CONSTRAINT positive CHECK (version >= 1))"
            )
            expected = _check_definitions(connection)
            connection.exec_driver_sql("DROP TABLE probe")
            connection.exec_driver_sql(
                "CREATE TABLE probe (version INTEGER, CONSTRAINT positive CHECK (1 = 1))"
            )
            with pytest.raises(AssertionError):
                _assert_check_definitions(connection, expected)
    finally:
        instance.dispose()


@pytest.mark.parametrize("flag", ["reconciled", "protected"])
def test_non_boolean_integer_protection_flags_are_refused(connection, flag):
    _protection_action(connection, 1, status="PENDING")
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(evaluations.update().values({flag: 2}))


def test_research_documents_keep_exact_bytes_hashes_and_append_only_semantics(connection):
    memory = ResearchMemory(Session(bind=connection))
    entry = ResearchRecord(
        record_id="parity-evidence",
        kind=RecordKind.FINDING,
        verdict=Verdict.EXPLORATORY_ONLY,
        subject="test fixture",
        statement="Synthetic parity record",
        source="test_storage_parity",
        recorded_at=NOW,
        evidence_hash="a" * 64,
        detail={"decimal": "0.12345678901234567890", "quoted": 'a "quote" and a newline\n'},
    )
    expected = entry.model_dump_json()
    digest = hashlib.sha256(expected.encode()).hexdigest()
    assert memory.record(entry) is True
    assert memory.record(entry) is False
    assert memory.get(entry.record_id) == entry
    with pytest.raises(MemoryError_, match="different content"):
        memory.record(entry.model_copy(update={"statement": "rewritten"}))
    stored = connection.execute(select(research_records.c.document_json)).scalar_one()
    assert stored == expected
    assert hashlib.sha256(stored.encode()).hexdigest() == digest


def test_canonical_observations_are_idempotent_and_immutable(connection):
    repository = StorageRepository(Session(bind=connection))
    bar = Bar(
        symbol="SPY",
        timestamp=NOW,
        open="100.1234567890123456789",
        high="110",
        low="90",
        close="101",
        volume="1234.5678",
        adjustment="1",
    )
    assert repository.save_bars([bar], provider="parity") == 1
    assert repository.save_bars([bar], provider="parity") == 0
    assert repository.list_bars(provider="parity") == [bar]
    row = connection.execute(select(bars)).mappings().one()
    assert row["open"] == encode_decimal(bar.open)
    assert row["timestamp"] == encode_utc(NOW)
    assert row["bar_id"] > 0
    before = dict(row)
    with pytest.raises(StateConflictError, match="bar observation conflicts"):
        repository.save_bars([bar.model_copy(update={"close": Decimal("102")})], provider="parity")
    assert dict(connection.execute(select(bars)).mappings().one()) == before


def test_boolean_round_trip_check_and_foreign_key_refusal(connection):
    connection.execute(
        ingestion_sources.insert().values(
            source_id="source",
            provider="test",
            source_kind="bars",
            configuration_hash="hash",
            enabled=False,
            created_at=encode_utc(NOW),
            updated_at=encode_utc(NOW),
        )
    )
    assert connection.execute(select(ingestion_sources.c.enabled)).scalar_one() is False
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            ingestion_cursors.insert().values(
                source_id="missing", stream_key="s", version=1, updated_at=encode_utc(NOW)
            )
        )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            ingestion_cursors.insert().values(
                source_id="source", stream_key="s", version=0, updated_at=encode_utc(NOW)
            )
        )
    connection.execute(
        ingestion_cursors.insert().values(
            source_id="source", stream_key="s", version=1, updated_at=encode_utc(NOW)
        )
    )
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(ingestion_sources.delete())


def _protection_action(connection, ordinal, *, status):
    stamp = encode_utc(NOW)
    repository = StorageRepository(Session(bind=connection))
    if ordinal == 1:
        repository.save_strategy_deployment(
            StrategyIdentity(
                strategy_id="test",
                version="v1",
                git_commit="abc",
                configuration_hash="hash",
                deployment_timestamp=NOW,
            )
        )
        repository.save_account_snapshot(
            "flat",
            Account(
                account_id="account", cash="100", buying_power="100", equity="100", currency="USD"
            ),
            (),
            captured_at=NOW,
        )
    common = dict(
        strategy_id="test",
        strategy_version="v1",
        configuration_hash="hash",
        account_id="account",
        policy_version="v1",
        policy_hash="hash",
    )
    cycle_id, evaluation_id = f"cycle-{ordinal}", f"eval-{ordinal}"
    connection.execute(
        cycles.insert().values(
            **common,
            cycle_id=cycle_id,
            observed_at=stamp,
            completed_at=stamp,
            outcome="ACTION_REQUIRED",
            reason="exit",
            position_count=1,
            evaluated_count=1,
            unprotected_count=0,
            exit_required_count=1,
            detail="{}",
        )
    )
    connection.execute(
        evaluations.insert().values(
            **common,
            evaluation_id=evaluation_id,
            cycle_id=cycle_id,
            symbol="SPY",
            quantity="1",
            average_entry_price="100",
            price="75",
            price_as_of=stamp,
            price_source="test",
            reconciled=1,
            outcome="EXIT_REQUIRED",
            rule="HARD_STOP",
            reason="exit",
            move_bps="-2500",
            protected=1,
            evaluated_at=stamp,
            detail="{}",
        )
    )
    intent_id = f"intent-{ordinal}"
    connection.execute(
        metadata.tables["order_intents"]
        .insert()
        .values(
            intent_id=intent_id,
            client_order_id=intent_id,
            strategy_id="test",
            signal_date="2026-09-06",
            symbol="SPY",
            side="SELL",
            order_type="MARKET",
            time_in_force="DAY",
            quantity="1",
            extended_hours=False,
            created_at=stamp,
            state="RISK_APPROVED",
        )
    )
    completed = status == "COMPLETED"
    connection.execute(
        actions.insert().values(
            action_id=f"action-{ordinal}",
            trigger_evaluation_id=evaluation_id,
            account_id="account",
            symbol="SPY",
            side="SELL",
            quantity="1",
            intent_id=intent_id,
            client_order_id=intent_id,
            status=status,
            created_at=stamp,
            updated_at=stamp,
            completed_at=stamp if completed else None,
            flat_confirmation_snapshot_id="flat" if completed else None,
            flat_confirmed_at=stamp if completed else None,
            last_error="",
            detail="{}",
        )
    )


@pytest.mark.parametrize(
    "active_status", ["PENDING", "WORKING", "UNCERTAIN", "FILLED_UNVERIFIED", "FAILED"]
)
def test_completed_actions_release_partial_uniqueness_but_active_actions_do_not(
    connection, active_status
):
    _protection_action(connection, 1, status="COMPLETED")
    _protection_action(connection, 2, status=active_status)
    with pytest.raises(IntegrityError), connection.begin_nested():
        _protection_action(connection, 3, status="FAILED")
    assert connection.execute(
        select(actions.c.action_id).order_by(actions.c.action_id)
    ).scalars().all() == [
        "action-1",
        "action-2",
    ]


def test_declared_protection_cascade_removes_child_evaluations(connection):
    _protection_action(connection, 1, status="PENDING")
    connection.execute(actions.delete())
    connection.execute(cycles.delete())
    assert connection.execute(select(evaluations.c.evaluation_id)).scalars().all() == []


def test_dialect_comparison_does_not_rename_canonical_metadata(connection):
    before = [
        constraint.name for table in metadata.sorted_tables for constraint in table.constraints
    ]
    metadata_for_dialect(connection.dialect)
    assert [
        constraint.name for table in metadata.sorted_tables for constraint in table.constraints
    ] == before


def test_runtime_postgresql_stays_disabled():
    from quantbot.storage.target import BackendNotEnabled, parse_database_url

    target = parse_database_url("postgresql://localhost/elrond_test_never_opened")
    with pytest.raises(BackendNotEnabled, match="not enabled"):
        target.require_enabled()


def test_alembic_refuses_an_unspecified_database_instead_of_creating_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="explicit"):
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
    assert list(tmp_path.iterdir()) == []


def test_postgresql_offline_sql_contains_the_full_chain_and_partial_index():
    output = StringIO()
    config = Config(str(ALEMBIC_INI), output_buffer=output)
    config.set_main_option("sqlalchemy.url", "postgresql://localhost/unused_offline")
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE" in sql
    assert "0026 -> 0027" in sql
    assert (
        "WHERE status IN ('PENDING', 'WORKING', 'UNCERTAIN', 'FILLED_UNVERIFIED', 'FAILED')" in sql
    )


def test_explicit_cli_database_url_runs_online_migrations(tmp_path):
    path = tmp_path / "explicit.db"
    config = Config(str(ALEMBIC_INI))
    config.cmd_opts = Namespace(x=[f"database_url=sqlite:///{path.as_posix()}"])
    command.upgrade(config, "head")
    instance = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with instance.connect() as connection:
            assert (
                connection.execute(select(schema_version.c.version)).scalar_one() == SCHEMA_VERSION
            )
    finally:
        instance.dispose()
