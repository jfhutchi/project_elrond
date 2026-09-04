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
from quantbot.research.hypothesis_graph import (
    BehaviorTerm,
    GraphArtifactKind,
    HypothesisComponent,
    HypothesisGraph,
    HypothesisLineage,
    LineageAction,
    LineageChange,
)
from quantbot.research.manifest import (
    CodeProvenance,
    DatasetSnapshot,
    EnvironmentFingerprint,
    ExecutionPath,
    ExperimentPeriod,
)
from quantbot.research.memory import RecordKind, Verdict
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
    RegistrationRefused,
    SearchOrigin,
    WindowState,
)
from quantbot.research.runner import (
    ExperimentDesign,
    ExperimentRunError,
    ExperimentRunner,
    Measured,
    ProtectedAccess,
    record_from_run,
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
    db = Database.create(tmp_path / "runner.db")
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


def _register(database: Database, **overrides: object):
    with database.transaction() as session:
        return HypothesisRegistry(session).register(
            draft(**overrides), now=NOW, critique=critique()
        )


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


def test_a_componentized_run_carries_the_exact_graph_into_its_manifest(
    database: Database, manifests: Path
) -> None:
    signal = HypothesisComponent(
        component_type="signal",
        behavior=(
            BehaviorTerm(key="implementation", value="close-above-sma"),
            BehaviorTerm(key="lookback", value=200),
        ),
    )
    graph = HypothesisGraph(components=(signal,))
    lineage = HypothesisLineage(
        changes=(
            LineageChange(
                artifact_kind=GraphArtifactKind.COMPONENT,
                action=LineageAction.INTRODUCED,
                child_content_id=signal.component_id,
            ),
        )
    )
    registration = _register(database, graph=graph, lineage=lineage)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()

    with database.transaction() as session:

        def measurement(_plan, _access: ProtectedAccess) -> Measured:
            return Measured(
                effect=Decimal("2.1"),
                test_statistic=Decimal("4.0"),
                probes_run=plan.probes,
            )

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

    assert plan.hypothesis_graph_hash == graph.graph_id
    assert result.manifest.hypothesis_graph_hash == graph.graph_id
    persisted = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert persisted["hypothesis_graph_hash"] == graph.graph_id


def test_a_plan_cannot_spoof_graph_provenance(
    database: Database, manifests: Path
) -> None:
    registration = _register(database)
    honest = compile_experiment(registration, datasets=[snapshot()])
    spoofed = honest.model_copy(update={"hypothesis_graph_hash": "f" * 64})
    code, environment = provenance()

    with database.transaction() as session, pytest.raises(
        ExperimentRunError, match="graph provenance"
    ):
        ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            spoofed,
            registration,
            lambda _plan, _access: Measured(
                effect=Decimal("2.1"),
                test_statistic=Decimal("4.0"),
                probes_run=spoofed.probes,
            ),
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )


