"""One research cycle: pick the most informative task and move it exactly one stage (#3).

`ResearchDirector` owned the task lifecycle -- states, legal transitions, durable history -- and
nothing drove it. `scripts/dashboard.py` read tasks; no code in `src/` ever advanced one. The
audit's summary of the whole v0.2 architecture was that every gate validates a claim supplied by
its caller and no caller exists. This is that caller for the orchestration layer.

Two decisions worth stating, because both are load-bearing.

**One step, not a loop.** `run_cycle` advances a single task by a single stage and returns. The
caller decides cadence, and a runaway orchestrator cannot burn the multiple-testing budget while
nobody is watching -- every registration permanently raises the luck bar for everything after it,
so an autonomous loop that registers is an autonomous loop that spends scarce evidence. A `while
True` here would be one bug away from exhausting the project's statistical capacity overnight.

**Stages are injected, never imported for effect.** Each handler is supplied by the caller, so a
cycle can run with scouting absent, or a model runtime absent, and simply not offer those stages.
That is what makes this usable today: #4's connector exists, #9's transport exists, #13's
discovery engine does not, and a composition root that hard-required all three would be
unrunnable until the last one lands.

The broker is unreachable from here, and that is enforced by the import-graph test rather than by
this docstring.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quantbot.research.budget import BudgetExceeded, BudgetGovernor, LoopBound
from quantbot.research.director import (
    ACTIVE,
    InvalidTaskTransition,
    ResearchDirector,
    ResearchTask,
    TaskState,
    prioritize,
)


class CycleRefused(RuntimeError):
    """Raised when a cycle cannot proceed honestly. Not the same as having no work."""


@dataclass(frozen=True, slots=True)
class StageResult:
    """What a stage handler decided. The handler reports; the director records.

    A handler cannot write to the task itself -- it returns the state it believes the task should
    reach and the reason, and `run_cycle` puts that through `ResearchDirector.advance`, which is
    the only thing that knows which transitions are legal. A handler that could set state
    directly could move a task from UNDERPOWERED to REFUTED, which the state machine exists to
    forbid.
    """

    next_state: TaskState
    reason: str
    detail: Mapping[str, str] | None = None


#: A stage handler: given the task, decide where it goes next. Pure with respect to the director.
StageHandler = Callable[[ResearchTask], StageResult]

#: What running a task is worth. Injected rather than computed here, because
#: `expected_information_gain` needs a power assessment, a novelty overlap and the remaining
#: trial budget -- and a cycle that guessed at any of those would be ordering real research by a
#: number it made up. A caller holding those inputs supplies the real function; one that does not
#: gets `equal_priority` and honest FIFO ordering.
TaskScore = Callable[[ResearchTask], Decimal]


def equal_priority(task: ResearchTask) -> Decimal:
    """Score every task the same, so `prioritize` falls back to oldest-first.

    Not a placeholder for `expected_information_gain` -- a deliberate refusal to approximate it.
    A fabricated gain would decide which hypotheses get run, which is exactly the decision that
    should not rest on an invented number.
    """
    _ = task
    return Decimal("0")


def budget_aware(
    governor: BudgetGovernor,
    *,
    trials_per_task: Decimal,
    gain: TaskScore,
) -> TaskScore:
    """Score a task at zero when its family cannot afford the trials it would spend (#14).

    `prioritize` sorts zero-gain work to the bottom, so an unaffordable question is deferred
    rather than refused -- it stays actionable if the family's cap is later raised by an
    attributable operator override, and it does not vanish from the queue in the meantime.

    The governor's own argument is why this is not a compute decision: a family close to its
    trial cap should decline a low-value question even when compute is free, because trials are
    the thing that cannot be bought back. Wall time and tokens refill overnight; a consumed
    holdout never does.
    """

    def score(task: ResearchTask) -> Decimal:
        value = gain(task)
        if governor.worth_spending(
            task.family_id, trials_per_task, expected_information_gain=value
        ):
            return value
        return Decimal("0")

    return score


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    task: ResearchTask | None
    moved_to: TaskState | None
    reason: str

    @property
    def idle(self) -> bool:
        return self.task is None


def actionable(
    director: ResearchDirector,
    stages: Mapping[TaskState, StageHandler],
    *,
    score: TaskScore = equal_priority,
) -> list[tuple[ResearchTask, Decimal]]:
    """Active tasks whose dependencies are met and whose stage someone can actually run.

    A task in SCOUTING with no scouting handler is not actionable, and is deliberately not an
    error either: a partially built system should run the stages it has rather than refuse to
    start. It stays in SCOUTING and is visible as stuck, which is the honest representation.
    """
    ready: list[tuple[ResearchTask, Decimal]] = []
    for task in director.by_state(*ACTIVE):
        if task.state not in stages or not director.dependencies_met(task):
            continue
        ready.append((task, score(task)))
    return ready


def run_cycle(
    director: ResearchDirector,
    stages: Mapping[TaskState, StageHandler],
    *,
    actor: str,
    now: datetime,
    score: TaskScore = equal_priority,
) -> CycleOutcome:
    """Advance the single most informative actionable task by one stage.

    Returns an idle outcome rather than raising when there is nothing to do. "No work" and "the
    work failed" are different facts and a caller polling this must be able to tell them apart.
    """
    if not actor.strip():
        raise CycleRefused("a cycle must record which actor ran it")

    ranked = prioritize(actionable(director, stages, score=score))
    if not ranked:
        return CycleOutcome(task=None, moved_to=None, reason="no actionable task")

    task, gain = ranked[0]
    handler = stages[task.state]
    try:
        decision = handler(task)
    except Exception as error:  # noqa: BLE001 -- a failed stage blocks the task, it does not vanish
        # BLOCKED, not the terminal states. A stage that raised did not establish that the
        # hypothesis is wrong or that the data cannot resolve it; it established that something
        # went wrong here. Recording it as REFUTED would manufacture a finding out of a crash.
        blocked = director.advance(
            task.task_id,
            TaskState.BLOCKED,
            actor=actor,
            reason=f"{task.state.value} stage raised {type(error).__name__}",
            now=now,
            detail={"error": str(error)[:500], "stage": task.state.value},
        )
        # Read back from the persisted task rather than restating the intent. A literal here
        # lets the outcome claim BLOCKED while the database holds something else -- which a
        # mutation proved, by changing the target to REFUTED and leaving every test green.
        return CycleOutcome(
            task=blocked,
            moved_to=blocked.state,
            reason=f"{task.state.value} stage raised {type(error).__name__}: {error}",
        )

    try:
        moved = director.advance(
            task.task_id,
            decision.next_state,
            actor=actor,
            reason=decision.reason,
            now=now,
            detail=_with_gain(decision.detail, gain),
        )
    except InvalidTaskTransition as error:
        # The handler asked for a move the state machine forbids. That is a defect in the
        # handler, and it must surface rather than be quietly downgraded to BLOCKED -- a
        # silently-corrected illegal transition is how UNDERPOWERED becomes REFUTED.
        raise CycleRefused(
            f"the {task.state.value} stage asked to move {task.task_id} to "
            f"{decision.next_state.value}, which the lifecycle forbids"
        ) from error

    return CycleOutcome(task=moved, moved_to=moved.state, reason=decision.reason)


def _with_gain(detail: Mapping[str, str] | None, gain: Decimal) -> dict[str, str]:
    """Record what the task was worth when it was chosen, not what it looks like later.

    Expected information gain moves as the project's trial burden rises, so recomputing it when
    reading history would judge a past decision against a bar it was never asked to clear.
    """
    recorded = dict(detail or {})
    recorded["expected_information_gain"] = str(gain)
    return recorded


def drain(
    director: ResearchDirector,
    stages: Mapping[TaskState, StageHandler],
    *,
    actor: str,
    now: datetime,
    max_steps: int,
    score: TaskScore = equal_priority,
    bound: LoopBound | None = None,
) -> list[CycleOutcome]:
    """Run up to `max_steps` cycles, stopping early when there is nothing left to do.

    `max_steps` is required and has no default on purpose. This is the only function here that
    runs more than one stage, and an unbounded version is the thing the module docstring argues
    against: each registration permanently raises the luck bar for every hypothesis after it, so
    the number of stages a single invocation may run is a decision the caller has to make
    explicitly rather than inherit.
    """
    if max_steps < 1:
        raise CycleRefused("a drain must be allowed at least one step")
    outcomes: list[CycleOutcome] = []
    counter = bound
    for _ in range(max_steps):
        if counter is not None:
            # Counted before the step, not after. A bound checked afterwards permits one round
            # beyond it, which for a recursive agent workflow is the round that matters (#14).
            try:
                counter = counter.advance()
            except BudgetExceeded as exceeded:
                outcomes.append(
                    CycleOutcome(
                        task=None,
                        moved_to=None,
                        reason=f"loop bound reached: {exceeded}",
                    )
                )
                break
        outcome = run_cycle(director, stages, actor=actor, now=now, score=score)
        outcomes.append(outcome)
        if outcome.idle:
            break
    return outcomes


__all__ = [
    "CycleOutcome",
    "CycleRefused",
    "StageHandler",
    "StageResult",
    "TaskScore",
    "actionable",
    "budget_aware",
    "drain",
    "equal_priority",
    "run_cycle",
]
