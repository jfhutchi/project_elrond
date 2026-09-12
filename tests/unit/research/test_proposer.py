"""The autonomous producer, and every way it could invent work it has no right to do (#3, #13).

The failure this module exists to prevent is not a crash. It is a research task that looks
legitimate and is not: asked of a dataset with no data, asked twice, asked because a model said a
word nobody offered it, or asked with provenance that cannot say why.

So the tests below are organised by what could go wrong rather than by function, and the ones that
matter most assert a *refusal*. A proposer that proposes is easy; one that declines for the right
reason is the whole design.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantbot.domain import Bar
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.models import ModelUnavailable, StructuredOutputError
from quantbot.research.proposer import (
    CATALOG,
    FirstEligibleSelector,
    ModelCandidateSelector,
    ProposalRefused,
    candidate_slate,
    deterministic_task_id,
    measure_readiness,
    proposal_id,
    propose,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import research_task_events

#: After `PRIOR_WINDOW_CONSUMPTION`'s 2026-08-21 boundary, so seeded sessions are genuinely
#: unsearched rather than part of the range cycles 2-10 already spent.
NOW = datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
TODAY = NOW.date()
SIP = "alpaca:sip:split:1Day"

#: Enough symbols for a cross-section, including the declared benchmark.
UNIVERSE = ("SPY", "QQQ", "IWM", "XLK")


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "proposer.db")
    yield db
    db.close()



def _bar(symbol: str, timestamp: datetime, price: Decimal) -> Bar:
    """One bar whose high and low actually bracket the close, which `Bar` requires."""
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("1000"),
        adjustment=Decimal("1"),
    )


def seed_sessions(session, *days: str, provider: str = SIP, symbols=UNIVERSE) -> None:
    """Store one daily bar per symbol per session, stamped at the Eastern close.

    Prices vary per symbol and per day so a cross-sectional statistic is not degenerate. A flat
    panel would make every measurement return a constant, which is a different test.
    """
    repository = StorageRepository(session)
    for index, day in enumerate(days):
        close = datetime.combine(
            date.fromisoformat(day), time(16, 0), tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)
        repository.save_bars(
            [
                _bar(symbol, close, Decimal(100 + index + position * 3))
                for position, symbol in enumerate(symbols)
            ],
            provider=provider,
        )


def readiness_for(session, *, as_of: date = TODAY):
    return measure_readiness(session, as_of=as_of)


# --- candidate generation -----------------------------------------------------------------------


def test_a_dataset_with_unsearched_sessions_produces_an_eligible_candidate(
    database: Database,
) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25", "2026-08-26")
        eligible, _ = candidate_slate(readiness_for(session), existing_task_ids=frozenset())

    assert eligible, "three unsearched sessions on the equity source should be proposable"
    assert {entry.readiness.dataset for entry in eligible} == {"sip-us-equities-daily"}


def test_a_declared_dataset_with_no_observations_is_refused(database: Database) -> None:
    """The trap this proposer exists to avoid.

    `crypto-daily`, `fred-macro-monthly` and `sec-edgar-filings` are all `AVAILABLE` with zero
    trials spent, so a proposer reading `status` would offer all three confidently. None of them
    has a single observation in this store, and nothing can be confirmed against data that is not
    here.
    """
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24")
        rows = {entry.dataset: entry for entry in readiness_for(session)}

    for empty in ("crypto-daily", "fred-macro-monthly", "sec-edgar-filings"):
        row = rows[empty]
        assert row.has_confirmatory_capacity, (
            f"{empty} is supposed to have capacity; if it does not, this test is measuring "
            "something other than the data-availability check"
        )
        assert row.eligible is False
        assert row.stored_sessions == 0
        assert "no observations" in row.reason


def test_an_unsupported_source_is_refused(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24")
        rows = {entry.dataset: entry for entry in readiness_for(session)}

    options = rows["options-implied-daily"]
    assert options.eligible is False
    assert options.has_confirmatory_capacity is False


def test_a_fully_searched_dataset_with_no_new_sessions_is_refused(database: Database) -> None:
    """Every stored session falls inside the range cycles 2-10 already spent.

    Note which refusal this is. Data *is* present -- three stored sessions -- so this is not the
    empty-dataset path. `CapacityRow.usable_for_confirmation` is False because the range is
    `EXHAUSTED` and the unsearched-session rescue has nothing to rescue it with, so the capacity
    branch answers first. The distinction matters: "spent" and "absent" are different states and
    an operator reading the reason has to be able to tell which one they are looking at.
    """
    with database.transaction() as session:
        seed_sessions(session, "2026-08-18", "2026-08-19", "2026-08-20")
        rows = {entry.dataset: entry for entry in readiness_for(session)}

    equities = rows["sip-us-equities-daily"]
    assert equities.stored_sessions == 3, "the bars are stored; they are simply already searched"
    assert equities.unsearched_sessions == 0
    assert equities.eligible is False
    assert equities.reason == "exhausted with no clean confirmatory capacity"
    assert "no observations" not in equities.reason, (
        "a spent dataset must not be reported as an absent one"
    )


def test_an_existing_equivalent_task_suppresses_the_duplicate(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        rows = readiness_for(session)
        equities = next(entry for entry in rows if entry.dataset == "sip-us-equities-daily")
        taken = deterministic_task_id(CATALOG[0], equities)

        eligible, rejected = candidate_slate(rows, existing_task_ids=frozenset({taken}))

    assert taken not in {entry.task_id for entry in eligible}
    suppressed = next(entry for entry in rejected if entry.task_id == taken)
    assert suppressed.reason == "an equivalent task already exists"


def test_the_slate_is_ordered_deterministically(database: Database) -> None:
    """A model shown a differently-ordered slate is a different experiment."""
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        rows = readiness_for(session)
        first, _ = candidate_slate(rows, existing_task_ids=frozenset())
        second, _ = candidate_slate(rows, existing_task_ids=frozenset())

    assert [entry.task_id for entry in first] == [entry.task_id for entry in second]
    assert [entry.task_id for entry in first] == sorted(entry.task_id for entry in first)


def test_a_template_naming_an_unknown_dataset_is_a_catalogue_defect(database: Database) -> None:
    """A typo must not read as "no constraint". It stops the proposer, loudly."""
    broken = type(CATALOG[0])(
        **{
            **{field: getattr(CATALOG[0], field) for field in CATALOG[0].__slots__},
            "required_datasets": ("sip-us-equties-daily",),  # deliberate typo
        }
    )
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24")
        with pytest.raises(ProposalRefused, match="not a declared source"):
            candidate_slate(
                readiness_for(session), existing_task_ids=frozenset(), catalog=(broken,)
            )


# --- model behaviour ------------------------------------------------------------------------------


class _Runtime:
    """A model runtime that answers however the test needs, without a socket."""

    def __init__(self, answer=None, error: Exception | None = None) -> None:
        self._answer = answer
        self._error = error
        self.calls = 0

    def call_structured(self, role, template, values, schema, *, now):  # noqa: ANN001, ARG002
        self.calls += 1
        if self._error is not None:
            raise self._error
        response = type(
            "Response",
            (),
            {
                "spec": type(
                    "Spec", (), {"provider": "ollama", "model": "test-model", "version": "1"}
                )(),
                "template": template,
                "response_hash": "a" * 64,
                "is_fallback": False,
            },
        )()
        return self._answer, response


def _choice(template_id: str, reason: str = "most informative"):
    from quantbot.research.proposer import CandidateChoice

    return CandidateChoice(template_id=template_id, reason=reason)


def _slate(session):
    rows = readiness_for(session)
    eligible, _ = candidate_slate(rows, existing_task_ids=frozenset())
    return eligible


def test_a_valid_structured_choice_is_accepted(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        eligible = _slate(session)
        wanted = eligible[0].template.template_id
        selector = ModelCandidateSelector(
            _Runtime(answer=_choice(wanted, "it has the most unsearched capacity"))
        )
        selection = selector.select(eligible, context={})

    assert selection.candidate.template.template_id == wanted
    assert selection.selector == "model"
    assert selection.model_identity == "ollama:test-model@1"
    assert selection.model_response_hash == "a" * 64
    assert selection.reason == "it has the most unsearched capacity"


def test_a_choice_outside_the_slate_is_refused(database: Database) -> None:
    """The hallucination case. A question nobody offered must not become a task."""
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        eligible = _slate(session)
        selector = ModelCandidateSelector(_Runtime(answer=_choice("invent-a-new-edge")))

        with pytest.raises(ProposalRefused, match="not among the"):
            selector.select(eligible, context={})


def test_malformed_model_output_is_refused(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        eligible = _slate(session)
        selector = ModelCandidateSelector(
            _Runtime(error=StructuredOutputError("test-model did not return JSON"))
        )

        with pytest.raises(ProposalRefused, match="did not produce a usable choice"):
            selector.select(eligible, context={})


def test_an_unavailable_model_fails_closed(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        eligible = _slate(session)
        selector = ModelCandidateSelector(_Runtime(error=ModelUnavailable("no endpoint")))

        with pytest.raises(ProposalRefused, match="did not produce a usable choice"):
            selector.select(eligible, context={})


def test_a_selector_is_never_asked_to_choose_from_nothing(database: Database) -> None:
    runtime = _Runtime(answer=_choice("anything"))
    selector = ModelCandidateSelector(runtime)

    with pytest.raises(ProposalRefused, match="no eligible candidate"):
        selector.select((), context={})
    assert runtime.calls == 0, "an empty slate must not spend a model call"


# --- persistence, identity and restart safety -----------------------------------------------------


def test_a_proposal_opens_one_task_and_records_why(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25", "2026-08-26")
        outcome = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit="abc123",
        )
        assert outcome.ok
        task = outcome.created
        assert task is not None
        assert task.state is TaskState.PROPOSED
        assert task.required_datasets == ("sip-us-equities-daily",)

        rows = session.execute(
            research_task_events.select().where(
                research_task_events.c.task_id == task.task_id
            )
        ).mappings().all()

    assert len(rows) == 1
    import json

    detail = json.loads(rows[0]["detail_json"])
    # Provenance has to answer every question an auditor would ask of an autonomous decision.
    for field in (
        "proposal_id",
        "proposer_version",
        "code_commit",
        "proposed_at",
        "template_id",
        "template_version",
        "catalogue_hash",
        "measurement_id",
        "dataset",
        "dataset_status",
        "dataset_trials_spent",
        "dataset_unsearched_sessions",
        "capacity_class",
        "novelty_decision",
        "selector",
        "selection_reason",
        "alternatives_considered",
    ):
        assert field in detail and detail[field], f"provenance is missing {field}"
    assert detail["code_commit"] == "abc123"
    assert detail["dataset_unsearched_sessions"] == "3"


def test_task_identity_does_not_move_when_more_data_arrives(database: Database) -> None:
    """Otherwise the same question is re-proposed every session and nothing suppresses it."""
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24")
        early = readiness_for(session)
        seed_sessions(session, "2026-08-25", "2026-08-26")
        later = readiness_for(session)

    template = CATALOG[0]
    first = deterministic_task_id(
        template, next(r for r in early if r.dataset == "sip-us-equities-daily")
    )
    second = deterministic_task_id(
        template, next(r for r in later if r.dataset == "sip-us-equities-daily")
    )
    assert first == second


def test_a_rerun_against_unchanged_state_creates_nothing_new(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        first = propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc")
        assert first.ok

    with database.transaction() as session:
        second = propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc")

    with database.transaction() as session:
        tasks = ResearchDirector(session).by_state()

    # The second run proposes the *next* catalogue question rather than repeating the first,
    # which is progress, not a duplicate. What must never happen is two rows for one question.
    assert second.created is None or second.created.task_id != first.created.task_id
    assert len({task.task_id for task in tasks}) == len(tasks)
    assert first.created is not None
    assert sum(1 for task in tasks if task.task_id == first.created.task_id) == 1


def test_the_whole_catalogue_drains_and_then_proposes_nothing(database: Database) -> None:
    """The steady state an unattended loop reaches: everything asked, nothing repeated."""
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        for _ in range(len(CATALOG)):
            propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc")
        exhausted = propose(
            session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc"
        )
        tasks = ResearchDirector(session).by_state()

    assert len(tasks) == len(CATALOG)
    assert exhausted.created is None
    assert "no eligible candidate" in exhausted.reason


def test_a_rolled_back_transaction_leaves_no_task_behind(database: Database) -> None:
    """A restart mid-proposal must not leave a task whose provenance was never written."""

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        with database.transaction() as session:
            seed_sessions(session, "2026-08-24", "2026-08-25")
            outcome = propose(
                session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc"
            )
            assert outcome.ok
            raise _Boom

    with database.transaction() as session:
        assert ResearchDirector(session).by_state() == []
        assert (
            session.execute(research_task_events.select()).mappings().all() == []
        ), "an event survived a rolled-back proposal"


def test_a_second_attempt_on_the_same_identity_reports_rather_than_duplicating(
    database: Database,
) -> None:
    """The race, forced: the task exists but the caller's view of existing ids is stale."""
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        first = propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc")
        assert first.created is not None

        director = ResearchDirector(session)
        duplicate = ResearchTask(
            task_id=first.created.task_id,
            state=TaskState.PROPOSED,
            question="a second row for one question",
            family_id="whatever",
            created_at=NOW,
            updated_at=NOW,
        )
        with pytest.raises(ValueError, match="already exists"):
            director.open_task(duplicate, actor="test", reason="must not be allowed")


