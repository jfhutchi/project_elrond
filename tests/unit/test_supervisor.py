"""The supervisor has restart authority, so its limits need pinning.

It can launch processes. What matters most is what it must never do: clear the kill switch,
resolve a reconciliation failure, or otherwise undo a halt the system chose deliberately. A
watchdog with unbounded authority converts a safety mechanism into a nuisance, and that
property had no test — only a hand-run `--self-check`.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from quantbot.domain import ReconciliationStatus, StrategyIdentity
from quantbot.storage import Database, SingleWriterLock, StorageRepository

ROOT = Path(__file__).parents[2]


def _supervisor() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "quantbot_supervisor", ROOT / "scripts" / "supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["quantbot_supervisor"] = module
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 17, 20, 5, tzinfo=UTC)


def _ledger(tmp_path: Path) -> Database:
    return Database(tmp_path / "quantbot.db")


def test_liveness_is_the_lock_not_the_process_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction that produced a wrong diagnosis: a process can hold nothing."""
    supervisor = _supervisor()
    lock = tmp_path / "probe.lock"
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(lock))

    assert supervisor.daemon_holds_lock() is False
    with SingleWriterLock(lock):
        assert supervisor.daemon_holds_lock() is True
    assert supervisor.daemon_holds_lock() is False


def test_an_engaged_kill_switch_is_critical_and_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most important limit: it reports a halt, it never lifts one."""
    supervisor = _supervisor()
    database = _ledger(tmp_path)
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(
            True, reason="halted for a reason", updated_at=NOW
        )
    database.close()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))

    findings = supervisor.inspect_durable(NOW)

    kill = [f for f in findings if f.check == "kill-switch"]
    assert len(kill) == 1
    assert kill[0].severity == "CRITICAL"
    assert "halted for a reason" in kill[0].detail

    reopened = Database(tmp_path / "quantbot.db")
    with reopened.transaction() as session:
        state = StorageRepository(session).get_kill_switch_state()
    reopened.close()
    assert state.engaged is True, "the supervisor must never clear a deliberate halt"
    assert state.reason == "halted for a reason"


def test_a_failed_reconciliation_is_critical_and_left_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor()
    database = _ledger(tmp_path)
    with database.transaction() as session:
        StorageRepository(session).save_reconciliation(
            "reconciliation-bad",
            status=ReconciliationStatus.RECONCILIATION_REQUIRED,
            started_at=NOW - timedelta(minutes=1),
            completed_at=NOW,
            diffs=[{
                "diff_id": "d1", "category": "POSITION", "key": "SPY",
                "expected": {"quantity": "1"}, "actual": {"quantity": "2"},
            }],
        )
    database.close()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))

    findings = supervisor.inspect_durable(NOW)

    entry = next(f for f in findings if f.check == "reconciliation")
    assert entry.severity == "CRITICAL"
    assert "blocks new orders" in entry.detail

    reopened = Database(tmp_path / "quantbot.db")
    with reopened.transaction() as session:
        latest = StorageRepository(session).get_latest_reconciliation()
    reopened.close()
    assert latest is not None
    assert latest.status is ReconciliationStatus.RECONCILIATION_REQUIRED
    assert len(latest.diffs) == 1, "the supervisor must not paper over a mismatch"


def test_repair_can_be_declined_so_the_watchdog_only_observes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With repair off it must report an absent daemon without launching anything."""
    supervisor = _supervisor()
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(tmp_path / "probe.lock"))
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))
    _ledger(tmp_path).close()
    launched: list[str] = []
    monkeypatch.setattr(
        supervisor, "restart_daemon", lambda: launched.append("called")  # type: ignore[misc]
    )

    findings = supervisor.run_once(repair=False)

    daemon = next(f for f in findings if f.check == "daemon")
    assert daemon.severity == "CRITICAL"
    assert launched == [], "repair=False must not launch a process"


def test_a_weekend_gap_is_not_reported_as_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 30h bound cried wolf every Monday; consecutive cycles are 72h apart over a weekend."""
    supervisor = _supervisor()
    assert supervisor.STALE_RUN_HOURS >= 96, "must tolerate a Monday-holiday gap"
    database = _ledger(tmp_path)
    friday = NOW - timedelta(hours=72)
    identity = StrategyIdentity(
        strategy_id="adaptive-momentum-v1-test",
        version="1.2.0",
        git_commit="abc123",
        configuration_hash="a" * 64,
        deployment_timestamp=friday,
    )
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_run("run-friday", identity, started_at=friday)
        repository.finish_run("run-friday", status="SUCCEEDED", finished_at=friday)
    database.close()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))

    findings = supervisor.inspect_durable(NOW)

    entry = next(f for f in findings if f.check == "last-cycle")
    assert entry.severity == "INFO"
    assert "stale" not in entry.detail


def test_a_missing_ledger_is_critical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = _supervisor()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "absent.db"))

    findings = supervisor.inspect_durable(NOW)

    assert len(findings) == 1
    assert findings[0].severity == "CRITICAL"
    assert findings[0].check == "ledger"