def test_a_crashing_measurement_still_spends_the_holdout(
    database: Database, manifests: Path
) -> None:
    """Reading the data and then failing has still read it.

    If consumption were recorded on success, failing would be a way to inspect a protected
    window for free and register against it again afterwards.

    Checked twice, and the second read is the one that proves it. The runner works inside the
    caller's transaction, so a state read from *within* that transaction is the runner's
    intention, not a durable fact -- it passes identically when the transaction is about to roll
    back and discard the transition. The read after the block is what says the consumption
    committed, which is the property #159 Finding 9 depends on.
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

    with database.transaction() as session:
        assert _window_state(session) == WindowState.CONSUMED.value, (
            "the consumption has to survive the commit, not merely happen inside it"
        )


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


def _run_to_completion(database, manifests, measurement):
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()
    with database.transaction() as session:
        return ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan,
            registration,
            measurement,
            now=NOW,
            code=code,
            environment=environment,
            applied_costs=COSTS,
            design=DESIGN,
        )


def test_a_stored_conclusion_names_the_bundle_behind_it(
    database: Database, manifests: Path
) -> None:
    """#18: `evidence_hash` was documented as the manifest hash and nothing populated it.

    So every stored conclusion was a sentence with no route back to the run that produced it.
    A finding you cannot trace to a manifest is a narrative, and storing those is the thing
    this project exists not to do.
    """

    def measurement(plan, _access) -> Measured:
        return Measured(
            effect=Decimal("2.4"), test_statistic=Decimal("4.0"), probes_run=plan.probes
        )

    result = _run_to_completion(database, manifests, measurement)

    record = record_from_run(
        result,
        record_id="R-1",
        subject="the 200-day trend filter on SPY",
        source="cycle 18",
        now=NOW,
    )

    assert record.kind is RecordKind.FINDING
    assert record.verdict is Verdict.SURVIVED
    assert record.evidence_hash == result.manifest.manifest_hash
    assert record.hypothesis_id == result.outcome.plan.hypothesis_id
    assert record.detail["manifest_path"] == str(result.manifest_path)


def test_a_run_that_did_not_complete_is_a_defect_and_never_a_finding(
    database: Database, manifests: Path
) -> None:
    """A crash produced no evidence about the hypothesis, so it gets no verdict about it.

    It is still recorded: this project's own error rate is evidence about the project even when
    it says nothing about the market. Recording it as `REFUTED` would be the same error class as
    a crashed cycle stage being stored as a refutation.
    """

    def explodes(_plan, _access) -> Measured:
        raise ZeroDivisionError("the covariance matrix was singular")

    result = _run_to_completion(database, manifests, explodes)

    record = record_from_run(
        result,
        record_id="R-2",
        subject="the 200-day trend filter on SPY",
        source="cycle 18",
        now=NOW,
    )

    assert record.kind is RecordKind.ANALYSIS_DEFECT
    assert record.verdict is None, "a crash is not a verdict about the hypothesis"
    # And it still points at the bundle, so the failure is as traceable as a success.
    assert record.evidence_hash == result.manifest.manifest_hash
    assert "ZeroDivisionError" in record.statement


def test_a_run_that_stored_nothing_promises_no_rerun(
    database: Database, manifests: Path
) -> None:
    """An ordinary measurement carries no source, so its bundle must not describe a rerun.

    Surfaced by a surviving mutation: every generated-experiment test supplies reproduction
    inputs, so emitting the command unconditionally was indistinguishable from emitting it when
    a rerun is actually possible.

    A manifest naming `reproduce-experiment` for a bundle with nothing stored beside it is worse
    than an empty field. An empty field says reproduction is unavailable; a command that finds
    no inputs says somebody checked and it works, right up until a person runs it.
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

    assert result.manifest.reproduction_command == (), (
        "a bundle with nothing stored beside it must not claim it can be rerun"
    )
    assert not (manifests / f"{result.manifest.experiment_id}.inputs").exists()


# --- #159 Finding 10: the handoff is a durable boundary, not a transaction participant ---------
#
# Finding 9 made a *grading refusal* durable by recording a failure instead of raising. That
# closed one path and left the shape of the defect: the consumption was still a row in the
# caller's open transaction, so a pydantic `ValidationError`, a manifest the validator refuses,
# an `OSError` from the write, or a plain bug in the runner would still escape `run()`, unwind
# the transaction, and erase the record that a protected window had been read.
#
# The fix is not a wider `except`. Catching a runner bug and filing it as a research FAILED
# outcome is a defect wearing a result's clothes. `_spend` commits before it issues
# `ProtectedAccess`, so every later failure mode is safe without any of them being anticipated,
# and genuine bugs still propagate -- loudly, and still having cost the holdout, which is the
# correct price for having read it.
#
# Every test below reads the window state from a NEW transaction. Reading it inside the one the
# runner used proves nothing: that read passes identically when the transaction is about to roll
# back, which is precisely the state this defect produced.

#: Credential-shaped, so `ExperimentManifest` refuses any bundle carrying it. Not a real token.
FAKE_TOKEN = "ghp_" + "a" * 24


