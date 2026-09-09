"""Recovery must observe the broken system without repairing it into plausible fiction."""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantbot.cli import main
from quantbot.operations.checkout import checkout_evidence, compare_commit
from quantbot.operations.deployment import (
    DeploymentRegistry,
    DeploymentWarning,
    RuntimeContext,
    RuntimeRole,
    summarize,
)
from quantbot.operations.inventory import discover_stores, inspect_store, inventory
from quantbot.storage import Database

NOW = datetime(2026, 9, 5, 17, tzinfo=UTC)


def legacy(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE schema_version (id INTEGER, version INTEGER);
            INSERT INTO schema_version VALUES (1, 12);
            CREATE TABLE runs (run_id TEXT, status TEXT, started_at TEXT, finished_at TEXT);
            INSERT INTO runs VALUES ('historical', 'HALTED', '2026-08-19T20:05:00Z', NULL);
            CREATE TABLE kill_switch_state (engaged INTEGER, updated_at TEXT, reason TEXT);
            INSERT INTO kill_switch_state VALUES (1, '2026-08-20T14:00:00Z', 'secret=do-not-print');
            CREATE TABLE account_snapshots (account_id TEXT, captured_at TEXT, run_id TEXT);
            INSERT INTO account_snapshots VALUES ('private-account', '2026-08-20T14:00:00Z',
                                                  'historical');
        """)


def test_missing_store_and_parent_are_not_created(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "quantbot.db"
    result = inventory(repo=tmp_path, environ={"QUANTBOT_DB_PATH": str(missing)}, now=NOW)
    assert result["stores"][0]["state"] == "ABSENT"
    assert not missing.parent.exists()
    assert result["ok"] is False
    assert result["store_authority"] == "UNKNOWN"
    assert result["pi_retirement"] == {
        "policy": "RETIRED",
        "authority": "OPERATOR_DECLARED",
        "remote_state": "NOT_PROBED",
    }
    assert result["startup_preflight"]["allowed"] is False
    assert result["workers_have_coordinator_authority"] is False


def test_operator_retired_pi_is_not_a_remote_state_blocker(tmp_path: Path) -> None:
    result = inventory(repo=tmp_path, environ={}, now=NOW)

    blockers = result["startup_preflight"]["blockers"]
    assert "PI_STATE_UNKNOWN" not in blockers
    assert "GLOBAL_COORDINATOR_AUTHORITY_UNVERIFIED" in blockers
    assert "OTHER_HOST_RETIREMENT_UNVERIFIED" in blockers
    assert result["pi_retirement"]["remote_state"] == "NOT_PROBED"


def test_old_schema_evidence_read_without_migration_or_kill_clear(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    legacy(path)
    before = path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir())
    result = inventory(repo=tmp_path, environ={"QUANTBOT_DB_PATH": str(path)}, now=NOW)
    store = result["stores"][0]
    assert store["evidence_state"] == "READABLE_SNAPSHOT"
    assert store["schema"] == [{"version": 12}]
    assert store["latest_run"][0]["status"] == "HALTED"
    assert store["kill_switch"][0]["engaged"] == 1
    assert store["runtime"] == "UNKNOWN"
    assert "CONFIGURED_STORE_SCHEMA_MISMATCH" in result["startup_preflight"]["blockers"]
    assert "KILL_SWITCH_ENGAGED" in result["startup_preflight"]["blockers"]
    assert "private-account" not in json.dumps(result)
    assert "do-not-print" not in json.dumps(result)
    assert store["latest_account_snapshot"][0]["account_hash"]
    assert before == (path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir()))


def test_two_plausible_stores_never_choose_newest_or_configured(tmp_path: Path) -> None:
    first, second = tmp_path / "first.db", tmp_path / "newer.db"
    legacy(first)
    shutil.copyfile(first, second)
    os.utime(second, (NOW.timestamp(), NOW.timestamp()))
    result = inventory(
        repo=tmp_path, candidates=[str(second)], environ={"QUANTBOT_DB_PATH": str(first)}, now=NOW
    )
    assert result["store_authority"] == "AMBIGUOUS"
    assert all(store["authority"] == "UNKNOWN" for store in result["stores"])
    assert "MULTIPLE_PLAUSIBLE_STORES" in result["startup_preflight"]["blockers"]


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_sidecar_is_not_ignored_to_publish_incomplete_truth(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / "state.db"
    legacy(path)
    sidecar = Path(str(path) + suffix)
    sidecar.write_bytes(b"unmerged durable state")
    before = path.read_bytes(), sidecar.read_bytes()
    result = inspect_store(path, now=NOW, repo=tmp_path, intended="UNKNOWN")
    assert result["evidence_state"] == "SIDECARS_REQUIRE_CONSISTENT_SNAPSHOT"
    assert "latest_run" not in result
    assert before == (path.read_bytes(), sidecar.read_bytes())


def test_real_uncheckpointed_wal_cannot_disappear_from_inventory(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE schema_version (version INTEGER)")
        connection.execute("INSERT INTO schema_version VALUES (12)")
        connection.commit()
        result = inspect_store(path, now=NOW, repo=tmp_path, intended="UNKNOWN")
        assert result["evidence_state"] == "SIDECARS_REQUIRE_CONSISTENT_SNAPSHOT"
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (12,)
    finally:
        connection.close()


def test_changed_file_discards_even_otherwise_readable_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "state.db"
    legacy(path)
    from quantbot.operations import store_inventory as module

    signatures = iter([((1, 1, 20, 1), None, None), ((1, 1, 20, 2), None, None)])
    monkeypatch.setattr(module, "_signature", lambda _: next(signatures))
    result = inspect_store(path, now=NOW, repo=tmp_path, intended="UNKNOWN")
    assert result["evidence_state"] == "CHANGED_DURING_INSPECTION"
    assert "latest_run" not in result


@pytest.mark.parametrize(
    "env",
    [
        {"QUANTBOT_DATABASE_URL": "postgresql://user:supersecret@localhost/db?token=hidden"},
        {
            "QUANTBOT_DATABASE_URL": "postgresql://user:supersecret@localhost/db",
            "QUANTBOT_DB_PATH": "wrong.db",
        },
    ],
)
def test_targets_refuse_without_credential_output_or_fallback(tmp_path: Path, env) -> None:
    result = inventory(repo=tmp_path, environ=env, now=NOW)
    rendered = json.dumps(result)
    assert "supersecret" not in rendered and "hidden" not in rendered
    assert result["database_target_error"] in ("BACKEND_NOT_ENABLED", "DATABASE_TARGET_AMBIGUOUS")
    assert result["stores"] == []
    assert result["startup_preflight"]["allowed"] is False


def test_live_configuration_is_reported_refused_and_untouched(tmp_path: Path) -> None:
    env = {
        "TRADING_MODE": "LIVE",
        "BROKER_ENVIRONMENT": "LIVE",
        "LIVE_TRADING_ACKNOWLEDGED": "true",
        "ALPACA_LIVE_API_SECRET": "private-token",
    }
    before = dict(env)
    result = inventory(repo=tmp_path, environ=env, now=NOW)
    assert "PAPER_ONLY_CONFIGURATION_REQUIRED" in result["startup_preflight"]["blockers"]
    assert result["configured_mode"] == "LIVE"
    assert result["runtime_mode"] == "UNKNOWN"
    assert result["startup_preflight"]["allowed"] is False
    assert env == before
    assert "private-token" not in json.dumps(result)


def test_cli_inventory_never_constructs_runtime_even_with_credentials(tmp_path: Path, monkeypatch):
    import quantbot.cli as cli

    path = tmp_path / "old.db"
    legacy(path)
    before = path.read_bytes()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(path))
    monkeypatch.setenv("ALPACA_PAPER_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "test-secret")

    def forbidden():
        pytest.fail("inventory constructed a writable runtime")

    monkeypatch.setattr(cli, "_default_context", forbidden)
    stream = io.StringIO()
    assert (
        main(["deployment-status", "--startup-check", "--repo", str(tmp_path)], output=stream) == 1
    )
    assert json.loads(stream.getvalue())["stores"][0]["schema"] == [{"version": 12}]
    assert path.read_bytes() == before


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, text=True, capture_output=True
    )
    return result.stdout.strip()


@pytest.fixture
def commits(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "old",
    )
    first = git(repo, "rev-parse", "HEAD")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "intended",
    )
    second = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", second)
    return repo, first, second


def test_stale_recorded_sha_visible_against_real_git_ancestry(tmp_path: Path, commits) -> None:
    repo, old, intended = commits
    path = tmp_path / "current.db"
    db = Database.create(path)
    context = RuntimeContext(
        role=RuntimeRole.COORDINATOR,
        store_identity=db.store_identity,
        trading_mode="PAPER",
        process_started_at=NOW - timedelta(minutes=5),
        git_commit=old,
    )
    with db.transaction() as session:
        DeploymentRegistry(session).record(context.observe(now=NOW))
    db.close()
    before = path.read_bytes(), set(tmp_path.iterdir())
    result = inventory(repo=repo, environ={"QUANTBOT_DB_PATH": str(path)}, now=NOW)
    runtime = result["stores"][0]["runtime"]
    assert runtime["deployment_drift"] == "BEHIND"
    assert "STALE_CHECKOUT" in runtime["warnings"]
    assert runtime["intended_commit"] == intended
    assert runtime["coordinator"]["git_commit"] == old
    assert runtime["process_liveness"] == "UNKNOWN"
    assert result["store_authority"] == "UNKNOWN"
    assert result["ok"] is False
    assert before == (path.read_bytes(), set(tmp_path.iterdir()))


def test_unknown_intended_commit_is_explicit_and_not_stale(tmp_path: Path) -> None:
    db = Database.create(tmp_path / "state.db")
    try:
        with db.transaction() as session:
            status = DeploymentRegistry(session).status(now=NOW, intended_commit="not-a-sha-secret")
        assert status.intended_commit == "UNKNOWN"
        assert DeploymentWarning.UNKNOWN_DEPLOYMENT_INTENT in status.warnings
        assert DeploymentWarning.STALE_CHECKOUT not in status.warnings
        assert "not-a-sha-secret" not in json.dumps(summarize(status))
    finally:
        db.close()


def test_git_comparison_distinguishes_ahead_missing_and_match(commits) -> None:
    repo, old, intended = commits
    assert compare_commit(intended, old, repo) == "AHEAD"
    assert compare_commit(old, "f" * 40, repo) == "MISMATCH"
    assert compare_commit(old, old, repo) == "MATCH"
    assert compare_commit("UNKNOWN", intended, repo) == "UNKNOWN"
    evidence = checkout_evidence(repo)
    assert evidence["intended_commit"] == intended
    assert evidence["dirty"] is False


def test_scan_scope_limits_and_missing_roots_are_visible(tmp_path: Path) -> None:
    (tmp_path / "state.sqlite3").write_bytes(b"not a database")
    excluded = tmp_path / ".venv"
    excluded.mkdir()
    (excluded / "cache.db").write_bytes(b"cache")
    result = discover_stores(tmp_path)
    assert result["candidates"] == [str(tmp_path / "state.sqlite3")]
    assert discover_stores(tmp_path, max_entries=0)["limit_reached"] is True
    assert discover_stores(tmp_path / "missing")["unreadable"]


def test_corrupt_store_is_unknown_not_empty_healthy(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not sqlite password=secret-value")
    result = inspect_store(path, now=NOW, repo=tmp_path, intended="UNKNOWN")
    assert result["evidence_state"] == "UNREADABLE"
    assert result["authority"] == "UNKNOWN"
    assert "secret-value" not in json.dumps(result)


@pytest.mark.parametrize("output", [None, "invalid password=must-not-print", "[]", "{}"])
def test_os_probe_failure_is_unknown_not_no_processes(monkeypatch, output) -> None:
    from quantbot.operations import host_inventory as module

    monkeypatch.setattr(module, "_command", lambda _: output)
    assert module._windows_inventory() == {"state": "UNKNOWN", "reason": "OS_INVENTORY_UNAVAILABLE"}


def test_os_probe_marks_inspector_chain_without_converting_workers_to_authority(monkeypatch):
    from quantbot.operations import host_inventory as module

    payload = {
        "processes": [
            {"pid": os.getpid(), "parent_pid": 1},
            {"pid": 1, "parent_pid": 0},
            {"pid": 999999, "parent_pid": 0},
        ]
    }
    monkeypatch.setattr(module, "_command", lambda _: json.dumps(payload))
    result = module._windows_inventory()
    assert [item["diagnostic_ancestry"] for item in result["processes"]] == [True, True, False]
    assert all(item["coordinator_authority"] == "UNKNOWN" for item in result["processes"])


def test_credential_text_in_allowed_inventory_fields_is_redacted(tmp_path, monkeypatch):
    from quantbot.operations import host_inventory as module

    monkeypatch.setattr(
        module,
        "host_inventory",
        lambda: {
            "services": [{"name": "password=hidden https://user:private@host/?key=secret"}],
        },
    )
    result = inventory(repo=tmp_path, environ={}, include_host=True, now=NOW)
    rendered = json.dumps(result)
    assert all(value not in rendered for value in ("hidden", "private", "key=secret"))


def test_lock_file_existence_does_not_manufacture_ownership(tmp_path):
    lock = tmp_path / "quantbot.lock"
    lock.write_bytes(b"\0")
    result = inventory(repo=tmp_path, environ={"QUANTBOT_LOCK_PATH": str(lock)}, now=NOW)
    assert result["locks"][0]["state"] == "PRESENT"
    assert result["locks"][0]["held"] == "UNKNOWN"
    assert result["locks"][0]["owner"] == "UNKNOWN"
    assert lock.read_bytes() == b"\0"


def test_same_lineage_copies_still_have_ambiguous_authority(tmp_path):
    first, second = tmp_path / "first.db", tmp_path / "copy.db"
    db = Database.create(first)
    context = RuntimeContext(
        role=RuntimeRole.COORDINATOR,
        store_identity=db.store_identity,
        trading_mode="PAPER",
        process_started_at=NOW,
        git_commit="a" * 40,
    )
    with db.transaction() as session:
        DeploymentRegistry(session).record(context.observe(now=NOW))
    db.close()
    # Add a legitimate-looking historical run to both copies, without fabricating authority.
    with sqlite3.connect(first) as conn:
        conn.execute(
            "INSERT INTO strategy_deployments VALUES (?, ?, ?, ?, ?)",
            ("strategy", "1", "a" * 40, "b" * 64, NOW.isoformat()),
        )
    conn.close()  # Close/checkpoint the fixture before making the deliberately copied lineage.
    shutil.copyfile(first, second)
    result = inventory(
        repo=tmp_path, environ={"QUANTBOT_DB_PATH": str(first)}, candidates=[str(second)], now=NOW
    )
    assert result["stores"][0]["store_identity"] == result["stores"][1]["store_identity"]
    assert result["store_authority"] == "AMBIGUOUS"
