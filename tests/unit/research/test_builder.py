"""What the compiler must refuse to produce, and what a result must earn before it counts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantbot.research.builder import (
    GENERALITY_PROBE,
    REQUIRED_PROBES,
    CompilationRefused,
    ExperimentOutcome,
    ExperimentPlan,
    OutcomeVerdict,
    compile_experiment,
    select_test,
)
from quantbot.research.critic import CriticVerdict, Critique
from quantbot.research.manifest import DatasetSnapshot, ExecutionPath, StatisticalTest
from quantbot.research.power import (
    ComparisonStructure,
    DependenceAssumptions,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    PowerOverride,
    assess,
)
from quantbot.research.registry import DataRole, DataWindow, HypothesisDraft, Registration

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def effect(**overrides: object) -> EffectSpecification:
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": Decimal("1.0"),
        "minimum_practical": Decimal("0.5"),
        "justification": "Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
        "comparison": ComparisonStructure.PAIRED,
        "economics": EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def draft(**overrides: object) -> HypothesisDraft:
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-001",
        "family_id": "trend-following",
        "question": "Does a 200-day trend filter on SPY beat buy-and-hold on Sharpe?",
        "prediction": "Sharpe is higher by at least 0.10.",
        "null_hypothesis": "The Sharpe difference is zero.",
        "falsified_if": "The paired difference fails to clear the luck bar.",
        "universe": ("SPY",),
        "features": ("close_sma_200",),
        "target": "next-session excess return",
        "windows": (
            DataWindow(
                dataset="sip-us-equities-daily",
                snapshot="2026-08-18",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(2016, 1, 4),
                end=date(2026, 8, 18),
            ),
        ),
        "primary_estimand": "annualised Sharpe difference against SPY buy-and-hold",
        "effect": effect(),
        "available_observations": 2669,
        "confounders": ("regime dependence",),
        "proposed_by": "claude-opus-5",
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def registration(**overrides: object) -> Registration:
    candidate = draft(**overrides)
    override = (
        PowerOverride(authorized_by="hutch", reason="fixture")
        if candidate.effect.expected < Decimal("0.9")
        else None
    )
    return Registration(
        draft=candidate,
        registered_at=NOW,
        cumulative_trials=69,
        power=assess(
            hypothesis_id=candidate.hypothesis_id,
            version=candidate.version,
            stage="REGISTRATION",
            specification=candidate.effect,
            observations_available=candidate.available_observations,
            cumulative_trials=69,
            assessed_at=NOW,
            override=override,
        ),
        critique=Critique(
            hypothesis_id=candidate.hypothesis_id,
            hypothesis_version=candidate.version,
            critic="deterministic",
            verdict=CriticVerdict.PROCEED,
            reasons=("no mechanical check produced an objection",),
            produced_at=NOW,
        ),
    )


def snapshot(**overrides: object) -> DatasetSnapshot:
    fields: dict[str, object] = {
        "dataset": "sip-us-equities-daily",
        "snapshot": "2026-08-18",
        "role": "PROTECTED_EVALUATION",
        "start": date(2016, 1, 4),
        "end": date(2026, 8, 18),
        "content_hash": "bars-hash",
        "observations": 2669,
        "provider": "alpaca",
        "retrieved_at": NOW,
        "earliest_available_at": NOW,
        "transformation_version": "adjust-v1",
    }
    fields.update(overrides)
    return DatasetSnapshot(**fields)


def outcome(**overrides: object) -> ExperimentOutcome:
    supplied = overrides.pop("plan", None)
    plan = (
        supplied
        if isinstance(supplied, ExperimentPlan)
        else compile_experiment(registration(), datasets=[snapshot()])
    )
    fields: dict[str, object] = {
        "plan": plan,
        "verdict": OutcomeVerdict.SURVIVED,
        "effect": Decimal("1.02"),
        "test_statistic": Decimal("3.4"),
        "probes_run": plan.probes,
        "detail": "cleared the bar on every probe",
        "completed_at": NOW,
    }
    fields.update(overrides)
    return ExperimentOutcome(**fields)


def test_the_statistic_comes_from_the_declared_dependence_structure() -> None:
    """Cycle 11's error: two Sharpes of the same asset compared as independent samples."""
    assert (
        select_test(Estimand.SHARPE, ComparisonStructure.PAIRED)
        is StatisticalTest.JOBSON_KORKIE_MEMMEL
    )
    assert select_test(Estimand.SHARPE, ComparisonStructure.UNPAIRED) is StatisticalTest.WELCH_T
    assert (
        select_test(Estimand.MEAN_DIFFERENCE, ComparisonStructure.PAIRED)
        is StatisticalTest.PAIRED_T
    )
    assert (
        select_test(Estimand.HIT_RATE, ComparisonStructure.SINGLE_SAMPLE)
        is StatisticalTest.BINOMIAL
    )
    assert (
        select_test(Estimand.INFORMATION_COEFFICIENT, ComparisonStructure.SINGLE_SAMPLE)
        is StatisticalTest.SPEARMAN
    )


def test_a_path_dependent_quantity_gets_a_bootstrap_not_a_t_test() -> None:
    """Cycle 12: the bootstrap turned an apparent doubling into a CI spanning zero."""
    assert (
        select_test(Estimand.SHARPE, ComparisonStructure.PAIRED, path_dependent=True)
        is StatisticalTest.STATIONARY_BOOTSTRAP
    )


def test_a_structure_with_no_valid_test_is_refused_rather_than_defaulted() -> None:
    """Defaulting is what produced the wrong test in the first place."""
    with pytest.raises(CompilationRefused) as error:
        select_test(Estimand.HIT_RATE, ComparisonStructure.PAIRED)
    assert error.value.reason == "NO_VALID_TEST"
    with pytest.raises(CompilationRefused):
        select_test(Estimand.INFORMATION_COEFFICIENT, ComparisonStructure.UNPAIRED)


