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
import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.builder import OutcomeVerdict, compile_experiment
from quantbot.research.manifest import ExperimentPeriod
from quantbot.research.registry import HypothesisRegistry, RegistrationRefused
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
from quantbot.storage.schema import search_runs
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

    **Scenarios live on the dataset the plan actually supplies.** An earlier version of this
    fixture put them on `f"{DATASET}-{case}"` while `snapshot()` supplied `DATASET`, so every
    campaign here was frozen over data the run never read -- the fixtures were demonstrating the
    defect Finding 4 exists to close, and the tests passed anyway because nothing checked. Each
    case gets its own `database`, so there is no window contention to route around.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        if subject is not None:
            registry.freeze_validation_campaign(subject, now=NOW)
        registration = registry.register(
            draft(
                hypothesis_id=f"H-RUN-{case}",
                materially_different=f"runner fixture {case}",
                validation_campaign=subject,
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


# --- the happy path, and the control for everything below ------------------------------------


def test_a_complete_valid_campaign_is_graded_and_recorded(
    database: Database, manifests: Path
) -> None:
    """Measurements in, mechanically derived verdicts out, attached to the outcome."""
    bound = campaign()
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
    bound = campaign()
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
    bound = campaign()
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
    bound = campaign()
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
    bound = campaign(scenarios=(regime, GENERAL))
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
    bound = campaign(scenarios=(regime, GENERAL))
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
    bound = campaign()
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
    bound = campaign()
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
    bound = campaign()
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
    bound = campaign()
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
    bound = subject
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
    bound = campaign()
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
    other = campaign(
        scenarios=(scenario(minimum_trades=31), GENERAL),
        stress_policy_version="stress-v2",
    )
    registered = campaign()
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
    bound = subject
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


# --- Finding 1: the campaign has to survive into the durable bundle ---------------------------
#
# The gate can downgrade SURVIVED -> REFUTED. If the manifest records only the downgraded primary
# verdict, the bundle says a result was refuted and cannot say by what -- no campaign id, no
# scenario verdicts, no statistics, no integrity defects. A gate whose reasoning is not written
# down cannot be audited, and this project's whole claim is that its conclusions can be.


def run_result(
    database: Database,
    manifests: Path,
    *,
    case: str,
    subject: ValidationCampaign | None,
    scenarios: tuple[ScenarioMeasured, ...] = (),
    statistic: Decimal = Decimal("9.0"),
):
    """As `run_campaign`, but returns the whole `RunResult` so the manifest can be read."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        if subject is not None:
            registry.freeze_validation_campaign(subject, now=NOW)
        registration = registry.register(
            draft(
                hypothesis_id=f"H-RUN-{case}",
                materially_different=f"runner fixture {case}",
                validation_campaign=subject,
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
                scenario_measurements=scenarios,
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )


def test_a_surviving_campaign_is_durable_in_the_manifest(
    database: Database, manifests: Path
) -> None:
    """Aggregate and per-scenario entries both reach the bundle."""
    subject = campaign()
    stress, general = subject.scenarios

    result = run_result(
        database,
        manifests,
        case="durable-ok",
        subject=subject,
        scenarios=(measured_scenario(stress), measured_scenario(general)),
    )

    aggregate = result.manifest.results["validation:campaign"]
    assert aggregate["campaign_id"] == subject.campaign_id
    assert aggregate["verdict"] == "SURVIVED"
    assert aggregate["underpowered_count"] == "0"
    assert aggregate["integrity_defects"] == ""

    entry = result.manifest.results[f"validation:scenario:{stress.scenario_id}"]
    assert entry["verdict"] == "SURVIVED"
    assert entry["test_statistic"] == "9.0"
    assert entry["trades"] == "60"
    assert entry["effective_observations"] == "400"
    assert entry["fresh_evidence"] == "true"


def test_a_refuted_campaign_records_what_refuted_it(
    database: Database, manifests: Path
) -> None:
    """**The case the durability gap hid.** The primary is downgraded to REFUTED, and the bundle
    has to be able to say which scenario did it and on what number."""
    subject = campaign()
    stress, general = subject.scenarios

    result = run_result(
        database,
        manifests,
        case="durable-refuted",
        subject=subject,
        scenarios=(
            measured_scenario(stress, test_statistic=Decimal("-9.0"), effect=Decimal("-1.0")),
            measured_scenario(general),
        ),
    )

    assert result.outcome.verdict is OutcomeVerdict.REFUTED
    assert result.manifest.results["validation:campaign"]["verdict"] == "REFUTED"
    entry = result.manifest.results[f"validation:scenario:{stress.scenario_id}"]
    assert entry["verdict"] == "REFUTED"
    assert entry["test_statistic"] == "-9.0"
    assert "opposite to the registered claim" in entry["detail"]
    # And the scenario that passed is still there, so the record shows the contrast.
    assert result.manifest.results[
        f"validation:scenario:{general.scenario_id}"
    ]["verdict"] == "SURVIVED"


def test_integrity_defects_are_durable(database: Database, manifests: Path) -> None:
    """Missing, duplicate and unknown results are all recorded rather than inferred later."""
    subject = campaign()
    stress, general = subject.scenarios

    result = run_result(
        database,
        manifests,
        case="durable-defects",
        subject=subject,
        scenarios=(
            measured_scenario(general),
            measured_scenario(general),
            ScenarioMeasured(
                scenario_id="d" * 64, trades=1, effective_observations=1,
                test_statistic=Decimal("9.0"),
            ),
        ),
    )

    aggregate = result.manifest.results["validation:campaign"]
    assert aggregate["verdict"] == "INCONCLUSIVE"
    assert "unknown scenario ids" in aggregate["integrity_defects"]
    # The unanswered required scenario is present and visibly unanswered.
    assert result.manifest.results[
        f"validation:scenario:{stress.scenario_id}"
    ]["verdict"] == "UNASSESSED"
    assert result.manifest.results[
        f"validation:scenario:{general.scenario_id}"
    ]["verdict"] == "UNASSESSED"


def test_the_campaign_survives_manifest_serialisation(
    database: Database, manifests: Path
) -> None:
    """Written to disk and read back unchanged, so the bundle is self-contained."""
    subject = campaign()
    stress, general = subject.scenarios

    result = run_result(
        database,
        manifests,
        case="durable-reload",
        subject=subject,
        scenarios=(measured_scenario(stress), measured_scenario(general)),
    )

    written = json.loads(
        next(manifests.glob("*.json")).read_text(encoding="utf-8")
    )
    reloaded = written["results"]
    assert reloaded["validation:campaign"]["campaign_id"] == subject.campaign_id
    assert (
        reloaded[f"validation:scenario:{stress.scenario_id}"]
        == result.manifest.results[f"validation:scenario:{stress.scenario_id}"]
    )


def test_different_scenario_outcomes_produce_different_manifest_hashes(
    tmp_path: Path,
) -> None:
    """Two runs whose hostile validation came out differently are not the same experiment.

    **Both runs survive, at both levels.** An earlier version of this test used t=9 against
    t=-9, which also flipped the *primary* verdict -- so the hashes differed for a reason that
    had nothing to do with the campaign, and the test passed even with campaign entries removed
    from the manifest entirely. Mutation testing caught that, which is what it is for.

    Here the stress scenario clears its bar in both runs, so every other recorded field is
    identical and the hash can only differ because the scenario's own statistic entered it.
    """
    subject = campaign()
    stress, general = subject.scenarios
    hashes = []
    for label, stress_statistic in (("high", Decimal("9.0")), ("low", Decimal("4.0"))):
        database = Database.create(tmp_path / f"{label}.db")
        try:
            result = run_result(
                database,
                tmp_path / f"manifests-{label}",
                case="hash",
                subject=subject,
                scenarios=(
                    measured_scenario(stress, test_statistic=stress_statistic),
                    measured_scenario(general),
                ),
            )
            hashes.append(result.manifest.manifest_hash)
        finally:
            database.close()

    assert hashes[0] != hashes[1]


def test_a_non_campaign_run_writes_no_validation_keys(
    database: Database, manifests: Path
) -> None:
    """Historical bundles are unchanged: no campaign, no keys, so the hash is what it was."""
    result = run_result(database, manifests, case="durable-plain", subject=None)

    assert result.outcome.validation_outcome is None
    assert not [key for key in result.manifest.results if key.startswith("validation:")]


# --- Finding 2: a failed measurement must not erase the campaign -------------------------------


def test_a_failed_measurement_keeps_the_campaign_visible(
    database: Database, manifests: Path
) -> None:
    """A failed run still spent the protected handoff, so the campaign frozen against it is part
    of what happened. Every scenario stays visibly unanswered rather than absent -- 'nobody
    answered this' and 'this was never asked' are different facts."""
    subject = campaign()
    stress, general = subject.scenarios

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.freeze_validation_campaign(subject, now=NOW)
        registration = registry.register(
            draft(hypothesis_id="H-RUN-boom", validation_campaign=subject),
            now=NOW,
            critique=critique("H-RUN-boom"),
        )
        plan = compile_experiment(registration, datasets=[snapshot()])
        code, environment = provenance()

        def explode(_plan, _access):
            raise RuntimeError("the measurement fell over")

        result = ExperimentRunner(registry, manifests=manifests).run(
            plan, registration, explode, now=NOW, code=code, environment=environment,
            applied_costs=COSTS, design=DESIGN,
        )

    assert result.outcome.verdict is OutcomeVerdict.FAILED
    assert result.outcome.validation_outcome is not None
    assert result.outcome.validation_outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert {item.verdict for item in result.outcome.validation_outcome.scenarios} == {
        ScenarioVerdict.UNASSESSED
    }
    # And it is durable, not only in memory.
    assert result.manifest.results["validation:campaign"]["verdict"] == "INCONCLUSIVE"
    assert result.manifest.results[
        f"validation:scenario:{stress.scenario_id}"
    ]["verdict"] == "UNASSESSED"
    assert f"validation:scenario:{general.scenario_id}" in result.manifest.results


def test_a_failed_measurement_without_a_campaign_records_none(
    database: Database, manifests: Path
) -> None:
    """The control: no campaign frozen, nothing invented to fill the space."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registration = registry.register(
            draft(hypothesis_id="H-RUN-boom2"), now=NOW, critique=critique("H-RUN-boom2")
        )
        plan = compile_experiment(registration, datasets=[snapshot()])
        code, environment = provenance()

        def explode(_plan, _access):
            raise RuntimeError("the measurement fell over")

        result = ExperimentRunner(registry, manifests=manifests).run(
            plan, registration, explode, now=NOW, code=code, environment=environment,
            applied_costs=COSTS, design=DESIGN,
        )

    assert result.outcome.verdict is OutcomeVerdict.FAILED
    assert result.outcome.validation_outcome is None
    assert not [key for key in result.manifest.results if key.startswith("validation:")]


# --- Finding 3: the persisted registration is the authority -------------------------------------
#
# `run()` compared the caller's registration against the caller's plan. Two consistent forgeries
# agree with each other perfectly, so the pair proved nothing about what the store holds.


def substitution_attempt(
    database: Database, manifests: Path, *, registered, forged, called: list[str]
):
    """Register `registered`, then try to run a plan compiled from `forged` instead."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        if registered is not None:
            registry.freeze_validation_campaign(registered, now=NOW)
        persisted = registry.register(
            draft(hypothesis_id="H-SUB", validation_campaign=registered),
            now=NOW,
            critique=critique("H-SUB"),
        )
        if forged is not None:
            registry.freeze_validation_campaign(forged, now=NOW)
        # Same hypothesis id and version, different campaign: exactly what the old check missed.
        forged_registration = persisted.model_copy(
            update={
                "draft": persisted.draft.model_copy(update={"validation_campaign": forged})
            }
        )
        plan = compile_experiment(forged_registration, datasets=[snapshot()])

        def record(_plan, _access):
            called.append("measured")
            return Measured(effect=Decimal("2.5"), test_statistic=Decimal("9.0"),
                            probes_run=plan.probes)

        code, environment = provenance()
        return ExperimentRunner(registry, manifests=manifests).run(
            plan, forged_registration, record, now=NOW, code=code, environment=environment,
            applied_costs=COSTS, design=DESIGN,
        )


def test_a_forged_registration_with_a_different_campaign_is_refused(
    database: Database, manifests: Path
) -> None:
    """**The substitution the previous check could not see.** Registration B carries a different
    campaign under the same id and version; its plan hash agrees with it perfectly; and the store
    still holds A. The store is asked, and the run is refused before anything is measured."""
    called: list[str] = []

    with pytest.raises(RegistrationRefused, match="is stored at"):
        substitution_attempt(
            database, manifests,
            registered=campaign(),
            forged=campaign(stress_policy_version="stress-v2"),
            called=called,
        )

    assert called == [], "the measurement must never run after a preflight refusal"


def test_a_forged_registration_with_no_campaign_cannot_replace_one(
    database: Database, manifests: Path
) -> None:
    """Dropping the campaign is a substitution too -- it would run the claim ungated."""
    called: list[str] = []

    with pytest.raises(RegistrationRefused, match="is stored at"):
        substitution_attempt(
            database, manifests, registered=campaign(), forged=None, called=called
        )

    assert called == []


def test_the_persisted_registration_runs_normally(
    database: Database, manifests: Path
) -> None:
    """The control. A check refusing every run would satisfy both cases above."""
    subject = campaign()
    stress, general = subject.scenarios

    outcome = run_campaign(
        database, manifests, case="persisted", subject=subject,
        scenarios=(measured_scenario(stress), measured_scenario(general)),
    )

    assert outcome.verdict is OutcomeVerdict.SURVIVED


def test_a_tampered_campaign_freeze_refuses_before_measurement(
    database: Database, manifests: Path
) -> None:
    """#216's row verification is re-run at execution time, not only at registration.

    A campaign whose ledger row has since been tampered with cannot become evidence just because
    the registration document still names it.
    """
    subject = campaign()
    called: list[str] = []

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.freeze_validation_campaign(subject, now=NOW)
        registration = registry.register(
            draft(hypothesis_id="H-TAMPER", validation_campaign=subject),
            now=NOW,
            critique=critique("H-TAMPER"),
        )
        plan = compile_experiment(registration, datasets=[snapshot()])
        session.execute(
            search_runs.update()
            .where(search_runs.c.search_id == subject.selection_search_id)
            .values(actor="someone-else")
        )

        def record(_plan, _access):
            called.append("measured")
            return Measured(effect=Decimal("2.5"), test_statistic=Decimal("9.0"),
                            probes_run=plan.probes)

        code, environment = provenance()
        with pytest.raises(RegistrationRefused, match="not an authoritative record"):
            ExperimentRunner(registry, manifests=manifests).run(
                plan, registration, record, now=NOW, code=code, environment=environment,
                applied_costs=COSTS, design=DESIGN,
            )

    assert called == []


# --- Finding 4: a scenario id is not data provenance --------------------------------------------
#
# `grade_campaign` can mark a resolved observed-market protected scenario `fresh_evidence=True`.
# So the runner must prove the frozen data boundary was honoured, or a worker reporting a
# favourable number under a frozen id mints protected evidence out of data nobody checked.


def bind_attempt(database: Database, manifests: Path, subject: ValidationCampaign,
                 called: list[str], *, datasets=None):
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.freeze_validation_campaign(subject, now=NOW)
        registration = registry.register(
            draft(hypothesis_id="H-BIND", validation_campaign=subject),
            now=NOW,
            critique=critique("H-BIND"),
        )
        plan = compile_experiment(
            registration, datasets=list(datasets) if datasets else [snapshot()]
        )

        def record(_plan, _access):
            called.append("measured")
            return Measured(effect=Decimal("2.5"), test_statistic=Decimal("9.0"),
                            probes_run=plan.probes)

        code, environment = provenance()
        return ExperimentRunner(registry, manifests=manifests).run(
            plan, registration, record, now=NOW, code=code, environment=environment,
            applied_costs=COSTS, design=DESIGN,
        )


def test_a_scenario_on_a_dataset_the_run_does_not_supply_is_refused(
    database: Database, manifests: Path
) -> None:
    """**The core of Finding 4.** The scenario names a dataset nothing in the plan covers, so
    nothing shows its data was read. Refused before measurement."""
    # Both scenarios move together: #216 records a campaign as one search over one dataset, so
    # a mixed campaign is refused at freeze and would never reach the binding check under test.
    subject = campaign(
        scenarios=(
            scenario(dataset="some-other-corpus"),
            GENERAL.model_copy(update={"dataset": "some-other-corpus"}),
        )
    )
    called: list[str] = []

    with pytest.raises(ExperimentRunError, match="supplies no dataset snapshot covering it"):
        bind_attempt(database, manifests, subject, called)

    assert called == []


def test_a_scenario_whose_role_the_run_does_not_supply_is_refused(
    database: Database, manifests: Path
) -> None:
    """Right dataset, wrong role. The plan supplies PROTECTED_EVALUATION; the scenario claims
    VALIDATION, which nothing here covers."""
    subject = campaign(
        scenarios=(scenario(data_role="VALIDATION"), GENERAL)
    )
    called: list[str] = []

    with pytest.raises(ExperimentRunError, match="supplies no dataset snapshot covering it"):
        bind_attempt(database, manifests, subject, called)

    assert called == []


def test_a_scenario_period_outside_the_supplied_snapshot_is_refused(
    database: Database, manifests: Path
) -> None:
    """Right dataset and role, but the snapshot does not span the frozen scenario period."""
    narrow = snapshot().model_copy(
        update={"start": date(2021, 1, 4), "end": date(2026, 8, 14)}
    )
    subject = campaign()
    called: list[str] = []

    with pytest.raises(ExperimentRunError, match="supplies no dataset snapshot covering it"):
        bind_attempt(database, manifests, subject, called, datasets=[narrow])

    assert called == []


def test_a_protected_scenario_outside_the_registrations_windows_is_refused(
    database: Database, manifests: Path
) -> None:
    """A holdout this claim never reserved cannot become its fresh evidence.

    The plan supplies a snapshot covering the scenario, so the dataset check passes -- what fails
    is that the persisted registration owns no protected window over it.
    """
    early = ExperimentPeriod(name="early", start=date(2014, 1, 2), end=date(2015, 12, 31))
    wide = snapshot().model_copy(update={"start": date(2014, 1, 2)})
    subject = campaign(
        eligible_period=ExperimentPeriod(
            name="eligible", start=date(2014, 1, 2), end=date(2026, 8, 14)
        ),
        scenarios=(scenario(period=early), GENERAL),
    )
    called: list[str] = []

    with pytest.raises(ExperimentRunError, match="never reserved"):
        bind_attempt(database, manifests, subject, called, datasets=[wide])

    assert called == []


def test_a_walk_forward_scenario_without_its_fit_snapshot_is_refused(
    database: Database, manifests: Path
) -> None:
    """A fit window nobody supplied is a fit that did not happen."""
    walk = scenario(
        kind=ScenarioKind.WALK_FORWARD,
        period=ExperimentPeriod(name="test", start=date(2020, 1, 2), end=date(2021, 12, 31)),
        fit_period=ExperimentPeriod(name="fit", start=date(2017, 1, 3), end=date(2019, 12, 31)),
        fit_data_role="DISCOVERY",
    )
    subject = campaign(scenarios=(walk, GENERAL))
    called: list[str] = []

    with pytest.raises(ExperimentRunError, match="supplies no snapshot for"):
        bind_attempt(database, manifests, subject, called)

    assert called == []


def test_a_resampled_scenario_needs_no_market_snapshot(
    database: Database, manifests: Path
) -> None:
    """Resampled paths are exempt from the snapshot requirement, and only from that.

    Requiring market data for a path that is explicitly not market data would be theatre --
    and `grade_campaign` still refuses it `fresh_evidence`, which is the protection that matters.
    """
    def simulated(name: str, start: date, end: date, *, adverse: bool) -> ValidationScenario:
        return scenario(
            kind=ScenarioKind.RESAMPLED_PATH,
            dataset="simulated-paths",
            adverse=adverse,
            period=ExperimentPeriod(name=name, start=start, end=end),
            evidence_source=ScenarioEvidenceSource.RESAMPLED_PATH,
            resampling_method=ResamplingMethod.STATIONARY_BLOCK_BOOTSTRAP,
        )

    # Every scenario simulated and on one dataset the plan supplies nothing for, so the run can
    # only proceed if resampled paths are genuinely exempt from the snapshot requirement.
    subject = campaign(
        scenarios=(
            simulated("sim-a", date(2018, 1, 2), date(2019, 12, 31), adverse=True),
            simulated("sim-b", date(2020, 1, 2), date(2021, 12, 31), adverse=False),
        )
    )
    called: list[str] = []

    result = bind_attempt(database, manifests, subject, called)

    assert called == ["measured"], "a resampled scenario must not block the run"
    assert result.outcome.validation_outcome is not None


def test_an_exactly_covered_protected_scenario_is_allowed(
    database: Database, manifests: Path
) -> None:
    """The control for every refusal above: the ordinary shape still runs."""
    called: list[str] = []

    result = bind_attempt(database, manifests, campaign(), called)

    assert called == ["measured"]
    assert result.outcome.validation_outcome is not None


# --- structural backstop ------------------------------------------------------------------------


def test_an_outcome_cannot_survive_while_carrying_a_refused_campaign(
    database: Database, manifests: Path
) -> None:
    """`runner._grade` downgrades on a blocking campaign; this is the type-level backstop.

    A caller constructing the outcome directly must not be able to keep SURVIVED while attaching
    the record of the gate that objected -- that would make hostile validation decoration on a
    verdict it was supposed to control.
    """
    subject = campaign()
    stress, general = subject.scenarios

    result = run_result(
        database,
        manifests,
        case="backstop",
        subject=subject,
        scenarios=(
            measured_scenario(stress, test_statistic=Decimal("-9.0")),
            measured_scenario(general),
        ),
    )
    refused = result.outcome.validation_outcome
    assert refused is not None and refused.verdict is CampaignVerdict.REFUTED

    with pytest.raises(ValueError, match="cannot carry a validation campaign that did not"):
        result.outcome.model_copy(
            update={"verdict": OutcomeVerdict.SURVIVED}
        ).model_validate(
            {
                **result.outcome.model_dump(),
                "verdict": OutcomeVerdict.SURVIVED,
            }
        )

    # And `citable` is conservative about it independently of the validator.
    assert not result.outcome.citable


def test_a_surviving_campaign_leaves_citable_intact(
    database: Database, manifests: Path
) -> None:
    """The control: a clean campaign does not make a genuine survivor uncitable."""
    subject = campaign()
    stress, general = subject.scenarios

    result = run_result(
        database,
        manifests,
        case="citable",
        subject=subject,
        scenarios=(measured_scenario(stress), measured_scenario(general)),
    )

    assert result.outcome.verdict is OutcomeVerdict.SURVIVED
    assert result.outcome.citable
