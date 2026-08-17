"""The system must survive its own first fill.

Broker orders were stored once at acknowledgement and never updated, so the moment an order
filled the broker stopped listing it as open while local state still called it `accepted`.
Reconciliation then reported an OPEN_ORDER mismatch on every subsequent cycle, halting the
system permanently and engaging the kill switch. These tests pin the transition.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.domain import (
    Account,
    BrokerOrder,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    TimeInForce,
)
from quantbot.execution.recovery import is_open_order
from quantbot.storage import (
    Database,
    RecordNotFoundError,
    StateConflictError,
    StorageRepository,
)

NOW = datetime(2026, 8, 17, 13, 37, tzinfo=UTC)


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="qb-lifecycle-1",
        strategy_id="adaptive-momentum-v1-test",
        signal_date=date(2026, 8, 14),
        symbol="XLE",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("0.161511"),
        created_at=NOW,
    )


def _order(status: str, filled: str = "0", price: str | None = None) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="broker-1",
        client_order_id="qb-lifecycle-1",
        symbol="XLE",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("0.161511"),
        filled_quantity=Decimal(filled),
        status=status,
        submitted_at=NOW,
        filled_average_price=Decimal(price) if price is not None else None,
    )


def _seed(database: Database) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(_intent())
        repository.transition_order_intent(
            "intent-1", IntentState.ORDER_CREATED, expected_current=IntentState.RISK_APPROVED
        )
        repository.save_broker_order(_order("accepted"))


def test_accepted_order_is_open_and_filled_order_is_not() -> None:
    assert is_open_order(_order("accepted")) is True
    assert is_open_order(_order("filled", "0.161511", "61.92")) is False
    assert is_open_order(_order("canceled")) is False


def test_fill_transition_is_recorded_so_local_open_orders_match_the_broker(
    tmp_path: Path,
) -> None:
    """The regression: without this, local keeps one open order the broker does not have."""
    database = Database(tmp_path / "quantbot.db")
    _seed(database)

    with database.transaction() as session:
        repository = StorageRepository(session)
        before = [o for o in repository.list_broker_orders() if is_open_order(o)]
    assert len(before) == 1, "an accepted order should count as open"

    with database.transaction() as session:
        repository = StorageRepository(session)
        changed = repository.update_broker_order_lifecycle(
            _order("filled", "0.161511", "61.92")
        )
    assert changed is True

    with database.transaction() as session:
        repository = StorageRepository(session)
        after = [o for o in repository.list_broker_orders() if is_open_order(o)]
        stored = repository.get_broker_order("broker-1")
    assert after == [], "a filled order must no longer count as a local open order"
    assert stored is not None
    assert stored.status == "filled"
    assert stored.filled_quantity == Decimal("0.161511")
    assert stored.filled_average_price == Decimal("61.92")
    database.close()


def test_repeating_the_same_transition_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    _seed(database)
    filled = _order("filled", "0.161511", "61.92")

    with database.transaction() as session:
        assert StorageRepository(session).update_broker_order_lifecycle(filled) is True
    with database.transaction() as session:
        assert StorageRepository(session).update_broker_order_lifecycle(filled) is False
    database.close()


def test_identity_change_is_rejected_as_a_conflict(tmp_path: Path) -> None:
    """Lifecycle fields may advance; identity may never change."""
    database = Database(tmp_path / "quantbot.db")
    _seed(database)
    tampered = _order("filled", "0.161511", "61.92").model_copy(
        update={"quantity": Decimal("99")}
    )

    with pytest.raises(StateConflictError, match="identity conflicts"):
        with database.transaction() as session:
            StorageRepository(session).update_broker_order_lifecycle(tampered)
    database.close()


def test_updating_an_unknown_order_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")

    with pytest.raises(RecordNotFoundError, match="broker order not found"):
        with database.transaction() as session:
            StorageRepository(session).update_broker_order_lifecycle(_order("filled"))
    database.close()


def test_reconciliation_snapshots_agree_once_the_transition_is_recorded(
    tmp_path: Path,
) -> None:
    """End to end: the comparison that used to produce a permanent halt now reconciles."""
    from quantbot.domain import ReconciliationStatus
    from quantbot.execution import ReconciliationSnapshot, compare_reconciliation

    database = Database(tmp_path / "quantbot.db")
    _seed(database)
    account = Account(
        account_id="paper-1",
        cash=Decimal("90.10"),
        buying_power=Decimal("90.10"),
        equity=Decimal("100"),
        currency="USD",
    )
    position = Position(
        symbol="XLE",
        quantity=Decimal("0.161511"),
        market_price=Decimal("61.92"),
        market_value=Decimal("10.00"),
        average_entry_price=Decimal("61.92"),
    )
    # The broker, post-fill: no open orders, one position.
    broker_view = ReconciliationSnapshot(account=account, positions=(position,), open_orders=())

    with database.transaction() as session:
        repository = StorageRepository(session)
        stale_local = ReconciliationSnapshot(
            account=account,
            positions=(position,),
            open_orders=tuple(
                o for o in repository.list_broker_orders() if is_open_order(o)
            ),
        )
    assert compare_reconciliation(stale_local, broker_view).status is (
        ReconciliationStatus.RECONCILIATION_REQUIRED
    ), "the pre-fix behaviour must genuinely have halted"

    with database.transaction() as session:
        StorageRepository(session).update_broker_order_lifecycle(
            _order("filled", "0.161511", "61.92")
        )
    with database.transaction() as session:
        repository = StorageRepository(session)
        fixed_local = ReconciliationSnapshot(
            account=account,
            positions=(position,),
            open_orders=tuple(
                o for o in repository.list_broker_orders() if is_open_order(o)
            ),
        )
    result = compare_reconciliation(fixed_local, broker_view)
    assert result.status is ReconciliationStatus.RECONCILED
    assert result.allows_new_orders is True
    database.close()