def test_the_proposal_id_moves_with_state_even_when_the_task_id_does_not(
    database: Database,
) -> None:
    """A suppressed duplicate and a proposal that never happened have to be tellable apart.

    `task_id` is the question and must not move when data arrives. `proposal_id` is the
    occasion, so it must.
    """
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24")
        early_rows = readiness_for(session)
        seed_sessions(session, "2026-08-25", "2026-08-26")
        later_rows = readiness_for(session)

    early = next(r for r in early_rows if r.dataset == "sip-us-equities-daily")
    later = next(r for r in later_rows if r.dataset == "sip-us-equities-daily")
    eligible, _ = candidate_slate(
        early_rows, existing_task_ids=frozenset(), catalog=(CATALOG[0],)
    )
    selection = FirstEligibleSelector().select(eligible, context={})

    assert deterministic_task_id(CATALOG[0], early) == deterministic_task_id(CATALOG[0], later)
    assert proposal_id(selection, early) != proposal_id(selection, later)


def test_one_call_opens_at_most_one_task(database: Database) -> None:
    """A producer that could open several is one bug away from spending the whole budget."""
    with database.transaction() as session:
        seed_sessions(session, "2026-08-24", "2026-08-25")
        propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="abc")
        tasks = ResearchDirector(session).by_state()

    assert len(tasks) == 1


