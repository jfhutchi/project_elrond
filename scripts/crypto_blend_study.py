"""Does adding a crypto sleeve improve the equity portfolio, rather than replace it?

Cycle 5 compared crypto AGAINST equities and concluded it lost. That is a different question
from whether crypto helps a blended portfolio. A sleeve that is worse standalone can still
improve a portfolio if its correlation is low enough, which is the only thing diversification
actually requires.

This decides whether the 24/7 architecture is worth building BEFORE any of it is written.

Returns are aligned on equity session dates: crypto trades every day, so its return over the
span between two equity sessions is compounded into that session. That models a portfolio
rebalanced on equity sessions, which is what the existing daemon could actually run.

Usage:
    uv run python scripts/crypto_blend_study.py
"""

from __future__ import annotations

import math
import os
import statistics
from datetime import datetime
from decimal import Decimal

import httpx

from quantbot.backtest import BacktestEngine, BenchmarkVariant, ComponentSwitches
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

PERIODS_PER_YEAR = 252
TREND_WINDOW = 100  # cycle 5's BTC trend rule, reused unchanged
COST_PER_SIDE = 0.0025  # crypto spreads are wider than equities; 25bps is realistic


def _fetch_crypto(symbol: str, start: str) -> dict[str, float]:
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
    }
    closes: dict[str, float] = {}
    token = None
    while True:
        params = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": start,
            "limit": "10000",
            "sort": "asc",
        }
        if token:
            params["page_token"] = token
        payload = httpx.get(
            "https://data.alpaca.markets/v1beta3/crypto/us/bars",
            params=params, headers=headers, timeout=60,
        ).json()
        for row in payload.get("bars", {}).get(symbol, []):
            closes[row["t"][:10]] = float(row["c"])
        token = payload.get("next_page_token")
        if not token:
            break
    return closes


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    dev = statistics.pstdev(returns)
    return 0.0 if dev == 0 else statistics.fmean(returns) / dev * math.sqrt(PERIODS_PER_YEAR)


def _drawdown(returns: list[float]) -> float:
    equity, peak, worst = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def _total(returns: list[float]) -> float:
    return math.prod(1 + r for r in returns) - 1


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    x, y = a[:n], b[:n]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in x))
    dy = math.sqrt(sum((v - my) ** 2 for v in y))
    return 0.0 if dx == 0 or dy == 0 else num / (dx * dy)


