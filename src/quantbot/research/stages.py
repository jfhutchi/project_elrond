"""Stage handlers the cycle can actually run today, and honesty about the ones it cannot (#3).

`run_cycle` and `drain` have existed since `d495d9e` and nothing supplied them with handlers, so
the driver was reachable only from a test. This is the first real one.

**What is here and what is not.** The research loop's stages divide into those software can
decide and those needing judgment. Novelty against durable memory is mechanical: either a record
already answers the question or it does not. Scouting is mechanical too once a connector exists --
it is a search, and the only judgment in it is what to do about an outage. Critique and experiment
design are not: they need a model's opinion (#7) or a measurement (#8), and a handler that
pretended otherwise would advance tasks through stages nobody performed, which is worse than
leaving them visibly stuck.

`actionable()` already treats a state with no handler as not-actionable rather than as an error,
so a partially built loop runs the stages it has and leaves the rest visible. That is the design
this module leans on rather than works around.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from quantbot.research.cycle import StageHandler, StageResult
from quantbot.research.director import ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, Verdict
from quantbot.research.scout import (
    LiteratureSearch,
    ScoutError,
    record_literature_outage,
    record_literature_search,
)


class LiteratureScout(Protocol):
    """What a scouting handler needs, so any connector satisfying it can be scheduled."""

    connector: str

    def search(self, query: str, *, max_results: int = 10) -> LiteratureSearch: ...

#: Verdicts that mean the question has already been answered against, so asking again spends the
#: multiple-testing budget to re-learn something durable memory already holds. `UNDERPOWERED` is
#: deliberately absent: "the data could not resolve this" is not an answer, and blocking on it
#: would make a thin sample permanently retire a live idea.
SETTLED = frozenset({Verdict.REFUTED, Verdict.LITERATURE_REFUTED})


def novelty_stage(memory: ResearchMemory) -> StageHandler:
    """Build a PROPOSED handler that refuses to re-ask a question memory already settled.

    This is the cheapest gate in the loop and belongs first for exactly that reason: it costs one
    query and can save an entire experiment plus the permanent luck-bar increment that experiment
    would have added.

    Matching is on the task's own question text against stored subjects and statements, and is
    deliberately literal -- `ResearchMemory.recall` uses substring matching rather than
    embeddings, because a wrong semantic neighbour is worse than a missed one when the answer
    decides whether to spend a holdout. A near-duplicate that slips through costs a trial; a
    false match silently retires a live idea, and nobody would ever look at it again.

    Blocks rather than terminates. `BLOCKED` says a human should look; `REFUTED` would be this
    handler claiming to have refuted something it merely recognised, and the state machine
    forbids moving out of `REFUTED` for good reason.
    """

    def handle(task: ResearchTask) -> StageResult:
        settled = [
            record
            for record in memory.recall(task.question, kinds=[RecordKind.FINDING])
            # `verdict` is optional on the model and mandatory on a FINDING, so this is a
            # narrowing rather than a guard -- but written as a membership test so a record
            # without one is skipped instead of raising inside a stage handler.
            if record.verdict is not None and record.verdict in SETTLED
        ]
        if settled:
            first = settled[0]
            verdict = first.verdict.value if first.verdict is not None else "SETTLED"
            return StageResult(
                next_state=TaskState.BLOCKED,
                reason=(
                    f"already settled by {first.record_id} ({verdict}); "
                    "re-testing would spend the budget to re-learn a stored result"
                ),
                detail={
                    "records": ", ".join(record.record_id for record in settled),
                    # The evidence, not just the count, so an operator can disagree with the
                    # match without going to look it up.
                    "statement": first.statement,
                },
            )
        return StageResult(
            next_state=TaskState.SCOUTING,
            reason="no stored record settles this question",
            detail={"searched": task.question},
        )

    return handle


def scouting_stage(
    scout: LiteratureScout,
    session: Session,
    *,
    search_id: Callable[[ResearchTask], str],
) -> StageHandler:
    """Build a SCOUTING handler that searches the literature and records that it did (#4, #13).

    The distinction this handler exists to preserve is the one `ArxivScout` was built around and
    that nothing downstream had yet consumed: **an outage is not an absence**.

    * Sources found, or a search that completed and found none -> CRITIQUE. Zero literature is
      not a blocker; a genuinely novel idea has none, and treating that as a failure would
      select against exactly the ideas worth having.
    * The source could not be reached -> BLOCKED, and the task does not advance. Advancing on an
      outage would let "arXiv returned 503" become "nothing has been published about this",
      recorded as though somebody had looked.

    Both outcomes are persisted before the state changes, so the durable record of what was
    searched exists whether or not the task moved.
    """

    def handle(task: ResearchTask) -> StageResult:
        identifier = search_id(task)
        try:
            search = scout.search(task.question)
        except ScoutError as error:
            record_literature_outage(
                session,
                search_id=identifier,
                connector=getattr(scout, "connector", "unknown"),
                query=task.question,
                attempted_at=datetime.now(UTC),
                # The exception type, not its message: a transport error can carry a URL, and a
                # URL can carry a contact or a key in some proxy formats.
                detail=f"{type(error).__name__} while searching",
            )
            return StageResult(
                next_state=TaskState.BLOCKED,
                reason=(
                    f"the literature source could not be reached ({type(error).__name__}); "
                    "an outage is not an absence of literature"
                ),
                detail={"search_id": identifier},
            )

        outcome = record_literature_search(session, search, search_id=identifier)
        return StageResult(
            next_state=TaskState.CRITIQUE,
            reason=(
                f"searched {search.connector}: {outcome.value.lower().replace('_', ' ')} "
                f"({len(search.results)} sources)"
            ),
            detail={
                "search_id": identifier,
                "outcome": outcome.value,
                "sources": ", ".join(source.source_id for source in search.results) or "none",
            },
        )

    return handle


__all__ = ["SETTLED", "novelty_stage", "scouting_stage"]
