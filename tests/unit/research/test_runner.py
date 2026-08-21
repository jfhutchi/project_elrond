"""The experiment runner: spending the holdout, and what a run is allowed to claim.

The property under test throughout is #32 — a protected window cannot be read without a durable
consumption record existing for it. Every test here should fail if that stops being true.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from quantbot.research.builder import OutcomeVerdict, compile_experiment
from quantbot.research.critic import DeterministicCritic, DeterministicReview
from quantbot.research.manifest import (
    CodeProvenance,
    DatasetSnapshot,
    EnvironmentFingerprint,
    ExecutionPath,
    ExperimentPeriod,
)
from quantbot.research.power import (
    ComparisonStructure,
    EconomicProfile,
    EffectSpecification,
    Estimand,
)
from quantbot.research.registry import (
    DataRole,
    DataWindow,
    HypothesisDraft,
    HypothesisRegistry,
    SearchOrigin,
    WindowState,
)
from quantbot.research.runner import (
    ExperimentDesign,
    ExperimentRunError,
    ExperimentRunner,
    Measured,
    ProtectedAccess,
)
from quantbot.research.sources import EpistemicStatus, EvidenceBasis
from quantbot.storage import Database

NOW = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)
DATASET = "sip-us-equities-daily"
#: What the run charged, as opposed to what the hypothesis assumed it would.
COSTS = {"slippage_bps": "1.1", "commission_per_order": "0"}
#: Fixed before the run, because a split chosen after seeing the result is not a split.
SPLITS = (
    ExperimentPeriod(name="train-1", start=date(2016, 1, 4), end=date(2021, 12, 31)),
    ExperimentPeriod(name="test-1", start=date(2022, 1, 3), end=date(2026, 8, 14)),
)
#: The whole design, fixed before the run. A grid chosen afterwards is a search, not a check.
DESIGN = ExperimentDesign(
    walk_forward_splits=SPLITS,
    neighborhood_grid={"trend_period": (150, 200, 250)},
    ablations=("without-trend-filter",),
)


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "runner.db")
    yield db
    db.close()


@pytest.fixture
def manifests(tmp_path) -> Path:
    """Where the runner writes its bundles. Required, so every test has somewhere real."""
    return tmp_path / "manifests"


def draft(**overrides: object) -> HypothesisDraft:
    fields: dict[str, object] = {
        "hypothesis_id": "H-RUN-1",
        "family_id": "trend-following",
        "question": "Does the 200-day trend filter earn a positive risk-adjusted return?",
        "prediction": "Annualised Sharpe is at least 2.0.",
        "null_hypothesis": "The Sharpe is zero.",
        "falsified_if": "The Sharpe fails to clear the luck bar.",
        "universe": ("SPY",),
        "features": ("close_sma_200",),
        "target": "next-session excess return",
        "windows": (
            DataWindow(
                dataset=DATASET,
                snapshot="2026-08-20",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(2016, 1, 4),
                end=date(2026, 8, 14),
            ),
        ),
        "primary_estimand": "annualised Sharpe against a null of zero",
        "effect": EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("2.0"),
            minimum_practical=Decimal("1.0"),
            justification="Large enough that 2669 sessions can resolve it.",
            comparison=ComparisonStructure.SINGLE_SAMPLE,
            economics=EconomicProfile(
                annual_rebalances=3,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        ),
        "available_observations": 2669,
        "search_cardinality": 1,
        "search_origin": SearchOrigin.NO_DATA_DEPENDENT_SEARCH,
        "confounders": ("the 2020 crash dominating the sample",),
        "basis": EvidenceBasis(citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE),
        "proposed_by": "claude-opus-5",
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def critique(hypothesis_id: str = "H-RUN-1"):
    review = DeterministicReview(objections=(), assessed=("hidden-beta", "cost-sensitivity"))
    return DeterministicCritic(review).review(hypothesis_id, 1, now=NOW)


def snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset=DATASET,
        snapshot="2026-08-20",
        role=DataRole.PROTECTED_EVALUATION.value,
        start=date(2016, 1, 4),
        end=date(2026, 8, 14),
        content_hash="sip-hash",
        observations=2669,
        # Lineage is required by the compiler (#17): without an availability timestamp a bundle
        # can assert it avoided look-ahead but cannot show it.
        provider="alpaca",
        retrieved_at=NOW,
        earliest_available_at=datetime(2016, 1, 5, 21, 0, tzinfo=UTC),
        transformation_version="v1",
    )


def provenance() -> tuple[CodeProvenance, EnvironmentFingerprint]:
    return (
        CodeProvenance(
            git_commit="d8ba75820f5d11969d755010538dc53c54e5c730",
            dirty=False,
            configuration_hash="cfg-hash",
            execution_path=ExecutionPath.PRODUCTION_ENGINE,
        ),
        EnvironmentFingerprint(
            python_version="3.11.9",
            platform="linux",
            dependency_lock_hash="lock-hash",
            host_class="ci",
        ),
    )


def _register(database: Database):
    with database.transaction() as session:
        return HypothesisRegistry(session).register(draft(), now=NOW, critique=critique())


def _window_state(session) -> str:
    """Read the window state through the caller's own session.

    Deliberately not opening a second transaction: the runner consumes inside the transaction it
    was handed, so a separate connection cannot see the change until commit and would report
    RESERVED however correct the runner is. Testing ordering requires reading from inside the
    same unit of work.
    """
    row = session.execute(
        text("SELECT state FROM hypothesis_data_windows WHERE hypothesis_id = 'H-RUN-1'")
    ).one()
    return str(row[0])


def test_the_holdout_is_spent_before_the_measurement_reads_it(
    database: Database, manifests: Path
) -> None:
    """#32: consumption happens at handoff, not on success.

    Asserted from *inside* the measurement rather than after the run, because the distinction
    that matters is ordering. Checking the state afterwards would pass even if consumption were
    recorded at the end, which is the design that lets a crashed experiment read a holdout for
    free.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()
    observed_during_run: list[str] = []

    with database.transaction() as session:

        def measurement(_plan, access: ProtectedAccess) -> Measured:
            observed_during_run.append(_window_state(session))
            assert access.windows == ((DATASET, DataRole.PROTECTED_EVALUATION),)
            return Measured(
                effect=Decimal("2.1"),
                test_statistic=Decimal("4.0"),
                probes_run=plan.probes,
            )

        ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            measurement,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    assert observed_during_run == [WindowState.CONSUMED.value]


