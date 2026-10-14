"""Immutable M1 contracts. Execution config is synthetic-only, separate from M0 manifest."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Account, BrokerOrder, Fill, OrderIntent, Position
from quantbot.research.manifest import content_id

Money = Annotated[Decimal, Field(allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class CoreConfig(BrokerModel):
    """Temporary execution contract; cannot authorize a real broker or LIVE lane."""

    account_id: str = Field(min_length=1)
    symbol: str
    release_id: str = "m1-synthetic-fixture"
    policy_id: str = "collect-owned-30m-stop100-v1"
    owner: str = "paper-core-synthetic"
    generation: Literal[1] = 1
    evidence_lane: Literal["SYNTHETIC_TEST"] = "SYNTHETIC_TEST"
    max_positions: Literal[1] = 1
    position_bps: Literal[50] = 50
    entry_risk_bps: Literal[25] = 25
    stop_bps: Literal[100] = 100
    horizon_minutes: Literal[30] = 30
    exit_buffer_seconds: Literal[120] = 120
    buying_power_reserve_bps: Literal[500] = 500
    clock_profile: Literal["STRICT_V0", "PAPER_TIME_V2"] = "STRICT_V0"

    @model_serializer(mode="wrap")
    def preserve_v31_config(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        if "clock_profile" not in self.model_fields_set:
            payload.pop("clock_profile", None)
        return payload


class Evidence(BrokerModel):
    legacy_optional_fields: ClassVar[frozenset[str]] = frozenset()
    schema_version: Literal[1] = 1
    release_id: str = "m1-synthetic-fixture"
    evidence_lane: Literal["SYNTHETIC_TEST"] = "SYNTHETIC_TEST"
    recorded_at_utc: datetime
    source_refs: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def preserve_legacy_document(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = handler(self)
        # Added optional contract fields are serialized only when supplied. A
        # decoded v30 document therefore retains its original canonical digest,
        # and new documents round-trip with the same explicit field set.
        for field in self.legacy_optional_fields - self.model_fields_set:
            payload.pop(field, None)
        return dict(payload)

    @property
    def id(self) -> str:
        return content_id(self)


class Observation(Evidence):
    legacy_optional_fields = frozenset({"time_evidence_id", "session_open"})
    time_evidence_id: str | None = None
    session_open: datetime | None = None
    symbol: str
    feed: Literal["EXPLICIT_SYNTHETIC"] = "EXPLICIT_SYNTHETIC"
    source_start: datetime
    source_end: datetime
    available_at: datetime
    raw_data_digest: str = Field(min_length=1)
    price: Positive
    features: tuple[tuple[str, float], ...]
    feature_version: Literal["synthetic-causal-v1"] = "synthetic-causal-v1"
    dependencies: tuple[str, ...]
    dependency_available_at: tuple[datetime | None, ...]
    sequence: int = Field(ge=1)
    session_close: datetime


class ModelArtifact(BrokerModel):
    mode: Literal["COLLECT"] = "COLLECT"
    description: Literal["nonpredictive baseline; no fitted model"] = (
        "nonpredictive baseline; no fitted model"
    )
    feature_version: Literal["synthetic-causal-v1"] = "synthetic-causal-v1"
    learning_receipt_id: Literal["M1_NO_LEARNING"] = "M1_NO_LEARNING"

    @property
    def id(self) -> str:
        return content_id(self)


class ReconciliationProof(Evidence):
    legacy_optional_fields = frozenset({"cash_complete"})
    account: Account
    baseline_id: str
    generation: int = Field(ge=1)
    fill_ids: tuple[str, ...]
    expected_quantities: tuple[tuple[str, Money], ...]
    actual_quantities: tuple[tuple[str, Money], ...]
    positions: tuple[Position, ...]
    expected_cash: Money | None
    cash_complete: bool = True
    acquisition_id: str
    complete: bool
    order_ids: tuple[str, ...] = ()


class TimeProjection(BrokerModel):
    """Immutable evaluation-time projection of a retained acquisition proof."""

    evidence_id: str
    process_id: str
    wall_at: datetime
    monotonic_at: float
    provider_earliest: datetime
    provider_latest: datetime
    objections: tuple[str, ...]


class Decision(Evidence):
    legacy_optional_fields = frozenset(
        {
            "source_start",
            "time_evidence_id",
            "clock_process_id",
            "monotonic_anchor",
            "provider_deadline",
            "time_projection",
        }
    )
    time_evidence_id: str | None = None
    clock_process_id: str | None = None
    monotonic_anchor: float | None = None
    provider_deadline: datetime | None = None
    time_projection: TimeProjection | None = None
    observation_id: str
    policy_id: str
    model_id: str
    learning_receipt_id: str
    config_id: str
    inventory_id: str
    inventory_generation: int
    symbol: str
    action: Literal["BUY", "WAIT"]
    gates: tuple[str, ...]
    quantity: Money
    price: Positive
    valid_until: datetime
    session_close: datetime
    source_start: datetime | None = None


class ProtectionPlan(Evidence):
    legacy_optional_fields = frozenset(
        {"clock_profile", "clock_process_id", "monotonic_anchor", "monotonic_expiry"}
    )
    clock_profile: Literal["STRICT_V0", "PAPER_TIME_V2"] = "STRICT_V0"
    clock_process_id: str | None = None
    monotonic_anchor: float | None = None
    monotonic_expiry: float | None = None
    decision_id: str
    owner: str
    generation: int
    inventory_generation: int
    stop_price: Positive
    deadline: datetime
    exit_buffer_seconds: Literal[120] = 120
    exit_quantity_rule: Literal["owned-filled-minus-owned-exited"] = (
        "owned-filled-minus-owned-exited"
    )

    @classmethod
    def for_decision(cls, decision: Decision, config: CoreConfig) -> Self:
        timing: dict[str, Any] = {}
        if config.clock_profile == "PAPER_TIME_V2":
            timing = {
                "clock_profile": config.clock_profile,
                "clock_process_id": decision.clock_process_id,
                "monotonic_anchor": decision.monotonic_anchor,
            }
            if (
                decision.time_projection is not None
                and decision.monotonic_anchor is not None
                and decision.provider_deadline is not None
            ):
                duration = max(
                    0,
                    min(
                        1800,
                        (
                            decision.provider_deadline - decision.time_projection.provider_latest
                        ).total_seconds(),
                    ),
                )
                timing["monotonic_expiry"] = decision.monotonic_anchor + duration
        return cls(
            **timing,
            recorded_at_utc=decision.recorded_at_utc,
            release_id=config.release_id,
            source_refs=(decision.id,),
            decision_id=decision.id,
            owner=config.owner,
            generation=config.generation,
            inventory_generation=decision.inventory_generation,
            stop_price=decision.price * Decimal("0.99"),
            deadline=decision.provider_deadline
            or min(
                decision.recorded_at_utc + timedelta(minutes=30),
                decision.session_close - timedelta(seconds=120),
            ),
        )


class EntryIntent(BrokerModel):
    intent: OrderIntent
    decision_id: str
    protection_id: str


class GuardianReceipt(Evidence):
    owner: str
    generation: int
    valid_until: datetime
    protection_ids: tuple[str, ...] = ()


class CycleReceipt(Evidence):
    exit_intent_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    proof_id: str | None = None


class RoundTrip(Evidence):
    legacy_optional_fields = frozenset(
        {
            "cost_item_ids",
            "actual_fees",
            "modeled_costs",
            "modeled_net_pnl",
            "source_state_hash",
            "revision",
            "supersedes",
            "entry_notional",
            "policy_id",
            "dataset",
            "source_start",
            "source_end",
            "available_at",
        }
    )
    lot_id: str
    decision_id: str
    entry_fill_ids: tuple[str, ...]
    exit_fill_ids: tuple[str, ...]
    terminal_order_ids: tuple[str, ...]
    protection_id: str
    reconciliation_id: str
    open_quantity: Money
    gross_pnl: Money
    net_pnl: Money | None = None
    status: Literal["COSTS_PENDING", "COMPLETE"] = "COSTS_PENDING"
    cost_reason: str = "M1_COST_PROVENANCE_UNAVAILABLE"
    cost_item_ids: tuple[str, ...] = ()
    actual_fees: Money | None = None
    modeled_costs: Money | None = None
    modeled_net_pnl: Money | None = None
    source_state_hash: str = ""
    revision: int = Field(default=1, ge=1)
    supersedes: str | None = None
    entry_notional: Money = Decimal(0)
    policy_id: str = "collect-owned-30m-stop100-v1"
    dataset: str = "EXPLICIT_SYNTHETIC"
    source_start: datetime | None = None
    source_end: datetime | None = None
    available_at: datetime | None = None

    @model_validator(mode="after")
    def completed_owned_lot(self) -> Self:
        if self.open_quantity != 0 or not self.entry_fill_ids or not self.exit_fill_ids:
            raise ValueError("round trip requires attributable nonempty fills and owned flat")
        return self


class CostItem(Evidence):
    fill_id: str
    component: Literal["FEE", "MODELED_SLIPPAGE"] = "FEE"
    amount: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None
    currency: Literal["USD"] = "USD"
    source: Literal["BROKER_REPORTED", "CONTRACTUAL_ZERO", "MODELED", "UNKNOWN"] = "UNKNOWN"
    document_id: str | None = None
    effective_start: datetime
    effective_end: datetime
    allocation_rule: Literal["EXACT_FILL"] = "EXACT_FILL"
    revision: int = Field(default=1, ge=1)
    supersedes: str | None = None

    @model_validator(mode="after")
    def authority(self) -> Self:
        if self.effective_end < self.effective_start:
            raise ValueError("cost effective dates are reversed")
        if self.source == "UNKNOWN" and self.amount is not None:
            raise ValueError("unknown cost cannot provide an amount")
        if self.source != "UNKNOWN" and (self.amount is None or not self.document_id):
            raise ValueError("known cost requires amount and document authority")
        if self.source == "CONTRACTUAL_ZERO" and self.amount != 0:
            raise ValueError("contractual zero requires exactly zero")
        if (self.source == "MODELED") != (self.component == "MODELED_SLIPPAGE"):
            raise ValueError("modeled economics must be separate from actual fees")
        if (self.revision == 1) != (self.supersedes is None):
            raise ValueError("cost revision requires exact predecessor")
        return self


class CashActivity(Evidence):
    account_id: str
    document_id: str = Field(min_length=1)
    activity_ids: tuple[str, ...]
    movements: tuple[tuple[str, Money], ...]
    currency: Literal["USD"] = "USD"
    complete: bool
    watermark: datetime


class BrokerEvent(Evidence):
    event_id: str = Field(min_length=1)
    raw_digest: str = Field(min_length=1)
    provider: Literal["EXPLICIT_SYNTHETIC"] = "EXPLICIT_SYNTHETIC"
    account_id: str
    endpoint: str = Field(min_length=1)
    client_order_id: str
    occurred_at: datetime
    order: BrokerOrder | None = None
    fill: Fill | None = None
    cost: CostItem | None = None
    cash_activity: CashActivity | None = None
    activity_complete: bool = False
    activity_fill_ids: tuple[str, ...] = ()
    activity_watermark: datetime | None = None
    correction_of: str | None = None

    @model_validator(mode="after")
    def event_payload(self) -> Self:
        if sum(v is not None for v in (self.order, self.fill, self.cost, self.cash_activity)) != 1:
            raise ValueError("event requires one order, fill or cost payload")
        if self.occurred_at > self.recorded_at_utc:
            raise ValueError("event cannot be received before occurrence")
        if self.activity_complete and self.order is None:
            raise ValueError("activity watermark belongs to an order observation")
        if self.activity_complete and (
            self.activity_watermark is None or self.activity_watermark > self.recorded_at_utc
        ):
            raise ValueError("complete activities require a provider watermark")
        return self


class OutcomeBatch(Evidence):
    outcomes: tuple[RoundTrip, ...] = ()
    reasons: tuple[str, ...] = ()


class Health(Evidence):
    role: Literal["TRADER", "GUARDIAN", "OUTCOMES"]
    scope: Literal["ACCOUNT", "SYMBOL", "LOT", "LEARNING"]
    scope_id: str
    state: Literal[
        "READY",
        "TEMPORARY_WAIT",
        "RETRYABLE_FAILURE",
        "PROTOCOL_FAILURE",
        "MANUAL_PAUSE",
        "INTEGRITY_FAILURE",
    ]
    objections: tuple[str, ...]
    first_blocker: str | None
    first_seen: datetime
    last_seen: datetime
    evidence_ids: tuple[str, ...] = ()
    evidence_age_seconds: float | None = None
    last_useful_progress: datetime | None = None
    next_retry: datetime | None = None
    recovery_requirements: tuple[str, ...] = ()
    recovered_from: tuple[str, ...] = ()
    new_proof_id: str | None = None
    sequence: int = 0
    failed_generation: int = 0
    protection_blocked: bool = False
    escalated: bool = False
