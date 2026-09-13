"""The autonomous producer, and every way it could invent work it has no right to do (#3, #13).

The failure this module exists to prevent is not a crash. It is a research task that looks
legitimate and is not: asked of a dataset with no data, asked twice, asked of sessions the question
has already seen, asked over a refutation nobody adjudicated, asked because a model said a word
nobody offered it, or asked with provenance that cannot say why.

So the tests below are organised by what could go wrong rather than by function, and the ones that
matter most assert a *refusal*. A proposer that proposes is easy; one that declines for the right
reason, and leaves a record of declining, is the whole design.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from quantbot.domain import Bar
from quantbot.research import proposer as proposer_module
from quantbot.research.capacity import capacity_inventory
from quantbot.research.composition import ModelConfiguration, build_model_runtime
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.power import Estimand
from quantbot.research.prior import load_prior_findings
from quantbot.research.proposer import (
    CATALOG,
    CREATED,
    NO_ELIGIBLE,
    REFUSED,
    CatalogueDefect,
    FirstEligibleSelector,
    MemoryState,
    ModelCandidateSelector,
    ProposalRefused,
    ProposalTemplate,
    candidate_slate,
    commit_proposal,
    deterministic_task_id,
    measure_readiness,
    proposal_for_task,
    propose,
    select_candidate,
    survey,
    template_by_id,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import research_proposals, research_task_events, research_tasks

#: Saturday after the fixture's last session. `PRIOR_WINDOW_CONSUMPTION` ends 2026-08-21, so the
#: seeded sessions on either side of it are genuinely consumed and genuinely unsearched.
NOW = datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
TODAY = NOW.date()
COMMIT = "0123456789abcdef0123456789abcdef01234567"
IEX = "alpaca:iex:all:1day"
UNIVERSE = ("SPY", "QQQ", "IWM", "XLK")

CONSUMED = ("2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21")
UNSEARCHED = ("2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28")


def _template(template_id: str = "probe-question", **overrides: object) -> ProposalTemplate:
    """A catalogue entry with no exposure and nothing to adjudicate, for the generic rules."""
    fields: dict[str, object] = {
        "template_id": template_id,
        "version": 1,
        "family_id": "test-family",
        "question": f"Does {template_id} predict anything at all?",
        "required_datasets": ("sip-us-equities-daily",),
        "estimand": Estimand.INFORMATION_COEFFICIENT,
        "expected_effect": Decimal("0.20"),
        "minimum_practical_effect": Decimal("0.10"),
        "measurement_id": "benchmark_absolute_move_persistence",
        "cluster_size": 1,
        "rationale": "a test question",
        "subject_terms": ("probe",),
    }
    fields.update(overrides)
    return ProposalTemplate(**fields)  # type: ignore[arg-type]


PROBE = _template()
OTHER = _template("other-question", subject_terms=("other",))


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "proposer.db")
    yield db
    db.close()


def _bar(symbol: str, timestamp: datetime, price: Decimal) -> Bar:
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


def seed_sessions(session, *days: str, provider: str = IEX, symbols=UNIVERSE) -> None:
    """One daily bar per symbol per session, stamped at the Eastern close."""
    repository = StorageRepository(session)
    for index, day in enumerate(days):
        close = datetime.combine(
            date.fromisoformat(day), time(16, 0), tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)
        repository.save_bars(
            [
                _bar(symbol, close, Decimal(100 + (index * (position + 1)) % 7 + position))
                for position, symbol in enumerate(symbols)
            ],
            provider=provider,
        )


def seed_both_halves(session) -> None:
    seed_sessions(session, *CONSUMED, *UNSEARCHED)


def _slate(session, catalog=(PROBE,)):
    return candidate_slate(
        measure_readiness(session, as_of=TODAY), memory=MemoryState.read(session), catalog=catalog
    )


def _count(session, table) -> int:
    return int(session.execute(select(func.count()).select_from(table)).scalar_one())


# --- candidate generation -----------------------------------------------------------------------


def test_a_dataset_with_clean_sessions_produces_an_eligible_candidate(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        eligible, rejected = _slate(session)

    assert [entry.template.template_id for entry in eligible] == ["probe-question"]
    assert rejected == ()
    assert eligible[0].clean_sessions == tuple(date.fromisoformat(day) for day in UNSEARCHED)
    assert eligible[0].readiness.consumed_through == date(2026, 8, 21)


def test_a_fully_searched_dataset_is_refused(database: Database) -> None:
    """Exhausted with nothing new: the searched range can only ever be exploratory."""
    with database.transaction() as session:
        seed_sessions(session, *CONSUMED)
        eligible, rejected = _slate(session)

    assert eligible == ()
    assert "exhausted with no clean confirmatory capacity" in rejected[0].reason


def test_a_declared_dataset_with_no_observations_is_refused(database: Database) -> None:
    """`AVAILABLE` with zero trials and no data. Reading status alone would propose here."""
    crypto = _template("crypto-question", required_datasets=("crypto-daily",))
    with database.transaction() as session:
        seed_both_halves(session)
        rows = {row.dataset: row for row in measure_readiness(session, as_of=TODAY)}
        eligible, rejected = _slate(session, catalog=(crypto,))

    assert rows["crypto-daily"].status == "AVAILABLE"
    assert rows["crypto-daily"].stored_sessions == 0
    assert eligible == ()
    assert "no observations in this store" in rejected[0].reason


def test_an_unsupported_source_is_refused(database: Database) -> None:
    options = _template("options-question", required_datasets=("options-implied-daily",))
    with database.transaction() as session:
        seed_both_halves(session)
        eligible, rejected = _slate(session, catalog=(options,))

    assert eligible == ()
    assert "unsupported" in rejected[0].reason


def test_an_existing_equivalent_task_suppresses_the_duplicate(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        first = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
        eligible, rejected = _slate(session)

    assert first.outcome == CREATED
    assert eligible == ()
    assert rejected[0].reason == "an equivalent task already exists"


def test_an_unadjudicated_refutation_matching_the_subject_makes_a_question_ineligible(
    database: Database,
) -> None:
    """A refutation recorded tomorrow retires the question today until somebody says why not."""
    with database.transaction() as session:
        seed_both_halves(session)
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="refuted-probe",
                kind=RecordKind.FINDING,
                verdict=Verdict.REFUTED,
                subject="probe signals as a return source",
                statement="measured and failed",
                source="test",
                recorded_at=NOW,
            )
        )
        eligible, rejected = _slate(session)

    assert eligible == ()
    assert "refuted-probe" in rejected[0].reason
    assert rejected[0].matched_refutations == ("refuted-probe",)


def test_an_adjudicated_refutation_is_recorded_and_does_not_block(database: Database) -> None:
    adjudicated = replace(
        PROBE, adjudicated_refutations=(("refuted-probe", "a different claim, and why"),)
    )
    with database.transaction() as session:
        seed_both_halves(session)
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="refuted-probe",
                kind=RecordKind.FINDING,
                verdict=Verdict.REFUTED,
                subject="probe signals as a return source",
                statement="measured and failed",
                source="test",
                recorded_at=NOW,
            )
        )
        outcome = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(adjudicated,),
        )

    assert outcome.outcome == CREATED
    assert outcome.provenance["refutations_matched"] == "refuted-probe"
    assert "a different claim, and why" in outcome.provenance["refutation_adjudications"]


def test_an_underpowered_record_does_not_count_as_a_refutation(database: Database) -> None:
    """ "We could not test this" must not retire a question the way "this failed" does."""
    with database.transaction() as session:
        seed_both_halves(session)
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="underpowered-probe",
                kind=RecordKind.FINDING,
                verdict=Verdict.UNDERPOWERED,
                subject="probe signals",
                statement="too few observations",
                source="test",
                recorded_at=NOW,
            )
        )
        eligible, _ = _slate(session)

    assert [entry.template.template_id for entry in eligible] == ["probe-question"]


def test_a_refuted_autonomous_task_retires_its_question_at_every_version(
    database: Database,
) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
        assert outcome.created is not None
        director = ResearchDirector(session)
        director.advance(
            outcome.created.task_id, TaskState.SCOUTING, actor="t", reason="t", now=NOW
        )
        director.advance(outcome.created.task_id, TaskState.REFUTED, actor="t", reason="t", now=NOW)
        eligible, rejected = _slate(session, catalog=(replace(PROBE, version=2),))

    assert eligible == ()
    assert "was refuted" in rejected[0].reason


def test_the_real_catalogue_protects_the_sessions_a_proof_run_already_saw(
    database: Database,
) -> None:
    """The three first questions were measured through 2026-09-11 by a run that read the holdout.

    Those sessions are unsearched for the project and spent for those questions. Deleting
    `exposed_through` from the catalogue makes all three eligible here, and this fails.
    """
    with database.transaction() as session:
        seed_both_halves(session)
        load_prior_findings(ResearchMemory(session))
        eligible, rejected = _slate(session, catalog=CATALOG)

    by_id = {entry.template.template_id: entry for entry in (*eligible, *rejected)}
    for exposed in (
        "dispersion-precedes-volatility",
        "breadth-persists-next-session",
        "dispersion-predicts-benchmark-direction",
    ):
        assert not by_id[exposed].eligible
        assert "already seen by this question" in by_id[exposed].reason
        assert by_id[exposed].clean_sessions == ()
    assert [entry.template.template_id for entry in eligible] == ["benchmark-move-size-persists"]


def test_exposure_ends_where_the_template_says_and_later_sessions_are_clean(
    database: Database,
) -> None:
    with database.transaction() as session:
        seed_sessions(session, *CONSUMED, *UNSEARCHED, "2026-09-14", "2026-09-15")
        load_prior_findings(ResearchMemory(session))
        candidates = candidate_slate(
            measure_readiness(session, as_of=date(2026, 9, 16)),
            memory=MemoryState.read(session),
            catalog=(template_by_id("dispersion-predicts-benchmark-direction"),),
        )

    eligible = candidates[0]
    assert eligible[0].clean_sessions == (date(2026, 9, 14), date(2026, 9, 15))


def test_one_autonomous_question_per_dataset_at_a_time(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        first = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE, OTHER),
        )
        eligible, rejected = _slate(session, catalog=(PROBE, OTHER))

    assert first.created is not None
    assert eligible == ()
    blocked = next(entry for entry in rejected if entry.task_id != first.created.task_id)
    assert f"{first.created.task_id} is still PROPOSED" in blocked.reason


def test_a_terminal_question_frees_its_dataset_for_the_next(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        first = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE, OTHER),
        )
        assert first.created is not None
        ResearchDirector(session).advance(
            first.created.task_id, TaskState.UNDERPOWERED, actor="t", reason="t", now=NOW
        )
        second = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE, OTHER),
        )
        sequences = sorted(
            row[0]
            for row in session.execute(select(research_proposals.c.dataset_sequence))
            if row[0] is not None
        )

    assert second.outcome == CREATED
    assert second.created is not None and second.created.task_id != first.created.task_id
    assert sequences == [1, 2]


def test_catalogue_defects_raise_rather_than_refuse(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        with pytest.raises(CatalogueDefect, match="not a declared source"):
            _slate(session, catalog=(_template(required_datasets=("nowhere-daily",)),))
        with pytest.raises(CatalogueDefect, match="no subject term"):
            _slate(session, catalog=(_template(subject_terms=("ab",)),))
        with pytest.raises(CatalogueDefect, match="both when it was exposed and how"):
            _slate(session, catalog=(_template(exposed_through=date(2026, 8, 25)),))


def test_the_slate_is_ordered_deterministically(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        forward, _ = _slate(session, catalog=(PROBE, OTHER))
        backward, _ = _slate(session, catalog=(OTHER, PROBE))

    assert [entry.task_id for entry in forward] == [entry.task_id for entry in backward]


# --- model behaviour, through the real runtime with only the socket replaced ----------------------


def _runtime(content: str | None = None, *, status: int = 200, error: Exception | None = None):
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        if error is not None:
            raise error
        return httpx.Response(
            status,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    configuration = ModelConfiguration(
        endpoint="http://127.0.0.1:11434/v1",
        generator="qwen3:4b-instruct",
        critic="qwen2.5:3b",
        coder="qwen3:4b-instruct",
        context_tokens=8192,
        critic_minimum_context_tokens=8192,
        api_key=None,
    )
    runtime = build_model_runtime(
        configuration, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return runtime, calls


def _answer(template_id: str, reason: str = "most informative", **extra: object) -> str:
    return json.dumps({"template_id": template_id, "reason": reason, **extra})


def _model_propose(session, content: str | None = None, *, error=None, catalog=(PROBE, OTHER)):
    runtime, calls = _runtime(content, error=error)
    outcome = propose(
        session,
        selector=ModelCandidateSelector(runtime),
        now=NOW,
        code_commit=COMMIT,
        catalog=catalog,
    )
    return outcome, calls


def test_a_valid_structured_choice_is_accepted_and_its_provenance_kept(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome, calls = _model_propose(
            session, _answer("other-question", "it asks the less-searched thing")
        )

    assert outcome.outcome == CREATED
    assert outcome.created is not None
    assert outcome.created.task_id == "auto-other-question-v1-us-equities-daily"
    assert outcome.provenance["model_identity"].startswith("openai-compatible:qwen3:4b-instruct")
    assert len(outcome.provenance["model_response_sha256"]) == 64
    assert outcome.provenance["prompt_template"] == "research-candidate-selection@1"
    assert outcome.provenance["selection_reason"] == "it asks the less-searched thing"
    # The schema reached the wire as a grammar, and carried no length keywords Ollama rejects.
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert "maxLength" not in json.dumps(schema)


def test_malformed_model_output_opens_nothing_and_is_recorded(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome, _ = _model_propose(session, "I think the probe question looks promising!")
        tasks = _count(session, research_tasks)
        row = session.execute(select(research_proposals)).mappings().one()

    assert outcome.outcome == REFUSED
    assert tasks == 0
    assert row["outcome"] == REFUSED and row["task_id"] is None
    assert "did not return JSON" in json.loads(row["evidence_json"])["reason"]


def test_a_hallucinated_question_or_dataset_opens_nothing(database: Database) -> None:
    """The model names something nobody offered. It must not become a task."""
    with database.transaction() as session:
        seed_both_halves(session)
        invented, _ = _model_propose(session, _answer("crypto-momentum-edge"))
        with_dataset, _ = _model_propose(session, _answer("probe-question", dataset="crypto-daily"))
        tasks = _count(session, research_tasks)

    assert invented.outcome == REFUSED and "not among the 2 option(s)" in invented.reason
    # An extra field is not a hint to follow; the schema forbids it, so nothing is chosen at all.
    assert with_dataset.outcome == REFUSED and "not a valid CandidateChoice" in with_dataset.reason
    assert tasks == 0


def test_a_model_cannot_add_a_metric_or_a_verdict(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        metric, _ = _model_propose(session, _answer("probe-question", metric="sharpe"))
        verdict, _ = _model_propose(session, _answer("probe-question", evidence_sufficient=True))
        tasks = _count(session, research_tasks)

    assert metric.outcome == REFUSED
    assert verdict.outcome == REFUSED
    assert tasks == 0


def test_a_model_choosing_an_already_asked_question_opens_nothing(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        first = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
        assert first.created is not None
        ResearchDirector(session).advance(
            first.created.task_id, TaskState.UNDERPOWERED, actor="t", reason="t", now=NOW
        )
        duplicate, _ = _model_propose(session, _answer("probe-question"))
        tasks = _count(session, research_tasks)

    assert duplicate.outcome == REFUSED
    assert "not among the 1 option(s)" in duplicate.reason
    assert tasks == 1


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out")],
    ids=["refused", "timeout"],
)
def test_an_unavailable_model_fails_closed(database: Database, error: Exception) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome, _ = _model_propose(session, error=error)
        tasks = _count(session, research_tasks)
        attempts = _count(session, research_proposals)

    assert outcome.outcome == REFUSED
    assert "did not produce a usable choice" in outcome.reason
    assert tasks == 0
    assert attempts == 1


def test_a_selector_is_never_asked_to_choose_from_nothing(database: Database) -> None:
    runtime, calls = _runtime(_answer("anything"))
    with database.transaction() as session:
        seed_sessions(session, *CONSUMED)
        outcome = propose(
            session,
            selector=ModelCandidateSelector(runtime),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )

    assert outcome.outcome == NO_ELIGIBLE
    assert calls == [], "a model was called with nothing to choose from"


def test_the_length_bounds_are_enforced_after_parsing(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome, _ = _model_propose(session, _answer("probe-question", "x" * 2001))

    assert outcome.outcome == REFUSED


# --- provenance and persistence -----------------------------------------------------------------


def test_a_proposal_records_why_it_exists_on_the_task_and_in_the_ledger(
    database: Database,
) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        load_prior_findings(ResearchMemory(session))
        outcome = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE, OTHER),
        )
        assert outcome.created is not None
        event = (
            session.execute(
                select(research_task_events).where(
                    research_task_events.c.task_id == outcome.created.task_id
                )
            )
            .mappings()
            .one()
        )
        record = proposal_for_task(session, outcome.created.task_id)
        row = session.execute(select(research_proposals)).mappings().one()

    detail = json.loads(event["detail_json"])
    for field in (
        "proposal_id",
        "proposer_version",
        "code_commit",
        "proposed_at",
        "template_id",
        "template_version",
        "catalogue_hash",
        "dataset",
        "dataset_identity",
        "dataset_sequence",
        "dataset_trials_spent",
        "dataset_consumed_through",
        "exploratory_through",
        "clean_confirmatory_sessions",
        "capacity_class",
        "novelty_decision",
        "refuting_records_checked",
        "selector",
        "selection_reason",
        "alternatives_considered",
        "alternatives_rejected",
    ):
        assert detail.get(field), f"provenance is missing {field}"
    assert event["actor"] == "proposer/2"
    assert detail["code_commit"] == COMMIT
    assert detail["exploratory_through"] == "2026-08-21"
    assert detail["clean_confirmatory_sessions"] == "5"
    assert int(detail["refuting_records_checked"]) >= 20, "prior findings were not consulted"
    assert record is not None and record.proposal_id == outcome.proposal_id
    # Deterministic order puts `auto-other-question` first, so that is what was opened, and the
    # probe question is recorded as the alternative that was eligible and not chosen.
    assert record.template_id == "other-question"
    assert record.catalogue_hash == OTHER.catalogue_hash
    assert detail["alternatives_considered"] == "probe-question"
    evidence = json.loads(row["evidence_json"])
    assert evidence["provenance"] == detail
    assert [item["template_id"] for item in evidence["eligible"]] == [
        "other-question",
        "probe-question",
    ]


def test_a_commit_that_cannot_be_stated_opens_nothing(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit="unknown",
            catalog=(PROBE,),
        )
        tasks = _count(session, research_tasks)

    assert outcome.outcome == REFUSED
    assert "not a full SHA" in outcome.reason
    assert tasks == 0


def test_task_identity_does_not_move_when_more_data_arrives(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        before = _slate(session)[0][0]
        seed_sessions(session, "2026-08-31", "2026-09-01")
        after = _slate(session)[0][0]

    assert before.task_id == after.task_id == deterministic_task_id(PROBE, before.readiness)


def test_a_rerun_against_unchanged_state_writes_nothing_new(database: Database) -> None:
    with database.transaction() as session:
        seed_sessions(session, *CONSUMED)
        first = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
        again = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW.replace(day=13),
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
        attempts = _count(session, research_proposals)

    assert first.outcome == again.outcome == NO_ELIGIBLE
    assert first.proposal_id == again.proposal_id, "the calendar date leaked into identity"
    assert (first.recorded, again.recorded) == (True, False)
    assert attempts == 1


def test_a_restart_does_not_duplicate_an_open_proposal(tmp_path: Path) -> None:
    """A new process opening the same file is the restart. It must find the task, not remake it."""
    path = tmp_path / "restart.db"
    first_process = Database.create(path)
    with first_process.transaction() as session:
        seed_both_halves(session)
        opened = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE, OTHER),
        )
    first_process.close()

    second_process = Database(path)
    try:
        with second_process.transaction() as session:
            again = propose(
                session,
                selector=FirstEligibleSelector(),
                now=NOW,
                code_commit=COMMIT,
                catalog=(PROBE, OTHER),
            )
            tasks = _count(session, research_tasks)
    finally:
        second_process.close()

    assert opened.outcome == CREATED
    assert again.outcome == NO_ELIGIBLE
    assert tasks == 1


def test_a_failure_after_the_task_write_leaves_no_task_and_no_record(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task, its event and its proposal row are one unit.

    The failure is injected into the proposal-row write, which runs *after* the task and its
    event are written, so a partial state is exactly what a broken transaction would leave.
    """
    original = proposer_module._insert_attempt

    def failing(session, **kwargs):
        if kwargs.get("outcome") == CREATED:
            raise RuntimeError("disk full")
        return original(session, **kwargs)

    monkeypatch.setattr(proposer_module, "_insert_attempt", failing)
    with database.transaction() as session:
        seed_both_halves(session)

    with pytest.raises(RuntimeError, match="disk full"):
        with database.transaction() as session:
            propose(
                session,
                selector=FirstEligibleSelector(),
                now=NOW,
                code_commit=COMMIT,
                catalog=(PROBE,),
            )

    with database.transaction() as session:
        assert _count(session, research_tasks) == 0
        assert _count(session, research_task_events) == 0
        assert _count(session, research_proposals) == 0


