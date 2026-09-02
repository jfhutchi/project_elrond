"""Preregistered identifying tests for hypothesis components and interactions (#188).

Component lineage says where behaviour came from.  It does not say whether that behaviour
worked.  A direct evidence claim needs a separate, frozen design that varies the named artifact,
an effect specification that can be power checked, and numeric output that the runner grades.

These models deliberately contain no free-form verdict input.  A test registration fixes what
will be measured; :mod:`quantbot.research.runner` alone turns measured statistics into an
artifact outcome.  Whole-hypothesis outcomes therefore cannot leak into component memory.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from quantbot.research.hypothesis_graph import GraphArtifactKind
from quantbot.research.manifest import content_id
from quantbot.research.power import EffectSpecification, PowerAssessment

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IdentificationMethod(StrEnum):
    """Design families that can identify an artifact-specific contrast."""

    STANDALONE = "STANDALONE"
    ABLATION = "ABLATION"
    FACTORIAL = "FACTORIAL"


class IdentificationArm(FrozenModel):
    """One precommitted arm, expressed by exact component versions present in it."""

    name: Text
    included_component_ids: tuple[Digest, ...]

    @field_validator("included_component_ids")
    @classmethod
    def canonicalize_components(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ordered = tuple(sorted(value))
        if len(ordered) != len(set(ordered)):
            raise ValueError("an identification arm cannot include a component twice")
        return ordered


class ArtifactTestDraft(FrozenModel):
    """An artifact-level claim and the design intended to identify it."""

    schema_version: int = Field(default=1, ge=1, le=1)
    artifact_kind: GraphArtifactKind
    artifact_content_id: Digest
    identification_method: IdentificationMethod
    arms: tuple[IdentificationArm, ...] = Field(min_length=2)
    estimand: Text
    benchmark: Text
    regime: Text
    null_hypothesis: Text
    falsified_if: Text
    effect: EffectSpecification

    @model_validator(mode="after")
    def validate_design_shape(self) -> Self:
        names = [arm.name for arm in self.arms]
        if len(names) != len(set(names)):
            raise ValueError("identification arms must have unique names")
        component_sets = [arm.included_component_ids for arm in self.arms]
        if len(component_sets) != len(set(component_sets)):
            raise ValueError("identification arms must describe unique component sets")
        if self.artifact_kind is GraphArtifactKind.RELATIONSHIP:
            if self.identification_method is not IdentificationMethod.FACTORIAL:
                raise ValueError("an interaction requires a factorial identifying design")
            if len(self.arms) < 4:
                raise ValueError("an interaction requires at least the four cells of a 2x2 design")
        return self

    @property
    def design_id(self) -> str:
        """Stable identity of the claim and design, independent of registration time."""
        return content_id(self)


class ArtifactTestRegistration(FrozenModel):
    """A powered artifact test frozen against one exact hypothesis graph."""

    schema_version: int = Field(default=1, ge=1, le=1)
    hypothesis_id: Text
    hypothesis_version: int = Field(ge=1)
    registration_hash: Digest
    graph_hash: Digest
    draft: ArtifactTestDraft
    registered_at: datetime
    cumulative_trials: int = Field(ge=1)
    power: PowerAssessment

    @model_validator(mode="after")
    def validate_power_reference(self) -> Self:
        if (self.power.hypothesis_id, self.power.version) != (
            self.hypothesis_id,
            self.hypothesis_version,
        ):
            raise ValueError("artifact-test power must reference its hypothesis registration")
        if self.power.cumulative_trials != self.cumulative_trials:
            raise ValueError("artifact-test power must use its charged cumulative trial count")
        return self

    @property
    def test_id(self) -> str:
        return content_id(
            {
                "schema_version": self.schema_version,
                "hypothesis_id": self.hypothesis_id,
                "hypothesis_version": self.hypothesis_version,
                "registration_hash": self.registration_hash,
                "graph_hash": self.graph_hash,
                "design_id": self.draft.design_id,
            }
        )

    @property
    def content_hash(self) -> str:
        return content_id(self)


class ArtifactOutcomeVerdict(StrEnum):
    SURVIVED = "SURVIVED"
    REFUTED = "REFUTED"
    UNDERPOWERED = "UNDERPOWERED"
    UNASSESSED = "UNASSESSED"


class ArtifactOutcome(FrozenModel):
    """Runner-derived result for one preregistered artifact test."""

    test_id: Digest
    test_registration_hash: Digest
    artifact_kind: GraphArtifactKind
    artifact_content_id: Digest
    verdict: ArtifactOutcomeVerdict
    effect: str = ""
    confidence_low: str = ""
    confidence_high: str = ""
    test_statistic: str = ""
    probes_run: tuple[Text, ...] = ()
    probes_failed: tuple[Text, ...] = ()
    detail: Text


class ArtifactTestRefused(ValueError):
    """Raised when a component claim cannot be frozen without inventing evidence."""

    def __init__(self, message: str, *, assessment: PowerAssessment | None = None) -> None:
        self.assessment = assessment
        super().__init__(message)


__all__ = [
    "ArtifactOutcome",
    "ArtifactOutcomeVerdict",
    "ArtifactTestDraft",
    "ArtifactTestRefused",
    "ArtifactTestRegistration",
    "IdentificationArm",
    "IdentificationMethod",
]
