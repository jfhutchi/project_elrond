"""The research cycle: what drives a task, and what it refuses to do to one."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.research.budget import BudgetGovernor, Cap, LoopBound, Resource
from quantbot.research.cycle import (
    CycleRefused,
    StageResult,
    actionable,
    budget_aware,
    drain,
    equal_priority,
    run_cycle,
)
from quantbot.research.director import (
    ResearchDirector,
    ResearchTask,
    TaskState,
    prioritize,
)
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
#: A stage that always advances, so tests about ordering are not also tests about handlers.
STAGES = {TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "ok")}


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "cycle.db")
    yield db
    db.close()


def task(task_id: str, state: TaskState = TaskState.PROPOSED, **overrides: object) -> ResearchTask:
    fields: dict[str, object] = {
        "task_id": task_id,
        "state": state,
        "question": f"does {task_id} predict anything",
        "family_id": "trend-following",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ResearchTask(**fields)


def _open(director: ResearchDirector, *tasks: ResearchTask) -> None:
    """Seed tasks through the real lifecycle.

    `open_task` accepts only PROPOSED, which is correct -- a task cannot be born mid-pipeline --
    so anything wanted in a later state is opened and then advanced legally. Writing the rows
    directly would let these tests seed states the state machine forbids and prove nothing about
    a system that cannot reach them.
    """
    for item in tasks:
        director.open_task(
            item.model_copy(update={"state": TaskState.PROPOSED}), actor="test", reason="seeded"
        )
        if item.state is not TaskState.PROPOSED:
            director.advance(
                item.task_id,
                item.state,
                actor="test",
                reason="seeded into position",
                now=item.created_at,
            )


def test_a_cycle_with_nothing_to_do_is_idle_rather_than_an_error(database: Database) -> None:
    """ "No work" and "the work failed" are different facts a poller must tell apart."""
    with database.transaction() as session:
        outcome = run_cycle(ResearchDirector(session), {}, actor="claude", now=NOW)

    assert outcome.idle
    assert outcome.task is None
    assert outcome.reason == "no actionable task"


def test_a_task_whose_stage_nobody_can_run_is_not_actionable(database: Database) -> None:
    """A partially built system runs the stages it has instead of refusing to start.

    #13's discovery engine does not exist. A cycle offered no SCOUTING handler must leave a
    scouting task alone and pick up work it can actually do, rather than erroring or -- worse --
    inventing a transition for a stage that never ran.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-scout", TaskState.SCOUTING), task("T-critique", TaskState.CRITIQUE))

        stages = {
            TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "critique cleared")
        }
        ready = [item.task_id for item, _ in actionable(director, stages)]
        outcome = run_cycle(director, stages, actor="claude", now=NOW)

    assert ready == ["T-critique"], "the unrunnable scouting task is skipped, not failed"
    assert outcome.task is not None
    assert outcome.task.task_id == "T-critique"
    assert outcome.moved_to is TaskState.REGISTERED


def test_a_stage_that_raises_blocks_the_task_and_does_not_refute_it(database: Database) -> None:
    """A crash is not a finding.

    This is the property the whole module exists to protect. A stage that raised established
    that something went wrong in the code, not that the hypothesis is false and not that the
    data cannot resolve it. Recording REFUTED or UNDERPOWERED here would manufacture a research
    result out of an exception, and `REFUTED.md` would then carry it as institutional knowledge.
    """

    def explode(_task: ResearchTask) -> StageResult:
        raise ZeroDivisionError("the critic fell over")

    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-1", TaskState.CRITIQUE))

        outcome = run_cycle(director, {TaskState.CRITIQUE: explode}, actor="claude", now=NOW)
        history = director.history("T-1")
        # Assert the DATABASE, not the returned label. A mutation that changed the recorded
        # target to REFUTED left this test green when it checked `outcome.moved_to`, because
        # that field was a literal restating the intent rather than the persisted state. The
        # stored row is the thing a later agent reads as research memory.
        stored = director.get("T-1")

    assert stored is not None
    assert stored.state is TaskState.BLOCKED
    assert stored.state is not TaskState.REFUTED, "a crash is not a refutation"
    assert stored.state is not TaskState.UNDERPOWERED, "a crash is not a power finding"
    assert outcome.moved_to is stored.state, "the outcome must report what was persisted"
    assert "ZeroDivisionError" in outcome.reason
    # The failure is durable, so a stuck task is visible rather than merely absent.
    assert any("ZeroDivisionError" in event.reason for event in history)


