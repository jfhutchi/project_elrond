from datetime import UTC, datetime
from decimal import Decimal

from quantbot.domain import (
    Account,
    BrokerOrder,
    Fill,
    OrderSide,
    OrderType,
    Position,
    ReconciliationStatus,
    TimeInForce,
)
from quantbot.execution.reconciliation import (
    ReconciliationPolicy,
    ReconciliationSnapshot,
    compare_reconciliation,
)

NOW = datetime(2026, 8, 14, 14, 30, tzinfo=UTC)


def _account(*, account_id: str = "paper-1", cash: str = "1000.00") -> Account:
    return Account(
        account_id=account_id,
        cash=Decimal(cash),
        buying_power=Decimal("2000.00"),
        equity=Decimal("1500.00"),
        currency="USD",
    )


def _position(quantity: str = "2") -> Position:
    return Position(
        symbol="SPY",
        quantity=Decimal(quantity),
        market_price=Decimal("500"),
        market_value=Decimal("1000"),
        average_entry_price=Decimal("490"),
    )


def _order(quantity: str = "2") -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-1",
        client_order_id="client-1",
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal(quantity),
        filled_quantity=Decimal("0"),
        status="new",
        submitted_at=NOW,
    )


def _fill(price: str = "500") -> Fill:
    return Fill(
        fill_id="fill-1",
        broker_order_id="broker-1",
        symbol="SPY",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=Decimal(price),
        occurred_at=NOW,
    )


def test_currency_differences_within_configured_precision_are_not_material() -> None:
    local = ReconciliationSnapshot(account=_account(cash="1000.00"))
    broker = ReconciliationSnapshot(account=_account(cash="1000.009"))

    result = compare_reconciliation(local, broker, ReconciliationPolicy(currency_places=2))

    assert result.status is ReconciliationStatus.RECONCILED
    assert result.diffs == ()


def test_material_account_difference_is_recorded_and_blocks_orders() -> None:
    local = ReconciliationSnapshot(account=_account(cash="1000.00"))
    broker = ReconciliationSnapshot(account=_account(cash="1000.02"))

    result = compare_reconciliation(local, broker, ReconciliationPolicy(currency_places=2))

    assert result.status is ReconciliationStatus.RECONCILIATION_REQUIRED
    assert result.allows_new_orders is False
    assert [(diff.category, diff.key) for diff in result.diffs] == [("ACCOUNT", "cash")]


def test_one_currency_quantum_is_material_not_silently_tolerated() -> None:
    local = ReconciliationSnapshot(account=_account(cash="1000.00"))
    broker = ReconciliationSnapshot(account=_account(cash="1000.01"))

    result = compare_reconciliation(local, broker, ReconciliationPolicy(currency_places=2))

    assert result.status is ReconciliationStatus.RECONCILIATION_REQUIRED
    assert result.diffs[0].key == "cash"


def test_account_identity_mismatch_has_distinct_fail_closed_status() -> None:
    local = ReconciliationSnapshot(account=_account(account_id="paper-1"))
    broker = ReconciliationSnapshot(account=_account(account_id="paper-2"))

    result = compare_reconciliation(local, broker)

    assert result.status is ReconciliationStatus.ACCOUNT_MISMATCH
    assert result.diffs[0].key == "account_id"


def test_positions_orders_and_fills_compare_by_stable_identity() -> None:
    local = ReconciliationSnapshot(
        account=_account(),
        positions=(_position("2"),),
        open_orders=(_order("2"),),
        fills=(_fill("500"),),
    )
    broker = ReconciliationSnapshot(
        account=_account(),
        positions=(_position("3"),),
        open_orders=(_order("4"),),
        fills=(_fill("501"),),
    )

    result = compare_reconciliation(local, broker)

    assert result.status is ReconciliationStatus.RECONCILIATION_REQUIRED
    assert {(diff.category, diff.key) for diff in result.diffs} == {
        ("POSITION", "SPY"),
        ("OPEN_ORDER", "client-1"),
        ("FILL", "fill-1"),
    }


def test_missing_broker_observations_are_material() -> None:
    local = ReconciliationSnapshot(
        account=_account(),
        positions=(_position(),),
        open_orders=(_order(),),
        fills=(_fill(),),
    )

    result = compare_reconciliation(local, ReconciliationSnapshot(account=_account()))

    assert result.status is ReconciliationStatus.RECONCILIATION_REQUIRED
    assert len(result.diffs) == 3
    assert all(diff.actual == {"present": "false"} for diff in result.diffs)
