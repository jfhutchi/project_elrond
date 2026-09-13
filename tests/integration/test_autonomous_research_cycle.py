"""The whole loop, with nothing seeded by hand (#3, #13, #21).

The milestone this file exists to prove is one sentence: with no human-created research task,
Elrond inspects its own measured state, chooses a question, opens the task, executes the
measurement under kernel-enforced isolation, grades it, persists what it found, and reaches a
terminal scientific verdict.

Every test here starts from an empty `research_tasks` and asserts that fact first. A test that
began by inserting a task would prove the consumer works, which was never in doubt.

The fixtures seed sessions on *both* sides of the 2026-08-21 consumption boundary. The first
version of this file seeded only sessions after it, so every measurement ran entirely on the
clean confirmatory sessions and no test could notice that the measurement was reading them.

Two kinds of sandbox appear. `_Subprocess` runs the real script in a real child process and
claims `os_enforced` so the stage accepts it; it tests the arithmetic, the boundary and the
evidence without depending on the host. The tests marked for isolation use the host's actual
OS-enforced backend and skip loudly without one, for the reason `test_sandbox_windows.py` gives:
a skipped isolation test and a passing one must never look alike.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import delete, func, select

from quantbot.cli import CLIContext, main
from quantbot.config import Settings
from quantbot.domain import Bar
from quantbot.operations.kill_switch import ReadinessEvidence
from quantbot.research import autoexperiment as autoexperiment_module
from quantbot.research import composition
from quantbot.research.autoexperiment import (
    POLICY,
    ExperimentRefused,
    MeasurementResult,
    Panel,
    autoexperiment_stage,
    build_script,
    confirmatory_observations,
    grade,
    load_panel,
    run_measurement,
)
from quantbot.research.capacity import DATA_SOURCES, capacity_inventory
from quantbot.research.composition import ModelConfiguration
from quantbot.research.cycle import CycleRefused, drain
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.prior import load_prior_findings
from quantbot.research.proposer import CATALOG, FirstEligibleSelector, propose, template_by_id
from quantbot.research.stages import capacity_stage, novelty_stage
from quantbot.sandbox import SandboxError, SandboxRunner, select_isolated_backend
from quantbot.sandbox.static import enforce
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import (
    bars,
    research_proposals,
    research_records,
    research_task_events,
    research_tasks,
    search_runs,
)

NOW = datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
COMMIT = "fedcba9876543210fedcba9876543210fedcba98"
IEX = "alpaca:iex:all:1day"
UNIVERSE = ("SPY", "QQQ", "IWM", "XLK", "XLE")

#: Searched by cycles 2-10: on or before `PRIOR_WINDOW_CONSUMPTION`'s 2026-08-21 end.
CONSUMED = (
    "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
    "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
)  # fmt: skip
#: After it, so unsearched. These are the sessions a measurement must never read.
CLEAN = (
    "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
    "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
)  # fmt: skip

#: What the real catalogue proposes on this fixture: the three older questions are exposed
#: through 2026-09-11, so the only one with clean sessions is the volatility-persistence question.
TASK_ID = "auto-benchmark-move-size-persists-v1-us-equities-daily"


def _isolated() -> bool:
    try:
        select_isolated_backend()
    except SandboxError:
        return False
    return True


requires_isolation = pytest.mark.skipif(
    not _isolated(), reason="no OS-enforced sandbox on this host; this must not pass without one"
)


class _Subprocess:
    """The real script in a real child process, claiming enforcement so the stage will run it.

    Never used outside tests. It exists so the boundary, the arithmetic and the evidence can be
    tested on any host; the isolation claims are tested only against the host's real backend.
    """

    os_enforced = True

    def __init__(self) -> None:
        self.sources: list[str] = []

    def run(self, source: str):
        self.sources.append(source)
        return SandboxRunner(POLICY).run(source)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "autonomous.db")
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


def seed(session, *, days=(*CONSUMED, *CLEAN), symbols=UNIVERSE, spike_clean: bool = False) -> None:
    """A panel with genuine variation, so no statistic is degenerate.

    `spike_clean` plants violent, alternating benchmark moves in the clean sessions only. A
    measurement that reads them reports a different statistic, a different session range and a
    different observation count, so the boundary is tested by its consequences as well as by
    inspection of the payload.
    """
    repository = StorageRepository(session)
    for index, day in enumerate(days):
        close = datetime.combine(
            date.fromisoformat(day), time(16, 0), tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)
        row = []
        for position, symbol in enumerate(symbols):
            price = Decimal(100 + (index * (position + 1)) % 7 + position)
            if spike_clean and day in CLEAN and symbol == "SPY":
                price = Decimal(400 if index % 2 else 40)
            row.append(_bar(symbol, close, price))
        repository.save_bars(row, provider=IEX)


def _stages(session, *, sandbox=None):
    """Exactly the handler set `cli.py` assembles on an isolated host."""
    return {
        TaskState.PROPOSED: capacity_stage(session, novelty_stage(ResearchMemory(session))),
        TaskState.SCOUTING: autoexperiment_stage(
            session, sandbox=sandbox, now=NOW, code_commit=COMMIT
        ),
    }


def _propose(session):
    return propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit=COMMIT)


def _count(session, table) -> int:
    return int(session.execute(select(func.count()).select_from(table)).scalar_one())


def _source():
    return next(item for item in DATA_SOURCES if item.dataset == "sip-us-equities-daily")


# --- the producer feeds the consumer --------------------------------------------------------------


def test_the_queue_starts_empty_and_the_proposer_fills_it(database: Database) -> None:
    with database.transaction() as session:
        seed(session)
        load_prior_findings(ResearchMemory(session))
        assert ResearchDirector(session).by_state() == [], "only meaningful from an empty queue"

        outcome = _propose(session)
        tasks = ResearchDirector(session).by_state()

    assert outcome.ok
    assert [task.task_id for task in tasks] == [TASK_ID]
    assert tasks[0].state is TaskState.PROPOSED


def test_the_existing_cycle_consumes_a_proposed_task_through_the_real_gates(
    database: Database,
) -> None:
    """`drain` is untouched. The only new thing is that it has something to drain."""
    with database.transaction() as session:
        seed(session)
        load_prior_findings(ResearchMemory(session))
        _propose(session)
        director = ResearchDirector(session)
        outcomes = drain(
            director,
            {TaskState.PROPOSED: capacity_stage(session, novelty_stage(ResearchMemory(session)))},
            actor="test",
            now=NOW,
            max_steps=1,
        )
        moved = [item for item in outcomes if not item.idle]

    assert len(moved) == 1, "the cycle advanced nothing, so the producer did not feed it"
    assert moved[0].moved_to is TaskState.SCOUTING
    assert "no stored record settles this question" in moved[0].reason


def test_nothing_is_proposed_when_no_question_has_a_clean_session(database: Database) -> None:
    with database.transaction() as session:
        seed(session, days=CONSUMED)
        outcome = _propose(session)
        tasks = _count(session, research_tasks)

    assert outcome.created is None
    assert outcome.outcome == "NO_ELIGIBLE"
    assert tasks == 0


def test_a_settled_question_is_blocked_by_the_novelty_gate(database: Database) -> None:
    """The gate runs on proposer output, using a record that really does settle it."""
    with database.transaction() as session:
        seed(session)
        outcome = _propose(session)
        assert outcome.created is not None
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="settles-it",
                kind=RecordKind.FINDING,
                verdict=Verdict.REFUTED,
                subject="a prior study of exactly this",
                statement=outcome.created.question,
                source="test",
                recorded_at=NOW,
            )
        )
        moved = [
            item
            for item in drain(
                ResearchDirector(session), _stages(session), actor="test", now=NOW, max_steps=1
            )
            if not item.idle
        ]

    assert moved[0].moved_to is TaskState.BLOCKED
    assert "already settled by settles-it" in moved[0].reason


# --- the measurement boundary --------------------------------------------------------------------


def test_the_measurement_never_reads_a_clean_session(database: Database) -> None:
    """The defect this version exists to fix, asserted three ways.

    The payload handed to the child contains no clean date; the child reports a range ending at
    the boundary; and the statistic is identical whether or not the clean sessions carry a
    violent planted signal. Loading the panel through `as_of` again -- reading every stored
    session -- fails all three.
    """
    sandbox = _Subprocess()
    with database.transaction() as session:
        seed(session, spike_clean=True)
        _propose(session)
        director = ResearchDirector(session)
        drain(director, _stages(session, sandbox=sandbox), actor="test", now=NOW, max_steps=2)
        spiked = director.history(TASK_ID)[-1].detail

    payload = json.loads(json.loads(sandbox.sources[0].split("\n", 1)[0].split(" = ", 1)[1]))
    assert set(payload["dates"]).isdisjoint(CLEAN)
    assert max(payload["dates"]) == "2026-08-21"
    assert spiked["last_session"] == "2026-08-21"
    assert spiked["observations"] == str(len(CONSUMED) - 2)
    assert spiked["exploratory_through"] == "2026-08-21"
    assert spiked["first_clean_session"] == "2026-08-24"


def test_the_clean_sessions_cannot_change_the_exploratory_statistic(tmp_path: Path) -> None:
    results = []
    for spike in (False, True):
        db = Database.create(tmp_path / f"spike-{spike}.db")
        try:
            with db.transaction() as session:
                seed(session, spike_clean=spike)
                _propose(session)
                director = ResearchDirector(session)
                drain(
                    director,
                    _stages(session, sandbox=_Subprocess()),
                    actor="test",
                    now=NOW,
                    max_steps=2,
                )
                results.append(director.history(TASK_ID)[-1].detail)
        finally:
            db.close()

    plain, spiked = results
    assert plain["statistic"] == spiked["statistic"]
    assert plain["panel_sha256"] == spiked["panel_sha256"]


def test_confirmation_never_borrows_a_session_from_the_exploratory_side() -> None:
    assert confirmatory_observations(len(CLEAN)) == len(CLEAN) - 2
    assert confirmatory_observations(1) == 0
    assert confirmatory_observations(0) == 0


# --- the verdict and what it leaves behind ------------------------------------------------------


def test_a_measured_question_reaches_a_verdict_and_leaves_its_evidence(database: Database) -> None:
    with database.transaction() as session:
        seed(session)
        load_prior_findings(ResearchMemory(session))
        before = [row.explain() for row in capacity_inventory(session, as_of=NOW.date())]
        assert _count(session, research_tasks) == 0

        _propose(session)
        director = ResearchDirector(session)
        moved = [
            item
            for item in drain(
                director,
                _stages(session, sandbox=_Subprocess()),
                actor="test",
                now=NOW,
                max_steps=2,
            )
            if not item.idle
        ]
        task = director.get(TASK_ID)
        detail = director.history(TASK_ID)[-1].detail
        search = session.execute(select(search_runs)).mappings().one()
        memory = ResearchMemory(session)
        exploratory = memory.get(detail["exploratory_record_id"])
        underpowered = memory.get(detail["underpowered_record_id"])
        relations = memory.related(detail["exploratory_record_id"])
        recalled = memory.recall("magnitude of the benchmark", kinds=[RecordKind.FINDING])
        after = [row.explain() for row in capacity_inventory(session, as_of=NOW.date())]

    assert [item.moved_to for item in moved] == [TaskState.SCOUTING, TaskState.UNDERPOWERED]
    assert task is not None and task.terminal
    reason = moved[-1].reason
    assert "did not decide this verdict" in reason
    assert "is not a refutation" in reason

    assert detail["verdict_basis"] == "power gate on clean confirmatory observations"
    assert int(detail["observations_required"]) > int(detail["confirmatory_observations_available"])
    assert detail["confirmatory_observations_available"] == str(len(CLEAN) - 2)
    # 68 prior trials plus the one a confirmation would add.
    assert detail["cumulative_trials"] == "69"
    assert detail["code_commit"] == COMMIT

    assert search["search_id"] == detail["search_id"]
    assert search["actor"] == "autoexperiment"
    assert search["candidates_evaluated"] == 1
    assert (search["start_date"], search["end_date"]) == ("2026-08-10", "2026-08-21")
    assert search["data_role"] == "DISCOVERY"
    assert search["configuration_hash"] == detail["script_sha256"]
    assert json.loads(search["detail_json"])["stdout"].strip()

    assert exploratory is not None and exploratory.verdict is Verdict.EXPLORATORY_ONLY
    assert underpowered is not None and underpowered.verdict is Verdict.UNDERPOWERED
    assert {record.record_id for record in recalled} == {
        exploratory.record_id,
        underpowered.record_id,
    }
    assert relations[0][1] == "us-equities-daily"
    # Measuring spent no confirmatory capacity: the inventory reads the same before and after.
    assert after == before


def test_a_rerun_after_the_verdict_changes_nothing(database: Database) -> None:
    with database.transaction() as session:
        seed(session)
        _propose(session)
        director = ResearchDirector(session)
        drain(director, _stages(session, sandbox=_Subprocess()), actor="t", now=NOW, max_steps=2)
        counts = [
            _count(session, table) for table in (research_tasks, search_runs, research_records)
        ]

        again = _propose(session)
        idle = drain(
            director, _stages(session, sandbox=_Subprocess()), actor="t", now=NOW, max_steps=2
        )
        recounts = [
            _count(session, table) for table in (research_tasks, search_runs, research_records)
        ]

    assert again.created is None
    assert all(item.idle for item in idle)
    assert recounts == counts


def test_evidence_that_cannot_be_written_takes_the_verdict_down_with_it(
    database: Database,
) -> None:
    """A search id already on record is refused (append-only), and the move goes with it."""
    with database.transaction() as session:
        seed(session)
        _propose(session)
        drain(ResearchDirector(session), _stages(session), actor="t", now=NOW, max_steps=1)
        session.execute(
            search_runs.insert().values(
                search_id=f"auto-exploratory:{TASK_ID}:e2",
                actor="someone-else",
                actor_version="1",
                candidates_evaluated=1,
                dataset="sip-us-equities-daily",
                start_date="2026-08-10",
                end_date="2026-08-21",
                data_role="DISCOVERY",
                started_at="2026-09-12T00:00:00Z",
                finished_at="2026-09-12T00:00:01Z",
                configuration_hash="x",
            )
        )

    with pytest.raises(CycleRefused, match="evidence could not be recorded"):
        with database.transaction() as session:
            drain(
                ResearchDirector(session),
                _stages(session, sandbox=_Subprocess()),
                actor="t",
                now=NOW,
                max_steps=1,
            )

    with database.transaction() as session:
        task = ResearchDirector(session).get(TASK_ID)
        records = _count(session, research_records)
    assert task is not None and task.state is TaskState.SCOUTING
    assert records == 0


def test_a_task_the_proposer_did_not_open_is_left_where_it_is(database: Database) -> None:
    moment = NOW
    with database.transaction() as session:
        seed(session)
        director = ResearchDirector(session)
        director.open_task(
            ResearchTask(
                task_id="T-HAND",
                state=TaskState.PROPOSED,
                question="an operator's question",
                family_id="operator",
                created_at=moment,
                updated_at=moment,
            ),
            actor="operator",
            reason="opened by hand",
        )
        director.advance("T-HAND", TaskState.SCOUTING, actor="t", reason="t", now=moment)
        outcomes = drain(
            director, _stages(session, sandbox=_Subprocess()), actor="t", now=NOW, max_steps=3
        )
        hand = director.get("T-HAND")

    assert all(item.idle for item in outcomes)
    assert hand is not None and hand.state is TaskState.SCOUTING


def test_a_catalogue_entry_edited_after_the_proposal_blocks_the_measurement(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    with database.transaction() as session:
        seed(session)
        _propose(session)
        drain(ResearchDirector(session), _stages(session), actor="t", now=NOW, max_steps=1)

    original = template_by_id("benchmark-move-size-persists")
    edited = replace(
        original, expected_effect=Decimal("0.05"), minimum_practical_effect=Decimal("0.05")
    )
    monkeypatch.setattr(
        autoexperiment_module,
        "template_by_id",
        lambda name: edited if name == original.template_id else None,
    )
    sandbox = _Subprocess()
    with database.transaction() as session:
        moved = drain(
            ResearchDirector(session),
            _stages(session, sandbox=sandbox),
            actor="t",
            now=NOW,
            max_steps=1,
        )

    assert moved[0].moved_to is TaskState.BLOCKED
    assert "has been changed or removed since" in moved[0].reason
    assert sandbox.sources == [], "a question the task was not opened for was measured"


def test_observations_that_vanish_before_the_measurement_block_it(database: Database) -> None:
    sandbox = _Subprocess()
    with database.transaction() as session:
        seed(session)
        _propose(session)
        director = ResearchDirector(session)
        drain(director, _stages(session), actor="t", now=NOW, max_steps=1)
        session.execute(delete(bars))
        moved = drain(director, _stages(session, sandbox=sandbox), actor="t", now=NOW, max_steps=1)
        runs = _count(session, search_runs)

    assert moved[0].moved_to is TaskState.BLOCKED
    assert sandbox.sources == []
    assert runs == 0


def test_an_unenforced_backend_is_refused(database: Database) -> None:
    with database.transaction() as session:
        seed(session)
        panel = load_panel(session, _source(), through=date(2026, 8, 21))

    with pytest.raises(ExperimentRefused, match="requires an OS-enforced sandbox"):
        run_measurement(CATALOG[0], panel, sandbox=SandboxRunner())


def test_a_panel_without_the_benchmark_is_refused(database: Database) -> None:
    with database.transaction() as session:
        seed(session, symbols=("QQQ", "IWM", "XLK"))
        with pytest.raises(ExperimentRefused, match="SPY is not present"):
            load_panel(session, _source(), through=date(2026, 8, 21))


def test_an_unknown_measurement_is_a_catalogue_defect() -> None:
    panel = Panel(identity="x", symbols=("SPY",), dates=(date(2026, 8, 24),), closes=((1.0,),))
    with pytest.raises(ExperimentRefused, match="not a known measurement"):
        build_script("measure_something_nobody_wrote", panel)


def test_every_catalogue_measurement_passes_the_static_scan(database: Database) -> None:
    """The scripts are machine-assembled, so nothing reviews the particular one that runs."""
    with database.transaction() as session:
        seed(session)
        panel = load_panel(session, _source(), through=date(2026, 8, 21))

    for template in CATALOG:
        enforce(build_script(template.measurement_id, panel))


# --- grading is deterministic and out of the child's reach----------------------------------------


def _result(statistic: str = "0.05", degenerate: bool = False) -> MeasurementResult:
    return MeasurementResult(
        measurement_id=CATALOG[0].measurement_id,
        observations=290,
        statistic=Decimal(statistic),
        t_statistic=Decimal("0.9"),
        degenerate=degenerate,
        sessions=292,
        universe=23,
        first_session=date(2025, 6, 24),
        last_session=date(2026, 8, 21),
        panel_digest="p" * 64,
        script_sha256="s" * 64,
        backend="WindowsSandbox",
        duration_seconds=0.1,
        peak_memory_mb=8.0,
        stdout="",
        stderr="",
    )


def test_too_little_confirmatory_capacity_is_underpowered_not_refuted() -> None:
    verdict = grade(CATALOG[0], _result(), cumulative_trials=69, confirmatory_observations=12)

    assert verdict.next_state is TaskState.UNDERPOWERED
    assert verdict.luck_bar > Decimal("2.9"), "69 trials must not be judged at 1.96"
    assert verdict.observations_required_for_confirmation > 12
    assert "not a refutation" in verdict.reason


def test_the_exploratory_statistic_cannot_move_the_verdict() -> None:
    """Sizing on what was measured would move the bar to wherever the result landed."""
    weak = grade(CATALOG[0], _result("0.01"), cumulative_trials=69, confirmatory_observations=12)
    strong = grade(CATALOG[0], _result("0.95"), cumulative_trials=69, confirmatory_observations=12)
    unmeasured = grade(CATALOG[0], None, cumulative_trials=69, confirmatory_observations=12)

    assert weak.next_state is strong.next_state is unmeasured.next_state
    assert (
        weak.observations_required_for_confirmation
        == strong.observations_required_for_confirmation
        == unmeasured.observations_required_for_confirmation
    )


def test_sufficient_capacity_advances_to_critique_instead() -> None:
    verdict = grade(CATALOG[0], _result(), cumulative_trials=69, confirmatory_observations=100_000)

    assert verdict.next_state is TaskState.CRITIQUE
    assert "before any confirmatory data is read" in verdict.reason


def test_a_degenerate_statistic_blocks_rather_than_concluding() -> None:
    verdict = grade(
        CATALOG[0], _result(degenerate=True), cumulative_trials=69, confirmatory_observations=12
    )

    assert verdict.next_state is TaskState.BLOCKED
    assert "degenerate" in verdict.reason


def test_the_bar_rises_with_accumulated_trials() -> None:
    fresh = grade(CATALOG[0], _result(), cumulative_trials=1, confirmatory_observations=12)
    spent = grade(CATALOG[0], _result(), cumulative_trials=69, confirmatory_observations=12)

    assert spent.luck_bar > fresh.luck_bar
    assert (
        spent.observations_required_for_confirmation > fresh.observations_required_for_confirmation
    )


# --- the production entry point ------------------------------------------------------------------


def _ready_context(database: Database) -> CLIContext:
    return CLIContext(
        settings=Settings(
            EXPECTED_ACCOUNT_ID="paper-1",
            KILL_SWITCH=False,
            BROKER_HEALTHY=True,
            MARKET_DATA_HEALTHY=True,
            RISK_ENGINE_HEALTHY=True,
            RECONCILIATION_SUCCESSFUL=True,
        ),
        database=database,
        handlers={},
        reported_account_id="paper-1",
        clearance_evidence=ReadinessEvidence(
            paper_mode=True,
            account_verified=True,
            broker_healthy=True,
            data_healthy=True,
            risk_healthy=True,
            reconciliation_successful=True,
        ),
    )


def _model(monkeypatch: pytest.MonkeyPatch, *, content: str | None, error: Exception | None = None):
    """Replace only the socket under the production model runtime."""
    real_build = composition.build_model_runtime

    def handler(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}], "usage": {}},
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
    monkeypatch.setattr(
        composition,
        "build_model_runtime",
        lambda *_a, **_k: real_build(
            configuration, client=httpx.Client(transport=httpx.MockTransport(handler))
        ),
    )


def test_the_scheduled_command_proposes_when_idle_and_the_cycle_consumes_it(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`research-cycle --propose-when-idle`, the argv the research service runs, from empty."""
    with database.transaction() as session:
        seed(session)
        assert _count(session, research_tasks) == 0
    _model(
        monkeypatch,
        content=json.dumps(
            {"template_id": "benchmark-move-size-persists", "reason": "the only clean question"}
        ),
    )

    output = StringIO()
    code = main(
        ["research-cycle", "--max-steps", "2", "--commit", "--propose-when-idle"],
        context=_ready_context(database),
        output=output,
    )
    report = json.loads(output.getvalue())

    assert code == 0, report
    assert report["proposal"]["outcome"] == "CREATED"
    assert report["proposal"]["created"] == TASK_ID
    provenance = report["proposal"]["provenance"]
    assert provenance["model_identity"].startswith("openai-compatible:qwen3:4b-instruct")
    assert provenance["prompt_template"] == "research-candidate-selection@1"
    assert provenance["code_commit"] not in {"unknown", ""}
    moved = [item["moved_to"] for item in report["advanced"]]
    if _isolated():
        assert moved == ["SCOUTING", "UNDERPOWERED"]
    else:
        assert moved == ["SCOUTING"]


