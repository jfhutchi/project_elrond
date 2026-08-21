"""Lifecycle and schema validation for the isolated shadow-forecast database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from quantbot.forecasting.schema import (
    FORECAST_SCHEMA_VERSION,
    forecast_schema_version,
    metadata,
)


class UnsupportedForecastSchemaVersionError(RuntimeError):
    """Raised when a file is not the exact forecast schema supported by this build."""

    def __init__(
        self,
        actual: int | str,
        supported: int = FORECAST_SCHEMA_VERSION,
    ) -> None:
        self.actual = actual
        self.supported = supported
        super().__init__(
            f"Unsupported forecast schema version {actual}; this build supports {supported}"
        )


class ForecastDatabase:
    """A durable SQLite store containing only immutable shadow evidence."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self._preflight_schema()
        self.engine: Engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}")
        event.listen(self.engine, "connect", self._configure_connection)
        self._initialize_schema()

    def _preflight_schema(self) -> None:
        """Reject active, unversioned, future, or structurally changed databases."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return

        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA query_only=ON")
            table_names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            expected_tables = set(metadata.tables) | {"alembic_version"}
            if table_names != expected_tables:
                raise UnsupportedForecastSchemaVersionError("incompatible schema")
            if {"id", "version"} - {
                str(row[1])
                for row in connection.execute('PRAGMA table_info("forecast_schema_version")')
            }:
                raise UnsupportedForecastSchemaVersionError("incompatible schema")
            schema_rows = connection.execute(
                "SELECT version FROM forecast_schema_version"
            ).fetchall()
            if len(schema_rows) != 1:
                raise UnsupportedForecastSchemaVersionError(
                    "invalid forecast schema version marker"
                )
            try:
                actual = int(schema_rows[0][0])
            except (TypeError, ValueError) as exc:
                raise UnsupportedForecastSchemaVersionError(
                    "invalid forecast schema version marker"
                ) from exc
            if actual != FORECAST_SCHEMA_VERSION:
                raise UnsupportedForecastSchemaVersionError(actual)
            alembic_columns = {
                str(row[1]) for row in connection.execute('PRAGMA table_info("alembic_version")')
            }
            if "version_num" not in alembic_columns:
                raise UnsupportedForecastSchemaVersionError("invalid Alembic version marker")
            alembic_rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
            if alembic_rows != [("0001",)]:
                raise UnsupportedForecastSchemaVersionError("invalid Alembic version marker")
        finally:
            connection.close()

        self._validate_metadata_compatibility()

    def _validate_metadata_compatibility(self) -> None:
        preflight_engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}")
        try:
            with preflight_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                self._compare_against_metadata(connection)
        finally:
            preflight_engine.dispose()

    @staticmethod
    def _compare_against_metadata(connection: Connection) -> None:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        if compare_metadata(context, metadata):
            raise UnsupportedForecastSchemaVersionError("incompatible schema")

    def _configure_connection(
        self,
        connection: sqlite3.Connection,
        _connection_record: ConnectionPoolEntry,
    ) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            cursor.execute("PRAGMA foreign_keys=ON")
            journal_mode = cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            foreign_keys = cursor.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = cursor.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            cursor.close()

        if str(journal_mode).lower() != "wal":
            raise RuntimeError(f"SQLite WAL mode was not enabled: {journal_mode}")
        if foreign_keys != 1:
            raise RuntimeError("SQLite foreign key enforcement was not enabled")
        if busy_timeout != self.busy_timeout_ms:
            raise RuntimeError("SQLite busy timeout was not configured")

    def _initialize_schema(self) -> None:
        with self.engine.begin() as connection:
            command.upgrade(self._alembic_config(connection), "head")
            actual = connection.execute(select(forecast_schema_version.c.version)).scalar_one()
            if actual != FORECAST_SCHEMA_VERSION:
                raise UnsupportedForecastSchemaVersionError(actual)
            self._compare_against_metadata(connection)

    @staticmethod
    def _alembic_config(connection: Connection) -> Config:
        config = Config()
        migrations = Path(__file__).resolve().parent / "migrations"
        config.set_main_option("script_location", str(migrations))
        config.attributes["connection"] = connection
        return config

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Open a transaction that commits on success and rolls back on error."""
        with Session(self.engine, expire_on_commit=False) as session, session.begin():
            yield session

    def close(self) -> None:
        self.engine.dispose()

    def __enter__(self) -> ForecastDatabase:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
