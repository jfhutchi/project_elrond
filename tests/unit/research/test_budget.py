"""Bounds that hold, and a boundary the trading daemon must never be on the wrong side of."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.budget import (
    RENEWABLE,
    BudgetExceeded,
    BudgetGovernor,
    BudgetOverride,
    Cap,
    LoopBound,
    Resource,
    dataset_key,
    family_key,
)
from quantbot.storage import Database

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)
SIP = dataset_key("sip-us-equities-daily")


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "budget.db")
    yield db
    db.close()


def caps() -> list[Cap]:
    return [
        Cap(budget_key=SIP, resource=Resource.TRIALS, limit=Decimal("100")),
        Cap(budget_key=family_key("trend"), resource=Resource.TRIALS, limit=Decimal("10")),
        Cap(budget_key="global", resource=Resource.TOKENS, limit=Decimal("1000000")),
    ]


def test_a_trial_budget_fails_closed_when_the_window_is_spent(database: Database) -> None:
    """Cycles 2-10 spent this window. The governor is what stops cycle 30 doing it again."""
    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        governor.spend(SIP, Resource.TRIALS, Decimal("68"), task="cycles 1-17", now=NOW)

    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        assert governor.spent(SIP, Resource.TRIALS) == Decimal("68")
        # Inside the cap, but past the warning line.
        admission = governor.admit(SIP, Resource.TRIALS, Decimal("20"))
        assert admission.warning
        assert admission.remaining == Decimal("32")
        assert admission.permanent

        with pytest.raises(BudgetExceeded) as error:
            governor.admit(SIP, Resource.TRIALS, Decimal("40"))
    assert error.value.resource is Resource.TRIALS
    assert "NON-RENEWABLE" in str(error.value)


def test_an_unbudgeted_key_is_refused_rather_than_allowed(database: Database) -> None:
    """Fail closed: a resource nobody capped is not a resource anybody may spend."""
    with database.transaction() as session, pytest.raises(BudgetExceeded):
        BudgetGovernor(session, caps()).admit(
            dataset_key("something-nobody-capped"), Resource.TRIALS, Decimal("1")
        )


def test_statistical_budget_is_keyed_per_dataset_and_family_not_globally(
    database: Database,
) -> None:
    """One global number hides which evidence was spent, and on what."""
    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        governor.spend(SIP, Resource.TRIALS, Decimal("90"), task="equities work", now=NOW)

    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        # US equities is nearly spent; the trend family has its own untouched allowance.
        assert governor.spent(SIP, Resource.TRIALS) == Decimal("90")
        assert governor.spent(family_key("trend"), Resource.TRIALS) == Decimal("0")
        assert governor.admit(family_key("trend"), Resource.TRIALS, Decimal("5")).remaining == (
            Decimal("10")
        )


def test_a_worker_spends_what_it_evaluated_not_what_it_returned(database: Database) -> None:
    """An external miner that evaluates 1,000 candidates and returns one has spent 1,000."""
    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        with pytest.raises(BudgetExceeded) as error:
            governor.admit(SIP, Resource.TRIALS, Decimal("1000"))
    assert error.value.requested == Decimal("1000")

    # Whereas the single candidate it returned would have sailed through.
    with database.transaction() as session:
        assert BudgetGovernor(session, caps()).admit(SIP, Resource.TRIALS, Decimal("1"))


def test_an_override_is_attributable_and_permanent(database: Database) -> None:
    """Raising a statistical cap is an evidence decision, so it is stored as one."""
    override = BudgetOverride(
        authorized_by="hutch", reason="one final confirmatory run before the window is retired"
    )
    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        governor.spend(SIP, Resource.TRIALS, Decimal("95"), task="prior work", now=NOW)
        governor.spend(
            SIP,
            Resource.TRIALS,
            Decimal("20"),
            task="over-cap run",
            now=NOW,
            override=override,
        )

    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        assert governor.spent(SIP, Resource.TRIALS) == Decimal("115")
        assert governor.overrides() == [("hutch", SIP, "TRIALS")]

        # And the cap still bites for anyone without an override.
        with pytest.raises(BudgetExceeded):
            governor.admit(SIP, Resource.TRIALS, Decimal("1"))


def test_spend_is_append_only_so_a_budget_cannot_be_rewound(database: Database) -> None:
    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        for _ in range(3):
            governor.spend(SIP, Resource.TRIALS, Decimal("10"), task="repeat", now=NOW)
    with database.transaction() as session:
        assert BudgetGovernor(session, caps()).spent(SIP, Resource.TRIALS) == Decimal("30")


def test_low_value_work_is_declined_even_when_compute_is_free(database: Database) -> None:
    """The Research Director's deferral: not everything affordable is worth the evidence."""
    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        governor.spend(SIP, Resource.TRIALS, Decimal("50"), task="prior", now=NOW)

    with database.transaction() as session:
        governor = BudgetGovernor(session, caps())
        # 30 of a 100-trial budget for a question worth little is refused.
        assert not governor.worth_spending(
            SIP, Decimal("30"), expected_information_gain=Decimal("0.1")
        )
        # The same spend for a question worth a lot is admitted.
        assert governor.worth_spending(SIP, Decimal("30"), expected_information_gain=Decimal("0.5"))
        # And nothing beyond the remaining allowance is affordable at any value.
        assert not governor.worth_spending(
            SIP, Decimal("60"), expected_information_gain=Decimal("1.0")
        )


