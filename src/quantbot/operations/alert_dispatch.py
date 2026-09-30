"""Bounded, durable sanitized GitHub transport; comment creation is not chat delivery."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, Self
from uuid import UUID, uuid4

from quantbot.operations.alert_delivery import accept_comment, encode_event, parse_comment
from quantbot.operations.service import ServiceRefusal, _git

REPOSITORY = "jfhutchi/project_elrond"
PR = 255


class Transport(Protocol):
    def find(self, identifier: str, wire: str) -> str | None: ...
    def post(self, wire: str) -> str: ...


class DeliveryQueue:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        tables = {
            row[0]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        expected = {
            "deliveries": ("id", "wire", "url"),
            "state": ("key", "value"),
            "ingested": ("event_id", "status"),
        }
        if tables:
            if tables != set(expected) or any(
                tuple(row[1] for row in self.connection.execute(f"PRAGMA table_info({table})"))
                != columns
                for table, columns in expected.items()
            ):
                self.connection.close()
                raise ValueError("DELIVERY_QUEUE_SCHEMA_REFUSED")
        else:
            self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, wire TEXT NOT NULL, url TEXT);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ingested (
                event_id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        """)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    def enqueue(
        self, source: dict[str, Any], *, now: datetime, identifier: UUID | None = None
    ) -> str:
        if source.get("code") != "DELIVERY_CANARY" and not self.confirmed():
            raise ValueError("CHAT_ROUTE_UNCONFIRMED")
        identifier = identifier or uuid4()
        wire = encode_event(source, delivery_id=identifier, emitted_at=now)
        with self.connection:
            old = self.connection.execute(
                "SELECT wire FROM deliveries WHERE id=?", (str(identifier),)
            ).fetchone()
            if old and old[0] != wire:
                raise ValueError("DELIVERY_ID_CONFLICT")
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, NULL)", (str(identifier), wire)
            )
        return wire

    def pending(self) -> tuple[str, str] | None:
        row = self.connection.execute(
            "SELECT id, wire FROM deliveries WHERE url IS NULL ORDER BY rowid LIMIT 1"
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def sent(self, identifier: str, url: str) -> None:
        if not url.startswith("https://github.com/jfhutchi/project_elrond/pull/255#issuecomment-"):
            raise ValueError("COMMENT_ORIGIN_REFUSED")
        suffix = url.rsplit("-", 1)[1]
        if not suffix.isascii() or not suffix.isdigit():
            raise ValueError("COMMENT_ORIGIN_REFUSED")
        with self.connection:
            self.connection.execute("UPDATE deliveries SET url=? WHERE id=?", (url, identifier))

    def confirmed(self) -> bool:
        return (
            self.connection.execute("SELECT 1 FROM state WHERE key='canary_receipt'").fetchone()
            is not None
        )

    def confirm_canary(self, identifier: str) -> None:
        """Call only after the parent confirms actual receipt in the approved chat."""
        row = self.connection.execute(
            "SELECT wire, url FROM deliveries WHERE id=?", (identifier,)
        ).fetchone()
        if not row or not row[1] or parse_comment(row[0])["code"] != "DELIVERY_CANARY":
            raise ValueError("CANARY_COMMENT_UNVERIFIED")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO state VALUES ('canary_receipt', ?)", (identifier,)
            )

    def ingest(self, path: Path, *, now: datetime) -> None:
        if not self.confirmed():
            raise ValueError("CHAT_ROUTE_UNCONFIRMED")
        if not path.is_absolute() or not path.is_file():
            raise ValueError("ALERT_SOURCE_REFUSED")
        source_identity = str(path.resolve())
        bound = self.connection.execute("SELECT value FROM state WHERE key='source'").fetchone()
        if bound and bound[0] != source_identity:
            raise ValueError("ALERT_SOURCE_DRIFT_REFUSED")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO state VALUES ('source', ?)", (source_identity,)
            )
        cursor = self.connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) FROM ingested"
        ).fetchone()[0]
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as source:
            rows = source.execute(
                "SELECT id, document FROM events WHERE id>? ORDER BY id LIMIT 20", (cursor,)
            ).fetchall()
        for event_id, document in rows:
            with self.connection:
                status = "FILTERED"
                try:
                    event = json.loads(document)
                    if (
                        event.get("severity") in ("WARNING", "CRITICAL")
                        and event.get("event") != "ACKNOWLEDGED"
                    ):
                        wire = encode_event(event, delivery_id=uuid4(), emitted_at=now)
                        identifier = parse_comment(wire)["delivery_id"]
                        self.connection.execute(
                            "INSERT INTO deliveries VALUES (?, ?, NULL)", (identifier, wire)
                        )
                        status = "QUEUED"
                except (ValueError, TypeError, AttributeError, RecursionError):
                    status = "ENVELOPE_REFUSED"
                self.connection.execute("INSERT INTO ingested VALUES (?, ?)", (event_id, status))

    def initialize_source(self, path: Path, *, now: datetime) -> None:
        """Commission current active conditions once; preserve the source history.

        Historical recovered alerts must not masquerade as a current outage on
        first delivery. Future source events retain their normal durable cursor.
        """
        if not self.confirmed() or not path.is_absolute() or not path.is_file():
            raise ValueError("ALERT_SOURCE_COMMISSION_REFUSED")
        own_path = Path(self.connection.execute("PRAGMA database_list").fetchone()[2]).resolve()
        if path.resolve() == own_path or path.samefile(own_path):
            raise ValueError("ALERT_QUEUE_SOURCE_ALIAS_REFUSED")
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as source:
            source.execute("BEGIN")
            cursor = source.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
            documents = source.execute("SELECT document FROM alerts LIMIT 21").fetchall()
        if len(documents) > 20:
            raise ValueError("ALERT_BASELINE_LIMIT_EXCEEDED")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.connection.execute("SELECT 1 FROM state WHERE key='source'").fetchone():
                raise ValueError("ALERT_SOURCE_ALREADY_COMMISSIONED")
            for (document,) in documents:
                event = json.loads(document)
                if (event.get("resolved_at") is not None
                        or event.get("acknowledged_at") is not None
                        or event.get("severity") not in ("WARNING", "CRITICAL")):
                    continue
                event["event"] = "OPENED"
                wire = encode_event(event, delivery_id=uuid4(), emitted_at=now)
                identifier = parse_comment(wire)["delivery_id"]
                self.connection.execute(
                    "INSERT INTO deliveries VALUES (?, ?, NULL)", (identifier, wire)
                )
            self.connection.execute(
                "INSERT INTO state VALUES ('source', ?)", (str(path.resolve()),)
            )
            if cursor:
                self.connection.execute("INSERT INTO ingested VALUES (?, 'BASELINE')", (cursor,))