def test_the_scheduled_command_does_not_propose_while_work_is_actionable(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        composition, "build_model_runtime", lambda *_a, **_k: calls.append(1) or None
    )
    with database.transaction() as session:
        seed(session)
        _propose(session)

    output = StringIO()
    main(
        ["research-cycle", "--max-steps", "1", "--commit", "--propose-when-idle"],
        context=_ready_context(database),
        output=output,
    )
    report = json.loads(output.getvalue())

    assert "proposal" not in report
    assert calls == []


def test_an_unavailable_model_fails_the_run_closed_and_records_why(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    with database.transaction() as session:
        seed(session)
    _model(monkeypatch, content=None, error=httpx.ConnectError("connection refused"))

    output = StringIO()
    code = main(
        ["research-cycle", "--max-steps", "1", "--commit", "--propose-when-idle"],
        context=_ready_context(database),
        output=output,
    )
    report = json.loads(output.getvalue())

    with database.transaction() as session:
        tasks = _count(session, research_tasks)
        attempt = session.execute(select(research_proposals)).mappings().one()

    assert code == 2
    assert report["ok"] is False
    assert report["proposal"]["outcome"] == "REFUSED"
    assert tasks == 0
    assert attempt["outcome"] == "REFUSED"


@requires_isolation
def test_the_milestone_under_the_hosts_real_isolation(database: Database) -> None:
    """End to end, nothing inserted by hand, measured under the host's OS-enforced backend."""
    with database.transaction() as session:
        seed(session, spike_clean=True)
        load_prior_findings(ResearchMemory(session))
        assert ResearchDirector(session).by_state() == []

        _propose(session)
        director = ResearchDirector(session)
        moved = [
            item
            for item in drain(director, _stages(session), actor="test", now=NOW, max_steps=2)
            if not item.idle
        ]
        detail = director.history(TASK_ID)[-1].detail
        events = _count(session, research_task_events)

    assert [item.moved_to for item in moved] == [TaskState.SCOUTING, TaskState.UNDERPOWERED]
    assert detail["sandbox_backend"] in {"WindowsSandbox", "WslSandbox"}
    assert detail["sandbox_network_enabled"] == "false"
    assert detail["last_session"] == "2026-08-21"
    assert detail["observations"] == str(len(CONSUMED) - 2)
    assert detail["exploratory_record_id"] and detail["search_id"]
    assert events == 3
