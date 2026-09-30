"""Frozen intraday candidate: causal fills and portfolio accounting."""

from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, localcontext

import pytest

from quantbot.domain import Bar
from quantbot.research.intraday import UNIVERSE, simulate
from quantbot.strategy.adaptive_momentum import XNYSSession

D = Decimal


def session(day: int = 10, *, early: bool = False) -> XNYSSession:
    return XNYSSession(
        session_date=date(2026, 9, day),
        open_at=datetime(2026, 9, day, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 9, day, 17 if early else 20, tzinfo=UTC),
    )


def bars(s: XNYSSession, symbol: str = "SPY", *, breakout: int = 2) -> list[Bar]:
    count = int((s.close_at - s.open_at).total_seconds() // 900)
    return [
        Bar(
            symbol=symbol,
            timestamp=s.open_at + timedelta(minutes=15 * i),
            open=D("100") if i <= breakout else D("102"),
            high=D("101") if i < breakout else D("103"),
            low=D("99") if i < breakout else D("101") if i > breakout else D("100"),
            close=D("100") if i < breakout else D("102"),
            volume=D("1000"),
            adjustment=D("1"),
        )
        for i in range(count)
    ]


def replace_bar(items: list[Bar], index: int, **prices: str) -> None:
    data = items[index].model_dump()
    data.update({name: D(value) for name, value in prices.items()})
    items[index] = Bar.model_validate(data)


def test_completed_signal_fills_next_open_and_exits_after_90_minutes() -> None:
    s = session()
    history = bars(s)
    replace_bar(history, 3, open="103", high="104")
    result = simulate({"SPY": history}, [s])
    trade = result.trades[0]
    assert trade.entry_time == s.open_at + timedelta(minutes=45)
    assert trade.entry_price == D("103.0515")
    assert trade.exit_time == s.open_at + timedelta(minutes=135)
    assert trade.exit_price == D("101.9490")
    assert trade.reason == "MAX_HOLD"
    assert result.stats.trade_count == 1
    assert result.status == "EXPLORATORY_ONLY"
    assert result.qualified is False


@pytest.mark.parametrize("gap, expected, minute", [(True, "96.9515", 60), (False, "98.9505", 75)])
def test_stop_fills_at_worse_gap_open_or_stop(gap: bool, expected: str, minute: int) -> None:
    s = session()
    history = bars(s)
    replace_bar(history, 4, open="97" if gap else "102", low="96", close="100")
    trade = simulate({"SPY": history}, [s]).trades[0]
    assert trade.reason == "STOP"
    assert trade.exit_price == D(expected)
    assert trade.exit_time == s.open_at + timedelta(minutes=minute)


def test_entry_bar_stop_is_counted_and_no_second_entry() -> None:
    s = session()
    history = bars(s)
    replace_bar(history, 3, low="98")
    result = simulate({"SPY": history}, [s])
    assert len(result.trades) == 1
    assert result.trades[0].reason == "STOP"
    assert result.trades[0].exit_price == D("98.9505")


def test_early_close_forces_exit_final_bar_open_without_overnight() -> None:
    s = session(early=True)
    trade = simulate({"SPY": bars(s, breakout=10)}, [s]).trades[0]
    assert trade.entry_time == s.open_at + timedelta(minutes=165)
    assert trade.exit_time == s.close_at - timedelta(minutes=15)
    assert trade.reason == "SESSION_EXIT"


def test_missing_bar_excludes_entire_symbol_day_and_discloses_reason() -> None:
    s = session()
    history = bars(s)
    del history[20]
    result = simulate({"SPY": history, "QQQ": bars(s, "QQQ")}, [s])
    assert [t.symbol for t in result.trades] == ["QQQ"]
    assert any(
        x.symbol == "SPY" and x.reason == "INCOMPLETE_SESSION" for x in result.skipped_symbol_days
    )
    assert result.sessions[0].excluded_symbols == tuple(x for x in UNIVERSE if x != "QQQ")


def test_costs_cash_risk_allocation_and_fractional_precision() -> None:
    s = session()
    histories = {symbol: bars(s, symbol) for symbol in UNIVERSE}
    result = simulate(histories, [s])
    assert len(result.trades) == 10
    assert sum(t.entry_price * t.quantity for t in result.trades) <= D("450")
    for trade in result.trades:
        assert trade.entry_price * trade.quantity <= D("45")
        assert (trade.entry_price - D("98.9505")) * trade.quantity <= D("2.5")
        assert trade.quantity.as_tuple().exponent == -6
        assert trade.net_pnl == (trade.exit_price - trade.entry_price) * trade.quantity
    pnl = sum(t.net_pnl for t in result.trades)
    assert result.sessions[0].equity == D("500") + pnl
    assert result.stats.net_return == pnl / D("500")
    stress = simulate(histories, [s], slippage_bps=10)
    assert stress.stats.net_return < result.stats.net_return < 0
    assert stress.input_hash != result.input_hash


def test_future_sessions_cannot_change_prior_trades_or_equity() -> None:
    first, second = session(), session(11)
    prefix = simulate({"SPY": bars(first)}, [first])
    future = bars(second)
    replace_bar(future, 9, open="1000", high="1001", close="1000")
    full = simulate({"SPY": bars(first) + future}, [first, second])
    assert full.trades[0] == prefix.trades[0]
    assert full.sessions[0] == prefix.sessions[0]
    assert all(t.entry_time.date() == t.exit_time.date() for t in full.trades)


def test_same_bar_future_profit_cannot_fund_other_symbol_entry() -> None:
    s = session()
    normal = {symbol: bars(s, symbol) for symbol in UNIVERSE}
    changed = {symbol: list(values) for symbol, values in normal.items()}
    replace_bar(changed["SPY"], 3, high="10000", close="10000")
    base = simulate(normal, [s])
    full = simulate(changed, [s])
    assert [(t.symbol, t.quantity) for t in base.trades] == [
        (t.symbol, t.quantity) for t in full.trades
    ]


def test_inputs_are_canonical_hashed_and_invalid_inputs_rejected() -> None:
    s = session()
    history = bars(s)
    a = simulate({"SPY": history, "QQQ": bars(s, "QQQ")}, [s])
    b = simulate({"QQQ": bars(s, "QQQ"), "SPY": history}, [s])
    assert a == b
    with pytest.raises(ValueError, match="duplicate|increasing"):
        simulate({"SPY": history + [history[-1]]}, [s])
    with pytest.raises(ValueError, match="universe"):
        simulate({"AAPL": bars(s, "AAPL")}, [s])
    with pytest.raises(ValueError, match="symbol"):
        simulate({"SPY": bars(s, "QQQ")}, [s])
    invalid = history[0].model_copy(update={"low": D("500")})
    with pytest.raises(ValueError):
        simulate({"SPY": [invalid, *history[1:]]}, [s])
    with pytest.raises(ValueError, match="raw"):
        simulate({"SPY": [history[0].model_copy(update={"adjustment": D("2")}), *history[1:]]}, [s])
    with pytest.raises(ValueError):
        simulate({"SPY": history}, [s, s])
    with pytest.raises(ValueError):
        simulate({"SPY": history}, [s], starting_cash=D("NaN"))


def test_ambient_decimal_context_cannot_change_simulation_with_same_hash() -> None:
    first, second = session(), session(11)
    histories = {"SPY": bars(first) + bars(second)}
    expected = simulate(histories, [first, second])
    with localcontext() as context:
        context.prec = 9
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        actual = simulate(histories, [first, second])
    assert actual == expected


def test_wide_stop_risk_caps_quantity_below_allocation_limit() -> None:
    s = session()
    history = bars(s)
    replace_bar(history, 0, low="50")
    trade = simulate({"SPY": history}, [s]).trades[0]
    assert trade.quantity < D("0.05")
    initial_risk_per_share = D("102.051") - D("49.975")
    assert trade.quantity * initial_risk_per_share <= D("2.5")
    assert (trade.quantity + D("0.000001")) * initial_risk_per_share > D("2.5")


@pytest.mark.parametrize("breakout, count", [(19, 1), (20, 0)])
def test_last_entry_cutoff_is_1430_new_york(breakout: int, count: int) -> None:
    s = session()
    result = simulate({"SPY": bars(s, breakout=breakout)}, [s])
    assert len(result.trades) == count
    if count:
        assert result.trades[0].entry_time == datetime(2026, 9, 10, 18, 30, tzinfo=UTC)


def test_gap_below_initial_stop_skips_unviable_entry() -> None:
    s = session()
    history = bars(s)
    replace_bar(history, 3, open="98", low="97")
    result = simulate({"SPY": history}, [s])
    assert result.trades == ()
    assert any(
        x.symbol == "SPY" and x.reason == "ENTRY_AT_OR_BELOW_STOP"
        for x in result.skipped_symbol_days
    )


def test_drawdown_includes_final_realized_transaction_costs() -> None:
    s = session()
    result = simulate({"SPY": bars(s)}, [s])
    assert result.stats.winning_trades == 0
    assert result.stats.max_drawdown == -result.stats.net_return
