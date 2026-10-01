"""Pinned native entry consumer; reads frozen observations, never fits or captures.

The --check path is read-only and constructs no broker. Registration, deployment,
consent recording and model promotion are deliberately absent from this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET

from quantbot.config import Settings
from quantbot.domain import BrokerEnvironment, TradingMode
from quantbot.learner.consent import PaperConsent
from quantbot.learner.execution_worker import PaperWorkerConfig, worker_risk_config
from quantbot.learner.five_minute_launch import (
    FiveMinuteLaunchConfig,
    bound_models,
    clock_healthy,
)
from quantbot.learner.five_minute_launch import (
    task_xml as observer_task_xml,
)
from quantbot.learner.five_minute_paper import (
    FiveMinuteModelView,
    build_five_minute_paper_services,
    dia_exposure_classification,
    five_minute_paper_binding,
    load_five_minute_cohort,
)
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, _guard, _read
from quantbot.learner.paper import assess_paper_entry
from quantbot.learner.paper_runner import PaperExecutionNotConfigured
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.operations.service import _git, isolated_application_environment
from quantbot.research.manifest import content_id
from quantbot.risk.policy_versions import recorded_policy_version
from quantbot.storage import Database, StorageRepository


def configured_source(config: PaperWorkerConfig) -> FiveMinuteLaunchConfig:
    path = config.five_minute_config
    if config.contract != "five-minute-price-alpha-v1" or path is None:
        raise ValueError("explicit five-minute config required")
    data = path.read_bytes()
    if len(data) > 100_000 or hashlib.sha256(data).hexdigest() != config.five_minute_config_sha256:
        raise ValueError("observer config identity mismatch")
    source = FiveMinuteLaunchConfig.model_validate_json(data)
    if (
        source.operations_database.resolve() != config.operational_database.resolve()
        or config.model_id not in (source.mean_model, source.ridge_model)
        or source.operations_database.resolve() == source.declaration_database.resolve()
    ):
        raise ValueError("five-minute canonical stores/model mismatch")
    return source


def verified_consent(directory: Path, config: PaperWorkerConfig, now: datetime) -> bool:
    try:
        data = (directory / "paper-consent.json").read_bytes()
        if len(data) > 4096:
            return False
        consent = PaperConsent.model_validate_json(data)
        return consent.model_id == config.model_id and consent.recorded_at <= now
    except (OSError, ValueError):
        return False


def selected_model(source: FiveMinuteLaunchConfig, config: PaperWorkerConfig) -> dict[str, Any]:
    return next(m for m in bound_models(source) if content_id(m) == config.model_id)


def validate_entry_paths(
    directory: Path, learner_path: Path, source: FiveMinuteLaunchConfig
) -> None:
    root = directory.resolve()
    if learner_path.resolve() != (root / "state" / "learner.db").resolve():
        raise ValueError("exact isolated entry guard store required")
    for path in (
        source.root,
        source.frozen,
        source.operations_database,
        source.declaration_database,
        source.clock_receipt,
    ):
        authoritative = path.resolve()
        if root == authoritative or root in authoritative.parents or authoritative in root.parents:
            raise ValueError("entry state overlaps observation/authoritative sources")


def protected_guard(
    learner: Database, source: FiveMinuteLaunchConfig, start: datetime, end: datetime
) -> bool:
    registry = _REGISTRY.set(source.operations_database)
    declarations = _DECLARATIONS.set(source.declaration_database)
    try:
        return _guard(learner, start, end)
    finally:
        _REGISTRY.reset(registry)
        _DECLARATIONS.reset(declarations)


def lineage_allowed(learner: Database, source: FiveMinuteLaunchConfig) -> bool:
    spec = _read(source.frozen / "preregistration.json")
    start = datetime.combine(date.fromisoformat(spec["training_start"]), datetime.min.time(), UTC)
    end = datetime.combine(date.fromisoformat(spec["validation_end"]), datetime.max.time(), UTC)
    return protected_guard(learner, source, start, end)


async def execute_five_minute_tick(
    directory: Path,
    learner: Database,
    settings: Settings,
    *,
    config: PaperWorkerConfig,
    code_commit: str,
) -> dict[str, Any]:
    """Only the ordinary coordinator may submit; original snapshots stay immutable."""

    def blocked(*codes: str) -> dict[str, Any]:
        return dict(
            status="BLOCKED",
            trading_enabled=False,
            orders_submitted=0,
            mode="PAPER",
            blockers=list(codes),
        )

    if (
        settings.TRADING_MODE is not TradingMode.PAPER
        or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
        or settings.LIVE_TRADING_ACKNOWLEDGED
        or not settings.EXPECTED_ACCOUNT_ID
    ):
        return blocked("PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED")
    source = configured_source(config)
    validate_entry_paths(directory, learner.path, source)
    now = datetime.now(UTC)
    if not verified_consent(directory, config, now):
        return blocked("PAPER_OPERATOR_CONSENT_REQUIRED")
    model = selected_model(source, config)
    if not lineage_allowed(learner, source):
        return blocked("PROTECTED_FEATURE_WINDOW")
    if not clock_healthy(source.clock_receipt, now):
        return blocked("PAPER_VERIFIED_CLOCK_UNAVAILABLE")
    cohort = load_five_minute_cohort(source, config.model_id, now=now)
    if cohort is not None and not all(
        protected_guard(
            learner, source, p.features.feature_at - timedelta(minutes=35), p.features.target_at
        )
        for p in cohort.predictions
    ):
        return blocked("PROTECTED_FEATURE_WINDOW")
    from quantbot.runtime import build_runtime, runtime_paths

    if runtime_paths().database.resolve() != config.operational_database.resolve():
        raise ValueError("PAPER worker store/registry binding mismatch")
    runtime = build_runtime(settings)
    try:
        if runtime.running_git_commit != code_commit:
            raise ValueError("PAPER worker checkout identity mismatch")
        if config.policy.buying_power_reserve_bps != settings.ENTRY_BUYING_POWER_RESERVE_BPS:
            return blocked("PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED")
        runtime.config = worker_risk_config(
            runtime.config, config, FiveMinuteModelView(config.model_id)
        )
        classification = dia_exposure_classification()
        with runtime.database.transaction() as session:
            repo = StorageRepository(session)
            policy = recorded_policy_version(repo)
            if policy is None:
                return blocked("PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED")
            binding = five_minute_paper_binding(
                model,
                config=runtime.config,
                policy=config.policy,
                veto_policy=policy.policy(),
                market_data=runtime.market_data_config,
                classification=classification,
            )
            if content_id(binding) != config.expected_binding_hash:
                return blocked("PAPER_EXECUTION_BINDING_OR_FORECAST_CHANGED")
            provisional = binding.strategy_identity(
                git_commit=code_commit, deployed_at=datetime.fromisoformat(model["created_at"])
            )
            identity = repo.get_strategy_deployment(provisional.strategy_id, provisional.version)
        if identity is None or identity.git_commit != code_commit:
            return blocked("PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED")
        eligibility = assess_paper_entry(runtime.database.path, binding=binding, identity=identity)
        if not eligibility.allowed:
            return blocked(*eligibility.reasons)
        common = dict(
            mode="PAPER",
            promotion=eligibility.stage,
            binding=binding.model_dump(mode="json"),
            identity=identity.model_dump(mode="json"),
            worker_config_hash=content_id(config.model_dump(mode="json")),
            cleared_gate_codes=["PAPER_PROMOTION_REQUIRED", "PAPER_EXECUTION_NOT_CONFIGURED"],
        )
        if cohort is None:
            return dict(
                status="WAITING",
                trading_enabled=False,
                orders_submitted=0,
                blockers=["PAPER_CURRENT_COHORT_UNAVAILABLE"],
                **common,
            )
        try:
            services = build_five_minute_paper_services(
                runtime,
                model=model,
                predictions=cohort.predictions,
                snapshot=cohort.snapshot,
                policy=config.policy,
                classification=classification,
                clock=lambda: datetime.now(UTC),
                clock_health=lambda stamp: clock_healthy(source.clock_receipt, stamp),
                holdout_guard=lambda start, end: protected_guard(
                    learner, source, start - timedelta(minutes=35), end
                ),
            )
        except PaperExecutionNotConfigured:
            return blocked("PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED")
        run_id = "learner-five-paper-" + str(uuid4())
        services.runner.bind_run(run_id)
        result = await services.cycle.run(
            run_id, started_at=now, trading_date=now.astimezone(XNYS_TIMEZONE).date()
        )
        return dict(
            status=result.status,
            trading_enabled=result.status == "SUCCEEDED" and result.orders_submitted > 0,
            orders_submitted=result.orders_submitted,
            run_id=run_id,
            blockers=list(dict.fromkeys((*result.reasons, *services.runner.last_reasons))),
            **common,
        )
    finally:
        await runtime.aclose()
        runtime.database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        repository = Path(__file__).resolve().parents[3]
        if (
            not args.directory.is_absolute()
            or not args.env_file.is_absolute()
            or _git(repository, "rev-parse", "HEAD") != args.commit
            or _git(repository, "status", "--porcelain", "--untracked-files=all")
        ):
            raise ValueError("clean pinned checkout and absolute paths required")
        config = PaperWorkerConfig.model_validate_json(
            (args.directory / "paper-execution.json").read_text(encoding="utf-8-sig")
        )
        source = configured_source(config)
        validate_entry_paths(args.directory, args.directory / "state" / "learner.db", source)
        selected_model(source, config)
        if not verified_consent(args.directory, config, datetime.now(UTC)):
            raise ValueError("explicit exact model consent required")
        if not (args.directory / "state" / "learner.db").is_file():
            raise ValueError("existing isolated learner guard store required")
        with isolated_application_environment(args.env_file):
            settings = Settings()
            if (
                settings.TRADING_MODE is not TradingMode.PAPER
                or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
                or settings.LIVE_TRADING_ACKNOWLEDGED
                or not settings.EXPECTED_ACCOUNT_ID
            ):
                raise ValueError("PAPER settings required")
            from quantbot.runtime import runtime_paths

            if runtime_paths().database.resolve() != config.operational_database.resolve():
                raise ValueError("runtime store mismatch")
            if args.check:
                print(
                    "FIVE_MINUTE_ENTRY_CONFIG_CHECK_OK; NO_BROKER_OR_WRITES; "
                    "RUNTIME_ELIGIBILITY_NOT_CHECKED"
                )
                return 0
            # Reuse the ordinary worker's lock, receipts, alerts and immutable consent status;
            # its five-minute branch never invokes one-minute capture/challenger training.
            from quantbot.learner.worker import run_cycle

            result = run_cycle(args.directory, code_commit=args.commit, settings=settings)
            print(json.dumps(result))
            return 2 if result["status"] == "ERROR" else 0
    except Exception:
        print("FIVE_MINUTE_ENTRY_REFUSED")
        return 2


def task_xml(
    *,
    repository: str,
    directory: str,
    env_file: str,
    host: str,
    log: str,
    user_id: str,
    commit: str,
    start_day: date | None = None,
) -> str:
    """Plan only, least privilege, separate entry state, after the observer tick.

    Registering/replacing any task is an independent operator action. Catch-up runs
    cannot bypass the <=120 second entry freshness gate or session calendar.
    """
    document = observer_task_xml(
        repository=repository,
        config=directory,
        env_file=env_file,
        host=host,
        log=log,
        user_id=user_id,
        commit=commit,
        start_day=start_day,
    )
    task = ET.fromstring(document)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    changes = {
        "t:RegistrationInfo/t:Description": "Elrond frozen five-minute PAPER entry; " + commit,
        "t:RegistrationInfo/t:URI": "\\Elrond Five Minute PAPER Entry",
        "t:Triggers/t:CalendarTrigger/t:StartBoundary": (start_day or date.today()).isoformat()
        + "T09:31:35",
    }
    for path, value in changes.items():
        node = task.find(path, ns)
        assert node is not None
        node.text = value
    arguments = task.find("t:Actions/t:Exec/t:Arguments", ns)
    assert arguments is not None and arguments.text is not None
    arguments.text = arguments.text.replace(
        "quantbot.learner.five_minute_launch", "quantbot.learner.five_minute_entry"
    ).replace(f"--config '{directory}'", f"--directory '{directory}' --commit '{commit}'")
    return ET.tostring(task, encoding="unicode")


if __name__ == "__main__":
    raise SystemExit(main())
