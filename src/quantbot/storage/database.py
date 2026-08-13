"""SQLite engine lifecycle and explicit transaction boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, event, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from quantbot.storage.schema import (
    SCHEMA_VERSION,
    kill_switch_state,
    metadata,
    schema_version,
)


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised when a database was written by an unsupported schema version."""

    def __init__(self, actual: int | str, supported: int = SCHEMA_VERSION) -> None:
        self.actual = actual
        self.supported = supported
        super().__init__(f"Unsupported schema version {actual}; this build supports {supported}")


def encode_decimal(value: Decimal) -> str:
    """Encode a finite Decimal as a unique, non-exponent SQLite string."""
    if not value.is_finite():
        raise ValueError("decimal must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def decode_decimal(value: str) -> Decimal:
    """Decode a finite Decimal string without passing through binary floats."""
    try:
        decoded = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("encoded decimal is invalid") from exc
    if not decoded.is_finite():
        raise ValueError("decimal must be finite")
    return decoded


def encode_utc(value: datetime) -> str:
    """Encode an aware datetime as a canonical UTC ISO-8601 string."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    encoded = value.astimezone(UTC).isoformat()
    return encoded.removesuffix("+00:00") + "Z"


def decode_utc(value: str) -> datetime:
    """Decode an ISO-8601 timestamp and normalize it to UTC."""
    decoded = datetime.fromisoformat(
        value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
    )
    if decoded.tzinfo is None or decoded.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return decoded.astimezone(UTC)


class Database:
    """A durable SQLite database with fail-fast schema and pragma checks."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = busy_timeout_ms
        self.engine: Engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}")
        event.listen(self.engine, "connect", self._configure_connection)
        self._initialize_schema()

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
            table_names = set(inspect(connection).get_table_names())
            has_schema_marker = schema_version.name in table_names
            has_alembic_table = "alembic_version" in table_names
            alembic_revision = (
                MigrationContext.configure(connection).get_current_revision()
                if has_alembic_table
                else None
            )
            user_tables = table_names - {"alembic_version"}

            if user_tables and not has_schema_marker and alembic_revision is None:
                raise UnsupportedSchemaVersionError("unversioned nonempty database")

            if has_schema_marker:
                actual = connection.execute(select(schema_version.c.version)).scalar_one()
                if actual != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(actual)

            config = self._alembic_config(connection)
            if has_schema_marker and alembic_revision is None:
                expected_tables = set(metadata.tables)
                if not expected_tables <= table_names:
                    raise UnsupportedSchemaVersionError("incomplete versioned database")
                command.stamp(config, "head")
            command.upgrade(config, "head")

            actual = connection.execute(select(schema_version.c.version)).scalar_one()
            if actual != SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(actual)

            if connection.execute(select(kill_switch_state.c.id)).scalar_one_or_none() is None:
                connection.execute(
                    kill_switch_state.insert().values(
                        id=1,
                        engaged=True,
                        reason="default fail-closed state",
                        updated_at=encode_utc(datetime.now(UTC)),
                    )
                )

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
        """Dispose pooled connections while leaving the durable file intact."""
        self.engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
