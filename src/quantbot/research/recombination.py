"""Bounded, evidence-aware selection of componentized hypothesis descendants (#189).

This is intentionally not a genetic algorithm.  It evaluates a finite set of explicit graph
recipes, reads immutable component/interaction evidence, and returns at most one candidate.  The
whole set examined is recorded as one durable search and charged to the selected candidate.

Evidence affects what uncertainty is visible and breaks ties between equally informative
questions; it never becomes an inherited verdict.  Power, behavioral novelty, and clean data
capacity remain mechanical gates, and the selected candidate still enters the ordinary
Candidate -> HypothesisDraft -> critic -> registry -> experiment path.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.research.artifact_testing import (
    ArtifactTestDraft,
    IdentificationArm,
    IdentificationMethod,
)
from quantbot.research.director import expected_information_gain
from quantbot.research.discovery import Candidate, DiscoveryMode, admissible
from quantbot.research.hypothesis_graph import (
    GraphArtifactKind,
    HypothesisGraph,
    HypothesisLineage,
)
from quantbot.research.manifest import content_id
from quantbot.research.memory import ArtifactEvidence, ArtifactEvidenceSummary, ResearchMemory
from quantbot.research.power import EffectSpecification, PowerAssessment
from quantbot.research.registry import HypothesisRegistry, Registration
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import search_runs

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RecombinationRefused(ValueError):
    """Raised when a proposed search would have unverifiable provenance or accounting."""


class RecombinationPolicy(FrozenModel):
    """Bounded selector policy.  No model confidence or narrative score is accepted."""

    version: Text
    actor: Text = "evidence-aware-recombiner"
    maximum_options: int = Field(default=20, ge=1)
    minimum_information_gain: Decimal = Field(default=Decimal("0.0001"), ge=0)

    @property
    def policy_id(self) -> str:
        return content_id(self)


class RecombinationOption(FrozenModel):
    """One complete graph recipe and a power assessment computed for its planned claim."""

    candidate: Candidate
    planned_hypothesis_id: Text
    planned_hypothesis_version: int = Field(default=1, ge=1)
    power: PowerAssessment | None

    @model_validator(mode="after")
    def validate_power_reference(self) -> Self:
        if self.power is not None and (
            self.power.hypothesis_id,
            self.power.version,
        ) != (self.planned_hypothesis_id, self.planned_hypothesis_version):
            raise ValueError("option power must name its exact planned hypothesis version")
        return self


class ArtifactEvidenceDigest(FrozenModel):
    """Lossless evidence references for one exact candidate artifact."""

    artifact_kind: GraphArtifactKind
    artifact_content_id: Digest
    supporting: tuple[Digest, ...] = ()
    refuting: tuple[Digest, ...] = ()
    underpowered: tuple[Digest, ...] = ()
    contextual: tuple[Digest, ...] = ()
    unassessed: tuple[Digest, ...] = ()
    contradictory: bool = False

    @property
    def evidence_count(self) -> int:
        return sum(
            len(items)
            for items in (
                self.supporting,
                self.refuting,
                self.underpowered,
                self.contextual,
                self.unassessed,
            )
        )

    @property
    def unresolved_count(self) -> int:
        return (
            len(self.underpowered)
            + len(self.contextual)
            + len(self.unassessed)
            + int(self.contradictory)
        )


class ParentEvidenceDigest(FrozenModel):
    """Claim-level outcomes remain attached to the exact parent, never its children."""

    hypothesis_id: Text
    version: int = Field(ge=1)
    graph_id: Digest
    record_ids: tuple[Text, ...] = ()
    verdicts: tuple[Text, ...] = ()


class ConsumptionDigest(FrozenModel):
    dataset: Text
    status: Text
    trials: int = Field(ge=0)
    consumers: tuple[Text, ...]


class RecombinationRationale(FrozenModel):
    """Machine-readable account of why a selected descendant is worth asking about."""

    candidate_id: Text
    graph_id: Digest
    parents: tuple[ParentEvidenceDigest, ...]
    artifacts: tuple[ArtifactEvidenceDigest, ...]
    consumption: tuple[ConsumptionDigest, ...]
    novelty_overlap: float = Field(ge=0, le=1)
    nearest_registered: tuple[Text, ...]
    expected_information_gain: Decimal = Field(ge=0)
    search_id: Text
    search_cardinality: int = Field(ge=1)
    trials_required: int = Field(ge=1)
    unresolved_evidence: int = Field(ge=0)
    why_more_information: Text
    falsified_if: Text


class DeferredOption(FrozenModel):
    candidate_id: Text
    reason: Text


class IdentificationQuestion(FrozenModel):
    """The scientific details the engine must not infer from a component's prose."""

    artifact_kind: GraphArtifactKind
    artifact_content_id: Text
    estimand: Text
    benchmark: Text
    regime: Text
    null_hypothesis: Text
    falsified_if: Text
    effect: EffectSpecification