def test_the_adversarial_probes_are_compiled_in_not_added_after_a_good_result() -> None:
    plan = compile_experiment(registration(), datasets=[snapshot()])
    assert set(REQUIRED_PROBES) <= set(plan.probes)
    # One instrument implies nothing about generality, so that probe is not demanded.
    assert GENERALITY_PROBE not in plan.probes

    wide = compile_experiment(
        registration(universe=("XLK", "XLF", "XLE"), materially_different="a breadth test"),
        datasets=[snapshot()],
    )
    assert GENERALITY_PROBE in wide.probes


def test_a_plan_that_drops_a_required_probe_cannot_be_built() -> None:
    """The probes are part of the plan's validity, not a convention a runner may skip."""
    plan = compile_experiment(registration(), datasets=[snapshot()])
    fields = plan.model_dump()
    fields["probes"] = ("cost-ladder", "regime-split", "hidden-beta")
    with pytest.raises(ValueError, match="must run shifted-feature-probe"):
        ExperimentPlan(**fields)


def test_a_dataset_the_registration_declared_must_actually_be_supplied() -> None:
    with pytest.raises(CompilationRefused) as error:
        compile_experiment(
            registration(), datasets=[snapshot(dataset="some-other-dataset")]
        )
    assert error.value.reason == "MISSING_DATASET"
    assert "sip-us-equities-daily" in error.value.detail


def test_a_dataset_without_an_availability_timestamp_is_refused() -> None:
    """Without it the run cannot show it avoided look-ahead, only assert it."""
    with pytest.raises(CompilationRefused) as error:
        compile_experiment(registration(), datasets=[snapshot(earliest_available_at=None)])
    assert error.value.reason == "NO_AVAILABILITY"


def test_an_uneconomic_hypothesis_has_nothing_worth_running() -> None:
    uneconomic = registration(
        effect=effect(
            expected=Decimal("2.0"),
            minimum_practical=Decimal("0.2"),
            economics=EconomicProfile(
                annual_rebalances=252,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("5"),
            ),
        )
    )
    with pytest.raises(CompilationRefused) as error:
        compile_experiment(uneconomic, datasets=[snapshot()])
    assert error.value.reason == "UNECONOMIC"


def test_the_plan_records_the_dependence_assumptions_it_selected_the_test_from() -> None:
    plan = compile_experiment(
        registration(
            effect=effect(
                dependence=DependenceAssumptions(
                    sampling="OVERLAPPING", horizon_observations=21
                )
            )
        ),
        datasets=[snapshot()],
    )
    assert plan.statistics.variance_inflation == Decimal("21")
    assert "horizon observations: 21" in plan.statistics.assumptions
    assert "dependence: PAIRED" in plan.statistics.assumptions


def test_a_result_that_skipped_its_probes_cannot_be_read_at_all() -> None:
    with pytest.raises(ValueError, match="did not run its adversarial probes"):
        outcome(probes_run=("hidden-beta",))
    # Including a refutation: a null from an unprobed run is not a null.
    with pytest.raises(ValueError, match="did not run its adversarial probes"):
        outcome(verdict=OutcomeVerdict.REFUTED, probes_run=())


def test_a_survivor_cannot_have_a_failing_probe() -> None:
    with pytest.raises(ValueError, match="survivor cannot have failing probes"):
        outcome(probes_failed=("hidden-beta",))
    # The same probe failing is a perfectly recordable refutation.
    assert (
        outcome(verdict=OutcomeVerdict.REFUTED, probes_failed=("hidden-beta",)).verdict
        is OutcomeVerdict.REFUTED
    )


def test_a_standalone_study_cannot_produce_a_survivor() -> None:
    """Commit f77ea08: reproducible, deterministic, and not about this system.

    A study measured a halt policy against SPY-200-day returns while the engine runs the
    momentum rotation, and concluded $252.43 against $126.80. Through the real engine the same
    change scored Sharpe 0.21 against 0.36.
    """
    standalone = compile_experiment(
        registration(),
        datasets=[snapshot()],
        execution_path=ExecutionPath.RESEARCH_SCRIPT,
    )
    with pytest.raises(ValueError, match="production evaluation path"):
        outcome(plan=standalone)

    # It can still say what it found, labelled as what it is.
    model = outcome(plan=standalone, verdict=OutcomeVerdict.INCONCLUSIVE)
    assert not model.citable

    assert outcome().citable


def test_a_survivor_must_clear_the_bar_frozen_for_its_own_trial_count() -> None:
    with pytest.raises(ValueError, match="clear its own frozen bar"):
        outcome(test_statistic=Decimal("1.5"))
    with pytest.raises(ValueError, match="report the statistic"):
        outcome(test_statistic=None)


def test_a_failed_run_is_recordable_rather_than_discarded() -> None:
    """A failure is not an absence, and #6 needs to tell them apart."""
    failed = outcome(
        verdict=OutcomeVerdict.FAILED,
        probes_run=(),
        test_statistic=None,
        effect=None,
        detail="the sandbox terminated the run on its memory ceiling",
    )
    assert failed.verdict is OutcomeVerdict.FAILED
    assert not failed.citable


def test_every_outcome_carries_its_minimum_detectable_effect() -> None:
    """So a null reads "no effect larger than X" rather than "no effect found"."""
    null = outcome(verdict=OutcomeVerdict.REFUTED, test_statistic=Decimal("0.4"))
    # The whole SIP history at 69 cumulative trials cannot separate a Sharpe below 0.894 from
    # zero, so "no effect found" would be a much weaker and more misleading statement.
    assert null.minimum_detectable_effect == Decimal("0.894174")
    assert not null.citable
