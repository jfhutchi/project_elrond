from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from quantbot.brokers import BrokerAccountSnapshot, BrokerAdapter, BrokerHealth
from quantbot.config import Settings
from quantbot.domain import (
    Account,
    BrokerOrder,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    ReconciliationStatus,
    TimeInForce,
)
from quantbot.execution import StartupRecovery
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)


def _account(*, account_id: str = "paper-1", cash: str = "10000") -> Account:
    return Account(
        account_id=account_id,
        cash=Decimal(cash),
        buying_power=Decimal("20000"),
        equity=Decimal("10000"),
        currency="USD",
    )


def _settings(**changes: bool) -> Settings:
    base = Settings(
        EXPECTED_ACCOUNT_ID="paper-1",
        ALPACA_PAPER_API_KEY="paper-key",
        ALPACA_PAPER_API_SECRET="paper-secret",
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=False,
    )
    return base.model_copy(update=changes)


class RecoveryBroker:
    def __init__(
        self,
        *,
        account: Account,
        healthy: bool = True,
        positions: tuple[Position, ...] = (),
        open_orders: tuple[BrokerOrder, ...] = (),
        lookup: BrokerOrder | None = None,
    ) -> None:
        self.account = account
        self.healthy = healthy
        self.positions = positions
        self.open_orders = open_orders
        self.lookup = lookup
        self.mutations: list[str] = []

    async def connect(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=self.healthy,
            provider="alpaca",
            environment="PAPER",
            account_id=self.account.account_id,
            reasons=() if self.healthy else ("STALE_BROKER",),
        )

    async def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            account=self.account,
            account_number=self.account.account_id,
            status="ACTIVE",
            order_entry_allowed=True,
            restrictions=(),
        )

    async def get_positions(self) -> tuple[Position, ...]:
        return self.positions

    async def get_open_orders(self) -> tuple[BrokerOrder, ...]:
        return self.open_orders

    async def get_fills(self, *, after: datetime | None = None) -> tuple[()]:
        return ()

    async def get_order_by_client_id(
        self,
        client_order_id: str,
        *,
        expected: OrderIntent | None = None,
    ) -> BrokerOrder | None:
        return self.lookup


def _prepare_database(
    tmp_path: Path,
    account: Account,
    positions: tuple[Position, ...] = (),
) -> Database:
    database = Database(tmp_path / "quantbot.db")
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.set_kill_switch(False, reason="test ready", updated_at=NOW)
        repository.save_account_snapshot(
            "local-1",
            account,
            positions,
            captured_at=NOW,
        )
    return database


@pytest.mark.asyncio
async def test_material_reconciliation_diff_is_durable_and_halts_startup(
    tmp_path: Path,
) -> None:
    database = _prepare_database(tmp_path, _account())
    broker = RecoveryBroker(account=_account(cash="9990"))
    recovery = StartupRecovery(database, cast(BrokerAdapter, broker), _settings())

    result = await recovery.recover("recon-1", started_at=NOW)

    assert result.ready is False
    assert result.reconciliation.status is ReconciliationStatus.RECONCILIATION_REQUIRED
    assert "RECONCILIATION_REQUIRED" in result.reasons
    with database.transaction() as session:
        stored = StorageRepository(session).get_reconciliation("recon-1")
        assert stored is not None
        assert stored.diffs[0].key == "cash"
    assert broker.mutations == []
    database.close()


@pytest.mark.asyncio
async def test_account_mismatch_stale_health_and_kill_switch_all_fail_closed(
    tmp_path: Path,
) -> None:
    database = _prepare_database(tmp_path, _account())
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(True, reason="operator", updated_at=NOW)
    broker = RecoveryBroker(account=_account(account_id="other"), healthy=False)
    recovery = StartupRecovery(
        database,
        cast(BrokerAdapter, broker),
        _settings(MARKET_DATA_HEALTHY=False),
    )

    result = await recovery.recover("recon-2", started_at=NOW)

    assert result.ready is False
    assert result.reconciliation.status is ReconciliationStatus.ACCOUNT_MISMATCH
    assert {
        "ACCOUNT_MISMATCH",
        "BROKER_UNHEALTHY",
        "MARKET_DATA_UNHEALTHY",
        "KILL_SWITCH_ENGAGED",
    } <= set(result.reasons)
    database.close()


@pytest.mark.asyncio
async def test_restart_adopts_partial_fill_without_resubmitting(tmp_path: Path) -> None:
    position = Position(
        symbol="SPY",
        quantity=Decimal("2"),
        market_price=Decimal("500"),
        market_value=Decimal("1000"),
        average_entry_price=Decimal("500"),
    )
    database = _prepare_database(tmp_path, _account(), (position,))
    intent = OrderIntent(
        intent_id="intent-1",
        client_order_id="client-1",
        strategy_id="full-v1:test",
        symbol="SPY",
        signal_date=date(2026, 8, 14),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("5"),
        created_at=NOW,
    )
    partial = BrokerOrder(
        broker_order_id="broker-1",
        client_order_id="client-1",
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("5"),
        filled_quantity=Decimal("2"),
        status="partially_filled",
        submitted_at=NOW,
        filled_average_price=Decimal("500"),
    )
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(intent)
        repository.transition_order_intent(
            intent.intent_id,
            IntentState.ORDER_CREATED,
            expected_current=IntentState.RISK_APPROVED,
        )
        repository.transition_order_intent(
            intent.intent_id,
            IntentState.SUBMITTING,
            expected_current=IntentState.ORDER_CREATED,
        )
    broker = RecoveryBroker(
        account=_account(),
        positions=(position,),
        open_orders=(partial,),
        lookup=partial,
    )
    recovery = StartupRecovery(database, cast(BrokerAdapter, broker), _settings())

    result = await recovery.recover("recon-3", started_at=NOW)

    assert result.ready is True
    assert result.resolved_intent_ids == ("intent-1",)
    with database.transaction() as session:
        stored = StorageRepository(session).get_order_intent("intent-1")
        assert stored is not None
        assert stored.state is IntentState.PARTIALLY_FILLED
    assert broker.mutations == []
    database.close()
