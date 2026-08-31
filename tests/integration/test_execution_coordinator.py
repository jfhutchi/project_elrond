from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from quantbot.brokers import BrokerAccountSnapshot, BrokerAdapter
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
from quantbot.execution import ExecutionCoordinator, OrderSubmissionBlocked
from quantbot.risk import (
    DrawdownState,
    OrderPurpose,
    PortfolioVetoDecision,
    PortfolioVetoPolicy,
)
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)


def clean_vetoes(symbol: str = "SPY") -> PortfolioVetoDecision:
    """A decision where every enabled veto ran and none refused.

    Supplied only on the ENTRY call. The exit and liquidation calls below deliberately pass
    nothing: a veto must not apply to them, and if one ever did these tests would start failing
    for the right reason.
    """
    policy = PortfolioVetoPolicy()
    return PortfolioVetoDecision(
        symbol=symbol,
        policy_version=policy.version,
        rule_parameters_hash=policy.identity,
        evaluated=tuple(sorted(policy.enabled, key=lambda item: item.value)),
        rejections=(),
    )


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


def _account_snapshot() -> BrokerAccountSnapshot:
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


def _long_position(quantity: Decimal = Decimal("2")) -> Position:
    return Position(
        symbol="SPY",
        quantity=quantity,
        market_price=Decimal("500"),
        market_value=quantity * Decimal("500"),
        average_entry_price=Decimal("450"),
    )


def _intent(*, side: OrderSide = OrderSide.BUY) -> OrderIntent:
    return OrderIntent(
        intent_id=f"intent-{side.value.lower()}",
        client_order_id=f"client-{side.value.lower()}",
        strategy_id="full-v1:test",
        symbol="SPY",
        signal_date=date(2026, 8, 14),
        side=side,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        created_at=NOW,
    )


def _accepted(intent: OrderIntent) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=f"broker-{intent.side.value.lower()}",
        client_order_id=intent.client_order_id,
        symbol=intent.symbol,
        side=intent.side,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        quantity=intent.quantity,
        filled_quantity=Decimal("0"),
        status="accepted",
        submitted_at=NOW,
    )


class AcceptingBroker:
    def __init__(
        self,
        database: Database,
        account_snapshot: BrokerAccountSnapshot | None = None,
        *,
        positions: tuple[Position, ...] | None = None,
        position_snapshots: list[tuple[Position, ...]] | None = None,
    ) -> None:
        self.database = database
        self.account_snapshot = account_snapshot or _account_snapshot()
        self.positions = (_long_position(),) if positions is None else positions
        self.position_snapshots = position_snapshots or []
        self.submitted: list[OrderIntent] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        return self.account_snapshot

    async def get_positions(self) -> tuple[Position, ...]:
        if self.position_snapshots:
            return self.position_snapshots.pop(0)
        return self.positions

    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        with self.database.transaction() as session:
            stored = StorageRepository(session).get_order_intent(intent.intent_id)
            assert stored is not None
            assert stored.state is IntentState.SUBMITTING
        self.submitted.append(intent)
        return _accepted(intent)

    async def get_order_by_client_id(
        self,
        client_order_id: str,
        *,
        expected: OrderIntent | None = None,
    ) -> BrokerOrder | None:
        raise AssertionError("unambiguous submission must not perform lookup")


def _drawdown() -> DrawdownState:
    return DrawdownState(
        current_equity=Decimal("10000"),
        high_water_equity=Decimal("10000"),
        drawdown_fraction=Decimal("0"),
        new_risk_multiplier=Decimal("1"),
        entry_halted=False,
        liquidation_required=False,
    )


def _mark_ready(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.set_kill_switch(False, reason="test ready", updated_at=NOW)
        repository.save_reconciliation(
            "ready-reconciliation",
            status=ReconciliationStatus.RECONCILED,
            started_at=NOW,
            completed_at=NOW,
            diffs=(),
        )


@pytest.mark.asyncio
async def test_intent_is_durable_before_single_broker_submission(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    _mark_ready(database)
    broker = AcceptingBroker(database)
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())
    intent = _intent()

    result = await coordinator.submit(
        intent,
        purpose=OrderPurpose.ENTRY,
        vetoes=clean_vetoes(),
        market_eligible=True,
        drawdown=_drawdown(),
        risk_approved=True,
    )

    assert broker.submitted == [intent]
    assert result.intent.state is IntentState.BROKER_ACCEPTED
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.get_order_intent(intent.intent_id) == result.intent
        assert repository.get_broker_order(result.broker_order.broker_order_id) is not None
    database.close()


@pytest.mark.asyncio
async def test_exit_bypasses_entry_risk_but_not_operational_safety(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    _mark_ready(database)
    broker = AcceptingBroker(database)
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())

    result = await coordinator.submit(
        _intent(side=OrderSide.SELL),
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )

    assert result.intent.state is IntentState.BROKER_ACCEPTED
    database.close()


@pytest.mark.asyncio
async def test_fresh_position_proof_allows_exit_without_cached_reconciliation(
    tmp_path: Path,
) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(False, reason="test ready", updated_at=NOW)
    broker = AcceptingBroker(database)
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())

    result = await coordinator.submit(
        _intent(side=OrderSide.SELL),
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )

    assert result.intent.state is IntentState.BROKER_ACCEPTED
    assert len(broker.submitted) == 1
    database.close()