class IdentificationRecommendation(FrozenModel):
    """A generated identifying design, or an explicit decision not to run one."""

    question: IdentificationQuestion
    evidence: ArtifactEvidenceDigest
    test: ArtifactTestDraft | None = None
    reason: Text


class RecombinationResult(FrozenModel):
    """At most one selected candidate; zero is a valid and important result."""

    search_id: Text
    evaluated: int = Field(ge=1)
    selected: Candidate | None = None
    rationale: RecombinationRationale | None = None
    deferred: tuple[DeferredOption, ...] = ()

    @model_validator(mode="after")
    def selection_and_rationale_travel_together(self) -> Self:
        if (self.selected is None) != (self.rationale is None):
            raise ValueError("a selected recombination and its rationale must travel together")
        return self


class _Evaluated(FrozenModel):
    option: RecombinationOption
    parents: tuple[ParentEvidenceDigest, ...]
    artifacts: tuple[ArtifactEvidenceDigest, ...]
    consumption: tuple[ConsumptionDigest, ...]
    novelty_overlap: float
    nearest: tuple[str, ...]
    gain: Decimal
    unresolved: int
    reason: str | None = None


class EvidenceAwareRecombiner:
    """Select one bounded descendant using durable evidence and existing research gates."""

    def __init__(self, session: Session, policy: RecombinationPolicy) -> None:
        self._session = session
        self._policy = policy
        self._registry = HypothesisRegistry(session)
        self._memory = ResearchMemory(session)

    def select(
        self,
        options: tuple[RecombinationOption, ...],
        *,
        search_id: str,
        evaluation_start: date,
        evaluation_end: date,
        trials_remaining: Decimal,
        now: datetime,
        forward_evidence: bool = False,
    ) -> RecombinationResult:
        """Evaluate every supplied recipe, record the search, and select at most one."""
        if not options:
            raise RecombinationRefused("a recombination search must examine at least one option")
        if len(options) > self._policy.maximum_options:
            raise RecombinationRefused(
                f"search proposed {len(options)} options against a hard bound of "
                f"{self._policy.maximum_options}"
            )
        if evaluation_end < evaluation_start:
            raise RecombinationRefused("evaluation window end cannot precede its start")
        if trials_remaining < 0:
            raise RecombinationRefused("remaining trial budget cannot be negative")
        if self._session.execute(
            select(search_runs.c.search_id).where(search_runs.c.search_id == search_id)
        ).one_or_none():
            raise RecombinationRefused(f"search run {search_id} is already recorded")

        projected_trials = self._registry.cumulative_trials() + len(options)
        for option in options:
            if (
                option.power is not None
                and option.power.cumulative_trials != projected_trials
            ):
                raise RecombinationRefused(
                    f"{option.candidate.candidate_id} power used cumulative burden "
                    f"{option.power.cumulative_trials}, expected {projected_trials} after "
                    "charging every recombination option"
                )

        evaluated = tuple(
            self._evaluate(
                option,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                trials_remaining=trials_remaining,
                search_size=len(options),
                forward_evidence=forward_evidence,
            )
            for option in options
        )
        viable = [item for item in evaluated if item.reason is None]
        chosen = (
            min(
                viable,
                key=lambda item: (
                    -item.gain,
                    -item.unresolved,
                    item.option.candidate.candidate_id,
                ),
            )
            if viable
            else None
        )
        selected = (
            None
            if chosen is None
            else self._selected_candidate(chosen.option, len(options), now)
        )
        rationale = (
            None
            if chosen is None or selected is None
            else RecombinationRationale(
                candidate_id=selected.candidate_id,
                graph_id=selected.graph.graph_id if selected.graph is not None else "",
                parents=chosen.parents,
                artifacts=chosen.artifacts,
                consumption=chosen.consumption,
                novelty_overlap=chosen.novelty_overlap,
                nearest_registered=chosen.nearest,
                expected_information_gain=chosen.gain,
                search_id=search_id,
                search_cardinality=len(options),
                trials_required=len(options),
                unresolved_evidence=chosen.unresolved,
                why_more_information=(
                    f"powered novel graph with {chosen.unresolved} unresolved artifact "
                    "evidence records or contradictions"
                ),
                falsified_if=selected.simplest_refutation,
            )
        )
        self._record_search(
            search_id,
            evaluated=evaluated,
            selected=selected,
            evaluation_start=evaluation_start,
            evaluation_end=evaluation_end,
            now=now,
        )
        return RecombinationResult(
            search_id=search_id,
            evaluated=len(options),
            selected=selected,
            rationale=rationale,
            deferred=tuple(
                DeferredOption(candidate_id=item.option.candidate.candidate_id, reason=item.reason)
                for item in evaluated
                if item.reason is not None
            ),
        )

    def propose_identification(
        self, registration: Registration, question: IdentificationQuestion
    ) -> IdentificationRecommendation:
        """Build the cheapest matched design when exact artifact evidence is unresolved.

        Effect size, estimand, benchmark and regime are required inputs because deriving them
        from prose would manufacture the claim.  Only the arm layout is mechanical.
        """
        current = self._registry.get(
            registration.draft.hypothesis_id, registration.draft.version
        )
        graph = registration.draft.graph
        if (
            current is None
            or current.content_hash != registration.content_hash
            or graph is None
            or question.artifact_content_id
            not in graph.artifact_ids(question.artifact_kind)
        ):
            raise RecombinationRefused(
                "identification requires an artifact in the exact persisted hypothesis graph"
            )
        summary = self._memory.summarize_artifact(
            question.artifact_content_id, kind=question.artifact_kind
        )
        digest = self._digest(question.artifact_kind, question.artifact_content_id, summary)
        all_entries = (
            *summary.supporting,
            *summary.refuting,
            *summary.underpowered,
            *summary.contextual,
            *summary.unassessed,
        )
        if not all_entries:
            return IdentificationRecommendation(
                question=question,
                evidence=digest,
                reason="no prior artifact evidence grounds an identification experiment",
            )
        applicable_support = [
            item for item in summary.supporting if self._applies(item, registration, question)
        ]
        applicable_refutation = [
            item for item in summary.refuting if self._applies(item, registration, question)
        ]
        if bool(applicable_support) != bool(applicable_refutation):
            return IdentificationRecommendation(
                question=question,
                evidence=digest,
                reason=(
                    "matching direct evidence already resolves this exact applicability; "
                    "retesting would not add a new question"
                ),
            )

        components = set(graph.component_ids)
        arms: tuple[IdentificationArm, ...]
        if question.artifact_kind is GraphArtifactKind.COMPONENT:
            target = question.artifact_content_id
            arms = (
                IdentificationArm(
                    name="without-target",
                    included_component_ids=tuple(sorted(components - {target})),
                ),
                IdentificationArm(
                    name="with-target", included_component_ids=tuple(sorted(components))
                ),
            )
            method = IdentificationMethod.ABLATION
        else:
            relationship = next(
                item
                for item in graph.relationships
                if item.relationship_id == question.artifact_content_id
            )
            source = relationship.source_component_id
            target = relationship.target_component_id
            background = components - {source, target}
            arms = (
                IdentificationArm(
                    name="neither",
                    included_component_ids=tuple(sorted(background)),
                ),
                IdentificationArm(
                    name="source-only",
                    included_component_ids=tuple(sorted(background | {source})),
                ),
                IdentificationArm(
                    name="target-only",
                    included_component_ids=tuple(sorted(background | {target})),
                ),
                IdentificationArm(
                    name="source-and-target",
                    included_component_ids=tuple(sorted(background | {source, target})),
                ),
            )
            method = IdentificationMethod.FACTORIAL
        return IdentificationRecommendation(
            question=question,
            evidence=digest,
            test=ArtifactTestDraft(
                artifact_kind=question.artifact_kind,
                artifact_content_id=question.artifact_content_id,
                identification_method=method,
                arms=arms,
                estimand=question.estimand,
                benchmark=question.benchmark,
                regime=question.regime,
                null_hypothesis=question.null_hypothesis,
                falsified_if=question.falsified_if,
                effect=question.effect,
            ),
            reason=(
                "prior evidence is contextual, underpowered, unassessed, or contradictory; "
                "a matched identifying contrast can resolve the exact claim"
            ),
        )

    @staticmethod
    def _applies(
        entry: ArtifactEvidence,
        registration: Registration,
        question: IdentificationQuestion,
    ) -> bool:
        return (
            isinstance(entry, ArtifactEvidence)
            and entry.estimand == question.estimand
            and entry.benchmark == question.benchmark
            and entry.regime == question.regime
            and entry.universe == registration.draft.universe
        )

    def _evaluate(
        self,
        option: RecombinationOption,
        *,
        evaluation_start: date,
        evaluation_end: date,
        trials_remaining: Decimal,
        search_size: int,
        forward_evidence: bool,
    ) -> _Evaluated:
        candidate = option.candidate
        graph = candidate.graph
        lineage = candidate.lineage
        if candidate.mode is not DiscoveryMode.MUTATION or graph is None or lineage is None:
            raise RecombinationRefused(
                f"{candidate.candidate_id} is not a componentized MUTATION candidate"
            )
        if len(lineage.parents) < 2:
            raise RecombinationRefused(
                f"{candidate.candidate_id} must name at least two exact parent versions"
            )
        if not candidate.why_different:
            raise RecombinationRefused(
                f"{candidate.candidate_id} must state what new question recombination creates"
            )
        parents = self._parent_evidence(lineage)
        artifacts = self._artifact_evidence(graph)
        consumption_rows = tuple(
            self._registry.window_consumption(dataset, evaluation_start, evaluation_end)
            for dataset in candidate.required_datasets
        )
        consumption = tuple(
            ConsumptionDigest(
                dataset=row.dataset,
                status=row.status,
                trials=row.trials,
                consumers=row.consumers,
            )
            for row in consumption_rows
        )
        allowed, why = admissible(
            candidate, consumption=consumption_rows, forward_evidence=forward_evidence
        )
        overlap = self._registry.graph_overlap(graph)
        trials_required = Decimal(search_size)
        gain = expected_information_gain(
            option.power,
            novelty_overlap=overlap.highest_overlap,
            trials_remaining=trials_remaining,
            trials_required=trials_required,
        )
        unresolved = sum(item.unresolved_count for item in artifacts)
        reason: str | None = None
        if not allowed:
            reason = why
        elif option.power is None or not option.power.cleared:
            reason = "claim is not power-cleared"
        elif not any(item.evidence_count for item in artifacts):
            reason = "no component or interaction evidence grounds this recombination"
        elif gain < self._policy.minimum_information_gain:
            reason = "expected information gain is not positive after novelty and budget gates"
        return _Evaluated(
            option=option,
            parents=parents,
            artifacts=artifacts,
            consumption=consumption,
            novelty_overlap=overlap.highest_overlap,
            nearest=overlap.nearest,
            gain=gain,
            unresolved=unresolved,
            reason=reason,
        )

    def _parent_evidence(self, lineage: HypothesisLineage) -> tuple[ParentEvidenceDigest, ...]:
        registrations: dict[tuple[str, int], Registration] = {}
        for parent in lineage.parents:
            registration = self._registry.get(parent.hypothesis_id, parent.version)
            if (
                registration is None
                or registration.draft.graph is None
                or registration.draft.graph.graph_id != parent.graph_id
            ):
                raise RecombinationRefused(
                    f"lineage parent {parent.hypothesis_id} v{parent.version}@{parent.graph_id} "
                    "does not match a frozen registration"
                )
            registrations[(parent.hypothesis_id, parent.version)] = registration
        for change in lineage.changes:
            if change.parent is None or change.parent_content_id is None:
                continue
            registration = registrations[(change.parent.hypothesis_id, change.parent.version)]
            parent_graph = registration.draft.graph
            if (
                parent_graph is None
                or change.parent_content_id not in parent_graph.artifact_ids(change.artifact_kind)
            ):
                raise RecombinationRefused(
                    f"lineage source {change.parent_content_id} is absent from its exact parent"
                )
        return tuple(
            self._parent_digest(parent.hypothesis_id, parent.version, parent.graph_id)
            for parent in lineage.parents
        )

    def _parent_digest(
        self, hypothesis_id: str, version: int, graph_id: str
    ) -> ParentEvidenceDigest:
        records = tuple(
            record
            for record in self._memory.findings_for(hypothesis_id)
            if record.hypothesis_version == version
        )
        return ParentEvidenceDigest(
            hypothesis_id=hypothesis_id,
            version=version,
            graph_id=graph_id,
            record_ids=tuple(record.record_id for record in records),
            verdicts=tuple(
                record.verdict.value for record in records if record.verdict is not None
            ),
        )

    def _artifact_evidence(
        self, graph: HypothesisGraph
    ) -> tuple[ArtifactEvidenceDigest, ...]:
        return tuple(
            self._digest(kind, artifact_id, self._memory.summarize_artifact(artifact_id, kind=kind))
            for kind in GraphArtifactKind
            for artifact_id in sorted(graph.artifact_ids(kind))
        )

    @staticmethod
    def _digest(
        kind: GraphArtifactKind, artifact_id: str, summary: ArtifactEvidenceSummary
    ) -> ArtifactEvidenceDigest:
        return ArtifactEvidenceDigest(
            artifact_kind=kind,
            artifact_content_id=artifact_id,
            supporting=tuple(item.evidence_id for item in summary.supporting),
            refuting=tuple(item.evidence_id for item in summary.refuting),
            underpowered=tuple(item.evidence_id for item in summary.underpowered),
            contextual=tuple(item.evidence_id for item in summary.contextual),
            unassessed=tuple(item.evidence_id for item in summary.unassessed),
            contradictory=summary.contradictory,
        )

    def _selected_candidate(
        self, option: RecombinationOption, search_size: int, now: datetime
    ) -> Candidate:
        candidate = option.candidate
        lineage = candidate.lineage
        if lineage is None:
            raise RecombinationRefused("selected candidate lost its lineage")
        payload = candidate.model_dump(mode="python")
        payload.update(
            {
                "search_cardinality": search_size,
                "prior_work": tuple(
                    f"{parent.hypothesis_id} v{parent.version}@{parent.graph_id}"
                    for parent in lineage.parents
                ),
                "proposed_by": f"{self._policy.actor}@{self._policy.version}",
                "proposed_at": now,
            }
        )
        return Candidate.model_validate(payload)

    def _record_search(
        self,
        search_id: str,
        *,
        evaluated: tuple[_Evaluated, ...],
        selected: Candidate | None,
        evaluation_start: date,
        evaluation_end: date,
        now: datetime,
    ) -> None:
        detail = {
            "algorithm": "bounded-evidence-ranked-recombination",
            "policy_id": self._policy.policy_id,
            "selected": None if selected is None else selected.candidate_id,
            "options": [
                {
                    "candidate_id": item.option.candidate.candidate_id,
                    "graph_id": item.option.candidate.graph.graph_id,
                    "gain": str(item.gain),
                    "reason": item.reason,
                    "evidence_ids": [
                        evidence_id
                        for artifact in item.artifacts
                        for evidence_id in (
                            *artifact.supporting,
                            *artifact.refuting,
                            *artifact.underpowered,
                            *artifact.contextual,
                            *artifact.unassessed,
                        )
                    ],
                }
                for item in evaluated
                if item.option.candidate.graph is not None
            ],
            "target_window": [evaluation_start.isoformat(), evaluation_end.isoformat()],
        }
        self._session.execute(
            search_runs.insert().values(
                search_id=search_id,
                actor=self._policy.actor,
                actor_version=self._policy.version,
                candidates_evaluated=len(evaluated),
                dataset="research-memory",
                start_date=now.date().isoformat(),
                end_date=now.date().isoformat(),
                data_role="RESEARCH_MEMORY",
                started_at=encode_utc(now),
                finished_at=encode_utc(now),
                configuration_hash=self._policy.policy_id,
                detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
            )
        )


__all__ = [
    "ArtifactEvidenceDigest",
    "ConsumptionDigest",
    "DeferredOption",
    "EvidenceAwareRecombiner",
    "IdentificationQuestion",
    "IdentificationRecommendation",
    "ParentEvidenceDigest",
    "RecombinationOption",
    "RecombinationPolicy",
    "RecombinationRationale",
    "RecombinationRefused",
    "RecombinationResult",
]
