from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from quantbot.runtime import CREDENTIALS_MISSING, RuntimeConfigurationError
from quantbot.storage import Database, SingleWriterLock


def _service() -> ModuleType:
    try:
        return importlib.import_module("quantbot.operations.service")
    except ModuleNotFoundError:
        pytest.fail("quantbot.operations.service is not implemented")


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def clean_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "service-tests@example.invalid")
    _git(repo, "config", "user.name", "Service Tests")
    (repo / "strategy.yaml").write_text("version: test\n", encoding="utf-8")
    _git(repo, "add", "strategy.yaml")
    _git(repo, "commit", "-qm", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _deployment(
    tmp_path: Path,
    repo: Path,
    *,
    extra: str = "",
    create_database: bool = True,
) -> tuple[Path, Path, Path, Path]:
    state = tmp_path / "state"
    reports = tmp_path / "reports"
    state.mkdir()
    reports.mkdir()
    database = state / "paper.db"
    if create_database:
        Database.create(database)
    env_file = tmp_path / "quantbot.env"
    values = {
        "TRADING_MODE": "PAPER",
        "BROKER_ENVIRONMENT": "PAPER",
        "LIVE_TRADING_ACKNOWLEDGED": "false",
        "QUANTBOT_DB_PATH": str(database),
        "QUANTBOT_LOCK_PATH": str(state / "quantbot.lock"),
        "QUANTBOT_CONFIG": str(repo / "strategy.yaml"),
        "QUANTBOT_REPORTS_DIR": str(reports),
    }
    if extra:
        name, _, value = extra.partition("=")
        values[name] = value
    env_file.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )
    return env_file, database, state, reports


def _arguments(
    service: ModuleType,
    env_file: Path,
    database: Path,
    commit: str,
    component: str = "console",
) -> object:
    return service.build_parser().parse_args(
        [
            "--env-file",
            str(env_file),
            "--commit",
            commit,
            "--database",
            str(database),
            "--component",
            component,
            "--check",
        ]
    )


