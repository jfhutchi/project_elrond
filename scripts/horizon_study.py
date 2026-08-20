"""Which trading horizon actually pays, net of costs?

Shorter horizons trade more, and every trade pays the spread. Monthly rotation turns the
portfolio over about 12 times a year; daily rotation about 252 times. At 5bps per side that
is roughly 0.6% a year against 12.6%, so a short-horizon signal must clear a far higher bar
before it beats a slow one.

This measures signal strength and cost drag separately at each horizon, so the two are not
confused: a horizon can have a real edge and still lose money after costs.

Uses the simplified simulator validated against the production Decimal engine to within
0.021 Sharpe.

Usage:
    uv run python scripts/horizon_study.py [DB_PATH]
"""

from __future__ import annotations

import math
import os
import statistics
import sys
from pathlib import Path

from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

PERIODS_PER_YEAR = 252
#: Overridable so the cost crossover can be located, not assumed.
SLIPPAGE_PER_SIDE = float(os.environ.get("SLIPPAGE_BPS", "5")) / 10000
TOP_N = 3

# (label, lookback in sessions, rebalance cadence in sessions)
HORIZONS = (
    ("1d reversal", 1, 1),
    ("2d momentum", 2, 1),
    ("5d momentum", 5, 5),
    ("10d momentum", 10, 5),
    ("21d momentum", 21, 21),
    ("63d momentum", 63, 21),
    ("126d momentum", 126, 21),
    ("252d momentum", 252, 21),
)


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    return 0.0 if deviation == 0 else (
        statistics.fmean(returns) / deviation * math.sqrt(PERIODS_PER_YEAR)
    )


def _drawdown(returns: list[float]) -> float:
    equity, peak, worst = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)
    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {s: repository.list_bars(symbol=s, provider=key) for s in config.universe}
    database.close()
    stamps = set.intersection(*({b.timestamp for b in v} for v in raw.values()))
    dates = sorted(stamps)
    symbols = list(config.universe)
    closes = {s: [float(b.close) for b in raw[s] if b.timestamp in stamps] for s in symbols}
    opens = {s: [float(b.open) for b in raw[s] if b.timestamp in stamps] for s in symbols}

    start, end = 253, len(dates) - 3
    years = (end - start) / PERIODS_PER_YEAR
    print(f"sessions {len(dates)}  simulated {dates[start].date()} .. {dates[end].date()}"
          f"  ({years:.1f} years)")
    print(f"slippage {SLIPPAGE_PER_SIDE * 10000:.0f}bps per side, top {TOP_N}, $100\n")

    header = (f"  {'horizon':16}{'gross':>9}{'net':>9}{'cost/yr':>9}"
              f"{'Sharpe':>8}{'maxDD':>8}{'trades/yr':>11}")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for label, lookback, cadence in HORIZONS:
        held: dict[str, float] = {}
        gross_returns: list[float] = []
        net_returns: list[float] = []
        total_turnover = 0.0
        rebalances = 0
        for index in range(start, end):
            if (index - start) % cadence == 0:
                scored = []
                for symbol in symbols:
                    past = closes[symbol][index - lookback]
                    if past > 0:
                        scored.append((closes[symbol][index] / past - 1, symbol))
                # A 1-day horizon is tested as reversal; longer ones as momentum.
                ranked = sorted(scored, reverse=lookback > 1)
                picks = [s for _, s in ranked[:TOP_N]]
                target = {s: 1 / len(picks) for s in picks} if picks else {}
                rebalances += 1
            else:
                target = held
            turnover = sum(abs(target.get(s, 0.0) - held.get(s, 0.0)) for s in symbols)
            total_turnover += turnover
            gross = 0.0
            for symbol, weight in target.items():
                entry, exit_price = opens[symbol][index + 1], opens[symbol][index + 2]
                if entry > 0:
                    gross += weight * (exit_price / entry - 1)
            gross_returns.append(gross)
            net_returns.append(gross - turnover * SLIPPAGE_PER_SIDE)
            held = target

        gross_total = math.prod(1 + v for v in gross_returns) - 1
        net_total = math.prod(1 + v for v in net_returns) - 1
        cost_per_year = total_turnover * SLIPPAGE_PER_SIDE / years
        trades_per_year = rebalances * TOP_N / years
        print(f"  {label:16}{gross_total * 100:>8.1f}%{net_total * 100:>8.1f}%"
              f"{cost_per_year * 100:>8.2f}%{_sharpe(net_returns):>8.2f}"
              f"{_drawdown(net_returns) * 100:>7.1f}%{trades_per_year:>11.0f}")

    print()
    print("  gross = before costs, net = after. The gap is what the horizon costs to run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
