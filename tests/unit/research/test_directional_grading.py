"""A hypothesis survives only when the evidence points the way it preregistered.

Grading was `abs(test_statistic) >= luck_threshold_z` at four independent sites: the primary
verdict, the artifact/component verdict, the `ExperimentOutcome` validator that is supposed to
backstop both, and `survivor_objections`, which is the gate between a result and
`RESEARCH_SURVIVOR` and therefore between a result and shadow, paper and M1.

`abs()` is the correct rule for a two-sided claim. **This schema cannot express one.**
`EffectSpecification.validate_effect` refuses `expected <= null_value` and
`minimum_practical <= null_value`, so every registration claims the effect *exceeds* its null,
and the statistic is signed evidence about exactly that. A t of -9 was therefore certified as
`SURVIVED`: overwhelming evidence against the preregistered direction, recorded as support for
it, by the component whose whole purpose is to stop the project fooling itself.

Every case here is a single fixture with one number changed, and each refusal is paired with a
control that passes, so a grader that refused everything would fail these as loudly as the
grader that certified everything.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.artifact_testing import ArtifactOutcomeVerdict
from quantbot.research.builder import OutcomeVerdict, compile_experiment
from quantbot.research.hypothesis_graph import GraphArtifactKind
from quantbot.research.manifest import DatasetSnapshot
from quantbot.research.power import (
    ComparisonStructure,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    supports_registered_direction,
)
from quantbot.research.promotion import survivor_objections
from quantbot.research.registry import DataRole, DataWindow, HypothesisRegistry
from quantbot.research.runner import ArtifactMeasured, ExperimentRunner, Measured
from quantbot.storage import Database
from tests.unit.research.test_artifact_evidence import (
    artifact_test_draft,
    register_graph,
)
from tests.unit.research.test_artifact_evidence import (
    run as run_with_artifacts,
)
from tests.unit.research.test_builder import outcome
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
    db = Database.create(tmp_path / "directional.db")
    yield db
    db.close()


@pytest.fixture
def manifests(tmp_path: Path) -> Path:
    return tmp_path / "manifests"


def _window(dataset: str) -> DataWindow:
    return DataWindow(
        dataset=dataset,
        snapshot="2026-08-20",
        role=DataRole.PROTECTED_EVALUATION,
        start=date(2016, 1, 4),
        end=date(2026, 8, 14),
    )


def _snapshot(dataset: str) -> DatasetSnapshot:
    return snapshot().model_copy(update={"dataset": dataset})


def _register(database: Database, case: str):
    """Freeze one registration over a window of its own.

    A distinct hypothesis id **and dataset** per graded run. Both are load-bearing: a
    registration is frozen once, and a protected evaluation window can be claimed by only one
    hypothesis -- the contamination guard from #22, which is doing its job when it refuses a
    second claim on a spent range. Sharing either across cases would make a test fail on
    registration or on window contention rather than on the statistic under test, and a test
    that fails for an unrelated reason proves nothing about the rule it names.
    """
    dataset = f"sip-us-equities-daily-{case}"
    with database.transaction() as session:
        return HypothesisRegistry(session).register(
            draft(
                hypothesis_id=f"H-DIR-{case}",
                windows=(_window(dataset),),
                # The novelty gate correctly reads these as overlapping: they share a universe
                # and features by construction, because holding everything constant except the
                # statistic is the whole design. Declared rather than evaded by perturbing the
                # features, which would have made the cases differ in a second way and weakened
                # what a differing verdict proves.
                materially_different=(
                    f"grading fixture {case}: identical claim over a private window, varying "
                    "only the measured statistic"
                ),
            ),
            now=NOW,
            critique=critique(f"H-DIR-{case}"),
        )


def _graded(
    database: Database, manifests: Path, *, case: str = "base", **measured: object
):
    """Run one measurement through the real grader and return its outcome.

    Deliberately the whole runner rather than `_grade` directly: the verdict a caller can
    actually obtain is the one that matters, and a private-method test would keep passing if the
    grader stopped being reached.
    """
    registration = _register(database, case)
    plan = compile_experiment(
        registration, datasets=[_snapshot(f"sip-us-equities-daily-{case}")]
    )
    code, environment = provenance()
    payload = Measured(probes_run=plan.probes, **measured)  # type: ignore[arg-type]
    with database.transaction() as session:
        return ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            lambda _plan, _access: payload,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        ).outcome


def _graded_on_its_own_bar(database: Database, manifests: Path, *, case: str, sign: int):
    """Grade a run at exactly `sign * its own luck bar`.

    The bar has to come from the registration being graded, not from a separate one: every
    registration raises the cumulative trial count, so the next one is judged against a strictly
    higher bar. Reading a threshold from one registration and applying it to another is the
    multiple-testing accounting working as designed, and would make this test measure that
    instead of the inequality it names.
    """
    registration = _register(database, case)
    threshold = registration.power.luck_threshold_z
    plan = compile_experiment(
        registration, datasets=[_snapshot(f"sip-us-equities-daily-{case}")]
    )
    code, environment = provenance()
    with database.transaction() as session:
        return ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            lambda _plan, _access: Measured(
                effect=Decimal(sign) * Decimal("2.5"),
                test_statistic=Decimal(sign) * threshold,
                probes_run=plan.probes,
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        ).outcome


# --- the primary verdict -----------------------------------------------------------------


def test_a_strong_result_in_the_registered_direction_survives(
    database: Database, manifests: Path
) -> None:
    """The control for everything below. Without it, a grader that refused every result would
    satisfy every other case in this file."""
    graded = _graded(
        database, manifests, effect=Decimal("2.5"), test_statistic=Decimal("9.0")
    )

    assert graded.verdict is OutcomeVerdict.SURVIVED
    assert "cleared" in graded.detail


def test_a_weak_result_in_the_registered_direction_is_refuted(
    database: Database, manifests: Path
) -> None:
    """An ordinary miss: right direction, not enough of it."""
    graded = _graded(
        database, manifests, effect=Decimal("0.2"), test_statistic=Decimal("0.4")
    )

    assert graded.verdict is OutcomeVerdict.REFUTED
    assert "did not clear" in graded.detail


def test_a_strong_result_in_the_opposite_direction_is_refuted_not_survived(
    database: Database, manifests: Path
) -> None:
    """**The defect.** Under `abs(statistic) >= threshold` this was `SURVIVED`.

    The reason is asserted as well as the verdict, because "signal, pointing the other way" and
    "no signal" are different findings and only the reason distinguishes them. An agent deciding
    whether to keep pursuing the mechanism needs that difference.
    """
    graded = _graded(
        database, manifests, effect=Decimal("-2.5"), test_statistic=Decimal("-9.0")
    )

    assert graded.verdict is OutcomeVerdict.REFUTED
    assert "opposite to the registered claim" in graded.detail
    assert "evidence against it" in graded.detail


def test_a_strong_opposite_direction_result_is_not_citable_as_confirmatory(
    database: Database, manifests: Path
) -> None:
    """The consequence downstream. `citable` gates whether a result may be quoted as
    confirmatory evidence about Elrond, and an inverted survivor would have been."""
    graded = _graded(
        database, manifests, effect=Decimal("-2.5"), test_statistic=Decimal("-9.0")
    )

    assert not graded.citable


def test_a_failed_probe_still_refutes_a_statistic_that_clears(
    database: Database, manifests: Path
) -> None:
    """Probe ordering is unchanged by the directional fix: an objection disqualifies before the
    headline number is read, so the reason names the probe."""
    graded = _graded(
        database,
        manifests,
        effect=Decimal("2.5"),
        test_statistic=Decimal("9.0"),
        probes_failed=("hidden-beta",),
    )

    assert graded.verdict is OutcomeVerdict.REFUTED
    assert "hidden-beta" in graded.detail


def test_the_bar_is_inclusive_and_its_negation_is_not(
    database: Database, manifests: Path
) -> None:
    """Both boundaries, because a signed comparison is exactly where an inequality slips.

    `-threshold` is the first value `abs()` wrongly certified, so it is the one case that must
    move from SURVIVED to REFUTED and stay there.
    """
    on_the_bar = _graded_on_its_own_bar(database, manifests, case="on-bar", sign=1)
    assert on_the_bar.verdict is OutcomeVerdict.SURVIVED

    negated = _graded_on_its_own_bar(database, manifests, case="off-bar", sign=-1)
    assert negated.verdict is OutcomeVerdict.REFUTED
    assert "opposite to the registered claim" in negated.detail


def test_the_verdicts_that_are_not_about_direction_are_unchanged(
    database: Database, manifests: Path
) -> None:
    """INCONCLUSIVE keeps its meaning. A missing statistic and a dropped probe are both
    "the number cannot be read", which is neither support nor refutation, and a directional
    rule must not quietly convert either into REFUTED."""
    no_statistic = _graded(
        database, manifests, case="no-stat", effect=Decimal("2.5"), test_statistic=None
    )
    assert no_statistic.verdict is OutcomeVerdict.INCONCLUSIVE
    assert "nothing was tested" in no_statistic.detail

    registration = _register(database, "dropped-probe")
    plan = compile_experiment(
        registration, datasets=[_snapshot("sip-us-equities-daily-dropped-probe")]
    )
    code, environment = provenance()
    with database.transaction() as session:
        dropped = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            lambda _plan, _access: Measured(
                effect=Decimal("-2.5"),
                test_statistic=Decimal("-9.0"),
                probes_run=plan.probes[:1],
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        ).outcome
    # Opposite-direction *and* missing a probe: still INCONCLUSIVE, not REFUTED. An unprobed run
    # has no readable result either way, and that exemption predates this fix.
    assert dropped.verdict is OutcomeVerdict.INCONCLUSIVE


# --- the structural backstop -------------------------------------------------------------


def test_an_outcome_cannot_be_constructed_as_a_survivor_against_its_own_direction() -> None:
    """`ExperimentOutcome`'s validator is what makes the grader unnecessary to trust: even a
    caller building an outcome by hand cannot mint a survivor below its own bar. It used
    `abs()`, so it would have certified the inverted result the grader produced -- the backstop
    carrying the same defect as the thing it backs up."""
    with pytest.raises(ValueError, match="in the registered direction"):
        outcome(verdict=OutcomeVerdict.SURVIVED, test_statistic=Decimal("-9.0"))

    # The control: the same construction in the registered direction is accepted, so the
    # validator is refusing the direction rather than the magnitude.
    accepted = outcome(verdict=OutcomeVerdict.SURVIVED, test_statistic=Decimal("9.0"))
    assert accepted.verdict is OutcomeVerdict.SURVIVED


# --- the survivor gate -------------------------------------------------------------------


def test_a_strong_opposite_direction_result_cannot_clear_the_survivor_gate() -> None:
    """The most consequential site. `survivor_objections` is what stands between a result and
    `RESEARCH_SURVIVOR`, and so between a result and shadow, paper trading and M1.

    Constructed as REFUTED because the validator now refuses to build it as SURVIVED at all --
    which is the layering working. The gate must object to the *statistic* on its own terms, not
    only inherit the verdict, so the objection text is asserted rather than the count.
    """
    inverted = outcome(verdict=OutcomeVerdict.REFUTED, test_statistic=Decimal("-9.0"))

    objections = survivor_objections(inverted)

    assert any("in the direction the hypothesis registered" in item for item in objections)
    assert any("evidence against the claim rather than for it" in item for item in objections)


def test_the_survivor_gate_still_passes_a_clean_result_in_the_registered_direction() -> None:
    """The control. A gate that objected to everything would satisfy the test above."""
    assert survivor_objections(outcome()) == ()


# --- artifact and component evidence ------------------------------------------------------


def _artifact_verdict(
    database: Database, manifests: Path, statistic: Decimal
) -> tuple[ArtifactOutcomeVerdict, str]:
    """Grade one preregistered component contrast at `statistic` and return its verdict."""
    signal, _risk, _interaction, registration = register_graph(database)
    with database.transaction() as session:
        test = HypothesisRegistry(session).register_artifact_test(
            registration,
            artifact_test_draft(
                registration, GraphArtifactKind.COMPONENT, signal.component_id
            ),
            now=NOW + timedelta(seconds=1),
        )
    probes = compile_experiment(registration, datasets=[snapshot()]).probes
    result = run_with_artifacts(
        database,
        manifests,
        registration,
        (test,),
        (
            ArtifactMeasured(
                test_id=test.test_id,
                effect=statistic / 4,
                test_statistic=statistic,
                probes_run=probes,
            ),
        ),
        statistic=Decimal("4"),
    )
    graded = next(
        item for item in result.outcome.artifact_outcomes if item.test_id == test.test_id
    )
    return graded.verdict, graded.detail


def test_artifact_evidence_in_the_registered_direction_survives(
    database: Database, manifests: Path
) -> None:
    """The control for the component-level case."""
    verdict, detail = _artifact_verdict(database, manifests, Decimal("9.0"))

    assert verdict is ArtifactOutcomeVerdict.SURVIVED
    assert "cleared" in detail


def test_artifact_evidence_in_the_opposite_direction_is_refuted_not_survived(
    database: Database, manifests: Path
) -> None:
    """The same inversion one level down, and it matters for the same reason: component evidence
    feeds recombination and lineage, so a component measured to *cost* return would have been
    recorded as one that contributes and reused on that basis."""
    verdict, detail = _artifact_verdict(database, manifests, Decimal("-9.0"))

    assert verdict is ArtifactOutcomeVerdict.REFUTED
    assert "opposite to the registered artifact claim" in detail
    assert "evidence against it" in detail


def test_weak_artifact_evidence_is_refuted_without_the_directional_reason(
    database: Database, manifests: Path
) -> None:
    """An ordinary artifact miss keeps its own explanation, so the two refusals stay
    distinguishable in the durable record rather than collapsing into one string."""
    verdict, detail = _artifact_verdict(database, manifests, Decimal("0.4"))

    assert verdict is ArtifactOutcomeVerdict.REFUTED
    assert "did not clear" in detail
    assert "opposite" not in detail


# --- the invariant the one-sided rule rests on --------------------------------------------


def test_the_schema_cannot_express_a_hypothesis_this_grading_would_misread() -> None:
    """`supports_registered_direction` is a signed comparison **because** every registration
    this schema can express claims the effect exceeds its null. That is enforced by
    `EffectSpecification.validate_effect`, not by convention.

    If two-sided or negative-direction hypotheses are ever added, a signed bar silently becomes
    the wrong rule for them and the inversion returns wearing different clothes. This fails the
    moment the schema stops enforcing one-sidedness, so whoever relaxes it is sent to the grader
    instead of finding out from a wrong conclusion months later.
    """
    common: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "justification": "Large enough that 2669 sessions can resolve it.",
        "comparison": ComparisonStructure.SINGLE_SAMPLE,
        "economics": EconomicProfile(
            annual_rebalances=3,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    }

    # A claim that the effect is *negative*.
    with pytest.raises(ValueError, match="must exceed the null"):
        EffectSpecification(
            expected=Decimal("-2.0"), minimum_practical=Decimal("-3.0"), **common
        )
    # A claim of no effect, which is what a two-sided registration would look like here.
    with pytest.raises(ValueError, match="must exceed the null"):
        EffectSpecification(expected=Decimal("0"), minimum_practical=Decimal("0"), **common)
    # And a directionally mixed one.
    with pytest.raises(ValueError, match="minimum practical effect must exceed the null"):
        EffectSpecification(
            expected=Decimal("2.0"), minimum_practical=Decimal("-1.0"), **common
        )

    # The control: a positive one-sided claim is accepted, so the validator is enforcing
    # direction rather than rejecting everything.
    accepted = EffectSpecification(
        expected=Decimal("2.0"), minimum_practical=Decimal("1.0"), **common
    )
    assert accepted.expected > accepted.null_value


def test_the_helper_is_signed_at_both_boundaries() -> None:
    """The unit the four call sites share, pinned directly so a future caller cannot
    reintroduce `abs()` by reimplementing it locally."""
    bar = Decimal("2.9")

    assert supports_registered_direction(Decimal("9"), bar)
    assert supports_registered_direction(bar, bar)
    assert not supports_registered_direction(Decimal("2.8"), bar)
    assert not supports_registered_direction(-bar, bar)
    assert not supports_registered_direction(Decimal("-9"), bar)
