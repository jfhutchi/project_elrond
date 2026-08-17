"""Dry-run the evaluation tonight's cycle will perform, without touching broker or ledger.

v1.2.0's component switches are new code in the live signal path and have never executed a
full cycle. Its first real run would otherwise be tonight, unattended, where a fault halts
the cycle and engages the kill switch. This exercises the same decision path read-only:
roster construction, per-symbol evaluation, position-context reconstruction for positions
entered under a previous version, and risk sizing.

Writes nothing. Submits nothing.

Usage:
    uv run python scripts/preflight_cycle.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from quantbot.market_data import MarketDataCache
from quantbot.market_data.calendar import latest_completed_session, session_sequence
from quantbot.operations.runners import _position_open_times
from quantbot.risk import (
    EntrySizingRequest,
    PositionRiskInput,
    RiskApproval,
    build_portfolio_risk_state,
    calculate_drawdown,
    size_entry,
)
from quantbot.runtime import build_runtime
from quantbot.storage import StorageRepository
from quantbot.strategy import (
    PositionContext,
    SignalAction,
    build_monthly_roster,
    evaluate_symbol,
    wilder_atr,
)


async def main() -> int:
    runtime = build_runtime()
    config, identity = runtime.config, runtime.identity
    print(f"strategy   {identity.strategy_id}  v{identity.version}")
    print(f"components {config.components.model_dump()}")

    sessions = await runtime.sessions.sessions()
    now = datetime.now(UTC)
    completed = latest_completed_session(sessions, now)
    if completed is None:
        print("no completed session")
        return 1
    index = next(i for i, s in enumerate(sessions) if s.session_date == completed.session_date)
    evaluation_at = completed.close_at
    next_open = sessions[index + 1].open_at
    sequence = session_sequence(sessions)
    print(f"evaluates at {evaluation_at.isoformat()} -> acts {next_open.isoformat()}")
    print("NOTE: tonight the completed session will be today's close, not this one.\n")

    feed = runtime.market_data_config
    key = MarketDataCache.provider_key(
        feed.provider, feed.feed, feed.adjustment, feed.timeframe
    )
    with runtime.database.transaction() as session:
        repository = StorageRepository(session)
        histories = {s: repository.list_bars(symbol=s, provider=key) for s in config.universe}
        fills = tuple(repository.list_fills())
        equity_history = repository.list_equity_snapshots(
            account_id=os.environ["EXPECTED_ACCOUNT_ID"]
        )

    missing = [s for s, bars in histories.items() if not bars]
    if missing:
        print(f"FAIL durable bars missing for {missing}")
        return 1

    def cut(at: datetime) -> dict[str, list]:
        out = {}
        for symbol, bars in histories.items():
            kept = [b for b in bars if b.timestamp <= at]
            if not kept or kept[-1].timestamp != at:
                raise SystemExit(f"FAIL history for {symbol} not fresh through {at}")
            out[symbol] = kept
        return out

    sliced = cut(evaluation_at)
    acting = index + 1
    month = sessions[acting].session_date.strftime("%Y-%m")
    effective = acting
    while effective > 0 and sessions[effective - 1].session_date.strftime("%Y-%m") == month:
        effective -= 1
    roster = build_monthly_roster(
        cut(sessions[effective - 1].close_at),
        sessions[effective - 1].close_at,
        sessions[effective].open_at,
        config,
        session_sequence=sequence,
    )
    print(f"roster OK  {len(roster.symbols)} names: {', '.join(roster.symbols)}\n")

    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
    }
    base = "https://paper-api.alpaca.markets/v2"
    account = httpx.get(f"{base}/account", headers=headers, timeout=30).json()
    raw_positions = httpx.get(f"{base}/positions", headers=headers, timeout=30).json()
    held = {p["symbol"]: p for p in raw_positions}
    opened = _position_open_times(fills)
    high_water = max((r.equity for r in equity_history), default=None)
    drawdown = calculate_drawdown(
        Decimal(account["equity"]),
        high_water if high_water and high_water > 0 else None,
        config,
    )
    print(f"equity ${account['equity']}  drawdown {drawdown.drawdown_fraction * 100:.2f}%"
          f"  multiplier {drawdown.new_risk_multiplier}  entry_halted={drawdown.entry_halted}\n")

    actions: dict[str, int] = {}
    bootstrapped = 0
    would_submit = []
    for symbol in config.universe:
        context = None
        if symbol in held:
            entered = opened.get(symbol)
            if entered is None:
                print(f"FAIL {symbol} held but no durable opening fill")
                return 1
            atr = wilder_atr(sliced[symbol], config.atr_period)
            if atr is None:
                print(f"FAIL {symbol} ATR undefined, cannot bootstrap a stop")
                return 1
            entry = Decimal(held[symbol]["avg_entry_price"])
            initial = entry - config.initial_stop_atr * atr
            if initial <= 0:
                initial = entry / Decimal("2")
            context = PositionContext(
                symbol=symbol, entered_at=entered, initial_stop=initial, active_stop=initial
            )
            bootstrapped += 1
        decision = evaluate_symbol(
            sliced[symbol], sliced[config.regime_symbol], roster, evaluation_at, next_open,
            config, identity, context, session_sequence=sequence,
        )
        actions[decision.action.value] = actions.get(decision.action.value, 0) + 1
        if symbol in held or decision.action is SignalAction.ENTER:
            marker = "HELD" if symbol in held else "    "
            print(f"  {marker} {symbol:5} {decision.action.value:10} {','.join(decision.reasons)}")
        if decision.action is SignalAction.ENTER and symbol not in held:
            would_submit.append((symbol, decision))

    print(f"\naction tally {actions}")
    print(f"position contexts bootstrapped from ATR: {bootstrapped} "
          f"(entered under v1.1.0, so no prior position_state exists)")

    print("\nrisk sizing for prospective entries:")
    reservations: list = []
    from quantbot.domain import Account
    acct = Account(
        account_id=account["id"], cash=Decimal(account["cash"]),
        buying_power=Decimal(account["buying_power"]), equity=Decimal(account["equity"]),
        currency=account["currency"],
    )
    if not would_submit:
        print("  none — nothing new would be entered on this session's data")
    for symbol, decision in would_submit:
        if decision.asset_close is None or decision.initial_stop_distance is None:
            print(f"  {symbol:5} inputs unavailable")
            continue
        portfolio = build_portfolio_risk_state(
            f"preflight:{symbol}", acct, symbol,
            [PositionRiskInput(position=p, active_stop=None) for p in []], reservations,
        )
        sized = size_entry(
            EntrySizingRequest(
                symbol=symbol, reference_price=decision.asset_close,
                stop_distance=decision.initial_stop_distance,
                portfolio=portfolio, drawdown=drawdown,
            ), config,
        )
        if isinstance(sized, RiskApproval):
            print(f"  {symbol:5} BUY {sized.quantity} "
                  f"(${sized.quantity * decision.asset_close:.2f}) "
                  f"cap={sized.limiting_caps[0].value}")
        else:
            print(f"  {symbol:5} rejected: {','.join(sized.reasons)}")

    print("\nPREFLIGHT PASSED — the v1.2.0 decision path executes without fault")
    await runtime.aclose()
    runtime.database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