def test_a_crashing_measurement_still_spends_the_holdout(
    database: Database, manifests: Path
) -> None:
    """Reading the data and then failing has still read it.

    If consumption were recorded on success, failing would be a way to inspect a protected
    window for free and register against it again afterwards.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    def measurement(_plan, _access) -> Measured:
        raise ZeroDivisionError("the experiment blew up after opening the data")

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            measurement,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )
        assert _window_state(session) == WindowState.CONSUMED.value

    assert result.outcome.verdict is OutcomeVerdict.FAILED
    assert "ZeroDivisionError" in result.outcome.detail


def test_protected_access_cannot_be_forged() -> None:
    """The token is the evidence. Manufacturing it would make the evidence worthless."""
    with pytest.raises(ExperimentRunError, match="cannot be constructed directly"):
        ProtectedAccess(
            hypothesis_id="H-RUN-1",
            version=1,
            windows=((DATASET, DataRole.PROTECTED_EVALUATION),),
            consumed_at=NOW,
            _issued=object(),
        )


def test_a_measurement_cannot_nominate_itself_a_survivor(
    database: Database, manifests: Path
) -> None:
    """The runner grades; the measurement reports. A number below the bar is refuted."""
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    def weak(_plan, _access) -> Measured:
        return Measured(
            effect=Decimal("0.2"),
            test_statistic=Decimal("0.4"),
            probes_run=plan.probes,
            detail="I would like to be a survivor",
        )

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            weak,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    assert result.outcome.verdict is OutcomeVerdict.REFUTED
    assert "did not clear" in result.outcome.detail


def test_a_failed_probe_refutes_even_when_the_statistic_clears(
    database: Database, manifests: Path
) -> None:
    """Ordering matters: probes are disqualifying, not advisory.

    A result that clears the luck bar and fails an adversarial probe is refuted. If the headline
    number won that contest, the probes would be decorative.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    def strong_but_objectionable(_plan, _access) -> Measured:
        return Measured(
            effect=Decimal("2.5"),
            test_statistic=Decimal("9.0"),
            probes_run=plan.probes,
            probes_failed=("hidden-beta",),
        )

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            strong_but_objectionable,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    assert result.outcome.verdict is OutcomeVerdict.REFUTED
    assert "hidden-beta" in result.outcome.detail


def test_skipping_a_required_probe_is_inconclusive_not_survived(
    database: Database, manifests: Path
) -> None:
    """A run that dropped its probes has no readable result, however good the number looks."""
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    def partial(_plan, _access) -> Measured:
        return Measured(
            effect=Decimal("2.5"),
            test_statistic=Decimal("9.0"),
            probes_run=plan.probes[:1],
        )

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            partial,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    assert result.outcome.verdict is OutcomeVerdict.INCONCLUSIVE
    assert "did not run" in result.outcome.detail


