"""The explicit v29 -> v30 fixture migration preserves stops and historical evidence."""

import sqlite3

import pytest
from alembic import command
from sqlalchemy import create_engine

from quantbot.storage import Database, MigrationRequiredError, StorageRepository


def test_explicit_v29_fixture_migration_is_additive_and_does_not_adopt_or_resume(tmp_path):
    path = tmp_path / "v29.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0029")
        connection.exec_driver_sql(
            "UPDATE kill_switch_state SET reason='operator pause', engaged=1 WHERE id=1"
        )
        connection.exec_driver_sql(
            "INSERT INTO account_snapshots VALUES ('legacy', NULL, 'synthetic', "
            "'2026-10-14T14:00:00+00:00', '100', '100', '100', 'USD')"
        )
    engine.dispose()
    before = path.read_bytes()
    with pytest.raises(MigrationRequiredError):
        Database(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        old_tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    with Database.migrate(path) as db:
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_kill_switch_state().reason == "operator pause"
            assert repo.get_kill_switch_state().engaged
            assert repo.get_latest_account_snapshot("synthetic").snapshot_id == "legacy"
            assert repo.get_latest_account_snapshot("synthetic", certified_only=True) is None
    with sqlite3.connect(path) as connection:
        new_tables = {
            r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert old_tables <= new_tables
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 30
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        backup_path = tmp_path / "restored.db"
        with sqlite3.connect(backup_path) as restored:
            connection.backup(restored)
    with Database(backup_path) as restored:
        with restored.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state().engaged


def test_v30_refuses_destructive_downgrade_and_preserves_evidence(tmp_path):
    path = tmp_path / "v30.db"
    Database.create(path).close()
    before = path.read_bytes()
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        with pytest.raises(RuntimeError, match="evidence-preserving"):
            with engine.begin() as connection:
                command.downgrade(Database._alembic_config(connection), "0029")
    finally:
        engine.dispose()
    assert path.read_bytes() == before


def test_migration_does_not_accept_malformed_preexisting_core_table(tmp_path):
    from quantbot.storage import UnsupportedSchemaVersionError

    path = tmp_path / "malformed.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        command.upgrade(Database._alembic_config(connection), "0029")
        connection.exec_driver_sql("CREATE TABLE core_identities (id VARCHAR(64) PRIMARY KEY)")
    engine.dispose()
    with pytest.raises(UnsupportedSchemaVersionError, match="incompatible schema"):
        Database.migrate(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 29
