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
from quantbot.research.stages import capacity_stage, novelty_stage, scouting_stage
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database.create(tmp_path / "stages.db")
    yield db
    db.close()


def open_task(
    director: ResearchDirector,
    task_id: str,
    question: str,
    required_datasets: tuple[str, ...] = (),
) -> None:
    director.open_task(
        ResearchTask(
            task_id=task_id,
            state=TaskState.PROPOSED,
            question=question,
            family_id="momentum",
            required_datasets=required_datasets,
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


# ---------------------------------------------------------------------------------------------
# The capacity gate (#50). `discovery.admissible` existed and was reachable only from a module
# no production path imported, so the rule that a search should not run where nothing could
# confirm it was a rule rather than a mechanism.
# ---------------------------------------------------------------------------------------------


def test_a_question_advances_while_any_source_still_has_confirmatory_capacity(
    database: Database,
) -> None:
    """The gate is not simply refusing everything: crypto, macro and filings are untouched."""
    with database.transaction() as session:
        director = ResearchDirector(session)
        open_task(director, "task-capacity-ok", "Do overnight returns follow volume shocks?")
        handler = capacity_stage(session, novelty_stage(ResearchMemory(session)))

        result = handler(director.by_state()[0])

    assert result.next_state is TaskState.SCOUTING


def test_a_question_is_blocked_before_compute_when_nothing_can_confirm_it(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#50's cheapest correct outcome, and the one nothing produced before.

    An expensive search followed by an attractive result that can never be confirmed is strictly
    worse than refusing at the door: it spends compute *and* permanently raises the luck bar for
    everything registered afterwards.

    Every declared source is driven to a state with no usable capacity rather than mocking the
    gate, so what is under test is the handler reading the inventory rather than a stub.
    """
    import quantbot.research.stages as stages

    monkeypatch.setattr(stages, "capacity_inventory", lambda session, *, as_of: ())

    with database.transaction() as session:
        director = ResearchDirector(session)
        open_task(director, "task-no-capacity", "Does momentum work on US equities?")
        handler = capacity_stage(session, novelty_stage(ResearchMemory(session)))

        result = handler(director.by_state()[0])

    assert result.next_state is TaskState.BLOCKED
    assert "no clean confirmatory capacity" in result.reason
    assert "exploratorily" in result.reason


class FakeRow:
    """Enough of a `CapacityRow` for the gate: an identity, a status and whether it is usable.

    Driving the real inventory to a chosen per-source state needs consumption records for every
    declared source, which would test the inventory rather than the gate. The gate's contract is
    exactly these three attributes.
    """

    def __init__(self, dataset: str, usable: bool) -> None:
        self.source = type("Source", (), {"dataset": dataset})()
        self.observation_identity = dataset
        self.status = type("Status", (), {"value": "AVAILABLE" if usable else "EXHAUSTED"})()
        self.usable_for_confirmation = usable


#: US equities spent, macro untouched. The two gates disagree on this inventory, which is what
#: makes it worth testing: the project-wide one advances an equity question on macro capacity.
MIXED = (FakeRow("sip-us-equities-daily", False), FakeRow("fred-macro-monthly", True))


def test_a_declared_dataset_is_checked_specifically_rather_than_project_wide(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sol's condition for closing #50: a question declares what it needs, in structured data.

    The project-wide gate passes whenever *anything* is unspent, which is weaker than it sounds.
    A question about US equities advancing on the strength of untouched macro capacity is exactly
    the significance-already-spent failure the inventory exists to prevent.
    """
    import quantbot.research.stages as stages

    monkeypatch.setattr(stages, "capacity_inventory", lambda session, *, as_of: MIXED)

    with database.transaction() as session:
        director = ResearchDirector(session)
        open_task(
            director,
            "task-declared-spent",
            "Does momentum work on US equities?",
            required_datasets=("sip-us-equities-daily",),
        )
        open_task(
            director,
            "task-declared-open",
            "Do macro regimes shift with policy?",
            required_datasets=("fred-macro-monthly",),
        )
        handler = capacity_stage(session, novelty_stage(ResearchMemory(session)))
        tasks = {task.task_id: task for task in director.by_state()}

        spent = handler(tasks["task-declared-spent"])
        available = handler(tasks["task-declared-open"])

    assert spent.next_state is TaskState.BLOCKED
    assert "sip-us-equities-daily" in spent.reason
    assert "declared it as required" in spent.reason

    # Same inventory, and the gate is not simply refusing everything.
    assert available.next_state is TaskState.SCOUTING


def test_every_declared_dataset_must_have_capacity_not_merely_one(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim needing two sources needs both.

    Advancing on the strength of whichever happens to be unspent is the permissive direction, and
    it is the sort of `any`-where-`all`-was-meant that reads correct at a glance.
    """
    import quantbot.research.stages as stages

    monkeypatch.setattr(stages, "capacity_inventory", lambda session, *, as_of: MIXED)

    with database.transaction() as session:
        director = ResearchDirector(session)
        open_task(
            director,
            "task-needs-both",
            "Do macro regimes change equity momentum?",
            required_datasets=("fred-macro-monthly", "sip-us-equities-daily"),
        )
        handler = capacity_stage(session, novelty_stage(ResearchMemory(session)))
        result = handler(director.by_state()[0])

    assert result.next_state is TaskState.BLOCKED
    assert "sip-us-equities-daily" in result.reason


def test_an_unrecognised_declared_dataset_blocks_rather_than_meaning_no_constraint(
    database: Database,
) -> None:
    """A typo must not buy unlimited capacity.

    If an unknown name were skipped, the cheapest route past this gate would be to misspell the
    source a question depends on, and the mistake would present as a clean advance.
    """
    with database.transaction() as session:
        director = ResearchDirector(session)
        open_task(
            director,
            "task-typo",
            "Does momentum work on US equities?",
            required_datasets=("sip-us-equities-dialy",),
        )
        handler = capacity_stage(session, novelty_stage(ResearchMemory(session)))
        result = handler(director.by_state()[0])

    assert result.next_state is TaskState.BLOCKED
    assert "not a declared source" in result.reason
    assert "must not read as an unconstrained one" in result.reason


def test_an_undeclared_question_still_gets_the_project_wide_gate(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is weaker, not absent, and declaring nothing must not skip the check."""
    import quantbot.research.stages as stages

    monkeypatch.setattr(stages, "capacity_inventory", lambda session, *, as_of: ())

    with database.transaction() as session:
        director = ResearchDirector(session)
        open_task(director, "task-undeclared", "Anything at all?")
        handler = capacity_stage(session, novelty_stage(ResearchMemory(session)))
        result = handler(director.by_state()[0])

    assert result.next_state is TaskState.BLOCKED
    assert "any declared source" in result.reason


def test_a_declaration_survives_the_round_trip_through_durable_state(
    database: Database,
) -> None:
    """The gate reads the stored task, not the object a caller built.

    `required_datasets` rides in `document_json`, so it needs no migration -- and a field that
    serialised but did not deserialise would make every stored task read as undeclared, silently
    downgrading every per-claim check to the weaker project-wide one.
    """
    with database.transaction() as session:
        open_task(
            ResearchDirector(session),
            "task-round-trip",
            "Does momentum work on US equities?",
            required_datasets=("sip-us-equities-daily",),
        )

    with database.transaction() as session:
        stored = ResearchDirector(session).by_state()[0]

    assert stored.required_datasets == ("sip-us-equities-daily",)


def test_the_capacity_gate_runs_before_the_memory_query(database: Database) -> None:
    """Cheaper first. A question nothing could confirm should not consume a memory query either.

    Asserted by giving the inner handler something that would fail loudly if reached, rather
    than by reading the implementation.
    """
    import quantbot.research.stages as stages

    reached: list[str] = []

    def inner(task: ResearchTask) -> object:
        reached.append(task.task_id)
        raise AssertionError("the novelty query ran despite there being nowhere to confirm")

    original = stages.capacity_inventory
    stages.capacity_inventory = lambda session, *, as_of: ()  # type: ignore[assignment]
    try:
        with database.transaction() as session:
            director = ResearchDirector(session)
            open_task(director, "task-order", "Anything at all?")
            handler = capacity_stage(session, inner)  # type: ignore[arg-type]
            result = handler(director.by_state()[0])
    finally:
        stages.capacity_inventory = original  # type: ignore[assignment]

    assert reached == [], "the inner handler must not have been called"
    assert result.next_state is TaskState.BLOCKED
