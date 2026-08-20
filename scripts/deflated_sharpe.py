"""Correct cycle 2's result for selection bias and estimation error.

Two separate questions, often conflated:

1. The selection-region winner was chosen from N trials. Bailey & Lopez de Prado's
   Deflated Sharpe Ratio asks how high the best of N *skill-free* strategies would be
   expected to score by luck alone, and whether the observed winner clears that bar.
2. The holdout was a single test of one candidate, so it needs no trial correction — but
   533 daily observations is a small sample, and a Sharpe estimated from it carries a
   standard error that is usually left unstated.

Usage:
    uv run python scripts/deflated_sharpe.py [DB_PATH]
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

from quantbot.backtest import BacktestEngine, BenchmarkVariant, ComponentSwitches
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

EULER_MASCHERONI = 0.5772156649015329
PERIODS_PER_YEAR = 252
MANIFEST = Path("reports") / "research" / "cycle2-2026-08-14.json"


def _inverse_normal_cdf(probability: float) -> float:
    """Acklam's rational approximation; accurate to ~1e-9 over the range we need."""
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    low, high = 0.02425, 1 - 0.02425
    if probability < low:
        q = math.sqrt(-2 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if probability > high:
        q = math.sqrt(-2 * math.log(1 - probability))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = probability - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


def expected_max_sharpe(sharpe_dispersion: float, trials: int) -> float:
    """Expected highest Sharpe among `trials` strategies that all truly have zero skill."""
    if trials < 2:
        return 0.0
    first = _inverse_normal_cdf(1 - 1 / trials)
    second = _inverse_normal_cdf(1 - 1 / (trials * math.e))
    return sharpe_dispersion * ((1 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second)


def deflated_sharpe(
    observed: float, benchmark: float, observations: int, skew: float, kurtosis: float
) -> float:
    """Probability the observed Sharpe reflects skill rather than selection luck."""
    denominator = 1 - skew * observed + ((kurtosis - 1) / 4) * observed**2
    if denominator <= 0 or observations < 2:
        return float("nan")
    statistic = (observed - benchmark) * math.sqrt(observations - 1) / math.sqrt(denominator)
    return _normal_cdf(statistic)


def _moments(returns: list[float]) -> tuple[float, float, float, float]:
    count = len(returns)
    mean = statistics.fmean(returns)
    deviation = statistics.pstdev(returns)
    if deviation == 0:
        return mean, 0.0, 0.0, 3.0
    skew = sum((value - mean) ** 3 for value in returns) / (count * deviation**3)
    kurtosis = sum((value - mean) ** 4 for value in returns) / (count * deviation**4)
    return mean, deviation, skew, kurtosis


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
    common = set.intersection(*({bar.timestamp for bar in bars} for bars in raw.values()))
    histories = {s: [b for b in v if b.timestamp in common] for s, v in raw.items()}
    sessions = sorted(common)
    holdout_start = sessions[-int(len(sessions) * 0.20)]

    off = dict.fromkeys(
        ("momentum", "asset_trend", "market_regime", "donchian_entry", "donchian_exit",
         "atr_risk", "trailing_stop", "roster_exit"), False)
    winner = ComponentSwitches(
        **{**off, "momentum": True, "market_regime": True, "roster_exit": True, "atr_risk": True}
    )

    print("=" * 78)
    print("PART 1 — Selection bias: was the winner distinguishable from luck over 31 trials?")
    print("=" * 78)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    selection = {
        name: float(payload["selection_sharpe"])
        for name, payload in manifest["results"].items()
        if not name.startswith("HOLDOUT:") and payload.get("selection_sharpe") not in (None, "n/a")
    }
    trials = len(selection)
    values = sorted(selection.values(), reverse=True)
    dispersion = statistics.stdev(values)
    best_name = max(selection, key=lambda k: selection[k])
    best = selection[best_name]
    threshold = expected_max_sharpe(dispersion, trials)
    print(f"  trials recorded            {trials}")
    print(f"  dispersion of Sharpe       {dispersion:.4f}")
    print(f"  best observed              {best:.4f}  ({best_name})")
    print(f"  expected best if NO skill  {threshold:.4f}")
    print(f"  margin over luck           {best - threshold:+.4f}")
    print(f"  verdict                    {'clears' if best > threshold else 'DOES NOT CLEAR'}"
          " the luck threshold")

    print()
    print("=" * 78)
    print("PART 2 — Holdout: one test, but how precise is a Sharpe from 533 observations?")
    print("=" * 78)
    for label, switches, variant in (
        ("D7 (selected)", winner, BenchmarkVariant.FULL_STRATEGY),
        ("SPY buy & hold", None, BenchmarkVariant.SPY_BUY_AND_HOLD),
    ):
        result = BacktestEngine(config, initial_cash=__import__("decimal").Decimal("100")).run(
            histories, variant, component_switches=switches
        )
        curve = [p for p in result.equity_curve if p.timestamp >= holdout_start]
        returns = [
            float(curve[i].equity / curve[i - 1].equity - 1)
            for i in range(1, len(curve))
            if curve[i - 1].equity > 0
        ]
        mean, deviation, skew, kurtosis = _moments(returns)
        if deviation == 0:
            print(f"  {label:16} flat equity, Sharpe undefined")
            continue
        per_period = mean / deviation
        annual = per_period * math.sqrt(PERIODS_PER_YEAR)
        observations = len(returns)
        standard_error = math.sqrt((1 + per_period**2 / 2) / observations) * math.sqrt(
            PERIODS_PER_YEAR
        )
        print(f"  {label}")
        print(f"    observations             {observations}")
        print(f"    annualised Sharpe        {annual:.3f}")
        print(f"    standard error           {standard_error:.3f}")
        print(f"    95% confidence interval  [{annual - 1.96 * standard_error:.3f},"
              f" {annual + 1.96 * standard_error:.3f}]")
        print(f"    skew / kurtosis          {skew:.2f} / {kurtosis:.2f}")
        probability = deflated_sharpe(per_period, 0.0, observations, skew, kurtosis)
        print(f"    P(Sharpe > 0)            {probability:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
