"""Running a registered validation campaign: measurements in, derived verdicts out (#159).

The third #159 slice. #215 built the campaign model and its grader; #216 made its selection
freedom cost something; this wires it to `ExperimentRunner` so a real run can be graded by it.

Two properties carry the weight here, and both are about what the wiring must *not* make
possible.

**The campaign is read from the frozen registration, never accepted from the caller.** There is
no parameter through which campaign B could be offered in place of registered campaign A once
results are visible, and `run()` separately refuses a registration whose `content_hash` does not
match the plan -- the campaign lives inside that hash, so the substitution is closed twice.

**A hostile gate subtracts and never adds.** The campaign is consulted only where the primary
result already stands. It can block a survivor; it cannot turn a refuted, underpowered or
unreadable primary into one. Applying it symmetrically would make a favourable set of scenarios
a second route to support, which is the shape of defect this project keeps closing.

Nothing here is evidence about a market. Every measurement is a synthetic fixture.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.builder import OutcomeVerdict, compile_experiment
from quantbot.research.manifest import ExperimentPeriod
from quantbot.research.registry import HypothesisRegistry
from quantbot.research.runner import (
    ExperimentRunError,
    ExperimentRunner,
    Measured,
    ScenarioMeasured,
)
from quantbot.research.validation_campaign import (
    CampaignVerdict,
    ConstraintOperator,
    RegimeConstraint,
    ResamplingMethod,
    ScenarioEvidenceSource,
    ScenarioKind,
    ScenarioSelectionMethod,
    ScenarioVerdict,
    ValidationCampaign,
    ValidationScenario,
)
from quantbot.storage import Database
from tests.unit.research.test_runner import (
    COSTS,
    DATASET,
    DESIGN,
    NOW,
    critique,
    draft,
    provenance,
    snapshot,
)

ELIGIBLE = ExperimentPeriod(name="eligible", start=date(2016, 1, 4), end=date(2026, 8, 14))


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database.create(tmp_path / "campaign-runner.db")
    yield db
    db.close()


@pytest.fixture
def manifests(tmp_path) -> Path:
    return tmp_path / "manifests"


def scenario(**overrides: object) -> ValidationScenario:
    fields: dict[str, object] = {
        "kind": ScenarioKind.EXECUTION_STRESS,
        "dataset": DATASET,
        "data_role": "PROTECTED_EVALUATION",
        "period": ExperimentPeriod(name="s", start=date(2018, 1, 2), end=date(2019, 12, 31)),
        "required": True,
        "adverse": True,
        "minimum_trades": 30,
        "minimum_effective_observations": 200,
    }
    fields.update(overrides)
    return ValidationScenario(**fields)


GENERAL = scenario(
    kind=ScenarioKind.GENERALIZATION,
    adverse=False,
    period=ExperimentPeriod(name="g", start=date(2020, 1, 2), end=date(2021, 12, 31)),
)


def campaign(**overrides: object) -> ValidationCampaign:
    fields: dict[str, object] = {
        "selection_method": ScenarioSelectionMethod.DECLARED,
        "selection_policy_version": "declared-v1",
        "eligible_period": ELIGIBLE,
        "regime_classifier_version": "regime-v1",
        "stress_policy_version": "stress-v1",
        "neighborhood_grid": {"trend_period": (150, 200, 250)},
        "scenarios": (scenario(), GENERAL),
        "maximum_underpowered_fraction": Decimal("0.5"),
    }
    fields.update(overrides)
    return ValidationCampaign(**fields)


def measured_scenario(target: ValidationScenario, **overrides: object) -> ScenarioMeasured:
    """A comfortably resolved, comfortably active result for `target`."""
    fields: dict[str, object] = {
        "scenario_id": target.scenario_id,
        "effect": Decimal("1.0"),
        "test_statistic": Decimal("9.0"),
        "trades": 60,
        "effective_observations": 400,
    }
    fields.update(overrides)
    return ScenarioMeasured(**fields)


def run_campaign(
    database: Database,
    manifests: Path,
    *,
    case: str,
    subject: ValidationCampaign | None,
    scenarios: tuple[ScenarioMeasured, ...] = (),
    statistic: Decimal = Decimal("9.0"),
    probes_failed: tuple[str, ...] = (),
):
    """Freeze a campaign, register it, run it, and return the graded outcome.

    A distinct hypothesis id and dataset per case: a registration is frozen once and a protected
    window is claimed by one hypothesis, so sharing either would make a case fail on contention
    rather than on the property it names.
    """
    dataset = f"{DATASET}-{case}"
    bound = (
        None
        if subject is None
        else subject.model_copy(
            update={
                "scenarios": tuple(
                    item.model_copy(update={"dataset": dataset}) for item in subject.scenarios
                )
            }
        )
    )
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        if bound is not None:
            registry.freeze_validation_campaign(bound, now=NOW)
        registration = registry.register(
            draft(
                hypothesis_id=f"H-RUN-{case}",
                materially_different=f"runner fixture {case}",
                validation_campaign=bound,
            ),
            now=NOW,
            critique=critique(f"H-RUN-{case}"),
        )
        plan = compile_experiment(registration, datasets=[snapshot()])
        code, environment = provenance()
        return ExperimentRunner(registry, manifests=manifests).run(
            plan,
            registration,
            lambda _plan, _access: Measured(
                effect=Decimal("2.5"),
                test_statistic=statistic,
                probes_run=plan.probes,
                probes_failed=probes_failed,
                scenario_measurements=scenarios,
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        ).outcome


def rebind(subject: ValidationCampaign, case: str) -> ValidationCampaign:
    """The campaign as `run_campaign` will have frozen it, so scenario ids line up."""
    dataset = f"{DATASET}-{case}"
    return subject.model_copy(
        update={
            "scenarios": tuple(
                item.model_copy(update={"dataset": dataset}) for item in subject.scenarios
            )
        }
    )


# --- the happy path, and the control for everything below ------------------------------------


def test_a_complete_valid_campaign_is_graded_and_recorded(
    database: Database, manifests: Path
) -> None:
    """Measurements in, mechanically derived verdicts out, attached to the outcome."""
    bound = rebind(campaign(), "ok")
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="ok",
        subject=campaign(),
        scenarios=(measured_scenario(stress), measured_scenario(general)),
    )

    assert outcome.verdict is OutcomeVerdict.SURVIVED
    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.verdict is CampaignVerdict.SURVIVED
    assert outcome.validation_outcome.campaign_id == bound.campaign_id
    assert {item.verdict for item in outcome.validation_outcome.scenarios} == {
        ScenarioVerdict.SURVIVED
    }


def test_a_registration_without_a_campaign_is_unchanged(
    database: Database, manifests: Path
) -> None:
    """Existing behaviour is untouched: no campaign, no campaign outcome, same verdict."""
    outcome = run_campaign(database, manifests, case="plain", subject=None)

    assert outcome.verdict is OutcomeVerdict.SURVIVED
    assert outcome.validation_outcome is None


# --- #212: the wiring must not become a route around signed grading ---------------------------


def test_a_strong_negative_scenario_cannot_support_through_the_runner(
    database: Database, manifests: Path
) -> None:
    """**The #212 invariant, asserted through the integration rather than the unit.**

    t=-9 against a positive claim is evidence against it. The scenario is refuted, and because
    it is required, the campaign is refuted, and because the campaign is a gate the primary
    survivor is blocked. No new `abs()` anywhere on the path.
    """
    bound = rebind(campaign(), "neg")
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="neg",
        subject=campaign(),
        scenarios=(
            measured_scenario(stress, test_statistic=Decimal("-9.0"), effect=Decimal("-1.0")),
            measured_scenario(general),
        ),
    )

    assert outcome.validation_outcome is not None
    refuted = next(
        item
        for item in outcome.validation_outcome.scenarios
        if item.scenario_id == stress.scenario_id
    )
    assert refuted.verdict is ScenarioVerdict.REFUTED
    assert "opposite to the registered claim" in refuted.detail
    assert outcome.verdict is OutcomeVerdict.REFUTED


def test_a_strong_negative_primary_stays_refuted_whatever_the_campaign_says(
    database: Database, manifests: Path
) -> None:
    """The primary path keeps its own signed authority. A campaign full of survivors cannot
    rescue a primary measured at t=-9."""
    bound = rebind(campaign(), "negprim")
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="negprim",
        subject=campaign(),
        statistic=Decimal("-9.0"),
        scenarios=(measured_scenario(stress), measured_scenario(general)),
    )

    assert outcome.verdict is OutcomeVerdict.REFUTED
    assert "opposite to the registered claim" in outcome.detail
    # The campaign was still graded and recorded -- it just cannot promote.
    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.verdict is CampaignVerdict.SURVIVED


# --- the gate subtracts, never adds -----------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "overrides", "expected"),
    [
        ("weak", {"test_statistic": Decimal("0.4")}, OutcomeVerdict.REFUTED),
        ("thin", {"trades": 1}, OutcomeVerdict.UNDERPOWERED),
        ("nostat", {"test_statistic": None}, OutcomeVerdict.INCONCLUSIVE),
    ],
)
def test_a_required_adverse_failure_blocks_a_surviving_primary(
    database: Database,
    manifests: Path,
    case: str,
    overrides: dict[str, object],
    expected: OutcomeVerdict,
) -> None:
    """The primary is measured at t=9 in every case, and the generalization scenario passes.

    So a wiring that scored the campaign on an average, or consulted it only when convenient,
    would report SURVIVED for all three.
    """
    bound = rebind(campaign(), case)
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case=case,
        subject=campaign(),
        scenarios=(measured_scenario(stress, **overrides), measured_scenario(general)),
    )

    assert outcome.verdict is expected
    assert outcome.verdict is not OutcomeVerdict.SURVIVED
    assert "validation campaign" in outcome.detail


def test_a_mislabeled_regime_blocks(database: Database, manifests: Path) -> None:
    """A stress measured in a window that is not the regime it claims answers another question."""
    regime = scenario(
        kind=ScenarioKind.REGIME,
        regime_constraints=(
            RegimeConstraint(
                metric="realised_volatility",
                operator=ConstraintOperator.AT_LEAST,
                threshold=Decimal("0.25"),
            ),
        ),
    )
    bound = rebind(campaign(scenarios=(regime, GENERAL)), "mislabel")
    frozen_regime, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="mislabel",
        subject=campaign(scenarios=(regime, GENERAL)),
        scenarios=(
            measured_scenario(
                frozen_regime, regime_metrics={"realised_volatility": Decimal("0.05")}
            ),
            measured_scenario(general),
        ),
    )

    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


def test_an_unverified_regime_blocks(database: Database, manifests: Path) -> None:
    """A diagnostic that was never reported is not agreement."""
    regime = scenario(
        kind=ScenarioKind.REGIME,
        regime_constraints=(
            RegimeConstraint(
                metric="realised_volatility",
                operator=ConstraintOperator.AT_LEAST,
                threshold=Decimal("0.25"),
            ),
        ),
    )
    bound = rebind(campaign(scenarios=(regime, GENERAL)), "unverified")
    frozen_regime, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="unverified",
        subject=campaign(scenarios=(regime, GENERAL)),
        scenarios=(measured_scenario(frozen_regime), measured_scenario(general)),
    )

    assert outcome.validation_outcome is not None
    unverified = next(
        item
        for item in outcome.validation_outcome.scenarios
        if item.scenario_id == frozen_regime.scenario_id
    )
    assert unverified.verdict is ScenarioVerdict.UNVERIFIED
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


# --- scenario result integrity ------------------------------------------------------------------


def test_a_missing_required_measurement_blocks(database: Database, manifests: Path) -> None:
    """Reporting nothing for a mandatory adverse scenario is the cheapest way to delete it."""
    bound = rebind(campaign(), "missing")
    _stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="missing",
        subject=campaign(),
        scenarios=(measured_scenario(general),),
    )

    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


def test_a_duplicate_measurement_blocks(database: Database, manifests: Path) -> None:
    """Two answers for one frozen scenario is ambiguity, not a choice of which to believe."""
    bound = rebind(campaign(), "dupe")
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="dupe",
        subject=campaign(),
        scenarios=(
            measured_scenario(stress),
            measured_scenario(stress, test_statistic=Decimal("-9.0")),
            measured_scenario(general),
        ),
    )

    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


def test_an_unknown_scenario_measurement_blocks(database: Database, manifests: Path) -> None:
    """Grading only what was reported would let a campaign be answered with a different one."""
    bound = rebind(campaign(), "unknown")
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="unknown",
        subject=campaign(),
        scenarios=(
            measured_scenario(stress),
            measured_scenario(general),
            ScenarioMeasured(
                scenario_id="c" * 64,
                trades=99,
                effective_observations=999,
                test_statistic=Decimal("9.0"),
            ),
        ),
    )

    assert outcome.validation_outcome is not None
    assert any(
        "unknown scenario ids" in defect
        for defect in outcome.validation_outcome.integrity_defects
    )
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


def test_a_result_for_one_scenario_cannot_satisfy_another(
    database: Database, manifests: Path
) -> None:
    """Measurements are keyed by frozen `scenario_id`, so answering the easy scenario twice
    leaves the hard one unanswered rather than covered."""
    bound = rebind(campaign(), "swap")
    stress, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="swap",
        subject=campaign(),
        scenarios=(measured_scenario(general), measured_scenario(general)),
    )

    assert outcome.validation_outcome is not None
    unanswered = next(
        item
        for item in outcome.validation_outcome.scenarios
        if item.scenario_id == stress.scenario_id
    )
    assert unanswered.verdict is ScenarioVerdict.UNASSESSED
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


def test_an_extra_favourable_result_cannot_wash_out_a_required_failure(
    database: Database, manifests: Path
) -> None:
    """Three passing scenarios against one failing mandatory one is still a failure.

    A wiring that averaged, counted, or took a majority would report SURVIVED here.
    """
    extras = tuple(
        scenario(
            kind=ScenarioKind.GENERALIZATION,
            adverse=False,
            required=False,
            period=ExperimentPeriod(
                name=f"extra-{index}",
                start=date(2020 + index, 1, 2),
                end=date(2020 + index, 12, 30),
            ),
        )
        for index in range(3)
    )
    subject = campaign(scenarios=(scenario(), *extras))
    bound = rebind(subject, "wash")
    stress, *rest = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="wash",
        subject=subject,
        scenarios=(
            measured_scenario(stress, test_statistic=Decimal("0.1")),
            *(measured_scenario(item) for item in rest),
        ),
    )

    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.verdict is CampaignVerdict.REFUTED
    assert outcome.verdict is OutcomeVerdict.REFUTED


# --- authority boundary ---------------------------------------------------------------------


def test_a_measurement_cannot_carry_a_scenario_or_campaign_verdict() -> None:
    """Workers report numbers. `ScenarioMeasured` has no verdict field to fill in, and the
    runner exposes no way to hand one in -- so `SURVIVED` is never in the gift of the code
    being judged."""
    assert "verdict" not in ScenarioMeasured.model_fields
    # `Measured` is a dataclass rather than a model, so its fields are read differently.
    measured_fields = {item.name for item in dataclasses.fields(Measured)}
    assert "validation_outcome" not in measured_fields
    assert "campaign_verdict" not in measured_fields
    assert "scenario_measurements" in measured_fields, "numbers are supplied; verdicts are not"
    with pytest.raises(ValueError):
        ScenarioMeasured(
            scenario_id="a" * 64,
            trades=1,
            effective_observations=1,
            verdict="SURVIVED",  # type: ignore[call-arg]
        )


def test_scenario_results_for_a_registration_without_a_campaign_are_refused(
    database: Database, manifests: Path
) -> None:
    """Silently dropping them would let a worker believe hostile validation had been graded
    when nothing was."""
    bound = rebind(campaign(), "orphan")
    stress, _general = bound.scenarios

    with pytest.raises(ExperimentRunError, match="froze no validation campaign"):
        run_campaign(
            database,
            manifests,
            case="orphan",
            subject=None,
            scenarios=(measured_scenario(stress),),
        )


def test_a_caller_cannot_substitute_another_campaigns_results(
    database: Database, manifests: Path
) -> None:
    """**Campaign identity cannot be swapped after results are visible.**

    There is no parameter for it: the runner reads the campaign from the frozen registration.
    The nearest attempt a caller *can* make is to supply results keyed to a different campaign's
    scenarios, and every one of those is an unknown id -- so the registered scenarios go
    unanswered and the campaign refuses rather than being quietly answered by another.
    """
    other = rebind(
        campaign(
            scenarios=(scenario(minimum_trades=31), GENERAL),
            stress_policy_version="stress-v2",
        ),
        "sub",
    )
    registered = rebind(campaign(), "sub")
    assert other.campaign_id != registered.campaign_id

    outcome = run_campaign(
        database,
        manifests,
        case="sub",
        subject=campaign(),
        scenarios=tuple(measured_scenario(item) for item in other.scenarios),
    )

    assert outcome.validation_outcome is not None
    assert outcome.validation_outcome.campaign_id == registered.campaign_id
    assert any(
        "unknown scenario ids" in defect
        for defect in outcome.validation_outcome.integrity_defects
    )
    assert outcome.verdict is OutcomeVerdict.INCONCLUSIVE


def test_a_resampled_scenario_cannot_mint_fresh_evidence_through_the_runner(
    database: Database, manifests: Path
) -> None:
    """Simulated paths stay diagnostics all the way through the integration."""
    resampled = scenario(
        kind=ScenarioKind.RESAMPLED_PATH,
        evidence_source=ScenarioEvidenceSource.RESAMPLED_PATH,
        resampling_method=ResamplingMethod.STATIONARY_BLOCK_BOOTSTRAP,
    )
    subject = campaign(scenarios=(resampled, GENERAL))
    bound = rebind(subject, "resampled")
    frozen_resampled, general = bound.scenarios

    outcome = run_campaign(
        database,
        manifests,
        case="resampled",
        subject=subject,
        scenarios=(measured_scenario(frozen_resampled), measured_scenario(general)),
    )

    assert outcome.validation_outcome is not None
    simulated = next(
        item
        for item in outcome.validation_outcome.scenarios
        if item.scenario_id == frozen_resampled.scenario_id
    )
    observed = next(
        item
        for item in outcome.validation_outcome.scenarios
        if item.scenario_id == general.scenario_id
    )
    assert simulated.verdict is ScenarioVerdict.SURVIVED
    assert simulated.fresh_evidence is False
    assert observed.fresh_evidence is True
