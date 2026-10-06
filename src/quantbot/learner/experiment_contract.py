"""Frozen PAPER research identity. Validation never grants execution authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol, Self, TypeVar

from pydantic import Field, model_validator

from quantbot.learner.estimators import FittedEstimator
from quantbot.learner.five_minute import FEATURE_NAMES, FiveMinuteFeatureRow
from quantbot.learner.models import Finite, LearnerModel, Text
from quantbot.research.manifest import content_id

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ResultT = TypeVar("ResultT")


class CanonicalGuard(Protocol):
    def permits(self, start: datetime, end: datetime) -> bool: ...


class TrialCharger(Protocol):
    def used_calls(self, experiment_id: str) -> int: ...

    def charge(self, trial_id: str, manifest_hash: str) -> None: ...


class InstrumentIdentity(LearnerModel):
    symbol: Text
    asset_id: Text
    asset_class: Literal["EQUITY", "ETF"]
    metadata_source: str = Field(pattern=r"^https://", min_length=9)
    metadata_hash: Digest
    coverage_hash: Digest
    verified_at: datetime
    tradable: Literal[True] = True
    coverage_qualified: Literal[True] = True
    qualification_basis: Literal["IDENTITY_AND_COVERAGE"] = "IDENTITY_AND_COVERAGE"
    performance_screened: Literal[False] = False


class ExperimentRisk(LearnerModel):
    maximum_new_positions: int = Field(default=1, ge=1, le=1, strict=True)
    maximum_position_bps: int = Field(default=500, gt=0, le=500, strict=True)
    maximum_entry_risk_bps: int = Field(default=25, gt=0, le=25, strict=True)
    stop_bps: int = Field(default=100, gt=0, le=100, strict=True)
    horizon_minutes: int = Field(default=30, ge=30, le=30, strict=True)
    inherited_positions: Literal["MONITOR_ONLY_EXISTING_EXIT_OWNER"] = (
        "MONITOR_ONLY_EXISTING_EXIT_OWNER"
    )


class ExperimentManifest(LearnerModel):
    schema_version: Literal["ADAPTIVE_PAPER_EXPERIMENT_V1"] = "ADAPTIVE_PAPER_EXPERIMENT_V1"
    experiment_id: Text
    mode: Literal["PAPER"] = "PAPER"
    registered_at: datetime
    prospective_start_at: datetime
    training_start: datetime
    training_end: datetime
    validation_start: datetime
    validation_end: datetime
    roster: tuple[InstrumentIdentity, ...] = Field(min_length=6, max_length=10)
    target: Literal["raw_return"] = "raw_return"
    feature_names: tuple[str, ...] = FEATURE_NAMES
    families: tuple[Literal["constant"], Literal["ridge"]] = ("constant", "ridge")
    ridge_alpha: float = Field(default=1.0, ge=1, le=1, strict=True)
    seed: int = Field(default=0, ge=0, le=0, strict=True)
    fit_budget: int = Field(default=6, ge=6, le=6, strict=True)
    maximum_successors: int = Field(default=4, ge=4, le=4, strict=True)
    successor_interval_sessions: int = Field(default=5, ge=5, le=5, strict=True)
    minimum_training_sessions: int = Field(default=20, ge=20, le=20, strict=True)
    minimum_validation_sessions: int = Field(default=5, ge=5, le=5, strict=True)
    performance_sessions: int = Field(default=20, ge=20, le=20, strict=True)
    maximum_evaluation_sessions: int = Field(default=30, ge=30, le=30, strict=True)
    zero_eligibility_review_sessions: int = Field(default=5, ge=5, le=5, strict=True)
    round_trip_cost_bps: int = Field(default=10, ge=10, strict=True)
    risk: ExperimentRisk = Field(default_factory=ExperimentRisk)
    code_hash: Digest
    policy_hash: Digest
    synthetic: bool = False

    @model_validator(mode="after")
    def frozen_contract(self) -> Self:
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("frozen ten-price-feature contract required")
        if not (
            self.training_start <= self.training_end
            and self.training_end + timedelta(minutes=35) <= self.validation_start
            and self.validation_start <= self.validation_end < self.registered_at
            and self.registered_at <= self.prospective_start_at
        ):
            raise ValueError("ordered, purged training/validation and prospective start required")
        if len({row.symbol for row in self.roster}) != len(self.roster) or len(
            {row.asset_id for row in self.roster}
        ) != len(self.roster):
            raise ValueError("distinct official instrument identities required")
        if any(row.verified_at > self.registered_at for row in self.roster):
            raise ValueError("identity and coverage must be frozen before registration")
        return self


def validate_manifest(manifest: ExperimentManifest, *, guard: CanonicalGuard) -> str:
    verified = ExperimentManifest.model_validate(manifest.model_dump())
    for start, end in (
        (verified.training_start, verified.training_end),
        (verified.validation_start, verified.validation_end),
    ):
        if not guard.permits(start - timedelta(minutes=35), end):
            raise ValueError("PROTECTED_OR_UNREGISTERED_HISTORY")
    return content_id(verified)


def guarded_read(
    manifest: ExperimentManifest, *, guard: CanonicalGuard, loader: Callable[[], ResultT]
) -> ResultT:
    """Canonical authority is consulted before the caller opens any archived source."""
    validate_manifest(manifest, guard=guard)
    return loader()


class DecisionRecord(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    model_id: Digest
    features: FiveMinuteFeatureRow
    score: Finite
    action: Literal["BUY", "HOLD", "EXIT", "WAIT"]
    reason: Text
    risk_vetoes: tuple[str, ...] = ()
    activation_id: str | None = None
    paper_eligible: bool = False
    synthetic: bool = False

    @property
    def decision_id(self) -> str:
        return content_id(self)

    @model_validator(mode="after")
    def point_in_time(self) -> Self:
        row = self.features
        if not (
            row.receipt_at <= row.decision_at <= self.recorded_at < row.target_at
            and self.recorded_at <= row.feature_at + timedelta(seconds=120)
        ):
            raise ValueError("decision must follow receipt and precede target within freshness")
        if self.paper_eligible and (
            self.activation_id is None or self.synthetic or self.risk_vetoes or self.action != "BUY"
        ):
            raise ValueError("eligible decision requires exact activation and all PAPER gates")
        return self


class WorkerReceipt(LearnerModel):
    """Bounded shadow worker phase; a start is not a successful fit or activation."""

    experiment_id: Text
    recorded_at: datetime
    status: Text
    reason: Text
    code_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    scope: Literal["SHADOW"]
    synthetic: bool


class CandidateArtifact(LearnerModel):
    """A fitted pooled artifact; this envelope carries no execution authority."""

    experiment_id: Text
    recorded_at: datetime
    manifest_hash: Digest
    trial_id: Text
    training_source_hash: Digest
    validation_source_hash: Digest
    artifact: FittedEstimator
    generation: int = Field(default=0, ge=0, le=4, strict=True)
    parent_model_id: Digest | None = None
    synthetic: bool = False

    @property
    def model_id(self) -> str:
        return content_id(self)

    @model_validator(mode="after")
    def frozen_estimator(self) -> Self:
        if (
            self.artifact.family not in {"mean", "ridge"}
            or self.artifact.feature_names != FEATURE_NAMES
            or self.artifact.alpha != 1.0
            or self.artifact.seed != 0
            or (self.generation == 0) != (self.parent_model_id is None)
        ):
            raise ValueError("FROZEN_POOLED_ESTIMATOR_REQUIRED")
        return self