def test_a_proposer_with_a_stale_view_collides_instead_of_opening_a_second_question(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two proposers both saw the dataset as free. Only the first may open a question on it.

    Deterministic rather than timed: B's survey is taken before A commits and then replayed, which
    is the interleaving a real race produces. B chose a *different* question, so the task id does
    not collide; the only thing that can stop it is the (dataset, sequence) rule, and only if B's
    sequence came from the same snapshot as its decision. Computing the sequence by a fresh read
    at insert time lets B through, and this test fails.
    """
    with database.transaction() as session:
        seed_both_halves(session)
        stale = survey(session, now=NOW, catalog=(PROBE, OTHER))
        other = next(entry for entry in stale.eligible if entry.template is OTHER)

    with database.transaction() as session:
        winner = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
    assert winner.outcome == CREATED

    monkeypatch.setattr(proposer_module, "survey", lambda *_args, **_kwargs: stale)
    loser_selection = proposer_module.Selection(
        candidate=other, reason="stale", selector="first-eligible"
    )
    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            commit_proposal(
                session, loser_selection, now=NOW, code_commit=COMMIT, catalog=(PROBE, OTHER)
            )
    monkeypatch.undo()

    with database.transaction() as session:
        tasks = [row[0] for row in session.execute(select(research_tasks.c.task_id))]
    assert tasks == [winner.created.task_id]


def test_proposing_spends_no_confirmatory_capacity(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        before = [row.explain() for row in capacity_inventory(session, as_of=TODAY)]
        propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE, OTHER),
        )
        after = [row.explain() for row in capacity_inventory(session, as_of=TODAY)]

    assert after == before


def test_select_candidate_refuses_a_selector_that_returns_something_off_the_slate(
    database: Database,
) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        state = survey(session, now=NOW, catalog=(PROBE,))
        foreign = replace(state.eligible[0], task_id="auto-something-else")

    class Rogue:
        identity = "rogue"

        def select(self, candidates, *, context):  # noqa: ANN001
            return proposer_module.Selection(candidate=foreign, reason="x", selector="rogue")

    with pytest.raises(ProposalRefused, match="not on the slate"):
        select_candidate(state, Rogue())


def test_every_catalogue_template_is_well_formed() -> None:
    from quantbot.research.autoexperiment import MEASUREMENTS

    ids = [template.template_id for template in CATALOG]
    assert len(ids) == len(set(ids))
    for template in CATALOG:
        proposer_module._check_catalogue(template)
        assert template.measurement_id in MEASUREMENTS
        assert template.minimum_practical_effect <= template.expected_effect
        matched = {record_id for record_id, _ in template.adjudicated_refutations}
        assert all(reason.strip() for _, reason in template.adjudicated_refutations)
        assert matched <= {f"refuted-{number:03d}" for number in range(1, 26)}


def test_the_task_the_proposer_opens_is_an_ordinary_research_task(database: Database) -> None:
    with database.transaction() as session:
        seed_both_halves(session)
        outcome = propose(
            session,
            selector=FirstEligibleSelector(),
            now=NOW,
            code_commit=COMMIT,
            catalog=(PROBE,),
        )
        stored = ResearchDirector(session).get("auto-probe-question-v1-us-equities-daily")

    assert isinstance(outcome.created, ResearchTask)
    assert stored == outcome.created
    assert stored.state is TaskState.PROPOSED
    assert stored.required_datasets == ("sip-us-equities-daily",)
