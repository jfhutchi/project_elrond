"""Component graph identity and immutable multi-parent lineage (#187).

These tests stay deliberately claim-level. They prove which exact components and interactions a
hypothesis contained, not what any component contributed to the result. Component evidence is a
later issue and must not be manufactured from a parent verdict here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from quantbot.research import (
    BehaviorTerm,
    ComponentRelationship,
    DataRole,
    GraphArtifactKind,
    HypothesisComponent,
    HypothesisGraph,
    HypothesisLineage,
    HypothesisRegistry,
    LineageAction,
    LineageChange,
    ParentHypothesis,
    RecordKind,
    RefusalReason,
    RegistrationRefused,
    ResearchMemory,
    ResearchRecord,
    Verdict,
)
from quantbot.storage import Database
from quantbot.storage.schema import hypotheses
from tests.unit.research.test_registry import NOW, make_draft, register, window


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database.create(tmp_path / "component-graph.db")
    yield db
    db.close()


def component(
    component_type: str,
    implementation: str,
    *,
    parameter: str = "1",
    display_name: str | None = None,
) -> HypothesisComponent:
    return HypothesisComponent(
        component_type=component_type,
        behavior=(
            BehaviorTerm(key="implementation", value=implementation),
            BehaviorTerm(key="parameter", value=parameter),
        ),
        display_name=display_name,
    )


def relationship(
    source: HypothesisComponent,
    target: HypothesisComponent,
    relationship_type: str,
    *,
    display_name: str | None = None,
) -> ComponentRelationship:
    return ComponentRelationship(
        source_component_id=source.component_id,
        target_component_id=target.component_id,
        relationship_type=relationship_type,
        display_name=display_name,
    )


def root_lineage(graph: HypothesisGraph) -> HypothesisLineage:
    return HypothesisLineage(
        changes=tuple(
            [
                LineageChange(
                    artifact_kind=GraphArtifactKind.COMPONENT,
                    action=LineageAction.INTRODUCED,
                    child_content_id=identity,
                )
                for identity in graph.component_ids
            ]
            + [
                LineageChange(
                    artifact_kind=GraphArtifactKind.RELATIONSHIP,
                    action=LineageAction.INTRODUCED,
                    child_content_id=identity,
                )
                for identity in graph.relationship_ids
            ]
        )
    )


def draft_for(
    hypothesis_id: str,
    graph: HypothesisGraph,
    lineage: HypothesisLineage,
    *,
    dataset: str,
    features: tuple[str, ...],
    materially_different: str | None = None,
):
    return make_draft(
        hypothesis_id=hypothesis_id,
        family_id="component-lineage",
        graph=graph,
        lineage=lineage,
        universe=(hypothesis_id,),
        features=features,
        windows=(
            window(
                DataRole.FORWARD_PAPER,
                "2026-01-02",
                "2026-08-18",
                dataset=dataset,
            ),
        ),
        materially_different=materially_different,
    )


def test_cosmetic_names_and_input_order_do_not_create_fake_identity() -> None:
    first = HypothesisComponent(
        component_type="signal",
        behavior=(
            BehaviorTerm(key="lookback", value=200),
            BehaviorTerm(key="implementation", value="close-above-sma"),
        ),
        display_name="The clever trend signal",
        description="Written for the research report.",
    )
    renamed = HypothesisComponent(
        component_type="signal",
        behavior=(
            BehaviorTerm(key="implementation", value="close-above-sma"),
            BehaviorTerm(key="lookback", value=200),
        ),
        display_name="SMA gate",
        description="Cosmetically rewritten prose.",
    )
    assert first.component_id == renamed.component_id

    risk = component("risk-control", "volatility-target", parameter="0.10")
    link = relationship(first, risk, "gates", display_name="signal controls risk")
    renamed_link = relationship(renamed, risk, "gates", display_name="new diagram label")
    before = HypothesisGraph(components=(first, risk), relationships=(link,))
    after = HypothesisGraph(components=(risk, renamed), relationships=(renamed_link,))

    assert link.relationship_id == renamed_link.relationship_id
    assert before.graph_id == after.graph_id
    assert before.model_dump_json() != after.model_dump_json(), "presentation metadata is retained"


def test_material_logic_parameters_and_interactions_create_new_identity() -> None:
    sma_200 = component("signal", "close-above-sma", parameter="200")
    sma_150 = component("signal", "close-above-sma", parameter="150")
    breakout = component("signal", "donchian-breakout", parameter="200")
    risk = component("risk-control", "volatility-target", parameter="0.10")

    assert len({sma_200.component_id, sma_150.component_id, breakout.component_id}) == 3

    gates = relationship(sma_200, risk, "gates")
    sizes = relationship(sma_200, risk, "sizes")
    assert gates.relationship_id != sizes.relationship_id
    assert (
        HypothesisGraph(components=(sma_200, risk), relationships=(gates,)).graph_id
        != HypothesisGraph(components=(sma_200, risk), relationships=(sizes,)).graph_id
    )


def test_cosmetic_graph_renaming_is_still_a_registry_duplicate(database: Database) -> None:
    original = component("signal", "close-above-sma", parameter="200", display_name="Trend")
    renamed = component("signal", "close-above-sma", parameter="200", display_name="Alpha gate")
    first_graph = HypothesisGraph(components=(original,))
    renamed_graph = HypothesisGraph(components=(renamed,))
    assert first_graph.graph_id == renamed_graph.graph_id

    with database.transaction() as session:
        register(
            HypothesisRegistry(session, prior_windows=()),
            draft_for(
                "H-NAMED-ONCE",
                first_graph,
                root_lineage(first_graph),
                dataset="first-name-forward",
                features=("first-name",),
            ),
        )

    cosmetically_new = draft_for(
        "H-NAMED-TWICE",
        renamed_graph,
        root_lineage(renamed_graph),
        dataset="second-name-forward",
        features=("completely-different-display-feature",),
    )
    with database.transaction() as session:
        report = HypothesisRegistry(session, prior_windows=()).novelty_report(cosmetically_new)
    assert report.highest_overlap == 1.0
    assert report.nearest == ("H-NAMED-ONCE v1",)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session, prior_windows=()), cosmetically_new)
    assert error.value.reason is RefusalReason.DUPLICATE


def test_relationships_must_name_exact_components_in_the_graph() -> None:
    signal = component("signal", "momentum", parameter="252")
    risk = component("risk-control", "atr-stop", parameter="2.0")
    dangling = relationship(signal, risk, "gates")

    with pytest.raises(ValidationError, match="endpoint must name a component"):
        HypothesisGraph(components=(signal,), relationships=(dangling,))


def _multi_parent_case() -> tuple[
    HypothesisGraph,
    HypothesisLineage,
    HypothesisGraph,
    HypothesisLineage,
    HypothesisGraph,
    HypothesisLineage,
]:
    signal = component("signal", "time-series-momentum", parameter="252")
    risk = component("risk-control", "atr-stop", parameter="2.0")
    signal_gates_risk = relationship(signal, risk, "gates")
    first_graph = HypothesisGraph(
        components=(signal, risk), relationships=(signal_gates_risk,)
    )

    sizing = component("sizing", "inverse-volatility", parameter="0.10")
    benchmark = component("benchmark", "spy-buy-and-hold")
    relative_to = relationship(sizing, benchmark, "benchmarked-by")
    second_graph = HypothesisGraph(
        components=(sizing, benchmark), relationships=(relative_to,)
    )

    tighter_risk = component("risk-control", "atr-stop", parameter="1.5")
    exit_rule = component("exit", "donchian-exit", parameter="20")
    modified_gate = relationship(signal, tighter_risk, "gates")
    risk_limits_size = relationship(tighter_risk, sizing, "constrains")
    exit_closes_signal = relationship(exit_rule, signal, "terminates")
    child_graph = HypothesisGraph(
        components=(signal, tighter_risk, sizing, exit_rule),
        relationships=(modified_gate, risk_limits_size, exit_closes_signal),
    )

    first_parent = ParentHypothesis(
        hypothesis_id="H-PARENT-A", version=1, graph_id=first_graph.graph_id
    )
    second_parent = ParentHypothesis(
        hypothesis_id="H-PARENT-B", version=1, graph_id=second_graph.graph_id
    )
    child_lineage = HypothesisLineage(
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
                action=LineageAction.MODIFIED,
                child_content_id=tighter_risk.component_id,
                parent=first_parent,
                parent_content_id=risk.component_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INHERITED,
                child_content_id=sizing.component_id,
                parent=second_parent,
                parent_content_id=sizing.component_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.REMOVED,
                parent=second_parent,
                parent_content_id=benchmark.component_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INTRODUCED,
                child_content_id=exit_rule.component_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.RELATIONSHIP,
                action=LineageAction.MODIFIED,
                child_content_id=modified_gate.relationship_id,
                parent=first_parent,
                parent_content_id=signal_gates_risk.relationship_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.RELATIONSHIP,
                action=LineageAction.REMOVED,
                parent=second_parent,
                parent_content_id=relative_to.relationship_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.RELATIONSHIP,
                action=LineageAction.INTRODUCED,
                child_content_id=risk_limits_size.relationship_id,
            ),
            LineageChange(
                artifact_kind=GraphArtifactKind.RELATIONSHIP,
                action=LineageAction.INTRODUCED,
                child_content_id=exit_closes_signal.relationship_id,
            ),
        ),
    )
    return (
        first_graph,
        root_lineage(first_graph),
        second_graph,
        root_lineage(second_graph),
        child_graph,
        child_lineage,
    )


def test_multi_parent_lineage_round_trips_without_rewriting_history(
    database: Database,
) -> None:
    first_graph, first_lineage, second_graph, second_lineage, child_graph, child_lineage = (
        _multi_parent_case()
    )
    with database.transaction() as session:
        registry = HypothesisRegistry(session, prior_windows=())
        first = register(
            registry,
            draft_for(
                "H-PARENT-A",
                first_graph,
                first_lineage,
                dataset="parent-a-forward",
                features=("momentum", "atr"),
            ),
        )
        register(
            registry,
            draft_for(
                "H-PARENT-B",
                second_graph,
                second_lineage,
                dataset="parent-b-forward",
                features=("inverse-volatility", "benchmark"),
            ),
        )
        child = register(
            registry,
            draft_for(
                "H-CHILD",
                child_graph,
                child_lineage,
                dataset="child-forward",
                features=("momentum", "atr", "inverse-volatility", "donchian-exit"),
                materially_different="combines two parents, tightens risk, and adds an exit",
            ),
        )
        raw = session.execute(
            select(hypotheses.c.document_json).where(
                hypotheses.c.hypothesis_id == "H-CHILD"
            )
        ).scalar_one()

    assert child.draft.lineage is not None
    assert {parent.hypothesis_id for parent in child.draft.lineage.parents} == {
        "H-PARENT-A",
        "H-PARENT-B",
    }
    assert {change.action for change in child.draft.lineage.changes} == set(LineageAction)
    assert json.loads(str(raw))["draft"]["graph"]["schema_version"] == 1

    with database.transaction() as session:
        registry = HypothesisRegistry(session, prior_windows=())
        reloaded_parent = registry.get("H-PARENT-A", 1)
        reloaded_child = registry.get("H-CHILD", 1)
        assert reloaded_parent is not None and reloaded_child is not None
        assert reloaded_parent.content_hash == first.content_hash
        assert reloaded_parent.draft.graph == first_graph
        assert reloaded_child.content_hash == child.content_hash
        assert reloaded_child.draft.graph == child_graph
        signal_id = next(
            item.component_id
            for item in first_graph.components
            if item.component_type == "signal"
        )
        users = registry.registrations_for_component(signal_id)
        assert {item.draft.hypothesis_id for item in users} == {
            "H-PARENT-A",
            "H-CHILD",
        }
        assert registry.registrations_for_graph(child_graph.graph_id) == [reloaded_child]

    changed = draft_for(
        "H-PARENT-A",
        HypothesisGraph(
            components=(component("signal", "different-logic"),), relationships=()
        ),
        root_lineage(
            HypothesisGraph(
                components=(component("signal", "different-logic"),), relationships=()
            )
        ),
        dataset="parent-a-forward",
        features=("different",),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session, prior_windows=()), changed)
    assert error.value.reason is RefusalReason.ALREADY_REGISTERED


def test_registry_refuses_lineage_that_lies_about_the_exact_parent_graph(
    database: Database,
) -> None:
    signal = component("signal", "momentum", parameter="252")
    parent_graph = HypothesisGraph(components=(signal,))
    with database.transaction() as session:
        register(
            HypothesisRegistry(session, prior_windows=()),
            draft_for(
                "H-EXACT-PARENT",
                parent_graph,
                root_lineage(parent_graph),
                dataset="exact-parent-forward",
                features=("momentum",),
            ),
        )

    false_parent = ParentHypothesis(
        hypothesis_id="H-EXACT-PARENT", version=1, graph_id="f" * 64
    )
    child_graph = HypothesisGraph(components=(signal,))
    false_lineage = HypothesisLineage(
        parents=(false_parent,),
        changes=(
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INHERITED,
                child_content_id=signal.component_id,
                parent=false_parent,
                parent_content_id=signal.component_id,
            ),
        ),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(
            HypothesisRegistry(session, prior_windows=()),
            draft_for(
                "H-FALSE-CHILD",
                child_graph,
                false_lineage,
                dataset="false-child-forward",
                features=("momentum-child",),
                materially_different="a lineage-integrity test",
            ),
        )
    assert error.value.reason is RefusalReason.INVALID_LINEAGE
    assert parent_graph.graph_id in error.value.detail


@pytest.mark.parametrize(
    "parent_verdict",
    [Verdict.SURVIVED, Verdict.REFUTED, Verdict.UNDERPOWERED],
)
def test_parent_verdicts_do_not_propagate_to_components_or_children(
    database: Database, parent_verdict: Verdict
) -> None:
    signal = component("signal", "momentum", parameter="252")
    graph = HypothesisGraph(components=(signal,))
    parent_ref = ParentHypothesis(hypothesis_id="H-REFUTED", version=1, graph_id=graph.graph_id)
    child_lineage = HypothesisLineage(
        parents=(parent_ref,),
        changes=(
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INHERITED,
                child_content_id=signal.component_id,
                parent=parent_ref,
                parent_content_id=signal.component_id,
            ),
        ),
    )

    with database.transaction() as session:
        registry = HypothesisRegistry(session, prior_windows=())
        register(
            registry,
            draft_for(
                "H-REFUTED",
                graph,
                root_lineage(graph),
                dataset="refuted-forward",
                features=("momentum",),
            ),
        )
        memory = ResearchMemory(session)
        memory.record(
            ResearchRecord(
                record_id="F-REFUTED-WHOLE",
                kind=RecordKind.FINDING,
                subject="the complete momentum claim",
                statement="The registered complete strategy was refuted.",
                verdict=parent_verdict,
                hypothesis_id="H-REFUTED",
                hypothesis_version=1,
                source="test experiment",
                recorded_at=NOW,
            )
        )
        register(
            registry,
            draft_for(
                "H-DESCENDANT",
                graph,
                child_lineage,
                dataset="descendant-forward",
                features=("momentum-descendant",),
                materially_different="a fresh claim that reuses one unassessed component",
            ),
        )
        assert memory.findings_for("H-DESCENDANT") == []
        assert memory.findings_for("H-REFUTED")[0].verdict is parent_verdict
        users = registry.registrations_for_component(signal.component_id)
        assert {item.draft.hypothesis_id for item in users} == {
            "H-REFUTED",
            "H-DESCENDANT",
        }

    with pytest.raises(ValidationError, match="verdict"):
        HypothesisComponent(
            component_type="signal",
            behavior=(BehaviorTerm(key="implementation", value="momentum"),),
            verdict="REFUTED",
        )
