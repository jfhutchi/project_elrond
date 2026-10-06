"""Shadow rollout backs up only declared nonsecret configuration and its own store."""

import json
from importlib import import_module

import pytest

from quantbot.learner.experiment_launch import ExperimentLaunchConfig
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.research.manifest import content_id
from tests.fixtures.adaptive_paper import manifest


def setup(tmp_path, monkeypatch):
    launch = import_module("quantbot.learner.experiment_launch")
    frozen = manifest().model_copy(update={"synthetic": False})
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(frozen.model_dump_json())
    config = ExperimentLaunchConfig(
        root=tmp_path / "output",
        store=tmp_path / "output" / "experiment.db",
        manifest_path=manifest_path,
        operations_database=tmp_path / "operations.db",
        declaration_database=tmp_path / "declarations.db",
        clock_receipt=tmp_path / "clock.json",
        code_commit="a" * 40,
        manifest_hash=content_id(frozen),
        environment_file=tmp_path / "private.env",
    )
    config.environment_file.write_text("DO_NOT_COPY_THIS_SECRET")
    path = tmp_path / "config.json"
    path.write_text(config.model_dump_json())
    monkeypatch.setattr(launch, "verify_checkout", lambda config: None)
    monkeypatch.setattr(
        import_module("quantbot.operations.experiment_rollout"),
        "clock_healthy",
        lambda path, now: True,
    )
    return config, path, frozen


def test_shadow_plan_only_has_no_runtime_writes_or_secret_copy(tmp_path, monkeypatch):
    config, path, _ = setup(tmp_path, monkeypatch)
    module = import_module("quantbot.operations.experiment_rollout")
    backup = tmp_path / "backup"
    report = module.prepare_shadow_rollout(path, expected_commit="a" * 40, backup=backup)
    assert report["task_name"] == "Elrond Adaptive Paper Shadow"
    assert report["protection_interruption"] is False
    assert report["operational_writes"] is False
    assert not backup.exists() and not config.store.exists()


def test_backup_keeps_store_evidence_without_any_operational_restore(tmp_path, monkeypatch):
    config, path, frozen = setup(tmp_path, monkeypatch)
    config.root.mkdir()
    store = ExperimentStore(config.store, create=True)
    store.append(frozen, idempotency_key="manifest")
    original = config.store.read_bytes()
    module = import_module("quantbot.operations.experiment_rollout")
    backup = tmp_path / "backup"
    module.prepare_shadow_rollout(path, expected_commit="a" * 40, backup=backup, write_backup=True)
    assert config.store.read_bytes() == original
    assert (backup / "experiment.db").exists()
    assert "DO_NOT_COPY_THIS_SECRET" not in "".join(p.read_text() for p in backup.glob("*.json"))
    assert not (backup / "private.env").exists()
    assert set(json.loads((backup / "rollback.json").read_text())["copied"]) == {
        "config.json",
        "manifest.json",
        "experiment.db",
    }


