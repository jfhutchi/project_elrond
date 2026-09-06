"""CI must fail for the named defect, and never borrow operator state."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def module(name: str) -> ModuleType:
    path = ROOT / "scripts" / f"{name}.py"
    assert path.is_file(), f"missing CI implementation: {path.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args], text=True, stderr=subprocess.STDOUT
    ).strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "CI test")
    (root / ".gitignore").write_text(".env\n*.db\n.local-ci/\n")
    (root / "tracked.txt").write_text("original\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture")
    return root


def test_child_environment_removes_credentials_config_and_real_home(tmp_path: Path) -> None:
    runner = module("local_ci")
    hostile = {
        "PATH": os.environ["PATH"],
        "ALPACA_PAPER_API_SECRET": "operator-secret-sentinel",
        "GITHUB_TOKEN": "github-secret-sentinel",
        "QUANTBOT_DB_PATH": "operational.db",
        "QUANTBOT_MODEL_ENDPOINT": "http://operator-control",
        "PYTHONPATH": "operator-plugin",
        "UV_INDEX_URL": "https://secret@index.invalid",
        "USERPROFILE": "real-operator-home",
        "TRADING_MODE": "LIVE",
    }
    env = runner.clean_environment(hostile, tmp_path)
    for key in hostile.keys() - {"PATH", "USERPROFILE", "TRADING_MODE"}:
        assert key not in env, key
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert env["TRADING_MODE"] == "PAPER"
    assert env["LIVE_TRADING_ACKNOWLEDGED"] == "false"
    assert env["KILL_SWITCH"] == "true"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_snapshot_uses_committed_bytes_without_operator_files(
    repository: Path, tmp_path: Path
) -> None:
    runner = module("local_ci")
    (repository / ".env").write_text("SECRET=sentinel")
    (repository / "operator.db").write_text("operator database")
    destination = tmp_path / "job"
    destination.mkdir()
    env = runner.clean_environment(os.environ, tmp_path / "home")
    sha = runner.export_snapshot(repository, destination, env)
    assert sha == git(repository, "rev-parse", "HEAD")
    assert (destination / "tracked.txt").read_text() == "original\n"
    assert not (destination / ".env").exists()
    assert not (destination / "operator.db").exists()
    assert git(destination, "remote", "-v") == ""
    assert git(destination, "status", "--porcelain") == ""
    (destination / "tracked.txt").write_text("job changed it")
    assert (repository / "tracked.txt").read_text() == "original\n"


def test_dirty_source_is_refused_instead_of_testing_old_commit(
    repository: Path, tmp_path: Path
) -> None:
    runner = module("local_ci")
    (repository / "tracked.txt").write_text("uncommitted")
    with pytest.raises(runner.CIError, match="SOURCE_DIRTY"):
        runner.export_snapshot(
            repository, tmp_path / "job", runner.clean_environment(os.environ, tmp_path / "home")
        )


def test_tracked_operational_files_are_refused(repository: Path, tmp_path: Path) -> None:
    runner = module("local_ci")
    (repository / ".env").write_text("not-a-real-secret")
    git(repository, "add", "-f", ".env")
    git(repository, "commit", "-qm", "unsafe fixture")
    with pytest.raises(runner.CIError, match="UNSAFE_SNAPSHOT"):
        runner.export_snapshot(
            repository, tmp_path / "job", runner.clean_environment(os.environ, tmp_path / "home")
        )


def test_child_failure_streams_output_and_preserves_failure(tmp_path: Path) -> None:
    runner = module("local_ci")
    log = io.StringIO()
    with pytest.raises(runner.CIError, match="exit 7"):
        runner.run_command(
            [sys.executable, "-c", "print('named-failure'); raise SystemExit(7)"],
            tmp_path,
            runner.clean_environment(os.environ, tmp_path / "home"),
            log,
        )
    assert "named-failure" in log.getvalue()


def test_clean_tree_gate_detects_tracked_and_untracked_writes(repository: Path) -> None:
    checks = module("ci_checks")
    assert checks.check_tree(repository) == 0
    (repository / "tracked.txt").write_text("changed\n")
    assert checks.check_tree(repository) != 0
    (repository / "tracked.txt").write_text("original\n")
    (repository / "unexpected.txt").write_text("new")
    assert checks.check_tree(repository) != 0


def test_shared_suite_stops_after_failed_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = module("ci_checks")
    calls: list[list[str]] = []

    def execute(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 9 if "ruff" in argv else 0)

    monkeypatch.setattr(checks.subprocess, "run", execute)
    monkeypatch.setattr(checks, "check_tree", lambda root: 0)
    assert checks.run_suite(tmp_path) == 9
    assert not any("pytest" in call for call in calls)


def test_shared_commands_preserve_lock_and_both_type_targets() -> None:
    checks = module("ci_checks")
    commands = checks.commands()
    assert ["uv", "lock", "--check"] in commands
    assert ["uv", "sync", "--frozen", "--group", "dev"] in commands
    assert ["uv", "run", "--frozen", "mypy", "--platform", "linux", "src"] in commands
    assert ["uv", "run", "--frozen", "mypy", "--platform", "win32", "src"] in commands
    assert ["uv", "run", "--frozen", "pytest"] in commands


def test_linux_execution_has_no_network_socket_mount_or_privilege() -> None:
    runner = module("local_ci")
    command = runner.docker_run_command("docker", "elrond-ci-test", "elrond-ci:test")
    assert "--network=none" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert "--user=10001:10001" in command
    assert not any("mount" in arg or "privileged" in arg or "docker.sock" in arg for arg in command)
    assert "--offline" in command


def test_missing_linux_prerequisite_is_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = module("local_ci")
    monkeypatch.setattr(runner.shutil, "which", lambda executable: None)
    with pytest.raises(runner.CIError, match="DOCKER_NOT_FOUND"):
        runner.linux_prerequisite(tmp_path, os.environ, io.StringIO())


def test_hosted_matrix_reuses_shared_script_without_auto_triggers() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    job = workflow["jobs"]["checks"]
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "windows-latest"]
    assert job["name"] == "checks"
    assert any(step.get("run") == "python scripts/ci_checks.py" for step in job["steps"])


def test_unavailable_requested_platform_writes_not_run_and_fails(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = module("local_ci")
    monkeypatch.setattr(runner, "__file__", str(repository / "scripts/local_ci.py"))
    monkeypatch.setattr(runner.shutil, "which", lambda executable: None)
    assert runner.main(["--platform", "linux"]) == 1
    reports = list((repository / ".local-ci").glob("*/summary.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["jobs"]["linux"]["status"] == "NOT RUN"
    assert "DOCKER_NOT_FOUND" in report["jobs"]["linux"]["reason"]
    assert report["jobs"]["windows"]["status"] == "NOT RUN"


def test_timeout_is_a_named_failure_and_stops_child(tmp_path: Path) -> None:
    runner = module("local_ci")
    with pytest.raises(runner.CIError, match="COMMAND_TIMEOUT"):
        runner.run_command(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path, runner.clean_environment(os.environ, tmp_path / "home"),
            io.StringIO(), timeout=1,
        )
