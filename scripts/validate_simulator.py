"""Cross-check the simplified ensemble simulator against the production engine.

Cycle 3's ensemble result was produced by a simplified weight-based simulator. Before any
of it is believed, that simulator has to agree with the production Decimal engine on a
strategy both can express: cross-sectional 12-1 momentum over the full universe, monthly.

They will not agree exactly — production fills at the next open with slippage and models
per-order costs, the simplified one charges cost on turnover — but gross divergence would
mean cycle 3's numbers describe a simulator bug rather than a market.

Usage:
    uv run python scripts/validate_simulator.py [DB_PATH]
"""

from __future__ import annotations

import math
import statistics
import sys
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import BacktestEngine, BenchmarkVariant
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

PERIODS_PER_YEAR = 252
COST_PER_UNIT_TURNOVER = 0.0005


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    return 0.0 if deviation == 0 else statistics.fmean(returns) / deviation * math.sqrt(
        PERIODS_PER_YEAR
    )


def _max_drawdown_from_equity(equity: list[float]) -> float:
    peak, worst = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
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
    common = sorted(stamps)
    histories = {s: [b for b in raw[s] if b.timestamp in stamps] for s in config.universe}
    closes = {s: [float(b.close) for b in histories[s]] for s in config.universe}
    opens = {s: [float(b.open) for b in histories[s]] for s in config.universe}
    symbols = list(config.universe)

    print("PRODUCTION ENGINE — PURE_MOMENTUM_12_1")
    result = BacktestEngine(config, initial_cash=Decimal("100")).run(
        histories, BenchmarkVariant.PURE_MOMENTUM_12_1
    )
    curve = [float(point.equity) for point in result.equity_curve]
    production_returns = [
        curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0
    ]
    print(f"  final equity     ${curve[-1]:.2f}")
    print(f"  total return     {(curve[-1] / 100 - 1) * 100:.2f}%")
    print(f"  Sharpe           {_sharpe(production_returns):.3f}")
    print(f"  max drawdown     {_max_drawdown_from_equity(curve) * 100:.2f}%")
    print(f"  trades           {len(result.trades)}")

    # Match the production roster size so the comparison is like for like.
    hold = config.roster_size
    start, end = 273, len(common) - 3
    held: dict[str, float] = {}
    equity, series = 100.0, [100.0]
    for index in range(start, end):
        if (index - start) % 21 == 0:
            scored = []
            for symbol in symbols:
                bars = closes[symbol]
                if index < 252 or bars[index - 252] <= 0 or bars[index - 21] <= 0:
                    continue
                scored.append((bars[index - 21] / bars[index - 252] - 1, symbol))
            picks = [s for _, s in sorted(scored, reverse=True)[:hold]]
            target = {s: 1 / len(picks) for s in picks} if picks else {}
        else:
            target = held
        turnover = sum(abs(target.get(s, 0.0) - held.get(s, 0.0)) for s in symbols)
        period = -turnover * COST_PER_UNIT_TURNOVER
        for symbol, weight in target.items():
            entry, exit_price = opens[symbol][index + 1], opens[symbol][index + 2]
            if entry > 0:
                period += weight * (exit_price / entry - 1)
        equity *= 1 + period
        series.append(equity)
        held = target

    simplified_returns = [
        series[i] / series[i - 1] - 1 for i in range(1, len(series)) if series[i - 1] > 0
    ]
    print()
    print(f"SIMPLIFIED SIMULATOR — same signal, top {hold}, monthly")
    print(f"  final equity     ${series[-1]:.2f}")
    print(f"  total return     {(series[-1] / 100 - 1) * 100:.2f}%")
    print(f"  Sharpe           {_sharpe(simplified_returns):.3f}")
    print(f"  max drawdown     {_max_drawdown_from_equity(series) * 100:.2f}%")

    print()
    print("AGREEMENT")
    sharpe_gap = abs(_sharpe(production_returns) - _sharpe(simplified_returns))
    ratio = series[-1] / curve[-1] if curve[-1] > 0 else float("inf")
    print(f"  Sharpe difference        {sharpe_gap:.3f}")
    print(f"  final equity ratio       {ratio:.2f}x  (simplified / production)")
    verdict = "CONSISTENT" if sharpe_gap < 0.35 and 0.6 < ratio < 1.7 else "DIVERGENT"
    print(f"  verdict                  {verdict}")
    if verdict == "DIVERGENT":
        print("  Cycle 3's ensemble result cannot be trusted until this is explained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
