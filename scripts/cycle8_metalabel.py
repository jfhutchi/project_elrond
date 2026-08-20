"""Cycle 8: can a meta-model tell which of the strategy's trades are worth taking?

Meta-labelling does not search for a better signal. It takes the primary strategy as given
and asks one narrow question at signal time: when this fires, will the trade work? The
meta-model can only decline trades, never invent them.

Pre-registered in reports/research/cycle-8-hypotheses.md. Two deviations recorded there and
here: gradient-boosted trees are dropped in favour of logistic regression alone, because 411
labelled examples cannot support tree ensembles and a smaller model keeps the trial count
countable; and no clean holdout exists, so the honest ceiling is CANDIDATE PENDING FORWARD
CONFIRMATION.

Validation is strictly expanding-window walk-forward: a fold trains only on trades that
CLOSED before the test window opens, minus an embargo. Trades span weeks, so naive k-fold
would leak the answer through overlapping positions.

Usage:
    uv run python scripts/cycle8_metalabel.py [DB_PATH]
"""

from __future__ import annotations

import math
import statistics
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import BacktestEngine, BenchmarkVariant
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config, momentum_12_1, sma, wilder_atr

EMBARGO = timedelta(days=5)
FOLDS = 4
MIN_TRAIN = 60
L2 = 1.0
EPOCHS = 4000
LEARNING_RATE = 0.05
FEATURES = (
    "atr_pct",
    "momentum",
    "dist_to_trend",
    "regime_on",
    "spy_vol_21d",
    "breakout_extent",
)


def _logistic(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-min(z, 60)))
    value = math.exp(max(z, -60))
    return value / (1 + value)


def _fit(rows: list[list[float]], labels: list[int]) -> list[float]:
    """Ridge-penalised logistic regression by gradient descent. Pure Python by design."""
    width = len(rows[0])
    weights = [0.0] * (width + 1)
    count = len(rows)
    for _ in range(EPOCHS):
        grad = [0.0] * (width + 1)
        for features, label in zip(rows, labels, strict=True):
            z = weights[0] + sum(w * f for w, f in zip(weights[1:], features, strict=True))
            error = _logistic(z) - label
            grad[0] += error
            for index, value in enumerate(features):
                grad[index + 1] += error * value
        weights[0] -= LEARNING_RATE * grad[0] / count
        for index in range(width):
            penalty = L2 * weights[index + 1] / count
            weights[index + 1] -= LEARNING_RATE * (grad[index + 1] / count + penalty)
    return weights


def _predict(weights: list[float], features: list[float]) -> float:
    return _logistic(weights[0] + sum(w * f for w, f in zip(weights[1:], features, strict=True)))


def _standardise(
    rows: list[list[float]],
) -> tuple[list[list[float]], list[float], list[float]]:
    width = len(rows[0])
    means = [statistics.fmean(r[i] for r in rows) for i in range(width)]
    devs = []
    for i in range(width):
        d = statistics.pstdev([r[i] for r in rows]) if len(rows) > 1 else 0.0
        devs.append(d if d > 1e-12 else 1.0)
    scaled = [[(r[i] - means[i]) / devs[i] for i in range(width)] for r in rows]
    return scaled, means, devs


