"""Opening a database must not carry the authority to rewrite it (#66).

#65 stopped the *test suite* from pointing at the operator's store. It did not remove the
capability that made the incident possible: `Database.__init__` ran Alembic to head, so any
caller that obtained a connection could upgrade whatever it opened -- a diagnostic, a read-only
CLI command, an experimental checkout with an unmerged migration.

The property under test is the one Sol stated: acquiring read access must not silently grant
mutation authority. These tests are written against the *bytes on disk*, not against the return
value, because "it did not migrate" is a claim about the file rather than about the object.
"""

from __future__ import annotations

import hashlib
import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from quantbot.storage import (
    Database,
    DatabaseNotFoundError,
    MigrationRequiredError,
    UnsupportedSchemaVersionError,
)
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import SCHEMA_VERSION, kill_switch_state, metadata, schema_version
from tests.unit.test_storage import NOW


def fingerprint(path: Path) -> tuple[str, int]:
    """Content hash and size. A migration cannot happen without changing one of them."""
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def behind_head(path: Path, *, version: int = 1) -> None:
    """A store shaped like a deployment left on an older revision."""
    from importlib import import_module

    v1 = import_module("quantbot.storage.migrations.versions.0001_initial_schema")
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        metadata.create_all(connection, tables=[metadata.tables[n] for n in v1.V1_TABLES])
        connection.execute(schema_version.insert().values(id=1, version=version))
        connection.execute(
            kill_switch_state.insert().values(
                id=1,
                engaged=True,
                reason="engaged before anyone opened it",
                updated_at=encode_utc(NOW),
            )
        )
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('0001')")
    engine.dispose()


def marker(path: Path) -> tuple[int | None, str | None]:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        version = connection.execute("SELECT version FROM schema_version").fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()
    return (int(version[0]) if version else None, str(revision[0]) if revision else None)


def test_opening_a_store_that_is_behind_head_changes_nothing_on_disk(tmp_path: Path) -> None:
    """Acceptance criterion 1, and the whole point of the issue.

    This is the incident in miniature: a checkout whose migrations run past what the file has
    been given, pointed at that file through the ordinary open path.
    """
    path = tmp_path / "deployment.db"
    behind_head(path)
    before = fingerprint(path)
    assert marker(path) == (1, "0001")

    with pytest.raises(MigrationRequiredError) as refused:
        Database(path)

    assert fingerprint(path) == before, "opening the store rewrote it"
    assert marker(path) == (1, "0001"), "the revision moved without anyone asking"
    # The refusal has to be actionable, or the next person works around it.
    assert "does not migrate" in str(refused.value)
    assert "migrate-database" in str(refused.value)


def test_the_refusal_names_the_version_it_found(tmp_path: Path) -> None:
    """An error that cannot say what it found sends the reader to guess."""
    path = tmp_path / "deployment.db"
    behind_head(path)

    with pytest.raises(MigrationRequiredError) as refused:
        Database(path)

    assert refused.value.actual == 1
    assert refused.value.path == path.resolve()


def test_an_explicit_migration_upgrades_and_verifies(tmp_path: Path) -> None:
    """Acceptance criterion 2. The authority still exists -- it just has to be asked for."""
    path = tmp_path / "deployment.db"
    behind_head(path)

    database = Database.migrate(path)
    try:
        assert marker(path)[0] == SCHEMA_VERSION
    finally:
        database.close()

    # And the operational state it already carried survived the upgrade.
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        engaged, reason = connection.execute(
            "SELECT engaged, reason FROM kill_switch_state"
        ).fetchone()
    finally:
        connection.close()
    assert bool(engaged) is True
    assert reason == "engaged before anyone opened it"


def test_a_store_ahead_of_this_build_stays_fail_closed_and_untouched(tmp_path: Path) -> None:
    """Acceptance criterion 3, and the state the operator's database is actually in.

    A file from a newer checkout must not be opened *or* "repaired" downward.
    """
    path = tmp_path / "ahead.db"
    behind_head(path, version=SCHEMA_VERSION + 1)
    before = fingerprint(path)

    with pytest.raises(UnsupportedSchemaVersionError):
        Database(path)
    with pytest.raises(UnsupportedSchemaVersionError):
        Database.migrate(path)

    assert fingerprint(path) == before, "a newer store was modified by a build that cannot read it"


