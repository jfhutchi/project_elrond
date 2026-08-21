"""The first stage handler the cycle can actually run, and what it refuses to decide (#3, #6)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from quantbot.research.cycle import actionable, drain
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.stages import novelty_stage
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "stages.db")
    yield db
    db.close()


def open_task(director: ResearchDirector, task_id: str, question: str) -> None:
    director.open_task(
        ResearchTask(
            task_id=task_id,
            state=TaskState.PROPOSED,
            question=question,
            family_id="momentum",
            created_at=NOW,
            updated_at=NOW,
        ),
        actor="operator",
        reason="opened for the stage test",
    )


def remember(memory: ResearchMemory, record_id: str, subject: str, verdict: Verdict) -> None:
    memory.record(
        ResearchRecord(
            record_id=record_id,
            kind=RecordKind.FINDING,
            verdict=verdict,
            subject=subject,
            statement=f"{subject}: measured against a bar it did not clear.",
            source="cycle 15",
            recorded_at=NOW,
        )
    )


def test_a_question_memory_already_settled_is_blocked_not_re_tested(database: Database) -> None:
    """The cheapest gate in the loop, and the one that saves the most.

    One query, against an entire experiment plus the permanent luck-bar increment that
    experiment would have added to everything registered after it.
    """
    with database.transaction() as session:
        memory = ResearchMemory(session)
        remember(memory, "finding-1", "momentum ranking on US sector ETFs", Verdict.REFUTED)
        director = ResearchDirector(session)
        open_task(director, "T-1", "momentum ranking on US sector ETFs")

        outcomes = drain(
            director,
            {TaskState.PROPOSED: novelty_stage(memory)},
            actor="cycle",
            now=NOW,
            max_steps=2,
        )
        moved = director.get("T-1")

    assert moved is not None and moved.state is TaskState.BLOCKED
    assert any("already settled by finding-1" in outcome.reason for outcome in outcomes)


def test_a_novel_question_advances_to_scouting(database: Database) -> None:
    """The counterweight: a gate that blocked everything would pass the test above too."""
    with database.transaction() as session:
        memory = ResearchMemory(session)
        remember(memory, "finding-1", "momentum ranking on US sector ETFs", Verdict.REFUTED)
        director = ResearchDirector(session)
        open_task(director, "T-2", "does revision dispersion precede index drawdowns")

        drain(
            director,
            {TaskState.PROPOSED: novelty_stage(memory)},
            actor="cycle",
            now=NOW,
            max_steps=2,
        )
        moved = director.get("T-2")

    assert moved is not None and moved.state is TaskState.SCOUTING


def test_an_underpowered_record_does_not_settle_anything(database: Database) -> None:
    """"The data could not resolve this" is not an answer.

    Blocking on it would let a thin sample permanently retire a live idea, which is the exact
    conflation `Verdict.UNDERPOWERED` exists to prevent. It stays out of `SETTLED` on purpose.
    """
    with database.transaction() as session:
        memory = ResearchMemory(session)
        remember(memory, "finding-thin", "BTC trend standalone", Verdict.UNDERPOWERED)
        director = ResearchDirector(session)
        open_task(director, "T-3", "BTC trend standalone")

        drain(
            director,
            {TaskState.PROPOSED: novelty_stage(memory)},
            actor="cycle",
            now=NOW,
            max_steps=2,
        )
        moved = director.get("T-3")

    assert moved is not None and moved.state is TaskState.SCOUTING


def test_a_state_with_no_handler_leaves_the_task_visibly_stuck(database: Database) -> None:
    """A partially built loop runs the stages it has rather than refusing to start.

    Scouting needs a model and experiment design needs a measurement. Advancing those anyway
    would move a task through a stage nobody performed, which is worse than leaving it stuck
    where an operator can see it.
    """
    with database.transaction() as session:
        memory = ResearchMemory(session)
        director = ResearchDirector(session)
        open_task(director, "T-4", "a question nobody has settled")
        stages = {TaskState.PROPOSED: novelty_stage(memory)}

        drain(director, stages, actor="cycle", now=NOW, max_steps=4)
        after = director.get("T-4")
        still_runnable = actionable(director, stages)

    assert after is not None and after.state is TaskState.SCOUTING
    assert still_runnable == [], "SCOUTING has no handler, so nothing further is actionable"
