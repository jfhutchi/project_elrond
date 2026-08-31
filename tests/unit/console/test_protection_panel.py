"""Operator visibility for the independent position-protection supervisor (#157)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.domain import Account, Position, StrategyIdentity
from quantbot.storage import Database, StorageRepository
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import position_protection_cycles

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
) -> None:
    identity = StrategyIdentity(
        strategy_id="adaptive-momentum",
        version="1.0.0",
        git_commit="abc123",
        configuration_hash="cfg-123",
        deployment_timestamp=observed_at - timedelta(days=30),
    )
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_strategy_deployment(identity)
        session.execute(
            position_protection_cycles.insert().values(
                cycle_id="console-protection-cycle",
                observed_at=encode_utc(observed_at),
                completed_at=encode_utc(observed_at),
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                account_id=ACCOUNT_ID,
                policy_version="position-protection-v1",
                policy_hash="a" * 64,
                outcome=outcome,
                reason=reason,
                position_count=1,
                evaluated_count=1,
                unprotected_count=unprotected,
                exit_required_count=exits_required,
                detail="{}",
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
