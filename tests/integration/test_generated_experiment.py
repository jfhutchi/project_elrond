"""A frozen registration through generated code to a manifest, with nothing injected (#8, #18).

Sol's closure criterion for #8 was one end-to-end test that starts with a frozen registration and
traverses compiler -> generated implementation -> sandbox -> production evaluation ->
manifest/result *without manually constructing the measurement result*.

That last clause is the whole test. Every earlier version of this pipeline had a `Measurement`
callable written by trusted application code, which meant the number under test was supplied by
the same hand that wrote the assertions. Here the number comes out of `BacktestEngine` after a
signal that model-authored code computed inside a kernel-isolated sandbox.

The model is stubbed rather than live, and the sandbox is real. That split is deliberate: a live
model makes the test non-deterministic and tests the weights rather than the wiring, while a
stubbed sandbox would prove nothing about the isolation that makes running generated code
acceptable at all. The signal source is untrusted code executing behind the boundary; only its
*author* is fixed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.backtest.engine import BacktestEngine
from quantbot.domain import Bar
from quantbot.research.builder import ExecutionPath, OutcomeVerdict, compile_experiment
from quantbot.research.codegen import GeneratedCode, measurement_from_signal, run_generated_signal
from quantbot.research.manifest import DatasetSnapshot, ExperimentPeriod
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
)
from quantbot.research.reproducibility import capture_environment
from quantbot.research.runner import ExperimentDesign, ExperimentRunner
from quantbot.research.sources import EpistemicStatus, EvidenceBasis
from quantbot.sandbox import SandboxPolicy
from quantbot.sandbox.wsl import WslSandbox, wsl_backend_available
from quantbot.storage import Database
from quantbot.strategy import load_strategy_config

pytestmark = pytest.mark.skipif(
    not wsl_backend_available(),
    reason="generated code requires the OS-enforced sandbox; this must not pass without one",
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DATASET = "sip-us-equities-daily"
START = date(2016, 1, 4)

#: A real implementation, written the way a model would be asked to write one. It reads the input
#: file this module materialises, computes a moving-average signal using only past bars, and emits
#: the JSON contract. Deterministic, so the test asserts wiring rather than a model's mood.
MINER_SOURCE = """
import json

with open("inputs/bars.json") as handle:
    bars = json.load(handle)

series = bars["SPY"]
signal = {}
window = 20
for index in range(window, len(series)):
    # Only bars strictly before this one, which is what "no look-ahead" means here.
    past = [float(row["close"]) for row in series[index - window : index]]
    average = sum(past) / window
    today = float(series[index]["close"])
    signal[series[index]["date"]] = {"SPY": "1" if today > average else "0"}

# Reported alongside the signal so the test can prove *where* this ran rather than infer it.
# `/mnt` is empty only inside the mount namespace, and os.sep is "/" only on Linux.
import os

