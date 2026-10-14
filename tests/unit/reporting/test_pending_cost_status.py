"""Unknown fees leave quantity/gross facts available without verified net."""

from datetime import UTC, datetime, timedelta

from quantbot.domain import Fill, OrderSide


def test_gross_cycle_remains_visible_with_unknown_fee():
    from quantbot.reporting.performance import analyze_gross_cycles

    now = datetime(2026, 10, 14, 14, tzinfo=UTC)
    entry = Fill(
        fill_id="entry",
        broker_order_id="o1",
        symbol="XLE",
        side=OrderSide.BUY,
        quantity=5,
        price=100,
        occurred_at=now,
    )
    exit_fill = Fill(
        fill_id="exit",
        broker_order_id="o2",
        symbol="XLE",
        side=OrderSide.SELL,
        quantity=5,
        price=102,
        occurred_at=now + timedelta(minutes=30),
        fee=0,
    )
    (cycle,) = analyze_gross_cycles((entry, exit_fill))
    assert cycle.gross_pnl == 10
    assert cycle.net_pnl is None
    assert cycle.costs is None
