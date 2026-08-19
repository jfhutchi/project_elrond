"""The research lifecycle, and the boundary it must never be able to cross."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.director import (
    ACTIVE,
    ALLOWED_TRANSITIONS,
    InvalidTaskTransition,
    ResearchDirector,
    ResearchTask,
    TaskState,
    expected_information_gain,
    prioritize,
)
from quantbot.research.power import (
    ComparisonStructure,
    DependenceAssumptions,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    PowerAssessment,
    PowerOverride,
    assess,
)
from quantbot.storage import Database

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "director.db")
    yield db
    db.close()


def task(**overrides: object) -> ResearchTask:
    fields: dict[str, object] = {
        "task_id": "T-1",
        "state": TaskState.PROPOSED,
        "question": "Does a 200-day trend filter on SPY beat buy-and-hold on Sharpe?",
        "family_id": "trend-following",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return ResearchTask(**fields)


def power(
    sharpe: str = "1.0", *, observations: int = 2669, trials: int = 69
) -> PowerAssessment:
    specification = EffectSpecification(
        estimand=Estimand.SHARPE,
        expected=Decimal(sharpe),
        minimum_practical=Decimal(sharpe) / 2,
        justification="Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
        comparison=ComparisonStructure.PAIRED,
        dependence=DependenceAssumptions(),
        economics=EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    )
    return assess(
        hypothesis_id="H-1",
        version=1,
        stage="REGISTRATION",
        specification=specification,
        observations_available=observations,
        cumulative_trials=trials,
        assessed_at=NOW,
    )


def test_a_task_runs_end_to_end_without_touching_the_database_by_hand(
    database: Database,
) -> None:
    with database.transaction() as session:
        ResearchDirector(session).open_task(task(), actor="director", reason="candidate raised")

    path = [
        TaskState.SCOUTING,
        TaskState.CRITIQUE,
        TaskState.REGISTERED,
        TaskState.EXPERIMENTING,
        TaskState.REVIEW,
        TaskState.SURVIVED,
        TaskState.PROMOTABLE,
    ]
    for index, state in enumerate(path):
        with database.transaction() as session:
            ResearchDirector(session).advance(
                "T-1",
                state,
                actor="director",
                reason=f"step {index}",
                now=NOW + timedelta(minutes=index),
            )

    with database.transaction() as session:
        director = ResearchDirector(session)
        finished = director.get("T-1")
        history = director.history("T-1")

    assert finished is not None
    assert finished.state is TaskState.PROMOTABLE
    assert finished.terminal
    assert finished.attempts == 1
    assert [event.to_state for event in history] == [TaskState.PROPOSED, *path]


def test_restarting_does_not_lose_task_state(database: Database) -> None:
    with database.transaction() as session:
        ResearchDirector(session).open_task(task(), actor="director", reason="candidate")
    with database.transaction() as session:
        ResearchDirector(session).advance(
            "T-1", TaskState.CRITIQUE, actor="director", reason="to critique", now=NOW
        )

    database.close()
    reopened = Database(database.path)
    try:
        with reopened.transaction() as session:
            restored = ResearchDirector(session).get("T-1")
            events = ResearchDirector(session).history("T-1")
    finally:
        reopened.close()

    assert restored is not None
    assert restored.state is TaskState.CRITIQUE
    assert len(events) == 2


def test_underpowered_is_terminal_and_can_never_become_refuted(database: Database) -> None:
    """A question the data could not resolve was never tested.

    Research memory already blocks this conversion; the lifecycle must not offer a way round
    it, so the transition simply does not exist.
    """
    assert ALLOWED_TRANSITIONS[TaskState.UNDERPOWERED] == frozenset()
    assert TaskState.REFUTED not in ALLOWED_TRANSITIONS[TaskState.UNDERPOWERED]

    with database.transaction() as session:
        director = ResearchDirector(session)
        director.open_task(task(), actor="director", reason="candidate")
        director.advance(
            "T-1",
            TaskState.UNDERPOWERED,
            actor="power-gate",
            reason="2669 observations against 8508 required",
            now=NOW,
        )

    with database.transaction() as session, pytest.raises(InvalidTaskTransition) as error:
        ResearchDirector(session).advance(
            "T-1", TaskState.REFUTED, actor="director", reason="call it dead", now=NOW
        )
    assert "never tested" in str(error.value)


def test_an_illegal_transition_is_refused(database: Database) -> None:
    with database.transaction() as session:
        ResearchDirector(session).open_task(task(), actor="director", reason="candidate")
    # Straight from PROPOSED to SURVIVED would skip registration, critique and measurement.
    with database.transaction() as session, pytest.raises(InvalidTaskTransition):
        ResearchDirector(session).advance(
            "T-1", TaskState.SURVIVED, actor="director", reason="looks good", now=NOW
        )


def test_a_task_waits_for_what_it_depends_on(database: Database) -> None:
    with database.transaction() as session:
        director = ResearchDirector(session)
        director.open_task(task(task_id="T-1"), actor="director", reason="first")
        director.open_task(
            task(task_id="T-2", depends_on=("T-1",)), actor="director", reason="second"
        )

    with database.transaction() as session, pytest.raises(InvalidTaskTransition):
        ResearchDirector(session).advance(
            "T-2", TaskState.CRITIQUE, actor="director", reason="start", now=NOW
        )

    with database.transaction() as session:
        ResearchDirector(session).advance(
            "T-1", TaskState.UNDERPOWERED, actor="power-gate", reason="cannot resolve", now=NOW
        )
    with database.transaction() as session:
        moved = ResearchDirector(session).advance(
            "T-2", TaskState.CRITIQUE, actor="director", reason="dependency finished", now=NOW
        )
    assert moved.state is TaskState.CRITIQUE

    with pytest.raises(ValueError, match="cannot depend on itself"):
        task(task_id="T-3", depends_on=("T-3",))


def test_every_transition_records_who_moved_it_and_why(database: Database) -> None:
    with database.transaction() as session:
        director = ResearchDirector(session)
        director.open_task(task(), actor="director", reason="candidate raised from anomaly")
        director.advance(
            "T-1",
            TaskState.BLOCKED,
            actor="hutch",
            reason="paused pending an operator decision on the holdout",
            now=NOW + timedelta(hours=1),
            detail={"trials_remaining": "32", "luck_threshold_z": "2.910019"},
        )

    with database.transaction() as session:
        events = ResearchDirector(session).history("T-1")

    assert [event.actor for event in events] == ["director", "hutch"]
    assert events[1].reason.startswith("paused pending")
    assert events[1].detail["trials_remaining"] == "32"
    assert events[1].occurred_at == NOW + timedelta(hours=1)
    assert events[0].from_state is None


def test_an_underpowered_experiment_is_worth_nothing_however_interesting() -> None:
    """Expected confirmatory information gain of zero, and the scorer returns exactly that."""
    underpowered = power("0.3")
    assert expected_information_gain(
        underpowered, novelty_overlap=0.0, trials_remaining=Decimal("100"),
        trials_required=Decimal("1"),
    ) == Decimal("0")

    powered = power("1.0")
    gain = expected_information_gain(
        powered, novelty_overlap=0.0, trials_remaining=Decimal("100"),
        trials_required=Decimal("1"),
    )
    assert gain > Decimal("0")

    # Overlap with prior work discounts it; an exact repeat is worth nothing.
    assert expected_information_gain(
        powered, novelty_overlap=0.5, trials_remaining=Decimal("100"),
        trials_required=Decimal("1"),
    ) == (gain / 2).quantize(Decimal("0.0001"))
    assert expected_information_gain(
        powered, novelty_overlap=1.0, trials_remaining=Decimal("100"),
        trials_required=Decimal("1"),
    ) == Decimal("0")

    # And a question the budget cannot afford is worth nothing to schedule.
    assert expected_information_gain(
        powered, novelty_overlap=0.0, trials_remaining=Decimal("1"),
        trials_required=Decimal("40"),
    ) == Decimal("0")


def test_an_overridden_underpowered_task_still_scores_above_zero() -> None:
    """The operator accepted the shortfall knowingly, which is a different situation."""
    overridden = assess(
        hypothesis_id="H-1",
        version=1,
        stage="REGISTRATION",
        specification=EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("0.3"),
            minimum_practical=Decimal("0.15"),
            justification="A small but real effect.",
            comparison=ComparisonStructure.PAIRED,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        ),
        observations_available=2669,
        cumulative_trials=69,
        assessed_at=NOW,
        override=PowerOverride(authorized_by="hutch", reason="precursor"),
    )
    assert expected_information_gain(
        overridden, novelty_overlap=0.0, trials_remaining=Decimal("100"),
        trials_required=Decimal("1"),
    ) > Decimal("0")


def test_prioritization_puts_zero_gain_work_last() -> None:
    interesting_but_unresolvable = (task(task_id="T-1"), Decimal("0"))
    dull_but_answerable = (task(task_id="T-2"), Decimal("2.5"))
    ordered = prioritize([interesting_but_unresolvable, dull_but_answerable])
    assert [item[0].task_id for item in ordered] == ["T-2", "T-1"]


def test_the_supervisor_can_see_stuck_and_active_work(database: Database) -> None:
    with database.transaction() as session:
        director = ResearchDirector(session)
        director.open_task(task(task_id="T-1"), actor="director", reason="a")
        director.open_task(task(task_id="T-2"), actor="director", reason="b")
        director.advance(
            "T-2", TaskState.CRITIQUE, actor="director", reason="moved", now=NOW + timedelta(days=2)
        )

    with database.transaction() as session:
        director = ResearchDirector(session)
        assert {item.task_id for item in director.by_state(*ACTIVE)} == {"T-1", "T-2"}
        stuck = director.stuck(now=NOW + timedelta(days=1), older_than=timedelta(hours=12))
        assert [item.task_id for item in stuck] == ["T-1"]
        assert director.by_state(TaskState.CRITIQUE)[0].task_id == "T-2"


def test_the_director_has_no_route_to_the_broker_or_the_kill_switch() -> None:
    """Covered by a test, not by convention, as the acceptance criteria require.

    A research agent must not be able to place an order, enable live trading, or clear the kill
    switch. That holds because the research package has no import path to any of them, and this
    walks the graph rather than trusting the layering. #14 asserts the same boundary from the
    trading side.
    """
    research = Path(__file__).resolve().parents[3] / "src" / "quantbot" / "research"
    modules = sorted(research.glob("*.py"))
    assert len(modules) >= 10

    forbidden = (
        "quantbot.brokers",
        "quantbot.execution",
        "quantbot.operations",
        "quantbot.runtime",
    )
    offenders: list[str] = []
    for module in modules:
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                name = node.module or ""
                offenders.extend(
                    f"{module.name} imports {name}" for bad in forbidden if name.startswith(bad)
                )
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{module.name} imports {alias.name}"
                    for alias in node.names
                    for bad in forbidden
                    if alias.name.startswith(bad)
                )

    assert offenders == [], (
        "research code must have no route to order placement, live trading, or the kill "
        "switch: " + "; ".join(offenders)
    )