print(json.dumps({
    "signal": signal,
    "ran_on": {
        "sep": os.sep,
        "mnt": sorted(os.listdir("/mnt")) if os.path.isdir("/mnt") else "absent",
    },
}))
"""


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "generated.db")
    yield db
    db.close()


def bars() -> tuple[Bar, ...]:
    """A rising-then-falling series, so the crossover has something to react to."""
    out = []
    for index in range(300):
        level = 100 + index if index < 200 else 300 - (index - 200)
        out.append(
            Bar(
                symbol="SPY",
                timestamp=datetime.combine(
                    START + timedelta(days=index), datetime.min.time(), tzinfo=UTC
                ),
                open=str(level),
                high=str(level + 1),
                low=str(level - 1),
                close=str(level),
                volume="1000000",
                adjustment="1",
            )
        )
    return tuple(out)


def draft() -> HypothesisDraft:
    return HypothesisDraft(
        hypothesis_id="H-GEN-1",
        family_id="trend-following",
        question="Does a 20-day moving-average crossover on SPY earn a positive Sharpe?",
        prediction="Annualised Sharpe is at least 1.0.",
        null_hypothesis="The Sharpe is zero.",
        falsified_if="The Sharpe fails to clear the luck bar.",
        universe=("SPY",),
        features=("close_sma_20",),
        target="next-session excess return",
        windows=(
            DataWindow(
                dataset=DATASET,
                snapshot="2026-08-21",
                role=DataRole.PROTECTED_EVALUATION,
                start=START,
                end=date(2026, 8, 14),
            ),
        ),
        primary_estimand="annualised Sharpe against a null of zero",
        effect=EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("2.0"),
            minimum_practical=Decimal("1.0"),
            justification="Large enough that the sample can resolve it.",
            comparison=ComparisonStructure.SINGLE_SAMPLE,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        ),
        available_observations=2669,
        search_cardinality=1,
        search_origin=SearchOrigin.NO_DATA_DEPENDENT_SEARCH,
        confounders=("the trend regime dominating the sample",),
        basis=EvidenceBasis(citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE),
        proposed_by="claude-opus-5",
    )


def snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        dataset=DATASET,
        snapshot="2026-08-21",
        role="PROTECTED_EVALUATION",
        start=START,
        end=date(2026, 8, 14),
        observations=2669,
        content_hash="c" * 64,
        earliest_available_at=datetime.combine(START, datetime.min.time(), tzinfo=UTC),
        provider="sip",
        retrieved_at=NOW,
        transformation_version="v1",
    )


def test_a_registration_reaches_a_manifest_through_generated_code(
    database: Database, tmp_path: Path
) -> None:
    """#8's closure test, and the clause that matters is "without manually constructing the
    measurement result".

    Every earlier version of this pipeline took a `Measurement` written by trusted application
    code, so the number under test was supplied by the same hand that wrote the assertions. Here
    the Sharpe comes out of `BacktestEngine` after a signal that untrusted code computed inside
    a kernel-isolated sandbox.
    """
    source = MINER_SOURCE
    generated = GeneratedCode(
        source=source,
        sha256=hashlib.sha256(source.encode()).hexdigest(),
        response=_StubResponse(),  # type: ignore[arg-type]
    )

    history = bars()
    payload = {
        "SPY": [
            {
                "date": bar.timestamp.date().isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
            }
            for bar in history
        ]
    }

    # 1. Generated code runs behind the kernel boundary and produces a signal.
    run = run_generated_signal(
        WslSandbox(SandboxPolicy(wall_clock_seconds=90.0, memory_mb=256)), generated, payload
    )
    assert run.signal, "the generated code produced no signal"
    assert run.code.sha256 == generated.sha256
    # 280 signal dates is 300 bars minus the 20-bar warm-up.
    assert len(run.signal) == 280, len(run.signal)

    # Proof of *where* it ran, reported by the code itself rather than inferred.
    #
    # The first version of this assertion used a duration threshold -- "faster than 0.5s means
    # the sandbox was skipped". That was measuring WSL warmth, not isolation: a cold start takes
    # 3.6s and a warm one 0.3s, and both are genuinely sandboxed. A timing proxy that fails on a
    # warm machine and passes on a cold one says nothing about the property it names, which is
    # the exact anti-pattern this project keeps finding in its own tests.
    evidence = json.loads(run.stdout.strip().splitlines()[-1])["ran_on"]
    assert evidence["sep"] == "/", f"this did not run on Linux: {evidence}"
    assert evidence["mnt"] == [], (
        f"the Windows drives were visible to generated code: {evidence['mnt']}"
    )

    # 2. That signal is bound to the production engine. Nothing here computes a return.
    config = load_strategy_config(
        Path(__file__).resolve().parents[2] / "config" / "strategy-v1-2.yaml"
    )
    engine = BacktestEngine(config, initial_cash=Decimal("100000"))
    measurement = measurement_from_signal(engine, {"SPY": history}, run)

    # 3. Registration, compilation and execution, with the runner spending the holdout.
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registration = registry.register(draft(), now=NOW, critique=_cleared())

    plan = compile_experiment(
        registration, datasets=[snapshot()], execution_path=ExecutionPath.PRODUCTION_ENGINE
    )
    code_provenance, environment = _provenance()

    with database.transaction() as session:
        result = ExperimentRunner(
            HypothesisRegistry(session), manifests=tmp_path / "manifests"
        ).run(
            plan,
            registration,
            measurement,
            now=NOW,
            code=code_provenance,
            environment=environment,
            applied_costs={"slippage_bps": "1.1", "commission_per_order": "0"},
            design=ExperimentDesign(
                # Declared before the result is visible, which is what the runner enforces:
                # splits chosen after seeing the number are not splits, they are a search.
                walk_forward_splits=(
                    ExperimentPeriod(name="in-sample", start=START, end=date(2020, 1, 1)),
                    ExperimentPeriod(
                        name="out-of-sample", start=date(2020, 1, 2), end=date(2026, 8, 14)
                    ),
                ),
                neighborhood_grid={"window": ["10", "20", "40"]},
                ablations=("no-trend-filter",),
            ),
        )

    # 4. A manifest exists on disk, and the verdict was graded by the runner rather than reported
    #    by the measurement.
    assert result.manifest_path.exists()
    assert result.manifest.mode == "CONFIRMATORY"
    assert result.manifest.hypothesis_id == "H-GEN-1"
    # `verdict in set(OutcomeVerdict)` stood here and was a tautology -- every verdict is in the
    # set of verdicts, so it passed whatever the pipeline did, in the one test that is supposed
    # to prove the pipeline works.
    #
    # The specific verdict is INCONCLUSIVE, and that is the *correct* answer rather than a
    # disappointment. The plan requires four probes; this measurement runs none of them, so the
    # runner refuses to read the number as evidence either way. A generated experiment that
    # graded SURVIVED without a single falsification attempt having run would be the worst
    # outcome this repository could produce.
    assert result.outcome.verdict is OutcomeVerdict.INCONCLUSIVE, result.outcome.detail
    assert "did not run" in result.outcome.detail
    assert result.outcome.probes_run == (), "nothing ran, so nothing may be claimed"
    # The effect still reached the outcome. INCONCLUSIVE means "this cannot be read as
    # evidence", not "this was not measured" -- the number is recorded for the manifest.
    assert result.outcome.effect is not None
    # The execution path says the production engine, and it is true rather than declared: the
    # Sharpe in this result came out of `BacktestEngine.run`.
    assert plan.execution_path is ExecutionPath.PRODUCTION_ENGINE

    # 5. The bundle says which source computed the number (#18).
    #
    # `CodeProvenance.git_commit` describes the harness. For a generated experiment the input
    # that most determines the result is the model-authored source, and it is the one nobody
    # reviewed -- a manifest recording only the commit would describe everything about the run
    # except the part worth checking. The digest, the model and the prompt hash now travel with
    # it, so a later reader can tell two runs apart when the harness is identical and the
    # generated code is not.
    diagnostics = {
        key.removeprefix("diagnostic:"): value["value"]
        for key, value in result.manifest.results.items()
        if key.startswith("diagnostic:")
    }
    assert diagnostics["code_sha256"], "the manifest does not say which source ran"
    assert diagnostics["model"], "nor which model wrote it"
    assert diagnostics["prompt_template_hash"], "nor under which prompt"
    # It is the digest of the source that actually executed, not of something rebuilt later.
    assert diagnostics["code_sha256"] == generated.sha256

    # 6. The bundle carries what a rerun needs (#18).
    #
    # `reproduction_command` sat empty for weeks deliberately: `verify-manifest` checks
    # invariants and is not a re-execution, and a command in that field that does not reproduce
    # the result reads as reproducibility somebody verified. It is populated now because the
    # bundle finally carries the model-authored source rather than only its hash -- a digest
    # identifies code and cannot run it.
    assert result.manifest.reproduction_command, "no rerun is described"
    assert "reproduce-experiment" in result.manifest.reproduction_command
    # Keyed on the experiment id, not the manifest path: the path derives from the manifest
    # hash, so a path inside the manifest would change the hash that named it.
    assert result.manifest.experiment_id in result.manifest.reproduction_command

    inputs = result.manifest_path.parent / f"{result.manifest.experiment_id}.inputs"
    assert inputs.is_dir(), "the source a rerun needs is not beside the bundle"
    stored = (inputs / "experiment.py").read_text(encoding="utf-8")
    assert stored.strip(), "an empty source would reproduce nothing"
    assert hashlib.sha256(stored.encode("utf-8")).hexdigest() == generated.sha256, (
        "the stored source is not the one that produced this result"
    )


class _StubResponse:
    """Stands in for the model call. The sandbox is real; only the author is fixed."""

    spec = type("S", (), {"model": "stub-coder", "parameters_hash": "p" * 8})()

    def provenance(self):
        return type("P", (), {"model": "stub-coder@1", "prompt_template_hash": "t" * 8})()


def _cleared():
    from quantbot.research.critic import CriticVerdict, Critique

    return Critique(
        hypothesis_id="H-GEN-1",
        hypothesis_version=1,
        critic="deterministic",
        verdict=CriticVerdict.PROCEED,
        reasons=("no mechanical objection",),
        produced_at=NOW,
    )


def _provenance():
    from quantbot.research.manifest import CodeProvenance

    return (
        CodeProvenance(
            git_commit="0" * 40,
            dirty=False,
            configuration_hash="cfg-" + "0" * 12,
            # The claim the whole test exists to make true rather than declare.
            execution_path=ExecutionPath.PRODUCTION_ENGINE,
        ),
        capture_environment(host_class="test"),
    )


def test_a_measurement_cannot_choose_where_the_runner_writes(tmp_path: Path) -> None:
    """Input names come from a measurement, and for a generated experiment that is downstream
    of model-authored code.

    A name like `../../.env` would let it pick a destination outside the bundle. Refused rather
    than sanitised: a silently rewritten filename is one nobody can match back to the manifest
    that references it.
    """
    from quantbot.research.runner import ExperimentRunError, ExperimentRunner

    for hostile in ("../escape.py", "nested/experiment.py", r"..\windows.py"):
        with pytest.raises(ExperimentRunError, match="plain filename"):
            ExperimentRunner._write_inputs(tmp_path / "x.inputs", {hostile: "print(1)"})

    # A plain name is written, so the refusal is specific rather than a blanket one.
    ExperimentRunner._write_inputs(tmp_path / "ok.inputs", {"experiment.py": "print(1)"})
    assert (tmp_path / "ok.inputs" / "experiment.py").exists()
