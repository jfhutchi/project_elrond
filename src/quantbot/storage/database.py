"""SQLite engine lifecycle and explicit transaction boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from sqlalchemy.pool import ConnectionPoolEntry

from quantbot.storage.schema import (
    SCHEMA_VERSION,
    kill_switch_state,
    metadata,
    schema_version,
)

#: The Alembic revision whose upgrade produces each schema version. Databases written before
#: Alembic was introduced carry only the `schema_version` marker, so they are stamped at the
#: revision matching their observed layout and then upgraded normally. Extend when
#: `SCHEMA_VERSION` is bumped; a version absent here cannot be opened.
REVISION_FOR_SCHEMA_VERSION: dict[int, str] = {
    1: "0001",
    2: "0002",
    3: "0003",
    4: "0004",
    5: "0005",
    6: "0006",
}


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
        self._stamp_revision, self._pending_upgrade = self._preflight_schema()
        self.engine: Engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}")
        event.listen(self.engine, "connect", self._configure_connection)
        self._initialize_schema()

    def _preflight_schema(self) -> tuple[str | None, bool]:
        """Validate an existing file without changing persistent SQLite settings.

        Returns the revision to stamp a pre-Alembic file at (or `None`), and whether the file
        is behind head and therefore still has migrations to run.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None, False

        stamp_revision: str | None = None
        observed_version = SCHEMA_VERSION
        validate_metadata = False
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
            has_schema_marker = schema_version.name in table_names
            has_alembic_table = "alembic_version" in table_names
            if has_alembic_table and "version_num" not in {
                str(row[1])
                for row in connection.execute('PRAGMA table_info("alembic_version")')
            }:
                raise UnsupportedSchemaVersionError("invalid Alembic version marker")
            alembic_rows = (
                connection.execute("SELECT version_num FROM alembic_version").fetchall()
                if has_alembic_table
                else []
            )
            if len(alembic_rows) > 1:
                raise UnsupportedSchemaVersionError("invalid Alembic version marker")
            alembic_revision = str(alembic_rows[0][0]) if alembic_rows else None
            user_tables = table_names - {"alembic_version"}

            if user_tables and not has_schema_marker and alembic_revision is None:
                raise UnsupportedSchemaVersionError("unversioned nonempty database")
            if alembic_revision is not None and not has_schema_marker:
                raise UnsupportedSchemaVersionError("incomplete versioned database")

            if has_schema_marker:
                if {"id", "version"} - {
                    str(row[1])
                    for row in connection.execute('PRAGMA table_info("schema_version")')
                }:
                    raise UnsupportedSchemaVersionError("incompatible schema")
                schema_rows = connection.execute("SELECT version FROM schema_version").fetchall()
                if len(schema_rows) != 1:
                    raise UnsupportedSchemaVersionError("invalid schema version marker")
                try:
                    actual = int(schema_rows[0][0])
                except (TypeError, ValueError) as exc:
                    raise UnsupportedSchemaVersionError(
                        "invalid schema version marker"
                    ) from exc
                if actual > SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(actual)
                observed_version = actual
                if actual == SCHEMA_VERSION:
                    if not set(metadata.tables) <= table_names:
                        raise UnsupportedSchemaVersionError("incomplete versioned database")
                    validate_metadata = True
                # An older marker is upgraded by Alembic below rather than rejected. It is not
                # compared against head metadata here: the difference is exactly what the
                # migration exists to close. The comparison runs after the upgrade instead.

            if has_schema_marker and alembic_revision is None:
                stamp_revision = REVISION_FOR_SCHEMA_VERSION.get(observed_version)
                if stamp_revision is None:
                    raise UnsupportedSchemaVersionError(observed_version)
        finally:
            connection.close()

        if validate_metadata:
            self._validate_metadata_compatibility()
        return stamp_revision, observed_version < SCHEMA_VERSION

    def _validate_metadata_compatibility(self) -> None:
        """Require a zero-diff schema using a read-only, non-operational engine."""
        preflight_engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}")
        try:
            with preflight_engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA query_only=ON")
                self._compare_against_metadata(connection)
        finally:
            preflight_engine.dispose()

    @staticmethod
    def _compare_against_metadata(connection: Connection) -> None:
        """Fail unless the live schema matches the metadata this build was compiled against."""
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "compare_server_default": True},
        )
        if compare_metadata(context, metadata):
            raise UnsupportedSchemaVersionError("incompatible schema")

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
            config = self._alembic_config(connection)
            if self._stamp_revision is not None:
                command.stamp(config, self._stamp_revision)
            command.upgrade(config, "head")

            actual = connection.execute(select(schema_version.c.version)).scalar_one()
            if actual != SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(actual)
            if self._pending_upgrade:
                # Preflight skipped the comparison because the file was behind head. Check it
                # here, inside the transaction, so a migration that fails to reproduce head
                # metadata rolls back instead of leaving a half-shaped store on disk.
                self._compare_against_metadata(connection)

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
