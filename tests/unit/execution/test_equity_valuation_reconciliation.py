from decimal import Decimal

import pytest

from quantbot.domain import Account, Position, ReconciliationStatus
from quantbot.execution.reconciliation import ReconciliationSnapshot, compare_reconciliation
from tests.unit.execution.test_reconciliation import _fill, _order


def _snapshot(
    equity: str = "494.18", value: str = "492.133166", *, quantity: str = "10"
) -> ReconciliationSnapshot:
    amount = Decimal(value)
    qty = Decimal(quantity)
    return ReconciliationSnapshot(
        account=Account(
            account_id="paper-1",
            cash=Decimal("2.05"),
            buying_power=Decimal("2.05"),
            equity=Decimal(equity),
            currency="USD",
        ),
        positions=(
            Position(
                symbol="SPY",
                quantity=qty,
                market_price=amount / qty,
                market_value=amount,
                average_entry_price=Decimal("49"),
            ),
        ),
    )


@pytest.mark.parametrize("change", ["0.01", "-0.01", "0.20", "-100"])
def test_explained_price_change_is_not_a_ledger_failure(change: str) -> None:
    delta = Decimal(change)
    local = _snapshot()
    actual = _snapshot(str(local.account.equity + delta), str(Decimal("492.133166") + delta))

    result = compare_reconciliation(local, actual)

    assert result.status is ReconciliationStatus.RECONCILED
    assert result.allows_new_orders
    assert result.diffs == ()
    assert len(result.valuation_changes) == 1
    evidence = result.valuation_changes[0]
    assert evidence.key == "equity"
    assert Decimal(evidence.expected["value"]) == local.account.equity
    assert Decimal(evidence.actual["value"]) == actual.account.equity
    assert Decimal(evidence.actual["unexplained_delta"]) == 0


def test_short_position_uses_signed_market_value() -> None:
    local = _snapshot("1000", "-500", quantity="-10")
    local = local.model_copy(
        update={"account": local.account.model_copy(update={"cash": Decimal("1500")})}
    )
    actual = _snapshot("999.99", "-500.01", quantity="-10")
    actual = actual.model_copy(update={"account": local.account})
    actual = actual.model_copy(
        update={"account": actual.account.model_copy(update={"equity": Decimal("999.99")})}
    )
    assert compare_reconciliation(local, actual).allows_new_orders


@pytest.mark.parametrize(
    "field,value",
    [
        ("cash", Decimal("2.06")),
        ("buying_power", Decimal("2.06")),
        ("account_id", "other"),
        ("currency", "EUR"),
    ],
)
def test_explained_equity_does_not_hide_another_account_difference(
    field: str, value: object
) -> None:
    actual = _snapshot("494.19", "492.143166")
    actual = actual.model_copy(update={"account": actual.account.model_copy(update={field: value})})
    result = compare_reconciliation(_snapshot(), actual)
    assert not result.allows_new_orders
    assert any(diff.key == field for diff in result.diffs)
    assert result.valuation_changes == ()


@pytest.mark.parametrize(
    "case",
    [
        "unchanged_marks",
        "inconsistent_equity",
        "quantity",
        "entry_price",
        "missing_positions",
        "wrong_sign",
        "zero_quantity",
        "price_value_disagree",
        "residual_delta",
        "rounding_boundary",
    ],
)
def test_unexplained_or_unusable_valuation_evidence_stays_blocked(case: str) -> None:
    local = _snapshot()
    actual = _snapshot("494.19", "492.143166")
    position = actual.positions[0]
    if case == "unchanged_marks":
        actual = actual.model_copy(update={"positions": local.positions})
    elif case == "inconsistent_equity":
        local = local.model_copy(
            update={"account": local.account.model_copy(update={"equity": Decimal("504.18")})}
        )
        actual = actual.model_copy(
            update={"account": actual.account.model_copy(update={"equity": Decimal("504.19")})}
        )
    elif case == "quantity":
        actual = actual.model_copy(
            update={"positions": (position.model_copy(update={"quantity": Decimal("11")}),)}
        )
    elif case == "entry_price":
        actual = actual.model_copy(
            update={
                "positions": (position.model_copy(update={"average_entry_price": Decimal("50")}),)
            }
        )
    elif case == "missing_positions":
        local = local.model_copy(update={"positions": ()})
        actual = actual.model_copy(update={"positions": ()})
    elif case in {"wrong_sign", "zero_quantity", "price_value_disagree"}:
        field, value = {
            "wrong_sign": ("market_value", Decimal("-492.143166")),
            "zero_quantity": ("quantity", Decimal("0")),
            "price_value_disagree": ("market_price", Decimal("1")),
        }[case]
        actual = actual.model_copy(
            update={"positions": (position.model_copy(update={field: value}),)}
        )
    elif case == "residual_delta":
        local = _snapshot("494.18", "492.139")  # residual -0.009
        actual = _snapshot("494.20", "492.141")  # residual +0.009
    elif case == "rounding_boundary":
        actual = _snapshot("494.20", "492.14")  # residual exactly 0.01
    result = compare_reconciliation(local, actual)
    assert not result.allows_new_orders
    assert result.valuation_changes == ()
    assert any(diff.key == "equity" for diff in result.diffs)


@pytest.mark.parametrize("field,items", [("open_orders", (_order(),)), ("fills", (_fill(),))])
def test_explained_equity_does_not_hide_missing_orders_or_fills(
    field: str, items: tuple[object, ...]
) -> None:
    local = _snapshot().model_copy(update={field: items})
    actual = _snapshot("494.19", "492.143166")
    result = compare_reconciliation(local, actual)
    assert not result.allows_new_orders
    assert result.valuation_changes == ()
    category = "FILL" if field == "fills" else "OPEN_ORDER"
    assert any(diff.category == category for diff in result.diffs)
