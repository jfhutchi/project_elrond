"""Read-only broker reconciliation with durable, deterministic differences."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from pydantic import Field, field_validator, model_validator

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Account, BrokerOrder, Fill, Position, ReconciliationStatus
from quantbot.storage import StorageRepository

T = TypeVar("T")


class ReconciliationPolicy(BrokerModel):
    """Only currency-denominated values receive a configurable tolerance."""

    currency_places: int = Field(default=2, ge=0, le=8)

    @property
    def money_tolerance(self) -> Decimal:
        return Decimal(1).scaleb(-self.currency_places)


class ReconciliationSnapshot(BrokerModel):
    """Comparable local or broker state captured at one reconciliation boundary."""

    account: Account
    positions: tuple[Position, ...] = ()
    open_orders: tuple[BrokerOrder, ...] = ()
    fills: tuple[Fill, ...] = ()

    @field_validator("positions")
    @classmethod
    def normalize_positions(cls, value: tuple[Position, ...]) -> tuple[Position, ...]:
        return _unique_sorted(value, key=lambda item: item.symbol, label="position symbol")

    @field_validator("open_orders")
    @classmethod
    def normalize_open_orders(cls, value: tuple[BrokerOrder, ...]) -> tuple[BrokerOrder, ...]:
        return _unique_sorted(
            value,
            key=lambda item: item.client_order_id,
            label="open-order client ID",
        )

    @field_validator("fills")
    @classmethod
    def normalize_fills(cls, value: tuple[Fill, ...]) -> tuple[Fill, ...]:
        return _unique_sorted(value, key=lambda item: item.fill_id, label="fill ID")


class ReconciliationDiff(BrokerModel):
    diff_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    key: str = Field(min_length=1)
    expected: dict[str, str]
    actual: dict[str, str]


class ReconciliationResult(BrokerModel):
    status: ReconciliationStatus
    diffs: tuple[ReconciliationDiff, ...]

    @model_validator(mode="after")
    def validate_status(self) -> ReconciliationResult:
        has_account_mismatch = any(
            diff.category == "ACCOUNT" and diff.key == "account_id" for diff in self.diffs
        )
        if self.status is ReconciliationStatus.RECONCILED and self.diffs:
            raise ValueError("reconciled result cannot contain material differences")
        if self.status is not ReconciliationStatus.RECONCILED and not self.diffs:
            raise ValueError("failed reconciliation must contain a material difference")
        if self.status is ReconciliationStatus.ACCOUNT_MISMATCH and not has_account_mismatch:
            raise ValueError("account mismatch status requires an account identity difference")
        if has_account_mismatch and self.status is not ReconciliationStatus.ACCOUNT_MISMATCH:
            raise ValueError("account identity difference requires account mismatch status")
        return self

    @property
    def allows_new_orders(self) -> bool:
        return self.status is ReconciliationStatus.RECONCILED and not self.diffs


def _unique_sorted(
    values: tuple[T, ...],
    *,
    key: Callable[[T], str],
    label: str,
) -> tuple[T, ...]:
    identifiers = [key(value) for value in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"reconciliation snapshot must have unique {label}s")
    return tuple(sorted(values, key=key))


def _decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _stable_diff(
    category: str,
    key: str,
    expected: Mapping[str, str],
    actual: Mapping[str, str],
) -> ReconciliationDiff:
    payload = {
        "actual": dict(actual),
        "category": category,
        "expected": dict(expected),
        "key": key,
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ReconciliationDiff(
        diff_id=f"diff-{digest}",
        category=category,
        key=key,
        expected=dict(expected),
        actual=dict(actual),
    )


def _account_diffs(
    expected: Account,
    actual: Account,
    policy: ReconciliationPolicy,
) -> list[ReconciliationDiff]:
    diffs: list[ReconciliationDiff] = []
    for key, expected_identity, actual_identity in (
        ("account_id", expected.account_id, actual.account_id),
        ("currency", expected.currency, actual.currency),
    ):
        if expected_identity != actual_identity:
            diffs.append(
                _stable_diff(
                    "ACCOUNT",
                    key,
                    {"value": expected_identity},
                    {"value": actual_identity},
                )
            )
    for key, expected_money, actual_money in (
        ("cash", expected.cash, actual.cash),
        ("equity", expected.equity, actual.equity),
        ("buying_power", expected.buying_power, actual.buying_power),
    ):
        if abs(expected_money - actual_money) >= policy.money_tolerance:
            diffs.append(
                _stable_diff(
                    "ACCOUNT",
                    key,
                    {"value": _decimal(expected_money)},
                    {"value": _decimal(actual_money)},
                )
            )
    return diffs


def _position_payload(position: Position) -> dict[str, str]:
    return {"quantity": _decimal(position.quantity)}


def _order_payload(order: BrokerOrder) -> dict[str, str]:
    return {
        "broker_order_id": order.broker_order_id,
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "time_in_force": order.time_in_force.value,
        "quantity": _decimal(order.quantity),
        "filled_quantity": _decimal(order.filled_quantity),
        "status": order.status,
        "submitted_at": _timestamp(order.submitted_at),
        "filled_average_price": (
            _decimal(order.filled_average_price) if order.filled_average_price is not None else ""
        ),
    }


def _fill_payload(fill: Fill) -> dict[str, str]:
    return {
        "broker_order_id": fill.broker_order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": _decimal(fill.quantity),
        "price": _decimal(fill.price),
        "occurred_at": _timestamp(fill.occurred_at),
        "fee": _decimal(fill.fee),
    }


def _collection_diffs(
    category: str,
    expected: Sequence[T],
    actual: Sequence[T],
    *,
    identity: Callable[[T], str],
    payload: Callable[[T], dict[str, str]],
) -> list[ReconciliationDiff]:
    expected_by_key = {identity(item): item for item in expected}
    actual_by_key = {identity(item): item for item in actual}
    diffs: list[ReconciliationDiff] = []
    for key in sorted(set(expected_by_key) | set(actual_by_key)):
        expected_item = expected_by_key.get(key)
        actual_item = actual_by_key.get(key)
        expected_payload = {"present": "false"} if expected_item is None else payload(expected_item)
        actual_payload = {"present": "false"} if actual_item is None else payload(actual_item)
        if expected_payload != actual_payload:
            diffs.append(_stable_diff(category, str(key), expected_payload, actual_payload))
    return diffs


def compare_reconciliation(
    expected: ReconciliationSnapshot,
    actual: ReconciliationSnapshot,
    policy: ReconciliationPolicy | None = None,
) -> ReconciliationResult:
    """Compare authoritative observations without changing broker state."""
    effective_policy = policy or ReconciliationPolicy()
    diffs = _account_diffs(expected.account, actual.account, effective_policy)
    diffs.extend(
        _collection_diffs(
            "POSITION",
            expected.positions,
            actual.positions,
            identity=lambda item: item.symbol,
            payload=_position_payload,
        )
    )
    diffs.extend(
        _collection_diffs(
            "OPEN_ORDER",
            expected.open_orders,
            actual.open_orders,
            identity=lambda item: item.client_order_id,
            payload=_order_payload,
        )
    )
    diffs.extend(
        _collection_diffs(
            "FILL",
            expected.fills,
            actual.fills,
            identity=lambda item: item.fill_id,
            payload=_fill_payload,
        )
    )
    ordered = tuple(sorted(diffs, key=lambda item: (item.category, item.key, item.diff_id)))
    account_mismatch = any(
        diff.category == "ACCOUNT" and diff.key == "account_id" for diff in ordered
    )
    if account_mismatch:
        status = ReconciliationStatus.ACCOUNT_MISMATCH
    elif ordered:
        status = ReconciliationStatus.RECONCILIATION_REQUIRED
    else:
        status = ReconciliationStatus.RECONCILED
    return ReconciliationResult(status=status, diffs=ordered)


def missing_local_snapshot_result(actual: Account) -> ReconciliationResult:
    """Fail closed when no durable local account state exists to compare."""
    diff = _stable_diff(
        "ACCOUNT",
        "snapshot",
        {"present": "false"},
        {"account_id": actual.account_id, "present": "true"},
    )
    return ReconciliationResult(
        status=ReconciliationStatus.RECONCILIATION_REQUIRED,
        diffs=(diff,),
    )


def record_reconciliation(
    repository: StorageRepository,
    reconciliation_id: str,
    result: ReconciliationResult,
    *,
    started_at: datetime,
    completed_at: datetime,
    run_id: str | None = None,
) -> bool:
    """Persist the complete comparison inside the caller-owned transaction."""
    if completed_at < started_at:
        raise ValueError("reconciliation cannot complete before it starts")
    return repository.save_reconciliation(
        reconciliation_id,
        status=result.status,
        started_at=started_at,
        completed_at=completed_at,
        diffs=[diff.model_dump(mode="python") for diff in result.diffs],
        run_id=run_id,
        summary={
            "allows_new_orders": result.allows_new_orders,
            "diff_count": len(result.diffs),
        },
    )