def _trade_stats(returns: list[float]) -> tuple[float, float, float]:
    """Expectancy, profit factor, and a per-trade Sharpe over the trade sequence."""
    if not returns:
        return 0.0, 0.0, 0.0
    wins = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    expectancy = statistics.fmean(returns)
    factor = float("inf") if losses == 0 else wins / losses
    dev = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = 0.0 if dev == 0 else expectancy / dev * math.sqrt(len(returns))
    return expectancy, factor, sharpe


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
    histories = {s: [b for b in raw[s] if b.timestamp in stamps] for s in config.universe}
    spy = histories[config.regime_symbol]
    index_of = {bar.timestamp: i for i, bar in enumerate(spy)}

    result = BacktestEngine(config, initial_cash=Decimal("1000")).run(
        histories, BenchmarkVariant.FULL_STRATEGY
    )
    trades = sorted(result.trades, key=lambda t: t.opened_at)
    print(f"primary strategy produced {len(trades)} completed trades")

    samples: list[tuple[object, list[float], int, float]] = []
    for trade in trades:
        position = index_of.get(trade.opened_at)
        if position is None or position < config.trend_period + 2:
            continue
        bars = [b for b in histories[trade.symbol] if b.timestamp <= trade.opened_at]
        spy_bars = spy[: position + 1]
        if len(bars) < config.trend_period + 2:
            continue
        atr = wilder_atr(bars, config.atr_period)
        trend = sma(bars, config.trend_period)
        spy_trend = sma(spy_bars, config.trend_period)
        if atr is None or trend is None or spy_trend is None or atr == 0:
            continue
        close = float(bars[-1].close)
        window = [float(b.close) for b in spy_bars[-22:]]
        rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
        spy_vol = statistics.pstdev(rets) * math.sqrt(252) if len(rets) > 1 else 0.0
        entry_channel = max(float(b.high) for b in bars[-config.entry_period - 1 : -1])
        momentum = momentum_12_1(bars, config.momentum_long, config.momentum_skip)
        if momentum is None:
            continue
        features = [
            float(atr) / close,
            float(momentum),
            float((close - float(trend)) / close),
            1.0 if float(spy_bars[-1].close) > float(spy_trend) else 0.0,
            spy_vol,
            (close - entry_channel) / float(atr),
        ]
        outcome = float(trade.net_pnl) / float(trade.notional) if trade.notional else 0.0
        samples.append((trade, features, 1 if float(trade.net_pnl) > 0 else 0, outcome))

    print(f"usable labelled samples: {len(samples)}")
    base_returns = [s[3] for s in samples]
    exp0, pf0, sh0 = _trade_stats(base_returns)
    win0 = sum(s[2] for s in samples) / len(samples)
    print(f"unfiltered baseline: expectancy {exp0 * 100:+.3f}%  profit factor {pf0:.2f}"
          f"  win rate {win0 * 100:.1f}%  trade-Sharpe {sh0:.2f}\n")

    ordered = sorted(samples, key=lambda s: s[0].opened_at)
    fold_size = len(ordered) // (FOLDS + 1)
    trials = 0
    print(f"  {'fold':>5}{'train':>7}{'test':>6}{'taken':>7}{'declined':>10}"
          f"{'exp filtered':>14}{'exp all':>10}{'delta':>9}")
    kept_all: list[float] = []
    base_all: list[float] = []
    for fold in range(1, FOLDS + 1):
        split = fold_size * fold
        test = ordered[split : split + fold_size]
        if not test:
            continue
        boundary = test[0][0].opened_at - EMBARGO
        train = [s for s in ordered[:split] if s[0].closed_at < boundary]
        if len(train) < MIN_TRAIN:
            print(f"  {fold:>5}{len(train):>7}{len(test):>6}   insufficient training data")
            continue
        scaled, means, devs = _standardise([s[1] for s in train])
        weights = _fit(scaled, [s[2] for s in train])
        trials += 1
        taken, declined = [], 0
        for sample in test:
            norm = [(sample[1][i] - means[i]) / devs[i] for i in range(len(means))]
            if _predict(weights, norm) >= 0.5:
                taken.append(sample[3])
            else:
                declined += 1
        exp_f = statistics.fmean(taken) if taken else 0.0
        exp_a = statistics.fmean([s[3] for s in test])
        kept_all.extend(taken)
        base_all.extend([s[3] for s in test])
        print(f"  {fold:>5}{len(train):>7}{len(test):>6}{len(taken):>7}{declined:>10}"
              f"{exp_f * 100:>13.3f}%{exp_a * 100:>9.3f}%{(exp_f - exp_a) * 100:>+8.3f}%")

    print()
    if kept_all and base_all:
        ef, pff, shf = _trade_stats(kept_all)
        eb, pfb, shb = _trade_stats(base_all)
        print(f"  pooled out-of-fold, filtered : expectancy {ef * 100:+.3f}%"
              f"  PF {pff:.2f}  trade-Sharpe {shf:.2f}  n={len(kept_all)}")
        print(f"  pooled out-of-fold, all      : expectancy {eb * 100:+.3f}%"
              f"  PF {pfb:.2f}  trade-Sharpe {shb:.2f}  n={len(base_all)}")
        print(f"  improvement in trade-Sharpe  : {shf - shb:+.3f}")
        print(f"\n  trials this cycle: {trials}. Any improvement must be deflated by the")
        print("  cumulative trial count across all cycles, not reported raw.")
        verdict = "IMPROVES" if shf > shb else "DOES NOT IMPROVE"
        print(f"  verdict: meta-model {verdict} the primary strategy on purged walk-forward")
    else:
        print("  no fold produced usable out-of-fold predictions")
    print("\n  No clean holdout exists (cycles 2-7 consumed every out-of-sample window),")
    print("  so the ceiling for this cycle is CANDIDATE PENDING FORWARD CONFIRMATION.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
