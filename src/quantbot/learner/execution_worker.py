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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantbot.config import Settings
from quantbot.domain import BrokerEnvironment, TradingMode
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, ForwardConfig, _guard, _models, _read
from quantbot.learner.intraday import IntradayModel, IntradayPrediction
from quantbot.learner.paper import assess_paper_entry
from quantbot.learner.paper_runner import (
    PaperExecutionNotConfigured,
    PaperExecutionPolicy,
    PaperModelView,
    build_intraday_paper_services,
    paper_model_binding,
)
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.storage import Database
from quantbot.strategy import StrategyConfig


class PaperWorkerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    mode: Literal["PAPER"] = "PAPER"
    model_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    operational_database: Path
    risk_per_trade_bps: int = Field(default=25, gt=0, le=25)
    max_position_value_bps: int = Field(default=500, gt=0, le=500)
    policy: PaperExecutionPolicy = Field(default_factory=PaperExecutionPolicy)
    experimental_symbol: Literal["DIA"] | None = None
    contract: Literal["one-minute-price-alpha-v1", "five-minute-price-alpha-v1"] = (
        "one-minute-price-alpha-v1"
    )
    five_minute_config: Path | None = None
    five_minute_config_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected_binding_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_contract(self) -> PaperWorkerConfig:
        fields = (
            self.five_minute_config,
            self.five_minute_config_sha256,
            self.expected_binding_hash,
        )
        if self.contract == "five-minute-price-alpha-v1":
            if (
                any(value is None for value in fields)
                or self.five_minute_config is None
                or not self.five_minute_config.is_absolute()
                or not self.operational_database.is_absolute()
                or self.experimental_symbol != "DIA"
                or not self.policy.exclusive_learner_entry
                or self.policy.inherited is None
                or self.policy.stop_loss_bps > 100
            ):
                raise ValueError("five-minute requires exact source, binding, baseline and limits")
        elif any(value is not None for value in fields):
            raise ValueError("one-minute config cannot borrow five-minute sources")
        return self


def worker_risk_config(
    base: StrategyConfig,
    config: PaperWorkerConfig,
    model: PaperModelView,
) -> StrategyConfig:
    """An isolated DIA roster; all other risk controls remain the base runtime's."""
    updates: dict[str, Any] = {
        "risk_per_trade_bps": min(config.risk_per_trade_bps, base.risk_per_trade_bps),
        "max_position_value_bps": min(config.max_position_value_bps, base.max_position_value_bps),
    }
    if config.experimental_symbol is not None:
        if (
            model.model_id != config.model_id
            or model.training_symbols != (config.experimental_symbol,)
            or not config.policy.exclusive_learner_entry
            or config.policy.inherited is None
        ):
            raise ValueError("isolated DIA pilot requires exact roster, baseline and exclusivity")
        updates.update(universe=("DIA", "SPY"), roster_size=1)
    planned = base.model_copy(update=updates)
    return type(base).model_validate(planned.model_dump())


def load_cohort(
    directory: Path,
    model_id: str,
    *,
    now: datetime,
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
            p.model_id == model_id
            and p.recorded_at <= now < p.features.target_at
            and timedelta(0) <= now - p.features.feature_at <= timedelta(seconds=120)
            for p in rows
        ):
            cohorts.append(rows)
    if not cohorts:
        return None
    return model, max(cohorts, key=lambda rows: rows[0].recorded_at)


