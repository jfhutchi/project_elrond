"""Transaction-scoped repositories for durable QuantBot state."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import TypeAlias, cast

from sqlalchemy import select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.domain import (
    Account,
    Bar,
    BrokerOrder,
    Fill,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    ReconciliationStatus,
    StrategyIdentity,
    TimeInForce,
    transition_order,
)
from quantbot.storage.database import decode_decimal, decode_utc, encode_decimal, encode_utc
from quantbot.storage.schema import (
    account_snapshots,
    bars,
    broker_orders,
    equity_snapshots,
    fills,
    incidents,
    kill_switch_state,
    order_events,
    order_intents,
    positions,
    qualification_days,
    reconciliation_diffs,
    reconciliation_runs,
    runs,
    signals,
    strategy_deployments,
)

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

TERMINAL_INTENT_STATES = frozenset(
    {
        IntentState.FILLED,
        IntentState.REJECTED,
        IntentState.CANCELLED,
        IntentState.EXPIRED,
        IntentState.ERROR,
    }
)


class StateConflictError(RuntimeError):
    """Raised when a stable identifier is observed with conflicting state."""


class RecordNotFoundError(LookupError):
    """Raised when an update targets a record that does not exist."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    strategy_id: str
    strategy_version: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    detail: dict[str, object]


@dataclass(frozen=True, slots=True)
class SignalRecord:
    signal_id: str
    run_id: str
    strategy_id: str
    symbol: str
    occurred_at: datetime
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class OrderEventRecord:
    event_id: str
    intent_id: str | None
    broker_order_id: str | None
    event_type: str
    occurred_at: datetime
    detail: dict[str, object]


@dataclass(frozen=True, slots=True)
class EquitySnapshotRecord:
    snapshot_id: str
    account_snapshot_id: str
    account_id: str
    captured_at: datetime
    equity: Decimal
    cash: Decimal


@dataclass(frozen=True, slots=True)
class QualificationDayRecord:
    strategy_id: str
    trading_date: date
    qualified: bool
    detail: dict[str, object]


