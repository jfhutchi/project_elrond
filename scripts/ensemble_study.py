"""Cycle 3: do several weak, genuinely different strategies combine into something better?

The cycle 1 and 2 result was that no single configuration of one momentum family is
distinguishable from noise. The evidenced alternative is a portfolio of low-correlation
strategies: Sharpe scales with the square root of the number of uncorrelated components,
so four honest 0.5-Sharpe strategies reach 1.0 together — and, unlike a single tuned
strategy, that is not achieved by fitting.

Signals are drawn from deliberately different information domains so their errors are
unlikely to coincide: cross-sectional momentum, short-horizon reversal, low volatility,
and absolute trend.

LIMITATION, and it matters: this is a simplified weight-based simulator — next-session
open-to-open returns, costs charged on turnover, float arithmetic. It exists to measure
correlation structure and ensemble benefit, not to price a strategy. Anything promising
here must be re-run through the production Decimal engine before it means anything.

Usage:
    uv run python scripts/ensemble_study.py [DB_PATH]
"""

from __future__ import annotations

import math
import statistics
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

PERIODS_PER_YEAR = 252
COST_PER_UNIT_TURNOVER = 0.0005  # 5bps per side, matching the production config.
TOP_N = 5

Weights = dict[str, float]
SignalFn = Callable[[int], Weights]


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    if deviation == 0:
        return 0.0
    return statistics.fmean(returns) / deviation * math.sqrt(PERIODS_PER_YEAR)


def _max_drawdown(returns: Sequence[float]) -> float:
    equity, peak, worst = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2:
        return 0.0
    left_dev, right_dev = statistics.pstdev(left), statistics.pstdev(right)
    if left_dev == 0 or right_dev == 0:
        return 0.0
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    covariance = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)
    ) / len(left)
    return covariance / (left_dev * right_dev)


def simulate(
    signal: SignalFn,
    symbols: list[str],
    closes: dict[str, list[float]],
    opens: dict[str, list[float]],
    start: int,
    end: int,
    rebalance_every: int,
) -> list[float]:
    """Weights decided on session i act from session i+1's open; costs charged on turnover."""
    held: Weights = {}
    daily: list[float] = []
    for index in range(start, end):
        if (index - start) % rebalance_every == 0:
            target = signal(index)
        else:
            target = held
        turnover = sum(
            abs(target.get(symbol, 0.0) - held.get(symbol, 0.0)) for symbol in symbols
        )
        period = -turnover * COST_PER_UNIT_TURNOVER
        for symbol, weight in target.items():
            entry, exit_price = opens[symbol][index + 1], opens[symbol][index + 2]
            if entry > 0:
                period += weight * (exit_price / entry - 1)
        daily.append(period)
        held = target
    return daily


def build_signals(
    symbols: list[str], closes: dict[str, list[float]], benchmark: str
) -> dict[str, tuple[SignalFn, int]]:
    def momentum(index: int) -> Weights:
        """Cross-sectional 12-1 momentum: the classic, and our incumbent."""
        scored = []
        for symbol in symbols:
            series = closes[symbol]
            if index < 252 or series[index - 252] <= 0 or series[index - 21] <= 0:
                continue
            scored.append((series[index - 21] / series[index - 252] - 1, symbol))
        winners = [s for _, s in sorted(scored, reverse=True)[:TOP_N]]
        return {s: 1 / len(winners) for s in winners} if winners else {}

    def reversal(index: int) -> Weights:
        """Short-horizon reversal: buy the worst of the last five sessions."""
        scored = []
        for symbol in symbols:
            series = closes[symbol]
            if index < 6 or series[index - 5] <= 0:
                continue
            scored.append((series[index] / series[index - 5] - 1, symbol))
        losers = [s for _, s in sorted(scored)[:TOP_N]]
        return {s: 1 / len(losers) for s in losers} if losers else {}

    def low_volatility(index: int) -> Weights:
        """Defensive: hold the lowest realised-volatility names."""
        scored = []
        for symbol in symbols:
            series = closes[symbol]
            if index < 61:
                continue
            window = [
                series[i] / series[i - 1] - 1
                for i in range(index - 59, index + 1)
                if series[i - 1] > 0
            ]
            if len(window) > 2:
                scored.append((statistics.pstdev(window), symbol))
        calm = [s for _, s in sorted(scored)[:TOP_N]]
        return {s: 1 / len(calm) for s in calm} if calm else {}

    def absolute_trend(index: int) -> Weights:
        """Binary risk-on/off on the benchmark's own 200-day average."""
        series = closes[benchmark]
        if index < 200:
            return {}
        average = statistics.fmean(series[index - 199 : index + 1])
        return {benchmark: 1.0} if series[index] > average else {}

    def sleeve_momentum(sleeve: tuple[str, ...], hold: int) -> SignalFn:
        """Same 12-1 signal confined to one asset class.

        Long-only equity strategies all load on the same beta, so they cannot diversify
        each other however different their signals look. Separating the asset class is
        the only way to change the underlying risk factor.
        """

        def signal(index: int) -> Weights:
            scored = []
            for symbol in sleeve:
                series = closes[symbol]
                if index < 252 or series[index - 252] <= 0 or series[index - 21] <= 0:
                    continue
                score = series[index - 21] / series[index - 252] - 1
                if score > 0:  # absolute momentum: hold cash rather than a falling asset
                    scored.append((score, symbol))
            picks = [s for _, s in sorted(scored, reverse=True)[:hold]]
            return {s: 1 / len(picks) for s in picks} if picks else {}

        return signal

    equities = ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLB", "XLE", "XLF",
                "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")
    bonds = ("TLT", "IEF", "TIP", "LQD", "HYG")
    real_assets = ("DBC", "GLD", "VNQ")

    return {
        "momentum-12-1": (momentum, 21),
        "reversal-5d": (reversal, 5),
        "low-volatility": (low_volatility, 21),
        "absolute-trend": (absolute_trend, 21),
        "sleeve-equity": (sleeve_momentum(equities, 3), 21),
        "sleeve-bonds": (sleeve_momentum(bonds, 2), 21),
        "sleeve-real": (sleeve_momentum(real_assets, 1), 21),
    }


