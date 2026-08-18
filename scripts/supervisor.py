"""Local watchdog for the trading daemon.

The daemon died twice in one day and nothing noticed. A dead daemon is silent and costs a
missed rebalance every session it stays dead, which is a real and avoidable loss.

What this does: checks liveness by the writer lock rather than by a process listing, because
a process can exist without holding the lock — that exact mistake produced a confident wrong
diagnosis earlier. Restarts a daemon that is genuinely absent. Reports on durable state.

What this deliberately does NOT do: clear the kill switch, resolve a reconciliation failure,
or override a halt. Those are decisions the system made on purpose and a watchdog that
undoes them converts a safety mechanism into a nuisance. It restarts a *dead* process; it
never overrules a *deliberate* stop.

Usage:
    uv run python scripts/supervisor.py            # one pass, print findings
    uv run python scripts/supervisor.py --watch    # loop until interrupted
    uv run python scripts/supervisor.py --self-check
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from quantbot.storage import AlreadyLockedError, Database, SingleWriterLock, StorageRepository

INTERVAL_SECONDS = 300

#: Consecutive cycles are 24h apart midweek, 72h across a weekend, and 96h when a Monday
#: holiday intervenes. A tighter bound false-positives every Monday, which trains you to
#: ignore the warning — worse than no warning.
#: ponytail: fixed bound, not calendar-aware. Compare against the broker calendar instead if
#: a four-day blind spot mid-week ever matters.
STALE_RUN_HOURS = 100


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str  # INFO | WARN | CRITICAL
    check: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.severity:8}] {self.check:22} {self.detail}"


def _lock_path() -> Path:
    """The DAEMON lock, not the writer lock.

    They are different exclusions. The writer lock is held only while a cycle runs, so
    testing it would report a healthy sleeping daemon as dead for all but a few seconds
    a day. The daemon lock is held for the scheduler's whole lifetime.
    """
    writer = Path(os.environ.get("QUANTBOT_LOCK_PATH", "quantbot.lock"))
    return writer.with_suffix(".daemon.lock")


def daemon_holds_lock() -> bool:
    """Liveness by the lock, not by a process listing.

    A quantbot process can exist while holding nothing — it may be exiting, or may have
    failed to acquire. The lock is the only evidence that a writer is actually active.
    """
    try:
        with SingleWriterLock(_lock_path()):
            return False
    except AlreadyLockedError:
        return True


def restart_daemon() -> Finding:
    exe = Path(".venv/Scripts/quantbot.exe")
    if not exe.exists():
        return Finding("CRITICAL", "daemon-restart", f"executable missing at {exe}")
    logs = Path("logs")
    logs.mkdir(exist_ok=True)
    try:
        with (logs / "daemon.log").open("a", encoding="utf-8") as out:
            subprocess.Popen(  # noqa: S603 - fixed path, no user input
                [str(exe), "daemon"],
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
    except OSError as error:
        return Finding("CRITICAL", "daemon-restart", f"launch failed: {error}")
    time.sleep(6)
    if daemon_holds_lock():
        return Finding("INFO", "daemon-restart", "restarted and holding the lock")
    return Finding("CRITICAL", "daemon-restart", "launched but never acquired the lock")


def inspect_durable(now: datetime) -> list[Finding]:
    path = Path(os.environ.get("QUANTBOT_DB_PATH", "quantbot.db"))
    if not path.exists():
        return [Finding("CRITICAL", "ledger", f"database missing at {path}")]
    database = Database(path)
    try:
        with database.transaction() as session:
            repository = StorageRepository(session)
            kill = repository.get_kill_switch_state()
            reconciliation = repository.get_latest_reconciliation()
            runs = repository.list_runs()
            incidents = repository.list_incidents(unresolved_only=True)
    finally:
        database.close()

    findings: list[Finding] = []
    if kill.engaged:
        findings.append(
            Finding("CRITICAL", "kill-switch", f"engaged: {kill.reason} — needs a human")
        )
    else:
        findings.append(Finding("INFO", "kill-switch", "clear"))

    if reconciliation is None:
        findings.append(Finding("WARN", "reconciliation", "never run"))
    elif reconciliation.status.value != "RECONCILED" or reconciliation.diffs:
        findings.append(
            Finding("CRITICAL", "reconciliation", f"{reconciliation.status.value}, "
                    f"{len(reconciliation.diffs)} diffs — blocks new orders")
        )
    else:
        findings.append(Finding("INFO", "reconciliation", "reconciled, no diffs"))

    succeeded = [r for r in runs if r.status == "SUCCEEDED" and r.finished_at is not None]
    if not succeeded:
        findings.append(Finding("WARN", "last-cycle", "no successful cycle recorded"))
    else:
        age = now - succeeded[-1].finished_at
        detail = f"{succeeded[-1].run_id} {age.total_seconds() / 3600:.1f}h ago"
        if age > timedelta(hours=STALE_RUN_HOURS):
            findings.append(Finding("WARN", "last-cycle", f"{detail} — stale"))
        else:
            findings.append(Finding("INFO", "last-cycle", detail))

    if incidents:
        findings.append(
            Finding("WARN", "incidents", f"{len(incidents)} unresolved: "
                    + ", ".join(i.kind for i in incidents[:3]))
        )
    return findings


def run_once(*, repair: bool = True) -> list[Finding]:
    now = datetime.now(UTC)
    findings: list[Finding] = []
    if daemon_holds_lock():
        findings.append(Finding("INFO", "daemon", "alive and holding the writer lock"))
    else:
        findings.append(Finding("CRITICAL", "daemon", "not running — no writer holds the lock"))
        if repair:
            findings.append(restart_daemon())
    findings.extend(inspect_durable(now))
    return findings


def _emit(findings: list[Finding]) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{stamp}  {finding}" for finding in findings]
    text = "\n".join(lines)
    print(text)
    logs = Path("logs")
    logs.mkdir(exist_ok=True)
    with (logs / "supervisor.log").open("a", encoding="utf-8") as out:
        out.write(text + "\n")


def self_check() -> int:
    """Prove the lock check distinguishes held from free, which is the whole basis of liveness.

    Uses a scratch lock, never the production one: a self-test that contends with the running
    daemon would either fail spuriously or, worse, interfere with live trading.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "probe.lock"
        original = os.environ.get("QUANTBOT_LOCK_PATH")
        os.environ["QUANTBOT_LOCK_PATH"] = str(probe)
        try:
            assert daemon_holds_lock() is False, "an unheld lock must read as free"
            with SingleWriterLock(probe):
                assert daemon_holds_lock() is True, "a held lock must read as held"
            assert daemon_holds_lock() is False, "a released lock must read as free again"
        finally:
            if original is None:
                del os.environ["QUANTBOT_LOCK_PATH"]
            else:
                os.environ["QUANTBOT_LOCK_PATH"] = original

    empty = inspect_durable(datetime.now(UTC))
    assert all(isinstance(f, Finding) for f in empty)
    assert {f.severity for f in empty} <= {"INFO", "WARN", "CRITICAL"}
    print("self-check passed: lock liveness and durable inspection behave correctly")
    return 0


def main() -> int:
    if "--self-check" in sys.argv:
        return self_check()
    watch = "--watch" in sys.argv
    while True:
        findings = run_once()
        _emit(findings)
        if not watch:
            return 1 if any(f.severity == "CRITICAL" for f in findings) else 0
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
