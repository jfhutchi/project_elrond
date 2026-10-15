"""Independent, durable after-entry position protection (#157).

This module has no strategy, model, forecasting, anomaly, or research dependency. It reconciles
fresh broker state against the latest durable account snapshot, evaluates deterministic protection
rules, persists a protective action before broker mutation, and routes only that durable intent
through the authoritative execution coordinator.
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

from sqlalchemy import select, update

from quantbot.brokers import BrokerAccountSnapshot, BrokerError
from quantbot.domain import (
    IntentState,
    MarketClock,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.execution import (
    OrderIntentNotResumable,
    OrderSubmissionBlocked,
)
from quantbot.operations.health import OperationsModel
from quantbot.operations.metrics import OperationalMetrics
from quantbot.risk import OrderPurpose
from quantbot.risk.protection import (
    ProtectionDecision,
    ProtectionPolicy,
    evaluate_position,
)
from quantbot.storage import Database, SingleWriterLock, StorageRepository
from quantbot.storage.database import decode_utc, encode_decimal, encode_utc
from quantbot.storage.lot_ownership import link_protection_exit
from quantbot.storage.schema import (
    incidents,
    metadata,
    position_protection_action_evaluations,
    position_protection_actions,
    position_protection_cycles,
    position_protection_evaluations,
)

PROTECTION_POLICY_VERSION = "position-protection-v2"
POSITION_PROTECTION_INTERVAL = timedelta(minutes=15)
FILLED_POSITION_CONFIRMATION_TIMEOUT = timedelta(minutes=30)


class PositionProtectionBroker(Protocol):
    async def get_account(self) -> BrokerAccountSnapshot: ...

    async def get_positions(self) -> Sequence[Position]: ...


class ProtectionSubmitter(Protocol):
    async def submit_durable(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: None,
        risk_approved: None,
        vetoes: None = None,
    ) -> object: ...

    async def reconcile_durable(self, intent: OrderIntent) -> object: ...


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


def _action_id(evaluation_id: str) -> str:
    digest = hashlib.sha256(evaluation_id.encode("utf-8")).hexdigest()
    return f"protection-action-{digest[:24]}"


def _action_client_order_id(action_id: str) -> str:
    digest = hashlib.sha256(action_id.encode("utf-8")).hexdigest()
    return f"qb-protect-{digest[:32]}"


def _action_incident_id(cycle_id: str, action_id: str, kind: str) -> str:
    digest = hashlib.sha256(f"{cycle_id}:{action_id}:{kind}".encode()).hexdigest()
    return f"protection-action-incident-{digest[:24]}"


def _broker_snapshot_id(cycle_id: str) -> str:
    digest = hashlib.sha256(f"{cycle_id}:broker-snapshot".encode()).hexdigest()
    return f"protection-broker-snapshot-{digest[:24]}"


def _flat_confirmation_incident_id(action_id: str) -> str:
    digest = hashlib.sha256(f"{action_id}:flat-confirmation-stale".encode()).hexdigest()
    return f"protection-flat-confirmation-{digest[:24]}"


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
    submitter: ProtectionSubmitter | None = None
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
            await self._dispatch_cycle_actions(cycle_id=cycle_id, moment=moment)
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
            local_snapshot = repository.get_latest_account_snapshot(account_id, certified_only=True)
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
        broker_snapshot_id = _broker_snapshot_id(cycle_id)
        self._persist(
            result=result,
            account_id=account_id,
            decisions=tuple(decisions),
            detail={
                "account_verified": True,
                "local_snapshot_id": (
                    None if local_snapshot is None else local_snapshot.snapshot_id
                ),
                "broker_snapshot_id": broker_snapshot_id,
            },
            broker_snapshot=account_snapshot,
            broker_positions=broker_positions,
            broker_snapshot_id=broker_snapshot_id,
        )
        self._confirm_filled_actions(
            account_id=account_id,
            open_symbols={position.symbol for position in broker_positions},
            moment=moment,
            broker_snapshot_id=broker_snapshot_id,
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
        await self._dispatch_cycle_actions(cycle_id=cycle_id, moment=moment)
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
        broker_snapshot: BrokerAccountSnapshot | None = None,
        broker_positions: tuple[Position, ...] = (),
        broker_snapshot_id: str | None = None,
    ) -> tuple[str, ...]:
        policy_hash = _policy_hash(self.policy)
        action_ids: list[str] = []
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            repository.save_strategy_deployment(self.strategy_identity)
            if broker_snapshot is not None:
                if broker_snapshot_id is None:
                    raise ValueError("broker snapshot identity is required")
                repository.save_account_snapshot(
                    broker_snapshot_id,
                    broker_snapshot.account,
                    broker_positions,
                    captured_at=result.observed_at,
                )
                raw = metadata.tables["raw_account_snapshot_sources"]
                if session.execute(select(raw.c.snapshot_id).where(
                    raw.c.snapshot_id == broker_snapshot_id)).first() is None:
                    session.execute(raw.insert().values(snapshot_id=broker_snapshot_id,
                        source="POSITION_PROTECTION_RAW"))
            existing = session.execute(
                select(position_protection_cycles.c.cycle_id).where(
                    position_protection_cycles.c.cycle_id == result.cycle_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return ()
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
                evaluation_rows = [
                    {
                        "evaluation_id": _evaluation_id(result.cycle_id, position.symbol),
                        "cycle_id": result.cycle_id,
                        "account_id": account_id,
                        "symbol": position.symbol,
                        "quantity": encode_decimal(position.quantity),
                        "average_entry_price": encode_decimal(position.average_entry_price),
                        "price": (
                            None if decision.price is None else encode_decimal(decision.price)
                        ),
                        "price_as_of": (
                            None if decision.price is None else encode_utc(result.observed_at)
                        ),
                        "price_source": "BROKER_MARK",
                        "strategy_id": self.strategy_identity.strategy_id,
                        "strategy_version": self.strategy_identity.version,
                        "configuration_hash": (self.strategy_identity.configuration_hash),
                        "policy_version": PROTECTION_POLICY_VERSION,
                        "policy_hash": policy_hash,
                        "reconciled": 1 if reconciled else 0,
                        "outcome": decision.outcome.value,
                        "rule": decision.rule.value,
                        "reason": decision.reason,
                        "move_bps": (
                            None if decision.move_bps is None else encode_decimal(decision.move_bps)
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
                ]
                session.execute(position_protection_evaluations.insert(), evaluation_rows)
                for position, decision, reconciled in decisions:
                    if not decision.requires_exit:
                        continue
                    evaluation_id = _evaluation_id(result.cycle_id, position.symbol)
                    active = (
                        session.execute(
                            select(position_protection_actions).where(
                                position_protection_actions.c.account_id == account_id,
                                position_protection_actions.c.symbol == position.symbol,
                                position_protection_actions.c.status != "COMPLETED",
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if active is None:
                        action_id = _action_id(evaluation_id)
                        client_order_id = _action_client_order_id(action_id)
                        intent = OrderIntent(
                            intent_id=f"intent-{client_order_id}",
                            client_order_id=client_order_id,
                            strategy_id=self.strategy_identity.strategy_id,
                            signal_date=result.observed_at.date(),
                            symbol=position.symbol,
                            side=(OrderSide.SELL if position.quantity > 0 else OrderSide.BUY),
                            order_type=OrderType.MARKET,
                            time_in_force=TimeInForce.DAY,
                            quantity=abs(position.quantity),
                            created_at=result.observed_at,
                        )
                        repository.create_order_intent(intent)
                        session.execute(
                            position_protection_actions.insert().values(
                                action_id=action_id,
                                trigger_evaluation_id=evaluation_id,
                                account_id=account_id,
                                symbol=position.symbol,
                                side=intent.side.value,
                                quantity=encode_decimal(intent.quantity),
                                intent_id=intent.intent_id,
                                client_order_id=intent.client_order_id,
                                status="PENDING",
                                created_at=encode_utc(result.observed_at),
                                updated_at=encode_utc(result.observed_at),
                                completed_at=None,
                                flat_confirmation_snapshot_id=None,
                                flat_confirmed_at=None,
                                last_error="",
                                detail=json.dumps(
                                    {
                                        "policy_hash": policy_hash,
                                        "policy_version": PROTECTION_POLICY_VERSION,
                                        "rule": decision.rule.value,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                            )
                        )
                        if account_id is not None:
                            link_protection_exit(
                                session, intent, account_id=account_id,
                                position_quantity=position.quantity, reconciled=reconciled,
                                now=result.observed_at,
                            )
                    else:
                        action_id = str(active["action_id"])
                    session.execute(
                        position_protection_action_evaluations.insert().values(
                            action_id=action_id,
                            evaluation_id=evaluation_id,
                            linked_at=encode_utc(result.observed_at),
                        )
                    )
                    action_ids.append(action_id)
        return tuple(action_ids)

    @staticmethod
    def _status_for_intent(state: IntentState) -> str:
        if state is IntentState.FILLED:
            return "FILLED_UNVERIFIED"
        if state in {
            IntentState.REJECTED,
            IntentState.CANCELLED,
            IntentState.EXPIRED,
            IntentState.ERROR,
        }:
            return "FAILED"
        if state is IntentState.SUBMISSION_UNKNOWN:
            return "UNCERTAIN"
        if state in {IntentState.BROKER_ACCEPTED, IntentState.PARTIALLY_FILLED}:
            return "WORKING"
        return "PENDING"

    def _confirm_filled_actions(
        self,
        *,
        account_id: str,
        open_symbols: set[str],
        moment: datetime,
        broker_snapshot_id: str,
    ) -> None:
        with self.database.transaction() as session:
            repository = StorageRepository(session)
            filled = (
                session.execute(
                    select(position_protection_actions).where(
                        position_protection_actions.c.account_id == account_id,
                        position_protection_actions.c.status == "FILLED_UNVERIFIED",
                    )
                )
                .mappings()
                .all()
            )
            for row in filled:
                action_id = str(row["action_id"])
                filled_at = decode_utc(str(row["updated_at"]))
                stale_at = filled_at + FILLED_POSITION_CONFIRMATION_TIMEOUT
                incident_id = _flat_confirmation_incident_id(action_id)
                incident = session.execute(
                    select(incidents.c.incident_id, incidents.c.resolved_at).where(
                        incidents.c.incident_id == incident_id
                    )
                ).one_or_none()
                if str(row["symbol"]) in open_symbols:
                    if moment >= stale_at and incident is None:
                        repository.create_incident(
                            incident_id,
                            severity="ERROR",
                            kind="PROTECTIVE_FILL_POSITION_NOT_FLAT",
                            message=(
                                "Filled protective exit is not confirmed flat by broker state"
                            ),
                            occurred_at=stale_at,
                            detail={
                                "action_id": action_id,
                                "symbol": str(row["symbol"]),
                                "filled_observed_at": encode_utc(filled_at),
                                "confirmation_deadline": encode_utc(stale_at),
                            },
                        )
                    continue
                if moment <= filled_at:
                    continue
                session.execute(
                    update(position_protection_actions)
                    .where(
                        position_protection_actions.c.action_id == action_id,
                        position_protection_actions.c.status == "FILLED_UNVERIFIED",
                    )
                    .values(
                        status="COMPLETED",
                        updated_at=encode_utc(moment),
                        completed_at=encode_utc(moment),
                        flat_confirmation_snapshot_id=broker_snapshot_id,
                        flat_confirmed_at=encode_utc(moment),
                        last_error="",
                    )
                )
                if incident is not None and incident.resolved_at is None:
                    repository.resolve_incident(incident_id, resolved_at=moment)

    def _set_action_status(
        self,
        action_id: str,
        *,
        status: str,
        moment: datetime,
        last_error: str = "",
    ) -> None:
        values: dict[str, object] = {
            "status": status,
            "updated_at": encode_utc(moment),
            "last_error": last_error,
            "completed_at": encode_utc(moment) if status == "COMPLETED" else None,
        }
        with self.database.transaction() as session:
            session.execute(
                update(position_protection_actions)
                .where(position_protection_actions.c.action_id == action_id)
                .values(**values)
            )

    def _record_action_incident(
        self,
        *,
        cycle_id: str,
        action_id: str,
        kind: str,
        moment: datetime,
        detail: dict[str, object],
    ) -> None:
        with self.database.transaction() as session:
            StorageRepository(session).create_incident(
                _action_incident_id(cycle_id, action_id, kind),
                severity="ERROR",
                kind=kind,
                message=f"Protective exit action reported {kind}",
                occurred_at=moment,
                detail={"action_id": action_id, **detail},
            )

    async def _dispatch_cycle_actions(self, *, cycle_id: str, moment: datetime) -> None:
        if self.submitter is None:
            return
        with self.database.transaction() as session:
            rows = (
                session.execute(
                    select(position_protection_actions)
                    .join(
                        position_protection_action_evaluations,
                        position_protection_action_evaluations.c.action_id
                        == position_protection_actions.c.action_id,
                    )
                    .join(
                        position_protection_evaluations,
                        position_protection_evaluations.c.evaluation_id
                        == position_protection_action_evaluations.c.evaluation_id,
                    )
                    .where(position_protection_evaluations.c.cycle_id == cycle_id)
                    .order_by(position_protection_actions.c.action_id)
                )
                .mappings()
                .all()
            )
        for row in rows:
            action_id = str(row["action_id"])
            if str(row["status"]) in {
                "FILLED_UNVERIFIED",
                "COMPLETED",
                "FAILED",
            }:
                continue
            with self.database.transaction() as session:
                intent = StorageRepository(session).get_order_intent(str(row["intent_id"]))
            if intent is None:
                self._set_action_status(
                    action_id,
                    status="FAILED",
                    moment=moment,
                    last_error="DURABLE_INTENT_MISSING",
                )
                self._record_action_incident(
                    cycle_id=cycle_id,
                    action_id=action_id,
                    kind="PROTECTIVE_INTENT_MISSING",
                    moment=moment,
                    detail={"intent_id": str(row["intent_id"])},
                )
                continue
            current_status = self._status_for_intent(intent.state)
            if current_status in {"FILLED_UNVERIFIED", "COMPLETED", "FAILED"}:
                self._set_action_status(action_id, status=current_status, moment=moment)
                continue
            reconciling_working_order = current_status == "WORKING"
            try:
                if reconciling_working_order:
                    await self.submitter.reconcile_durable(intent)
                else:
                    await self.submitter.submit_durable(
                        intent.model_copy(update={"state": IntentState.RISK_APPROVED}),
                        purpose=OrderPurpose.STRATEGY_EXIT,
                        market_eligible=True,
                        drawdown=None,
                        risk_approved=None,
                        vetoes=None,
                    )
            except OrderSubmissionBlocked as error:
                with self.database.transaction() as session:
                    current = StorageRepository(session).get_order_intent(intent.intent_id)
                status = (
                    "FAILED"
                    if current is not None and current.state is IntentState.ERROR
                    else "PENDING"
                )
                reason = ",".join(error.decision.reasons)
                self._set_action_status(
                    action_id,
                    status=status,
                    moment=moment,
                    last_error=reason,
                )
                self._record_action_incident(
                    cycle_id=cycle_id,
                    action_id=action_id,
                    kind="PROTECTIVE_EXIT_BLOCKED",
                    moment=moment,
                    detail={"reasons": list(error.decision.reasons)},
                )
                continue
            except OrderIntentNotResumable as error:
                self._set_action_status(
                    action_id,
                    status="FAILED",
                    moment=moment,
                    last_error=error.intent.state.value,
                )
                self._record_action_incident(
                    cycle_id=cycle_id,
                    action_id=action_id,
                    kind="PROTECTIVE_EXIT_TERMINAL",
                    moment=moment,
                    detail={"intent_state": error.intent.state.value},
                )
                continue
            except BrokerError as error:
                with self.database.transaction() as session:
                    current = StorageRepository(session).get_order_intent(intent.intent_id)
                status = (
                    "UNCERTAIN"
                    if reconciling_working_order or current is None
                    else self._status_for_intent(current.state)
                )
                self._set_action_status(
                    action_id,
                    status=status,
                    moment=moment,
                    last_error=type(error).__name__,
                )
                self._record_action_incident(
                    cycle_id=cycle_id,
                    action_id=action_id,
                    kind="PROTECTIVE_EXIT_BROKER_ERROR",
                    moment=moment,
                    detail={"error_type": type(error).__name__},
                )
                continue
            with self.database.transaction() as session:
                current = StorageRepository(session).get_order_intent(intent.intent_id)
            if current is None:
                raise RuntimeError(
                    f"protective intent disappeared after submission: {intent.intent_id}"
                )
            final_status = self._status_for_intent(current.state)
            self._set_action_status(
                action_id,
                status=final_status,
                moment=moment,
            )
            if final_status == "FAILED":
                self._record_action_incident(
                    cycle_id=cycle_id,
                    action_id=action_id,
                    kind="PROTECTIVE_EXIT_TERMINAL",
                    moment=moment,
                    detail={"intent_state": current.state.value},
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
    "FILLED_POSITION_CONFIRMATION_TIMEOUT",
    "POSITION_PROTECTION_INTERVAL",
    "PositionProtectionDaemonResult",
    "PositionProtectionDaemonRunner",
    "PositionProtectionSupervisor",
    "ProtectionCycleOutcome",
    "ProtectionCycleResult",
]