@pytest.mark.asyncio
async def test_fresh_position_proof_allows_exit_after_unrelated_material_diff(
    tmp_path: Path,
) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    _mark_ready(database)
    with database.transaction() as session:
        StorageRepository(session).save_reconciliation(
            "material-reconciliation",
            status=ReconciliationStatus.RECONCILIATION_REQUIRED,
            started_at=NOW + timedelta(seconds=1),
            completed_at=NOW + timedelta(seconds=1),
            diffs=(
                {
                    "diff_id": "cash-diff",
                    "category": "ACCOUNT",
                    "key": "cash",
                    "expected": {"value": "10000"},
                    "actual": {"value": "9990"},
                },
            ),
        )
    broker = AcceptingBroker(database)
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())

    result = await coordinator.submit(
        _intent(side=OrderSide.SELL),
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )

    assert result.intent.state is IntentState.BROKER_ACCEPTED
    assert len(broker.submitted) == 1
    database.close()

@pytest.mark.asyncio
async def test_exit_requires_a_fresh_matching_broker_position(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    _mark_ready(database)
    broker = AcceptingBroker(database, positions=())
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())

    with pytest.raises(OrderSubmissionBlocked) as error:
        await coordinator.submit(
            _intent(side=OrderSide.SELL),
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )

    assert error.value.decision.reasons == ("EXIT_POSITION_NOT_FOUND",)
    assert broker.submitted == []
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "positions", "reason"),
    [
        (
            _intent(side=OrderSide.BUY),
            (_long_position(),),
            "EXIT_SIDE_INCREASES_RISK",
        ),
        (
            _intent(side=OrderSide.SELL),
            (_long_position(Decimal("1")),),
            "EXIT_QUANTITY_EXCEEDS_POSITION",
        ),
    ],
)
async def test_exit_purpose_cannot_disguise_an_order_that_increases_risk(
    tmp_path: Path,
    intent: OrderIntent,
    positions: tuple[Position, ...],
    reason: str,
) -> None:
    database = Database.create(tmp_path / f"{reason}.db")
    _mark_ready(database)
    broker = AcceptingBroker(database, positions=positions)
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())

    with pytest.raises(OrderSubmissionBlocked) as error:
        await coordinator.submit(
            intent,
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )

    assert reason in error.value.decision.reasons
    assert broker.submitted == []
    database.close()


@pytest.mark.asyncio
async def test_exit_rechecks_live_position_immediately_before_submission(
    tmp_path: Path,
) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    _mark_ready(database)
    broker = AcceptingBroker(
        database,
        position_snapshots=[(_long_position(),), ()],
    )
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())
    intent = _intent(side=OrderSide.SELL)

    with pytest.raises(OrderSubmissionBlocked) as error:
        await coordinator.submit(
            intent,
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )

    assert error.value.decision.reasons == ("EXIT_POSITION_NOT_FOUND",)
    assert broker.submitted == []
    with database.transaction() as session:
        stored = StorageRepository(session).get_order_intent(intent.intent_id)
    assert stored is not None
    assert stored.state is IntentState.ERROR
    database.close()


@pytest.mark.asyncio
async def test_broker_account_restrictions_block_exit_submission(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "quantbot.db")
    _mark_ready(database)
    restricted = _account_snapshot().model_copy(
        update={"order_entry_allowed": False, "restrictions": ("trading_blocked",)}
    )
    broker = AcceptingBroker(database, restricted)
    coordinator = ExecutionCoordinator(database, cast(BrokerAdapter, broker), _settings())

    with pytest.raises(OrderSubmissionBlocked) as error:
        await coordinator.submit(
            _intent(side=OrderSide.SELL),
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )

    assert {
        "BROKER_ORDER_ENTRY_DISABLED",
        "BROKER_ACCOUNT_RESTRICTED",
    } <= set(error.value.decision.reasons)
    assert broker.submitted == []
    database.close()