def _committed_state(database: Database, hypothesis_id: str = "H-RUN-1") -> str | None:
    """The window's state as a *separate* transaction sees it."""
    with database.transaction() as session:
        row = session.execute(
            text("SELECT state FROM hypothesis_data_windows WHERE hypothesis_id = :h"),
            {"h": hypothesis_id},
        ).one_or_none()
    return None if row is None else str(row[0])


def _refuses_a_second_handoff(database: Database, manifests: Path, plan, registration) -> None:
    """A spent window cannot be handed over again, and the measurement must not run."""
    reached: list[str] = []

    def again(_plan, _access) -> Measured:
        reached.append("measured")
        return Measured(effect=None)

    code, environment = provenance()
    with database.transaction() as session:
        with pytest.raises(RegistrationRefused, match="is CONSUMED"):
            ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
                plan, registration, again, now=NOW, code=code, environment=environment,
                applied_costs=COSTS, design=DESIGN,
            )
    assert reached == [], "a spent holdout must not be readable a second time"


def _run_expecting(
    database: Database,
    manifests: Path,
    measurement,
    *,
    applied_costs=None,
    registration=None,
    plan=None,
):
    """Run inside its own transaction and hand back whatever escaped, plus the plan used."""
    registration = registration or _register(database)
    plan = plan or compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()
    escaped: list[BaseException] = []
    try:
        with database.transaction() as session:
            ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
                plan,
                registration,
                measurement,
                now=NOW,
                code=code,
                environment=environment,
                applied_costs=COSTS if applied_costs is None else applied_costs,
                design=DESIGN,
            )
    except BaseException as error:  # noqa: BLE001 -- propagation is the property under test
        escaped.append(error)
    return escaped, plan, registration


def _good_measurement(plan):
    def measurement(_plan, _access) -> Measured:
        return Measured(
            effect=Decimal("2.5"), test_statistic=Decimal("9.0"), probes_run=plan.probes
        )

    return measurement


def test_a_runner_bug_after_the_handoff_still_costs_the_holdout(
    database: Database, manifests: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The core of Finding 10.** Not an `ExperimentRunError`, so Finding 9 does not catch it.

    A defect inside `_grade` is the case that must not be laundered into a research verdict: it
    propagates, exactly as a bug should, and the consumption survives it anyway.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])

    def boom(*_args, **_kwargs):
        raise RuntimeError("a defect in the runner, not a finding about the hypothesis")

    monkeypatch.setattr(ExperimentRunner, "_grade", boom)
    escaped, plan, registration = _run_expecting(
        database, manifests, _good_measurement(plan), registration=registration, plan=plan
    )

    assert [type(error).__name__ for error in escaped] == ["RuntimeError"], (
        "a runner bug must propagate rather than be recorded as a research result"
    )
    assert _committed_state(database) == WindowState.CONSUMED.value
    monkeypatch.undo()
    _refuses_a_second_handoff(database, manifests, plan, registration)


def test_measurement_output_that_fails_validation_still_costs_the_holdout(
    database: Database, manifests: Path
) -> None:
    """A pydantic `ValidationError` while building the outcome, from the measurement's own output.

    An empty probe name is rejected by `ExperimentOutcome`, which raises a `ValidationError` --
    not an `ExperimentRunError`, so nothing catches it. The data was read regardless.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])

    def measurement(_plan, _access) -> Measured:
        return Measured(
            effect=Decimal("2.5"),
            test_statistic=Decimal("9.0"),
            probes_run=("", *plan.probes),
        )

    escaped, plan, registration = _run_expecting(
        database, manifests, measurement, registration=registration, plan=plan
    )

    assert escaped and not isinstance(escaped[0], ExperimentRunError)
    assert _committed_state(database) == WindowState.CONSUMED.value
    _refuses_a_second_handoff(database, manifests, plan, registration)


def test_a_manifest_the_validator_refuses_still_costs_the_holdout(
    database: Database, manifests: Path
) -> None:
    """`ExperimentManifest` refuses a bundle carrying a credential-shaped string.

    The refusal happens in `_record`, after the measurement has read the holdout, and it is a
    `ValueError` rather than an `ExperimentRunError`. Nothing is written, and the consumption
    still stands.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])

    escaped, plan, registration = _run_expecting(
        database,
        manifests,
        _good_measurement(plan),
        applied_costs={**COSTS, "broker_note": f"key {FAKE_TOKEN}"},
        registration=registration,
        plan=plan,
    )

    assert escaped and "must not carry credentials" in str(escaped[0])
    written = list(manifests.glob("*.json")) if manifests.exists() else []
    assert written == [], "a bundle the validator refused must not reach disk"
    assert _committed_state(database) == WindowState.CONSUMED.value
    _refuses_a_second_handoff(database, manifests, plan, registration)


