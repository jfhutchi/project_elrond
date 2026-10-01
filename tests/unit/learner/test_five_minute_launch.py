"""Synthetic package fixtures: no real providers, accounts or research charges."""

import importlib
import json
import sqlite3
from datetime import timedelta

import pytest

from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.learner.forward import _save
from quantbot.research.manifest import content_id
from tests.unit.learner.test_five_minute import OPEN
from tests.unit.learner.test_five_minute_forward import model


def module():
    try:
        return importlib.import_module("quantbot.learner.five_minute_launch")
    except ModuleNotFoundError:
        pytest.fail("bounded frozen observation launcher is missing")


@pytest.fixture
def package(tmp_path):
    frozen = tmp_path / "frozen"
    spec = dict(contract="five-minute-price-alpha-v1", trial_budget=2, search_allowed=False)
    _save(frozen / "preregistration.json", spec)
    identities = {}
    trials = {}
    models = {}
    from quantbot.learner.estimators import fit_estimator

    for index, family in enumerate(("mean", "ridge")):
        m = model()
        m["artifact"] = fit_estimator(
            ((0.0,) * 10, (1.0,) * 10), (0.0, 0.0), FEATURE_NAMES, family=family, alpha=1.0, seed=0
        ).model_dump(mode="json")
        m["research_only_not_runtime_compatible"] = True
        identities[family] = content_id(m)
        trials[family] = "intraday:dia-five-minute-" + str(index) * 64
        models[family] = m
        _save(frozen / "models" / (identities[family] + ".json"), m)
    result = dict(
        spec_hash=content_id(spec),
        frozen_data_identity="e" * 64,
        models={k: dict(model_id=v) for k, v in identities.items()},
        trial_ids=trials,
    )
    _save(frozen / "result.json", result)
    registry = tmp_path / "operations.db"
    with sqlite3.connect(registry) as c:
        c.execute(
            "create table search_runs(search_id text, candidates_evaluated integer, "
            "configuration_hash text, detail_json text, started_at text)"
        )
        for family in identities:
            c.execute(
                "insert into search_runs values (?,?,?,?,?)",
                (
                    trials[family],
                    1,
                    content_id(["e" * 64, family]),
                    json.dumps(dict(phase="PRE_ENGINE")),
                    (OPEN - timedelta(days=2)).isoformat(),
                ),
            )
    declarations = tmp_path / "declarations.db"
    declarations.write_bytes(b"never opened by check")
    clock = tmp_path / "monitor" / "receipt.json"
    clock.parent.mkdir()
    clock.write_text(json.dumps(dict(measured_at=OPEN.isoformat(), clock_status="HEALTHY")))
    config = dict(
        root=str(tmp_path / "observations"),
        frozen=str(frozen),
        operations_database=str(registry),
        declaration_database=str(declarations),
        clock_receipt=str(clock),
        code_commit="a" * 40,
        spec_hash=content_id(spec),
        frozen_data_identity="e" * 64,
        mean_model=identities["mean"],
        ridge_model=identities["ridge"],
        mean_trial=trials["mean"],
        ridge_trial=trials["ridge"],
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    env = tmp_path / "paper.env"
    env.write_text(
        "TRADING_MODE=PAPER\nBROKER_ENVIRONMENT=PAPER\nLIVE_TRADING_ACKNOWLEDGED=false\n"
    )
    return path, config, env


def test_check_validates_frozen_charges_without_provider_or_output_writes(package, monkeypatch):
    launcher = module()
    path, config, env = package
    monkeypatch.setattr(launcher, "_git", lambda *a: "a" * 40 if a[-1] == "HEAD" else "")
    monkeypatch.setattr(launcher, "run_once", lambda *a, **k: pytest.fail("check executed tick"))
    before = path.parent.joinpath("operations.db").read_bytes()
    assert launcher.main(["--config", str(path), "--env-file", str(env), "--check"]) == 0
    assert not path.parent.joinpath("observations").exists()
    assert path.parent.joinpath("operations.db").read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["pin", "dirty", "live", "model", "charge", "charge-after-fit", "scope"]
)
def test_invalid_package_refuses_before_any_tick(package, monkeypatch, fault):
    launcher = module()
    path, config, env = package
    monkeypatch.setattr(
        launcher,
        "_git",
        lambda *a: (
            ("b" * 40 if fault == "pin" else "a" * 40)
            if a[-1] == "HEAD"
            else ("M unsafe" if fault == "dirty" else "")
        ),
    )
    if fault == "live":
        env.write_text("TRADING_MODE=LIVE\nLIVE_TRADING_ACKNOWLEDGED=true\n")
    elif fault == "scope":
        config["root"] = config["frozen"]
        path.write_text(json.dumps(config))
    elif fault == "model":
        model_path = path.parent / "frozen" / "models" / (config["mean_model"] + ".json")
        model_path.write_text("{}")
    elif fault in ("charge", "charge-after-fit"):
        with sqlite3.connect(config["operations_database"]) as c:
            if fault == "charge":
                c.execute("update search_runs set candidates_evaluated=0")
            else:
                c.execute("update search_runs set started_at=?", (OPEN.isoformat(),))
    monkeypatch.setattr(launcher, "run_once", lambda *a, **k: pytest.fail("invalid tick executed"))
    assert launcher.main(["--config", str(path), "--env-file", str(env), "--check"]) == 2
    assert not path.parent.joinpath("observations").exists()