def test_unhealthy_clock_wrong_pin_or_enabled_fitting_refuses_rollout(tmp_path, monkeypatch):
    config, path, _ = setup(tmp_path, monkeypatch)
    module = import_module("quantbot.operations.experiment_rollout")
    with pytest.raises(ValueError, match="PIN"):
        module.prepare_shadow_rollout(path, expected_commit="b" * 40, backup=tmp_path / "backup")
    launch = import_module("quantbot.learner.experiment_launch")
    monkeypatch.setattr(module, "clock_healthy", lambda path, now: False)
    with pytest.raises(ValueError, match="CLOCK"):
        module.prepare_shadow_rollout(path, expected_commit="a" * 40, backup=tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    sources = dict(
        training_path=tmp_path / "training.json",
        validation_path=tmp_path / "validation.json",
        training_hash="a" * 64,
        validation_hash="b" * 64,
    )
    path.write_text(
        config.model_copy(
            update={"research_sources": launch.ResearchSources(**sources)}
        ).model_dump_json()
    )
    with pytest.raises(ValueError, match="SEPARATE_CHARGED_RESEARCH"):
        module.prepare_shadow_rollout(path, expected_commit="a" * 40, backup=tmp_path / "backup")


def test_backup_cannot_overlap_sources_outputs_or_existing_directory(tmp_path, monkeypatch):
    config, path, _ = setup(tmp_path, monkeypatch)
    module = import_module("quantbot.operations.experiment_rollout")
    for target in (config.root, tmp_path, config.environment_file):
        with pytest.raises(ValueError, match="BACKUP"):
            module.prepare_shadow_rollout(path, expected_commit="a" * 40, backup=target)


def test_powershell_rollout_refuses_existing_task_before_any_registration(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("Windows task wrapper checked on native Windows")
    script = Path(__file__).resolve().parents[3] / "deploy" / "omen" / "adaptive-paper-rollout.ps1"

    def quote(path):
        return "'" + str(path).replace("'", "''") + "'"

    command = (
        """
    function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction)
      if($TaskName -ne 'Elrond Adaptive Paper Shadow'){throw 'UNRELATED_TASK_READ'}
      return [pscustomobject]@{State='Running'}
    }
    function Register-ScheduledTask {throw 'REGISTRATION_FORBIDDEN_IN_TEST'}
    """
        + f"""
    try {{ & {quote(script)} -Repository {quote(tmp_path)} -Config {quote(tmp_path / "config.json")}
       -Commit {"a" * 40} -ServiceHost {quote(tmp_path / "host.exe")}
       -BackupDirectory {quote(tmp_path / "backup")} -Apply
       exit 3
    }} catch {{ if($_.Exception.Message -eq 'ADAPTIVE_TASK_ALREADY_EXISTS_NO_IMPLICIT_UPDATE')
       {{'EXPECTED_REFUSAL';exit 0}};throw }}
    """
    )
    # Keep the script invocation on one line; no continuation or shell interpolation of user input.
    command = command.replace("\n       -", " -")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "EXPECTED_REFUSAL" in result.stdout
    assert not (tmp_path / "backup").exists()


def test_powershell_scripts_parse_without_registration_or_execution():
    import shutil
    import subprocess
    from pathlib import Path

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("Native PowerShell parser unavailable")
    directory = Path(__file__).resolve().parents[3] / "deploy" / "omen"
    for name in (
        "adaptive-paper-rollout.ps1",
        "adaptive-paper-rollback.ps1",
        "start-adaptive-paper.ps1",
    ):
        literal = str(directory / name).replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{literal}',[ref]$tokens,[ref]$errors);"
            "if($errors.Count){$errors.Message;exit 2}"
        )
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_mocked_shadow_rollback_only_stops_its_exact_task_and_keeps_evidence(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("Native Windows rollback command contract")
    backup = tmp_path / "backup"
    backup.mkdir()
    xml = "<Task><Actions><Exec><Command>synthetic-host</Command></Exec></Actions></Task>"
    (backup / "shadow-task.xml").write_text(xml)
    evidence = tmp_path / "experiment.db"
    evidence.write_bytes(b"later-evidence-must-remain")
    script = Path(__file__).resolve().parents[3] / "deploy" / "omen" / "adaptive-paper-rollback.ps1"
    literal = str(script).replace("'", "''")
    backup_literal = str(backup).replace("'", "''")
    marker = "Managed by project_elrond deploy/omen/adaptive-paper-rollout.ps1. Commit=" + "a" * 40
    command = f"""
      function Get-ScheduledTask {{param($TaskName,$TaskPath)
        if($TaskName -ne 'Elrond Adaptive Paper Shadow'){{throw 'UNRELATED_TASK'}}
        [pscustomobject]@{{State='Running';
          Description='{marker}';
          Principal=[pscustomobject]@{{LogonType='Interactive';RunLevel='Limited'}}}}
      }}
      function Export-ScheduledTask {{param($TaskName,$TaskPath) '{xml}'}}
      function Stop-ScheduledTask {{param($TaskName,$TaskPath) "STOP:$TaskName"}}
      function Unregister-ScheduledTask {{param($TaskName,$TaskPath,$Confirm) "REMOVE:$TaskName"}}
      & '{literal}' -Commit {"a" * 40} -BackupDirectory '{backup_literal}' -Apply
    """
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "STOP:Elrond Adaptive Paper Shadow" in result.stdout
    assert "REMOVE:Elrond Adaptive Paper Shadow" in result.stdout
    assert evidence.read_bytes() == b"later-evidence-must-remain"