def test_a_manifest_that_cannot_be_written_still_costs_the_holdout(
    database: Database, tmp_path: Path
) -> None:
    """An `OSError` from the filesystem, after the data has been read.

    The manifest directory is a regular file, so creating it fails. Disk-full, a revoked
    permission or a vanished mount are the same case, and none of them can un-read a holdout.
    """
    blocked = tmp_path / "manifests-is-a-file"
    blocked.write_text("not a directory", encoding="utf-8")
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])

    escaped, plan, registration = _run_expecting(
        database, blocked, _good_measurement(plan), registration=registration, plan=plan
    )

    assert escaped and isinstance(escaped[0], OSError)
    assert _committed_state(database) == WindowState.CONSUMED.value
    _refuses_a_second_handoff(database, blocked, plan, registration)


def test_a_failure_before_the_handoff_does_not_spend_anything(
    database: Database, manifests: Path
) -> None:
    """The other side of the boundary, and the reason it is a boundary rather than a policy.

    Nothing has been read yet, so refusing must leave the reservation exactly as it was. A fix
    that made consumption durable by consuming earlier, or more often, would pass every test
    above and fail this one.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    reached: list[str] = []

    def measurement(_plan, _access) -> Measured:
        reached.append("measured")
        return Measured(effect=None)

    escaped, _plan, _registration = _run_expecting(
        database,
        manifests,
        measurement,
        # Missing `slippage_bps`, refused at the top of `run()` long before `_spend`.
        applied_costs={"commission_per_order": "0"},
        registration=registration,
        plan=plan,
    )

    assert escaped and isinstance(escaped[0], ExperimentRunError)
    assert reached == []
    assert _committed_state(database) == WindowState.RESERVED.value, (
        "a refusal before the handoff must leave the holdout unspent"
    )

    # And the window is still usable, which is what "unspent" has to mean.
    code, environment = provenance()
    with database.transaction() as session:
        result = ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan, registration, _good_measurement(plan), now=NOW, code=code,
            environment=environment, applied_costs=COSTS, design=DESIGN,
        )
    assert result.outcome.verdict is OutcomeVerdict.SURVIVED
    assert _committed_state(database) == WindowState.CONSUMED.value


def test_the_handoff_commits_before_the_token_is_issued(
    database: Database, manifests: Path
) -> None:
    """Ordering, asserted from inside the measurement rather than after the run.

    The consumption is visible to a *separate* transaction while the measurement is still
    running, which is only true if the commit precedes the handoff. Checking afterwards would
    pass just as well if the commit happened at the end, and that ordering is the whole defect:
    a window consumed but not yet committed must not be reachable by a measurement.
    """
    registration = _register(database)
    plan = compile_experiment(registration, datasets=[snapshot()])
    code, environment = provenance()
    seen_by_another_transaction: list[str | None] = []

    def measurement(_plan, access: ProtectedAccess) -> Measured:
        assert access.windows == ((DATASET, DataRole.PROTECTED_EVALUATION),)
        seen_by_another_transaction.append(_committed_state(database))
        return Measured(
            effect=Decimal("2.1"), test_statistic=Decimal("4.0"), probes_run=plan.probes
        )

    with database.transaction() as session:
        ExperimentRunner(HypothesisRegistry(session), manifests=manifests).run(
            plan, registration, measurement, now=NOW, code=code, environment=environment,
            applied_costs=COSTS, design=DESIGN,
        )

    assert seen_by_another_transaction == [WindowState.CONSUMED.value]
