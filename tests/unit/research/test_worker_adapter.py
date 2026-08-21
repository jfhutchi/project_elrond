"""The subprocess worker: what it accepts from an external engine, and what it charges for it."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantbot.research.budget import BudgetGovernor, Cap, Resource
from quantbot.research.registry import DataRole
from quantbot.research.worker_adapter import (
    SubprocessWorker,
    record_search_run,
    record_worker_search,
    recorded_artifacts,
)
from quantbot.research.workers import (
    WorkerCapability,
    WorkerFailure,
    WorkerInput,
    WorkerSpec,
    WorkerTrust,
)
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "worker.db")
    yield db
    db.close()


@dataclass
class FakeCompleted:
    returncode: int
    stdout: str
    stderr: str = ""


def spec(*, discloses: bool = True, name: str = "rd-agent", version: str = "0.4.1") -> WorkerSpec:
    return WorkerSpec(
        name=name,
        version=version,
        capabilities=frozenset({WorkerCapability.FACTOR_MINING}),
        configuration_hash="cfg-abc",
        discloses_search_cardinality=discloses,
    )


def request() -> WorkerInput:
    return WorkerInput(
        mandate="search for factors on the discovery window",
        datasets=("sip-us-equities-daily",),
        data_roles=frozenset({DataRole.DISCOVERY}),
        trial_budget=5000,
    )


def result_payload(worker: WorkerSpec, *, cardinality: int | None = 4200) -> str:
    return json.dumps(
        {
            "worker": worker.model_dump(mode="json"),
            "request": request().model_dump(mode="json"),
            "started_at": NOW.isoformat(),
            "finished_at": NOW.isoformat(),
            "search_cardinality": cardinality,
            "metrics": {"best_ic": "0.031"},
            "artifacts": {"factors.parquet": "sha256:abc"},
        }
    )


def runner_returning(output: str, *, code: int = 0):
    def run(command, **kwargs):
        run.seen = (command, kwargs)  # type: ignore[attr-defined]
        return FakeCompleted(returncode=code, stdout=output)

    return run


def test_a_worker_result_is_returned_with_its_provenance() -> None:
    """#11: Elrond can invoke one external worker and receive structured results."""
    worker_spec = spec()
    worker = SubprocessWorker(
        worker_spec, ["rd-agent", "run"], runner=runner_returning(result_payload(worker_spec))
    )

    result = worker.run(request(), now=NOW)

    assert result.worker.name == "rd-agent"
    assert result.worker.version == "0.4.1"
    assert result.search_cardinality == 4200
    assert result.metrics["best_ic"] == "0.031"
    # Traceable to worker version, configuration and the input it was given.
    assert result.worker.configuration_hash == "cfg-abc"
    assert result.request.datasets == ("sip-us-equities-daily",)


def test_a_result_attributed_to_a_different_worker_is_refused() -> None:
    """Provenance a worker writes about itself has to match the worker that was invoked.

    Without this, an engine could return a result claiming to be a more trusted worker and
    inherit its confirmatory eligibility.
    """
    invoked = spec(name="rd-agent")
    impostor = spec(name="trusted-engine")
    worker = SubprocessWorker(
        invoked, ["rd-agent"], runner=runner_returning(result_payload(impostor))
    )

    with pytest.raises(WorkerFailure, match="attributed to"):
        worker.run(request(), now=NOW)


def test_every_failure_mode_raises_rather_than_returning_a_partial_result() -> None:
    """#11: an external worker failure must not corrupt director state.

    Each of these is a way an external program disappoints. None of them may produce a
    `WorkerResult`, because a partial result is a claim nobody checked.
    """
    worker_spec = spec()

    non_zero = SubprocessWorker(worker_spec, ["x"], runner=runner_returning("{}", code=2))
    with pytest.raises(WorkerFailure, match="exited 2"):
        non_zero.run(request(), now=NOW)

    prose = SubprocessWorker(worker_spec, ["x"], runner=runner_returning("it went well!"))
    with pytest.raises(WorkerFailure, match="did not return JSON"):
        prose.run(request(), now=NOW)

    wrong_shape = SubprocessWorker(
        worker_spec, ["x"], runner=runner_returning('{"summary": "found some factors"}')
    )
    with pytest.raises(WorkerFailure, match="not a WorkerResult"):
        wrong_shape.run(request(), now=NOW)


def test_a_timeout_is_a_failure_and_never_a_null_result() -> None:
    """A worker that does not finish has produced nothing.

    Reading a timeout as "it searched and found nothing" would turn an infrastructure problem
    into a research finding, which is the same error class as a crashed stage being recorded as
    REFUTED.
    """

    def timing_out(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=1.0)

    worker = SubprocessWorker(spec(), ["slow"], timeout_seconds=1.0, runner=timing_out)

    with pytest.raises(WorkerFailure, match="has produced nothing"):
        worker.run(request(), now=NOW)


