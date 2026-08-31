"""Independent, durable after-entry position protection (#157).

This module has no strategy, model, forecasting, anomaly, or research dependency. It reconciles
fresh broker state against the latest durable account snapshot, evaluates deterministic protection
rules, and records a measurable result for every broker-open position. This slice decides and
records; exit submission is wired separately through the authoritative execution coordinator.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import select

from quantbot.brokers import BrokerAccountSnapshot, BrokerError
from quantbot.domain import MarketClock, Position, StrategyIdentity
from quantbot.operations.health import OperationsModel
from quantbot.operations.metrics import OperationalMetrics
from quantbot.risk.protection import (
    ProtectionDecision,
    ProtectionPolicy,
    evaluate_position,
)
from quantbot.storage import Database, SingleWriterLock, StorageRepository
from quantbot.storage.database import decode_utc, encode_decimal, encode_utc
from quantbot.storage.schema import (
    position_protection_cycles,
    position_protection_evaluations,
)

PROTECTION_POLICY_VERSION = "position-protection-v1"
POSITION_PROTECTION_INTERVAL = timedelta(minutes=15)


class PositionProtectionBroker(Protocol):
    async def get_account(self) -> BrokerAccountSnapshot: ...

    async def get_positions(self) -> Sequence[Position]: ...


class ProtectionClockProvider(Protocol):
    async def get_market_clock(self) -> MarketClock: ...


class ProtectionStopSignal(Protocol):
    def is_set(self) -> bool: ...


ProtectionSleeper = Callable[[Decimal], Awaitable[None]]
ProtectionEvaluator = Callable[[datetime], Awaitable[object]]


class ProtectionCycleOutcome(StrEnum):
    HEALTHY = "HEALTHY"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    DEGRADED = "DEGRADED"
    INCIDENT = "INCIDENT"


class ProtectionCycleResult(OperationsModel):
    cycle_id: str
    observed_at: datetime
    outcome: ProtectionCycleOutcome
    reason: str = ""
    position_count: int | None
    evaluated_count: int
    unprotected_count: int
    exit_required_count: int


class PositionProtectionDaemonResult(OperationsModel):
    cycles_completed: int
    stopped_cleanly: bool


def _cycle_id(
    moment: datetime,
    *,
    expected_account_id: str,
    strategy_identity: StrategyIdentity,
    policy: ProtectionPolicy,
) -> str:
    payload = {
        "observed_at": encode_utc(moment),
        "expected_account_id": expected_account_id,
        "strategy_id": strategy_identity.strategy_id,
        "strategy_version": strategy_identity.version,
        "configuration_hash": strategy_identity.configuration_hash,
        "policy_version": PROTECTION_POLICY_VERSION,
        "policy_hash": _policy_hash(policy),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"protection-{digest[:32]}"


def _evaluation_id(cycle_id: str, symbol: str) -> str:
    digest = hashlib.sha256(f"{cycle_id}:{symbol}".encode()).hexdigest()
    return f"protection-evaluation-{digest[:24]}"


def _incident_id(cycle_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"{cycle_id}:{kind}".encode()).hexdigest()
    return f"protection-incident-{digest[:24]}"


def _policy_hash(policy: ProtectionPolicy) -> str:
    payload = {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in asdict(policy).items()
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _positions_agree(local: Position | None, broker: Position) -> bool:
    return (
        local is not None
        and local.quantity == broker.quantity
        and local.average_entry_price == broker.average_entry_price
    )


@dataclass(slots=True)
class PositionProtectionSupervisor:
    database: Database
    broker: PositionProtectionBroker
    expected_account_id: str
    strategy_identity: StrategyIdentity
    metrics: OperationalMetrics
    policy: ProtectionPolicy = field(default_factory=ProtectionPolicy)

    def __post_init__(self) -> None:
        if not self.expected_account_id.strip():
            raise ValueError("expected_account_id must not be empty")

    async def evaluate(self, *, now: datetime) -> ProtectionCycleResult:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        moment = now.astimezone(UTC)
        cycle_id = _cycle_id(
            moment,
            expected_account_id=self.expected_account_id,
            strategy_identity=self.strategy_identity,
            policy=self.policy,
        )
        existing = self._existing(cycle_id)
        if existing is not None:
            return existing

        try:
            account_snapshot = await self.broker.get_account()
        except BrokerError as exc:
            return self._record_incident_cycle(
                cycle_id=cycle_id,
                moment=moment,
                reason="BROKER_STATE_UNAVAILABLE",
                account_id=self.expected_account_id,
                detail={
                    "operation": "get_account",
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                },
            )

        account_id = account_snapshot.account.account_id
        if account_id != self.expected_account_id:
            return self._record_incident_cycle(
                cycle_id=cycle_id,
                moment=moment,
                reason="BROKER_ACCOUNT_MISMATCH",
                account_id=self.expected_account_id,
                detail={
                    "account_verified": False,
                    "reported_account_id_hash": hashlib.sha256(
                        account_id.encode("utf-8")
                    ).hexdigest(),
                },
            )

        try:
            broker_positions = tuple(await self.broker.get_positions())
        except BrokerError as exc:
            return self._record_incident_cycle(
                cycle_id=cycle_id,
                moment=moment,
                reason="BROKER_STATE_UNAVAILABLE",
                account_id=account_id,
                detail={
                    "operation": "get_positions",
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                },
            )

        with self.database.transaction() as session:
            repository = StorageRepository(session)
            local_snapshot = repository.get_latest_account_snapshot(account_id)
            snapshots = repository.list_account_snapshots(account_id=account_id)
            high_water = max(
                (snapshot.account.equity for snapshot in snapshots),
                default=None,
            )

        local_by_symbol = (
            {}
            if local_snapshot is None
            else {position.symbol: position for position in local_snapshot.positions}
        )
        drawdown_bps = self._drawdown_bps(
            current=account_snapshot.account.equity,
            encoded_high_water=high_water,
        )
        decisions: list[tuple[Position, ProtectionDecision, bool]] = []
        for position in broker_positions:
            reconciled = _positions_agree(local_by_symbol.get(position.symbol), position)
            decision = evaluate_position(
                position,
                policy=self.policy,
                price=position.market_price,
                priced_at=moment,
                observed_at=moment,
                reconciled=reconciled,
                account_drawdown_bps=drawdown_bps,
            )
            decisions.append((position, decision, reconciled))

        unprotected_count = sum(not decision.protected for _, decision, _ in decisions)
        exit_required_count = sum(decision.requires_exit for _, decision, _ in decisions)
        if unprotected_count:
            outcome = ProtectionCycleOutcome.DEGRADED
            reason = (
                "POSITION_STATE_UNRECONCILED"
                if any(not reconciled for _, _, reconciled in decisions)
                else "POSITION_PROTECTION_BLOCKED"
            )
        elif exit_required_count:
            outcome = ProtectionCycleOutcome.ACTION_REQUIRED
            reason = "PROTECTIVE_EXIT_REQUIRED"
        else:
            outcome = ProtectionCycleOutcome.HEALTHY
            reason = ""

        result = ProtectionCycleResult(
            cycle_id=cycle_id,
            observed_at=moment,
            outcome=outcome,
            reason=reason,
            position_count=len(broker_positions),
            evaluated_count=len(decisions),
            unprotected_count=unprotected_count,
            exit_required_count=exit_required_count,
        )
        self._persist(
            result=result,
            account_id=account_id,
            decisions=tuple(decisions),
            detail={
                "account_verified": True,
                "local_snapshot_id": (
                    None if local_snapshot is None else local_snapshot.snapshot_id
                ),
            },
        )
        if reason:
            self._record_incident(
                cycle_id=cycle_id,
                kind=reason,
                moment=moment,
                severity="ERROR" if unprotected_count else "WARNING",
                detail={
                    "position_count": len(broker_positions),
                    "unprotected_count": unprotected_count,
                    "exit_required_count": exit_required_count,
                },
            )
        self._record_metrics(result)
        return result

    @staticmethod
    def _drawdown_bps(
        *,
        current: Decimal,
        encoded_high_water: Decimal | None,
    ) -> Decimal | None:
        if encoded_high_water is None:
            return None
        high_water = encoded_high_water
        if high_water <= 0:
            return None
        return max((high_water - current) / high_water * Decimal(10_000), Decimal("0"))

    def _existing(self, cycle_id: str) -> ProtectionCycleResult | None:
        with self.database.transaction() as session:
            row = (
                session.execute(
                    select(position_protection_cycles).where(
                        position_protection_cycles.c.cycle_id == cycle_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        count = row["position_count"]
        return ProtectionCycleResult(
            cycle_id=str(row["cycle_id"]),
            observed_at=decode_utc(str(row["observed_at"])),
            outcome=ProtectionCycleOutcome(str(row["outcome"])),
            reason=str(row["reason"]),
            position_count=None if count is None else int(count),
            evaluated_count=int(row["evaluated_count"]),
            unprotected_count=int(row["unprotected_count"]),
            exit_required_count=int(row["exit_required_count"]),
        )

    def _record_incident_cycle(
        self,
        *,
        cycle_id: str,
        moment: datetime,
        reason: str,
        account_id: str | None,
        detail: dict[str, object],
    ) -> ProtectionCycleResult:
        result = ProtectionCycleResult(
            cycle_id=cycle_id,
            observed_at=moment,
            outcome=ProtectionCycleOutcome.INCIDENT,
            reason=reason,
            position_count=None,
            evaluated_count=0,
            unprotected_count=0,
            exit_required_count=0,
        )
        self._persist(result=result, account_id=account_id, decisions=(), detail=detail)
        self._record_incident(
            cycle_id=cycle_id,
            kind=reason,
            moment=moment,
            severity="ERROR",
            detail=detail,
        )
        self._record_metrics(result)
        return result

    def _persist(
        self,
        *,
        result: ProtectionCycleResult,
        account_id: str | None,
        decisions: tuple[tuple[Position, ProtectionDecision, bool], ...],
        detail: dict[str, object],
    ) -> None:
        policy_hash = _policy_hash(self.policy)
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            repository.save_strategy_deployment(self.strategy_identity)
            existing = session.execute(
                select(position_protection_cycles.c.cycle_id).where(
                    position_protection_cycles.c.cycle_id == result.cycle_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            session.execute(
                position_protection_cycles.insert().values(
                    cycle_id=result.cycle_id,
                    observed_at=encode_utc(result.observed_at),
                    completed_at=encode_utc(result.observed_at),
                    strategy_id=self.strategy_identity.strategy_id,
                    strategy_version=self.strategy_identity.version,
                    configuration_hash=self.strategy_identity.configuration_hash,
                    account_id=account_id,
                    policy_version=PROTECTION_POLICY_VERSION,
                    policy_hash=policy_hash,
                    outcome=result.outcome.value,
                    reason=result.reason,
                    position_count=result.position_count,
                    evaluated_count=result.evaluated_count,
                    unprotected_count=result.unprotected_count,
                    exit_required_count=result.exit_required_count,
                    detail=json.dumps(detail, sort_keys=True, separators=(",", ":")),
                )
            )
            if decisions:
                session.execute(
                    position_protection_evaluations.insert(),
                    [
                        {
                            "evaluation_id": _evaluation_id(
                                result.cycle_id, position.symbol
                            ),
                            "cycle_id": result.cycle_id,
                            "account_id": account_id,
                            "symbol": position.symbol,
                            "quantity": encode_decimal(position.quantity),
                            "average_entry_price": encode_decimal(
                                position.average_entry_price
                            ),
                            "price": (
                                None
                                if decision.price is None
                                else encode_decimal(decision.price)
                            ),
                            "price_as_of": (
                                None
                                if decision.price is None
                                else encode_utc(result.observed_at)
                            ),
                            "price_source": "BROKER_MARK",
                            "strategy_id": self.strategy_identity.strategy_id,
                            "strategy_version": self.strategy_identity.version,
                            "configuration_hash": (
                                self.strategy_identity.configuration_hash
                            ),
                            "policy_version": PROTECTION_POLICY_VERSION,
                            "policy_hash": policy_hash,
                            "reconciled": 1 if reconciled else 0,
                            "outcome": decision.outcome.value,
                            "rule": decision.rule.value,
                            "reason": decision.reason,
                            "move_bps": (
                                None
                                if decision.move_bps is None
                                else encode_decimal(decision.move_bps)
                            ),
                            "protected": 1 if decision.protected else 0,
                            "evaluated_at": encode_utc(decision.observed_at),
                            "detail": json.dumps(
                                {"requires_exit": decision.requires_exit},
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        }
                        for position, decision, reconciled in decisions
                    ],
                )

    def _record_incident(
        self,
        *,
        cycle_id: str,
        kind: str,
        moment: datetime,
        severity: str,
        detail: dict[str, object],
    ) -> None:
        with self.database.transaction() as session:
            StorageRepository(session).create_incident(
                _incident_id(cycle_id, kind),
                severity=severity,
                kind=kind,
                message=f"Position protection supervisor reported {kind}",
                occurred_at=moment,
                detail=detail,
            )

    def _record_metrics(self, result: ProtectionCycleResult) -> None:
        self.metrics.increment("quantbot_position_protection_cycles_total")
        self.metrics.set_gauge(
            "quantbot_position_protection_positions",
            Decimal(result.position_count or 0),
        )
        self.metrics.set_gauge(
            "quantbot_position_protection_unprotected",
            Decimal(result.unprotected_count),
        )
        self.metrics.set_gauge(
            "quantbot_position_protection_exits_required",
            Decimal(result.exit_required_count),
        )


@dataclass(slots=True)
class PositionProtectionDaemonRunner:
    """Independent market-session loop with its own liveness lock."""

    clock_provider: ProtectionClockProvider
    evaluate: ProtectionEvaluator
    sleeper: ProtectionSleeper
    metrics: OperationalMetrics
    lock_path: str | Path
    interval: timedelta = POSITION_PROTECTION_INTERVAL

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("interval must be positive")
        self.lock_path = Path(self.lock_path)

    async def run(
        self,
        stop: ProtectionStopSignal,
        *,
        now: Callable[[], datetime],
    ) -> PositionProtectionDaemonResult:
        completed = 0
        with SingleWriterLock(self.lock_path):
            while not stop.is_set():
                clock = await self.clock_provider.get_market_clock()
                current = now()
                if current.tzinfo is None or current.utcoffset() is None:
                    raise ValueError("now must return a timezone-aware datetime")
                current = current.astimezone(UTC)
                if clock.is_open:
                    await self.evaluate(current)
                    completed += 1
                    self.metrics.increment("quantbot_position_protection_daemon_cycles_total")
                    if stop.is_set():
                        break
                    await self.sleeper(Decimal(str(self.interval.total_seconds())))
                    continue

                delay_seconds = (clock.next_open - current).total_seconds()
                if delay_seconds <= 0:
                    raise ValueError("broker market clock does not provide a future open")
                await self.sleeper(Decimal(str(delay_seconds)))
        return PositionProtectionDaemonResult(
            cycles_completed=completed,
            stopped_cleanly=True,
        )


__all__ = [
    "POSITION_PROTECTION_INTERVAL",
    "PositionProtectionDaemonResult",
    "PositionProtectionDaemonRunner",
    "PositionProtectionSupervisor",
    "ProtectionCycleOutcome",
    "ProtectionCycleResult",
]
