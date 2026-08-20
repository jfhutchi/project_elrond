"""Cycle 6: does trend/momentum work better in crypto than in US equity ETFs?

Five cycles closed the equity ETF momentum family: no configuration beat buy-and-hold on
risk-adjusted return, and the exposure hypothesis was eliminated by proof. Crypto is the
one remaining asset class with an independent published evidence base (trend-following
Sharpe reported at 1.0-2.1) that is tradeable on this account.

This answers the research question only. Live crypto trading would additionally need a 24/7
session model, which the entire system currently lacks — that work is deliberately deferred
until the evidence justifies it.

Same simplified simulator as cycle 3, which was validated against the production Decimal
engine to within 0.021 Sharpe. Crypto trades every day, so the annualisation factor is 365.

Usage:
    uv run python scripts/crypto_study.py
"""

from __future__ import annotations

import math
import os
import statistics
from collections.abc import Callable, Sequence

import httpx

BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
PERIODS_PER_YEAR = 365  # crypto trades continuously
COST_PER_UNIT_TURNOVER = 0.0020  # 20bps per side: crypto spreads are wider than equities
START = "2021-01-01T00:00:00Z"

CANDIDATES = (
    "BTC/USD", "ETH/USD", "LTC/USD", "BCH/USD", "LINK/USD", "UNI/USD",
    "AAVE/USD", "AVAX/USD", "DOT/USD", "SOL/USD", "XTZ/USD", "MKR/USD",
)


def fetch(symbol: str, headers: dict[str, str]) -> dict[str, float]:
    """One symbol at a time with pagination; a shared limit silently truncates symbols."""
    closes: dict[str, float] = {}
    token: str | None = None
    while True:
        params = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": START,
            "limit": "10000",
            "sort": "asc",
        }
        if token:
            params["page_token"] = token
        response = httpx.get(BARS_URL, params=params, headers=headers, timeout=120)
        if response.status_code != 200:
            return {}
        payload = response.json()
        for row in payload.get("bars", {}).get(symbol) or []:
            closes[row["t"][:10]] = float(row["c"])
        token = payload.get("next_page_token")
        if not token:
            return closes


def _sharpe(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    return 0.0 if deviation == 0 else (
        statistics.fmean(returns) / deviation * math.sqrt(PERIODS_PER_YEAR)
    )


def _interval(returns: Sequence[float]) -> tuple[float, float, float]:
    sharpe = _sharpe(returns)
    per_period = sharpe / math.sqrt(PERIODS_PER_YEAR)
    error = math.sqrt((1 + per_period**2 / 2) / len(returns)) * math.sqrt(PERIODS_PER_YEAR)
    return sharpe, sharpe - 1.96 * error, sharpe + 1.96 * error


def _drawdown(returns: Sequence[float]) -> float:
    equity, peak, worst = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def simulate(
    weights_at: Callable[[int], dict[str, float]],
    symbols: list[str],
    series: dict[str, list[float]],
    start: int,
    end: int,
    cadence: int,
) -> list[float]:
    held: dict[str, float] = {}
    daily: list[float] = []
    for index in range(start, end):
        target = weights_at(index) if (index - start) % cadence == 0 else held
        turnover = sum(abs(target.get(s, 0.0) - held.get(s, 0.0)) for s in symbols)
        period = -turnover * COST_PER_UNIT_TURNOVER
        for symbol, weight in target.items():
            previous, current = series[symbol][index], series[symbol][index + 1]
            if previous > 0:
                period += weight * (current / previous - 1)
        daily.append(period)
        held = target
    return daily


def main() -> int:
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
    }
    fetched = {symbol: fetch(symbol, headers) for symbol in CANDIDATES}
    latest = max((max(v) for v in fetched.values() if v), default="")
    usable: dict[str, dict[str, float]] = {}
    for symbol, values in fetched.items():
        if len(values) <= 1200:
            reason = "SKIPPED (short history)"
        elif max(values) < latest[:8] + "01":
            # Stopped reporting well before the others: delisted or halted. Intersecting
            # dates with it would silently truncate every other symbol's recent history.
            reason = f"SKIPPED (ends {max(values)}, delisted)"
        else:
            usable[symbol] = values
            reason = "used"
        print(f"  {symbol:10} {len(values):5} bars  {reason}")
    if len(usable) < 4:
        print("insufficient crypto history for a universe study")
        return 1

    dates = sorted(set.intersection(*(set(v) for v in usable.values())))
    symbols = sorted(usable)
    series = {s: [usable[s][d] for d in dates] for s in symbols}
    print(f"\nuniverse {len(symbols)}  sessions {len(dates)}  {dates[0]} .. {dates[-1]}")

    lookback, hold = 90, 3
    trend_window = 100
    start, end = max(lookback, trend_window) + 1, len(dates) - 1

    def momentum(index: int) -> dict[str, float]:
        """90-day absolute momentum, hold the strongest positive names, else cash."""
        scored = []
        for symbol in symbols:
            past = series[symbol][index - lookback]
            if past > 0:
                score = series[symbol][index] / past - 1
                if score > 0:
                    scored.append((score, symbol))
        picks = [s for _, s in sorted(scored, reverse=True)[:hold]]
        return {s: 1 / len(picks) for s in picks} if picks else {}

    def trend_btc(index: int) -> dict[str, float]:
        """Absolute trend on BTC alone: hold when above its 100-day average."""
        window = series["BTC/USD"][index - trend_window + 1 : index + 1]
        return {"BTC/USD": 1.0} if series["BTC/USD"][index] > statistics.fmean(window) else {}

    def buy_hold(_index: int) -> dict[str, float]:
        return {"BTC/USD": 1.0}

    strategies = {
        "crypto momentum": (momentum, 7),
        "BTC trend (100d)": (trend_btc, 7),
        "BTC buy & hold": (buy_hold, 10**9),
    }
    results = {
        name: simulate(fn, symbols, series, start, end, cadence)
        for name, (fn, cadence) in strategies.items()
    }

    print()
    print(f"  {'strategy':20}{'total':>11}{'Sharpe':>8}{'maxDD':>9}   {'95% CI':>18}")
    for name, returns in results.items():
        total = math.prod(1 + value for value in returns) - 1
        sharpe, low, high = _interval(returns)
        print(f"  {name:20}{total * 100:>10.1f}%{sharpe:>8.2f}{_drawdown(returns) * 100:>8.1f}%"
              f"   [{low:>6.2f}, {high:>5.2f}]")

    print()
    print("Reference — US equity ETFs, cycles 4/5 (production engine, 10.6y):")
    print("  ensemble Sharpe 0.80-0.82, SPY buy & hold Sharpe 0.90")
    print()
    print("NOTE 20bps per side assumed; crypto spreads vary and are wider than equities.")
    print("     Only 5.6 years available, one crypto cycle. Survivorship is real: pairs")
    print("     with short history were dropped, and delisted pairs never appear at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