def dispatch_one(queue: DeliveryQueue, transport: Transport) -> str:
    try:
        with queue.connection:
            queue.connection.execute("BEGIN IMMEDIATE")
            return _dispatch_locked(queue, transport)
    except sqlite3.OperationalError:
        return "DISPATCH_BUSY"


def _dispatch_locked(queue: DeliveryQueue, transport: Transport) -> str:
    pending = queue.pending()
    if pending is None:
        return "NO_PENDING_DELIVERY"
    identifier, wire = pending
    try:
        parse_comment(wire)
        url = transport.find(identifier, wire)
        if url is None:
            url = transport.post(wire)
        queue.sent(identifier, url)
        return "COMMENT_CREATED_CHAT_UNCONFIRMED"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "RETRY_PENDING"


class GitHubTransport:
    def __init__(self) -> None:
        executable = shutil.which("gh")
        if not executable:
            raise ValueError("EXISTING_GITHUB_AUTH_REQUIRED")
        self.executable = executable
        user = self._api("user")
        repository = self._api(f"repos/{REPOSITORY}")
        if (
            user.get("login") != "jfhutchi"
            or user.get("type") != "User"
            or repository.get("full_name") != REPOSITORY
            or repository.get("private") is not True
        ):
            raise ValueError("GITHUB_ORIGIN_REFUSED")

    def _api(self, endpoint: str, *, body: str | None = None) -> Any:
        command = [self.executable, "api", "--hostname", "github.com", endpoint]
        if body is not None:
            command.extend(["--method", "POST", "--input", "-"])
        result = subprocess.run(
            command,
            input=body,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            env={
                key: value
                for key, value in os.environ.items()
                if key.upper()
                in {
                    "PATH",
                    "PATHEXT",
                    "SYSTEMROOT",
                    "WINDIR",
                    "COMSPEC",
                    "USERPROFILE",
                    "HOMEDRIVE",
                    "HOMEPATH",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "TEMP",
                    "TMP",
                    "GH_CONFIG_DIR",
                    "GH_TOKEN",
                    "GITHUB_TOKEN",
                }
            },
        )
        if result.returncode:
            raise OSError("GITHUB_REQUEST_FAILED")
        try:
            return json.loads(result.stdout)
        except (ValueError, RecursionError):
            raise ValueError("GITHUB_RESPONSE_REFUSED") from None

    def find(self, identifier: str, wire: str) -> str | None:
        for page in range(1, 4):
            comments = self._api(
                f"repos/{REPOSITORY}/issues/{PR}/comments?per_page=100&page={page}"
            )
            if not isinstance(comments, list):
                raise ValueError("GITHUB_RESPONSE_REFUSED")
            for comment in comments:
                try:
                    parsed = accept_comment(
                        comment["body"],
                        repository=REPOSITORY,
                        pull_request=PR,
                        author=comment["user"]["login"],
                        author_type=comment["user"]["type"],
                        repository_private=True,
                    )
                    if parsed["delivery_id"] == identifier:
                        if comment["body"] != wire:
                            raise OSError("DELIVERY_ID_CONTENT_DRIFT")
                        return str(comment["html_url"])
                except (KeyError, TypeError, ValueError):
                    continue
            if len(comments) < 100:
                return None
        raise ValueError("COMMENT_SCAN_BOUND_REFUSED")

    def post(self, wire: str) -> str:
        parse_comment(wire)
        comment = self._api(
            f"repos/{REPOSITORY}/issues/{PR}/comments", body=json.dumps({"body": wire})
        )
        accept_comment(
            comment["body"],
            repository=REPOSITORY,
            pull_request=PR,
            author=comment["user"]["login"],
            author_type=comment["user"]["type"],
            repository_private=True,
        )
        if comment["body"] != wire:
            raise OSError("COMMENT_CONTENT_DRIFT")
        return str(comment["html_url"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--stage-canary", action="store_true")
    parser.add_argument("--confirm-canary")
    parser.add_argument("--events-database", type=Path)
    parser.add_argument("--initialize-source", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--commit")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.queue.is_absolute():
        print("DELIVERY_QUEUE_REFUSED")
        return 1
    if args.events_database and (
        args.queue.resolve() == args.events_database.resolve()
        or (
            args.queue.exists()
            and args.events_database.exists()
            and args.queue.samefile(args.events_database)
        )
    ):
        print("ALERT_QUEUE_SOURCE_ALIAS_REFUSED")
        return 1
    try:
        if args.commit is not None:
            repository = Path(__file__).resolve().parents[3]
            if (
                len(args.commit) != 40
                or any(c not in "0123456789abcdef" for c in args.commit)
                or _git(repository, "rev-parse", "HEAD") != args.commit
                or _git(repository, "status", "--porcelain")
            ):
                raise ValueError("DISPATCH_CHECKOUT_REFUSED")
        if args.check:
            if args.commit is None:
                raise ValueError("DISPATCH_PIN_REQUIRED")
            print("DISPATCH_LAUNCH_CHECK_OK")
            return 0
        with DeliveryQueue(args.queue) as queue:
            if args.stage_canary:
                now = datetime.now(UTC)
                wire = queue.enqueue(
                    dict(
                        code="DELIVERY_CANARY",
                        event="CANARY",
                        severity="INFO",
                        first_seen=now.isoformat(),
                    ),
                    now=now,
                )
                print(wire)
            if args.confirm_canary:
                queue.confirm_canary(args.confirm_canary)
            if args.events_database:
                if args.initialize_source:
                    queue.initialize_source(args.events_database, now=datetime.now(UTC))
                else:
                    queue.ingest(args.events_database, now=datetime.now(UTC))
            if args.send:
                status = (
                    dispatch_one(queue, GitHubTransport()) if queue.pending()
                    else "NO_PENDING_DELIVERY"
                )
                print(status)
                if status in ("RETRY_PENDING", "DISPATCH_BUSY"):
                    return 1
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
            subprocess.SubprocessError, ServiceRefusal):
        print("DELIVERY_REFUSED_OR_UNAVAILABLE")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
