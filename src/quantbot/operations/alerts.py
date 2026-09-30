"""Durable operational alerts, separate from trading state and authority.

Events are an outbox, not a claim of external delivery. Acknowledgement never resolves
the underlying condition; only a later measured update can do that.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

Severity = Literal["INFO", "WARNING", "CRITICAL"]


@dataclass(frozen=True)
class AlertPolicy:
    warning_seconds: int = 1800
    critical_seconds: int = 3600
    reminder_seconds: int = 3600
    notify_severities: tuple[Severity, ...] = ("WARNING", "CRITICAL")

    def __post_init__(self) -> None:
        if not 0 < self.warning_seconds < self.critical_seconds or self.reminder_seconds <= 0:
            raise ValueError("invalid alert thresholds")
        if not set(self.notify_severities) <= {"INFO", "WARNING", "CRITICAL"}:
            raise ValueError("invalid severity route")


@dataclass(frozen=True)
class Condition:
    code: str
    subsystem: str
    severity: Severity
    category: str
    owner: str
    action: str


class AlertStore:
    """Dedicated alert state; never opens the broker/learner database."""

    def __init__(self, path: Path, *, policy: AlertPolicy | None = None) -> None:
        self.policy = policy or AlertPolicy()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                code TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY, document TEXT NOT NULL);
        """)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.connection.close()

    def state(self, key: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT document FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_state(self, key: str, document: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(document))
            )

    def active(self) -> list[dict[str, Any]]:
        return [
            a
            for row in self.connection.execute("SELECT document FROM alerts ORDER BY code")
            if (a := json.loads(row[0]))["resolved_at"] is None
        ]

    def _save(self, alert: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO alerts VALUES (?, ?)", (alert["code"], json.dumps(alert))
        )

    def _time(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("aware clock required")
        latest = self.state("clock").get("at")
        if latest and now < datetime.fromisoformat(latest):
            raise ValueError("alert clock moved backwards")
        self.connection.execute(
            "INSERT OR REPLACE INTO state VALUES (?, ?)",
            ("clock", json.dumps({"at": now.isoformat()})),
        )

    def acknowledge(self, code: str, *, now: datetime) -> None:
        with self.connection:
            self._time(now)
            matches = [a for a in self.active() if a["code"] == code]
            if not matches:
                raise ValueError("active alert not found")
            alert = matches[0]
            alert["acknowledged_at"] = now.isoformat()
            self._save(alert)
            self.connection.execute(
                "INSERT INTO events(document) VALUES (?)",
                (json.dumps(dict(alert, event="ACKNOWLEDGED")),),
            )

    def update(
        self, conditions: list[Condition], *, now: datetime, retain_codes: tuple[str, ...] = ()
    ) -> list[dict[str, Any]]:
        """Refresh measured conditions; retain paused watchdog alerts without reminders."""
        notifications = []
        with self.connection:
            self._time(now)
            active = {a["code"]: a for a in self.active()}
            seen = set(retain_codes)
            for condition in conditions:
                if condition.code in seen:
                    raise ValueError("duplicate condition")
                seen.add(condition.code)
                alert = active.get(condition.code)
                event = None
                if alert is None:
                    alert = dict(
                        asdict(condition),
                        first_seen=now.isoformat(),
                        acknowledged_at=None,
                        resolved_at=None,
                        last_notified_at=None,
                        escalation=0,
                    )
                    event = "OPENED"
                elif alert["severity"] != condition.severity:
                    event = "SEVERITY_CHANGED"
                alert.update(asdict(condition), last_seen=now.isoformat())
                alert["duration_seconds"] = (
                    now - datetime.fromisoformat(alert["first_seen"])
                ).total_seconds()
                last = alert["last_notified_at"]
                if (
                    event is None
                    and alert["severity"] == "CRITICAL"
                    and last
                    and (now - datetime.fromisoformat(last)).total_seconds()
                    >= self.policy.reminder_seconds
                ):
                    event = "REMINDER"
                    alert["escalation"] += 1
                if event:
                    routed = condition.severity in self.policy.notify_severities
                    if routed:
                        alert["last_notified_at"] = now.isoformat()
                    document = dict(
                        alert,
                        event=event,
                        route="STDOUT_AND_CONSOLE",
                        external_delivery="NOT_CONFIGURED",
                    )
                    self.connection.execute(
                        "INSERT INTO events(document) VALUES (?)", (json.dumps(document),)
                    )
                    if routed:
                        notifications.append(document)
                self._save(alert)
            for code, alert in active.items():
                if code in seen:
                    continue
                alert["resolved_at"] = now.isoformat()
                alert["duration_seconds"] = (
                    now - datetime.fromisoformat(alert["first_seen"])
                ).total_seconds()
                self._save(alert)
                document = dict(
                    alert,
                    event="RECOVERED",
                    route="STDOUT_AND_CONSOLE",
                    external_delivery="NOT_CONFIGURED",
                )
                self.connection.execute(
                    "INSERT INTO events(document) VALUES (?)", (json.dumps(document),)
                )
                if alert["severity"] in self.policy.notify_severities:
                    notifications.append(document)
        return notifications


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List or acknowledge existing local alerts.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--acknowledge")
    args = parser.parse_args(argv)
    if not args.database.is_file():
        print("ALERT_STORE_MISSING")
        return 1
    try:
        with AlertStore(args.database) as store:
            if args.acknowledge:
                store.acknowledge(args.acknowledge, now=datetime.now(UTC))
                print("ACKNOWLEDGED; condition remains active until measured recovery")
            else:
                print(json.dumps(store.active()))
        return 0
    except (ValueError, sqlite3.Error, OSError):
        print("ALERT_OPERATION_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