@dataclass(frozen=True, slots=True)
class AccountSnapshotRecord:
    snapshot_id: str
    run_id: str | None
    captured_at: datetime
    account: Account
    positions: tuple[Position, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationDiffRecord:
    diff_id: str
    category: str
    key: str
    expected: dict[str, object]
    actual: dict[str, object]


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    reconciliation_id: str
    run_id: str | None
    status: ReconciliationStatus
    started_at: datetime
    completed_at: datetime
    summary: dict[str, object]
    diffs: tuple[ReconciliationDiffRecord, ...]


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    incident_id: str
    run_id: str | None
    severity: str
    kind: str
    message: str
    occurred_at: datetime
    resolved_at: datetime | None
    detail: dict[str, object]


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    engaged: bool
    reason: str
    updated_at: datetime


def _json_compatible(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return encode_decimal(value)
    if isinstance(value, datetime):
        return encode_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_compatible(value.value)
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized[key] = _json_compatible(nested)
        return normalized
    if isinstance(value, Sequence):
        return [_json_compatible(nested) for nested in value]
    raise TypeError(f"Unsupported audit JSON value: {type(value).__name__}")


def _encode_json(value: Mapping[str, object] | None = None) -> str:
    normalized = _json_compatible({} if value is None else value)
    return json.dumps(normalized, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _decode_json(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise TypeError("stored JSON must be text")
    loaded: object = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("stored audit JSON must be an object")
    return cast(dict[str, object], loaded)


def _row_matches(row: RowMapping, payload: Mapping[str, object]) -> bool:
    return all(row[key] == value for key, value in payload.items())


class StorageRepository:
    """Persist and restore records inside one caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def save_strategy_deployment(self, identity: StrategyIdentity) -> bool:
        payload = {
            "strategy_id": identity.strategy_id,
            "version": identity.version,
            "git_commit": identity.git_commit,
            "configuration_hash": identity.configuration_hash,
            "deployment_timestamp": encode_utc(identity.deployment_timestamp),
        }
        row = (
            self._session.execute(
                select(strategy_deployments).where(
                    strategy_deployments.c.strategy_id == identity.strategy_id,
                    strategy_deployments.c.version == identity.version,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(strategy_deployments.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(
            f"strategy deployment {identity.strategy_id}:{identity.version} "
            "conflicts with stored data"
        )

    def get_strategy_deployment(
        self,
        strategy_id: str,
        version: str | None = None,
    ) -> StrategyIdentity | None:
        statement = select(strategy_deployments).where(
            strategy_deployments.c.strategy_id == strategy_id
        )
        if version is not None:
            statement = statement.where(strategy_deployments.c.version == version)
        else:
            statement = statement.order_by(
                strategy_deployments.c.deployment_timestamp.desc()
            ).limit(1)
        row = self._session.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return StrategyIdentity(
            strategy_id=str(row["strategy_id"]),
            version=str(row["version"]),
            git_commit=str(row["git_commit"]),
            configuration_hash=str(row["configuration_hash"]),
            deployment_timestamp=decode_utc(str(row["deployment_timestamp"])),
        )

    def create_run(
        self,
        run_id: str,
        strategy: StrategyIdentity,
        *,
        started_at: datetime,
        status: str = "RUNNING",
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        self.save_strategy_deployment(strategy)
        payload = {
            "run_id": run_id,
            "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.version,
            "status": status,
            "started_at": encode_utc(started_at),
            "finished_at": None,
            "detail_json": _encode_json(detail),
        }
        row = (
            self._session.execute(select(runs).where(runs.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(runs.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(f"run {run_id} conflicts with stored data")

    def finish_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        detail: Mapping[str, object] | None = None,
    ) -> RunRecord:
        row = (
            self._session.execute(select(runs).where(runs.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RecordNotFoundError(f"run not found: {run_id}")
        values: dict[str, object] = {
            "finished_at": encode_utc(finished_at),
            "status": status,
        }
        if detail is not None:
            values["detail_json"] = _encode_json(detail)
        self._session.execute(update(runs).where(runs.c.run_id == run_id).values(**values))
        restored = self.get_run(run_id)
        if restored is None:
            raise RecordNotFoundError(f"run not found after update: {run_id}")
        return restored

    def get_run(self, run_id: str) -> RunRecord | None:
        row = (
            self._session.execute(select(runs).where(runs.c.run_id == run_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        finished_raw = row["finished_at"]
        return RunRecord(
            run_id=str(row["run_id"]),
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            status=str(row["status"]),
            started_at=decode_utc(str(row["started_at"])),
            finished_at=decode_utc(str(finished_raw)) if finished_raw is not None else None,
            detail=_decode_json(row["detail_json"]),
        )

    def save_bars(
        self,
        observations: Sequence[Bar],
        *,
        provider: str,
        adjustment_metadata: Mapping[str, object] | None = None,
    ) -> int:
        if not provider.strip():
            raise ValueError("provider must not be empty")
        metadata_json = _encode_json(adjustment_metadata)
        inserted = 0
        for bar in observations:
            payload = {
                "symbol": bar.symbol,
                "timestamp": encode_utc(bar.timestamp),
                "provider": provider,
                "adjustment_metadata_json": metadata_json,
                "open": encode_decimal(bar.open),
                "high": encode_decimal(bar.high),
                "low": encode_decimal(bar.low),
                "close": encode_decimal(bar.close),
                "volume": encode_decimal(bar.volume),
                "adjustment": encode_decimal(bar.adjustment),
            }
            row = (
                self._session.execute(
                    select(bars).where(
                        bars.c.symbol == bar.symbol,
                        bars.c.timestamp == payload["timestamp"],
                        bars.c.provider == provider,
                        bars.c.adjustment_metadata_json == metadata_json,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                self._session.execute(bars.insert().values(**payload))
                inserted += 1
            elif not _row_matches(row, payload):
                raise StateConflictError(
                    f"bar observation conflicts for {bar.symbol} at {payload['timestamp']}"
                )
        return inserted

    def list_bars(self, *, symbol: str | None = None, provider: str | None = None) -> list[Bar]:
        statement = select(bars)
        if symbol is not None:
            statement = statement.where(bars.c.symbol == symbol)
        if provider is not None:
            statement = statement.where(bars.c.provider == provider)
        statement = statement.order_by(bars.c.timestamp, bars.c.symbol)
        rows = self._session.execute(statement).mappings().all()
        return [self._bar_from_row(row) for row in rows]

    @staticmethod
    def _bar_from_row(row: RowMapping) -> Bar:
        return Bar(
            symbol=str(row["symbol"]),
            timestamp=decode_utc(str(row["timestamp"])),
            open=decode_decimal(str(row["open"])),
            high=decode_decimal(str(row["high"])),
            low=decode_decimal(str(row["low"])),
            close=decode_decimal(str(row["close"])),
            volume=decode_decimal(str(row["volume"])),
            adjustment=decode_decimal(str(row["adjustment"])),
        )

    def record_signal(
        self,
        signal_id: str,
        *,
        run_id: str,
        strategy_id: str,
        symbol: str,
        occurred_at: datetime,
        payload: Mapping[str, object],
    ) -> bool:
        values = {
            "signal_id": signal_id,
            "run_id": run_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "occurred_at": encode_utc(occurred_at),
            "payload_json": _encode_json(payload),
        }
        row = (
            self._session.execute(select(signals).where(signals.c.signal_id == signal_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(signals.insert().values(**values))
            return True
        if _row_matches(row, values):
            return False
        raise StateConflictError(f"signal {signal_id} conflicts with stored data")

    def get_signal(self, signal_id: str) -> SignalRecord | None:
        row = (
            self._session.execute(select(signals).where(signals.c.signal_id == signal_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._signal_from_row(row)

    def list_signals(
        self,
        *,
        run_id: str | None = None,
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> list[SignalRecord]:
        statement = select(signals)
        if run_id is not None:
            statement = statement.where(signals.c.run_id == run_id)
        if strategy_id is not None:
            statement = statement.where(signals.c.strategy_id == strategy_id)
        if symbol is not None:
            statement = statement.where(signals.c.symbol == symbol)
        rows = (
            self._session.execute(statement.order_by(signals.c.occurred_at, signals.c.signal_id))
            .mappings()
            .all()
        )
        return [self._signal_from_row(row) for row in rows]

    @staticmethod
    def _signal_from_row(row: RowMapping) -> SignalRecord:
        return SignalRecord(
            signal_id=str(row["signal_id"]),
            run_id=str(row["run_id"]),
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            occurred_at=decode_utc(str(row["occurred_at"])),
            payload=_decode_json(row["payload_json"]),
        )

    def create_order_intent(self, intent: OrderIntent) -> bool:
        payload = self._intent_payload(intent)
        row = (
            self._session.execute(
                select(order_intents).where(order_intents.c.intent_id == intent.intent_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            if _row_matches(row, payload):
                return False
            raise StateConflictError(f"order intent {intent.intent_id} conflicts with stored data")

        client_row = self._session.execute(
            select(order_intents.c.intent_id).where(
                order_intents.c.client_order_id == intent.client_order_id
            )
        ).scalar_one_or_none()
        if client_row is not None:
            raise StateConflictError(
                f"client_order_id {intent.client_order_id} already belongs to intent {client_row}"
            )
        self._session.execute(order_intents.insert().values(**payload))
        return True

    @staticmethod
    def _intent_payload(intent: OrderIntent) -> dict[str, object]:
        return {
            "intent_id": intent.intent_id,
            "client_order_id": intent.client_order_id,
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "signal_date": intent.signal_date.isoformat(),
            "side": intent.side.value,
            "order_type": intent.order_type.value,
            "time_in_force": intent.time_in_force.value,
            "quantity": encode_decimal(intent.quantity),
            "limit_price": encode_decimal(intent.limit_price)
            if intent.limit_price is not None
            else None,
            "stop_price": encode_decimal(intent.stop_price)
            if intent.stop_price is not None
            else None,
            "extended_hours": intent.extended_hours,
            "state": intent.state.value,
            "created_at": encode_utc(intent.created_at),
        }

    def get_order_intent(self, intent_id: str) -> OrderIntent | None:
        row = (
            self._session.execute(
                select(order_intents).where(order_intents.c.intent_id == intent_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._intent_from_row(row)

    def list_unresolved_order_intents(self) -> list[OrderIntent]:
        terminal_values = [state.value for state in TERMINAL_INTENT_STATES]
        rows = (
            self._session.execute(
                select(order_intents)
                .where(order_intents.c.state.not_in(terminal_values))
                .order_by(order_intents.c.created_at, order_intents.c.intent_id)
            )
            .mappings()
            .all()
        )
        return [self._intent_from_row(row) for row in rows]

    @staticmethod
    def _intent_from_row(row: RowMapping) -> OrderIntent:
        limit_raw = row["limit_price"]
        stop_raw = row["stop_price"]
        return OrderIntent(
            intent_id=str(row["intent_id"]),
            client_order_id=str(row["client_order_id"]),
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            signal_date=date.fromisoformat(str(row["signal_date"])),
            side=OrderSide(str(row["side"])),
            order_type=OrderType(str(row["order_type"])),
            time_in_force=TimeInForce(str(row["time_in_force"])),
            quantity=decode_decimal(str(row["quantity"])),
            limit_price=decode_decimal(str(limit_raw)) if limit_raw is not None else None,
            stop_price=decode_decimal(str(stop_raw)) if stop_raw is not None else None,
            extended_hours=bool(row["extended_hours"]),
            state=IntentState(str(row["state"])),
            created_at=decode_utc(str(row["created_at"])),
        )

    def transition_order_intent(
        self,
        intent_id: str,
        target: IntentState,
        *,
        expected_current: IntentState,
    ) -> OrderIntent:
        stored = self.get_order_intent(intent_id)
        if stored is None:
            raise RecordNotFoundError(f"order intent not found: {intent_id}")
        if stored.state is not expected_current:
            raise StateConflictError(
                f"order intent {intent_id} expected {expected_current.value}, "
                f"found {stored.state.value}"
            )
        transition_order(expected_current, target)
        result = self._session.connection().execute(
            update(order_intents)
            .where(
                order_intents.c.intent_id == intent_id,
                order_intents.c.state == expected_current.value,
            )
            .values(state=target.value)
        )
        if result.rowcount != 1:
            raise StateConflictError(f"order intent {intent_id} changed during transition")
        return stored.model_copy(update={"state": target})

    def save_broker_order(self, order: BrokerOrder) -> bool:
        payload = self._broker_order_payload(order)
        existing = (
            self._session.execute(
                select(broker_orders).where(
                    broker_orders.c.broker_order_id == order.broker_order_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if _row_matches(existing, payload):
                return False
            raise StateConflictError(
                f"broker order {order.broker_order_id} conflicts with stored data"
            )

        intent_id = self._session.execute(
            select(order_intents.c.intent_id).where(
                order_intents.c.client_order_id == order.client_order_id
            )
        ).scalar_one_or_none()
        if intent_id is None:
            raise StateConflictError(
                f"client_order_id {order.client_order_id} does not match a stored intent"
            )
        linked_order = self._session.execute(
            select(broker_orders.c.broker_order_id).where(
                broker_orders.c.client_order_id == order.client_order_id
            )
        ).scalar_one_or_none()
        if linked_order is not None:
            raise StateConflictError(
                f"client_order_id {order.client_order_id} already maps to "
                f"broker order {linked_order}"
            )
        self._session.execute(broker_orders.insert().values(**payload))
        return True

    @staticmethod
    def _broker_order_payload(order: BrokerOrder) -> dict[str, object]:
        return {
            "broker_order_id": order.broker_order_id,
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "order_type": order.order_type.value,
            "time_in_force": order.time_in_force.value,
            "quantity": encode_decimal(order.quantity),
            "filled_quantity": encode_decimal(order.filled_quantity),
            "status": order.status,
            "submitted_at": encode_utc(order.submitted_at),
            "filled_average_price": (
                encode_decimal(order.filled_average_price)
                if order.filled_average_price is not None
                else None
            ),
        }

    def get_broker_order(self, broker_order_id: str) -> BrokerOrder | None:
        row = (
            self._session.execute(
                select(broker_orders).where(broker_orders.c.broker_order_id == broker_order_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        average_raw = row["filled_average_price"]
        return BrokerOrder(
            broker_order_id=str(row["broker_order_id"]),
            client_order_id=str(row["client_order_id"]),
            symbol=str(row["symbol"]),
            side=OrderSide(str(row["side"])),
            order_type=OrderType(str(row["order_type"])),
            time_in_force=TimeInForce(str(row["time_in_force"])),
            quantity=decode_decimal(str(row["quantity"])),
            filled_quantity=decode_decimal(str(row["filled_quantity"])),
            status=str(row["status"]),
            submitted_at=decode_utc(str(row["submitted_at"])),
            filled_average_price=(
                decode_decimal(str(average_raw)) if average_raw is not None else None
            ),
        )

    def record_fill(self, fill: Fill) -> bool:
        payload = {
            "fill_id": fill.fill_id,
            "broker_order_id": fill.broker_order_id,
            "symbol": fill.symbol,
            "side": fill.side.value,
            "quantity": encode_decimal(fill.quantity),
            "price": encode_decimal(fill.price),
            "occurred_at": encode_utc(fill.occurred_at),
            "fee": encode_decimal(fill.fee),
        }
        row = (
            self._session.execute(select(fills).where(fills.c.fill_id == fill.fill_id))
            .mappings()
            .one_or_none()
        )
        if row is not None:
            if _row_matches(row, payload):
                return False
            raise StateConflictError(f"fill {fill.fill_id} conflicts with stored data")
        broker_id = self._session.execute(
            select(broker_orders.c.broker_order_id).where(
                broker_orders.c.broker_order_id == fill.broker_order_id
            )
        ).scalar_one_or_none()
        if broker_id is None:
            raise StateConflictError(f"fill {fill.fill_id} references an unknown broker order")
        self._session.execute(fills.insert().values(**payload))
        return True

    def get_fill(self, fill_id: str) -> Fill | None:
        row = (
            self._session.execute(select(fills).where(fills.c.fill_id == fill_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._fill_from_row(row)

    def list_fills(self, *, broker_order_id: str | None = None) -> list[Fill]:
        statement = select(fills)
        if broker_order_id is not None:
            statement = statement.where(fills.c.broker_order_id == broker_order_id)
        rows = (
            self._session.execute(statement.order_by(fills.c.occurred_at, fills.c.fill_id))
            .mappings()
            .all()
        )
        return [self._fill_from_row(row) for row in rows]

    @staticmethod
    def _fill_from_row(row: RowMapping) -> Fill:
        return Fill(
            fill_id=str(row["fill_id"]),
            broker_order_id=str(row["broker_order_id"]),
            symbol=str(row["symbol"]),
            side=OrderSide(str(row["side"])),
            quantity=decode_decimal(str(row["quantity"])),
            price=decode_decimal(str(row["price"])),
            occurred_at=decode_utc(str(row["occurred_at"])),
            fee=decode_decimal(str(row["fee"])),
        )

    def record_order_event(
        self,
        event_id: str,
        *,
        event_type: str,
        occurred_at: datetime,
        intent_id: str | None = None,
        broker_order_id: str | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        payload = {
            "event_id": event_id,
            "intent_id": intent_id,
            "broker_order_id": broker_order_id,
            "event_type": event_type,
            "occurred_at": encode_utc(occurred_at),
            "detail_json": _encode_json(detail),
        }
        row = (
            self._session.execute(select(order_events).where(order_events.c.event_id == event_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(order_events.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(f"order event {event_id} conflicts with stored data")

    def get_order_event(self, event_id: str) -> OrderEventRecord | None:
        row = (
            self._session.execute(select(order_events).where(order_events.c.event_id == event_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._order_event_from_row(row)

    def list_order_events(
        self,
        *,
        intent_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> list[OrderEventRecord]:
        statement = select(order_events)
        if intent_id is not None:
            statement = statement.where(order_events.c.intent_id == intent_id)
        if broker_order_id is not None:
            statement = statement.where(order_events.c.broker_order_id == broker_order_id)
        rows = (
            self._session.execute(
                statement.order_by(order_events.c.occurred_at, order_events.c.event_id)
            )
            .mappings()
            .all()
        )
        return [self._order_event_from_row(row) for row in rows]

    @staticmethod
    def _order_event_from_row(row: RowMapping) -> OrderEventRecord:
        intent_raw = row["intent_id"]
        broker_raw = row["broker_order_id"]
        return OrderEventRecord(
            event_id=str(row["event_id"]),
            intent_id=str(intent_raw) if intent_raw is not None else None,
            broker_order_id=str(broker_raw) if broker_raw is not None else None,
            event_type=str(row["event_type"]),
            occurred_at=decode_utc(str(row["occurred_at"])),
            detail=_decode_json(row["detail_json"]),
        )

    def save_account_snapshot(
        self,
        snapshot_id: str,
        account: Account,
        snapshot_positions: Sequence[Position],
        *,
        captured_at: datetime,
        run_id: str | None = None,
    ) -> bool:
        if len({position.symbol for position in snapshot_positions}) != len(snapshot_positions):
            raise ValueError("snapshot positions must contain unique symbols")
        payload = {
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "account_id": account.account_id,
            "captured_at": encode_utc(captured_at),
            "cash": encode_decimal(account.cash),
            "buying_power": encode_decimal(account.buying_power),
            "equity": encode_decimal(account.equity),
            "currency": account.currency,
        }
        position_payloads = [
            {
                "snapshot_id": snapshot_id,
                "symbol": position.symbol,
                "quantity": encode_decimal(position.quantity),
                "market_price": encode_decimal(position.market_price),
                "market_value": encode_decimal(position.market_value),
                "average_entry_price": encode_decimal(position.average_entry_price),
            }
            for position in sorted(snapshot_positions, key=lambda item: item.symbol)
        ]
        existing = (
            self._session.execute(
                select(account_snapshots).where(account_snapshots.c.snapshot_id == snapshot_id)
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            stored_positions = (
                self._session.execute(
                    select(positions)
                    .where(positions.c.snapshot_id == snapshot_id)
                    .order_by(positions.c.symbol)
                )
                .mappings()
                .all()
            )
            if _row_matches(existing, payload) and len(stored_positions) == len(position_payloads):
                if all(
                    _row_matches(stored, expected)
                    for stored, expected in zip(stored_positions, position_payloads, strict=True)
                ):
                    return False
            raise StateConflictError(f"account snapshot {snapshot_id} conflicts with stored data")

        self._session.execute(account_snapshots.insert().values(**payload))
        self._session.execute(
            equity_snapshots.insert().values(
                snapshot_id=snapshot_id,
                account_snapshot_id=snapshot_id,
                account_id=account.account_id,
                captured_at=payload["captured_at"],
                equity=payload["equity"],
                cash=payload["cash"],
            )
        )
        if position_payloads:
            self._session.execute(positions.insert(), position_payloads)
        return True

    def get_latest_account_snapshot(self, account_id: str) -> AccountSnapshotRecord | None:
        row = (
            self._session.execute(
                select(account_snapshots)
                .where(account_snapshots.c.account_id == account_id)
                .order_by(account_snapshots.c.captured_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        position_rows = (
            self._session.execute(
                select(positions)
                .where(positions.c.snapshot_id == row["snapshot_id"])
                .order_by(positions.c.symbol)
            )
            .mappings()
            .all()
        )
        account = Account(
            account_id=str(row["account_id"]),
            cash=decode_decimal(str(row["cash"])),
            buying_power=decode_decimal(str(row["buying_power"])),
            equity=decode_decimal(str(row["equity"])),
            currency=str(row["currency"]),
        )
        restored_positions = tuple(
            Position(
                symbol=str(position["symbol"]),
                quantity=decode_decimal(str(position["quantity"])),
                market_price=decode_decimal(str(position["market_price"])),
                market_value=decode_decimal(str(position["market_value"])),
                average_entry_price=decode_decimal(str(position["average_entry_price"])),
            )
            for position in position_rows
        )
        run_raw = row["run_id"]
        return AccountSnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            run_id=str(run_raw) if run_raw is not None else None,
            captured_at=decode_utc(str(row["captured_at"])),
            account=account,
            positions=restored_positions,
        )

    def get_equity_snapshot(self, snapshot_id: str) -> EquitySnapshotRecord | None:
        row = (
            self._session.execute(
                select(equity_snapshots).where(equity_snapshots.c.snapshot_id == snapshot_id)
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._equity_snapshot_from_row(row)

    def list_equity_snapshots(
        self,
        *,
        account_id: str | None = None,
    ) -> list[EquitySnapshotRecord]:
        statement = select(equity_snapshots)
        if account_id is not None:
            statement = statement.where(equity_snapshots.c.account_id == account_id)
        rows = (
            self._session.execute(
                statement.order_by(
                    equity_snapshots.c.captured_at,
                    equity_snapshots.c.snapshot_id,
                )
            )
            .mappings()
            .all()
        )
        return [self._equity_snapshot_from_row(row) for row in rows]

    @staticmethod
    def _equity_snapshot_from_row(row: RowMapping) -> EquitySnapshotRecord:
        return EquitySnapshotRecord(
            snapshot_id=str(row["snapshot_id"]),
            account_snapshot_id=str(row["account_snapshot_id"]),
            account_id=str(row["account_id"]),
            captured_at=decode_utc(str(row["captured_at"])),
            equity=decode_decimal(str(row["equity"])),
            cash=decode_decimal(str(row["cash"])),
        )

    def save_reconciliation(
        self,
        reconciliation_id: str,
        *,
        status: ReconciliationStatus,
        started_at: datetime,
        completed_at: datetime,
        diffs: Sequence[Mapping[str, object]],
        run_id: str | None = None,
        summary: Mapping[str, object] | None = None,
    ) -> bool:
        payload = {
            "reconciliation_id": reconciliation_id,
            "run_id": run_id,
            "status": status.value,
            "started_at": encode_utc(started_at),
            "completed_at": encode_utc(completed_at),
            "summary_json": _encode_json(summary),
        }
        diff_payloads = [
            self._reconciliation_diff_payload(reconciliation_id, diff) for diff in diffs
        ]
        if len({diff["diff_id"] for diff in diff_payloads}) != len(diff_payloads):
            raise ValueError("reconciliation diff IDs must be unique")
        existing = (
            self._session.execute(
                select(reconciliation_runs).where(
                    reconciliation_runs.c.reconciliation_id == reconciliation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            stored_diffs = (
                self._session.execute(
                    select(reconciliation_diffs)
                    .where(reconciliation_diffs.c.reconciliation_id == reconciliation_id)
                    .order_by(reconciliation_diffs.c.diff_id)
                )
                .mappings()
                .all()
            )
            expected_diffs = sorted(diff_payloads, key=lambda item: str(item["diff_id"]))
            if _row_matches(existing, payload) and len(stored_diffs) == len(expected_diffs):
                if all(
                    _row_matches(stored, expected)
                    for stored, expected in zip(stored_diffs, expected_diffs, strict=True)
                ):
                    return False
            raise StateConflictError(
                f"reconciliation {reconciliation_id} conflicts with stored data"
            )
        self._session.execute(reconciliation_runs.insert().values(**payload))
        if diff_payloads:
            self._session.execute(reconciliation_diffs.insert(), diff_payloads)
        return True

    @staticmethod
    def _reconciliation_diff_payload(
        reconciliation_id: str,
        diff: Mapping[str, object],
    ) -> dict[str, object]:
        required = ("diff_id", "category", "key", "expected", "actual")
        missing = [key for key in required if key not in diff]
        if missing:
            raise ValueError(f"reconciliation diff missing fields: {', '.join(missing)}")
        expected = diff["expected"]
        actual = diff["actual"]
        if not isinstance(expected, Mapping) or not isinstance(actual, Mapping):
            raise TypeError("reconciliation expected and actual values must be mappings")
        return {
            "diff_id": str(diff["diff_id"]),
            "reconciliation_id": reconciliation_id,
            "category": str(diff["category"]),
            "record_key": str(diff["key"]),
            "expected_json": _encode_json(cast(Mapping[str, object], expected)),
            "actual_json": _encode_json(cast(Mapping[str, object], actual)),
        }

    def get_reconciliation(self, reconciliation_id: str) -> ReconciliationRecord | None:
        row = (
            self._session.execute(
                select(reconciliation_runs).where(
                    reconciliation_runs.c.reconciliation_id == reconciliation_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        diff_rows = (
            self._session.execute(
                select(reconciliation_diffs)
                .where(reconciliation_diffs.c.reconciliation_id == reconciliation_id)
                .order_by(reconciliation_diffs.c.diff_id)
            )
            .mappings()
            .all()
        )
        restored_diffs = tuple(
            ReconciliationDiffRecord(
                diff_id=str(diff["diff_id"]),
                category=str(diff["category"]),
                key=str(diff["record_key"]),
                expected=_decode_json(diff["expected_json"]),
                actual=_decode_json(diff["actual_json"]),
            )
            for diff in diff_rows
        )
        run_raw = row["run_id"]
        return ReconciliationRecord(
            reconciliation_id=str(row["reconciliation_id"]),
            run_id=str(run_raw) if run_raw is not None else None,
            status=ReconciliationStatus(str(row["status"])),
            started_at=decode_utc(str(row["started_at"])),
            completed_at=decode_utc(str(row["completed_at"])),
            summary=_decode_json(row["summary_json"]),
            diffs=restored_diffs,
        )

    def create_incident(
        self,
        incident_id: str,
        *,
        severity: str,
        kind: str,
        message: str,
        occurred_at: datetime,
        run_id: str | None = None,
        resolved_at: datetime | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        payload = {
            "incident_id": incident_id,
            "run_id": run_id,
            "severity": severity,
            "kind": kind,
            "message": message,
            "occurred_at": encode_utc(occurred_at),
            "resolved_at": encode_utc(resolved_at) if resolved_at is not None else None,
            "detail_json": _encode_json(detail),
        }
        row = (
            self._session.execute(select(incidents).where(incidents.c.incident_id == incident_id))
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(incidents.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(f"incident {incident_id} conflicts with stored data")

    def list_incidents(self, *, unresolved_only: bool = False) -> list[IncidentRecord]:
        statement = select(incidents)
        if unresolved_only:
            statement = statement.where(incidents.c.resolved_at.is_(None))
        rows = (
            self._session.execute(
                statement.order_by(incidents.c.occurred_at, incidents.c.incident_id)
            )
            .mappings()
            .all()
        )
        records: list[IncidentRecord] = []
        for row in rows:
            run_raw = row["run_id"]
            resolved_raw = row["resolved_at"]
            records.append(
                IncidentRecord(
                    incident_id=str(row["incident_id"]),
                    run_id=str(run_raw) if run_raw is not None else None,
                    severity=str(row["severity"]),
                    kind=str(row["kind"]),
                    message=str(row["message"]),
                    occurred_at=decode_utc(str(row["occurred_at"])),
                    resolved_at=(
                        decode_utc(str(resolved_raw)) if resolved_raw is not None else None
                    ),
                    detail=_decode_json(row["detail_json"]),
                )
            )
        return records

    def get_kill_switch_state(self) -> KillSwitchState:
        row = (
            self._session.execute(select(kill_switch_state).where(kill_switch_state.c.id == 1))
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RecordNotFoundError("kill switch singleton is missing")
        return KillSwitchState(
            engaged=bool(row["engaged"]),
            reason=str(row["reason"]),
            updated_at=decode_utc(str(row["updated_at"])),
        )

    def set_kill_switch(
        self,
        engaged: bool,
        *,
        reason: str,
        updated_at: datetime,
    ) -> KillSwitchState:
        result = self._session.connection().execute(
            update(kill_switch_state)
            .where(kill_switch_state.c.id == 1)
            .values(engaged=engaged, reason=reason, updated_at=encode_utc(updated_at))
        )
        if result.rowcount != 1:
            raise RecordNotFoundError("kill switch singleton is missing")
        return KillSwitchState(engaged=engaged, reason=reason, updated_at=updated_at)

    def record_qualification_day(
        self,
        strategy_id: str,
        trading_date: date,
        *,
        qualified: bool,
        detail: Mapping[str, object] | None = None,
    ) -> bool:
        payload = {
            "strategy_id": strategy_id,
            "trading_date": trading_date.isoformat(),
            "qualified": qualified,
            "detail_json": _encode_json(detail),
        }
        row = (
            self._session.execute(
                select(qualification_days).where(
                    qualification_days.c.strategy_id == strategy_id,
                    qualification_days.c.trading_date == payload["trading_date"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            self._session.execute(qualification_days.insert().values(**payload))
            return True
        if _row_matches(row, payload):
            return False
        raise StateConflictError(
            f"qualification day {strategy_id}:{payload['trading_date']} conflicts with stored data"
        )

    def get_qualification_day(
        self,
        strategy_id: str,
        trading_date: date,
    ) -> QualificationDayRecord | None:
        row = (
            self._session.execute(
                select(qualification_days).where(
                    qualification_days.c.strategy_id == strategy_id,
                    qualification_days.c.trading_date == trading_date.isoformat(),
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else self._qualification_day_from_row(row)

    def list_qualification_days(
        self,
        strategy_id: str,
        *,
        qualified: bool | None = None,
    ) -> list[QualificationDayRecord]:
        statement = select(qualification_days).where(
            qualification_days.c.strategy_id == strategy_id
        )
        if qualified is not None:
            statement = statement.where(qualification_days.c.qualified.is_(qualified))
        rows = (
            self._session.execute(statement.order_by(qualification_days.c.trading_date))
            .mappings()
            .all()
        )
        return [self._qualification_day_from_row(row) for row in rows]

    @staticmethod
    def _qualification_day_from_row(row: RowMapping) -> QualificationDayRecord:
        return QualificationDayRecord(
            strategy_id=str(row["strategy_id"]),
            trading_date=date.fromisoformat(str(row["trading_date"])),
            qualified=bool(row["qualified"]),
            detail=_decode_json(row["detail_json"]),
        )

    def count_qualification_days(self, strategy_id: str) -> int:
        rows = self._session.execute(
            select(qualification_days.c.trading_date).where(
                qualification_days.c.strategy_id == strategy_id,
                qualification_days.c.qualified.is_(True),
            )
        ).all()
        return len(rows)