def test_creating_a_store_is_explicit_and_refuses_an_existing_one(tmp_path: Path) -> None:
    """Acceptance criterion 4. `create` must not become a way to migrate by another name."""
    fresh = tmp_path / "new.db"
    database = Database.create(fresh)
    try:
        assert marker(fresh)[0] == SCHEMA_VERSION
    finally:
        database.close()

    existing = tmp_path / "deployment.db"
    behind_head(existing)
    before = fingerprint(existing)
    with pytest.raises(FileExistsError):
        Database.create(existing)
    assert fingerprint(existing) == before


def test_migrate_refuses_a_store_that_is_not_there(tmp_path: Path) -> None:
    """The mirror of the above: `migrate` must not quietly become `create`.

    Otherwise a typo in a path silently produces an empty store and the operator's real one is
    reported as successfully migrated.
    """
    with pytest.raises(FileNotFoundError):
        Database.migrate(tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()


def test_opening_a_store_already_at_head_still_works(tmp_path: Path) -> None:
    """The guard must not have been bought by making ordinary use fail.

    If this broke, every reader would be pushed toward the migrating path to get their work done,
    which is how the authority leaks back in.
    """
    path = tmp_path / "current.db"
    Database.create(path).close()
    before = fingerprint(path)

    database = Database(path)
    try:
        with database.transaction():
            pass
    finally:
        database.close()

    assert marker(path)[0] == SCHEMA_VERSION
    assert fingerprint(path) == before, "opening an at-head store rewrote it"


def test_the_cli_reports_before_it_migrates_and_refuses_without_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 5, and the command the refusal message promises.

    An upgrade is not reversible, so the default has to be a report. The first run says what
    would happen and changes nothing; only `--confirm` carries the authority.
    """
    import io
    import json

    from quantbot.cli import main

    path = tmp_path / "deployment.db"
    behind_head(path)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(path))
    before = fingerprint(path)

    output = io.StringIO()
    code = main(["migrate-database"], output=output)
    reported = json.loads(output.getvalue())

    assert code == 2
    assert reported["ok"] is False
    assert reported["migrated"] is False
    assert reported["current_schema_version"] == 1
    assert reported["target_schema_version"] == SCHEMA_VERSION
    assert fingerprint(path) == before, "a report changed the store"

    output = io.StringIO()
    code = main(["migrate-database", "--confirm"], output=output)
    performed = json.loads(output.getvalue())

    assert code == 0
    assert performed["migrated"] is True
    assert marker(path)[0] == SCHEMA_VERSION


def test_an_ordinary_cli_command_does_not_migrate_the_store_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident, at the layer it actually happened.

    `quantbot.cli.main` builds a `Database` for every command from the ambient path. Before #66
    that construction upgraded whatever it found, so a command that only reports state could
    rewrite a deployment's schema. It must now fail instead, leaving the file alone.
    """
    import io

    from quantbot.cli import main

    path = tmp_path / "deployment.db"
    behind_head(path)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(path))
    before = fingerprint(path)

    with pytest.raises(MigrationRequiredError):
        main(["status"], output=io.StringIO())

    assert fingerprint(path) == before, "a status command migrated the database"
    assert marker(path) == (1, "0001")


# --- #86: opening must not create, either -------------------------------------------------------
#
# #66 closed one half of the storage lifecycle boundary: an ordinary open cannot *migrate*. It
# left the other half open, and the acceptance criterion it claimed to satisfy was not actually
# satisfied -- `Database(path)` against a missing file still created a store at head, complete
# with its default kill-switch row.
#
# The hazard is quieter than the migration one and reaches further. A typo, a wrong working
# directory, a stale service definition or an unmounted volume all present as "no file there".
# A constructor that creates one converts every such mistake into a pristine, healthy-looking,
# entirely wrong database -- and the operator sees a system with no positions rather than a
# system pointed somewhere it should not be. These tests are written against the filesystem for
# the same reason the ones above are: "it did not bootstrap" is a claim about the directory.


