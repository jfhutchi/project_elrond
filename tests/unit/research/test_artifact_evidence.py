"""Component evidence is earned by identifying experiments, never inherited from parents."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.artifact_testing import (
    ArtifactOutcomeVerdict,
    ArtifactTestDraft,
    ArtifactTestRefused,
    IdentificationArm,
    IdentificationMethod,
)
from quantbot.research.builder import compile_experiment
from quantbot.research.hypothesis_graph import GraphArtifactKind, HypothesisGraph
from quantbot.research.memory import ArtifactEvidenceStatus, ResearchMemory, Verdict
from quantbot.research.power import PowerOverride
from quantbot.research.registry import HypothesisRegistry
from quantbot.research.runner import (
    ArtifactMeasured,
    ExperimentRunner,
    Measured,
    record_from_run,
)
from quantbot.storage import Database
from tests.unit.research.test_hypothesis_graph import component, relationship, root_lineage
from tests.unit.research.test_runner import (
    COSTS,
    DESIGN,
    NOW,
    critique,
    draft,
    provenance,
    snapshot,
)


@pytest.fixture
def database(tmp_path: Path):
    database = Database.create(tmp_path / "artifact-evidence.db")
    yield database
    database.close()


def graph_case():
    signal = component("signal", "time-series-momentum", parameter="252")
    risk = component("risk-control", "atr-stop", parameter="2")
    interaction = relationship(signal, risk, "gates")
    graph = HypothesisGraph(components=(signal, risk), relationships=(interaction,))
    return signal, risk, interaction, graph


def register_graph(database: Database):
    signal, risk, interaction, graph = graph_case()
    candidate = draft(graph=graph, lineage=root_lineage(graph))
    with database.transaction() as session:
        registration = HypothesisRegistry(session).register(
            candidate, now=NOW, critique=critique()
        )
    return signal, risk, interaction, registration


def factorial_arms(signal_id: str, risk_id: str) -> tuple[IdentificationArm, ...]:
    return (
        IdentificationArm(name="neither", included_component_ids=()),
        IdentificationArm(name="signal", included_component_ids=(signal_id,)),
        IdentificationArm(name="risk", included_component_ids=(risk_id,)),
        IdentificationArm(name="both", included_component_ids=(signal_id, risk_id)),
    )


def artifact_test_draft(registration, kind, artifact_id, *, benchmark="cash", effect=None):
    signal, risk = registration.draft.graph.components
    return ArtifactTestDraft(
        artifact_kind=kind,
        artifact_content_id=artifact_id,
        identification_method=IdentificationMethod.FACTORIAL,
        arms=factorial_arms(signal.component_id, risk.component_id),
        estimand="annualised Sharpe difference",
        benchmark=benchmark,
        regime="all preregistered market regimes",
        null_hypothesis="The artifact has zero incremental effect.",
        falsified_if="The contrast fails to clear its frozen luck bar.",
        effect=registration.draft.effect if effect is None else effect,
    )


def run(
    database: Database,
    manifests: Path,
    registration,
    artifact_tests=(),
    artifact_measurements=(),
    *,
    statistic=Decimal("0"),
):
    plan = compile_experiment(
        registration, datasets=[snapshot()], artifact_tests=artifact_tests
    )
    code, environment = provenance()
    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            lambda _plan, _access: Measured(
                effect=Decimal("0"),
                test_statistic=statistic,
                probes_run=plan.probes,
                artifact_measurements=artifact_measurements,
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )
    return result


def remember(database: Database, result):
    record = record_from_run(
        result,
        record_id=f"record-{result.manifest.experiment_id[:16]}",
        subject="componentized trend and risk hypothesis",
        source="artifact evidence test",
        now=NOW,
    )
    with database.transaction() as session:
        return ResearchMemory(session).record_run_artifacts(result, record)


def test_refuted_combination_does_not_refute_its_components(
    database: Database, tmp_path: Path
) -> None:
    signal, risk, interaction, registration = register_graph(database)
    result = run(database, tmp_path / "manifests", registration)
    assert result.outcome.verdict.value == "REFUTED"

    evidence = remember(database, result)
    assert {entry.artifact_content_id for entry in evidence} == {
        signal.component_id,
        risk.component_id,
        interaction.relationship_id,
    }
    assert all(
        entry.epistemic_status is ArtifactEvidenceStatus.CONTEXTUAL_ASSOCIATION
        for entry in evidence
    )
    assert all(entry.verdict is None for entry in evidence)


def test_factorial_design_records_a_b_and_interaction_separately(
    database: Database, tmp_path: Path
) -> None:
    signal, risk, interaction, registration = register_graph(database)
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        tests = tuple(
            registry.register_artifact_test(
                registration,
                artifact_test_draft(registration, kind, artifact_id),
                now=NOW,
            )
            for kind, artifact_id in (
                (GraphArtifactKind.COMPONENT, signal.component_id),
                (GraphArtifactKind.COMPONENT, risk.component_id),
                (GraphArtifactKind.RELATIONSHIP, interaction.relationship_id),
            )
        )
    plan_probes = compile_experiment(registration, datasets=[snapshot()]).probes
    result = run(
        database,
        tmp_path / "manifests",
        registration,
        tests,
        tuple(
            ArtifactMeasured(
                test_id=test.test_id,
                effect=Decimal("1"),
                test_statistic=statistic,
                probes_run=plan_probes,
            )
            for test, statistic in zip(
                tests, (Decimal("4"), Decimal("0"), Decimal("5")), strict=True
            )
        ),
        statistic=Decimal("4"),
    )
    assert [item.verdict for item in result.outcome.artifact_outcomes] == [
        ArtifactOutcomeVerdict.SURVIVED,
        ArtifactOutcomeVerdict.REFUTED,
        ArtifactOutcomeVerdict.SURVIVED,
    ]

    remembered = remember(database, result)
    verdicts = {entry.artifact_content_id: entry.verdict for entry in remembered}
    assert verdicts == {
        signal.component_id: Verdict.SURVIVED,
        risk.component_id: Verdict.REFUTED,
        interaction.relationship_id: Verdict.SURVIVED,
    }
    assert all(entry.manifest_hash == result.manifest.manifest_hash for entry in remembered)
    assert all(entry.datasets == result.manifest.datasets for entry in remembered)
    assert all(entry.code == result.manifest.code for entry in remembered)


def test_underpowered_override_stays_underpowered_at_artifact_level(
    database: Database, tmp_path: Path
) -> None:
    signal, _risk, _interaction, registration = register_graph(database)
    weak_effect = registration.draft.effect.model_copy(
        update={"expected": Decimal("0.01"), "minimum_practical": Decimal("0.005")}
    )
    with database.transaction() as session:
        test = HypothesisRegistry(session).register_artifact_test(
            registration,
            artifact_test_draft(
                registration,
                GraphArtifactKind.COMPONENT,
                signal.component_id,
                effect=weak_effect,
            ),
            now=NOW,
            override=PowerOverride(authorized_by="operator", reason="method development"),
        )
    probes = compile_experiment(registration, datasets=[snapshot()]).probes
    result = run(
        database,
        tmp_path / "manifests",
        registration,
        (test,),
        (
            ArtifactMeasured(
                test_id=test.test_id,
                effect=Decimal("3"),
                test_statistic=Decimal("20"),
                probes_run=probes,
            ),
        ),
        statistic=Decimal("4"),
    )
    remembered = remember(database, result)
    direct = next(item for item in remembered if item.artifact_content_id == signal.component_id)
    assert direct.verdict is Verdict.UNDERPOWERED


def test_conflicting_direct_findings_coexist_and_are_surfaced(
    database: Database, tmp_path: Path
) -> None:
    signal, _risk, _interaction, registration = register_graph(database)
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        supported = registry.register_artifact_test(
            registration,
            artifact_test_draft(
                registration, GraphArtifactKind.COMPONENT, signal.component_id
            ),
            now=NOW,
        )
        refuted = registry.register_artifact_test(
            registration,
            artifact_test_draft(
                registration,
                GraphArtifactKind.COMPONENT,
                signal.component_id,
                benchmark="SPY buy-and-hold",
            ),
            now=NOW + timedelta(seconds=1),
        )
    probes = compile_experiment(registration, datasets=[snapshot()]).probes
    result = run(
        database,
        tmp_path / "manifests",
        registration,
        (supported, refuted),
        (
            ArtifactMeasured(
                test_id=supported.test_id,
                effect=Decimal("1"),
                test_statistic=Decimal("4"),
                probes_run=probes,
            ),
            ArtifactMeasured(
                test_id=refuted.test_id,
                effect=Decimal("0"),
                test_statistic=Decimal("0"),
                probes_run=probes,
            ),
        ),
        statistic=Decimal("4"),
    )
    remember(database, result)
    with database.transaction() as session:
        summary = ResearchMemory(session).summarize_artifact(signal.component_id)
    assert summary.contradictory
    assert len(summary.supporting) == 1
    assert len(summary.refuting) == 1
    assert {item.benchmark for item in (*summary.supporting, *summary.refuting)} == {
        "cash",
        "SPY buy-and-hold",
    }


def test_artifact_tests_cannot_be_backfilled_after_the_holdout_was_read(
    database: Database, tmp_path: Path
) -> None:
    signal, _risk, _interaction, registration = register_graph(database)
    run(database, tmp_path / "manifests", registration)
    with database.transaction() as session, pytest.raises(
        ArtifactTestRefused, match="before protected data"
    ):
        HypothesisRegistry(session).register_artifact_test(
            registration,
            artifact_test_draft(
                registration, GraphArtifactKind.COMPONENT, signal.component_id
            ),
            now=NOW + timedelta(days=1),
        )


def test_confounded_component_arms_are_not_identifying(database: Database) -> None:
    signal, risk, _interaction, registration = register_graph(database)
    confounded = ArtifactTestDraft(
        artifact_kind=GraphArtifactKind.COMPONENT,
        artifact_content_id=signal.component_id,
        identification_method=IdentificationMethod.ABLATION,
        arms=(
            IdentificationArm(name="without", included_component_ids=()),
            IdentificationArm(
                name="with-signal-and-risk",
                included_component_ids=(signal.component_id, risk.component_id),
            ),
        ),
        estimand="annualised Sharpe difference",
        benchmark="cash",
        regime="all preregistered market regimes",
        null_hypothesis="The signal has zero incremental effect.",
        falsified_if="The contrast fails to clear its frozen luck bar.",
        effect=registration.draft.effect,
    )
    with database.transaction() as session, pytest.raises(
        ArtifactTestRefused, match="matched component background"
    ):
        HypothesisRegistry(session).register_artifact_test(
            registration, confounded, now=NOW
        )


def test_missing_artifact_results_remain_unassessed(
    database: Database, tmp_path: Path
) -> None:
    signal, _risk, _interaction, registration = register_graph(database)
    with database.transaction() as session:
        test = HypothesisRegistry(session).register_artifact_test(
            registration,
            artifact_test_draft(
                registration, GraphArtifactKind.COMPONENT, signal.component_id
            ),
            now=NOW,
        )
    result = run(
        database,
        tmp_path / "manifests",
        registration,
        (test,),
        (),
        statistic=Decimal("4"),
    )
    entries = remember(database, result)
    signal_evidence = next(
        item for item in entries if item.artifact_content_id == signal.component_id
    )
    assert signal_evidence.epistemic_status is ArtifactEvidenceStatus.UNASSESSED
    assert signal_evidence.test_id == test.test_id
    assert signal_evidence.verdict is None


def test_each_artifact_claim_is_charged_to_trial_and_window_accounting(
    database: Database, tmp_path: Path
) -> None:
    signal, risk, _interaction, registration = register_graph(database)
    before = registration.cumulative_trials
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        first = registry.register_artifact_test(
            registration,
            artifact_test_draft(
                registration, GraphArtifactKind.COMPONENT, signal.component_id
            ),
            now=NOW,
        )
        second = registry.register_artifact_test(
            registration,
            artifact_test_draft(
                registration, GraphArtifactKind.COMPONENT, risk.component_id
            ),
            now=NOW + timedelta(seconds=1),
        )
        assert first.cumulative_trials == before + 1
        assert second.cumulative_trials == before + 2
        assert registry.cumulative_trials() == before + 2
    run(database, tmp_path / "manifests", registration, (first, second))
    with database.transaction() as session:
        consumption = HypothesisRegistry(session).window_consumption(
            snapshot().dataset, snapshot().start, snapshot().end
        )
    assert consumption.trials >= before + 2
