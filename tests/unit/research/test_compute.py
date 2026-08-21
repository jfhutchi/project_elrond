"""Sandboxed work meeting the budget that is supposed to bound it (#14).

`BudgetGovernor` could account for wall-clock seconds and `SandboxPolicy` could enforce a wall
clock, and nothing connected them. These tests are about the connection, not about either side:
each is well covered alone, and a research loop can still run a hundred experiments inside a
budget that records none of them if the two never meet.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from quantbot.research.budget import BudgetGovernor, Cap, Resource
from quantbot.research.compute import (
    MINIMUM_USEFUL_SECONDS,
    ComputeExhausted,
    policy_within_budget,
    record_sandbox_run,
)
from quantbot.sandbox import SandboxPolicy
from quantbot.sandbox.runner import SandboxResult
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
KEY = "compute:research"


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "compute.db")
    yield db
    db.close()


def governor(session, limit: str = "600") -> BudgetGovernor:
    return BudgetGovernor(
        session,
        caps=[Cap(budget_key=KEY, resource=Resource.WALL_SECONDS, limit=Decimal(limit))],
    )


def finished(
    seconds: float, *, terminated: str | None = None, peak: float | None = 42.5
) -> SandboxResult:
    return SandboxResult(
        ok=terminated is None,
        exit_code=0 if terminated is None else None,
        stdout="",
        stderr="",
        duration_seconds=seconds,
        peak_memory_mb=peak,
        terminated_reason=terminated,
    )


def test_a_sandboxed_run_is_charged_to_the_compute_budget(database: Database) -> None:
    """The connection that did not exist: work done inside a budget that records it."""
    with database.transaction() as session:
        book = governor(session)
        charged = record_sandbox_run(
            book, finished(12.5), budget_key=KEY, task="experiment-1", now=NOW
        )
        spent = book.spent(KEY, Resource.WALL_SECONDS)

    assert charged == Decimal("12.500")
    assert spent == Decimal("12.500")


def test_a_terminated_run_is_charged_in_full(database: Database) -> None:
    """It used the wall clock. Being killed at the ceiling does not refund the seconds.

    Charging only successful runs would make failing a way to get free compute, which is the
    same argument that puts holdout consumption before the measurement rather than after it.
    """
    with database.transaction() as session:
        book = governor(session)
        charged = record_sandbox_run(
            book,
            finished(60.0, terminated="wall-clock"),
            budget_key=KEY,
            task="runaway",
            now=NOW,
        )
        spent = book.spent(KEY, Resource.WALL_SECONDS)

    assert charged == Decimal("60.000")
    assert spent == Decimal("60.000"), "a killed run still spent the time"


def test_an_overrun_is_recorded_rather_than_dropped(database: Database) -> None:
    """A budget that omits the overrun it failed to prevent understates itself permanently.

    The time is already gone by the time anyone knows, so refusing to record it would leave the
    ledger reporting less spend than actually happened -- which is the exact failure this module
    exists to prevent, committed by the accounting rather than by the agent.
    """
    with database.transaction() as session:
        book = governor(session, limit="10")
        record_sandbox_run(book, finished(45.0), budget_key=KEY, task="overrun", now=NOW)
        spent = book.spent(KEY, Resource.WALL_SECONDS)
        attributed = book.overrides()
        notes = session.execute(text("SELECT note FROM budget_spend")).all()

    assert spent == Decimal("45.000"), "the ledger holds what happened, not what was allowed"
    # Recorded as an override rather than as an ordinary spend, and attributable, so a reader
    # can tell a cap that was respected from one that was blown through after the fact.
    assert attributed == [("compute-accounting", KEY, "WALL_SECONDS")]
    assert any("already completed" in str(row[0]) for row in notes), notes


def test_the_budget_shrinks_the_sandbox_rather_than_only_reporting_on_it(
    database: Database,
) -> None:
    """Forward-looking, and the reason this is a control rather than a diary."""
    generous = SandboxPolicy(wall_clock_seconds=300.0)

    with database.transaction() as session:
        book = governor(session, limit="600")
        record_sandbox_run(book, finished(450.0), budget_key=KEY, task="earlier", now=NOW)
        constrained = policy_within_budget(book, generous, budget_key=KEY)

    assert constrained.wall_clock_seconds == pytest.approx(150.0), (
        "150 seconds remain, so that is the ceiling the next run gets"
    )
    # Everything else about the policy is untouched.
    assert constrained.memory_mb == generous.memory_mb
    assert constrained.allowed_third_party == generous.allowed_third_party


def test_a_policy_within_budget_is_never_widened(database: Database) -> None:
    """The budget is a ceiling, not a target.

    Handing a short experiment the whole remaining allowance would spend it on the first thing
    that asked, which is how a budget stops constraining anything.
    """
    modest = SandboxPolicy(wall_clock_seconds=30.0)

    with database.transaction() as session:
        unchanged = policy_within_budget(governor(session, limit="600"), modest, budget_key=KEY)

    assert unchanged.wall_clock_seconds == pytest.approx(30.0)
    assert unchanged is modest, "an affordable policy comes back untouched"


def test_an_exhausted_compute_budget_refuses_rather_than_returning_a_useless_sandbox(
    database: Database,
) -> None:
    """A one-second sandbox is a wall-clock termination with extra steps.

    Returning one would turn budget exhaustion into a stream of terminated results that read
    like flaky experiments, and an operator would go looking for a bug in the sandbox.
    """
    with database.transaction() as session:
        book = governor(session, limit="600")
        record_sandbox_run(book, finished(598.0), budget_key=KEY, task="earlier", now=NOW)

        with pytest.raises(ComputeExhausted) as exhausted:
            policy_within_budget(book, SandboxPolicy(wall_clock_seconds=60.0), budget_key=KEY)

    assert exhausted.value.budget_key == KEY
    assert exhausted.value.remaining < MINIMUM_USEFUL_SECONDS


def test_peak_memory_is_reported_and_never_budgeted(database: Database) -> None:
    """Two runs peaking at 500MB have not used a gigabyte of anything.

    Adding a peak to a running total produces a number that looks like a budget and means
    nothing, so memory reaches the ledger as a note on the spend rather than as a spend.
    """
    with database.transaction() as session:
        book = governor(session)
        record_sandbox_run(
            book, finished(3.0, peak=512.0), budget_key=KEY, task="hungry", now=NOW
        )
        record_sandbox_run(
            book, finished(3.0, peak=512.0), budget_key=KEY, task="hungrier", now=NOW
        )
        wall = book.spent(KEY, Resource.WALL_SECONDS)

    assert wall == Decimal("6.000")
    # There is no memory resource to accumulate into, which is the point.
    assert not hasattr(Resource, "MEMORY_MB")


def test_an_unmeasured_peak_is_recorded_as_unmeasured(database: Database) -> None:
    """A process that exits between polls is measured zero times, not measured at zero."""
    with database.transaction() as session:
        book = governor(session)
        record_sandbox_run(
            book, finished(0.2, peak=None), budget_key=KEY, task="brief", now=NOW
        )
        notes = session.execute(
            text("SELECT note FROM budget_spend")
        ).all()

    assert any("unmeasured" in str(row[0]) for row in notes), notes