async def execute_paper_tick(
    directory: Path,
    learner_database: Database,
    settings: Settings,
    *,
    code_commit: str,
) -> dict[str, Any]:
    """One ordinary locked execution cycle after this worker's forecast capture.

    No promotion, migrations, catch-up orders or synthetic cohort generation.
    Runtime paths must identify the existing registry/store exactly.
    """
    config_path = directory / "paper-execution.json"
    if not config_path.is_file():
        return {
            "status": "NOT_CONFIGURED",
            "trading_enabled": False,
            "blockers": ["PAPER_EXECUTION_NOT_CONFIGURED"],
        }
    config = PaperWorkerConfig.model_validate_json(config_path.read_text(encoding="utf-8-sig"))
    if (
        settings.TRADING_MODE is not TradingMode.PAPER
        or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
    ):
        raise ValueError("PAPER worker cannot construct a live runtime")
    if config.contract == "five-minute-price-alpha-v1":
        from quantbot.learner.five_minute_entry import execute_five_minute_tick

        return await execute_five_minute_tick(
            directory, learner_database, settings, config=config, code_commit=code_commit
        )
    forward = ForwardConfig.model_validate_json(
        (directory / "forward.json").read_text(encoding="utf-8-sig")
    )
    if config.experimental_symbol is not None and (
        forward.symbols != (config.experimental_symbol,)
        or forward.experiment_id is None
        or forward.declaration_database is None
        or not forward.declaration_database.is_absolute()
        or not forward.declaration_database.is_file()
        or forward.declaration_database.resolve() == learner_database.path.resolve()
    ):
        raise ValueError("isolated DIA requires an existing declaration store and trial scope")
    from quantbot.runtime import build_runtime, runtime_paths

    paths = runtime_paths()
    if (
        forward.registry_database is None
        or not config.operational_database.is_absolute()
        or paths.database.resolve() != config.operational_database.resolve()
        or forward.registry_database.resolve() != config.operational_database.resolve()
    ):
        raise ValueError("PAPER worker store/registry binding mismatch")
    now = datetime.now(UTC)
    cohort = load_cohort(directory, config.model_id, now=now)
    if cohort is None:
        return await _waiting_setup(
            directory, learner_database, settings, config, code_commit=code_commit
        )
    model, predictions = cohort
    token = _REGISTRY.set(config.operational_database)
    declared = _DECLARATIONS.set(forward.declaration_database)
    try:
        if not _guard(learner_database, model.training_start, model.validation_end) or not _guard(
            learner_database,
            min(p.features.feature_at for p in predictions) - timedelta(minutes=31),
            max(p.features.target_at for p in predictions),
        ):
            return {
                "status": "BLOCKED",
                "trading_enabled": False,
                "blockers": ["PROTECTED_FEATURE_WINDOW"],
            }
    finally:
        _REGISTRY.reset(token)
        _DECLARATIONS.reset(declared)
    runtime = build_runtime(settings)
    try:
        if runtime.running_git_commit != code_commit:
            raise ValueError("PAPER worker checkout identity mismatch")
        runtime.config = worker_risk_config(runtime.config, config, model)
        try:
            services = build_intraday_paper_services(
                runtime,
                model=model,
                predictions=predictions,
                policy=config.policy,
                clock=lambda: datetime.now(UTC),
            )
        except PaperExecutionNotConfigured:
            return {
                "status": "BLOCKED",
                "trading_enabled": False,
                "blockers": ["PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED"],
            }
        eligibility = assess_paper_entry(
            runtime.database.path,
            binding=services.runner.binding,
            identity=services.runner._identity,
        )
        if not eligibility.allowed:
            return {
                "status": "BLOCKED",
                "trading_enabled": False,
                "gate_stage": eligibility.stage,
                "blockers": list(eligibility.reasons),
            }
        # Do not refresh the heartbeat here: entry cannot prove independent exits.
        run_id = "learner-paper-" + str(uuid4())
        services.runner.bind_run(run_id)
        result = await services.cycle.run(
            run_id, started_at=now, trading_date=now.astimezone(XNYS_TIMEZONE).date()
        )
        return {
            "status": result.status,
            "trading_enabled": result.status == "SUCCEEDED" and result.orders_submitted > 0,
            "orders_submitted": result.orders_submitted,
            "run_id": run_id,
            "promotion": eligibility.stage,
            "mode": "PAPER",
            "binding": services.runner.binding.model_dump(mode="json"),
            "identity": services.runner._identity.model_dump(mode="json"),
            "cleared_gate_codes": ["PAPER_PROMOTION_REQUIRED", "PAPER_EXECUTION_NOT_CONFIGURED"],
            "blockers": list(dict.fromkeys((*result.reasons, *services.runner.last_reasons))),
        }
    finally:
        await runtime.aclose()
        runtime.database.close()


