"""Secret-safe operational readiness aggregation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.brokers import BrokerHealth
from quantbot.domain import ReconciliationStatus


class OperationsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class DataFreshness(OperationsModel):
    completed_through: datetime
    checked_at: datetime
    max_age_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_time_order(self) -> DataFreshness:
        if self.completed_through > self.checked_at:
            raise ValueError("completed market data cannot be in the future")
        return self

    @property
    def age_seconds(self) -> Decimal:
        return Decimal(str((self.checked_at - self.completed_through).total_seconds()))

    @property
    def stale(self) -> bool:
        return self.age_seconds > Decimal(self.max_age_seconds)


class HealthInputs(OperationsModel):
    broker: BrokerHealth
    data: DataFreshness
    stream_connected: bool
    risk_healthy: bool
    reconciliation_status: ReconciliationStatus
    account_verified: bool
    kill_switch_engaged: bool


class OperationalHealth(OperationsModel):
    ready: bool
    reasons: tuple[str, ...]
    broker_provider: str
    broker_environment: str
    account_verified: bool
    market_data_age_seconds: Decimal
    stream_connected: bool
    risk_healthy: bool
    reconciliation_status: ReconciliationStatus
    kill_switch_engaged: bool

    def safe_summary(self) -> dict[str, str | bool]:
        return {
            "ready": self.ready,
            "broker_provider": self.broker_provider,
            "broker_environment": self.broker_environment,
            "account_verified": self.account_verified,
            "stream_connected": self.stream_connected,
            "risk_healthy": self.risk_healthy,
            "reconciliation_status": self.reconciliation_status.value,
            "kill_switch_engaged": self.kill_switch_engaged,
        }


def evaluate_operational_health(inputs: HealthInputs) -> OperationalHealth:
    reasons: list[str] = []
    if not inputs.broker.healthy:
        reasons.append("BROKER_UNHEALTHY")
    if not inputs.account_verified:
        reasons.append("ACCOUNT_VERIFICATION_REQUIRED")
    if inputs.data.stale:
        reasons.append("MARKET_DATA_STALE")
    if not inputs.stream_connected:
        reasons.append("TRADE_STREAM_DISCONNECTED")
    if not inputs.risk_healthy:
        reasons.append("RISK_ENGINE_UNHEALTHY")
    if inputs.reconciliation_status is not ReconciliationStatus.RECONCILED:
        reasons.append(inputs.reconciliation_status.value)
    if inputs.kill_switch_engaged:
        reasons.append("KILL_SWITCH_ENGAGED")
    ordered = tuple(dict.fromkeys(reasons))
    return OperationalHealth(
        ready=not ordered,
        reasons=ordered,
        broker_provider=inputs.broker.provider,
        broker_environment=inputs.broker.environment,
        account_verified=inputs.account_verified,
        market_data_age_seconds=inputs.data.age_seconds,
        stream_connected=inputs.stream_connected,
        risk_healthy=inputs.risk_healthy,
        reconciliation_status=inputs.reconciliation_status,
        kill_switch_engaged=inputs.kill_switch_engaged,
    )