# --- the schema the server is actually asked to compile ---------------------------------------


def test_the_emitted_schema_carries_no_string_length_constraints() -> None:
    """A grammar the model server cannot compile makes every selection call fail.

    `call_structured` sends `model_json_schema()` to the endpoint to constrain decoding. Ollama's
    grammar compiler rejects `minLength`/`maxLength` on a string property with
    `HTTP 400 Failed to initialize samplers: failed to parse grammar`, and the proposer then
    reports a refusal indistinguishable from the model being down -- which is exactly what
    happened on 2026-09-12 until the constraints were moved into a validator.

    So this asserts the shape of the request, not the behaviour of the parser. Re-adding
    `min_length=` to either field turns it red.
    """
    from quantbot.research.proposer import CandidateChoice

    schema = CandidateChoice.model_json_schema()
    for name, spec in schema["properties"].items():
        assert set(spec) <= {"title", "type"}, (
            f"{name} carries {sorted(set(spec) - {'title', 'type'})}, which the grammar "
            "compiler may refuse; bounds belong in validate_choice"
        )


def test_the_length_bounds_are_still_enforced_after_parsing() -> None:
    """Moving them out of the schema must not mean dropping them."""
    import pydantic

    from quantbot.research.proposer import CandidateChoice

    assert CandidateChoice(template_id="a", reason="b").template_id == "a"
    with pytest.raises(pydantic.ValidationError):
        CandidateChoice(template_id="a" * 129, reason="b")
    with pytest.raises(pydantic.ValidationError):
        CandidateChoice(template_id="a", reason="b" * 2001)
    with pytest.raises(pydantic.ValidationError):
        CandidateChoice(template_id="   ", reason="b")
    with pytest.raises(pydantic.ValidationError):
        CandidateChoice(template_id="a", reason="   ")


def test_an_answer_with_extra_fields_is_refused() -> None:
    """`extra="forbid"`, so a model that volunteers a verdict field does not get one read."""
    import pydantic

    from quantbot.research.proposer import CandidateChoice

    with pytest.raises(pydantic.ValidationError):
        CandidateChoice.model_validate(
            {"template_id": "a", "reason": "b", "verdict": "SURVIVED"}
        )
