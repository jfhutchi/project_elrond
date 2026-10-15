"""Readiness permits market marks, never changes to spending or ownership."""

from decimal import Decimal

import pytest

from quantbot.brokers.base import BrokerAccountSnapshot
from quantbot.domain import Account, Position
from quantbot.paper_core.funds import stable_account_reads, stable_position_reads


@pytest.mark.parametrize(
    "field,value,stable",
    [
        ("equity", Decimal("99999.99"), True),
        ("cash", Decimal("99999.99"), False),
        ("buying_power", Decimal("99999.99"), False),
        ("account_id", "other", False),
        ("currency", "GBP", False),
    ],
)
def test_account_read_stability(field, value, stable):
    account = Account(
        account_id="paper", cash=100000, equity=100000, buying_power=100000, currency="USD"
    )
    first = BrokerAccountSnapshot(
        account=account,
        account_number="offline",
        status="ACTIVE",
        order_entry_allowed=True,
        restrictions=(),
    )
    second = first.model_copy(update={"account": account.model_copy(update={field: value})})
    assert stable_account_reads(first, second) is stable
    assert not stable_account_reads(first, second.model_copy(update={"restrictions": ("blocked",)}))
    assert not stable_account_reads(first, second.model_copy(update={"order_entry_allowed": False}))


@pytest.mark.parametrize(
    "field,value,stable",
    [
        ("market_price", Decimal("101"), True),
        ("market_value", Decimal("101"), True),
        ("quantity", Decimal("2"), False),
        ("average_entry_price", Decimal("101"), False),
        ("symbol", "MSFT", False),
    ],
)
def test_position_read_stability(field, value, stable):
    first = Position(
        symbol="AAPL", quantity=1, market_price=100, market_value=100, average_entry_price=100
    )
    second = first.model_copy(update={field: value})
    assert stable_position_reads((first,), (second,)) is stable
    assert not stable_position_reads((first,), ())
    assert not stable_position_reads((first,), (second, second))
