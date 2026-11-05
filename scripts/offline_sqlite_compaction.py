"""Verify/compact a CLOSED offline SQLite backup without deleting any history.

Never pass the operating database. Obtain a consistent SQLite backup first, stop
all users of that backup, and retain it for rollback. This tool does not install
the candidate, stop writers, change authority, or prune receipts/projections.
Run with the project environment: python -m scripts.offline_sqlite_compaction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from itertools import zip_longest
from pathlib import Path


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def standalone(path: Path) -> Path:
    path = path.resolve(strict=True)
    if not path.is_file() or any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("closed standalone offline backup required (no sidecars)")
    return path


def connect(path: Path) -> sqlite3.Connection:
    # immutable is safe ONLY because the caller explicitly supplies a closed copy.
    # It prevents creating even WAL/shared-memory sidecars on the source.
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def check(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign key check failed")


def projection(connection: sqlite3.Connection) -> dict:
    from quantbot.paper_core.contracts import GuardianReceipt

    expected, receipts = 0, 0
    for identity, payload in connection.execute("SELECT id,payload FROM core_guardian_receipts"):
        receipt = GuardianReceipt.model_validate_json(payload)
        if receipt.id != identity:
            raise ValueError("guardian receipt identity does not match immutable payload")
        plans = set(receipt.protection_ids)
        if len(plans) != len(receipt.protection_ids):
            raise ValueError("duplicate guardian membership in receipt")
        for plan in plans:
            if (
                connection.execute(
                    "SELECT 1 FROM core_guardian_memberships WHERE plan_id=? AND receipt_id=?",
                    (plan, identity),
                ).fetchone()
                is None
            ):
                raise ValueError("incomplete guardian membership projection")
        expected += len(plans)
        receipts += 1
    actual = connection.execute("SELECT count(*) FROM core_guardian_memberships").fetchone()[0]
    if actual != expected:
        raise ValueError("unexpected guardian membership projection rows")
    return {"receipts": receipts, "memberships": actual, "complete": True}


def verify_equivalent(source: Path, restored: Path) -> dict:
    """Exact ordered row equality, including all immutable payloads and identities."""
    with closing(connect(standalone(source))) as old, closing(connect(standalone(restored))) as new:
        check(old)
        check(new)
        schema_sql = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        schema = old.execute(schema_sql).fetchall()
        if schema != new.execute(schema_sql).fetchall():
            raise ValueError("schema changed")
        result = {}
        for kind, name, _, _ in schema:
            if kind != "table":
                continue
            columns = old.execute("PRAGMA table_info(" + quote(name) + ")").fetchall()
            primary = sorted((column[5], column[1]) for column in columns if column[5])
            ordering = [name for _, name in primary] or [column[1] for column in columns]
            query = "SELECT * FROM " + quote(name) + " ORDER BY " + ",".join(map(quote, ordering))
            digest, count = hashlib.sha256(), 0
            sentinel = object()
            pairs = zip_longest(old.execute(query), new.execute(query), fillvalue=sentinel)
            for left, right in pairs:
                # repr distinguishes SQLite scalar types, unlike Python's 1 == 1.0.
                if left is sentinel or right is sentinel or repr(left) != repr(right):
                    raise ValueError("restored table differs: " + name)
                encoded = repr(left).encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big") + encoded)
                count += 1
            result[name] = {"rows": count, "sha256": digest.hexdigest()}
        return result


def inspect_copy(source: Path, *, seconds: float = 30) -> dict:
    """Bound page attribution on the offline copy; interrupted scans are not totals."""
    with closing(connect(standalone(source))) as connection:
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        free = connection.execute("PRAGMA freelist_count").fetchone()[0]
        deadline = time.monotonic() + seconds
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        result = {
            "bytes": pages * page_size,
            "free_page_bytes": free * page_size,
            "additional_compaction_space_budget": 2 * pages * page_size,
            "dbstat_complete": False,
        }
        try:
            result["objects"] = [
                dict(zip(("name", "bytes", "unused_bytes"), row, strict=True))
                for row in connection.execute(
                    "SELECT name,sum(pgsize),sum(unused) FROM dbstat "
                    "GROUP BY name ORDER BY sum(pgsize) DESC"
                )
            ]
            result["dbstat_complete"] = True
        except sqlite3.OperationalError as error:
            result["dbstat_error"] = str(error)
        return result


def compact_copy(source: Path, destination: Path) -> dict:
    """Produce a new verified candidate; never overwrite or mutate the input."""
    destination = destination.absolute()
    partial = destination.with_name(destination.name + ".unverified")
    if destination.exists() or partial.exists():
        raise ValueError("destination or unverified candidate already exists")
    source = standalone(source)
    before = source.stat()
    if shutil.disk_usage(destination.parent).free < 2 * before.st_size:
        raise ValueError("insufficient scratch capacity: reserve twice the source size")
    with closing(connect(source)) as connection:
        check(connection)
        membership = projection(connection)
        # Exclusive reservation plus VACUUM INTO's empty-file requirement prevents overwrite.
        with partial.open("xb"):
            pass
        connection.execute("VACUUM INTO ?", (str(partial),))
    tables = verify_equivalent(source, partial)
    after = standalone(source).stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("offline source changed during verification")
    # On Windows rename refuses existing destinations. Recheck for other platforms.
    if destination.exists():
        raise ValueError("destination already exists")
    partial.rename(destination)
    return {
        "verified": True,
        "source": str(source),
        "candidate": str(destination),
        "source_bytes": before.st_size,
        "candidate_bytes": destination.stat().st_size,
        "reclaimed_bytes": before.st_size - destination.stat().st_size,
        "projection": membership,
        "tables": tables,
        "installed": False,
        "history_deleted": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline-copy",
        type=Path,
        required=True,
        help="closed backup, never the operating database",
    )
    parser.add_argument(
        "--candidate", type=Path, help="new compacted output; omitted = inspect only"
    )
    parser.add_argument("--inspect-seconds", type=float, default=30)
    args = parser.parse_args()
    result = (
        compact_copy(args.offline_copy, args.candidate)
        if args.candidate
        else inspect_copy(args.offline_copy, seconds=args.inspect_seconds)
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