def test_a_recursive_workflow_stops_at_its_bound() -> None:
    """A loop that can always try once more does not terminate."""
    bound = LoopBound(task="critic-revision", max_rounds=3)
    for expected in (1, 2, 3):
        bound = bound.advance()
        assert bound.rounds_taken == expected
    assert bound.exhausted

    with pytest.raises(BudgetExceeded) as error:
        bound.advance()
    assert error.value.resource is Resource.ROUNDS

    with pytest.raises(ValueError, match="against a bound of"):
        LoopBound(task="critic-revision", max_rounds=2, rounds_taken=3)


def test_renewable_and_permanent_resources_are_distinguished() -> None:
    """The distinction is the whole design principle, so it is asserted rather than assumed."""
    assert Resource.TRIALS not in RENEWABLE
    assert {Resource.TOKENS, Resource.WALL_SECONDS, Resource.DOLLARS} <= RENEWABLE

    admission_permanence = {resource: resource not in RENEWABLE for resource in Resource}
    assert admission_permanence[Resource.TRIALS] is True
    assert admission_permanence[Resource.TOKENS] is False


def test_research_budgets_cannot_starve_the_trading_daemon() -> None:
    """The boundary both reviews asked to be tested rather than assumed.

    The paper account is the only genuinely uncontaminated evidence this project will ever get.
    If the trading path imported research code, a research budget refusal could reach it. This
    walks the import graph rather than trusting the layering.
    """
    root = Path(__file__).resolve().parents[3] / "src" / "quantbot"
    trading_modules = [
        root / "runtime.py",
        root / "cli.py",
        *sorted((root / "operations").glob("*.py")),
        *sorted((root / "execution").glob("*.py")),
        *sorted((root / "brokers").glob("*.py")),
        *sorted((root / "risk").glob("*.py")),
    ]
    assert len(trading_modules) > 15

    # Top-level imports only. The property is that research is not a *load-time* dependency
    # of the trading path: importing the kill switch must not import the budget governor. A
    # function-local import inside an operator-only command is fine and is why `quantbot
    # hypotheses` can still exist in `cli.py`.
    offenders: list[str] = []
    for module in trading_modules:
        for node in ast.parse(module.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "quantbot.research"
            ):
                offenders.append(f"{module.name} imports {node.module}")
            if isinstance(node, ast.Import):
                offenders.extend(
                    f"{module.name} imports {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("quantbot.research")
                )

    assert offenders == [], (
        "the trading path must not depend on research code, or a research budget refusal "
        "could interrupt the paper account: " + "; ".join(offenders)
    )
