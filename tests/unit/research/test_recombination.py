"""Evidence-aware recombination stays bounded and enters the existing research pipeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from quantbot.research.artifact_testing import IdentificationMethod
from quantbot.research.bridge import draft_from_candidate
from quantbot.research.discovery import Candidate, DiscoveryMode
from quantbot.research.hypothesis_graph import (
    ComponentRelationship,
    GraphArtifactKind,
    HypothesisGraph,
    HypothesisLineage,
    LineageAction,
    LineageChange,
    ParentHypothesis,
)
from quantbot.research.manifest import CodeProvenance, DatasetSnapshot, ExecutionPath
from quantbot.research.memory import (
    ArtifactEvidence,
    ArtifactEvidenceStatus,
    RecordKind,
    ResearchMemory,
    ResearchRecord,
    Verdict,
)
from quantbot.research.power import PowerVerdict, assess
from quantbot.research.recombination import (
    EvidenceAwareRecombiner,
    IdentificationQuestion,
    RecombinationOption,
    RecombinationPolicy,
    RecombinationRefused,
)
from quantbot.research.registry import DataRole, DataWindow, HypothesisRegistry
from quantbot.research.sources import EpistemicStatus, EvidenceBasis
from quantbot.storage import Database
from quantbot.storage.schema import search_runs
from tests.unit.research.test_bridge import effect
from tests.unit.research.test_hypothesis_graph import component, root_lineage
from tests.unit.research.test_registry import NOW, make_draft, register, window

TARGET_DATASET = "fresh-global-futures-daily"


@pytest.fixture
def database(tmp_path: Path):
    database = Database.create(tmp_path / "recombination.db")
    yield database
    database.close()


def register_parents(database: Database):
    signal = component("signal", "time-series-momentum", parameter="252")
    risk = component("risk-control", "volatility-target", parameter="0.10")
    signal_graph = HypothesisGraph(components=(signal,))
    risk_graph = HypothesisGraph(components=(risk,))
    with database.transaction() as session:
        registry = HypothesisRegistry(session, prior_windows=())
        first = register(
            registry,
            make_draft(
                hypothesis_id="H-PARENT-SIGNAL",
                family_id="recombination-parent",
                graph=signal_graph,
                lineage=root_lineage(signal_graph),
                universe=("ES",),
                features=("tsm-252",),
                windows=(
                    window(
                        DataRole.FORWARD_PAPER,
                        "2025-01-01",
                        "2026-08-18",
                        dataset="signal-paper",
                    ),
                ),
            ),
        )
        second = register(
            registry,
            make_draft(
                hypothesis_id="H-PARENT-RISK",
                family_id="recombination-parent",
                graph=risk_graph,
                lineage=root_lineage(risk_graph),
                universe=("NQ",),
                features=("vol-target",),
                windows=(
                    window(
                        DataRole.FORWARD_PAPER,
                        "2025-01-01",
                        "2026-08-18",
                        dataset="risk-paper",
                    ),
                ),
            ),
        )
    return signal, risk, first, second


def add_parent_memory(database: Database, first, second) -> None:
    for registration, verdict, marker in (
        (first, Verdict.REFUTED, "a"),
        (second, Verdict.SURVIVED, "b"),
    ):
        graph = registration.draft.graph
        assert graph is not None
        record = ResearchRecord(
            record_id=f"parent-{marker}",
            kind=RecordKind.FINDING,
            verdict=verdict,
            subject=registration.draft.question,
            statement=f"parent result remained {verdict.value}",
            hypothesis_id=registration.draft.hypothesis_id,
            hypothesis_version=registration.draft.version,
            evidence_hash=marker * 64,
            source="frozen recombination corpus",
            recorded_at=NOW,
        )
        artifact_id = graph.component_ids[0]
        evidence = ArtifactEvidence(
            source_record_id=record.record_id,
            artifact_kind=GraphArtifactKind.COMPONENT,
            artifact_content_id=artifact_id,
            epistemic_status=ArtifactEvidenceStatus.CONTEXTUAL_ASSOCIATION,
            hypothesis_id=registration.draft.hypothesis_id,
            hypothesis_version=registration.draft.version,
            registration_hash=registration.content_hash,
            graph_hash=graph.graph_id,
            experiment_id=f"experiment-{marker}",
            manifest_hash=marker * 64,
            datasets=(
                DatasetSnapshot(
                    dataset=f"parent-{marker}",
                    snapshot="v1",
                    role="FORWARD_PAPER",
                    start=date(2025, 1, 1),
                    end=date(2026, 8, 18),
                    content_hash=f"data-{marker}",
                    observations=400,
                ),
            ),
            estimand="whole-parent annualised Sharpe",
            benchmark="cash",
            universe=registration.draft.universe,
            regime="all registered observations",
            code=CodeProvenance(
                git_commit=marker * 40,
                dirty=False,
                configuration_hash=f"cfg-{marker}",
                execution_path=ExecutionPath.PRODUCTION_ENGINE,
            ),
            configuration_hash=f"configuration-{marker}",
            cumulative_trials=registration.cumulative_trials,
            recorded_at=NOW,
        )
        with database.transaction() as session:
            memory = ResearchMemory(session)
            memory.record(record)
            memory._store_artifact_evidence(evidence)


def combined_lineage(first, second, signal_id: str, risk_id: str) -> HypothesisLineage:
    first_graph = first.draft.graph
    second_graph = second.draft.graph
    assert first_graph is not None and second_graph is not None
    first_parent = ParentHypothesis(
        hypothesis_id=first.draft.hypothesis_id,
        version=first.draft.version,
        graph_id=first_graph.graph_id,
    )
    second_parent = ParentHypothesis(
        hypothesis_id=second.draft.hypothesis_id,
        version=second.draft.version,
        graph_id=second_graph.graph_id,
    )
    return HypothesisLineage(
        parents=(first_parent, second_parent),
        changes=(
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INHERITED,
                child_content_id=signal_id,
                parent=first_parent,
                parent_content_id=signal_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INHERITED,
                child_content_id=risk_id,
                parent=second_parent,
                parent_content_id=risk_id,
            ),
        ),
    )


def candidate(first, second, signal, risk, **overrides: object) -> Candidate:
    graph = HypothesisGraph(components=(signal, risk))
    fields: dict[str, object] = {
        "candidate_id": "C-RECOMBINED",
        "mode": DiscoveryMode.MUTATION,
        "question": "Does trend plus volatility targeting improve futures Sharpe?",
        "family_id": "managed-futures",
        "graph": graph,
        "lineage": combined_lineage(
            first, second, signal.component_id, risk.component_id
        ),
        "overlooked": "the risk control was only observed inside a different parent",
        "why_unpriced": "implementation and leverage constraints limit adoption",
        "expected_direction": "the combination improves risk-adjusted return",
        "horizon_observations": 252,
        "universe": ("ES", "NQ"),
        "features": ("tsm-252", "vol-target"),
        "required_datasets": (TARGET_DATASET,),
        "point_in_time_available": True,
        "inspected_roles": frozenset(),
        "search_cardinality": 1,
        "confounders": ("trend and volatility regimes overlap",),
        "simplest_refutation": "paired Sharpe fails the frozen luck threshold",
        "why_different": "tests a new cross-parent signal/risk combination",
        "basis": EvidenceBasis(
            citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE
        ),
        "estimated_trials": 1,
        "proposed_by": "recipe-fixture",
        "proposed_at": NOW,
    }
    fields.update(overrides)
    return Candidate(**fields)


def powered(planned_id: str, cumulative_trials: int):
    assessment = assess(
        hypothesis_id=planned_id,
        version=1,
        stage="RECOMBINATION",
        specification=effect(),
        observations_available=2669,
        cumulative_trials=cumulative_trials,
        assessed_at=NOW,
    )
    assert assessment.verdict is PowerVerdict.POWERED
    return assessment


def identification_question(artifact_content_id: str) -> IdentificationQuestion:
    return IdentificationQuestion(
        artifact_kind=GraphArtifactKind.COMPONENT,
        artifact_content_id=artifact_content_id,
        estimand="incremental annualised Sharpe",
        benchmark="otherwise identical parent without the component",
        regime="all preregistered observations",
        null_hypothesis="the matched component contrast is zero",
        falsified_if="the matched confidence interval includes zero",
        effect=effect(),
    )


def test_bounded_recombination_uses_failed_and_survived_parents_without_rewriting_them(
    database: Database,
) -> None:
    signal, risk, first, second = register_parents(database)
    add_parent_memory(database, first, second)
    proposed = candidate(first, second, signal, risk)
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        option = RecombinationOption(
            candidate=proposed,
            planned_hypothesis_id="H-DESCENDANT",
            power=powered("H-DESCENDANT", registry.cumulative_trials() + 1),
        )
        result = EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1")
        ).select(
            (option,),
            search_id="SEARCH-RECOMBINATION-1",
            evaluation_start=date(2027, 1, 1),
            evaluation_end=date(2028, 1, 1),
            trials_remaining=Decimal("10"),
            now=NOW,
        )

        assert result.selected is not None
        assert result.rationale is not None
        assert result.selected.search_cardinality == 1
        assert {parent.verdicts for parent in result.rationale.parents} == {
            (Verdict.REFUTED.value,),
            (Verdict.SURVIVED.value,),
        }
        assert all(artifact.contextual for artifact in result.rationale.artifacts)
        assert all(
            not artifact.supporting and not artifact.refuting
            for artifact in result.rationale.artifacts
        )

        assert ResearchMemory(session).get("parent-a").verdict is Verdict.REFUTED
        assert ResearchMemory(session).get("parent-b").verdict is Verdict.SURVIVED


def test_contextual_component_memory_proposes_a_preregisterable_matched_ablation(
    database: Database,
) -> None:
    signal, _, first, second = register_parents(database)
    add_parent_memory(database, first, second)
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        recommendation = EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1")
        ).propose_identification(first, identification_question(signal.component_id))

        assert recommendation.test is not None
        assert recommendation.test.identification_method is IdentificationMethod.ABLATION
        assert tuple(arm.included_component_ids for arm in recommendation.test.arms) == (
            (),
            (signal.component_id,),
        )
        assert recommendation.evidence.contextual
        assert not recommendation.evidence.supporting
        assert not recommendation.evidence.refuting

        frozen = registry.register_artifact_test(first, recommendation.test, now=NOW)
        assert frozen.draft == recommendation.test
        assert frozen.hypothesis_id == first.draft.hypothesis_id
        assert frozen.power.verdict is PowerVerdict.POWERED


def test_recombination_preserves_an_explicit_new_interaction_in_its_rationale(
    database: Database,
) -> None:
    signal, risk, first, second = register_parents(database)
    add_parent_memory(database, first, second)
    interaction = ComponentRelationship(
        source_component_id=signal.component_id,
        target_component_id=risk.component_id,
        relationship_type="scales-exposure",
        description="volatility targeting scales the trend signal's exposure",
    )
    base = candidate(first, second, signal, risk)
    assert base.lineage is not None
    proposed = base.model_copy(
        update={
            "graph": HypothesisGraph(
                components=(signal, risk), relationships=(interaction,)
            ),
            "lineage": HypothesisLineage(
                parents=base.lineage.parents,
                changes=(
                    *base.lineage.changes,
                    LineageChange(
                        artifact_kind=GraphArtifactKind.RELATIONSHIP,
                        action=LineageAction.INTRODUCED,
                        child_content_id=interaction.relationship_id,
                    ),
                ),
            ),
        }
    )
    # model_copy is deliberately non-validating; cross the public model boundary again.
    proposed = Candidate.model_validate(proposed.model_dump(mode="python"))

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        result = EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1")
        ).select(
            (
                RecombinationOption(
                    candidate=proposed,
                    planned_hypothesis_id="H-INTERACTION",
                    power=powered(
                        "H-INTERACTION", registry.cumulative_trials() + 1
                    ),
                ),
            ),
            search_id="SEARCH-INTERACTION",
            evaluation_start=date(2027, 1, 1),
            evaluation_end=date(2028, 1, 1),
            trials_remaining=Decimal("10"),
            now=NOW,
        )

        assert result.selected is not None
        assert result.rationale is not None
        relationship_evidence = next(
            item
            for item in result.rationale.artifacts
            if item.artifact_kind is GraphArtifactKind.RELATIONSHIP
        )
        assert relationship_evidence.artifact_content_id == interaction.relationship_id
        assert relationship_evidence.evidence_count == 0
        assert result.selected.graph is not None
        assert result.selected.graph.relationships == (interaction,)


def test_search_cardinality_reaches_the_existing_registry_pipeline(database: Database) -> None:
    signal, risk, first, second = register_parents(database)
    add_parent_memory(database, first, second)
    combined = candidate(first, second, signal, risk)
    # An exact parent graph is behaviorally non-novel and must not win, but it still counts as an
    # option examined. Its second parent contributes a REMOVED lineage action.
    first_graph = first.draft.graph
    second_graph = second.draft.graph
    assert first_graph is not None and second_graph is not None
    first_parent = ParentHypothesis(
        hypothesis_id=first.draft.hypothesis_id,
        version=1,
        graph_id=first_graph.graph_id,
    )
    second_parent = ParentHypothesis(
        hypothesis_id=second.draft.hypothesis_id,
        version=1,
        graph_id=second_graph.graph_id,
    )
    repeat = candidate(
        first,
        second,
        signal,
        risk,
        candidate_id="C-REPEAT",
        graph=first_graph,
        lineage=HypothesisLineage(
            parents=(first_parent, second_parent),
            changes=(
                LineageChange(
                    artifact_kind=GraphArtifactKind.COMPONENT,
                    action=LineageAction.INHERITED,
                    child_content_id=signal.component_id,
                    parent=first_parent,
                    parent_content_id=signal.component_id,
                ),
                LineageChange(
                    artifact_kind=GraphArtifactKind.COMPONENT,
                    action=LineageAction.REMOVED,
                    parent=second_parent,
                    parent_content_id=risk.component_id,
                ),
            ),
        ),
    )

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        burden = registry.cumulative_trials() + 2
        result = EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1")
        ).select(
            (
                RecombinationOption(
                    candidate=combined,
                    planned_hypothesis_id="H-DESCENDANT",
                    power=powered("H-DESCENDANT", burden),
                ),
                RecombinationOption(
                    candidate=repeat,
                    planned_hypothesis_id="H-REPEAT",
                    power=powered("H-REPEAT", burden),
                ),
            ),
            search_id="SEARCH-TWO-OPTIONS",
            evaluation_start=date(2027, 1, 1),
            evaluation_end=date(2028, 1, 1),
            trials_remaining=Decimal("10"),
            now=NOW,
        )
        assert result.selected is not None
        assert result.selected.candidate_id == combined.candidate_id
        assert result.selected.search_cardinality == 2
        assert any(item.candidate_id == "C-REPEAT" for item in result.deferred)
        recorded = session.execute(
            select(search_runs.c.candidates_evaluated).where(
                search_runs.c.search_id == result.search_id
            )
        ).scalar_one()
        assert recorded == 2

        draft = draft_from_candidate(
            result.selected,
            effect=effect(),
            windows=(
                DataWindow(
                    dataset=TARGET_DATASET,
                    snapshot="2028-01-01",
                    role=DataRole.PROTECTED_EVALUATION,
                    start=date(2027, 1, 1),
                    end=date(2028, 1, 1),
                ),
            ),
            available_observations=2669,
            hypothesis_id="H-DESCENDANT",
            session=session,
            search_run_id=result.search_id,
        )
        frozen = register(registry, draft)
        assert frozen.draft.search_cardinality == 2
        assert frozen.draft.graph == combined.graph
        assert frozen.draft.lineage == combined.lineage


def test_underpowered_or_ungrounded_options_are_deferred_and_nothing_runs(
    database: Database,
) -> None:
    signal, risk, first, second = register_parents(database)
    proposed = candidate(first, second, signal, risk)
    with database.transaction() as session:
        projected_trials = HypothesisRegistry(session).cumulative_trials() + 1
        underpowered = assess(
            hypothesis_id="H-DEFER",
            version=1,
            stage="RECOMBINATION",
            specification=effect().model_copy(update={"expected": Decimal("0.01")}),
            observations_available=20,
            cumulative_trials=projected_trials,
            assessed_at=NOW,
        )
        assert underpowered.verdict is PowerVerdict.UNDERPOWERED
        result = EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1")
        ).select(
            (
                RecombinationOption(
                    candidate=proposed,
                    planned_hypothesis_id="H-DEFER",
                    power=underpowered,
                ),
            ),
            search_id="SEARCH-DEFERRED",
            evaluation_start=date(2027, 1, 1),
            evaluation_end=date(2028, 1, 1),
            trials_remaining=Decimal("10"),
            now=NOW,
        )
        assert result.selected is None
        assert result.rationale is None
        assert "not power-cleared" in result.deferred[0].reason


def test_search_is_hard_bounded_and_append_only(database: Database) -> None:
    signal, risk, first, second = register_parents(database)
    proposed = candidate(first, second, signal, risk)
    option = RecombinationOption(
        candidate=proposed,
        planned_hypothesis_id="H-BOUND",
        power=powered("H-BOUND", 100),
    )
    with database.transaction() as session, pytest.raises(
        RecombinationRefused, match="hard bound"
    ):
        EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1", maximum_options=1)
        ).select(
            (option, option),
            search_id="SEARCH-TOO-WIDE",
            evaluation_start=date(2027, 1, 1),
            evaluation_end=date(2028, 1, 1),
            trials_remaining=Decimal("10"),
            now=NOW,
        )


def test_power_must_include_the_exact_recombination_search_burden(
    database: Database,
) -> None:
    signal, risk, first, second = register_parents(database)
    option = RecombinationOption(
        candidate=candidate(first, second, signal, risk),
        planned_hypothesis_id="H-STALE-POWER",
        power=powered("H-STALE-POWER", 1),
    )
    with database.transaction() as session, pytest.raises(
        RecombinationRefused, match="charging every recombination option"
    ):
        EvidenceAwareRecombiner(
            session, RecombinationPolicy(version="1")
        ).select(
            (option,),
            search_id="SEARCH-STALE-POWER",
            evaluation_start=date(2027, 1, 1),
            evaluation_end=date(2028, 1, 1),
            trials_remaining=Decimal("10"),
            now=NOW,
        )
