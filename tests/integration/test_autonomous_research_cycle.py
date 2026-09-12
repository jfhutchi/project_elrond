"""The whole loop, with nothing seeded by hand (#3, #13, #21).

The milestone this file exists to prove is one sentence: with no human-created research task,
Elrond inspects its own measured state, chooses a question, opens the task, executes the
measurement under kernel-enforced isolation, grades it, and reaches a terminal scientific verdict.

Every test here starts from an empty `research_tasks` and asserts that fact first. A test that
began by inserting a task would prove the consumer works, which was never in doubt -- the consumer
has worked the whole time, on an empty queue.

The sandbox tests skip loudly when no OS-enforced backend exists, for the reason
`test_sandbox_windows.py` gives: a skipped isolation test and a passing one must never look alike.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quantbot.domain import Bar
from quantbot.research.autoexperiment import (
    ExperimentRefused,
    MeasurementResult,
    Panel,
    autoexperiment_stage,
    build_script,
    grade,
    load_panel,
    run_measurement,
)
from quantbot.research.capacity import DATA_SOURCES
from quantbot.research.cycle import drain
from quantbot.research.director import ResearchDirector, TaskState
from quantbot.research.memory import ResearchMemory
from quantbot.research.prior import load_prior_findings
from quantbot.research.proposer import CATALOG, FirstEligibleSelector, propose
from quantbot.research.stages import capacity_stage, novelty_stage
from quantbot.sandbox import SandboxError, SandboxRunner, select_isolated_backend
from quantbot.sandbox.static import enforce
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import research_task_events

NOW = datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
SIP = "alpaca:sip:split:1Day"
UNIVERSE = ("SPY", "QQQ", "IWM", "XLK", "XLE")

#: Sessions after the 2026-08-21 prior-consumption boundary, so they are genuinely unsearched.
#: Weekends are skipped because a date with no bar is not a session.
SESSIONS = (
    "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
    "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
)


def _isolated() -> bool:
    try:
        select_isolated_backend()
    except SandboxError:
        return False
    return True


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


def seed(session, *, days=SESSIONS, symbols=UNIVERSE) -> None:
    """A panel with genuine cross-sectional variation, so no statistic is degenerate."""
    repository = StorageRepository(session)
    for index, day in enumerate(days):
        close = datetime.combine(
            date.fromisoformat(day), time(16, 0), tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)
        repository.save_bars(
            [
                # A different walk per symbol, so dispersion and breadth both move.
                _bar(symbol, close, Decimal(100 + (index * (position + 1)) % 7 + position))
                for position, symbol in enumerate(symbols)
            ],
            provider=SIP,
        )


def _stages(session, *, sandbox=None):
    """Exactly the handler set `cli.py` assembles, so this exercises the shipped wiring."""
    return {
        TaskState.PROPOSED: capacity_stage(session, novelty_stage(ResearchMemory(session))),
        TaskState.SCOUTING: autoexperiment_stage(session, sandbox=sandbox, now=NOW),
    }


# --- the producer feeds the consumer --------------------------------------------------------------


def test_the_queue_starts_empty_and_the_proposer_fills_it(database: Database) -> None:
    with database.transaction() as session:
        seed(session)
        assert ResearchDirector(session).by_state() == [], (
            "this test is only meaningful from an empty queue"
        )

        outcome = propose(
            session, selector=FirstEligibleSelector(), now=NOW, code_commit="test"
        )
        tasks = ResearchDirector(session).by_state()

    assert outcome.ok
    assert len(tasks) == 1
    assert tasks[0].state is TaskState.PROPOSED
    assert tasks[0].task_id.startswith("auto-")


def test_the_existing_cycle_consumes_a_proposed_task_through_the_real_gates(
    database: Database,
) -> None:
    """`drain` is untouched. The only new thing is that it has something to drain."""
    with database.transaction() as session:
        seed(session)
        load_prior_findings(ResearchMemory(session))
        propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="test")

        director = ResearchDirector(session)
        outcomes = drain(
            director,
            {TaskState.PROPOSED: capacity_stage(session, novelty_stage(ResearchMemory(session)))},
            actor="test",
            now=NOW,
            max_steps=1,
        )
        moved = [item for item in outcomes if not item.idle]
        tasks = director.by_state()

    assert len(moved) == 1, "the cycle advanced nothing, so the producer did not feed it"
    assert moved[0].moved_to is TaskState.SCOUTING
    assert tasks[0].state is TaskState.SCOUTING
    assert "no stored record settles this question" in moved[0].reason


def test_the_capacity_gate_still_blocks_a_question_it_should(database: Database) -> None:
    """The gates are not bypassed by the producer. Seed inside the spent range and watch.

    Without this, a proposer that quietly declared no datasets would sail through a gate that
    only refuses what it is told about.
    """
    with database.transaction() as session:
        seed(session, days=("2026-08-18", "2026-08-19", "2026-08-20"))
        # Nothing is eligible, so nothing is proposed -- which is itself the gate working one
        # layer earlier than the cycle.
        outcome = propose(
            session, selector=FirstEligibleSelector(), now=NOW, code_commit="test"
        )

    assert outcome.created is None
    assert "no eligible candidate" in outcome.reason


def test_a_settled_question_is_blocked_by_the_novelty_gate(database: Database) -> None:
    """Proves the gate runs on proposer output, using a record that really does settle it."""
    from quantbot.research.memory import RecordKind, ResearchRecord, Verdict

    with database.transaction() as session:
        seed(session)
        outcome = propose(
            session, selector=FirstEligibleSelector(), now=NOW, code_commit="test"
        )
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
        director = ResearchDirector(session)
        moved = [
            item
            for item in drain(
                director,
                {
                    TaskState.PROPOSED: capacity_stage(
                        session, novelty_stage(ResearchMemory(session))
                    )
                },
                actor="test",
                now=NOW,
                max_steps=1,
            )
            if not item.idle
        ]

    assert moved[0].moved_to is TaskState.BLOCKED
    assert "already settled by settles-it" in moved[0].reason


# --- the measurement, under real isolation--------------------------------------------------------


@pytest.mark.skipif(
    not _isolated(),
    reason="no OS-enforced sandbox on this host; this must not pass without one",
)
def test_a_proposed_question_is_measured_in_the_sandbox_and_reaches_a_verdict(
    database: Database,
) -> None:
    """The milestone, end to end, with no task inserted by hand."""
    with database.transaction() as session:
        seed(session)
        load_prior_findings(ResearchMemory(session))
        assert ResearchDirector(session).by_state() == []

        propose(session, selector=FirstEligibleSelector(), now=NOW, code_commit="test")
        director = ResearchDirector(session)
        # Two steps: PROPOSED -> SCOUTING through the gates, then SCOUTING -> a verdict.
        moved = [
            item
            for item in drain(
                director, _stages(session), actor="test", now=NOW, max_steps=2
            )
            if not item.idle
        ]
        task = director.by_state()[0]
        events = (
            session.execute(
                research_task_events.select().where(
                    research_task_events.c.task_id == task.task_id
                )
            )
            .mappings()
            .all()
        )

    assert [item.moved_to for item in moved] == [TaskState.SCOUTING, TaskState.UNDERPOWERED]
    assert task.terminal, "the task must reach a terminal scientific verdict, not merely move"

    measurement = json.loads(events[-1]["detail_json"])
    assert measurement["sandbox_backend"] in {"WindowsSandbox", "WslSandbox"}
    assert measurement["sandbox_network_enabled"] == "false"
    assert int(measurement["observations"]) > 0
    assert measurement["script_sha256"], "the executed script must be content-addressed"
    assert measurement["panel_sha256"], "the observations must be content-addressed"
    # The verdict is quantitative, not a shrug: it names what confirmation would need.
    assert int(measurement["observations_required"]) > int(
        measurement["confirmatory_observations_available"]
    )
    assert "is emphatically not a refutation" in moved[-1].reason


@pytest.mark.skipif(not _isolated(), reason="no OS-enforced sandbox on this host")
def test_the_measurement_runs_against_the_real_panel(database: Database) -> None:
    with database.transaction() as session:
        seed(session)
        source = next(item for item in DATA_SOURCES if item.dataset == "sip-us-equities-daily")
        panel = load_panel(session, source, as_of=NOW.date())

    result = run_measurement(CATALOG[0], panel)

    assert result.observations == len(SESSIONS) - 2, (
        "one observation is lost to differencing and one to the forward shift"
    )
    assert result.universe == len(UNIVERSE)
    assert not result.degenerate
    assert -1 <= result.statistic <= 1


@pytest.mark.skipif(not _isolated(), reason="no OS-enforced sandbox on this host")
def test_every_catalogue_measurement_passes_the_static_scan(database: Database) -> None:
    """The scripts are machine-assembled, so nothing reviews the particular one that runs."""
    with database.transaction() as session:
        seed(session)
        source = next(item for item in DATA_SOURCES if item.dataset == "sip-us-equities-daily")
        panel = load_panel(session, source, as_of=NOW.date())

    for template in CATALOG:
        enforce(build_script(template.measurement_id, panel))


def test_an_unenforced_backend_is_refused(database: Database) -> None:
    """`SandboxRunner` is honest about not being OS-enforced, and this must honour that."""
    with database.transaction() as session:
        seed(session)
        source = next(item for item in DATA_SOURCES if item.dataset == "sip-us-equities-daily")
        panel = load_panel(session, source, as_of=NOW.date())

    with pytest.raises(ExperimentRefused, match="requires an OS-enforced sandbox"):
        run_measurement(CATALOG[0], panel, sandbox=SandboxRunner())


def test_a_panel_without_the_benchmark_is_refused(database: Database) -> None:
    with database.transaction() as session:
        seed(session, symbols=("QQQ", "IWM", "XLK"))
        source = next(item for item in DATA_SOURCES if item.dataset == "sip-us-equities-daily")

        with pytest.raises(ExperimentRefused, match="SPY is not present"):
            load_panel(session, source, as_of=NOW.date())


def test_an_unknown_measurement_is_a_catalogue_defect() -> None:
    panel = Panel(identity="x", symbols=("SPY",), dates=(date(2026, 8, 24),), closes=((1.0,),))
    with pytest.raises(ExperimentRefused, match="not a known measurement"):
        build_script("measure_something_nobody_wrote", panel)


# --- grading is deterministic and out of the child's reach----------------------------------------


def _result(statistic: str = "0.05", degenerate: bool = False) -> MeasurementResult:
    return MeasurementResult(
        measurement_id=CATALOG[0].measurement_id,
        observations=300,
        statistic=Decimal(statistic),
        t_statistic=Decimal("0.9"),
        degenerate=degenerate,
        sessions=302,
        universe=23,
        panel_digest="p" * 64,
        script_sha256="s" * 64,
        backend="WindowsSandbox",
        duration_seconds=0.1,
        peak_memory_mb=8.0,
        stdout="",
        stderr="",
    )


def test_too_little_confirmatory_capacity_is_underpowered_not_refuted() -> None:
    verdict = grade(
        CATALOG[0], _result(), cumulative_trials=68, confirmatory_observations=14
    )

    assert verdict.next_state is TaskState.UNDERPOWERED
    assert verdict.luck_bar > Decimal("2.8"), "68 trials must not be judged at 1.96"
    assert verdict.observations_required_for_confirmation > 14
    assert "not a refutation" in verdict.reason


def test_sufficient_capacity_advances_to_critique_instead() -> None:
    """The other branch really exists; the verdict is a function of state, not a constant."""
    verdict = grade(
        CATALOG[0], _result(), cumulative_trials=68, confirmatory_observations=100_000
    )

    assert verdict.next_state is TaskState.CRITIQUE


def test_a_degenerate_statistic_blocks_rather_than_concluding() -> None:
    verdict = grade(
        CATALOG[0], _result(degenerate=True), cumulative_trials=68, confirmatory_observations=14
    )

    assert verdict.next_state is TaskState.BLOCKED
    assert "degenerate" in verdict.reason


def test_the_bar_rises_with_accumulated_trials() -> None:
    """A question asked of a well-searched range is not judged as though it were the first."""
    fresh = grade(CATALOG[0], _result(), cumulative_trials=1, confirmatory_observations=14)
    spent = grade(CATALOG[0], _result(), cumulative_trials=68, confirmatory_observations=14)

    assert spent.luck_bar > fresh.luck_bar
    assert (
        spent.observations_required_for_confirmation
        > fresh.observations_required_for_confirmation
    )
