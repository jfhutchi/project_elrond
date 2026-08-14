from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.brokers import BrokerHealth
from quantbot.domain import (
    BrokerOrder,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    ReconciliationStatus,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.execution import ReconciliationResult, StartupRecoveryResult, SubmissionResult
from quantbot.operations.cycle import (
    CycleStrategyResult,
    DataSyncResult,
    RunOnceCycle,
    SubmissionRequest,
)
from quantbot.operations.kill_switch import KillSwitchController, ReadinessEvidence
from quantbot.operations.metrics import OperationalMetrics
from quantbot.risk import DrawdownState, OrderPurpose
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 14, 21, 5, tzinfo=UTC)


def _identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_id="adaptive-momentum-v1-test",
        version="1.0.0",
        git_commit="abc123",
        configuration_hash="a" * 64,
        deployment_timestamp=NOW,
    )


def _recovery(ready: bool = True) -> StartupRecoveryResult:
    reconciliation = ReconciliationResult(
        status=(
            ReconciliationStatus.RECONCILED
            if ready
            else ReconciliationStatus.RECONCILIATION_REQUIRED
        ),
        diffs=(
            ()
            if ready
            else (
                {
                    "diff_id": "diff-1",
                    "category": "ACCOUNT",
                    "key": "cash",
                    "expected": {"value": "100"},
                    "actual": {"value": "90"},
                },
            )
        ),
    )
    return StartupRecoveryResult(
        ready=ready,
        reasons=() if ready else ("RECONCILIATION_REQUIRED",),
        reconciliation=reconciliation,
        resolved_intent_ids=(),
        unresolved_intent_ids=(),
        broker_health=BrokerHealth(
            healthy=True,
            provider="alpaca",
            environment="PAPER",
            account_id="paper-1",
            reasons=(),
        ),
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="client-1",
        strategy_id=_identity().strategy_id,
        symbol="SPY",
        signal_date=date(2026, 8, 14),
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("1"),
        created_at=NOW,
    )


class FakeRecovery:
    def __init__(self, result: StartupRecoveryResult, calls: list[str]) -> None:
        self.result = result
        self.calls = calls

    async def recover(
        self,
        reconciliation_id: str,
        *,
        started_at: datetime,
        run_id: str | None = None,
    ) -> StartupRecoveryResult:
        self.calls.append("recover")
        return self.result


class FakeDataSync:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def sync(self) -> DataSyncResult:
        self.calls.append("sync")
        return DataSyncResult(
            healthy=True,
            completed_through=NOW,
            bars_stored=23,
            stale_symbols=(),
            reasons=(),
        )


class FakeStrategy:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def evaluate(self) -> CycleStrategyResult:
        self.calls.append("strategy")
        return CycleStrategyResult(
            submissions=(
                SubmissionRequest(
                    intent=_intent(),
                    purpose=OrderPurpose.STRATEGY_EXIT,
                    market_eligible=True,
                    drawdown=None,
                    risk_approved=None,
                ),
            ),
            signal_count=1,
        )


class FakeSubmitter:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def submit(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
    ) -> SubmissionResult:
        _ = purpose, market_eligible, drawdown, risk_approved
        self.calls.append("submit")
        accepted = intent.model_copy(update={"state": IntentState.BROKER_ACCEPTED})
        return SubmissionResult(
            intent=accepted,
            broker_order=BrokerOrder(
                broker_order_id="broker-1",
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                quantity=intent.quantity,
                filled_quantity=Decimal("0"),
                status="accepted",
                submitted_at=NOW,
            ),
        )


def _clear(database: Database) -> None:
    KillSwitchController(database).clear(
        reason="test ready",
        evidence=ReadinessEvidence(
            paper_mode=True,
            account_verified=True,
            broker_healthy=True,
            data_healthy=True,
            risk_healthy=True,
            reconciliation_successful=True,
        ),
        updated_at=NOW,
    )


@pytest.mark.asyncio
async def test_run_once_orders_recovery_data_strategy_and_submission(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    metrics = OperationalMetrics()
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "quantbot.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=FakeDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=metrics,
    )

    result = await cycle.run("run-1", started_at=NOW, trading_date=date(2026, 8, 14))

    assert result.status == "SUCCEEDED"
    assert result.orders_submitted == 1
    assert calls == ["recover", "sync", "strategy", "submit"]
    with database.transaction() as session:
        repository = StorageRepository(session)
        stored = repository.get_run("run-1")
        assert stored is not None and stored.status == "SUCCEEDED"
        day = repository.get_qualification_day(_identity().strategy_id, date(2026, 8, 14))
        assert day is not None and day.qualified is True
    assert "quantbot_cycles_total 1" in metrics.render_prometheus()
    database.close()
