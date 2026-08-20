"""One candidate through every gate, in order, against one database.

Each gate has unit tests proving it refuses what it should. None of them proves the gates
*compose*: that the object one produces is the object the next accepts, that the trial counter
one increments is the one another reads, and that a candidate which should reach protected data
actually can. A system of individually correct parts that do not fit together refuses
everything, which looks exactly like a system that is working.

The order is the one the roadmap specifies:

    candidate -> novelty + consumed data -> power -> critic -> registration
              -> compile -> outcome -> memory -> promotion
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.budget import (
    BudgetGovernor,
    Cap,
    Resource,
    dataset_key,
)
from quantbot.research.builder import (
    ExperimentOutcome,
    OutcomeVerdict,
    compile_experiment,
)
from quantbot.research.critic import (
    CriticVerdict,
    Critique,
    DeterministicCritic,
    Objection,
    Severity,
    cost_ladder,
    cross_asset_generality,
    deterministic_objections,
    hidden_beta,
    regime_split,
)
from quantbot.research.dashboard import EvidencePanel
from quantbot.research.director import (
    ResearchDirector,
    ResearchTask,
    TaskState,
)
from quantbot.research.discovery import (
    Candidate,
    DiscoveryMode,
    admissible,
)
from quantbot.research.manifest import DatasetSnapshot
from quantbot.research.memory import (
    RecordKind,
    ResearchMemory,
    ResearchRecord,
    Verdict,
)
from quantbot.research.power import (
    ComparisonStructure,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    PowerVerdict,
)
from quantbot.research.promotion import (
    PromotionState,
    Stage,
    promote,
    survivor_objections,
)
from quantbot.research.registry import (
    DataRole,
    DataWindow,
    HypothesisDraft,
    HypothesisRegistry,
    RefusalReason,
    RegistrationRefused,
)
from quantbot.research.sources import EpistemicStatus, EvidenceBasis
from quantbot.storage import Database

NOW = datetime(2026, 8, 19, 14, 30, tzinfo=UTC)

#: A genuinely untouched dataset. The point of the pipeline is that a candidate on new data can
#: get all the way through; a candidate on the exhausted window cannot, and both are asserted.
NEW_DATASET = "fred-macro-daily"
SPENT_DATASET = "sip-us-equities-daily"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "pipeline.db")
    yield db
    db.close()


def effect(**overrides: object) -> EffectSpecification:
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": Decimal("1.2"),
        "minimum_practical": Decimal("0.6"),
        "justification": "Published term-structure effects sit near this magnitude.",
        "comparison": ComparisonStructure.PAIRED,
        "economics": EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def candidate(**overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "candidate_id": "C-1",
        "mode": DiscoveryMode.LITERATURE_GAP,
        "question": "Does the yield-curve slope predict equity drawdowns one month ahead?",
        "family_id": "macro-term-structure",
        "overlooked": "The slope is studied for recessions, rarely for drawdown timing.",
        "why_unpriced": "Monthly rebalancing on a macro series is unattractive to fast money.",
        "expected_direction": "A flatter curve precedes deeper drawdowns.",
        "horizon_observations": 21,
        "universe": ("SPY",),
        "features": ("yield_curve_slope_10y2y",),
        "required_datasets": (NEW_DATASET,),
        "point_in_time_available": True,
        "inspected_roles": frozenset({DataRole.DISCOVERY}),
        "search_cardinality": 3,
        "confounders": ("the slope is itself driven by policy expectations",),
        "simplest_refutation": "Sort months by slope and compare the following drawdown.",
        "basis": EvidenceBasis(
            citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE
        ),
        "estimated_trials": 3,
        "proposed_by": "literature-gap-miner",
        "proposed_at": NOW,
    }
    fields.update(overrides)
    return Candidate(**fields)


def draft_from(proposal: Candidate, **overrides: object) -> HypothesisDraft:
    fields: dict[str, object] = {
        "hypothesis_id": "H-PIPE-1",
        "family_id": proposal.family_id,
        "question": proposal.question,
        "prediction": "A flatter curve precedes a deeper drawdown by at least 0.6 Sharpe.",
        "null_hypothesis": "Slope carries no information about forward drawdown.",
        "falsified_if": "The paired difference fails to clear the frozen luck bar.",
        "universe": proposal.universe,
        "features": proposal.features,
        "target": "next-month maximum drawdown",
        "windows": (
            DataWindow(
                dataset=NEW_DATASET,
                snapshot="2026-08-19",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(1990, 1, 2),
                end=date(2026, 8, 14),
            ),
        ),
        "primary_estimand": "annualised Sharpe of the slope-sorted overlay",
        "effect": effect(),
        "available_observations": 9000,
        "search_cardinality": proposal.search_cardinality,
        "confounders": proposal.confounders,
        "basis": proposal.basis,
        "proposed_by": proposal.proposed_by,
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset=NEW_DATASET,
        snapshot="2026-08-19",
        role="PROTECTED_EVALUATION",
        start=date(1990, 1, 2),
        end=date(2026, 8, 14),
        content_hash="fred-hash",
        observations=9000,
        provider="fred",
        retrieved_at=NOW,
        earliest_available_at=NOW,
        transformation_version="slope-v1",
    )


def clean_critique(
    hypothesis_id: str, version: int, statistics: dict[str, list[float]]
) -> Critique:
    """Run the real deterministic checks and hand back whatever verdict they produce."""
    objections = deterministic_objections(
        beta=hidden_beta(statistics["strategy"], statistics["benchmark"]),
        ladder=cost_ladder(gross_return=0.42, trade_legs=24.0),
        split=regime_split({"1990s": 0.4, "2000s": 0.3, "2010s": 0.5, "2020s": 0.2}),
        generality=cross_asset_generality(
            {"SPY": 3.4, "IWM": 3.1, "EFA": 3.0}, threshold=2.9
        ),
        shifted_probe_passed=True,
        survivorship_bias=None,
        alpha_threshold=2.9,
    )
    return DeterministicCritic(objections).review(hypothesis_id, version, now=NOW)


def series() -> dict[str, list[float]]:
    """A benchmark and a strategy that genuinely adds return, so the critic can pass it."""
    import random

    generator = random.Random(5)
    benchmark = [generator.gauss(0.0004, 0.011) for _ in range(2669)]
    strategy = [0.5 * value + generator.gauss(0.0006, 0.004) for value in benchmark]
    return {"benchmark": benchmark, "strategy": strategy}


def test_a_candidate_on_new_data_reaches_protected_evaluation(database: Database) -> None:
    """The whole chain, in order, on a dataset nothing has searched."""
    proposal = candidate()

    # 1. Admission, against consumed-window state.
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        consumption = [
            registry.window_consumption(NEW_DATASET, date(1990, 1, 2), date(2026, 8, 14))
        ]
    ok, reason = admissible(proposal, consumption=consumption)
    assert ok, reason
    assert consumption[0].status == "UNTOUCHED"

    # 2. Statistical budget admits the search it declares.
    caps = [Cap(budget_key=dataset_key(NEW_DATASET), resource=Resource.TRIALS, limit=Decimal(50))]
    with database.transaction() as session:
        BudgetGovernor(session, caps).spend(
            dataset_key(NEW_DATASET),
            Resource.TRIALS,
            Decimal(proposal.search_cardinality),
            task=proposal.candidate_id,
            now=NOW,
        )

    # 3. The deterministic critic runs the real checks and raises no blocking objection.
    critique = clean_critique("H-PIPE-1", 1, series())
    assert critique.verdict is CriticVerdict.PROCEED

    # 4. Registration: novelty, contamination and power all pass, and the critique is frozen in.
    draft = draft_from(proposal)
    with database.transaction() as session:
        registration = HypothesisRegistry(session).register(draft, now=NOW, critique=critique)
    assert registration.power.verdict is PowerVerdict.POWERED
    assert registration.critique == critique

    # 5. Compilation picks the statistic from the declared structure and demands the probes.
    plan = compile_experiment(registration, datasets=[snapshot()])
    assert plan.statistics.test.value == "JOBSON_KORKIE_MEMMEL"
    assert "shifted-feature-probe" in plan.probes
    assert plan.registration_hash == registration.content_hash

    # 6. An outcome that ran every probe on the production path, clearing its own frozen bar.
    outcome = ExperimentOutcome(
        plan=plan,
        verdict=OutcomeVerdict.SURVIVED,
        effect=Decimal("1.25"),
        test_statistic=plan.statistics.luck_threshold_z + Decimal("0.5"),
        probes_run=plan.probes,
        detail="cleared every probe",
        completed_at=NOW,
    )
    assert outcome.citable
    assert survivor_objections(outcome) == ()

    # 7. Memory records it as a finding linked to the exact hypothesis version.
    with database.transaction() as session:
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="finding-pipe-1",
                kind=RecordKind.FINDING,
                verdict=Verdict.SURVIVED,
                subject="yield-curve slope and forward drawdown",
                statement=f"Cleared the {plan.statistics.luck_threshold_z} bar.",
                hypothesis_id="H-PIPE-1",
                hypothesis_version=1,
                evidence_hash=registration.content_hash,
                source="pipeline test",
                recorded_at=NOW,
            )
        )
    with database.transaction() as session:
        recalled = ResearchMemory(session).recall("yield-curve slope")
    assert [record.verdict for record in recalled] == [Verdict.SURVIVED]

    # 8. Promotion reaches RESEARCH_SURVIVOR and stops well short of anything live.
    promoted = promote(
        PromotionState(
            strategy_id="slope-overlay",
            stage=Stage.REGISTERED,
            strategy_version="0.1.0",
            configuration_hash="cfg-slope",
            reason="registered",
            actor="director",
            updated_at=NOW,
        ),
        Stage.RESEARCH_SURVIVOR,
        actor="director",
        reason="survived every probe",
        now=NOW,
        outcome=outcome,
    )
    assert promoted.stage is Stage.RESEARCH_SURVIVOR
    assert not promoted.eligible_for_human_review


def test_the_same_candidate_on_the_exhausted_window_is_stopped(database: Database) -> None:
    """The other half of the property: the chain must refuse where it should.

    A pipeline that passes everything is as useless as one that passes nothing, so the same
    candidate is run against the window cycles 2-10 consumed.
    """
    # Seed the spent window the way the project's own history spent it.
    spent_draft = draft_from(
        candidate(),
        hypothesis_id="H-HISTORY",
        search_cardinality=40,
        windows=(
            DataWindow(
                dataset=SPENT_DATASET,
                snapshot="2026-08-18",
                role=DataRole.VALIDATION,
                start=date(2016, 1, 4),
                end=date(2026, 8, 14),
            ),
        ),
        available_observations=2669,
    )
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            spent_draft, now=NOW, critique=clean_critique("H-HISTORY", 1, series())
        )

    with database.transaction() as session:
        consumption = [
            HypothesisRegistry(session).window_consumption(
                SPENT_DATASET, date(2016, 1, 4), date(2026, 8, 14)
            )
        ]
    assert consumption[0].status == "EXHAUSTED"

    on_spent_data = candidate(
        candidate_id="C-2", required_datasets=(SPENT_DATASET,)
    )
    ok, reason = admissible(on_spent_data, consumption=consumption)
    assert not ok
    assert "exploratory" in reason

    # And registration refuses it outright as contaminated, not merely as low-value.
    contaminated = draft_from(
        candidate(),
        hypothesis_id="H-PIPE-2",
        materially_different="a different macro series on the same equity window",
        windows=(
            DataWindow(
                dataset=SPENT_DATASET,
                snapshot="2026-08-19",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(2016, 1, 4),
                end=date(2026, 8, 14),
            ),
        ),
        available_observations=2669,
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(
            contaminated, now=NOW, critique=clean_critique("H-PIPE-2", 1, series())
        )
    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW


def test_a_critic_objection_stops_the_chain_before_anything_is_frozen(
    database: Database,
) -> None:
    """The gate order matters: a blocked critique means there is no registration to compile."""
    blocking = DeterministicCritic(
        [
            Objection(
                check="hidden-beta",
                severity=Severity.BLOCKING,
                finding="alpha is t=0.05 against a 2.90 bar",
                evidence={"alpha_t": "0.05"},
            )
        ]
    ).review("H-PIPE-1", 1, now=NOW)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(
            draft_from(candidate()), now=NOW, critique=blocking
        )
    assert error.value.reason is RefusalReason.CRITIQUE_BLOCKS

    with database.transaction() as session:
        assert HypothesisRegistry(session).get("H-PIPE-1", 1) is None


def test_the_lifecycle_and_the_evidence_panel_agree_with_the_registry(
    database: Database,
) -> None:
    """The director's view and the dashboard's must come from the same durable state."""
    with database.transaction() as session:
        director = ResearchDirector(session)
        director.open_task(
            ResearchTask(
                task_id="T-PIPE-1",
                state=TaskState.PROPOSED,
                question=candidate().question,
                family_id="macro-term-structure",
                created_at=NOW,
                updated_at=NOW,
            ),
            actor="director",
            reason="candidate admitted",
        )
        director.advance(
            "T-PIPE-1", TaskState.CRITIQUE, actor="director", reason="to critique", now=NOW
        )
        director.attach_hypothesis("T-PIPE-1", "H-PIPE-1", 1)

    with database.transaction() as session:
        task = ResearchDirector(session).get("T-PIPE-1")
    assert task is not None
    assert task.hypothesis_id == "H-PIPE-1"
    assert task.state is TaskState.CRITIQUE

    # One forward day against a thirty-day window, whatever the research volume is.
    panel = EvidencePanel(
        forward_days=1,
        forward_trades=0,
        research_cycles=17,
        hypotheses_registered=1,
        hypotheses_refuted=0,
        hypotheses_underpowered=0,
        hypotheses_survived=1,
    )
    assert not panel.qualified
    assert panel.qualification_fraction == "1 of 30 days, 0 of 30 trades"