@pytest.mark.parametrize(
    "age,status,healthy",
    [(0, "HEALTHY", True), (301, "HEALTHY", False), (-1, "HEALTHY", False), (0, "UNKNOWN", False)],
)
def test_clock_gate_never_accepts_old_future_or_unverified_receipt(package, age, status, healthy):
    launcher = module()
    path, config, env = package
    receipt = path.parent / "monitor" / "receipt.json"
    receipt.write_text(
        json.dumps(
            dict(measured_at=(OPEN - timedelta(seconds=age)).isoformat(), clock_status=status)
        )
    )
    assert launcher.clock_healthy(receipt, OPEN) is healthy


def test_generated_task_is_limited_without_policy_or_existing_task_mutation(package):
    launcher = module()
    from xml.etree import ElementTree as ET

    xml = launcher.task_xml(
        repository=r"D:\Elrond\releases\five-observer-test",
        config=r"D:\Elrond\five-minute-observer\config.json",
        env_file=r"C:\Users\leepa\.config\quantbot\quantbot.env",
        host=r"D:\Elrond\bin\ElrondServiceHost.exe",
        log=r"D:\Elrond\five-minute-observer\logs\host.log",
        user_id="S-1-5-21-fixture",
        commit="a" * 40,
    )
    root = ET.fromstring(xml)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:RunLevel", ns).text == "LeastPrivilege"
    assert root.find(".//t:LogonType", ns).text == "InteractiveToken"
    assert root.find(".//t:Interval", ns).text == "PT10M"
    assert root.find(".//t:Duration", ns).text == "PT6H40M"
    assert root.find(".//t:StartBoundary", ns).text.endswith("T09:31:05")
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT2M"
    args = root.find(".//t:Arguments", ns).text
    assert "five_minute_launch" in args
    assert "ExecutionPolicy" not in args and "Password" not in xml
    assert "Register-ScheduledTask" not in xml
    assert root.find(".//t:MultipleInstancesPolicy", ns).text == "IgnoreNew"


async def test_failed_tick_publishes_safe_failure_not_stale_success(package, monkeypatch):
    launcher = module()
    path, config, _ = package
    cfg = launcher.FiveMinuteLaunchConfig.model_validate(config)
    from quantbot.config import Settings

    async def failure(*a, **k):
        raise ValueError("sensitive provider response must not be rendered")

    monkeypatch.setattr(launcher, "observe_tick", failure)
    receipt = await launcher.run_once(cfg, Settings(), launcher.bound_models(cfg))
    assert receipt["status"] == "OBSERVATION_FAILED"
    assert receipt["orders"] == receipt["trained"] == 0
    assert "sensitive" not in json.dumps(receipt)
    latest = json.loads((cfg.root / "receipt.json").read_text())
    assert latest["status"] == "OBSERVATION_FAILED"
    assert len(list((cfg.root / "receipts").glob("*.json"))) == 1


async def test_tick_binds_original_guards_and_preserves_authoritative_files(package, monkeypatch):
    launcher = module()
    path, config, _ = package
    cfg = launcher.FiveMinuteLaunchConfig.model_validate(config)
    from quantbot.config import Settings
    from quantbot.learner.forward import _DECLARATIONS, _REGISTRY

    original = [p.read_bytes() for p in (cfg.operations_database, cfg.declaration_database)]

    async def observe(root, settings, models, *, guard, clock_health):
        assert _REGISTRY.get() == cfg.operations_database
        assert _DECLARATIONS.get() == cfg.declaration_database
        assert callable(guard) and clock_health(OPEN)
        return dict(status="OBSERVING", orders=0, trained=0, predictions=0, outcomes=0)

    monkeypatch.setattr(launcher, "observe_tick", observe)
    receipt = await launcher.run_once(cfg, Settings(), launcher.bound_models(cfg))
    assert receipt["scope"] == "OBSERVATION_ONLY"
    assert original == [p.read_bytes() for p in (cfg.operations_database, cfg.declaration_database)]


async def test_duplicate_tick_cannot_enter_capture(package, monkeypatch):
    launcher = module()
    path, config, _ = package
    cfg = launcher.FiveMinuteLaunchConfig.model_validate(config)
    from quantbot.config import Settings
    from quantbot.storage import AlreadyLockedError, SingleWriterLock

    cfg.root.mkdir()

    async def forbidden(*a, **k):
        pytest.fail("duplicate capture ran")

    monkeypatch.setattr(launcher, "observe_tick", forbidden)
    with SingleWriterLock(cfg.root / "observer.lock"):
        with pytest.raises(AlreadyLockedError):
            await launcher.run_once(cfg, Settings(), launcher.bound_models(cfg))


def test_native_action_reports_missing_interpreter_as_failure(tmp_path):
    import os
    import subprocess
    from xml.etree import ElementTree as ET

    if os.name != "nt":
        pytest.skip("native Windows command acceptance")
    launcher = module()
    xml = launcher.task_xml(
        repository=str(tmp_path / "absent-release"),
        config=str(tmp_path / "config.json"),
        env_file=str(tmp_path / "profile.env"),
        host=str(tmp_path / "host.exe"),
        log=str(tmp_path / "host.log"),
        user_id="fixture",
        commit="a" * 40,
    )
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    arguments = ET.fromstring(xml).find(".//t:Arguments", ns).text
    command = arguments.split('-Command "', 1)[1][:-1]
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, "missing interpreter incorrectly reported a successful tick"
