from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from quantbot.brokers import (
    BrokerAccountSnapshot,
    BrokerAdapter,
    BrokerRequestError,
    SubmissionUncertainError,
)
from quantbot.config import Settings
from quantbot.domain import (
    Account,
    BrokerOrder,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    ReconciliationStatus,
    TimeInForce,
)
from quantbot.execution import ExecutionCoordinator, SubmissionPolicy, SubmissionResult
from quantbot.risk import OrderPurpose
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        EXPECTED_ACCOUNT_ID="paper-1",
        ALPACA_PAPER_API_KEY="paper-key",
        ALPACA_PAPER_API_SECRET="paper-secret",
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=True,
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="stable-client-id",
        strategy_id="full-v1:test",
        symbol="SPY",
        signal_date=date(2026, 8, 14),
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        created_at=NOW,
    )


def _accepted() -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-1",
        client_order_id="stable-client-id",
        symbol="SPY",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        filled_quantity=Decimal("0"),
        status="accepted",
        submitted_at=NOW,
    )


class ScriptedAmbiguousBroker:
    def __init__(
        self,
        submit_outcomes: list[BrokerOrder | Exception],
        lookup_outcomes: list[BrokerOrder | None],
    ) -> None:
        self.submit_outcomes = submit_outcomes
        self.lookup_outcomes = lookup_outcomes
        self.submitted_client_ids: list[str] = []
        self.lookup_client_ids: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            account=Account(
                account_id="paper-1",
                cash=Decimal("10000"),
                buying_power=Decimal("20000"),
                equity=Decimal("10000"),
                currency="USD",
            ),
            account_number="paper-1",
            status="ACTIVE",
            order_entry_allowed=True,
            restrictions=(),
        )

    async def get_positions(self) -> tuple[()]:
        return ()

    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        self.submitted_client_ids.append(intent.client_order_id)
        outcome = self.submit_outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def get_order_by_client_id(
        self,
        client_order_id: str,
        *,
        expected: OrderIntent | None = None,
    ) -> BrokerOrder | None:
        self.lookup_client_ids.append(client_order_id)
        return self.lookup_outcomes.pop(0)


def _coordinator(database: Database, broker: ScriptedAmbiguousBroker) -> ExecutionCoordinator:
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(
            False,
            reason="test ready",
            updated_at=NOW,
        )
        StorageRepository(session).save_reconciliation(
            "ready-reconciliation",
            status=ReconciliationStatus.RECONCILED,
            started_at=NOW,
            completed_at=NOW,
            diffs=(),
        )
    return ExecutionCoordinator(
        database,
        cast(BrokerAdapter, broker),
        _settings(),
        submission_policy=SubmissionPolicy(max_not_found_retries=1),
    )


async def _submit(coordinator: ExecutionCoordinator) -> SubmissionResult:
    return await coordinator.submit(
        _intent(),
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )


@pytest.mark.asyncio
async def test_timeout_adopts_order_found_by_same_client_id(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    broker = ScriptedAmbiguousBroker(
        [SubmissionUncertainError("timeout")],
        [_accepted()],
    )

    result = await _submit(_coordinator(database, broker))

    assert result.recovered_after_ambiguity is True
    assert broker.submitted_client_ids == ["stable-client-id"]
    assert broker.lookup_client_ids == ["stable-client-id"]
    database.close()


@pytest.mark.asyncio
async def test_definitive_not_found_allows_only_bounded_same_id_retry(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    broker = ScriptedAmbiguousBroker(
        [SubmissionUncertainError("timeout"), _accepted()],
        [None],
    )

    result = await _submit(_coordinator(database, broker))

    assert result.not_found_retries == 1
    assert broker.submitted_client_ids == ["stable-client-id", "stable-client-id"]
    assert broker.lookup_client_ids == ["stable-client-id"]
    database.close()


@pytest.mark.asyncio
async def test_retry_rejection_is_looked_up_before_any_rejected_transition(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "quantbot.db")
    broker = ScriptedAmbiguousBroker(
        [
            SubmissionUncertainError("timeout"),
            BrokerRequestError("duplicate client ID", status_code=422),
        ],
        [None, _accepted()],
    )

    result = await _submit(_coordinator(database, broker))

    assert result.intent.state is IntentState.BROKER_ACCEPTED
    assert broker.submitted_client_ids == ["stable-client-id", "stable-client-id"]
    assert broker.lookup_client_ids == ["stable-client-id", "stable-client-id"]
    database.close()


@pytest.mark.asyncio
async def test_initial_definitive_rejection_is_not_retried(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    broker = ScriptedAmbiguousBroker(
        [BrokerRequestError("invalid order", status_code=422)],
        [],
    )

    with pytest.raises(BrokerRequestError, match="invalid order"):
        await _submit(_coordinator(database, broker))

    with database.transaction() as session:
        stored = StorageRepository(session).get_order_intent("intent-1")
        assert stored is not None
        assert stored.state is IntentState.REJECTED
    assert broker.submitted_client_ids == ["stable-client-id"]
    assert broker.lookup_client_ids == []
    database.close()


@pytest.mark.asyncio
async def test_exhausted_ambiguity_remains_submission_unknown(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    broker = ScriptedAmbiguousBroker(
        [SubmissionUncertainError("timeout"), SubmissionUncertainError("timeout again")],
        [None, None],
    )

    with pytest.raises(SubmissionUncertainError, match="remains uncertain"):
        await _submit(_coordinator(database, broker))

    with database.transaction() as session:
        repository = StorageRepository(session)
        stored = repository.get_order_intent("intent-1")
        assert stored is not None
        assert stored.state is IntentState.SUBMISSION_UNKNOWN
        incidents = repository.list_incidents(unresolved_only=True)
        assert [(incident.kind, incident.severity) for incident in incidents] == [
            ("SUBMISSION_UNKNOWN", "ERROR")
        ]
    assert set(broker.submitted_client_ids) == {"stable-client-id"}
    assert set(broker.lookup_client_ids) == {"stable-client-id"}
    database.close()