def test_environment_file_replaces_inherited_application_values_without_interpolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    env_file = tmp_path / "safe.env"
    env_file.write_text(
        "TRADING_MODE=PAPER\n"
        "BROKER_ENVIRONMENT=PAPER\n"
        "LIVE_TRADING_ACKNOWLEDGED=false\n"
        "QUANTBOT_MODEL_API_KEY=$ALPACA_LIVE_API_SECRET\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("QUANTBOT_DATABASE_URL", "sqlite:///injected.db")
    monkeypatch.setenv("ALPACA_LIVE_API_SECRET", "inherited-secret")

    with service.isolated_application_environment(env_file):
        assert os.environ["TRADING_MODE"] == "PAPER"
        assert "QUANTBOT_DATABASE_URL" not in os.environ
        assert "ALPACA_LIVE_API_SECRET" not in os.environ
        assert os.environ["QUANTBOT_MODEL_API_KEY"] == "$ALPACA_LIVE_API_SECRET"

    assert os.environ["TRADING_MODE"] == "LIVE"
    assert os.environ["ALPACA_LIVE_API_SECRET"] == "inherited-secret"


@pytest.mark.parametrize(
    "unsafe_line",
    (
        "TRADING_MODE=LIVE",
        "BROKER_ENVIRONMENT=LIVE",
        "LIVE_TRADING_ACKNOWLEDGED=true",
        "ALPACA_LIVE_API_KEY=live-key",
        "ALPACA_LIVE_API_SECRET=live-secret",
    ),
)
def test_check_refuses_every_live_boundary_without_echoing_secret(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unsafe_line: str,
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(tmp_path, repo, extra=unsafe_line)
    monkeypatch.setattr(service, "repository_root", lambda: repo)

    result = service.main(
        [
            "--env-file",
            str(env_file),
            "--commit",
            commit,
            "--database",
            str(database),
            "--component",
            "console",
            "--check",
        ]
    )

    captured = capsys.readouterr()
    assert result == service.EXIT_REFUSED
    assert "PAPER_CONFIGURATION_REQUIRED" in captured.err
    assert "live-key" not in captured.out + captured.err
    assert "live-secret" not in captured.out + captured.err


def test_check_refuses_ambiguous_database_configuration(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(
        tmp_path,
        repo,
        extra="QUANTBOT_DATABASE_URL=sqlite:///other.db",
    )
    args = _arguments(service, env_file, database, commit)

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="DATABASE_TARGET_AMBIGUOUS"
    ):
        service.validate_configuration(args, repository=repo)


def test_check_refuses_relative_runtime_path(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(
        tmp_path,
        repo,
        extra="QUANTBOT_LOCK_PATH=relative.lock",
    )
    args = _arguments(service, env_file, database, commit)

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="ABSOLUTE_RUNTIME_PATHS_REQUIRED"
    ):
        service.validate_configuration(args, repository=repo)


def test_check_does_not_bootstrap_a_missing_database(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(
        tmp_path,
        repo,
        create_database=False,
    )
    args = _arguments(service, env_file, database, commit)

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="DATABASE_NOT_FOUND"
    ):
        service.validate_configuration(args, repository=repo)

    assert not database.exists()


def test_check_refuses_commit_pin_drift(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, _commit = clean_repo
    env_file, database, _, _ = _deployment(tmp_path, repo)
    args = _arguments(service, env_file, database, "f" * 40)

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="CHECKOUT_PIN_MISMATCH"
    ):
        service.validate_configuration(args, repository=repo)


def test_check_refuses_a_dirty_checkout(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(tmp_path, repo)
    (repo / "untracked.txt").write_text("drift", encoding="utf-8")
    args = _arguments(service, env_file, database, commit)

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="CHECKOUT_NOT_CLEAN"
    ):
        service.validate_configuration(args, repository=repo)


@pytest.mark.parametrize("variable", ("QUANTBOT_LOCK_PATH", "QUANTBOT_REPORTS_DIR"))
def test_check_refuses_lock_and_report_paths_inside_checkout(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
    variable: str,
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(tmp_path, repo)
    if variable == "QUANTBOT_REPORTS_DIR":
        inside = repo / "reports"
        inside.mkdir()
        (inside / ".keep").write_text("", encoding="utf-8")
        _git(repo, "add", "reports/.keep")
        _git(repo, "commit", "-qm", "tracked report directory")
        commit = _git(repo, "rev-parse", "HEAD")
    else:
        inside = repo / "quantbot.lock"
    lines = [
        f"{variable}={inside}" if line.startswith(f"{variable}=") else line
        for line in env_file.read_text(encoding="utf-8").splitlines()
    ]
    lines.append(f"QUANTBOT_LOGS_DIR={tmp_path / 'logs'}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args = _arguments(service, env_file, database, commit)

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="EXTERNAL_STATE_REQUIRED"
    ):
        service.validate_configuration(args, repository=repo)


def test_broker_component_check_reports_missing_paper_credentials(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(tmp_path, repo)
    args = _arguments(service, env_file, database, commit, component="daemon")

    with service.isolated_application_environment(env_file), pytest.raises(
        service.ServiceRefusal, match="PAPER_CREDENTIALS_REQUIRED"
    ):
        service.validate_configuration(args, repository=repo)


def test_duplicate_component_is_refused_before_cli_dispatch(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, _ = _deployment(tmp_path, repo)
    args = _arguments(service, env_file, database, commit, component="ingestion")
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "\nALPACA_PAPER_API_KEY=paper-key\nALPACA_PAPER_API_SECRET=paper-secret\n",
        encoding="utf-8",
    )

    with service.isolated_application_environment(env_file):
        configuration = service.validate_configuration(args, repository=repo)
        dispatched = False

        def cli_runner(_argv: list[str], *, output: object) -> int:
            nonlocal dispatched
            dispatched = True
            return 0

        with SingleWriterLock(configuration.component_lock):
            result = service.run_component(configuration, cli_runner=cli_runner)

    assert result == service.EXIT_DUPLICATE
    assert dispatched is False


def test_research_dispatch_uses_fixed_command_external_cwd_and_no_broker_secrets(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, state, reports = _deployment(tmp_path, repo)
    env_file.write_text(
        env_file.read_text(encoding="utf-8")
        + "\nALPACA_PAPER_API_KEY=paper-key\nALPACA_PAPER_API_SECRET=paper-secret\n",
        encoding="utf-8",
    )
    args = _arguments(service, env_file, database, commit, component="research")

    seen: dict[str, object] = {}

    def cli_runner(argv: list[str], *, output: object) -> int:
        seen["argv"] = argv
        seen["cwd"] = Path.cwd()
        seen["key"] = os.environ.get("ALPACA_PAPER_API_KEY")
        seen["secret"] = os.environ.get("ALPACA_PAPER_API_SECRET")
        print("cycle output", file=output)
        return 0

    original_cwd = Path.cwd()
    with service.isolated_application_environment(env_file):
        configuration = service.validate_configuration(args, repository=repo)
        result = service.run_component(configuration, cli_runner=cli_runner)

    assert result == 0
    assert seen == {
        "argv": ["research-cycle", "--max-steps", "1", "--commit"],
        "cwd": state,
        "key": None,
        "secret": None,
    }
    assert Path.cwd() == original_cwd
    assert (reports.parent / "logs" / "research.log").read_text(encoding="utf-8") == (
        "cycle output\n"
    )


def test_component_exception_is_logged_without_its_secret_text(
    tmp_path: Path,
    clean_repo: tuple[Path, str],
) -> None:
    service = _service()
    repo, commit = clean_repo
    env_file, database, _, reports = _deployment(tmp_path, repo)
    args = _arguments(service, env_file, database, commit)

    def cli_runner(_argv: list[str], *, output: object) -> int:
        raise RuntimeError("paper-secret-value")

    with service.isolated_application_environment(env_file):
        configuration = service.validate_configuration(args, repository=repo)
        result = service.run_component(configuration, cli_runner=cli_runner)

    log = (reports.parent / "logs" / "console.log").read_text(encoding="utf-8")
    assert result == service.EXIT_COMPONENT_FAILED
    assert "COMPONENT_FAILED RuntimeError" in log
    assert "paper-secret-value" not in log


def test_missing_credentials_are_reported_for_protection_daemon_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import quantbot.runtime as runtime
    from quantbot import cli

    database = tmp_path / "paper.db"
    Database.create(database)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(database))

    def unavailable_runtime() -> object:
        raise RuntimeConfigurationError(CREDENTIALS_MISSING)

    monkeypatch.setattr(runtime, "build_cli_context", unavailable_runtime)

    result = cli.main(["protection-daemon"])

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["reason"] == CREDENTIALS_MISSING


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows Task Scheduler installer")
def test_windows_installer_registers_bounded_tasks_through_scheduler_doubles(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).resolve().parents[3]
    source_script = source_root / "deploy" / "omen" / "setup-windows.ps1"
    if not source_script.exists():
        pytest.fail("deploy/omen/setup-windows.ps1 is not implemented")

    repo = tmp_path / "installer-repo"
    shutil.copytree(source_root / "src", repo / "src")
    (repo / "deploy" / "omen").mkdir(parents=True)
    shutil.copy2(source_script, repo / "deploy" / "omen" / "setup-windows.ps1")
    (repo / "config").mkdir()
    (repo / "config" / "strategy.yaml").write_text("version: test\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".venv/\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Junction -Path '{repo / '.venv'}' -Target "
            f"'{source_root / '.venv'}' | Out-Null",
        ],
        check=True,
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "installer-tests@example.invalid")
    _git(repo, "config", "user.name", "Installer Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")

    state = tmp_path / "state"
    reports = tmp_path / "reports"
    state.mkdir()
    reports.mkdir()
    database = state / "paper.db"
    Database.create(database)
    env_file = tmp_path / "quantbot.env"
    env_file.write_text(
        "\n".join(
            (
                "TRADING_MODE=PAPER",
                "BROKER_ENVIRONMENT=PAPER",
                "LIVE_TRADING_ACKNOWLEDGED=false",
                f"QUANTBOT_DB_PATH={database}",
                f"QUANTBOT_LOCK_PATH={state / 'quantbot.lock'}",
                f"QUANTBOT_CONFIG={repo / 'config' / 'strategy.yaml'}",
                f"QUANTBOT_REPORTS_DIR={reports}",
            )
        ),
        encoding="utf-8",
    )
    wrapper = tmp_path / "invoke-installer.ps1"
    wrapper.write_text(
        """
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
$PSModuleAutoLoadingPreference = 'None'
$global:registrations = @()
$global:disabled = @()
function Get-ScheduledTask { param([string]$TaskName, [string]$ErrorAction) return $null }
function New-ScheduledTaskAction {
  param($Execute, $Argument, $WorkingDirectory)
  [pscustomobject]@{Execute=$Execute;Argument=$Argument;WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger {
  param([switch]$AtLogOn, [string]$User, [switch]$Once, [datetime]$At,
        [timespan]$RepetitionInterval, [timespan]$RepetitionDuration)
  [pscustomobject]@{AtLogOn=[bool]$AtLogOn;User=$User;Once=[bool]$Once;Interval=$RepetitionInterval}
}
function New-ScheduledTaskSettingsSet {
  param($RestartCount, $RestartInterval, $MultipleInstances, $ExecutionTimeLimit,
        [switch]$StartWhenAvailable, [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries)
  [pscustomobject]@{RestartCount=$RestartCount;MultipleInstances=$MultipleInstances;ExecutionTimeLimit=$ExecutionTimeLimit}
}
function New-ScheduledTaskPrincipal {
  param($UserId, $LogonType, $RunLevel)
  [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel}
}
function Register-ScheduledTask {
  param($TaskName, $Description, $Action, $Trigger, $Settings, $Principal,
        $User, $Password, [switch]$Force)
  $global:registrations += [pscustomobject]@{
    Name=$TaskName; Description=$Description; Action=$Action; Trigger=$Trigger
    Settings=$Settings; Principal=$Principal; Force=[bool]$Force
  }
  [pscustomobject]@{TaskName=$TaskName}
}
function Disable-ScheduledTask { param($TaskName) $global:disabled += $TaskName }
& $args[0] -EnvironmentFile $args[1] -Database $args[2]
[pscustomobject]@{
  Registrations=$global:registrations
  Disabled=$global:disabled
} | ConvertTo-Json -Depth 8 -Compress
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(wrapper),
            str(repo / "deploy" / "omen" / "setup-windows.ps1"),
            str(env_file),
            str(database),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(repo / "src")},
    )

    assert completed.returncode == 0, completed.stderr
    installation = json.loads(completed.stdout.splitlines()[-1])
    registrations = installation["Registrations"]
    assert {item["Name"] for item in registrations} == {
        "Elrond Console",
        "Elrond Daemon",
        "Elrond Protection",
        "Elrond Ingestion",
        "Elrond Research",
        "Elrond Report",
    }
    for item in registrations:
        assert item["Settings"]["MultipleInstances"] == "IgnoreNew"
        assert item["Principal"]["LogonType"] == "Interactive"
        assert item["Principal"]["RunLevel"] == "Limited"
        assert item["Action"]["Execute"].endswith("pythonw.exe")
        assert "quantbot.operations.service" in item["Action"]["Argument"]
        assert len(item["Trigger"]) == 2
    daemon = next(item for item in registrations if item["Name"] == "Elrond Daemon")
    assert daemon["Settings"]["ExecutionTimeLimit"]["TotalSeconds"] == 0
    assert set(installation["Disabled"]) == {
        "Elrond Daemon",
        "Elrond Protection",
        "Elrond Ingestion",
        "Elrond Report",
    }

    refusal_wrapper = tmp_path / "refuse-existing.ps1"
    refusal_wrapper.write_text(
        """
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
$PSModuleAutoLoadingPreference = 'None'
function Get-ScheduledTask {
  param([string]$TaskName, [string]$ErrorAction)
  [pscustomobject]@{TaskName=$TaskName;Description='created by someone else'}
}
function New-ScheduledTaskAction { throw 'TEST_DOUBLE_LEAK' }
function New-ScheduledTaskTrigger { throw 'TEST_DOUBLE_LEAK' }
function New-ScheduledTaskSettingsSet { throw 'TEST_DOUBLE_LEAK' }
function New-ScheduledTaskPrincipal { throw 'TEST_DOUBLE_LEAK' }
function Register-ScheduledTask { throw 'TEST_DOUBLE_LEAK' }
function Disable-ScheduledTask { throw 'TEST_DOUBLE_LEAK' }
& $args[0] -EnvironmentFile $args[1] -Database $args[2] -UpdateManaged
""",
        encoding="utf-8",
    )
    refused = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(refusal_wrapper),
            str(repo / "deploy" / "omen" / "setup-windows.ps1"),
            str(env_file),
            str(database),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(repo / "src")},
    )
    assert refused.returncode != 0
    assert "TASK_NOT_MANAGED_CONSOLE" in refused.stderr

    running_wrapper = tmp_path / "refuse-running.ps1"
    running_wrapper.write_text(
        """
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
$PSModuleAutoLoadingPreference = 'None'
function Get-ScheduledTask {
  param([string]$TaskName, [string]$ErrorAction)
  [pscustomobject]@{
    TaskName=$TaskName
    Description='Managed by project_elrond deploy/omen/setup-windows.ps1.'
    State='Running'
  }
}
function New-ScheduledTaskAction { throw 'TEST_DOUBLE_LEAK' }
function New-ScheduledTaskTrigger { throw 'TEST_DOUBLE_LEAK' }
function New-ScheduledTaskSettingsSet { throw 'TEST_DOUBLE_LEAK' }
function New-ScheduledTaskPrincipal { throw 'TEST_DOUBLE_LEAK' }
function Register-ScheduledTask { throw 'TEST_DOUBLE_LEAK' }
function Disable-ScheduledTask { throw 'TEST_DOUBLE_LEAK' }
& $args[0] -EnvironmentFile $args[1] -Database $args[2] -UpdateManaged
""",
        encoding="utf-8",
    )
    running = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(running_wrapper),
            str(repo / "deploy" / "omen" / "setup-windows.ps1"),
            str(env_file),
            str(database),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(repo / "src")},
    )
    assert running.returncode != 0
    assert "RUNNING_TASK_MUST_STOP_CONSOLE" in running.stderr
