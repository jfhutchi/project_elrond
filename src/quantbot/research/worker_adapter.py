"""One concrete external research worker, and the budget accounting it cannot avoid (#11).

`workers.py` defined `ResearchWorker`, `WorkerSpec`, the trust rule and the import path, and
nothing implemented the protocol. So the contract for delegating research existed and no research
could be delegated.

`SubprocessWorker` is the first implementation. It runs an external engine -- RD-Agent, Qlib, a
script -- as a child process that exchanges JSON on stdin and stdout, which is the lowest common
denominator every such tool already supports and the reason it is the right first target rather
than a vendor SDK.

**What this adapter refuses to do, and why each matters.**

*It cannot deploy anything.* The result is data. This module imports nothing from `quantbot.brokers`
or `quantbot.execution`, and the import-graph test enforces that in both directions. A worker's
output reaches a strategy only by a human registering a hypothesis, which is several gates away.

*It cannot spend a trial it did not disclose.* `record_worker_search` charges the worker's
reported cardinality to the family's `TRIALS` budget. A worker that will not report one is
`EXPLORATORY_ONLY` by `WorkerSpec.trust`, and there is no configuration that changes that -- the
whole point of #11's trust rule is that undisclosed search cannot be laundered into confirmatory
evidence by an engine nobody can audit.

*It cannot corrupt director state by failing.* Every failure mode -- a non-zero exit, a timeout,
unparseable output, output that is valid JSON but not a `WorkerResult` -- raises `WorkerFailure`.
The caller decides what that means for the task; the cycle's stage handler turns it into BLOCKED,
which is not a research finding.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.research.budget import BudgetGovernor, Resource
from quantbot.research.workers import (
    ResearchWorker,
    WorkerFailure,
    WorkerInput,
    WorkerResult,
    WorkerSpec,
    WorkerTrust,
)
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import search_runs


class SubprocessWorker:
    """Run an external engine as a child process exchanging JSON.

    Satisfies `ResearchWorker`. The command is supplied by the caller rather than discovered,
    because a research system that goes looking for executables to run is a different and much
    worse thing than one that runs what it was told to.
    """

    def __init__(
        self,
        spec: WorkerSpec,
        command: Sequence[str],
        *,
        timeout_seconds: float = 900.0,
        runner: object | None = None,
    ) -> None:
        if not command:
            raise ValueError("a subprocess worker needs a command to run")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._spec = spec
        self._command = tuple(command)
        self._timeout = timeout_seconds
        # Injected for tests, exactly as the HTTP transports do it: the production path is
        # exercised without spawning anything.
        self._runner = runner or subprocess.run

    @property
    def spec(self) -> WorkerSpec:
        return self._spec

    def run(self, request: WorkerInput, *, now: datetime) -> WorkerResult:
        """Invoke the worker and return its result, or raise. Never returns a partial one."""
        payload = request.model_dump_json()
        try:
            completed = self._runner(  # type: ignore[operator]
                self._command,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise WorkerFailure(
                f"{self._spec.name}@{self._spec.version} exceeded {self._timeout}s and was "
                "killed; a worker that does not finish has produced nothing, not a null result"
            ) from error
        except OSError as error:
            raise WorkerFailure(
                f"{self._spec.name}@{self._spec.version} could not be started"
            ) from error

        if completed.returncode != 0:
            # stderr is deliberately not interpolated. It is arbitrary text from an external
            # program, it can be enormous, and it may echo back the input manifest.
            raise WorkerFailure(
                f"{self._spec.name}@{self._spec.version} exited {completed.returncode}"
            )

        try:
            decoded = json.loads(completed.stdout)
        except ValueError as error:
            raise WorkerFailure(
                f"{self._spec.name}@{self._spec.version} did not return JSON"
            ) from error

        try:
            result = WorkerResult.model_validate(decoded)
        except ValidationError as error:
            # A worker that answers with the wrong shape has not produced a weaker result; it
            # has produced no result. Accepting a partial one is how an unvalidated field
            # becomes a claim nobody checked.
            raise WorkerFailure(
                f"{self._spec.name}@{self._spec.version} returned JSON that is not a WorkerResult"
            ) from error

        if result.worker.name != self._spec.name or result.worker.version != self._spec.version:
            raise WorkerFailure(
                f"{self._spec.name}@{self._spec.version} returned a result attributed to "
                f"{result.worker.name}@{result.worker.version}; provenance a worker writes about "
                "itself has to match the worker that was invoked"
            )
        return result


def record_worker_search(
    governor: BudgetGovernor,
    result: WorkerResult,
    *,
    budget_key: str,
    now: datetime,
) -> Decimal:
    """Charge a worker's disclosed search to the family's trial budget (#11, #14).

    External search volume has to enter statistical-budget accounting or an engine that
    evaluated five thousand candidates costs the project nothing, and the luck bar every later
    hypothesis is judged against stays where it was. That is the specific way a factor miner
    launders thousands of trials into a few clean-looking results.

    A worker that discloses nothing spends nothing here -- and cannot produce confirmatory
    evidence either, by `WorkerSpec.trust`. Charging a guessed number instead would be worse
    than charging none: it would put an invented figure into the multiple-testing correction.
    """
    if result.worker.trust is WorkerTrust.EXPLORATORY_ONLY:
        return Decimal("0")
    if result.search_cardinality is None:
        raise WorkerFailure(
            f"{result.worker.name}@{result.worker.version} is configured as disclosing its "
            "search cardinality and returned none; the trust rule and the result disagree, and "
            "guessing a number would put an invented figure into the luck bar"
        )
    spend = Decimal(result.search_cardinality)
    governor.spend(
        budget_key,
        Resource.TRIALS,
        spend,
        task=f"{result.worker.name}@{result.worker.version}",
        now=now,
        note=_reason(result),
    )
    return spend


def record_search_run(
    session: Session,
    result: WorkerResult,
    *,
    search_id: str,
) -> str:
    """Persist a worker's disclosed search as a durable record (#23).

    This is what makes `SearchOrigin.MEASURED` reachable. It was refused outright while nothing
    emitted search telemetry, because accepting it on trust would have reopened the laundering
    path one enum value across from where #23 closed it -- an agent forbidden from self-reporting
    a count could simply have relabelled it as measured. A row here is the evidence that was
    missing.

    Append-only. A search that happened cannot un-happen, and a record that could be revised
    downward would be exactly the understatement the multiple-testing budget exists to prevent,
    so a second write under the same id is refused rather than merged.
    """
    if result.search_cardinality is None:
        raise WorkerFailure(
            f"{result.worker.name}@{result.worker.version} disclosed no search cardinality; "
            "there is nothing to record, and a record with a guessed number would be worse "
            "than no record at all"
        )
    existing = session.execute(
        select(search_runs.c.search_id).where(search_runs.c.search_id == search_id)
    ).one_or_none()
    if existing is not None:
        raise WorkerFailure(
            f"search run {search_id} is already recorded; search records are append-only "
            "because a count that can be revised downward is a count that can be understated"
        )

    request = result.request
    session.execute(
        search_runs.insert().values(
            search_id=search_id,
            actor=result.worker.name,
            actor_version=result.worker.version,
            candidates_evaluated=result.search_cardinality,
            dataset=request.datasets[0],
            # The role the worker was permitted, which `WorkerInput` already restricts to
            # discovery or validation -- a worker is never handed a protected window.
            data_role=sorted(role.value for role in request.data_roles)[0],
            start_date=result.started_at.date().isoformat(),
            end_date=result.finished_at.date().isoformat(),
            started_at=encode_utc(result.started_at),
            finished_at=encode_utc(result.finished_at),
            configuration_hash=result.worker.configuration_hash,
            detail_json=json.dumps(
                {"mandate": request.mandate, "trial_budget": request.trial_budget},
                sort_keys=True,
            ),
        )
    )
    return search_id


def measured_cardinality(session: Session, search_id: str) -> int:
    """Read a recorded search, so a registration derives its count instead of asserting it.

    Raises when the record does not exist. A `MEASURED` origin naming a search nobody recorded
    is a stronger claim on less evidence than the self-report it replaced.
    """
    row = session.execute(
        select(search_runs.c.candidates_evaluated).where(search_runs.c.search_id == search_id)
    ).one_or_none()
    if row is None:
        raise WorkerFailure(
            f"no search run {search_id} is recorded, so its cardinality cannot be measured"
        )
    return int(row[0])


def _reason(result: WorkerResult) -> str:
    return (
        f"{result.worker.name}@{result.worker.version} evaluated "
        f"{result.search_cardinality} candidates"
    )


def _assert_protocol(worker: SubprocessWorker) -> ResearchWorker:
    """Compile-time proof that the adapter satisfies the protocol it claims to."""
    return worker


__all__ = [
    "SubprocessWorker",
    "measured_cardinality",
    "record_search_run",
    "record_worker_search",
]