async def _waiting_setup(
    directory: Path,
    learner: Database,
    settings: Settings,
    config: PaperWorkerConfig,
    *,
    code_commit: str,
) -> dict[str, Any]:
    """Read real commissioning independently of session/cohort availability; no orders."""
    from quantbot.risk.policy_versions import recorded_policy_version
    from quantbot.runtime import build_runtime
    from quantbot.storage import StorageRepository

    if not settings.EXPECTED_ACCOUNT_ID:
        return dict(
            status="BLOCKED",
            trading_enabled=False,
            blockers=["PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED"],
        )
    model = next(
        (m for m in _models(directory / "intraday") if m.model_id == config.model_id), None
    )
    if model is None:
        return dict(status="BLOCKED", trading_enabled=False, blockers=["PAPER_MODEL_UNAVAILABLE"])
    forward = ForwardConfig.model_validate_json((directory / "forward.json").read_text())
    token = _REGISTRY.set(config.operational_database)
    declared = _DECLARATIONS.set(forward.declaration_database)
    try:
        if not _guard(learner, model.training_start, model.validation_end):
            return dict(
                status="BLOCKED", trading_enabled=False, blockers=["PROTECTED_FEATURE_WINDOW"]
            )
    finally:
        _REGISTRY.reset(token)
        _DECLARATIONS.reset(declared)
    runtime = build_runtime(settings)
    try:
        if runtime.running_git_commit != code_commit:
            raise ValueError("PAPER waiting checkout identity mismatch")
        if config.policy.buying_power_reserve_bps != settings.ENTRY_BUYING_POWER_RESERVE_BPS:
            raise ValueError("PAPER waiting reserve mismatch")
        runtime.config = worker_risk_config(runtime.config, config, model)
        with runtime.database.transaction() as session:
            repository = StorageRepository(session)
            version = recorded_policy_version(repository)
            if version is None:
                return dict(
                    status="BLOCKED",
                    trading_enabled=False,
                    blockers=["PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED"],
                )
            binding = paper_model_binding(
                model,
                config=runtime.config,
                policy=config.policy,
                veto_policy=version.policy(),
                market_data=runtime.market_data_config,
            )
            provisional = binding.strategy_identity(
                git_commit=code_commit, deployed_at=model.created_at
            )
            identity = repository.get_strategy_deployment(
                provisional.strategy_id, provisional.version
            )
        if identity is None or identity.git_commit != code_commit:
            return dict(
                status="BLOCKED",
                trading_enabled=False,
                blockers=["PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED"],
            )
        eligibility = assess_paper_entry(runtime.database.path, binding=binding, identity=identity)
        if not eligibility.allowed:
            return dict(status="BLOCKED", trading_enabled=False, blockers=list(eligibility.reasons))
        return dict(
            status="WAITING",
            trading_enabled=False,
            orders_submitted=0,
            mode="PAPER",
            promotion=eligibility.stage,
            binding=binding.model_dump(mode="json"),
            identity=identity.model_dump(mode="json"),
            blockers=["PAPER_CURRENT_COHORT_UNAVAILABLE"],
            cleared_gate_codes=["PAPER_PROMOTION_REQUIRED", "PAPER_EXECUTION_NOT_CONFIGURED"],
        )
    finally:
        await runtime.aclose()
        runtime.database.close()


async def reconcile_deadline_fills(runtime: Any, now: datetime) -> tuple[str, ...]:
    """Refresh confirmed fill state without order authority or stop clearing.

    A latched entry stop is preserved. Acknowledgement means only that strict
    broker-ledger reconciliation and independent protection both succeeded.
    """
    from dataclasses import replace

    if (runtime.settings.TRADING_MODE is not TradingMode.PAPER
            or runtime.settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER):
        raise ValueError("post-fill synchronization requires PAPER runtime")
    recovery = await runtime.recovery.recover(
        "paper-fill-reconciliation-" + str(uuid4()), started_at=now,
    )
    if (not recovery.reconciliation.allows_new_orders
            or not recovery.broker_health.healthy
            or recovery.unresolved_intent_ids
            or (not recovery.ready and set(recovery.reasons) != {"KILL_SWITCH_ENGAGED"})):
        return ("POST_FILL_RECONCILIATION_REQUIRED",)
    assessment = await replace(runtime.protector(), submitter=None).evaluate(
        now=datetime.now(UTC),
    )
    if assessment.outcome != "HEALTHY" or assessment.unprotected_count:
        return ("POST_FILL_PROTECTION_REQUIRED",)
    return ()


async def serve_deadlines(
    settings: Settings, *, clock_contract: Literal["STRICT_V0", "RELATIVE_V1"] = "STRICT_V0"
) -> None:
    """Independent risk-reducing loop; deliberately never builds an entry runner."""
    if (
        settings.TRADING_MODE is not TradingMode.PAPER
        or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
        or not settings.EXPECTED_ACCOUNT_ID
    ):
        raise ValueError("independent PAPER exits require verified PAPER settings")
    from quantbot.learner.paper_deadlines import PaperDeadlineSupervisor
    from quantbot.runtime import build_runtime

    runtime = build_runtime(settings)
    try:
        supervisor = PaperDeadlineSupervisor(
            database=runtime.database,
            broker=runtime.broker,
            coordinator=runtime.coordinator,
            identity=runtime.identity,
            expected_account_id=settings.EXPECTED_ACCOUNT_ID,
            lock_path=runtime.paths.lock,
            reconcile_fills=lambda now: reconcile_deadline_fills(runtime, now),
            clock_contract=clock_contract,
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
    parser.add_argument('--clock-contract',choices=('STRICT_V0','RELATIVE_V1'),default='STRICT_V0')
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
            if (
                not args.database.is_absolute()
                or not args.database.is_file()
                or runtime_paths().database.resolve() != args.database.resolve()
                or settings.TRADING_MODE is not TradingMode.PAPER
                or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
                or not settings.EXPECTED_ACCOUNT_ID
            ):
                raise ValueError("independent deadline PAPER/database binding mismatch")
            if args.check:
                print("PAPER_DEADLINE_LAUNCH_CHECK_OK")
            else:
                if args.clock_contract=='RELATIVE_V1':
                    asyncio.run(serve_deadlines(settings,clock_contract='RELATIVE_V1'))
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