def test_a_stage_asking_for_an_illegal_transition_surfaces_rather_than_being_corrected(
    database: Database,
) -> None:
    """A silently-downgraded illegal move is how UNDERPOWERED would become REFUTED.

    The lifecycle forbids that transition specifically. If `run_cycle` caught the refusal and
    quietly marked the task BLOCKED instead, a handler with that bug would look like it was
    working and the guarantee would be gone.
    """

    def illegal(_task: ResearchTask) -> StageResult:
        return StageResult(TaskState.SURVIVED, "skipping straight to the answer")

    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-1", TaskState.PROPOSED))

        with pytest.raises(CycleRefused, match="which the lifecycle forbids"):
            run_cycle(director, {TaskState.PROPOSED: illegal}, actor="claude", now=NOW)


def test_one_cycle_moves_exactly_one_task_one_stage(database: Database) -> None:
    """The budget argument: a registration permanently raises the bar for everything after it.

    A loop that advanced every actionable task per call would spend the multiple-testing budget
    at a rate nobody chose. Cadence belongs to the caller.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-1", TaskState.CRITIQUE), task("T-2", TaskState.CRITIQUE))

        stages = {TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "ok")}
        run_cycle(director, stages, actor="claude", now=NOW)
        states = {item.task_id: item.state for item in director.by_state()}

    assert sorted(states.values(), key=str) == sorted(
        [TaskState.CRITIQUE, TaskState.REGISTERED], key=str
    ), "the second task is untouched"


def test_a_drain_is_bounded_and_stops_when_the_work_runs_out(database: Database) -> None:
    """`max_steps` has no default, so the bound is always a decision someone made."""
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-1", TaskState.CRITIQUE), task("T-2", TaskState.CRITIQUE))

        stages = {TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "ok")}
        outcomes = drain(director, stages, actor="claude", now=NOW, max_steps=10)

    # Two tasks advanced, then one idle outcome that ended it early.
    assert [o.moved_to for o in outcomes] == [
        TaskState.REGISTERED,
        TaskState.REGISTERED,
        None,
    ]
    assert outcomes[-1].idle


def test_a_drain_respects_its_ceiling(database: Database) -> None:
    """Bounded means bounded, even with work remaining."""
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, *(task(f"T-{n}", TaskState.CRITIQUE) for n in range(5)))

        stages = {TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "ok")}
        outcomes = drain(director, stages, actor="claude", now=NOW, max_steps=2)
        remaining = len(director.by_state(TaskState.CRITIQUE))

    assert len(outcomes) == 2
    assert remaining == 3, "the ceiling held with work still available"


def test_a_drain_must_be_allowed_at_least_one_step(database: Database) -> None:
    with database.transaction() as session:
        with pytest.raises(CycleRefused, match="at least one step"):
            drain(ResearchDirector(session), {}, actor="claude", now=NOW, max_steps=0)


def test_a_cycle_must_record_who_ran_it(database: Database) -> None:
    """Attribution is not optional; a durable history with an anonymous actor is not history."""
    with database.transaction() as session:
        with pytest.raises(CycleRefused, match="which actor"):
            run_cycle(ResearchDirector(session), {}, actor="   ", now=NOW)


def test_the_gain_that_chose_a_task_is_recorded_with_the_move(database: Database) -> None:
    """Expected information gain moves as the trial burden rises.

    Recomputing it when reading history would judge a past decision against a bar it was never
    asked to clear, which is the same error the power gate avoids by freezing the luck threshold
    into each registration.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-1", TaskState.CRITIQUE))

        run_cycle(
            director,
            {TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "ok")},
            actor="claude",
            now=NOW,
            score=lambda _t: Decimal("0.75"),
        )
        history = director.history("T-1")

    recorded = [event for event in history if event.detail.get("expected_information_gain")]
    assert recorded, "the score that selected the task is durable"
    assert recorded[-1].detail["expected_information_gain"] == "0.75"


def test_equal_priority_orders_oldest_first_rather_than_guessing(database: Database) -> None:
    """The default refuses to approximate information gain; it does not invent a ranking."""
    older = task("T-old", TaskState.CRITIQUE, created_at=NOW - timedelta(days=2))
    newer = task("T-new", TaskState.CRITIQUE, created_at=NOW)

    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, newer, older)

        stages = {TaskState.CRITIQUE: lambda _t: StageResult(TaskState.REGISTERED, "ok")}
        outcome = run_cycle(director, stages, actor="claude", now=NOW)

    assert equal_priority(older) == equal_priority(newer)
    assert outcome.task is not None
    assert outcome.task.task_id == "T-old"


