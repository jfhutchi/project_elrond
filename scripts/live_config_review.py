"""What is the currently deployed configuration costing, against the alternatives?

The live account runs strategy-v1-1.yaml with every component enabled. Six research cycles
scored that configuration lowest of everything tested. This puts the incumbent and its
alternatives side by side over the full history in the production engine, so the deployment
decision rests on one comparable table rather than numbers scattered across six reports.

Usage:
    uv run python scripts/live_config_review.py [DB_PATH]
"""

from __future__ import annotations

import math
import statistics
import sys
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import BacktestEngine, BenchmarkVariant, ComponentSwitches
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, load_strategy_config

PERIODS_PER_YEAR = 252


def _stats(curve: list[float]) -> tuple[float, float, float, float]:
    returns = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0]
    deviation = statistics.pstdev(returns)
    sharpe = 0.0 if deviation == 0 else (
        statistics.fmean(returns) / deviation * math.sqrt(PERIODS_PER_YEAR)
    )
    peak, worst = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    years = len(returns) / PERIODS_PER_YEAR
    cagr = (curve[-1] / curve[0]) ** (1 / years) - 1 if years > 0 and curve[0] > 0 else 0.0
    return curve[-1], sharpe, worst, cagr


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    base = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)
    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {s: repository.list_bars(symbol=s, provider=key) for s in base.universe}
    database.close()
    stamps = set.intersection(*({b.timestamp for b in v} for v in raw.values()))
    histories = {s: [b for b in raw[s] if b.timestamp in stamps] for s in base.universe}

    engine = BacktestEngine(base, initial_cash=Decimal("100"))
    candidates: dict[str, list[float]] = {}
    for label, variant in (
        ("LIVE: full strategy", BenchmarkVariant.FULL_STRATEGY),
        ("pure 12-1 momentum", BenchmarkVariant.PURE_MOMENTUM_12_1),
        ("momentum + trend", BenchmarkVariant.MOMENTUM_TREND),
        ("SPY 200d trend", BenchmarkVariant.SPY_SMA200),
        ("SPY buy & hold", BenchmarkVariant.SPY_BUY_AND_HOLD),
    ):
        result = engine.run(histories, variant)
        candidates[label] = [float(p.equity) for p in result.equity_curve]

    # The sleeve ensemble from cycles 4/5, rebuilt here so every row is comparable.
    sleeves = {
        "equity": (("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLB", "XLE", "XLF",
                    "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"), 3),
        "bonds": (("TLT", "IEF", "TIP", "LQD", "HYG"), 2),
        "real": (("DBC", "GLD", "VNQ"), 1),
    }
    switches = ComponentSwitches(
        momentum=True, asset_trend=False, market_regime=False, donchian_entry=False,
        donchian_exit=False, atr_risk=True, trailing_stop=False, roster_exit=True,
    )
    sleeve_returns: list[list[float]] = []
    for universe, hold in sleeves.values():
        payload = base.model_dump(mode="python")
        payload.update({"version": "1.2.0", "universe": universe, "roster_size": hold})
        config = StrategyConfig.model_validate(payload)
        curve = [
            float(p.equity)
            for p in BacktestEngine(config, initial_cash=Decimal("100")).run(
                histories, BenchmarkVariant.FULL_STRATEGY, component_switches=switches
            ).equity_curve
        ]
        sleeve_returns.append(
            [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0]
        )
    length = min(len(r) for r in sleeve_returns)
    ensemble_curve = [100.0]
    for index in range(length):
        blended = statistics.fmean([r[index] for r in sleeve_returns])
        ensemble_curve.append(ensemble_curve[-1] * (1 + blended))
    candidates["sleeve ensemble"] = ensemble_curve

    print(f"Production engine, {len(stamps)} sessions, $100 initial, costs applied")
    print()
    print(f"  {'configuration':24}{'final':>10}{'CAGR':>9}{'Sharpe':>8}{'maxDD':>9}")
    rows = {name: _stats(curve) for name, curve in candidates.items()}
    for name, (final, sharpe, drawdown, cagr) in sorted(
        rows.items(), key=lambda kv: kv[1][1], reverse=True
    ):
        marker = "  <-- DEPLOYED" if name.startswith("LIVE") else ""
        print(f"  {name:24}${final:>9.2f}{cagr * 100:>8.2f}%{sharpe:>8.2f}"
              f"{drawdown * 100:>8.2f}%{marker}")

    live_final, live_sharpe, _, _ = rows["LIVE: full strategy"]
    best_name = max(rows, key=lambda k: rows[k][1])
    best_final, best_sharpe, _, _ = rows[best_name]
    ranked = sorted(rows, key=lambda k: -rows[k][1])
    place = ranked.index("LIVE: full strategy") + 1
    print()
    print(f"  deployed configuration ranks {place} of {len(rows)} on Sharpe")
    print(f"  best available is '{best_name}': ${best_final:.2f} vs ${live_final:.2f}")
    print(f"  gap over the period: ${best_final - live_final:.2f} per $100 of capital")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