def main() -> int:
    config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)
    database = Database("research/bars.db")
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {s: repository.list_bars(symbol=s, provider=key) for s in config.universe}
    database.close()
    stamps = set.intersection(*({b.timestamp for b in v} for v in raw.values()))
    histories = {s: [b for b in raw[s] if b.timestamp in stamps] for s in config.universe}

    # Equity sleeve: the best live-expressible configuration measured so far.
    switches = ComponentSwitches(
        momentum=True, asset_trend=True, market_regime=False, donchian_entry=False,
        donchian_exit=False, atr_risk=True, trailing_stop=False, roster_exit=True,
    )
    equity_curve = [
        float(p.equity)
        for p in BacktestEngine(config, initial_cash=Decimal("1000")).run(
            histories, BenchmarkVariant.FULL_STRATEGY, component_switches=switches
        ).equity_curve
    ]
    equity_dates = [b.timestamp for b in histories["SPY"]]

    btc = _fetch_crypto("BTC/USD", "2021-01-01T00:00:00Z")
    print(f"BTC daily closes fetched: {len(btc)}  "
          f"{min(btc) if btc else 'n/a'} .. {max(btc) if btc else 'n/a'}")
    if len(btc) < TREND_WINDOW + 60:
        print("insufficient BTC history")
        return 1

    # BTC trend sleeve on its own calendar, then sampled onto equity session dates.
    days = sorted(btc)
    position = 0.0
    btc_equity = {days[TREND_WINDOW - 1]: 1.0}
    level = 1.0
    for i in range(TREND_WINDOW, len(days)):
        window = [btc[d] for d in days[i - TREND_WINDOW : i]]
        average = statistics.fmean(window)
        target = 1.0 if btc[days[i - 1]] > average else 0.0
        cost = abs(target - position) * COST_PER_SIDE
        step = btc[days[i]] / btc[days[i - 1]] - 1
        level *= 1 + position * step - cost
        position = target
        btc_equity[days[i]] = level

    # Align: only equity sessions that fall inside the BTC series.
    common = [d for d in equity_dates if d.strftime("%Y-%m-%d") in btc_equity]
    if len(common) < 200:
        print("insufficient overlap between equity sessions and BTC history")
        return 1
    start_index = equity_dates.index(common[0])
    eq_levels = equity_curve[start_index : start_index + len(common)]
    btc_levels = [btc_equity[d.strftime("%Y-%m-%d")] for d in common]
    eq_ret = [eq_levels[i] / eq_levels[i - 1] - 1 for i in range(1, len(eq_levels))]
    btc_ret = [btc_levels[i] / btc_levels[i - 1] - 1 for i in range(1, len(btc_levels))]
    n = min(len(eq_ret), len(btc_ret))
    eq_ret, btc_ret = eq_ret[:n], btc_ret[:n]

    years = n / PERIODS_PER_YEAR
    print(f"overlap: {n} equity sessions ({years:.1f} years), "
          f"{common[0].date()} .. {common[min(len(common), n)-1].date()}")
    print(f"correlation(equity sleeve, BTC trend) = {_correlation(eq_ret, btc_ret):+.3f}")
    print(f"crypto cost assumption: {COST_PER_SIDE * 10000:.0f}bps per side\n")

    print(f"  {'blend':>22}{'total':>10}{'CAGR':>9}{'Sharpe':>8}{'maxDD':>8}")
    print("  " + "-" * 55)
    best = None
    for weight in (0.0, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0):
        blend = [(1 - weight) * eq_ret[i] + weight * btc_ret[i] for i in range(n)]
        sharpe = _sharpe(blend)
        total = _total(blend)
        cagr = (1 + total) ** (1 / years) - 1 if years > 0 else 0.0
        label = f"{(1-weight)*100:.0f}% eq / {weight*100:.0f}% BTC"
        print(f"  {label:>22}{total * 100:>9.1f}%{cagr * 100:>8.2f}%"
              f"{sharpe:>8.2f}{_drawdown(blend) * 100:>7.1f}%")
        if best is None or sharpe > best[1]:
            best = (weight, sharpe)
    base = _sharpe(eq_ret)
    print()
    print(f"  equity-only Sharpe {base:.2f}; best blend {best[0]*100:.0f}% BTC "
          f"at Sharpe {best[1]:.2f}  (delta {best[1] - base:+.2f})")

    # A raw Sharpe comparison is not a verdict. The best of several weights is a selected
    # maximum, and at this sample size the standard error swamps the difference.
    standard_error = math.sqrt(PERIODS_PER_YEAR / n)
    sigmas = (best[1] - base) / standard_error if standard_error else 0.0
    print(f"  standard error on Sharpe at n={n}: {standard_error:.3f}"
          f"  ->  improvement is {sigmas:.2f} sigma, p ~ "
          f"{math.erfc(abs(sigmas) / math.sqrt(2)):.2f}")
    significant = abs(sigmas) >= 1.96
    verdict = "JUSTIFIED" if significant and best[0] > 0 else "NOT JUSTIFIED (inside noise)"
    print(f"  building the 24/7 architecture is {verdict} on expected-return grounds")
    print("  note: 24/7 trading still accumulates forward out-of-sample observations about")
    print("  45% faster (365 vs 252 days/yr), which is a data-velocity argument this test")
    print("  does not measure either way.")
    print(f"\n  generated {datetime.now().isoformat(timespec='seconds')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