def test_opening_a_missing_store_refuses_and_leaves_the_filesystem_alone(tmp_path: Path) -> None:
    """Acceptance criteria 1 and 2. Nothing is created -- not the file, not its parents."""
    missing = tmp_path / "nested" / "deeper" / "quantbot.db"

    with pytest.raises(DatabaseNotFoundError) as refusal:
        Database(missing)

    assert not missing.exists()
    assert not missing.parent.exists(), "an ordinary open created directories on the way to a file"
    assert not (tmp_path / "nested").exists()
    assert list(tmp_path.iterdir()) == []
    # The refusal has to be actionable, because the next question is always "then where is my
    # database?" and the answer is usually visible in the resolved path.
    assert str(missing.resolve()) in str(refusal.value)
    assert "QUANTBOT_DB_PATH" in str(refusal.value)


def test_a_zero_length_placeholder_is_not_a_store_to_bootstrap(tmp_path: Path) -> None:
    """Acceptance criterion 3. An empty file is the shape a half-failed deployment leaves."""
    placeholder = tmp_path / "quantbot.db"
    placeholder.touch()

    with pytest.raises(DatabaseNotFoundError):
        Database(placeholder)

    assert placeholder.stat().st_size == 0, "the refused open wrote a schema into the placeholder"


def test_create_bootstraps_the_whole_store_including_the_engaged_kill_switch(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 4. Bootstrap authority lives in exactly one named place."""
    fresh = tmp_path / "first-run" / "quantbot.db"

    database = Database.create(fresh)
    try:
        assert fresh.exists()
        version, revision = marker(fresh)
        assert version == SCHEMA_VERSION
        assert revision is not None, "a created store must carry an Alembic revision marker"
        with database.engine.connect() as connection:
            state = connection.execute(kill_switch_state.select()).fetchone()
        assert state is not None and bool(state.engaged) is True
    finally:
        database.close()

    # And it is now an ordinary store: opening it is allowed, creating it again is not.
    Database(fresh).close()
    with pytest.raises(FileExistsError):
        Database.create(fresh)


def test_a_read_only_cli_pointed_at_a_typo_path_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 7, and the failure this issue is actually about.

    `status` is the command an operator runs to find out whether anything is wrong. Before this,
    running it against a mistyped path answered that question by creating a database in which
    nothing was wrong.
    """
    from quantbot.cli import main

    real = tmp_path / "quantbot.db"
    Database.create(real).close()
    typo = tmp_path / "quantbo.db"
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(typo))

    output = StringIO()
    with pytest.raises(DatabaseNotFoundError):
        main(["status"], output=output)

    assert not typo.exists(), "a read-only command bootstrapped a store at the wrong path"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["quantbot.db"]


def test_the_bootstrap_command_refuses_to_run_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bootstrap that happens because a path was mistyped is the thing being prevented.

    So the command holding the authority reports what it would do and stops, and the path it
    echoes is the *resolved* one -- the mistake is usually a relative path landing somewhere
    unexpected, which an unresolved argument would hide.
    """
    import json

    from quantbot.cli import main

    target = tmp_path / "quantbot.db"
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(target))

    output = StringIO()
    assert main(["create-database"], output=output) == 2
    payload = json.loads(output.getvalue())
    assert payload["ok"] is False and payload["created"] is False
    assert str(target.resolve()) in payload["error"]
    assert not target.exists()

    confirmed = StringIO()
    assert main(["create-database", "--confirm"], output=confirmed) == 0
    assert json.loads(confirmed.getvalue())["created"] is True
    assert marker(target)[0] == SCHEMA_VERSION

    # A second run refuses rather than touching what it just made.
    again = StringIO()
    assert main(["create-database", "--confirm"], output=again) == 2
    assert json.loads(again.getvalue())["created"] is False


def test_the_runtime_cannot_start_against_a_store_it_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance criterion 8. The runtime is the caller with the most to lose here.

    A daemon that starts against a freshly created store has no positions, no fills and no
    reconciliation history, and every one of those absences reads as a clean system rather than
    as a system that cannot see its own state.
    """
    from quantbot.runtime import build_runtime

    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "key")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "secret")

    with pytest.raises(DatabaseNotFoundError):
        build_runtime()

    assert not (tmp_path / "quantbot.db").exists()
