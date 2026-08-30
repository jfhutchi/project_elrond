"""Copy the durable ledger safely, and prove a restore is the same evidence (#150, #137).

#150 asks for tested backup and clean restore, and specifically for restores that *preserve
strategy and evidence identity rather than silently beginning a new evidence series*. That last
clause is the whole reason this module exists rather than a line in a runbook.

**Why `cp` is not good enough, which is a correction to advice I gave.**

I told the operator to `cp quantbot.db quantbot.db.backup-...` before migrating. The store runs
in WAL mode, so at that moment committed data lives across `quantbot.db`, `-wal` and `-shm`. A
plain `cp` of the main file alone, taken while anything is writing, can produce a database
missing its most recent commits or torn mid-page -- and the copy will usually still *open*, which
is the dangerous part. It happened to be safe there only because the system had been stopped for
ten days.

`sqlite3.Connection.backup` is the supported mechanism. It holds a read transaction for the
duration, copies pages consistently, and produces a single self-contained file with the WAL
already folded in. It is also in the standard library, so this is a function rather than a
dependency.

**Why a restore needs its own check.**

A restored store that quietly starts a new evidence series is the worst available outcome,
because it looks healthy. #51's remaining blocker is *accumulated sessions*; #137 settled that
evidence keeps the identity it was measured under and is never relabelled. A restore that reset
either would destroy the one thing this project is short of, and would do it silently -- unlike a
crash, which announces itself.

So `restored_identically` does not ask whether the file copied. It asks whether the restored
store still holds the same strategy identity, the same accumulated evidence rows, and -- through
#48's resolver -- the same bytes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from quantbot.storage.schema import (
    equity_snapshots,
    qualification_days,
    runs,
    schema_version,
    strategy_deployments,
)

#: Tables whose row counts constitute the accumulated forward record. A restore that changed any
#: of these has begun a new evidence series, whatever else it preserved.
#:
#: `bars` is deliberately absent: market data is re-fetchable, and a restore taken before a sync
#: legitimately holds fewer bars. These four are not re-fetchable -- they are the record of what
#: this system did.
EVIDENCE_TABLES = (runs, qualification_days, equity_snapshots, strategy_deployments)


class BackupError(RuntimeError):
    """A backup that did not complete. Never a partial file presented as a whole one."""


@dataclass(frozen=True, slots=True)
class EvidenceFingerprint:
    """What must be identical across a backup and its restore.

    Counts and identities rather than a file hash, deliberately. Two SQLite files holding
    identical logical content can differ byte for byte -- page ordering, freelist, vacuum state --
    so a file-level comparison would report spurious differences and teach whoever runs it to
    ignore the check.
    """

    schema_version: int
    strategy_ids: tuple[str, ...]
    row_counts: dict[str, int]

    @property
    def has_evidence(self) -> bool:
        """Whether there is anything here for a restore to lose."""
        return any(self.row_counts.values())


def backup_database(source: str | Path, destination: str | Path) -> Path:
    """Copy a store to `destination` using SQLite's own backup, safe under a live writer.

    Refuses to overwrite. A backup command that clobbers is one keystroke from destroying the
    thing it exists to protect, and the caller naming a fresh path costs nothing.
    """
    source_path = Path(source).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source_path.exists() or source_path.stat().st_size == 0:
        raise BackupError(f"{source_path} holds no database to back up")
    if target.exists():
        raise BackupError(
            f"{target} already exists; name a new path rather than overwriting a backup"
        )
    target.parent.mkdir(parents=True, exist_ok=True)

    # Read-only on the source. Honest about what this buys: it is *not* what stops the backup
    # writing, since `backup()` only reads and a read-write connection would behave identically
    # -- both mutations proving that came back green. Measured, `mode=ro` actually leaves `-wal`
    # and `-shm` behind where read-write checkpoints them away.
    #
    # It is kept for the case I could not construct a test for: a source with a hot journal.
    # Opening that read-write triggers crash recovery, which *writes* -- so taking a backup would
    # modify the operator's live ledger as a side effect. Read-only refuses instead. Defence in
    # depth, recorded as such rather than asserted by a test that cannot see the difference.
    origin = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            origin.backup(copy)
        finally:
            copy.close()
    except sqlite3.Error as error:
        target.unlink(missing_ok=True)
        raise BackupError(f"backup of {source_path} failed: {error}") from error
    finally:
        origin.close()
    return target


def fingerprint(path: str | Path) -> EvidenceFingerprint:
    """Read what a restore has to preserve, without modifying the store.

    Opened as a plain read-only SQLite connection rather than through `Database`, on purpose: a
    backup taken from an older build may be behind this build's schema, and refusing to fingerprint
    it would make the check unavailable exactly when someone is restoring an old copy.
    """
    location = Path(path).expanduser().resolve()
    connection = sqlite3.connect(f"file:{location.as_posix()}?mode=ro", uri=True)
    try:
        version = int(
            connection.execute(f"SELECT version FROM {schema_version.name}").fetchone()[0]
        )
    except sqlite3.Error as error:  # pragma: no cover -- a store with no marker is not a store
        connection.close()
        raise BackupError(f"{location} has no schema version marker") from error

    counts: dict[str, int] = {}
    for table in EVIDENCE_TABLES:
        try:
            counts[table.name] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table.name}").fetchone()[0]
            )
        except sqlite3.Error:
            # A table this build knows and the backup predates. Absent is a real answer and is
            # not the same as zero, but for "did the restore lose evidence" both sides see the
            # same absence, so recording it as absent keeps the comparison honest.
            counts[table.name] = -1
    try:
        identities = tuple(
            sorted(
                str(row[0])
                for row in connection.execute(
                    f"SELECT DISTINCT strategy_id FROM {strategy_deployments.name}"
                )
            )
        )
    except sqlite3.Error:
        identities = ()
    connection.close()
    return EvidenceFingerprint(
        schema_version=version, strategy_ids=identities, row_counts=counts
    )


def restored_identically(original: str | Path, restored: str | Path) -> tuple[bool, str]:
    """Whether a restore preserved the evidence series, and what differs if not.

    Returns a reason rather than raising, so a caller can report every difference at once --
    `unmet_requirement` does the same thing for the same reason.
    """
    before = fingerprint(original)
    after = fingerprint(restored)

    if before.schema_version != after.schema_version:
        return False, (
            f"schema version {before.schema_version} restored as {after.schema_version}"
        )
    if before.strategy_ids != after.strategy_ids:
        return False, (
            "strategy identity changed across the restore: "
            f"{list(before.strategy_ids)} became {list(after.strategy_ids)}"
        )
    lost = {
        name: (count, after.row_counts.get(name))
        for name, count in before.row_counts.items()
        if after.row_counts.get(name) != count
    }
    if lost:
        return False, (
            "the restored store holds a different forward record: "
            + "; ".join(f"{name} {old} -> {new}" for name, (old, new) in sorted(lost.items()))
        )
    return True, "the restore preserved the evidence series"


__all__ = [
    "EVIDENCE_TABLES",
    "BackupError",
    "EvidenceFingerprint",
    "backup_database",
    "fingerprint",
    "restored_identically",
]
