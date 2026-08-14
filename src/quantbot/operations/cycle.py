"""One sequential, single-writer operational paper-trading cycle."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from pydantic import Field

from quantbot.config import SafetyValidation, Settings, validate_trading_safety
from quantbot.domain import BrokerEnvironment, OrderIntent, StrategyIdentity, TradingMode
from quantbot.execution import StartupRecoveryResult, SubmissionResult
from quantbot.operations.health import OperationsModel
from quantbot.operations.kill_switch import (
    KillSwitchController,
    ReadinessEvidence,
)
from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.qualification import QualificationTracker
from quantbot.risk import DrawdownState, OrderPurpose
from quantbot.storage import Database, SingleWriterLock, StorageRepository

PAPER_SMOKE_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_ONE_PAPER_SMOKE_ORDER"


class DataSyncResult(OperationsModel):
    healthy: bool
    completed_through: datetime
    bars_stored: int = Field(ge=0)
    stale_symbols: tuple[str, ...]
    reasons: tuple[str, ...]
    stream_connected: bool = True
    risk_healthy: bool = True


class SubmissionRequest(OperationsModel):
    intent: OrderIntent
    purpose: OrderPurpose
    market_eligible: bool
    drawdown: DrawdownState | None
    risk_approved: bool | None


class CycleStrategyResult(OperationsModel):
    submissions: tuple[SubmissionRequest, ...]
    signal_count: int = Field(ge=0)


class CycleResult(OperationsModel):
    run_id: str
    status: str
    reasons: tuple[str, ...]
    signals_evaluated: int
    orders_submitted: int
    started_at: datetime
    finished_at: datetime


class PaperSmokeAuthorization(OperationsModel):
    authorized: bool
    environment: str
    account_id: str
    validation: SafetyValidation


class RecoveryRunner(Protocol):
    async def recover(
        self,
        reconciliation_id: str,
        *,
        started_at: datetime,
        run_id: str | None = None,
    ) -> StartupRecoveryResult: ...


class DataSyncRunner(Protocol):
    async def sync(self) -> DataSyncResult: ...


class StrategyRunner(Protocol):
    async def evaluate(self) -> CycleStrategyResult: ...


class OrderSubmitter(Protocol):
    def submit(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
    ) -> Awaitable[SubmissionResult]: ...


def authorize_paper_smoke(
    settings: Settings,
    *,
    reported_account_id: str,
    durable_kill_switch_engaged: bool,
    durable_reconciliation_successful: bool,
    readiness_evidence: ReadinessEvidence,
    acknowledgement: str,
) -> PaperSmokeAuthorization:
    """Require a second, exact one-shot acknowledgement and every paper safety gate."""
    if (
        settings.TRADING_MODE is not TradingMode.PAPER
        or settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
    ):
        raise ValueError("paper smoke is available only in paper mode")
    if acknowledgement != PAPER_SMOKE_ACKNOWLEDGEMENT:
        raise ValueError("paper smoke acknowledgement is missing or invalid")
    if not durable_reconciliation_successful:
        raise ValueError("paper smoke requires a durable reconciliation")
    if readiness_evidence.reasons:
        raise ValueError(
            "paper smoke readiness gates failed: " + ", ".join(readiness_evidence.reasons)
        )
    effective = settings.model_copy(
        update={"KILL_SWITCH": settings.KILL_SWITCH or durable_kill_switch_engaged}
    )
    validation = validate_trading_safety(effective, reported_account_id)
    if not validation.allowed:
        raise ValueError("paper smoke safety gates failed: " + ", ".join(validation.reasons))
    return PaperSmokeAuthorization(
        authorized=True,
        environment=BrokerEnvironment.PAPER.value,
        account_id=reported_account_id,
        validation=validation,
    )


class RunOnceCycle:
    def __init__(
        self,
        *,
        database: Database,
        lock_path: str | Path,
        strategy_identity: StrategyIdentity,
        recovery: RecoveryRunner,
        data_sync: DataSyncRunner,
        strategy: StrategyRunner,
        submitter: OrderSubmitter,
        metrics: OperationalMetrics,
        max_data_age_seconds: int = 900,
    ) -> None:
        if max_data_age_seconds <= 0:
            raise ValueError("max_data_age_seconds must be positive")
        self._database = database
        self._lock_path = Path(lock_path)
        self._identity = strategy_identity
        self._recovery = recovery
        self._data_sync = data_sync
        self._strategy = strategy
        self._submitter = submitter
        self._metrics = metrics
        self._max_data_age_seconds = max_data_age_seconds
        self._kill_switch = KillSwitchController(database)
        self._qualification = QualificationTracker(database, strategy_identity.strategy_id)
        self._metrics.increment("quantbot_process_restarts_total")

    @staticmethod
    def _finished_at(started_at: datetime) -> datetime:
        return max(started_at, datetime.now(UTC))

    def _record_incident(
        self,
        run_id: str,
        kind: str,
        *,
        occurred_at: datetime,
        reasons: tuple[str, ...],
        message: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        digest = hashlib.sha256(f"{run_id}:{kind}".encode()).hexdigest()
        with self._database.transaction() as session:
            StorageRepository(session).create_incident(
                f"incident-{digest}",
                severity="ERROR",
                kind=kind,
                message=message,
                occurred_at=occurred_at,
                run_id=run_id,
                detail={"reasons": reasons, **(detail or {})},
            )

    def _finish(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: datetime,
        detail: dict[str, object],
    ) -> None:
        with self._database.transaction() as session:
            StorageRepository(session).finish_run(
                run_id,
                finished_at=finished_at,
                status=status,
                detail=detail,
            )

    def _halt(
        self,
        run_id: str,
        *,
        kind: str,
        reasons: tuple[str, ...],
        started_at: datetime,
        trading_date: date,
    ) -> CycleResult:
        finished_at = self._finished_at(started_at)
        self._kill_switch.engage_hard_failure(kind, updated_at=finished_at)
        self._record_incident(
            run_id,
            kind,
            occurred_at=finished_at,
            reasons=reasons,
            message="Operational cycle halted by a hard safety failure",
        )
        detail = {"reasons": reasons, "signals_evaluated": 0, "orders_submitted": 0}
        self._finish(run_id, status="HALTED", finished_at=finished_at, detail=detail)
        self._qualification.record_real_trading_day(
            trading_date,
            qualified=False,
            detail=detail,
        )
        self._metrics.increment("quantbot_cycle_failures_total")
        return CycleResult(
            run_id=run_id,
            status="HALTED",
            reasons=reasons,
            signals_evaluated=0,
            orders_submitted=0,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def run(
        self,
        run_id: str,
        *,
        started_at: datetime,
        trading_date: date,
    ) -> CycleResult:
        if not run_id or run_id != run_id.strip():
            raise ValueError("run_id must be nonempty without surrounding whitespace")
        created = False
        with SingleWriterLock(self._lock_path):
            try:
                with self._database.transaction() as session:
                    StorageRepository(session).create_run(
                        run_id,
                        self._identity,
                        started_at=started_at,
                        detail={"trading_date": trading_date.isoformat()},
                    )
                created = True
                self._metrics.increment("quantbot_cycles_total")
                recovery = await self._recovery.recover(
                    f"{run_id}:reconciliation",
                    started_at=started_at,
                    run_id=run_id,
                )
                if not recovery.ready:
                    return self._halt(
                        run_id,
                        kind="RECONCILIATION_FAILED",
                        reasons=recovery.reasons,
                        started_at=started_at,
                        trading_date=trading_date,
                    )
                if self._kill_switch.get().engaged:
                    kill_reasons = ("KILL_SWITCH_ENGAGED",)
                    finished_at = self._finished_at(started_at)
                    self._finish(
                        run_id,
                        status="HALTED",
                        finished_at=finished_at,
                        detail={"reasons": kill_reasons},
                    )
                    return CycleResult(
                        run_id=run_id,
                        status="HALTED",
                        reasons=kill_reasons,
                        signals_evaluated=0,
                        orders_submitted=0,
                        started_at=started_at,
                        finished_at=finished_at,
                    )

                data = await self._data_sync.sync()
                data_age = Decimal(
                    str((started_at.astimezone(UTC) - data.completed_through).total_seconds())
                )
                data_reasons_list = list(data.reasons)
                if data_age < 0:
                    data_reasons_list.append("MARKET_DATA_FROM_FUTURE")
                elif data_age > Decimal(self._max_data_age_seconds):
                    data_reasons_list.append("MARKET_DATA_STALE")
                if data.stale_symbols and "MARKET_DATA_STALE" not in data_reasons_list:
                    data_reasons_list.append("MARKET_DATA_STALE")
                if not data.healthy and not data_reasons_list:
                    data_reasons_list.append("MARKET_DATA_UNHEALTHY")
                if not data.stream_connected:
                    data_reasons_list.append("TRADE_STREAM_DISCONNECTED")
                if not data.risk_healthy:
                    data_reasons_list.append("RISK_ENGINE_UNHEALTHY")
                data_reasons = tuple(dict.fromkeys(data_reasons_list))
                self._metrics.set_gauge(
                    "quantbot_market_data_age_seconds",
                    max(data_age, Decimal("0")),
                )
                if data_reasons:
                    kind = data_reasons[0]
                    return self._halt(
                        run_id,
                        kind=kind,
                        reasons=data_reasons,
                        started_at=started_at,
                        trading_date=trading_date,
                    )
                strategy = await self._strategy.evaluate()
                submitted = 0
                for request in strategy.submissions:
                    await self._submitter.submit(
                        request.intent,
                        purpose=request.purpose,
                        market_eligible=request.market_eligible,
                        drawdown=request.drawdown,
                        risk_approved=request.risk_approved,
                    )
                    submitted += 1
                finished_at = self._finished_at(started_at)
                detail: dict[str, object] = {
                    "signals_evaluated": strategy.signal_count,
                    "orders_submitted": submitted,
                    "bars_stored": data.bars_stored,
                }
                self._finish(run_id, status="SUCCEEDED", finished_at=finished_at, detail=detail)
                self._qualification.record_real_trading_day(
                    trading_date,
                    qualified=True,
                    detail=detail,
                )
                self._metrics.increment("quantbot_orders_submitted_total", Decimal(submitted))
                return CycleResult(
                    run_id=run_id,
                    status="SUCCEEDED",
                    reasons=(),
                    signals_evaluated=strategy.signal_count,
                    orders_submitted=submitted,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            except Exception as error:
                finished_at = self._finished_at(started_at)
                self._kill_switch.engage_hard_failure(
                    "UNHANDLED_OPERATIONAL_EXCEPTION",
                    updated_at=finished_at,
                )
                if created:
                    self._record_incident(
                        run_id,
                        "UNHANDLED_OPERATIONAL_EXCEPTION",
                        occurred_at=finished_at,
                        reasons=("UNHANDLED_OPERATIONAL_EXCEPTION",),
                        message="Unhandled exception during operational cycle",
                        detail={"exception_type": type(error).__name__},
                    )
                    self._finish(
                        run_id,
                        status="ERROR",
                        finished_at=finished_at,
                        detail={"reason": "UNHANDLED_OPERATIONAL_EXCEPTION"},
                    )
                self._metrics.increment("quantbot_unhandled_exceptions_total")
                raise