def test_the_error_never_carries_the_workers_stderr() -> None:
    """It is arbitrary text from an external program and may echo the input manifest back."""

    def noisy(command, **kwargs):
        return FakeCompleted(returncode=1, stdout="", stderr="SECRET_TOKEN=sk-abc123 crashed")

    worker = SubprocessWorker(spec(), ["x"], runner=noisy)

    with pytest.raises(WorkerFailure) as failure:
        worker.run(request(), now=NOW)

    assert "SECRET_TOKEN" not in str(failure.value)


def test_disclosed_external_search_is_charged_to_the_family_budget(database: Database) -> None:
    """#11: external search volume enters statistical-budget accounting.

    Otherwise an engine that evaluated 4,200 candidates costs the project nothing and the luck
    bar every later hypothesis is judged against stays where it was -- which is precisely how a
    factor miner launders thousands of trials into a few clean-looking results.
    """
    worker_spec = spec()
    worker = SubprocessWorker(
        worker_spec, ["x"], runner=runner_returning(result_payload(worker_spec))
    )
    result = worker.run(request(), now=NOW)

    with database.transaction() as session:
        governor = BudgetGovernor(
            session,
            caps=[Cap(budget_key="factors", resource=Resource.TRIALS, limit=Decimal("10000"))],
        )
        spent = record_worker_search(governor, result, budget_key="factors", now=NOW)
        remaining = governor.spent("factors", Resource.TRIALS)

    assert spent == Decimal("4200")
    assert remaining == Decimal("4200"), "the whole search, not the candidates it returned"


def test_a_worker_that_discloses_nothing_spends_nothing_and_stays_exploratory(
    database: Database,
) -> None:
    """The trust rule, end to end.

    A worker that will not report its search cannot produce confirmatory evidence, and charging
    it a guessed number would be worse than charging none -- an invented figure in the
    multiple-testing correction is worse than a known gap in it.
    """
    silent = spec(discloses=False)
    assert silent.trust is WorkerTrust.EXPLORATORY_ONLY

    worker = SubprocessWorker(
        silent, ["x"], runner=runner_returning(result_payload(silent, cardinality=None))
    )
    result = worker.run(request(), now=NOW)

    with database.transaction() as session:
        governor = BudgetGovernor(
            session,
            caps=[Cap(budget_key="factors", resource=Resource.TRIALS, limit=Decimal("10000"))],
        )
        spent = record_worker_search(governor, result, budget_key="factors", now=NOW)

    assert spent == Decimal("0")
    assert result.worker.trust is WorkerTrust.EXPLORATORY_ONLY


def test_a_worker_claiming_disclosure_but_reporting_none_is_refused(database: Database) -> None:
    """The trust rule and the result disagreeing is a defect, not a zero."""
    worker_spec = spec(discloses=True)
    worker = SubprocessWorker(
        worker_spec, ["x"], runner=runner_returning(result_payload(worker_spec, cardinality=None))
    )
    result = worker.run(request(), now=NOW)

    with database.transaction() as session:
        governor = BudgetGovernor(
            session,
            caps=[Cap(budget_key="factors", resource=Resource.TRIALS, limit=Decimal("10000"))],
        )
        with pytest.raises(WorkerFailure, match="guessing a number"):
            record_worker_search(governor, result, budget_key="factors", now=NOW)


def test_a_worker_is_never_handed_protected_data() -> None:
    """`WorkerInput` refuses it, so the adapter cannot pass it on."""
    with pytest.raises(ValueError, match="may not be handed"):
        WorkerInput(
            mandate="search",
            datasets=("sip-us-equities-daily",),
            data_roles=frozenset({DataRole.PROTECTED_EVALUATION}),
            trial_budget=10,
        )


def test_what_a_worker_produced_is_durable_not_only_that_it_searched(
    database: Database,
) -> None:
    """#11's last box: result and artifact provenance imports into durable storage.

    `search_runs` recorded that a search happened and how large it was, which is what the
    multiple-testing budget needs. It said nothing about what came out. An artifact found on
    disk later could not be matched to the run that made it, and an artifact that cannot be
    matched is one nobody should treat as evidence.
    """
    worker_spec = spec()
    payload = json.loads(result_payload(worker_spec))
    payload["artifacts"] = {"factors.parquet": "sha256:abc", "report.md": "sha256:def"}
    worker = SubprocessWorker(worker_spec, ["x"], runner=runner_returning(json.dumps(payload)))
    outcome = worker.run(request(), now=NOW)

    with database.transaction() as session:
        record_search_run(session, outcome, search_id="SR-ARTIFACTS")

    with database.transaction() as session:
        stored = recorded_artifacts(session, "SR-ARTIFACTS")

    assert stored == {"factors.parquet": "sha256:abc", "report.md": "sha256:def"}


def test_an_unrecorded_search_produced_nothing_that_anybody_knows_of(
    database: Database,
) -> None:
    """Raises rather than returning {}.

    An empty mapping says "this run produced nothing". A run nobody logged produced nothing
    *that we know of*, which is a different claim -- and it is the one that matters when
    deciding whether a file sitting on disk counts as evidence.
    """
    with database.transaction() as session:
        with pytest.raises(WorkerFailure, match="nothing is known"):
            recorded_artifacts(session, "SR-NEVER-HAPPENED")
