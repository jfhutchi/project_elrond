"""Backup and clean restore, and the evidence series surviving both (#150, #137).

#150's criterion is not "the file copied". It is *restores that preserve strategy and evidence
identity rather than silently beginning a new evidence series*, and that is the clause worth
testing, because a restore that resets the forward record looks healthy afterwards.

It matters concretely rather than in principle: #51's only remaining blocker is accumulated
sessions, and an unnoticed identity reset would destroy the one thing this project is short of.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.domain import Bar, StrategyIdentity
from quantbot.research.dataset_snapshots import (
    Resolution,
    resolve_snapshot,
    snapshot_bars,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.backup import (
    BackupError,
    backup_database,
    fingerprint,
    restored_identically,
)

NOW = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
PROVIDER = "alpaca:iex:all:1day"


def identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_id="adaptive-momentum-v1-abc123",
        version="1.2.0",
        git_commit="a" * 40,
        configuration_hash="c" * 16,
        deployment_timestamp=NOW,
    )


def bar(day: int) -> Bar:
    return Bar(
        symbol="AAPL",
        timestamp=datetime(2026, 8, day, 20, 0, tzinfo=UTC),
        open="99",
        high="101",
        low="98",
        close="100",
        volume="1000",
        adjustment="1",
    )


@pytest.fixture
def populated(tmp_path: Path) -> Path:
    """A store carrying a forward record: a deployment, runs, and bars."""
    path = tmp_path / "live.db"
    with Database.create(path) as database:
        with database.transaction() as session:
            repository = StorageRepository(session)
            repository.create_run("run-1", identity(), started_at=NOW)
            repository.save_bars([bar(3), bar(4), bar(5)], provider=PROVIDER)
    return path


def test_a_backup_preserves_the_evidence_series(populated: Path, tmp_path: Path) -> None:
    """The criterion, asserted directly.

    Not "the file exists" and not "it opens" -- the strategy identity, the schema version and
    every non-refetchable row count have to match. A restore that quietly reset any of them would
    still open, still look healthy, and would have begun a new evidence series.
    """
    copy = backup_database(populated, tmp_path / "backup.db")

    identical, reason = restored_identically(populated, copy)

    assert identical, reason
    assert "preserved the evidence series" in reason


def test_the_restored_store_holds_the_same_bytes(populated: Path, tmp_path: Path) -> None:
    """Proved through #48's resolver rather than by comparing files.

    Two SQLite files with identical logical content can differ byte for byte -- page ordering,
    freelist, vacuum state -- so a file hash would report spurious differences and teach whoever
    runs it to ignore the check. `resolve_snapshot` asks the question that actually matters: does
    the restored store still contain the exact data a manifest would name.
    """
    with Database(populated) as database, database.transaction() as session:
        snapshot = snapshot_bars(
            session,
            dataset="AAPL",
            provider=PROVIDER,
            start=bar(3).timestamp.date(),
            end=bar(5).timestamp.date(),
            role="DISCOVERY",
            snapshot="vintage-1",
        )

    copy = backup_database(populated, tmp_path / "backup.db")

    with Database(copy) as restored, restored.transaction() as session:
        result = resolve_snapshot(session, snapshot)

    assert result.reproduced, result.explain()
    assert result.resolution is Resolution.EXACT


def test_a_fresh_store_is_not_a_restore(populated: Path, tmp_path: Path) -> None:
    """The failure this whole module exists to make visible.

    A brand-new store opens cleanly, reports a healthy schema version, and has lost every run,
    every qualification day and every deployment. It is the most plausible way an evidence series
    silently restarts -- somebody points the system at the wrong path, or bootstraps rather than
    restores -- and it must be reported as a difference rather than as a working system.
    """
    empty = tmp_path / "fresh.db"
    with Database.create(empty):
        pass

    identical, reason = restored_identically(populated, empty)

    assert not identical
    # The identity loss is reported first, which is the right order: a store with no deployment
    # cannot be the same evidence series whatever its row counts say.
    assert "strategy identity changed" in reason
    # And the forward record is gone too, which is the part that would never come back.
    assert fingerprint(populated).row_counts["runs"] == 1
    assert fingerprint(empty).row_counts["runs"] == 0


def test_a_restore_that_lost_only_the_strategy_identity_is_caught(
    populated: Path, tmp_path: Path
) -> None:
    """Row counts alone are not enough.

    A store with the same number of rows under a different strategy id is a different experiment
    wearing the same shape, and #137's rule is that evidence keeps the identity it was measured
    under. Deleting a deployment would change a count; *renaming* one would not.
    """
    copy = backup_database(populated, tmp_path / "backup.db")
    connection = sqlite3.connect(copy)
    connection.execute("UPDATE strategy_deployments SET strategy_id = 'something-else-v1'")
    connection.commit()
    connection.close()

    identical, reason = restored_identically(populated, copy)

    assert not identical
    assert "strategy identity changed" in reason


def test_a_backup_refuses_to_overwrite(populated: Path, tmp_path: Path) -> None:
    """A backup command that clobbers is one keystroke from destroying what it protects."""
    destination = tmp_path / "backup.db"
    backup_database(populated, destination)

    with pytest.raises(BackupError, match="already exists"):
        backup_database(populated, destination)


def test_a_backup_succeeds_from_a_read_only_file(populated: Path, tmp_path: Path) -> None:
    """A protected source can still be backed up -- a real scenario, and all this proves.

    Worth being explicit that this does **not** demonstrate the `mode=ro` connection, because two
    attempts to make it do so both failed. Asserting the source's mtime was unchanged came back
    green under a read-write connection, since a backup only reads either way. Marking the file
    read-only came back green too, because SQLite does not refuse the open until something
    writes, and nothing does.

    Measuring it directly was more surprising still: the read-only open *leaves* `-wal` and `-shm`
    sidecars behind, while the read-write open checkpoints and removes them. So the observable
    effect of `mode=ro` here is slightly worse, not better.

    It is kept anyway, and the module docstring says why: it prevents a hot-journal recovery
    write on open, which is a real failure mode for a backup to have and one I could not
    construct a test for. That makes it defence-in-depth rather than a tested guarantee, and
    saying so is better than a test that implies otherwise.
    """
    import os
    import stat

    os.chmod(populated, stat.S_IREAD)
    try:
        written = backup_database(populated, tmp_path / "backup.db")
        assert fingerprint(written).row_counts["runs"] == 1
    finally:
        os.chmod(populated, stat.S_IREAD | stat.S_IWRITE)


def test_a_restore_missing_rows_under_the_same_identity_is_caught(
    populated: Path, tmp_path: Path
) -> None:
    """Identity alone is not enough either.

    A partial restore keeps the deployment and loses runs -- the schema version matches, the
    strategy id matches, and the forward record is shorter. That is a new evidence series wearing
    the old name, and it is exactly what #51's session count would silently lose.
    """
    copy = backup_database(populated, tmp_path / "backup.db")
    connection = sqlite3.connect(copy)
    connection.execute("DELETE FROM runs")
    connection.commit()
    connection.close()

    identical, reason = restored_identically(populated, copy)

    assert not identical
    assert "different forward record" in reason
    assert "runs 1 -> 0" in reason


def test_a_restore_at_a_different_schema_version_is_caught(
    populated: Path, tmp_path: Path
) -> None:
    """A backup restored against a build that has moved on is not the same store.

    Reported before identity and row counts, because the counts are not comparable across a
    schema change and saying "the runs table shrank" would be the wrong explanation.
    """
    copy = backup_database(populated, tmp_path / "backup.db")
    connection = sqlite3.connect(copy)
    connection.execute("UPDATE schema_version SET version = version - 1")
    connection.commit()
    connection.close()

    identical, reason = restored_identically(populated, copy)

    assert not identical
    assert "schema version" in reason


def test_backing_up_nothing_is_refused(tmp_path: Path) -> None:
    """An empty or missing file must not produce a backup that looks real."""
    missing = tmp_path / "nope.db"

    with pytest.raises(BackupError, match="no database to back up"):
        backup_database(missing, tmp_path / "backup.db")

    missing.touch()
    with pytest.raises(BackupError, match="no database to back up"):
        backup_database(missing, tmp_path / "backup2.db")


def test_the_cli_reports_what_the_backup_carries(populated: Path, tmp_path: Path) -> None:
    """"The file copied" is not the property that matters.

    An operator taking a backup before a migration should be able to see the identity and the
    forward record they are protecting, so a later restore can be checked against it.
    """
    import io

    from quantbot.cli import main

    stream = io.StringIO()
    code = main(
        [
            "backup-database",
            "--database",
            str(populated),
            "--to",
            str(tmp_path / "cli-backup.db"),
        ],
        output=stream,
    )

    payload = json.loads(stream.getvalue())
    assert code == 0
    assert payload["ok"] is True
    assert payload["strategy_ids"] == ["adaptive-momentum-v1-abc123"]
    assert payload["evidence_rows"]["runs"] == 1
    assert payload["bytes"] > 0


def test_the_cli_refuses_rather_than_overwriting(populated: Path, tmp_path: Path) -> None:
    import io

    from quantbot.cli import main

    destination = tmp_path / "cli-backup.db"
    backup_database(populated, destination)

    stream = io.StringIO()
    code = main(
        ["backup-database", "--database", str(populated), "--to", str(destination)],
        output=stream,
    )

    assert code == 1
    assert "already exists" in json.loads(stream.getvalue())["error"]
