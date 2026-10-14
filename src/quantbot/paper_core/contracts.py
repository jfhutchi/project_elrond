"""Immutable M1 contracts. Execution config is synthetic-only, separate from M0 manifest."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Account, OrderIntent, Position
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


class Evidence(BrokerModel):
    schema_version: Literal[1] = 1
    release_id: str = "m1-synthetic-fixture"
    evidence_lane: Literal["SYNTHETIC_TEST"] = "SYNTHETIC_TEST"
    recorded_at_utc: datetime
    source_refs: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return content_id(self)


class Observation(Evidence):
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
    dependency_available_at: tuple[datetime, ...]
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
    account: Account
    baseline_id: str
    generation: int = Field(ge=1)
    fill_ids: tuple[str, ...]
    expected_quantities: tuple[tuple[str, Money], ...]
    actual_quantities: tuple[tuple[str, Money], ...]
    positions: tuple[Position, ...]
    expected_cash: Money
    acquisition_id: str
    complete: bool
    order_ids: tuple[str, ...] = ()


class Decision(Evidence):
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


class ProtectionPlan(Evidence):
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
        return cls(
            recorded_at_utc=decision.recorded_at_utc,
            release_id=config.release_id,
            source_refs=(decision.id,),
            decision_id=decision.id,
            owner=config.owner,
            generation=config.generation,
            inventory_generation=decision.inventory_generation,
            stop_price=decision.price * Decimal("0.99"),
            deadline=min(
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
    lot_id: str
    decision_id: str
    entry_fill_ids: tuple[str, ...]
    exit_fill_ids: tuple[str, ...]
    terminal_order_ids: tuple[str, ...]
    protection_id: str
    reconciliation_id: str
    open_quantity: Money
    gross_pnl: Money
    net_pnl: None = None
    status: Literal["COSTS_PENDING"] = "COSTS_PENDING"
    cost_reason: Literal["M1_COST_PROVENANCE_UNAVAILABLE"] = "M1_COST_PROVENANCE_UNAVAILABLE"

    @model_validator(mode="after")
    def completed_owned_lot(self) -> Self:
        if self.open_quantity != 0 or not self.entry_fill_ids or not self.exit_fill_ids:
            raise ValueError("round trip requires attributable nonempty fills and owned flat")
        return self
