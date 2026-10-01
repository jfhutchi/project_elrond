"""Pinned frozen-model observational task. No fitting or trading entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from xml.etree import ElementTree as ET

from pydantic import Field, model_validator

from quantbot.config import Settings
from quantbot.learner.estimators import FittedEstimator
from quantbot.learner.five_minute import CONTRACT, FEATURE_NAMES
from quantbot.learner.five_minute_forward import observe_tick
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, _guard, _read, _save
from quantbot.learner.models import LearnerModel
from quantbot.operations.service import _git, isolated_application_environment
from quantbot.research.manifest import content_id
from quantbot.storage import AlreadyLockedError, Database, SingleWriterLock

HASH = r"^[a-f0-9]{64}$"
TRIAL = r"^intraday:dia-five-minute-[a-f0-9]{64}$"


class FiveMinuteLaunchConfig(LearnerModel):
    root: Path
    frozen: Path
    operations_database: Path
    declaration_database: Path
    clock_receipt: Path
    code_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    spec_hash: str = Field(pattern=HASH)
    frozen_data_identity: str = Field(pattern=HASH)
    mean_model: str = Field(pattern=HASH)
    ridge_model: str = Field(pattern=HASH)
    mean_trial: str = Field(pattern=TRIAL)
    ridge_trial: str = Field(pattern=TRIAL)

    @model_validator(mode="after")
    def isolated(self) -> FiveMinuteLaunchConfig:
        paths = (
            self.root,
            self.frozen,
            self.operations_database,
            self.declaration_database,
            self.clock_receipt,
        )
        if any(not p.is_absolute() for p in paths):
            raise ValueError("absolute paths required")
        root = self.root.resolve()
        for path in paths[1:]:
            source = path.resolve()
            if root == source or root in source.parents or source in root.parents:
                raise ValueError("output must be separate from all authoritative sources")
        if self.mean_model == self.ridge_model or self.mean_trial == self.ridge_trial:
            raise ValueError("distinct frozen families and original charges required")
        return self


def bound_models(config: FiveMinuteLaunchConfig) -> list[dict[str, Any]]:
    spec = _read(config.frozen / "preregistration.json")
    result = _read(config.frozen / "result.json")
    if (
        content_id(spec) != config.spec_hash
        or result["spec_hash"] != config.spec_hash
        or result["frozen_data_identity"] != config.frozen_data_identity
        or spec["contract"] != CONTRACT
        or spec["trial_budget"] != 2
        or spec["search_allowed"] is not False
    ):
        raise ValueError("frozen specification binding refused")
    models = []
    with sqlite3.connect(
        config.operations_database.resolve().as_uri() + "?mode=ro", uri=True
    ) as registry:
        registry.row_factory = sqlite3.Row
        registry.execute("PRAGMA query_only=ON")
        for family, identity, trial in (
            ("mean", config.mean_model, config.mean_trial),
            ("ridge", config.ridge_model, config.ridge_trial),
        ):
            model = _read(config.frozen / "models" / (identity + ".json"))
            estimator = FittedEstimator.model_validate(model["artifact"])
            created = datetime.fromisoformat(model["created_at"])
            if (
                content_id(model) != identity
                or result["models"][family]["model_id"] != identity
                or result["trial_ids"][family] != trial
                or created.utcoffset() is None
                or model["contract"] != CONTRACT
                or estimator.family != family
                or tuple(model["feature_names"]) != FEATURE_NAMES
                or estimator.feature_names != FEATURE_NAMES
                or model["training_symbols"] != ["DIA"]
                or model["research_only_not_runtime_compatible"] is not True
            ):
                raise ValueError("frozen model binding refused")
            charge = registry.execute(
                "SELECT candidates_evaluated,configuration_hash,detail_json,"
                "started_at FROM search_runs WHERE search_id=?",
                (trial,),
            ).fetchone()
            if (
                charge is None
                or charge["candidates_evaluated"] != 1
                or charge["configuration_hash"] != content_id([config.frozen_data_identity, family])
                or json.loads(charge["detail_json"])["phase"] != "PRE_ENGINE"
                or datetime.fromisoformat(charge["started_at"]) > created
            ):
                raise ValueError("original pre-fit canonical charge required")
            models.append(model)
    return models


def clock_healthy(path: Path, now: datetime) -> bool:
    try:
        if path.stat().st_size > 1_000_000:
            return False
        receipt = json.loads(path.read_text())
        stamp = datetime.fromisoformat(receipt["measured_at"])
        return receipt["clock_status"] == "HEALTHY" and 0 <= (now - stamp).total_seconds() <= 300
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def run_once(
    config: FiveMinuteLaunchConfig, settings: Settings, models: list[dict[str, Any]]
) -> dict[str, Any]:
    config.root.mkdir(parents=True, exist_ok=True)
    with SingleWriterLock(config.root / "observer.lock"):
        _save(config.root / "deployment.json", config.model_dump(mode="json"))
        local = (
            Database(config.root / "guard.db")
            if (config.root / "guard.db").exists()
            else Database.create(config.root / "guard.db")
        )
        rt = _REGISTRY.set(config.operations_database)
        dt = _DECLARATIONS.set(config.declaration_database)
        try:
            try:
                receipt = await asyncio.wait_for(
                    observe_tick(
                        config.root,
                        settings,
                        models,
                        guard=lambda start, end: _guard(local, start, end),
                        clock_health=lambda now: clock_healthy(config.clock_receipt, now),
                    ),
                    timeout=90,
                )
            except Exception as error:
                receipt = dict(
                    status="OBSERVATION_FAILED",
                    orders=0,
                    trained=0,
                    error_code="CAPTURE_TIMEOUT"
                    if isinstance(error, TimeoutError)
                    else "CAPTURE_FAILED",
                )
            now = datetime.now(UTC)
            receipt.update(
                finished_at=now.isoformat(),
                code_commit=config.code_commit,
                config_hash=content_id(config.model_dump(mode="json")),
                scope="OBSERVATION_ONLY",
            )
            _save(config.root / "receipts" / (now.strftime("%Y%m%dT%H%M%S%f") + ".json"), receipt)
            temporary = config.root / "receipt.json.tmp"
            temporary.write_text(json.dumps(receipt))
            temporary.replace(config.root / "receipt.json")
            return receipt
        finally:
            _REGISTRY.reset(rt)
            _DECLARATIONS.reset(dt)
            local.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.config.is_absolute() or not args.env_file.is_absolute():
            raise ValueError("absolute configuration required")
        config = FiveMinuteLaunchConfig.model_validate_json(args.config.read_text())
        repository = Path(__file__).resolve().parents[3]
        if _git(repository, "rev-parse", "HEAD") != config.code_commit or _git(
            repository, "status", "--porcelain", "--untracked-files=all"
        ):
            raise ValueError("clean pinned checkout required")
        for source in (config.declaration_database, config.clock_receipt):
            if not source.is_file():
                raise ValueError("existing declaration and clock sources required")
        for source in (args.config, args.env_file, repository):
            if (
                config.root.resolve() == source.resolve()
                or config.root.resolve() in source.resolve().parents
                or source.resolve() in config.root.resolve().parents
            ):
                raise ValueError("output overlaps code or configuration")
        models = bound_models(config)
        with isolated_application_environment(args.env_file):
            settings = Settings()
            if (
                settings.TRADING_MODE.value != "PAPER"
                or settings.BROKER_ENVIRONMENT.value != "PAPER"
                or settings.LIVE_TRADING_ACKNOWLEDGED
            ):
                raise ValueError("PAPER only")
            if args.check:
                print("FIVE_MINUTE_OBSERVATION_CHECK_OK; NO_PROVIDER_CALLS_OR_WRITES")
                return 0
            receipt = asyncio.run(run_once(config, settings, models))
            print(json.dumps(receipt))
            return 2 if receipt["status"] == "OBSERVATION_FAILED" else 0
    except AlreadyLockedError:
        print("FIVE_MINUTE_OBSERVATION_ALREADY_RUNNING")
        return 0
    except Exception as error:
        # Never render provider errors, paths, configuration contents or secrets.
        print("FIVE_MINUTE_OBSERVATION_REFUSED; " + type(error).__name__)
        return 2


def task_xml(
    *,
    repository: str,
    config: str,
    env_file: str,
    host: str,
    log: str,
    user_id: str,
    commit: str,
    start_day: date | None = None,
) -> str:
    """Return a plan only. There is intentionally no registration or elevation API."""
    for path in (repository, config, env_file, host, log):
        if not PureWindowsPath(path).is_absolute() or any(c in path for c in "'\"\r\n\x00"):
            raise ValueError("safe absolute native paths required")
    if not user_id or not re.fullmatch("[a-f0-9]{40}", commit):
        raise ValueError("existing principal and commit required")
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)

    def node(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
        child = ET.SubElement(parent, "{" + namespace + "}" + name)
        child.text = text
        return child

    task = ET.Element("{" + namespace + "}Task", version="1.4")
    registration = node(task, "RegistrationInfo")
    node(registration, "Description", "Elrond frozen five-minute observations only; " + commit)
    node(registration, "URI", "\\Elrond Five Minute Observations")
    triggers = node(task, "Triggers")
    trigger = node(triggers, "CalendarTrigger")
    repeat = node(trigger, "Repetition")
    node(repeat, "Interval", "PT10M")
    node(repeat, "Duration", "PT6H40M")
    node(repeat, "StopAtDurationEnd", "false")
    node(trigger, "StartBoundary", (start_day or date.today()).isoformat() + "T09:31:05")
    node(trigger, "Enabled", "true")
    weekly = node(trigger, "ScheduleByWeek")
    node(weekly, "WeeksInterval", "1")
    days = node(weekly, "DaysOfWeek")
    for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"):
        node(days, day)
    principal = node(node(task, "Principals"), "Principal")
    principal.set("id", "Author")
    node(principal, "UserId", user_id)
    node(principal, "LogonType", "InteractiveToken")
    node(principal, "RunLevel", "LeastPrivilege")
    settings = node(task, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("WakeToRun", "false"),
        ("Enabled", "true"),
        ("ExecutionTimeLimit", "PT2M"),
    ):
        node(settings, name, value)
    actions = node(task, "Actions")
    actions.set("Context", "Author")
    action = node(actions, "Exec")
    node(action, "Command", host)
    source = str(PureWindowsPath(repository) / "src")
    python = str(PureWindowsPath(repository) / ".venv" / "Scripts" / "python.exe")
    command = (
        f"$env:PYTHONPATH='{source}'; $env:PYTHONNOUSERSITE='1'; "
        "Remove-Item Env:PYTHONHOME,Env:PYTHONEXECUTABLE,Env:__PYVENV_LAUNCHER__ "
        "-ErrorAction SilentlyContinue; "
        f"& '{python}' -m quantbot.learner.five_minute_launch "
        f"--config '{config}' --env-file '{env_file}'; exit $LASTEXITCODE"
    )
    node(
        action,
        "Arguments",
        f'"{log}" -NoProfile -NonInteractive -WindowStyle Hidden -Command "{command}"',
    )
    node(action, "WorkingDirectory", repository)
    return ET.tostring(task, encoding="unicode")


if __name__ == "__main__":
    raise SystemExit(main())
