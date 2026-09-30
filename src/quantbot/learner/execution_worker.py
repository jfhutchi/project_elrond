"""Explicitly commissioned PAPER worker: consume genuine forecasts, never promote.

Absent configuration constructs no runtime. A configured worker still requires
the exact durable deployment, PAPER_OBSERVATION gate and independent deadline
heartbeat. Challenger training remains separate from model order authority.
"""
from __future__ import annotations

import argparse
import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from quantbot.config import Settings
from quantbot.domain import BrokerEnvironment, TradingMode
from quantbot.learner.forward import _REGISTRY, ForwardConfig, _guard, _models, _read
from quantbot.learner.intraday import IntradayModel, IntradayPrediction
from quantbot.learner.paper import assess_paper_entry
from quantbot.learner.paper_runner import (
    PaperExecutionNotConfigured,
    PaperExecutionPolicy,
    build_intraday_paper_services,
    paper_model_binding,
)
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.storage import Database


class PaperWorkerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["PAPER"] = "PAPER"
    model_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    operational_database: Path
    risk_per_trade_bps: int = Field(default=25, gt=0, le=25)
    max_position_value_bps: int = Field(default=500, gt=0, le=500)
    policy: PaperExecutionPolicy = Field(default_factory=PaperExecutionPolicy)


def load_cohort(
    directory: Path, model_id: str, *, now: datetime,
) -> tuple[IntradayModel, tuple[IntradayPrediction, ...]] | None:
    root = directory / "intraday"
    model = next((item for item in _models(root) if item.model_id == model_id), None)
    if model is None:
        return None
    paths = sorted((root / "predictions").glob("*/*.json"))[-100:]
    cohorts = []
    for path in paths:
        document = _read(path)
        if document["model_id"] != model_id:
            continue
        rows = tuple(IntradayPrediction.model_validate(p) for p in document["predictions"])
        if rows and all(
            p.model_id == model_id and p.recorded_at <= now < p.features.target_at
            and timedelta(0) <= now - p.features.feature_at <= timedelta(seconds=120)
            for p in rows
        ):
            cohorts.append(rows)
    if not cohorts:
        return None
    return model, max(cohorts, key=lambda rows: rows[0].recorded_at)


async def execute_paper_tick(
    directory: Path, learner_database: Database, settings: Settings, *, code_commit: str,
) -> dict[str, Any]:
    """One ordinary locked execution cycle after this worker's forecast capture.

    No promotion, migrations, catch-up orders or synthetic cohort generation.
    Runtime paths must identify the existing registry/store exactly.
    """
    config_path = directory / "paper-execution.json"
    if not config_path.is_file():
        return {"status": "NOT_CONFIGURED", "trading_enabled": False,
                "blockers": ["PAPER_EXECUTION_NOT_CONFIGURED"]}
    config = PaperWorkerConfig.model_validate_json(config_path.read_text(encoding="utf-8-sig"))
    if (settings.TRADING_MODE is not TradingMode.PAPER
            or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER):
        raise ValueError("PAPER worker cannot construct a live runtime")
    forward = ForwardConfig.model_validate_json(
        (directory / "forward.json").read_text(encoding="utf-8-sig")
    )
    from quantbot.runtime import build_runtime, runtime_paths

    paths = runtime_paths()
    if (forward.registry_database is None or not config.operational_database.is_absolute()
            or paths.database.resolve() != config.operational_database.resolve()
            or forward.registry_database.resolve() != config.operational_database.resolve()):
        raise ValueError("PAPER worker store/registry binding mismatch")
    now = datetime.now(UTC)
    cohort = load_cohort(directory, config.model_id, now=now)
    if cohort is None:
        return await _waiting_setup(directory, learner_database, settings, config,
                                    code_commit=code_commit)
    model, predictions = cohort
    token = _REGISTRY.set(config.operational_database)
    try:
        if not _guard(learner_database, model.training_start, model.validation_end) or not _guard(
            learner_database,
            min(p.features.feature_at for p in predictions) - timedelta(minutes=31),
            max(p.features.target_at for p in predictions),
        ):
            return {"status": "BLOCKED", "trading_enabled": False,
                    "blockers": ["PROTECTED_FEATURE_WINDOW"]}
    finally:
        _REGISTRY.reset(token)
    runtime = build_runtime(settings)
    try:
        if runtime.running_git_commit != code_commit:
            raise ValueError("PAPER worker checkout identity mismatch")
        runtime.config = runtime.config.model_copy(update={
            "risk_per_trade_bps": min(config.risk_per_trade_bps, runtime.config.risk_per_trade_bps),
            "max_position_value_bps": min(
                config.max_position_value_bps, runtime.config.max_position_value_bps
            ),
        })
        try:
            services = build_intraday_paper_services(
                runtime, model=model, predictions=predictions, policy=config.policy,
                clock=lambda: datetime.now(UTC),
            )
        except PaperExecutionNotConfigured:
            return {"status": "BLOCKED", "trading_enabled": False,
                    "blockers": ["PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED"]}
        eligibility = assess_paper_entry(
            runtime.database.path, binding=services.runner.binding,
            identity=services.runner._identity,
        )
        if not eligibility.allowed:
            return {"status": "BLOCKED", "trading_enabled": False,
                    "gate_stage": eligibility.stage, "blockers": list(eligibility.reasons)}
        # Do not refresh the heartbeat here: entry cannot prove independent exits.
        run_id = "learner-paper-" + str(uuid4())
        services.runner.bind_run(run_id)
        result = await services.cycle.run(
            run_id, started_at=now, trading_date=now.astimezone(XNYS_TIMEZONE).date()
        )
        return {"status": result.status,
                "trading_enabled": result.status == "SUCCEEDED" and result.orders_submitted > 0,
                "orders_submitted": result.orders_submitted, "run_id": run_id,
                "promotion": eligibility.stage,
                "mode": "PAPER", "binding": services.runner.binding.model_dump(mode="json"),
                "identity": services.runner._identity.model_dump(mode="json"),
                "cleared_gate_codes": ["PAPER_PROMOTION_REQUIRED",
                                       "PAPER_EXECUTION_NOT_CONFIGURED"],
                "blockers": list(dict.fromkeys((*result.reasons, *services.runner.last_reasons)))}
    finally:
        await runtime.aclose()
        runtime.database.close()


