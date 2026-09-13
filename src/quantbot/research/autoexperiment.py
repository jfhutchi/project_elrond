"""Run a proposed question's pre-registered measurement under kernel isolation, and grade it.

`proposer.py` produces a `ResearchTask`; `cli.py` wires a `PROPOSED` handler that advances it to
`SCOUTING` once capacity and novelty agree. This module is the `SCOUTING` handler for the tasks the
proposer opened, and only those: `applies_to` declines anything without a proposal record, so a
hand-opened task in `SCOUTING` stays where it is.

**Four separations, and each one is the difference between evidence and a story.**

*The measurement reads only the exploratory range.* The first version of this module loaded every
stored session, including the ones the capacity gate had just counted as the clean confirmatory
sessions, while its docstring said it ran "on the exploratory range". The panel now ends at
`ProposalTemplate.exploratory_through`: the last session any search consumed, or the last one this
question has already seen, whichever is later. A session after that boundary is never loaded, so
it cannot reach the sandbox, and a question with no exploratory range is not measured at all.

*The measurement is catalogue code, not model code.* The model chose which question to ask; the
arithmetic that answers it was written in advance and is content-addressed. A model that writes its
own measurement writes its own success criterion with it.

*It still runs in the sandbox.* The script is machine-assembled per run, data included, and nobody
reviews the particular one that executes, which is the case `mine_for_anomalies` and `codegen`
refuse an unenforced backend for. `select_isolated_backend()` is used rather than a directly
constructed runner, so a host with no OS-enforced isolation gets a refusal and not a quieter
sandbox.

*Grading happens out here, and the exploratory statistic does not decide it.* The verdict is the
power gate: whether the clean confirmatory observations could let the pre-registered effect clear
`luck_threshold` at the project's cumulative trial count. That is computable without the
measurement, and the reason text says so. The exploratory statistic is recorded -- as a
`search_runs` row, so a later registration built on it has a measured search to cite, and as an
`EXPLORATORY_ONLY` finding, so memory can answer what this range showed -- and it is never read
as confirmation or as refutation.

**Why the honest terminal state here is usually `UNDERPOWERED`.** The only dataset on this host with
both capacity and observations is `us-equities-daily`, which cycles 2-10 spent 68 trials on. The
bar at that burden is 2.91, and the clean confirmatory sessions are the handful that have happened
since the searching stopped. `UNDERPOWERED` is deliberately terminal and deliberately not
`REFUTED`: a question the data could not resolve was never tested.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.research.capacity import DATA_SOURCES, DataSource
from quantbot.research.cycle import StageResult
from quantbot.research.director import ResearchTask, TaskState
from quantbot.research.memory import (
    RecordKind,
    Relation,
    ResearchMemory,
    ResearchRecord,
    Verdict,
)
from quantbot.research.mining import IsolatedSandbox
from quantbot.research.power import (
    ComparisonStructure,
    DependenceAssumptions,
    EffectSpecification,
    luck_threshold,
    minimum_detectable_effect,
    observations_required,
)
from quantbot.research.proposer import (
    DatasetReadiness,
    ProposalTemplate,
    measure_readiness,
    proposal_for_task,
    template_by_id,
)
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.sandbox import SandboxPolicy, SandboxResult, select_isolated_backend
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import bars, search_runs

#: Bumped when the measurement scripts, the panel boundary or the grading rule change. Recorded on
#: every result, because a statistic is only comparable with another produced the same way.
#:
#: 2: the panel ends at the exploratory boundary; grading uses the project-wide trial count; the
#: exploratory statistic is persisted and no longer described as sizing anything.
EXPERIMENT_VERSION = 2

#: How this stage appears in `search_runs.actor` and on the records it writes.
ACTOR = "autoexperiment"

#: The series every measurement here is asked about. Declared rather than inferred so a panel
#: missing it fails closed instead of silently benchmarking against whatever sorted first.
BENCHMARK_SYMBOL = "SPY"

#: Enough for a 300-session panel of two dozen symbols and the arithmetic over it, and small
#: enough that a runaway is stopped in under a second. The wall clock is generous by comparison
#: so that a memory breach is reported as a memory breach.
POLICY = SandboxPolicy(wall_clock_seconds=90.0, memory_mb=256)

#: Observations a lead-lag measurement loses inside a range of sessions: one to differencing
#: closes into returns, one to shifting the target forward a session.
LEAD_LAG_LOSS = 2

#: How much of the child's output is kept with the search record. The report is one JSON line; the
#: cap exists so a misbehaving script cannot write megabytes into the ledger.
_OUTPUT_CAP = 4000


class ExperimentRefused(RuntimeError):
    """Raised when a measurement must not run, or its result cannot be believed."""


@dataclass(frozen=True, slots=True)
class Panel:
    """The immutable observation set one measurement sees."""

    identity: str
    symbols: tuple[str, ...]
    dates: tuple[date, ...]
    #: `closes[i]` is the row for `dates[i]`, aligned to `symbols`. Only complete sessions are
    #: kept: a session missing a symbol would change the cross-section it is compared against.
    closes: tuple[tuple[float, ...], ...]

    @property
    def digest(self) -> str:
        """Content address of the observations, so a result can name the data that produced it."""
        material = json.dumps(
            {
                "identity": self.identity,
                "symbols": list(self.symbols),
                "dates": [day.isoformat() for day in self.dates],
                "closes": [list(row) for row in self.closes],
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def as_payload(self) -> str:
        return json.dumps(
            {
                "symbols": list(self.symbols),
                "dates": [day.isoformat() for day in self.dates],
                "closes": [list(row) for row in self.closes],
                "benchmark": BENCHMARK_SYMBOL,
            },
            sort_keys=True,
        )


def load_panel(session: Session, source: DataSource, *, through: date) -> Panel:
    """Assemble the complete-session panel for one source, oldest first, ending at `through`.

    `through` is inclusive and has no default. It is the boundary between what may be measured and
    what is being kept for confirmation, and a default would be a place for that decision to be
    made by accident -- which is how the first version read the clean sessions it was meant to keep.

    Sessions missing any symbol are dropped rather than padded. A cross-sectional statistic over
    a varying universe is a different statistic each day, and filling the gap with a stale close
    would invent a zero return on exactly the days the vendor had trouble.
    """
    if not source.observation_prefixes:
        raise ExperimentRefused(
            f"{source.dataset} keeps no local observations, so there is nothing to measure"
        )
    rows = session.execute(
        select(bars.c.symbol, bars.c.timestamp, bars.c.close).where(
            or_(*(bars.c.provider.startswith(prefix) for prefix in source.observation_prefixes))
        )
    ).all()
    by_date: dict[date, dict[str, float]] = {}
    for symbol, timestamp, close in rows:
        day = decode_utc(str(timestamp)).astimezone(XNYS_TIMEZONE).date()
        if day > through:
            continue
        by_date.setdefault(day, {})[str(symbol)] = float(close)

    if not by_date:
        raise ExperimentRefused(f"{source.dataset} has no observations on or before {through}")

    # The universe is the set of symbols present on every session considered, so the
    # cross-section is the same width throughout.
    universe = set.intersection(*(set(row) for row in by_date.values()))
    if BENCHMARK_SYMBOL not in universe:
        raise ExperimentRefused(
            f"{BENCHMARK_SYMBOL} is not present in every session of {source.dataset}, so there "
            "is no consistent benchmark to measure against"
        )
    symbols = tuple(sorted(universe))
    days = tuple(sorted(by_date))
    closes = tuple(tuple(by_date[day][symbol] for symbol in symbols) for day in days)
    return Panel(identity=source.observation_identity, symbols=symbols, dates=days, closes=closes)


# --- the measurements ----------------------------------------------------------------------------

#: Shared arithmetic, injected ahead of the per-measurement tail. Written once so every
#: measurement agrees about what a return is.
_PREFIX = '''import json, math, statistics

PANEL = json.loads(PAYLOAD)
SYMBOLS = PANEL["symbols"]
DATES = PANEL["dates"]
CLOSES = PANEL["closes"]
BENCHMARK = PANEL["benchmark"]
INDEX = SYMBOLS.index(BENCHMARK)

RETURNS = []
for row in range(1, len(CLOSES)):
    previous, current = CLOSES[row - 1], CLOSES[row]
    RETURNS.append([
        (current[column] / previous[column]) - 1.0 if previous[column] else 0.0
        for column in range(len(SYMBOLS))
    ])

DISPERSION = [statistics.pstdev(row) for row in RETURNS]
BREADTH = [sum(1 for value in row if value > 0.0) / len(row) for row in RETURNS]
BENCHMARK_RETURNS = [row[INDEX] for row in RETURNS]


def correlation(left, right):
    """Pearson correlation and its t-statistic, with the sample size that produced them."""
    count = len(left)
    if count < 4:
        return {"observations": count, "statistic": 0.0, "t_statistic": 0.0, "degenerate": True}
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    spread_left = math.sqrt(sum((a - mean_left) ** 2 for a in left))
    spread_right = math.sqrt(sum((b - mean_right) ** 2 for b in right))
    if spread_left == 0.0 or spread_right == 0.0:
        return {"observations": count, "statistic": 0.0, "t_statistic": 0.0, "degenerate": True}
    value = numerator / (spread_left * spread_right)
    value = max(-0.999999, min(0.999999, value))
    t = value * math.sqrt((count - 2) / (1.0 - value * value))
    return {
        "observations": count,
        "statistic": value,
        "t_statistic": t,
        "degenerate": False,
    }


def emit(measurement_id, signal, target):
    report = correlation(signal, target)
    report["measurement_id"] = measurement_id
    report["sessions"] = len(DATES)
    report["universe"] = len(SYMBOLS)
    report["first_session"] = DATES[0]
    report["last_session"] = DATES[-1]
    print(json.dumps(report, sort_keys=True))
'''

#: Each tail names its own signal and forward target. Kept to one line of intent apiece so the
#: claim a measurement tests is readable next to the question the template asks.
_TAILS: Mapping[str, str] = {
    # Dispersion today against the size of tomorrow's benchmark move.
    "dispersion_forward_absolute_move": (
        'emit("dispersion_forward_absolute_move", DISPERSION[:-1],\n'
        "     [abs(value) for value in BENCHMARK_RETURNS[1:]])\n"
    ),
    # Breadth today against breadth tomorrow.
    "breadth_autocorrelation": (
        'emit("breadth_autocorrelation", BREADTH[:-1], BREADTH[1:])\n'
    ),
    # Dispersion today against the signed benchmark return tomorrow.
    "dispersion_forward_signed_return": (
        'emit("dispersion_forward_signed_return", DISPERSION[:-1], BENCHMARK_RETURNS[1:])\n'
    ),
    # The size of today's benchmark move against the size of tomorrow's.
    "benchmark_absolute_move_persistence": (
        'emit("benchmark_absolute_move_persistence",\n'
        "     [abs(value) for value in BENCHMARK_RETURNS[:-1]],\n"
        "     [abs(value) for value in BENCHMARK_RETURNS[1:]])\n"
    ),
}

MEASUREMENTS: tuple[str, ...] = tuple(sorted(_TAILS))


def build_script(measurement_id: str, panel: Panel) -> str:
    """Assemble the exact source that will run, data included, for content addressing."""
    tail = _TAILS.get(measurement_id)
    if tail is None:
        raise ExperimentRefused(
            f"{measurement_id!r} is not a known measurement; a template naming one that does not "
            "exist is a catalogue defect and must not silently run something else"
        )
    payload = json.dumps(panel.as_payload())
    return f"PAYLOAD = {payload}\n{_PREFIX}\n{tail}"


# --- running and grading ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MeasurementResult:
    """What one sandboxed measurement produced, and under what conditions."""

    measurement_id: str
    observations: int
    statistic: Decimal
    t_statistic: Decimal
    degenerate: bool
    sessions: int
    universe: int
    first_session: date
    last_session: date
    panel_digest: str
    script_sha256: str
    backend: str
    duration_seconds: float
    peak_memory_mb: float | None
    stdout: str
    stderr: str

    def as_detail(self) -> dict[str, str]:
        return {
            "measurement_id": self.measurement_id,
            "observations": str(self.observations),
            "statistic": str(self.statistic),
            "t_statistic": str(self.t_statistic),
            "sessions": str(self.sessions),
            "universe": str(self.universe),
            "first_session": self.first_session.isoformat(),
            "last_session": self.last_session.isoformat(),
            "panel_sha256": self.panel_digest,
            "script_sha256": self.script_sha256,
            "sandbox_backend": self.backend,
            "sandbox_wall_clock_seconds": str(POLICY.wall_clock_seconds),
            "sandbox_memory_mb": str(POLICY.memory_mb),
            "sandbox_network_enabled": str(POLICY.network_enabled).lower(),
            "duration_seconds": f"{self.duration_seconds:.3f}",
            "peak_memory_mb": "unmeasured"
            if self.peak_memory_mb is None
            else str(self.peak_memory_mb),
        }


def _parse(result: SandboxResult, measurement_id: str) -> Mapping[str, object]:
    """Read the child's report, refusing anything that is not the report we asked for."""
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("measurement_id") == measurement_id:
            return parsed
    raise ExperimentRefused(
        f"the sandboxed measurement printed no {measurement_id} report, so nothing about it is "
        "known; a run whose output cannot be read has not produced evidence"
    )


