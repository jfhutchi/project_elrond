"""Immutable M1 contracts. Execution config is synthetic-only, separate from M0 manifest."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Account, BrokerOrder, Fill, OrderIntent, Position
from quantbot.learner.estimators import FittedEstimator
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.research.manifest import content_id
from quantbot.strategy import XNYSSessionSequence

Money = Annotated[Decimal, Field(allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class CoreConfig(BrokerModel):
    """Temporary execution contract; cannot authorize a real broker or LIVE lane."""

    account_id: str = Field(min_length=1)
    symbol: str
    symbols: tuple[str, ...] = ()
    selection_policy: Literal["MODEL_ONLY_V1", "SIGNAL_OR_EXPLORE_V1"] = "MODEL_ONLY_V1"
    release_id: str = "m1-synthetic-fixture"
    policy_id: str = "collect-owned-30m-stop100-v1"
    owner: str = "paper-core-synthetic"
    generation: int = Field(default=1, ge=1)
    evidence_lane: Literal["SYNTHETIC_TEST", "LOCAL_SIM_REAL_DATA", "BROKER_PAPER"] = (
        "SYNTHETIC_TEST"
    )
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
        for name in {"clock_profile", "symbols", "selection_policy"} - self.model_fields_set:
            payload.pop(name, None)
        return payload

    @property
    def entry_symbols(self) -> tuple[str, ...]:
        return self.symbols or (self.symbol,)

    @model_validator(mode="after")
    def stock_universe(self) -> Self:
        if self.symbols or self.selection_policy == "SIGNAL_OR_EXPLORE_V1":
            if (
                len(self.entry_symbols) > 32
                or len(set(self.entry_symbols)) != len(self.entry_symbols)
                or any(
                    re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", s) is None or s == "SPY"
                    for s in self.entry_symbols
                )
            ):
                raise ValueError(
                    "unique uppercase supported stock universe, excluding SPY, required"
                )
        if self.selection_policy == "MODEL_ONLY_V1" and self.symbols not in {(), (self.symbol,)}:
            raise ValueError("multi-symbol universe requires SIGNAL_OR_EXPLORE_V1")
        return self


class Evidence(BrokerModel):
    legacy_optional_fields: ClassVar[frozenset[str]] = frozenset()
    schema_version: Literal[1] = 1
    release_id: str = "m1-synthetic-fixture"
    evidence_lane: Literal["SYNTHETIC_TEST", "LOCAL_SIM_REAL_DATA", "BROKER_PAPER"] = (
        "SYNTHETIC_TEST"
    )
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
    legacy_optional_fields = frozenset({"time_evidence_id", "session_open", "capture_received_at"})
    capture_received_at: datetime | None = None
    time_evidence_id: str | None = None
    session_open: datetime | None = None
    symbol: str
    feed: Literal["EXPLICIT_SYNTHETIC", "LOCAL_SIM_REAL_DATA", "alpaca"] = "EXPLICIT_SYNTHETIC"
    source_start: datetime
    source_end: datetime
    available_at: datetime
    raw_data_digest: str = Field(min_length=1)
    price: Positive
    features: tuple[tuple[str, float], ...]
    feature_version: Literal["synthetic-causal-v1", "five-minute-price-alpha-v1"] = (
        "synthetic-causal-v1"
    )
    dependencies: tuple[str, ...]
    dependency_available_at: tuple[datetime | None, ...]
    sequence: int = Field(ge=1)
    session_close: datetime


class ModelArtifact(BrokerModel):
    mode: Literal["COLLECT", "PREDICT"] = "COLLECT"
    description: str = "nonpredictive baseline; no fitted model"
    feature_version: Literal["synthetic-causal-v1", "five-minute-price-alpha-v1"] = (
        "synthetic-causal-v1"
    )
    learning_receipt_id: str = "M1_NO_LEARNING"
    estimator: FittedEstimator | None = None
    target_contract: (
        Literal["owned-executed-net-return-v1", "owned-executed-paper-gross-return-v1"] | None
    ) = None
    policy_id: str | None = None
    parent_model_id: str | None = None
    input_ids: tuple[str, ...] = ()
    fixture_authority: str | None = None
    constant_bps: Annotated[float, Field(allow_inf_nan=False)] | None = None

    @model_serializer(mode="wrap")
    def preserve_collect_payload(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        additions = {
            "estimator",
            "target_contract",
            "policy_id",
            "parent_model_id",
            "input_ids",
            "fixture_authority",
            "constant_bps",
        }
        for field in additions - self.model_fields_set:
            payload.pop(field, None)
        return payload

    @model_validator(mode="after")
    def predictive_contract(self) -> Self:
        if self.mode == "COLLECT":
            if self.estimator is not None or self.constant_bps is not None:
                raise ValueError("COLLECT remains nonpredictive")
        elif self.estimator is None and not (
            self.fixture_authority and self.constant_bps is not None
        ):
            raise ValueError("PREDICT needs immutable parameters or explicit fixture authority")
        elif self.feature_version != "five-minute-price-alpha-v1" or not self.target_contract:
            raise ValueError(
                "predictive artifact requires exact feature and owned target contracts"
            )
        if self.estimator is not None and (
            self.estimator.feature_names != FEATURE_NAMES
            or self.estimator.family != "ridge"
            or self.estimator.alpha != 1
            or self.estimator.seed != 0
        ):
            raise ValueError("model parameters violate fixed ridge envelope")
        return self

    @classmethod
    def fixture_constant(cls, bps: float) -> Self:
        return cls(
            mode="PREDICT",
            description="explicit synthetic constant incumbent",
            feature_version="five-minute-price-alpha-v1",
            constant_bps=bps,
            target_contract="owned-executed-net-return-v1",
            policy_id="collect-owned-30m-stop100-v1",
            fixture_authority="M4_SYNTHETIC_NEGATIVE_INCUMBENT",
            learning_receipt_id="M4_FIXTURE_INCUMBENT",
        )

    @property
    def id(self) -> str:
        return content_id(self)


class ReconciliationProof(Evidence):
    legacy_optional_fields = frozenset({"cash_complete", "ledger_state_id"})
    ledger_state_id: str | None = None
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


class ObservationCohort(BrokerModel):
    """One bounded completed source boundary with explicit missing-member evidence."""

    source_end: datetime
    session_open: datetime
    session_close: datetime
    observations: tuple[Observation, ...]
    unavailable: tuple[tuple[str, str], ...] = ()


class SelectionEvidence(BrokerModel):
    policy: Literal["SIGNAL_OR_EXPLORE_V1"] = "SIGNAL_OR_EXPLORE_V1"
    kind: Literal["SIGNAL", "EXPLORATION", "UNAVAILABLE"]
    cohort: ObservationCohort
    boundary_id: str
    session_counts: tuple[tuple[str, int], ...]
    prior_counts: tuple[tuple[str, int], ...]
    exclusions: tuple[tuple[str, tuple[str, ...]], ...]


class Decision(Evidence):
    legacy_optional_fields = frozenset(
        {
            "source_start",
            "time_evidence_id",
            "clock_process_id",
            "monotonic_anchor",
            "provider_deadline",
            "time_projection",
            "activation_id",
            "model_generation",
            "model_digest",
            "prediction_bps",
            "model_mode",
            "selection",
        }
    )
    time_evidence_id: str | None = None
    clock_process_id: str | None = None
    monotonic_anchor: float | None = None
    provider_deadline: datetime | None = None
    time_projection: TimeProjection | None = None
    activation_id: str | None = None
    model_generation: int | None = None
    model_digest: str | None = None
    prediction_bps: Annotated[float, Field(allow_inf_nan=False)] | None = None
    model_mode: Literal["COLLECT", "PREDICT"] | None = None
    selection: SelectionEvidence | None = None
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
            evidence_lane=config.evidence_lane,
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
    provider: Literal["EXPLICIT_SYNTHETIC", "LOCAL_SIM_REAL_DATA", "alpaca"] = "EXPLICIT_SYNTHETIC"
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


class LearningEpisode(BrokerModel):
    """Guarded immutable join; historical RoundTrip documents are never expanded."""

    outcome: RoundTrip
    observation: Observation
    session: date


class LearningPolicy(BrokerModel):
    parent_model: ModelArtifact = Field(default_factory=ModelArtifact)
    policy_id: str = "collect-owned-30m-stop100-v1"
    dataset: str = "EXPLICIT_SYNTHETIC"
    evidence_lane: Literal["SYNTHETIC_TEST", "LOCAL_SIM_REAL_DATA", "BROKER_PAPER"] = (
        "SYNTHETIC_TEST"
    )
    feature_version: Literal["five-minute-price-alpha-v1"] = "five-minute-price-alpha-v1"
    target_contract: Literal[
        "owned-executed-net-return-v1", "owned-executed-paper-gross-return-v1"
    ] = "owned-executed-net-return-v1"
    exit_contract: Literal["owned-30m-stop100-v1"] = "owned-30m-stop100-v1"
    family: Literal["ridge"] = "ridge"
    alpha: Literal[1] = 1
    seed: Literal[0] = 0
    clip_bps: Literal[100] = 100
    fit_timeout_seconds: Literal[30] = 30
    max_sessions: Literal[60] | None = None
    procedure_version: (
        Literal["daily-all-history-walk-forward-v2", "daily-paper-exploration-walk-forward-v3"]
        | None
    ) = "daily-all-history-walk-forward-v2"
    min_training: Literal[30] = 30
    min_training_sessions: Literal[5] = 5
    min_validation: Literal[10] = 10
    min_validation_sessions: Literal[5] = 5
    embargo_minutes: Literal[30] = 30
    min_improvement: Annotated[float, Field(ge=0.05, le=0.05)] = 0.05
    fits_per_session: Literal[1] = 1

    @model_serializer(mode="wrap")
    def legacy_policy(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        if self.procedure_version is None:
            payload.pop("procedure_version", None)
        return payload

    @model_validator(mode="before")
    @classmethod
    def decode_legacy_policy(cls, value: Any) -> Any:
        if (
            isinstance(value, dict)
            and value.get("max_sessions") == 60
            and "procedure_version" not in value
        ):
            return {**value, "procedure_version": None}
        return value

    @classmethod
    def legacy_fixture(cls, **fields: Any) -> Self:
        return cls(**fields, max_sessions=60, procedure_version=None)

    @model_validator(mode="after")
    def versioned_envelope(self) -> Self:
        if (self.target_contract == "owned-executed-paper-gross-return-v1") != (
            self.procedure_version == "daily-paper-exploration-walk-forward-v3"
        ):
            raise ValueError("target and procedure must match exactly")
        if (self.procedure_version is None) != (self.max_sessions == 60):
            raise ValueError(
                "legacy fixed split uses 60 sessions; daily procedure uses all history"
            )
        return self

    @property
    def id(self) -> str:
        return content_id(self)


class SessionMetrics(BrokerModel):
    session: date
    outcome_ids: tuple[str, ...]
    actual_bps: tuple[float, ...]
    candidate_bps: tuple[float, ...]
    incumbent_bps: tuple[float, ...]
    constant_bps: tuple[float, ...]
    candidate_mse: float
    incumbent_mse: float
    constant_mse: float


class LearningCalendar(BrokerModel):
    """Explicit synthetic contiguous exchange calendar; runtime acquisition belongs to M6."""

    sequence: XNYSSessionSequence
    authority: Literal[
        "EXPLICIT_SYNTHETIC_XNYS_V1", "LOCAL_ARCHIVED_XNYS_V1", "ALPACA_PAPER_XNYS_V1"
    ] = "EXPLICIT_SYNTHETIC_XNYS_V1"

    @property
    def id(self) -> str:
        return content_id(self)


class LearningFold(BrokerModel):
    session: date
    cutoff: datetime
    training: tuple[LearningEpisode, ...]
    validation: tuple[LearningEpisode, ...]
    historical_model_ids: tuple[str, ...]
    historical_models: tuple[ModelArtifact, ...]
    historical_available_at: tuple[datetime | None, ...]
    historical_receipt_ids: tuple[str | None, ...]
    historical_acknowledgement_ids: tuple[str | None, ...]
    historical_acknowledgement_available_at: tuple[datetime | None, ...]
    historical_receipt_payloads: tuple[str | None, ...]
    historical_acknowledgement_payloads: tuple[str | None, ...]


class FoldResult(BrokerModel):
    session: date
    cutoff: datetime
    training_ids: tuple[str, ...]
    validation_ids: tuple[str, ...]
    scoring_available_at: tuple[datetime, ...]
    raw_weights: tuple[float, ...]
    artifact: ModelArtifact
    historical_model_ids: tuple[str, ...]
    historical_available_at: tuple[datetime | None, ...]
    historical_receipt_ids: tuple[str | None, ...]
    historical_acknowledgement_ids: tuple[str | None, ...]
    historical_acknowledgement_available_at: tuple[datetime | None, ...]
    scores: SessionMetrics


class ProcedureResult(BrokerModel):
    candidate: ModelArtifact | None = None
    folds: tuple[FoldResult, ...]
    metrics: tuple[tuple[str, float], ...]
    session_metrics: tuple[SessionMetrics, ...]
    gates_passed: bool
    final_raw_weights: tuple[float, ...] = ()


class LearningReceipt(Evidence):
    legacy_optional_fields = frozenset(
        {
            "execution_clock_domain",
            "started_at",
            "fit_started_at",
            "completed_at",
            "available_at",
            "completion_clock_domain",
            "calendar_id",
            "completed_session",
            "fold_results",
            "final_input_ids",
            "final_raw_weights",
            "final_validation_absent_reason",
        }
    )
    execution_clock_domain: str | None = None
    started_at: datetime | None = None
    fit_started_at: datetime | None = None
    completed_at: datetime | None = None
    available_at: datetime | None = None
    completion_clock_domain: str | None = None
    calendar_id: str | None = None
    completed_session: date | None = None
    fold_results: tuple[FoldResult, ...] = ()
    final_input_ids: tuple[str, ...] = ()
    final_raw_weights: tuple[float, ...] = ()
    final_validation_absent_reason: str | None = None
    run_id: str
    cutoff: datetime
    parent_model_id: str
    resulting_model_id: str
    candidate_model_id: str | None = None
    policy: LearningPolicy
    considered_ids: tuple[str, ...]
    exclusions: tuple[tuple[str, str], ...]
    training_ids: tuple[str, ...] = ()
    validation_ids: tuple[str, ...] = ()
    input_export_id: str | None = None
    split_digest: str
    trial_charge_id: str | None = None
    status: Literal["NO_CHANGE", "FIT_FAILED", "CANDIDATE_REJECTED", "AWAITING_ACTIVATION"]
    reason: str
    validation_absent_reason: str | None = None
    metrics: tuple[tuple[str, float], ...] = ()
    session_metrics: tuple[SessionMetrics, ...] = ()
    activation_at: None = None

    @model_validator(mode="after")
    def completion_evidence(self) -> Self:
        if self.available_at is not None:
            if (
                not self.execution_clock_domain
                or self.started_at is None
                or self.completed_at != self.available_at
                or self.recorded_at_utc != self.completed_at
                or self.started_at > self.available_at
                or (
                    self.fit_started_at is not None
                    and not self.started_at <= self.fit_started_at <= self.available_at
                )
            ):
                raise ValueError(
                    "learning availability requires ordered declared execution evidence"
                )
        return self


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