def test_a_plan_cannot_be_reported_against_a_different_registration(
    database: Database, manifests: Path
) -> None:
    """The hash ties the result to the claim it was frozen against."""
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    with database.transaction() as session:
        other = HypothesisRegistry(session).register(
            draft(
                hypothesis_id="H-RUN-2",
                materially_different="a different question on a different window",
                windows=(
                    DataWindow(
                        dataset="fred-macro-monthly",
                        snapshot="2026-08-20",
                        role=DataRole.PROTECTED_EVALUATION,
                        start=date(1990, 1, 1),
                        end=date(2026, 8, 1),
                    ),
                ),
            ),
            now=NOW,
            critique=critique("H-RUN-2"),
        )

    with database.transaction() as session:
        with pytest.raises(ExperimentRunError, match="compiled against registration"):
            ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
                plan,
                other,
                lambda _p, _a: Measured(effect=None),
                now=NOW,
                code=code,
                environment=environment,
                applied_costs=COSTS,
                design=DESIGN,
            )


def test_the_manifest_records_the_run_as_confirmatory_with_its_provenance(
    database: Database, manifests: Path
) -> None:
    """#18: a result cited as evidence has to say what produced it.

    `mode` defaults to EXPLORATORY everywhere else so a forgetful caller cannot pass as
    evidence. Here the registration is in hand, so the stronger claim is the true one.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            lambda _p, _a: Measured(
                effect=Decimal("2.1"), test_statistic=Decimal("4.0"), probes_run=plan.probes
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    manifest = result.manifest
    assert manifest.mode == "CONFIRMATORY"
    assert manifest.registration_hash == registration.content_hash
    assert manifest.hypothesis_id == "H-RUN-1"
    assert manifest.code == code
    assert manifest.environment == environment
    assert manifest.datasets == plan.datasets
    # A null result must read as "no effect larger than X", not "no effect found" (#19).
    assert manifest.minimum_detectable_effect == str(registration.power.minimum_detectable_effect)
    assert manifest.power_verdict == "POWERED"


def test_a_plan_with_no_protected_window_is_refused(
    database: Database, manifests: Path
) -> None:
    """There is no holdout to spend, so nothing the run produces would be evidence."""
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    exploratory = plan.model_copy(
        update={"datasets": (snapshot().model_copy(update={"role": "DISCOVERY"}),)}
    )
    code, environment = provenance()

    with database.transaction() as session:
        with pytest.raises(ExperimentRunError, match="no PROTECTED_EVALUATION dataset"):
            ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
                exploratory,
                registration,
                lambda _p, _a: Measured(effect=None),
                now=NOW,
                code=code,
                environment=environment,
                applied_costs=COSTS,
                design=DESIGN,
            )


def test_a_confirmatory_outcome_cannot_be_obtained_without_its_manifest_on_disk(
    database: Database, manifests: Path
) -> None:
    """#18: persisting the bundle was a caller responsibility, so eventually it would not happen.

    Same shape of omission as `compile_experiment()` and `consume()` having no callers: correct,
    optional, and therefore absent the first time somebody is in a hurry. The runner now takes
    the directory in its constructor, so a runner that keeps no record cannot be built.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    def measurement(_plan, _access) -> Measured:
        return Measured(
            effect=Decimal("2.4"), test_statistic=Decimal("4.0"), probes_run=plan.probes
        )

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            measurement,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    assert result.manifest_path.exists(), "the outcome arrived without its provenance"
    written = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert written["mode"] == "CONFIRMATORY"
    assert written["hypothesis_id"] == registration.draft.hypothesis_id
    # The name is content-addressed, so the file can be found from the manifest alone.
    assert result.manifest.manifest_hash[:16] in result.manifest_path.name


def test_a_failed_run_is_recorded_as_fully_as_a_successful_one(
    database: Database, manifests: Path
) -> None:
    """An archive that only keeps what worked is the selection bias this project resists.

    The crash path used to build a manifest and hand it back with everything else; it would
    have been the easiest one to skip writing, and the most damaging, because a failure nobody
    recorded is a failure the next agent repeats.
    """

    def explodes(plan, access):
        raise ZeroDivisionError("the covariance matrix was singular")

    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            explodes,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )

    assert result.outcome.verdict is OutcomeVerdict.FAILED
    assert result.manifest_path.exists(), "a failed run must leave a record too"
    written = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert written["results"]["primary"]["verdict"] == "FAILED"
    assert "ZeroDivisionError" in result.outcome.detail


def test_a_runner_cannot_be_built_without_somewhere_to_record(database: Database) -> None:
    """The enforcement is the required argument, so this asserts the argument is required.

    An optional `manifests=None` would leave the default construction -- the one people
    actually write -- as the one that keeps no evidence.
    """
    import inspect

    signature = inspect.signature(ExperimentRunner.__init__)
    assert signature.parameters["manifests"].default is inspect.Parameter.empty

    with database.transaction() as session:
        with pytest.raises(TypeError):
            ExperimentRunner(HypothesisRegistry(session))  # type: ignore[call-arg]