async def _waiting_setup(
    directory: Path, learner: Database, settings: Settings, config: PaperWorkerConfig,
    *, code_commit: str,
) -> dict[str, Any]:
    """Read real commissioning independently of session/cohort availability; no orders."""
    from quantbot.risk.policy_versions import recorded_policy_version
    from quantbot.runtime import build_runtime
    from quantbot.storage import StorageRepository

    if not settings.EXPECTED_ACCOUNT_ID:
        return dict(status='BLOCKED', trading_enabled=False,
                    blockers=['PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED'])
    model = next((m for m in _models(directory / 'intraday') if m.model_id == config.model_id),
                 None)
    if model is None:
        return dict(status='BLOCKED', trading_enabled=False, blockers=['PAPER_MODEL_UNAVAILABLE'])
    token = _REGISTRY.set(config.operational_database)
    try:
        if not _guard(learner, model.training_start, model.validation_end):
            return dict(status='BLOCKED', trading_enabled=False,
                        blockers=['PROTECTED_FEATURE_WINDOW'])
    finally:
        _REGISTRY.reset(token)
    runtime = build_runtime(settings)
    try:
        if runtime.running_git_commit != code_commit:
            raise ValueError('PAPER waiting checkout identity mismatch')
        if config.policy.buying_power_reserve_bps != settings.ENTRY_BUYING_POWER_RESERVE_BPS:
            raise ValueError('PAPER waiting reserve mismatch')
        runtime.config = runtime.config.model_copy(update={
            'risk_per_trade_bps': min(config.risk_per_trade_bps, runtime.config.risk_per_trade_bps),
            'max_position_value_bps': min(config.max_position_value_bps,
                                        runtime.config.max_position_value_bps)})
        with runtime.database.transaction() as session:
            repository = StorageRepository(session)
            version = recorded_policy_version(repository)
            if version is None:
                return dict(status='BLOCKED', trading_enabled=False,
                            blockers=['PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED'])
            binding = paper_model_binding(model, config=runtime.config, policy=config.policy,
                veto_policy=version.policy(), market_data=runtime.market_data_config)
            provisional = binding.strategy_identity(git_commit=code_commit,
                                                     deployed_at=model.created_at)
            identity = repository.get_strategy_deployment(provisional.strategy_id,
                                                          provisional.version)
        if identity is None or identity.git_commit != code_commit:
            return dict(status='BLOCKED', trading_enabled=False,
                        blockers=['PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED'])
        eligibility = assess_paper_entry(runtime.database.path, binding=binding, identity=identity)
        if not eligibility.allowed:
            return dict(status='BLOCKED', trading_enabled=False, blockers=list(eligibility.reasons))
        return dict(status='WAITING', trading_enabled=False, orders_submitted=0, mode='PAPER',
                    promotion=eligibility.stage, binding=binding.model_dump(mode='json'),
                    identity=identity.model_dump(mode='json'),
                    blockers=['PAPER_CURRENT_COHORT_UNAVAILABLE'],
                    cleared_gate_codes=['PAPER_PROMOTION_REQUIRED','PAPER_EXECUTION_NOT_CONFIGURED'])
    finally:
        await runtime.aclose()
        runtime.database.close()


async def serve_deadlines(settings: Settings) -> None:
    """Independent risk-reducing loop; deliberately never builds an entry runner."""
    if (settings.TRADING_MODE is not TradingMode.PAPER
            or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
            or not settings.EXPECTED_ACCOUNT_ID):
        raise ValueError("independent PAPER exits require verified PAPER settings")
    from quantbot.learner.paper_deadlines import PaperDeadlineSupervisor
    from quantbot.runtime import build_runtime

    runtime = build_runtime(settings)
    try:
        supervisor = PaperDeadlineSupervisor(
            database=runtime.database, broker=runtime.broker, coordinator=runtime.coordinator,
            identity=runtime.identity, expected_account_id=settings.EXPECTED_ACCOUNT_ID,
            lock_path=runtime.paths.lock,
        )
        await supervisor.serve(asyncio.Event())
    finally:
        await runtime.aclose()
        runtime.database.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent PAPER deadline supervisor")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True, timeout=30
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repository, text=True, timeout=30
        ).strip()
        if commit != args.commit or dirty:
            raise ValueError("independent deadline checkout must match clean pin")
        from quantbot.operations.service import isolated_application_environment
        with isolated_application_environment(args.env_file):
            from quantbot.runtime import runtime_paths

            settings = Settings()
            if (not args.database.is_absolute() or not args.database.is_file()
                    or runtime_paths().database.resolve() != args.database.resolve()
                    or settings.TRADING_MODE is not TradingMode.PAPER
                    or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
                    or not settings.EXPECTED_ACCOUNT_ID):
                raise ValueError('independent deadline PAPER/database binding mismatch')
            if args.check:
                print('PAPER_DEADLINE_LAUNCH_CHECK_OK')
            else:
                asyncio.run(serve_deadlines(settings))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("PAPER_DEADLINE_START_FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