def report(name: str, returns: Sequence[float]) -> None:
    observations = len(returns)
    sharpe = _sharpe(returns)
    total = math.prod(1 + value for value in returns) - 1
    per_period = sharpe / math.sqrt(PERIODS_PER_YEAR)
    error = math.sqrt((1 + per_period**2 / 2) / observations) * math.sqrt(PERIODS_PER_YEAR)
    print(
        f"  {name:16}{total * 100:>9.2f}%{sharpe:>8.2f}{_max_drawdown(returns) * 100:>9.2f}%"
        f"   [{sharpe - 1.96 * error:>6.2f}, {sharpe + 1.96 * error:>5.2f}]"
    )


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

    common = sorted(set.intersection(*({b.timestamp for b in v} for v in raw.values())))
    symbols = list(config.universe)
    closes = {s: [float(b.close) for b in raw[s] if b.timestamp in set(common)] for s in symbols}
    opens = {s: [float(b.open) for b in raw[s] if b.timestamp in set(common)] for s in symbols}

    start, end = 273, len(common) - 3
    holdout_index = len(common) - int(len(common) * 0.20)
    print(f"sessions {len(common)}  {common[0].date()} .. {common[-1].date()}")
    print(f"simulated {common[start].date()} .. {common[end].date()}")
    print(f"holdout from {common[holdout_index].date()}")
    print()

    signals = build_signals(symbols, closes, config.benchmark_symbol)
    series: dict[str, list[float]] = {}
    for name, (function, cadence) in signals.items():
        series[name] = simulate(function, symbols, closes, opens, start, end, cadence)

    names = list(series)
    print("individual strategies (full simulated period)")
    print(f"  {'strategy':16}{'total':>9}{'Sharpe':>8}{'maxDD':>10}   {'95% CI on Sharpe':>18}")
    for name in names:
        report(name, series[name])

    print()
    print("pairwise correlation of daily returns")
    print(f"  {'':16}" + "".join(f"{n[:12]:>14}" for n in names))
    for row in names:
        cells = "".join(f"{_correlation(series[row], series[col]):>14.2f}" for col in names)
        print(f"  {row:16}{cells}")

    length = len(series[names[0]])
    equal_weight = [
        statistics.fmean([series[name][i] for name in names]) for i in range(length)
    ]
    print()
    print("ensemble")
    report("equal-weight", equal_weight)

    holdout_offset = holdout_index - start
    print()
    print("holdout only (never used to choose anything)")
    for name in names:
        report(name, series[name][holdout_offset:])
    report("equal-weight", equal_weight[holdout_offset:])
    print()
    print("NOTE simplified simulator: open-to-open, turnover costs, float arithmetic.")
    print("     Confirm anything promising in the production Decimal engine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
