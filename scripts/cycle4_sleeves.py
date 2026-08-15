"""Cycle 4: confirm the sleeve ensemble in the production Decimal engine.

Design and promotion criteria were fixed in reports/research/cycle-4-hypotheses.md before
this ran. Nothing here is selected or tuned — it is a single confirmation run, so the
number of trials is one and no multiple-testing correction is required.

Usage:
    uv run python scripts/cycle4_sleeves.py [DB_PATH]
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

SLEEVES: dict[str, tuple[tuple[str, ...], int]] = {
    "equity": (
        ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLB", "XLE", "XLF",
         "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"),
        3,
    ),
    "bonds": (("TLT", "IEF", "TIP", "LQD", "HYG"), 2),
    "real": (("DBC", "GLD", "VNQ"), 1),
}

# Momentum ranking plus absolute momentum (the regime gate), and roster-based exit.
# No breakout entry, no trailing stop: cycles 1-3 showed those destroy the signal.
SLEEVE_SWITCHES = ComponentSwitches(
    momentum=True,
    asset_trend=False,
    market_regime=False,
    donchian_entry=False,
    donchian_exit=False,
    atr_risk=True,
    trailing_stop=False,
    roster_exit=True,
)


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    return 0.0 if deviation == 0 else (
        statistics.fmean(returns) / deviation * math.sqrt(PERIODS_PER_YEAR)
    )


def _sharpe_interval(returns: list[float]) -> tuple[float, float, float]:
    sharpe = _sharpe(returns)
    per_period = sharpe / math.sqrt(PERIODS_PER_YEAR)
    error = math.sqrt((1 + per_period**2 / 2) / len(returns)) * math.sqrt(PERIODS_PER_YEAR)
    return sharpe, sharpe - 1.96 * error, sharpe + 1.96 * error


def _drawdown(equity: list[float]) -> float:
    peak, worst = equity[0], 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _sleeve_config(base: StrategyConfig, universe: tuple[str, ...], hold: int) -> StrategyConfig:
    payload = base.model_dump(mode="python")
    payload.update({"version": "1.2.0", "universe": universe, "roster_size": hold})
    return StrategyConfig.model_validate(payload)


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

    curves: dict[str, list[float]] = {}
    for name, (universe, hold) in SLEEVES.items():
        config = _sleeve_config(base, universe, hold)
        result = BacktestEngine(config, initial_cash=Decimal("100")).run(
            histories, BenchmarkVariant.FULL_STRATEGY, component_switches=SLEEVE_SWITCHES
        )
        curves[name] = [float(p.equity) for p in result.equity_curve]

    benchmark = BacktestEngine(base, initial_cash=Decimal("100")).run(
        histories, BenchmarkVariant.SPY_BUY_AND_HOLD
    )
    curves["SPY buy & hold"] = [float(p.equity) for p in benchmark.equity_curve]

    def to_returns(curve: list[float]) -> list[float]:
        return [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0]

    returns = {name: to_returns(curve) for name, curve in curves.items()}
    length = min(len(v) for v in returns.values())
    sleeve_names = list(SLEEVES)
    ensemble = [
        statistics.fmean([returns[name][i] for name in sleeve_names]) for i in range(length)
    ]
    ensemble_equity = [100.0]
    for value in ensemble:
        ensemble_equity.append(ensemble_equity[-1] * (1 + value))

    print("PRODUCTION ENGINE — sleeves and ensemble")
    print(f"  {'series':18}{'final':>10}{'Sharpe':>8}{'maxDD':>9}   {'95% CI':>18}")
    rows: dict[str, tuple[float, float, float]] = {}
    for name in [*sleeve_names, "SPY buy & hold"]:
        sharpe, low, high = _sharpe_interval(returns[name])
        rows[name] = (sharpe, _drawdown(curves[name]), curves[name][-1])
        print(f"  {name:18}${curves[name][-1]:>9.2f}{sharpe:>8.2f}"
              f"{_drawdown(curves[name]) * 100:>8.2f}%   [{low:>6.2f}, {high:>5.2f}]")
    e_sharpe, e_low, e_high = _sharpe_interval(ensemble)
    e_dd = _drawdown(ensemble_equity)
    print(f"  {'ENSEMBLE (equal)':18}${ensemble_equity[-1]:>9.2f}{e_sharpe:>8.2f}"
          f"{e_dd * 100:>8.2f}%   [{e_low:>6.2f}, {e_high:>5.2f}]")

    print()
    print("PRE-REGISTERED PROMOTION CRITERIA")
    best_sleeve = max(rows[n][0] for n in sleeve_names)
    checks = [
        ("1. confirmed in production engine", True),
        ("2. ensemble Sharpe > every sleeve", e_sharpe > best_sleeve),
        ("3. ensemble maxDD < equity sleeve", e_dd < rows["equity"][1]),
        ("4. ensemble Sharpe CI excludes zero", e_low > 0),
        ("5. ensemble Sharpe >= SPY buy & hold", e_sharpe >= rows["SPY buy & hold"][0]),
    ]
    for label, passed in checks:
        print(f"  {label:38}{'PASS' if passed else 'FAIL'}")
    verdict = all(passed for _, passed in checks)
    print()
    print(f"VERDICT: {'PROMOTE to a candidate version' if verdict else 'DO NOT PROMOTE'}")
    if not verdict:
        print("  Recorded as a negative result. No deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
