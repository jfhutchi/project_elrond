"""Run a proposed question's pre-registered measurement under kernel isolation, and grade it.

`proposer.py` produces a `ResearchTask`; `cli.py` wires a `PROPOSED` handler that advances it to
`SCOUTING` once capacity and novelty agree. Nothing then ran the experiment, so the task stopped
there -- "visibly stuck", as `cli.py` puts it, which was the honest thing to do while no execution
backend existed. One does now, so this module is the `SCOUTING` handler.

**Three separations, and each one is the difference between evidence and a story.**

*The measurement is catalogue code, not model code.* The model chose which question to ask;
the arithmetic that answers it was written in advance and is content-addressed. A model that
writes its own measurement writes its own success criterion with it.

*It still runs in the sandbox.* The script is machine-assembled per run and nobody reviews the
particular one that executes, which is exactly the case `mine_for_anomalies` and `codegen` refuse
an unenforced backend for. `select_isolated_backend()` is used rather than a directly constructed
runner, so a host with no OS-enforced isolation gets a refusal and not a quieter sandbox.

*Grading happens out here, deterministically.* The sandbox returns a statistic and nothing else.
Whether that statistic clears the bar, and whether the bar can be cleared at all with the
observations available, is computed from `power.luck_threshold` and `power.observations_required`
against the effect the template pre-registered. The child cannot influence its own verdict beyond
the number it reports, and a script that lies about its statistic is still judged against a
threshold it never saw.

**Why the honest terminal state here is usually `UNDERPOWERED`.** The only dataset on this host
with both capacity and observations is `us-equities-daily`, and cycles 2-10 spent 68 trials on it.
The luck bar at 68 trials is 2.90, and the clean confirmatory capacity is the handful of sessions
that have happened since the searching stopped. The measurement therefore runs on the *exploratory*
range -- legitimate, and the only thing that range can still support -- while confirmation needs
observations that do not exist yet.

That is a real scientific conclusion and not a failure to produce one. `UNDERPOWERED` is
deliberately terminal and deliberately not `REFUTED`: an exploratory statistic from an exhausted
range cannot refute a mechanism, and recording one as though it had would teach a later agent that
something was tested and failed when it was never tested. The state machine forbids the laundering
and so does this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.research.capacity import DATA_SOURCES, DataSource, capacity_row
from quantbot.research.cycle import StageHandler, StageResult
from quantbot.research.director import ResearchTask, TaskState
from quantbot.research.mining import IsolatedSandbox
from quantbot.research.power import (
    ComparisonStructure,
    DependenceAssumptions,
    EffectSpecification,
    luck_threshold,
    observations_required,
)
from quantbot.research.proposer import ProposalTemplate, template_by_id
from quantbot.sandbox import SandboxPolicy, SandboxResult, select_isolated_backend
from quantbot.storage.database import decode_utc
from quantbot.storage.schema import bars

#: Bumped when the measurement scripts or the grading rule change. Recorded on every result,
#: because a statistic is only comparable with another produced the same way.
EXPERIMENT_VERSION = 1

#: The series every measurement here is asked about. Declared rather than inferred so a panel
#: missing it fails closed instead of silently benchmarking against whatever sorted first.
BENCHMARK_SYMBOL = "SPY"

#: Enough for a 300-session panel of two dozen symbols and the arithmetic over it, and small
#: enough that a runaway is stopped in under a second. The wall clock is generous by comparison
#: so that a memory breach is reported as a memory breach.
POLICY = SandboxPolicy(wall_clock_seconds=90.0, memory_mb=256)


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


def load_panel(session: Session, source: DataSource, *, as_of: date) -> Panel:
    """Assemble the complete-session panel for one source, oldest first.

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
        if day > as_of:
            continue
        by_date.setdefault(day, {})[str(symbol)] = float(close)

    if not by_date:
        raise ExperimentRefused(f"{source.dataset} has no observations on or before {as_of}")

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

