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
from quantbot.storage.target import DatabaseTarget, coerce_target

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
    7: "0007",
    8: "0008",
    9: "0009",
    10: "0010",
    11: "0011",
    12: "0012",
    13: "0013",
    14: "0014",
    15: "0015",
    16: "0016",
    17: "0017",
    18: "0018",
    19: "0019",
    20: "0020",
    21: "0021",
    22: "0022",
    23: "0023",
    24: "0024",
    25: "0025",
    26: "0026",
    27: "0027",
    28: "0028",
    29: "0029",
    30: "0030",
    31: "0031",
}


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised when a database was written by an unsupported schema version."""

    def __init__(self, actual: int | str, supported: int = SCHEMA_VERSION) -> None:
        self.actual = actual
        self.supported = supported
        super().__init__(f"Unsupported schema version {actual}; this build supports {supported}")


class DatabaseNotFoundError(FileNotFoundError):
    """The configured store does not exist, and opening one is not authority to create it (#86).

    #66 stopped an ordinary open from *migrating* an existing store. This closes the other half
    of the same boundary: an ordinary open must not **bootstrap** a missing one either.

    The failure it prevents is quiet. A typo, a wrong working directory, a stale service
    definition or an unmounted path all resolve to "no file there", and a constructor that
    creates one turns every such mistake into a pristine empty database that reports itself as
    healthy. The operator sees a system with no positions rather than a system pointed at the
    wrong place, and a deployment begins accumulating real state somewhere nobody intended.

    A default-engaged kill switch makes that new store safe to trade from. It does not make it
    the *right* store, and those are different properties.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"no database at {path}; opening a store does not create one. If this path is "
            "correct and the system is being installed for the first time, run "
            "`quantbot create-database` to bootstrap it deliberately. Otherwise the configured "
            "target is wrong -- check QUANTBOT_DB_PATH or QUANTBOT_DATABASE_URL and the "
            "working directory"
        )