def test_a_task_waiting_on_a_dependency_is_not_actionable(database: Database) -> None:
    """Ordering constraints are the director's, and the cycle must not route around them."""
    with database.transaction() as session:
        director = ResearchDirector(session)
        # Both left in PROPOSED: `advance` enforces dependencies too, so seeding the waiter into
        # a later state is itself impossible while the blocker is unfinished. That is the guard
        # working, and routing around it here would test a state the system cannot reach.
        _open(director, task("T-blocker"))
        _open(director, task("T-waiter", depends_on=("T-blocker",)))

        stages = {TaskState.PROPOSED: lambda _t: StageResult(TaskState.CRITIQUE, "ok")}
        ready = [item.task_id for item, _ in actionable(director, stages)]
        outcome = run_cycle(director, stages, actor="claude", now=NOW)

    assert "T-waiter" not in ready
    assert "T-blocker" in ready
    assert outcome.task is not None
    assert outcome.task.task_id == "T-blocker", "the cycle cannot route around the ordering"


def test_a_family_that_cannot_afford_trials_is_deferred_not_refused(database: Database) -> None:
    """#14: the director defers low-value work because of statistical-budget cost.

    Deferred rather than refused is the deliberate part. `prioritize` sorts zero-gain work to
    the bottom, so an unaffordable question stays in the queue and becomes actionable again if
    an operator raises the cap. Dropping it would lose the question; refusing it would need a
    terminal state, and "we could not afford it" is not a research finding.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-rich", family_id="affordable"))
        _open(director, task("T-poor", family_id="exhausted"))

        governor = BudgetGovernor(
            session,
            caps=[
                Cap(budget_key="affordable", resource=Resource.TRIALS, limit=Decimal("100")),
                Cap(budget_key="exhausted", resource=Resource.TRIALS, limit=Decimal("0")),
            ],
        )
        score = budget_aware(governor, trials_per_task=Decimal("1"), gain=lambda _t: Decimal("0.9"))
        # Both tasks are PROPOSED, so the stage map has to cover PROPOSED for either to be
        # actionable at all.
        proposed = {TaskState.PROPOSED: lambda _t: StageResult(TaskState.CRITIQUE, "ok")}
        ranked = [
            item.task_id for item, _ in prioritize(actionable(director, proposed, score=score))
        ]
        scores = {
            item.task_id: value for item, value in actionable(director, proposed, score=score)
        }

    assert scores["T-poor"] == Decimal("0"), "an exhausted family scores zero"
    assert scores["T-rich"] == Decimal("0.9")
    assert ranked[0] == "T-rich", "affordable work is chosen first"
    assert "T-poor" in ranked, "and the unaffordable question is still in the queue"


def test_a_recursive_workflow_stops_at_its_configured_bound(database: Database) -> None:
    """#14: a deliberately recursive agent workflow stops at configured bounds.

    The stage here always sends the task back where it came from, which is the shape of a debate
    or revision loop that can always try once more. Without a bound `drain` would run to
    `max_steps` every time; the point is that the bound stops it earlier and says so.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-loop", TaskState.CRITIQUE))

        # CRITIQUE -> PROPOSED -> CRITIQUE ... a revision loop with no natural end.
        stages = {
            TaskState.CRITIQUE: lambda _t: StageResult(TaskState.PROPOSED, "needs revision"),
            TaskState.PROPOSED: lambda _t: StageResult(TaskState.CRITIQUE, "revised"),
        }
        outcomes = drain(
            director,
            stages,
            actor="claude",
            now=NOW,
            max_steps=50,
            bound=LoopBound(task="revision", max_rounds=3),
        )

    advanced = [o for o in outcomes if o.task is not None]
    assert len(advanced) == 3, "exactly the bound, not one more"
    assert "loop bound reached" in outcomes[-1].reason
    assert len(outcomes) < 50, "the bound stopped it well short of max_steps"


def test_the_bound_is_counted_before_the_step_not_after(database: Database) -> None:
    """A bound checked afterwards permits one round beyond it.

    For a recursive workflow that extra round is the one that matters -- it is the round the
    bound existed to prevent. A bound of 1 must allow exactly one step.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        _open(director, task("T-1", TaskState.CRITIQUE), task("T-2", TaskState.CRITIQUE))

        outcomes = drain(
            director,
            STAGES,
            actor="claude",
            now=NOW,
            max_steps=10,
            bound=LoopBound(task="revision", max_rounds=1),
        )
        remaining = len(director.by_state(TaskState.CRITIQUE))

    assert len([o for o in outcomes if o.task is not None]) == 1
    assert remaining == 1, "the second task was never touched"
