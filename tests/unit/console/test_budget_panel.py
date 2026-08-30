"""What research has already spent, and which of it comes back (#82, #14).

The distinction the panel exists for: wall seconds and tokens renew, and trials against a dataset
window do not. Every candidate evaluated uses up a little of what that data can still be asked,
and a table that showed both kinds identically would let a reader treat a permanent cost as a
recurring one -- which is the whole reason `budget.py` separates them rather than counting tokens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.state import read_console_state
from quantbot.research.budget import Resource
from quantbot.storage import Database

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "budget.db")
    try:
        yield db
    finally:
        db.close()


def spend(database: Database, key: str, resource: Resource, amount: str) -> None:
    """Record spend directly.

    `BudgetGovernor.admit` is the production writer and needs a whole admission to exercise; this
    panel reads the ledger, so the ledger is what the test writes.
    """
    from quantbot.storage.schema import budget_spend

    with database.transaction() as session:
        session.execute(
            budget_spend.insert().values(
                budget_key=key,
                category=resource.value,
                amount=amount,
                task="task-1",
                recorded_at=NOW.isoformat(),
                override_by=None,
            )
        )


def budgets(database: Database):
    with database.transaction() as session:
        return read_console_state(session, now=NOW).budgets


def test_an_empty_ledger_is_not_an_unlimited_budget(database: Database) -> None:
    """Absence styled as absence, the rule this console applies everywhere else."""
    assert budgets(database) == ()

    body = TestClient(create_app(database)).get("/research").text
    assert "empty ledger, not a system with unlimited budget" in body


def test_a_permanent_cost_is_distinguished_from_a_recurring_one(database: Database) -> None:
    """The distinction the panel exists for.

    Trials against a dataset window are gone once spent -- that is why `budget.py` exists rather
    than a token counter. Showing them beside wall seconds without saying which is which would
    invite exactly the reading the module was built to prevent.
    """
    spend(database, "family:momentum", Resource.TRIALS, "40")
    spend(database, "family:momentum", Resource.WALL_SECONDS, "1200")

    rows = {row.resource: row for row in budgets(database)}

    assert rows["TRIALS"].renewable is False
    assert rows["WALL_SECONDS"].renewable is True


def test_non_renewable_spend_is_listed_first(database: Database) -> None:
    """A permanent cost deserves the reader's attention before a recurring one.

    Alphabetical ordering would put DOLLARS and REQUESTS above TRIALS, burying the only number on
    the page that cannot be recovered.
    """
    spend(database, "family:aaa", Resource.WALL_SECONDS, "10")
    spend(database, "family:zzz", Resource.TRIALS, "5")

    ordered = budgets(database)

    assert ordered[0].resource == "TRIALS"
    assert ordered[0].budget_key == "family:zzz"


def test_spend_is_summed_across_entries(database: Database) -> None:
    """The ledger is append-only, so a budget's cost is a sum rather than a latest value."""
    spend(database, "family:momentum", Resource.TRIALS, "10")
    spend(database, "family:momentum", Resource.TRIALS, "32")

    rows = budgets(database)

    assert len(rows) == 1
    assert Decimal(rows[0].spent) == Decimal("42")


def test_a_budget_nobody_capped_still_appears(database: Database) -> None:
    """Read from recorded spend rather than from configured caps.

    Listing caps instead would fill the table with rows describing nothing while hiding the one
    resource being consumed without a limit -- which is the resource most worth seeing.
    """
    spend(database, "family:uncapped", Resource.TRIALS, "7")

    rows = budgets(database)

    assert len(rows) == 1
    assert rows[0].cap is None
    assert rows[0].fraction is None


def test_an_unknown_category_is_reported_rather_than_dropped(database: Database) -> None:
    """A spend nobody can classify is still a spend.

    Dropping it would understate the total, and it would do so silently -- the failure mode of
    every filter that quietly discards what it does not recognise.
    """
    from quantbot.storage.schema import budget_spend

    with database.transaction() as session:
        session.execute(
            budget_spend.insert().values(
                budget_key="family:momentum",
                category="SOMETHING_NEW",
                amount="3",
                task="task-1",
                recorded_at=NOW.isoformat(),
                override_by=None,
            )
        )

    rows = budgets(database)

    assert [row.resource for row in rows] == ["SOMETHING_NEW"]
    assert rows[0].renewable is False, "an unclassifiable spend is not assumed to renew"


def test_the_page_renders_both_kinds(database: Database) -> None:
    """A reader has to be able to tell them apart without knowing the enum."""
    spend(database, "family:momentum", Resource.TRIALS, "40")
    spend(database, "family:momentum", Resource.TOKENS, "9000")

    body = TestClient(create_app(database)).get("/research").text

    assert "permanent" in body
    assert "renews" in body
    assert "trials against a dataset window do not" in body