class MigrationRequiredError(RuntimeError):
    """An existing store is behind head and was opened through a path with no migration authority.

    Raised rather than upgraded (#66). Opening a database is how nearly everything in this
    project acquires a connection -- a CLI command, a diagnostic, an experimental checkout -- and
    if opening also migrates, then every one of those holds authority to rewrite operator state
    as a side effect of wanting to read it. The test suite exercised exactly that and upgraded a
    live trading store from an unmerged branch.
    """

    def __init__(self, path: Path, actual: int | None) -> None:
        self.path = path
        self.actual = actual
        super().__init__(
            f"{path} is at schema version {actual} and this build expects {SCHEMA_VERSION}; "
            "opening a store does not migrate it. Run `quantbot migrate-database` to upgrade it "
            "deliberately, after taking a backup"
        )


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

    def __init__(
        self,
        path: str | Path | DatabaseTarget,
        *,
        busy_timeout_ms: int = 5000,
        may_migrate: bool = False,
        may_create: bool = False,
    ) -> None:
        """Open an existing store. Creates nothing and upgrades nothing.

        Three operations, three names, and none of them reachable by accident:

        * `Database(path)` requires the store to exist at head. It raises
          `DatabaseNotFoundError` if the file is missing or empty (#86) and
          `MigrationRequiredError` if it is behind (#66).
        * `Database.create(path)` bootstraps a new store and refuses an existing one.
        * `Database.migrate(path)` upgrades an existing store and refuses a missing one.

        The earlier version of this docstring argued that creating a store where none existed
        "cannot damage operator state". That is true of the file and false of the operator: the
        damage is that a wrong path stops looking wrong. Both keyword flags are private plumbing
        for the two classmethods -- a caller that sets them by hand has named its intent, which
        is the property being protected.
        """
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        # A `DatabaseTarget` is accepted so callers can carry a backend-neutral location (#196),
        # and refused here if it names a backend this build cannot open. Checked before anything
        # touches the filesystem: a PostgreSQL URL must not silently become a SQLite file named
        # after it, which is #86's empty-healthy-store failure arriving through a new door.
        self.target = coerce_target(path)
        self.target.require_enabled()
        self.path = self.target.path.resolve()
        self.busy_timeout_ms = busy_timeout_ms
        existed = self.path.exists() and self.path.stat().st_size > 0
        if not existed and not may_create:
            # Before `mkdir` and before `create_engine`, so a failed open leaves the filesystem
            # exactly as it found it -- no file, and no parent directories either. A directory
            # tree conjured at a typo path is the same wrong signal in a quieter form.
            raise DatabaseNotFoundError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stamp_revision, self._pending_upgrade = self._preflight_schema()
        if existed and self._pending_upgrade and not may_migrate:
            # Raised *before* the engine is created, so nothing has touched the file beyond the
            # read-only preflight. Acquiring a connection must not be how a store gets rewritten.
            raise MigrationRequiredError(self.path, self._observed_version())
        self.engine: Engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}")
        event.listen(self.engine, "connect", self._configure_connection)
        self._initialize_schema()
        with self.engine.connect() as connection:
            self.store_identity = str(
                connection.execute(select(schema_version.c.store_identity)).scalar_one()
            )

    @classmethod
    @contextmanager
    def read_only(cls, path: str | Path | DatabaseTarget) -> Iterator[Session]:
        """Read a current store without initialization, repair, stamping or journal changes.

        SQLite enforces the read-only capability, including on callers' SQL. An explicit
        transaction gives all report queries the same snapshot while a writer is active.
        """
        target = coerce_target(path)
        target.require_enabled()
        location = target.path.resolve()
        if not location.is_file() or location.stat().st_size == 0:
            raise DatabaseNotFoundError(location)
        engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(location.as_uri() + "?mode=ro", uri=True),
        )
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                actual = connection.execute(select(schema_version.c.version)).scalar_one()
                if actual < SCHEMA_VERSION:
                    raise MigrationRequiredError(location, actual)
                if actual != SCHEMA_VERSION:
                    raise UnsupportedSchemaVersionError(actual)
                cls._compare_against_metadata(connection)
                with Session(bind=connection) as session:
                    yield session
        finally:
            engine.dispose()

    @classmethod
    def create(
        cls, path: str | Path | DatabaseTarget, *, busy_timeout_ms: int = 5000
    ) -> Database:
        """Create a new store at head, refusing to touch one that already exists."""
        target = coerce_target(path)
        target.require_enabled()
        location = target.path.resolve()
        if location.exists() and location.stat().st_size > 0:
            raise FileExistsError(
                f"{location} already holds a database; use Database.migrate to upgrade it, or "
                "Database(...) to open it"
            )
        return cls(
            location, busy_timeout_ms=busy_timeout_ms, may_migrate=True, may_create=True
        )

    @classmethod
    def migrate(
        cls, path: str | Path | DatabaseTarget, *, busy_timeout_ms: int = 5000
    ) -> Database:
        """Upgrade an existing store to head. The only path in this module that may rewrite one.

        Deliberately not reachable by opening: a caller has to name this to get the authority.
        """
        target = coerce_target(path)
        target.require_enabled()
        location = target.path.resolve()
        if not location.exists() or location.stat().st_size == 0:
            raise FileNotFoundError(
                f"{location} holds no database to migrate; use Database.create for a new one"
            )
        return cls(location, busy_timeout_ms=busy_timeout_ms, may_migrate=True)

    def _observed_version(self) -> int | None:
        """The version marker as it stands on disk, read without modifying anything."""
        try:
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            row = connection.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        try:
            return int(row[0]) if row else None
        except (TypeError, ValueError):
            return None

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
                str(row[1]) for row in connection.execute('PRAGMA table_info("alembic_version")')
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
                    str(row[1]) for row in connection.execute('PRAGMA table_info("schema_version")')
                }:
                    raise UnsupportedSchemaVersionError("incompatible schema")
                schema_rows = connection.execute("SELECT version FROM schema_version").fetchall()
                if len(schema_rows) != 1:
                    raise UnsupportedSchemaVersionError("invalid schema version marker")
                try:
                    actual = int(schema_rows[0][0])
                except (TypeError, ValueError) as exc:
                    raise UnsupportedSchemaVersionError("invalid schema version marker") from exc
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
        """Open a transaction that commits on success and rolls back on error.

        Managed by hand rather than through `session.begin()`, and the difference is one
        capability: a caller may commit *inside* the block and keep using the session. Under
        `with session.begin()` the first statement after an inner `commit()` raises
        `InvalidRequestError: Can't operate on closed transaction inside context manager`, so
        no work in the middle of a block could ever be made durable.

        `ExperimentRunner` needs exactly that (#159 Finding 10). It consumes a protected window
        and then hands the data to a measurement, and while the consumption is only part of the
        caller's transaction, *any* later exception -- a validation error, a failed manifest
        write, a plain bug -- unwinds it and erases the record that the holdout was ever read.
        A durable boundary at the handoff is the only thing that makes "this data has been seen"
        survive the ways a run can go wrong afterwards.

        Behaviour for every existing caller is unchanged: commit on clean exit, roll back on
        exception. `BaseException` is caught rather than `Exception` so a `KeyboardInterrupt` or
        a cancellation rolls back rather than leaving the transaction dangling.
        """
        with Session(self.engine, expire_on_commit=False) as session:
            try:
                yield session
            except BaseException:
                session.rollback()
                raise
            session.commit()

    def close(self) -> None:
        """Dispose pooled connections while leaving the durable file intact."""
        self.engine.dispose()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()
