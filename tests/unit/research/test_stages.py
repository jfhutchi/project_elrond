"""The first stage handler the cycle can actually run, and what it refuses to decide (#3, #6)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from quantbot.research.cycle import actionable, drain
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.scout import LiteratureSearch, SourceUnavailable
from quantbot.research.stages import novelty_stage, scouting_stage
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

class FakeScout:
    """Returns a scripted search, or raises the way a real connector raises."""

    connector = "arxiv"

    def __init__(self, search=None, error: Exception | None = None) -> None:
        self._search = search
        self._error = error
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int = 10):
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return self._search


def literature(results: tuple = (), matched: int = 0) -> LiteratureSearch:
    return LiteratureSearch(
        connector="arxiv",
        query="does revision dispersion precede index drawdowns",
        searched_at=NOW,
        total_matched=matched,
        results=results,
    )


def scouting_task(director: ResearchDirector, task_id: str) -> None:
    open_task(director, task_id, "does revision dispersion precede index drawdowns")
    director.advance(
        task_id, TaskState.SCOUTING, actor="operator", reason="ready to search", now=NOW
    )


def test_a_completed_search_that_found_nothing_still_advances(database: Database) -> None:
    """Zero literature is not a blocker.

    A genuinely novel idea has none, and treating an empty search as a failure would select
    against exactly the ideas worth having. What matters is that somebody looked, and that the
    looking is on the record.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        scouting_task(director, "T-SCOUT-1")
        scout = FakeScout(literature())

        drain(
            director,
            {
                TaskState.SCOUTING: scouting_stage(
                    scout, session, search_id=lambda t: f"LS-{t.task_id}"
                )
            },
            actor="cycle",
            now=NOW,
            max_steps=2,
        )
        moved = director.get("T-SCOUT-1")
        recorded = session.execute(
            text("SELECT outcome, query FROM literature_searches WHERE search_id = 'LS-T-SCOUT-1'")
        ).all()

    assert moved is not None and moved.state is TaskState.CRITIQUE
    assert recorded == [("NO_RESULTS", "does revision dispersion precede index drawdowns")]


def test_an_outage_blocks_the_task_and_is_recorded_as_an_outage(database: Database) -> None:
    """The distinction `ArxivScout` was built around, finally consumed by something.

    Advancing here would let "arXiv returned 503" become "nothing has been published about
    this", recorded as though somebody had looked. The task stays put and the failure is
    durable, so a later reader can tell the gap in the evidence from the gap in the search.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        scouting_task(director, "T-SCOUT-2")
        scout = FakeScout(error=SourceUnavailable("export.arxiv.org timed out"))

        drain(
            director,
            {
                TaskState.SCOUTING: scouting_stage(
                    scout, session, search_id=lambda t: f"LS-{t.task_id}"
                )
            },
            actor="cycle",
            now=NOW,
            max_steps=2,
        )
        stuck = director.get("T-SCOUT-2")
        recorded = session.execute(
            text("SELECT outcome, detail FROM literature_searches WHERE search_id = 'LS-T-SCOUT-2'")
        ).all()

    assert stuck is not None and stuck.state is TaskState.BLOCKED
    assert recorded[0][0] == "OUTAGE"
    # The exception type, never its message: a transport error can carry a URL, and a URL can
    # carry a contact or a key in some proxy formats.
    assert "SourceUnavailable" in recorded[0][1]
    assert "export.arxiv.org" not in recorded[0][1]