#: Shared arithmetic, injected ahead of the per-measurement tail. Written once so three
#: measurements cannot disagree about what a return is.
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
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise ExperimentRefused(
            f"the measurement report is missing or malformed: {error}"
        ) from error

    return MeasurementResult(
        measurement_id=template.measurement_id,
        observations=observations,
        statistic=statistic,
        t_statistic=t_statistic,
        degenerate=degenerate,
        sessions=sessions,
        universe=universe,
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

    def as_detail(self) -> dict[str, str]:
        return {
            "cumulative_trials": str(self.cumulative_trials),
            "luck_threshold_t": str(self.luck_bar),
            "observations_required": str(self.observations_required_for_confirmation),
            "confirmatory_observations_available": str(self.confirmatory_observations_available),
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


def grade(
    template: ProposalTemplate,
    result: MeasurementResult,
    *,
    cumulative_trials: int,
    confirmatory_observations: int,
) -> Grade:
    """Decide the task's fate from the pre-registered rule and the measured state.

    The bar is `luck_threshold(cumulative_trials)`: 68 trials on this project's equity range puts
    it at 2.90, not at 1.96, because the forty-first question asked of a range is not judged as
    though it were the first.

    The sample needed is sized on the **pre-registered** effect, never on the observed one. Sizing
    on what was measured would move the bar to wherever the result landed, which is the whole
    failure mode the registration exists to prevent.
    """
    bar = luck_threshold(cumulative_trials)
    required = observations_required(_specification(template), bar)

    if result.degenerate:
        return Grade(
            next_state=TaskState.BLOCKED,
            reason=(
                "the measurement produced a degenerate statistic (a constant series or too few "
                "observations), so there is nothing to judge"
            ),
            cumulative_trials=cumulative_trials,
            luck_bar=bar,
            observations_required_for_confirmation=required,
            confirmatory_observations_available=confirmatory_observations,
        )

    if confirmatory_observations < required:
        return Grade(
            next_state=TaskState.UNDERPOWERED,
            reason=(
                f"confirmation needs {required} observations for the pre-registered effect of "
                f"{template.expected_effect} to clear a {bar} luck bar at {cumulative_trials} "
                f"cumulative trials, and {confirmatory_observations} unsearched session(s) exist. "
                f"The exploratory measurement over {result.observations} observations returned "
                f"{result.measurement_id}={result.statistic} (t={result.t_statistic}); that is "
                "recorded as evidence for sizing a future confirmation and is emphatically not a "
                "refutation, because an exhausted range cannot refute"
            ),
            cumulative_trials=cumulative_trials,
            luck_bar=bar,
            observations_required_for_confirmation=required,
            confirmatory_observations_available=confirmatory_observations,
        )

    return Grade(
        next_state=TaskState.CRITIQUE,
        reason=(
            f"{confirmatory_observations} clean confirmatory observation(s) meet the {required} "
            f"the pre-registered effect needs at a {bar} bar; the exploratory measurement "
            f"returned {result.statistic} (t={result.t_statistic}) and the claim is worth "
            "critiquing before it is frozen"
        ),
        cumulative_trials=cumulative_trials,
        luck_bar=bar,
        observations_required_for_confirmation=required,
        confirmatory_observations_available=confirmatory_observations,
    )


def autoexperiment_stage(
    session: Session,
    *,
    sandbox: IsolatedSandbox | None = None,
    now: datetime | None = None,
) -> StageHandler:
    """Build a `SCOUTING` handler that measures a proposed question and grades the result.

    Returns a `StageResult` for every outcome including the refusals, because a task that could
    not be measured has to say so in its own audit trail rather than raising through the drain and
    leaving the queue looking merely quiet.
    """

    def handle(task: ResearchTask) -> StageResult:
        moment = now or datetime.now(UTC)
        as_of = moment.astimezone(UTC).date()
        template = next(
            (
                entry
                for entry in (template_by_id(name) for name in _template_names(task))
                if entry is not None
            ),
            None,
        )
        if template is None:
            return StageResult(
                next_state=TaskState.BLOCKED,
                reason=(
                    "this task was not produced by the catalogue proposer, so there is no "
                    "pre-registered measurement to run for it"
                ),
                detail={"task_id": task.task_id},
            )
        source = next(
            (entry for entry in DATA_SOURCES if entry.dataset in task.required_datasets), None
        )
        if source is None:
            return StageResult(
                next_state=TaskState.BLOCKED,
                reason="the task declares no dataset this project can load observations for",
                detail={"required_datasets": ", ".join(task.required_datasets)},
            )

        row = capacity_row(session, source, as_of=as_of)
        try:
            panel = load_panel(session, source, as_of=as_of)
            result = run_measurement(template, panel, sandbox=sandbox)
        except ExperimentRefused as error:
            return StageResult(
                next_state=TaskState.BLOCKED,
                reason=str(error),
                detail={"experiment_version": str(EXPERIMENT_VERSION)},
            )

        verdict = grade(
            template,
            result,
            cumulative_trials=row.trials,
            confirmatory_observations=len(row.unsearched_sessions),
        )
        detail = {
            "experiment_version": str(EXPERIMENT_VERSION),
            "template_id": template.template_id,
            "measured_at": moment.astimezone(UTC).isoformat(timespec="seconds"),
            **result.as_detail(),
            **verdict.as_detail(),
        }
        return StageResult(next_state=verdict.next_state, reason=verdict.reason, detail=detail)

    return handle


def _template_names(task: ResearchTask) -> Sequence[str]:
    """Recover the catalogue template from a proposer-minted task id.

    The id is `auto-<template_id>-v<version>-<identity>` by construction, so the template is
    recoverable without a second column. Returns candidates rather than one name because the
    template id itself may contain hyphens, and guessing the split point would occasionally pick
    a template that exists and is the wrong one.
    """
    if not task.task_id.startswith("auto-"):
        return ()
    body = task.task_id[len("auto-") :]
    parts = body.split("-")
    return tuple(
        "-".join(parts[:index])
        for index in range(len(parts), 0, -1)
    )


__all__ = [
    "BENCHMARK_SYMBOL",
    "EXPERIMENT_VERSION",
    "MEASUREMENTS",
    "POLICY",
    "ExperimentRefused",
    "Grade",
    "MeasurementResult",
    "Panel",
    "autoexperiment_stage",
    "build_script",
    "grade",
    "load_panel",
    "run_measurement",
]