def run_measurement(
    template: ProposalTemplate,
    panel: Panel,
    *,
    sandbox: IsolatedSandbox | None = None,
) -> MeasurementResult:
    """Execute the template's measurement under OS-enforced isolation.

    `sandbox` is injected for tests. Left unset, `select_isolated_backend()` chooses, and raises
    when no OS-enforced backend exists -- which is the correct answer, not a reason to reach for
    `SandboxRunner`.
    """
    backend: IsolatedSandbox = (
        sandbox if sandbox is not None else select_isolated_backend(POLICY)
    )
    if not backend.os_enforced:
        raise ExperimentRefused(
            "a research measurement requires an OS-enforced sandbox; this backend reports it is "
            "not one, and a measurement that could reach the ledger is not evidence about markets"
        )
    source = build_script(template.measurement_id, panel)
    result = backend.run(source)
    if result.terminated_reason is not None:
        raise ExperimentRefused(
            f"the measurement was stopped ({result.terminated_reason}); a computation that did "
            "not finish has an unknown result and must not be read as one"
        )
    if not result.ok:
        raise ExperimentRefused(
            f"the measurement failed with exit code {result.exit_code}: "
            f"{result.stderr.strip()[-300:]}"
        )
    report = _parse(result, template.measurement_id)
    try:
        observations = int(report["observations"])  # type: ignore[call-overload]
        statistic = Decimal(str(report["statistic"]))
        t_statistic = Decimal(str(report["t_statistic"]))
        degenerate = bool(report["degenerate"])
        sessions = int(report["sessions"])  # type: ignore[call-overload]
        universe = int(report["universe"])  # type: ignore[call-overload]
        first_session = date.fromisoformat(str(report["first_session"]))
        last_session = date.fromisoformat(str(report["last_session"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise ExperimentRefused(
            f"the measurement report is missing or malformed: {error}"
        ) from error
    reported = (first_session, last_session, sessions)
    if reported != (panel.dates[0], panel.dates[-1], len(panel.dates)):
        # The child reports the range it read. A report that disagrees with the panel it was given
        # describes some other data, and grading it would attach a statistic to the wrong sessions.
        raise ExperimentRefused(
            f"the measurement reported sessions {first_session}..{last_session} ({sessions}) but "
            f"was given {panel.dates[0]}..{panel.dates[-1]} ({len(panel.dates)})"
        )

    return MeasurementResult(
        measurement_id=template.measurement_id,
        observations=observations,
        statistic=statistic,
        t_statistic=t_statistic,
        degenerate=degenerate,
        sessions=sessions,
        universe=universe,
        first_session=first_session,
        last_session=last_session,
        panel_digest=panel.digest,
        script_sha256=result.script_sha256,
        backend=type(backend).__name__,
        duration_seconds=result.duration_seconds,
        peak_memory_mb=result.peak_memory_mb,
        stdout=result.stdout,
        stderr=result.stderr,
    )


@dataclass(frozen=True, slots=True)
class Grade:
    """The deterministic verdict, and every number that produced it."""

    next_state: TaskState
    reason: str
    cumulative_trials: int
    luck_bar: Decimal
    observations_required_for_confirmation: int
    confirmatory_observations_available: int
    #: Smallest effect the clean confirmatory observations could separate from zero at this bar.
    #: `None` when there are too few for the question to have an answer.
    minimum_detectable_effect: Decimal | None

    def as_detail(self) -> dict[str, str]:
        return {
            "cumulative_trials": str(self.cumulative_trials),
            "luck_threshold_t": str(self.luck_bar),
            "observations_required": str(self.observations_required_for_confirmation),
            "confirmatory_observations_available": str(self.confirmatory_observations_available),
            "minimum_detectable_effect": "undefined"
            if self.minimum_detectable_effect is None
            else str(self.minimum_detectable_effect),
            "verdict_basis": "power gate on clean confirmatory observations",
        }


def _specification(template: ProposalTemplate) -> EffectSpecification:
    return EffectSpecification(
        estimand=template.estimand,
        expected=template.expected_effect,
        minimum_practical=template.minimum_practical_effect,
        justification=template.rationale,
        comparison=ComparisonStructure.SINGLE_SAMPLE,
        dependence=DependenceAssumptions(cluster_size=template.cluster_size),
    )


def confirmatory_observations(clean_sessions: int) -> int:
    """Lead-lag observations that fit entirely inside the clean sessions.

    Conservative on purpose. The first clean session's return could be taken against the last
    exploratory close, which would add one; not doing so means confirmation never borrows a
    session from the side of the boundary that has already been seen.
    """
    return max(0, clean_sessions - LEAD_LAG_LOSS)


def grade(
    template: ProposalTemplate,
    exploratory: MeasurementResult | None,
    *,
    cumulative_trials: int,
    confirmatory_observations: int,
) -> Grade:
    """Decide the task's fate from the pre-registered rule and the measured state.

    The bar is `luck_threshold(cumulative_trials)`, and the caller passes the project's burden
    *including* the trial a confirmation would add: the sixty-ninth question asked of a range is
    not judged as though it were the first.

    The sample needed is sized on the **pre-registered** effect, never on the observed one. Sizing
    on what was measured would move the bar to wherever the result landed, which is the failure
    the registration exists to prevent -- and it is why the exploratory statistic, when there is
    one, is reported beside the verdict and never consulted by it.
    """
    specification = _specification(template)
    bar = luck_threshold(cumulative_trials)
    required = observations_required(specification, bar)
    try:
        detectable: Decimal | None = minimum_detectable_effect(
            specification, confirmatory_observations, bar
        )
    except ValueError:
        detectable = None

    def verdict(state: TaskState, reason: str) -> Grade:
        return Grade(
            next_state=state,
            reason=reason,
            cumulative_trials=cumulative_trials,
            luck_bar=bar,
            observations_required_for_confirmation=required,
            confirmatory_observations_available=confirmatory_observations,
            minimum_detectable_effect=detectable,
        )

    if exploratory is not None and exploratory.degenerate:
        return verdict(
            TaskState.BLOCKED,
            "the exploratory measurement produced a degenerate statistic (a constant series or "
            "too few observations), so the instrument, not the question, needs looking at",
        )

    if exploratory is None:
        measured = (
            "No exploratory range exists for this question, so nothing was measured and no "
            "data was read."
        )
    else:
        measured = (
            f"The exploratory measurement over {exploratory.first_session}.."
            f"{exploratory.last_session} ({exploratory.observations} observations, none after "
            f"the exploratory boundary) returned {exploratory.measurement_id}="
            f"{exploratory.statistic} (t={exploratory.t_statistic}). It is recorded as "
            "exploratory evidence only: it did not decide this verdict, it cannot be cited as "
            "confirmation, and it is not a refutation."
        )

    if confirmatory_observations < required:
        return verdict(
            TaskState.UNDERPOWERED,
            f"confirmation needs {required} observations for the pre-registered effect of "
            f"{template.expected_effect} to clear a {bar} luck bar at {cumulative_trials} "
            f"cumulative trials, and {confirmatory_observations} clean confirmatory "
            f"observation(s) exist. {measured}",
        )

    return verdict(
        TaskState.CRITIQUE,
        f"{confirmatory_observations} clean confirmatory observation(s) meet the {required} the "
        f"pre-registered effect needs at a {bar} bar, so the claim goes to critique and "
        f"registration before any confirmatory data is read. {measured}",
    )


# --- the stage -----------------------------------------------------------------------------------


def _blocked(reason: str, **detail: str) -> StageResult:
    return StageResult(
        next_state=TaskState.BLOCKED,
        reason=reason,
        detail={"experiment_version": str(EXPERIMENT_VERSION), **detail},
    )


class AutoExperimentStage:
    """The `SCOUTING` handler for proposer-opened tasks. See the module docstring."""

    def __init__(
        self,
        session: Session,
        *,
        sandbox: IsolatedSandbox | None = None,
        now: datetime | None = None,
        code_commit: str = "unknown",
    ) -> None:
        self._session = session
        self._sandbox = sandbox
        self._now = now
        self._code_commit = code_commit

    def applies_to(self, task: ResearchTask) -> bool:
        """Only a task opened by the proposer has a pre-registered measurement to run."""
        return proposal_for_task(self._session, task.task_id) is not None

    def __call__(self, task: ResearchTask) -> StageResult:
        moment = self._now or datetime.now(UTC)
        as_of = moment.astimezone(UTC).date()
        record = proposal_for_task(self._session, task.task_id)
        if record is None:
            return _blocked(
                "this task was not produced by the catalogue proposer, so there is no "
                "pre-registered measurement to run for it",
                task_id=task.task_id,
            )
        template = template_by_id(record.template_id)
        if (
            template is None
            or template.version != record.template_version
            or template.catalogue_hash != record.catalogue_hash
        ):
            return _blocked(
                f"the catalogue entry {record.template_id} v{record.template_version} this task "
                "was proposed under has been changed or removed since; measuring it now would run "
                "a question the task was not opened for",
                recorded_catalogue_hash=record.catalogue_hash,
                current_catalogue_hash="none" if template is None else template.catalogue_hash,
            )
        source = next(
            (
                entry
                for entry in DATA_SOURCES
                if entry.observation_identity == record.dataset_identity
                and entry.dataset in task.required_datasets
            ),
            None,
        )
        if source is None:
            return _blocked(
                "the task declares no dataset this project can load observations for",
                required_datasets=", ".join(task.required_datasets),
            )
        readiness = next(
            row
            for row in measure_readiness(self._session, as_of=as_of)
            if row.identity == source.observation_identity
        )
        clean = template.clean_sessions(readiness)
        boundary = template.exploratory_through(readiness)
        available = confirmatory_observations(len(clean))
        cumulative = HypothesisRegistry(self._session).cumulative_trials() + 1

        exploratory: MeasurementResult | None = None
        started = finished = moment
        if boundary is not None:
            try:
                panel = load_panel(self._session, source, through=boundary)
                if clean and panel.dates[-1] >= clean[0]:  # pragma: no cover - by construction
                    raise ExperimentRefused(
                        f"the panel reaches {panel.dates[-1]}, past the first clean session "
                        f"{clean[0]}; refusing rather than reading confirmatory data"
                    )
                started = datetime.now(UTC)
                exploratory = run_measurement(template, panel, sandbox=self._sandbox)
                finished = datetime.now(UTC)
            except ExperimentRefused as error:
                return _blocked(str(error), template_id=template.template_id)

        verdict = grade(
            template,
            exploratory,
            cumulative_trials=cumulative,
            confirmatory_observations=available,
        )
        ids = _evidence_ids(task.task_id)
        detail: dict[str, str] = {
            "experiment_version": str(EXPERIMENT_VERSION),
            "code_commit": self._code_commit,
            "proposal_id": record.proposal_id,
            "template_id": template.template_id,
            "template_version": str(template.version),
            "catalogue_hash": template.catalogue_hash,
            "measured_at": moment.astimezone(UTC).isoformat(timespec="seconds"),
            "dataset_identity": readiness.identity,
            "consumed_through": _iso(readiness.consumed_through),
            "exposed_through": _iso(template.exposed_through),
            "exploratory_through": _iso(boundary),
            "clean_confirmatory_sessions": str(len(clean)),
            "first_clean_session": _iso(clean[0] if clean else None),
            **verdict.as_detail(),
        }
        if exploratory is not None:
            detail.update(exploratory.as_detail())
            detail["search_id"] = ids["search"]
            detail["exploratory_record_id"] = ids["exploratory"]
        if verdict.next_state is TaskState.UNDERPOWERED:
            detail["underpowered_record_id"] = ids["underpowered"]

        evidence = _Evidence(
            session=self._session,
            task=task,
            template=template,
            readiness=readiness,
            record_proposal_id=record.proposal_id,
            exploratory=exploratory,
            verdict=verdict,
            detail=detail,
            ids=ids,
            moment=moment,
            started=started,
            finished=finished,
        )
        return StageResult(
            next_state=verdict.next_state,
            reason=verdict.reason,
            detail=detail,
            persist=evidence.write,
        )


def autoexperiment_stage(
    session: Session,
    *,
    sandbox: IsolatedSandbox | None = None,
    now: datetime | None = None,
    code_commit: str = "unknown",
) -> AutoExperimentStage:
    """Build the `SCOUTING` handler. Kept as a function so `cli.py` reads like the other stages."""
    return AutoExperimentStage(session, sandbox=sandbox, now=now, code_commit=code_commit)


def _evidence_ids(task_id: str) -> dict[str, str]:
    suffix = f"{task_id}:e{EXPERIMENT_VERSION}"
    return {
        "search": f"auto-exploratory:{suffix}",
        "exploratory": f"exploratory:{suffix}",
        "underpowered": f"underpowered:{suffix}",
    }


@dataclass(frozen=True, slots=True)
class _Evidence:
    """Everything one measurement leaves behind, written only after its move is accepted."""

    session: Session
    task: ResearchTask
    template: ProposalTemplate
    readiness: DatasetReadiness
    record_proposal_id: str
    exploratory: MeasurementResult | None
    verdict: Grade
    detail: Mapping[str, str]
    ids: Mapping[str, str]
    moment: datetime
    started: datetime
    finished: datetime

    def write(self) -> None:
        memory = ResearchMemory(self.session)
        # Built before anything is written, so a record that fails validation fails with nothing
        # on disk rather than after the search row.
        records: list[ResearchRecord] = []
        if self.exploratory is not None and not self.exploratory.degenerate:
            records.append(
                ResearchRecord(
                    record_id=self.ids["exploratory"],
                    kind=RecordKind.FINDING,
                    verdict=Verdict.EXPLORATORY_ONLY,
                    subject=self.template.question,
                    statement=(
                        f"Exploratory only, from sessions already searched or already seen by "
                        f"this question ({self.exploratory.first_session}.."
                        f"{self.exploratory.last_session}): {self.exploratory.measurement_id}="
                        f"{self.exploratory.statistic} (t={self.exploratory.t_statistic}) over "
                        f"{self.exploratory.observations} observations. Not confirmation and not "
                        "a refutation; a registration that builds on it must cite its search."
                    ),
                    source=f"{ACTOR}/e{EXPERIMENT_VERSION} {self.task.task_id}",
                    recorded_at=self.moment,
                    detail=dict(self.detail),
                )
            )
        if self.verdict.next_state is TaskState.UNDERPOWERED:
            records.append(
                ResearchRecord(
                    record_id=self.ids["underpowered"],
                    kind=RecordKind.FINDING,
                    verdict=Verdict.UNDERPOWERED,
                    subject=self.template.question,
                    statement=self.verdict.reason,
                    source=f"{ACTOR}/e{EXPERIMENT_VERSION} {self.task.task_id}",
                    recorded_at=self.moment,
                    detail=dict(self.detail),
                )
            )

        if self.exploratory is not None:
            self._record_search()
        for entry in records:
            memory.record(entry)
            memory.relate(
                entry.record_id,
                Relation.USES_DATASET,
                self.readiness.identity,
                now=self.moment,
                note=f"task {self.task.task_id}",
            )

    def _record_search(self) -> None:
        """The exploratory look, in the search ledger, with the count it actually spent."""
        if self.exploratory is None:  # pragma: no cover - `write` only calls this with a result
            return
        search_id = self.ids["search"]
        existing = self.session.execute(
            select(search_runs.c.search_id).where(search_runs.c.search_id == search_id)
        ).one_or_none()
        if existing is not None:
            # Append-only, like `worker_adapter.record_search_run`: a second write under the same
            # id could revise a count downward. Raising takes the move with it (`run_cycle`).
            raise ExperimentRefused(f"search run {search_id} is already recorded")
        result = self.exploratory
        self.session.execute(
            search_runs.insert().values(
                search_id=search_id,
                actor=ACTOR,
                actor_version=str(EXPERIMENT_VERSION),
                # One pre-registered statistic, computed once. Not the three templates that share
                # the panel: each is its own row when, and if, its own task measures it.
                candidates_evaluated=1,
                dataset=self.readiness.dataset,
                start_date=result.first_session.isoformat(),
                end_date=result.last_session.isoformat(),
                data_role=DataRole.DISCOVERY.value,
                started_at=encode_utc(self.started),
                finished_at=encode_utc(self.finished),
                configuration_hash=result.script_sha256,
                detail_json=json.dumps(
                    {
                        **self.detail,
                        "task_id": self.task.task_id,
                        "proposal_id": self.record_proposal_id,
                        "stdout": result.stdout[-_OUTPUT_CAP:],
                        "stderr": result.stderr[-_OUTPUT_CAP:],
                    },
                    sort_keys=True,
                ),
            )
        )


def _iso(value: date | None) -> str:
    return "none" if value is None else value.isoformat()


__all__ = [
    "ACTOR",
    "BENCHMARK_SYMBOL",
    "EXPERIMENT_VERSION",
    "LEAD_LAG_LOSS",
    "MEASUREMENTS",
    "POLICY",
    "AutoExperimentStage",
    "ExperimentRefused",
    "Grade",
    "MeasurementResult",
    "Panel",
    "autoexperiment_stage",
    "build_script",
    "confirmatory_observations",
    "grade",
    "load_panel",
    "run_measurement",
]
