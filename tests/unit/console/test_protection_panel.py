"""Operator visibility for the independent position-protection supervisor (#157)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.domain import (
    Account,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import (
    position_protection_action_evaluations,
    position_protection_actions,
    position_protection_cycles,
    position_protection_evaluations,
)

ACCOUNT_ID = "paper-protection-account"


@pytest.fixture
def database(tmp_path: Path) -> Database:
    with Database.create(tmp_path / "console-protection.db") as store:
        yield store


@pytest.fixture
def client(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", ACCOUNT_ID)
    return TestClient(create_app(database))


def _seed_open_position(database: Database, *, now: datetime) -> None:
    with database.transaction() as session:
        StorageRepository(session).save_account_snapshot(
            "protection-console-account",
            Account(
                account_id=ACCOUNT_ID,
                cash=Decimal("10000"),
                buying_power=Decimal("10000"),
                equity=Decimal("10000"),
                currency="USD",
            ),
            (
                Position(
                    symbol="SPY",
                    quantity=Decimal("1"),
                    market_price=Decimal("500"),
                    market_value=Decimal("500"),
                    average_entry_price=Decimal("450"),
                ),
            ),
            captured_at=now,
        )


def _seed_protection_cycle(
    database: Database,
    *,
    observed_at: datetime,
    outcome: str = "DEGRADED",
    reason: str = "POSITION_STATE_UNRECONCILED",
    unprotected: int = 1,
    exits_required: int = 0,
    account_id: str = ACCOUNT_ID,
    cycle_id: str = "console-protection-cycle",
    position_count: int | None = 1,
    evaluated_count: int = 1,
) -> None:
    identity = StrategyIdentity(
        strategy_id="adaptive-momentum",
        version="1.0.0",
        git_commit="abc123",
        configuration_hash="cfg-123",
        deployment_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
    )
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_strategy_deployment(identity)
        session.execute(
            position_protection_cycles.insert().values(
                cycle_id=cycle_id,
                observed_at=encode_utc(observed_at),
                completed_at=encode_utc(observed_at),
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                account_id=account_id,
                policy_version="position-protection-v1",
                policy_hash="a" * 64,
                outcome=outcome,
                reason=reason,
                position_count=position_count,
                evaluated_count=evaluated_count,
                unprotected_count=unprotected,
                exit_required_count=exits_required,
                detail="{}",
            )
        )


def _seed_protection_action(
    database: Database,
    *,
    observed_at: datetime,
    status: str,
    cycle_id: str = "console-protection-cycle",
) -> None:
    intent = OrderIntent(
        intent_id="intent-console-protection",
        client_order_id="qb-protect-console",
        strategy_id="adaptive-momentum",
        signal_date=observed_at.date(),
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("1"),
        created_at=observed_at,
    )
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(intent)
        repository.transition_order_intent(
            intent.intent_id,
            IntentState.ORDER_CREATED,
            expected_current=IntentState.RISK_APPROVED,
        )
        submitting = repository.transition_order_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            expected_current=IntentState.ORDER_CREATED,
        )
        if status == "WORKING":
            repository.transition_order_intent(
                intent.intent_id,
                IntentState.BROKER_ACCEPTED,
                expected_current=submitting.state,
            )
        elif status == "UNCERTAIN":
            repository.transition_order_intent(
                intent.intent_id,
                IntentState.SUBMISSION_UNKNOWN,
                expected_current=submitting.state,
            )
        session.execute(
            position_protection_evaluations.insert().values(
                evaluation_id="console-protection-evaluation",
                cycle_id=cycle_id,
                account_id=ACCOUNT_ID,
                symbol="SPY",
                quantity="1",
                average_entry_price="450",
                price="350",
                price_as_of=encode_utc(observed_at),
                price_source="BROKER_MARK",
                strategy_id="adaptive-momentum",
                strategy_version="1.0.0",
                configuration_hash="cfg-123",
                policy_version="position-protection-v1",
                policy_hash="a" * 64,
                reconciled=1,
                outcome="EXIT_REQUIRED",
                rule="HARD_STOP",
                reason="hard stop crossed",
                move_bps="-2222",
                protected=1,
                evaluated_at=encode_utc(observed_at),
                detail='{"requires_exit":true}',
            )
        )
        session.execute(
            position_protection_actions.insert().values(
                action_id="console-protection-action",
                trigger_evaluation_id="console-protection-evaluation",
                account_id=ACCOUNT_ID,
                symbol="SPY",
                side="SELL",
                quantity="1",
                intent_id=intent.intent_id,
                client_order_id=intent.client_order_id,
                status=status,
                created_at=encode_utc(observed_at),
                updated_at=encode_utc(observed_at),
                completed_at=(encode_utc(observed_at) if status == "COMPLETED" else None),
                flat_confirmation_snapshot_id=(
                    "protection-console-account" if status == "COMPLETED" else None
                ),
                flat_confirmed_at=(encode_utc(observed_at) if status == "COMPLETED" else None),
                last_error="",
                detail="{}",
            )
        )
        session.execute(
            position_protection_action_evaluations.insert().values(
                action_id="console-protection-action",
                evaluation_id="console-protection-evaluation",
                linked_at=encode_utc(observed_at),
            )
        )


def test_open_positions_without_a_cycle_are_explicitly_unprotected(
    database: Database,
    client: TestClient,
) -> None:
    _seed_open_position(database, now=datetime.now(UTC))

    page = client.get("/").text
    health = client.get("/api/state").json()["health"]

    assert "never evaluated" in page
    assert "open positions have no measurable protection state" in page
    assert "1 open position(s) have never received a durable protection evaluation" in page
    assert health["last_protection_at"] is None
    assert health["unprotected_positions"] is None


def test_latest_degraded_protection_cycle_is_visible_and_actionable(
    database: Database,
    client: TestClient,
) -> None:
    moment = datetime.now(UTC) - timedelta(minutes=5)
    _seed_open_position(database, now=moment)
    _seed_protection_cycle(database, observed_at=moment)

    page = client.get("/").text
    health = client.get("/api/state").json()["health"]

    assert "DEGRADED" in page
    assert "1 unprotected" in page
    assert "1 position(s) are explicitly unprotected" in page
    assert health["last_protection_outcome"] == "DEGRADED"
    assert health["protected_positions"] == 0
    assert health["unprotected_positions"] == 1


def test_stale_protection_loop_is_reported_while_a_position_is_open(
    database: Database,
    client: TestClient,
) -> None:
    moment = datetime.now(UTC) - timedelta(hours=1)
    _seed_open_position(database, now=moment)
    _seed_protection_cycle(
        database,
        observed_at=moment,
        outcome="HEALTHY",
        reason="",
        unprotected=0,
    )

    page = client.get("/").text

    assert "the position-protection loop last ran" in page
    assert "while 1 position(s) remain open" in page


def test_a_newer_cycle_for_another_account_cannot_mask_this_account(
    database: Database,
    client: TestClient,
) -> None:
    moment = datetime.now(UTC)
    _seed_open_position(database, now=moment - timedelta(minutes=10))
    _seed_protection_cycle(
        database,
        observed_at=moment - timedelta(minutes=5),
        outcome="HEALTHY",
        reason="",
        unprotected=0,
        cycle_id="expected-account-cycle",
    )
    _seed_protection_cycle(
        database,
        observed_at=moment,
        account_id="different-paper-account",
        cycle_id="different-account-cycle",
    )

    health = client.get("/api/state").json()["health"]

    assert health["last_protection_outcome"] == "HEALTHY"
    assert health["unprotected_positions"] == 0


def test_a_cycle_that_could_not_evaluate_the_open_position_reports_the_gap(
    database: Database,
    client: TestClient,
) -> None:
    moment = datetime.now(UTC)
    _seed_open_position(database, now=moment)
    _seed_protection_cycle(
        database,
        observed_at=moment,
        outcome="INCIDENT",
        reason="BROKER_STATE_UNAVAILABLE",
        unprotected=0,
        position_count=None,
        evaluated_count=0,
    )

    page = client.get("/").text

    assert "1 open position(s) are absent from the latest protection cycle" in page


def test_a_working_protective_exit_is_visible_and_not_reported_as_unhandled(
    database: Database,
    client: TestClient,
) -> None:
    moment = datetime.now(UTC)
    _seed_open_position(database, now=moment)
    _seed_protection_cycle(
        database,
        observed_at=moment,
        outcome="ACTION_REQUIRED",
        reason="PROTECTIVE_EXIT_REQUIRED",
        unprotected=0,
        exits_required=1,
    )
    _seed_protection_action(database, observed_at=moment, status="WORKING")

    page = client.get("/").text
    health = client.get("/api/state").json()["health"]

    assert "exit actions:" in page
    assert "WORKING 1" in page
    assert "no durable exit action exists" not in page
    assert health["protection_action_counts"] == {"WORKING": 1}
    assert health["latest_protection_action_count"] == 1


def test_an_old_completed_action_does_not_mask_an_unhandled_latest_exit(
    database: Database,
    client: TestClient,
) -> None:
    old = datetime.now(UTC) - timedelta(minutes=10)
    latest = old + timedelta(minutes=5)
    _seed_open_position(database, now=latest)
    _seed_protection_cycle(
        database,
        observed_at=old,
        outcome="ACTION_REQUIRED",
        reason="PROTECTIVE_EXIT_REQUIRED",
        unprotected=0,
        exits_required=1,
        cycle_id="old-protection-cycle",
    )
    _seed_protection_action(
        database,
        observed_at=old,
        status="COMPLETED",
        cycle_id="old-protection-cycle",
    )
    _seed_protection_cycle(
        database,
        observed_at=latest,
        outcome="ACTION_REQUIRED",
        reason="PROTECTIVE_EXIT_REQUIRED",
        unprotected=0,
        exits_required=1,
        cycle_id="latest-protection-cycle",
    )

    page = client.get("/").text
    health = client.get("/api/state").json()["health"]

    assert "no durable exit action exists" in page
    assert health["protection_action_counts"] == {"COMPLETED": 1}
    assert health["latest_protection_action_count"] == 0


def test_an_uncertain_protective_exit_warns_against_blind_resubmission(
    database: Database,
    client: TestClient,
) -> None:
    moment = datetime.now(UTC)
    _seed_open_position(database, now=moment)
    _seed_protection_cycle(
        database,
        observed_at=moment,
        outcome="ACTION_REQUIRED",
        reason="PROTECTIVE_EXIT_REQUIRED",
        unprotected=0,
        exits_required=1,
    )
    _seed_protection_action(database, observed_at=moment, status="UNCERTAIN")

    page = client.get("/").text

    assert "uncertain broker outcome" in page
    assert "will not be resubmitted blindly" in page
