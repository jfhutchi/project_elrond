"""Cycle 10: does cross-sectional breadth improve momentum, tested without survivorship bias?

Pre-registered before running.

HYPOTHESIS. Grinold's fundamental law says information ratio scales with the square root of
breadth, so a cross-sectional momentum strategy ranking hundreds of independent names should
beat one ranking 23 ETFs whose nine US sectors correlate 0.85-0.95 with SPY. This is the
most principled untested direction in the project, and the one the academic momentum
literature actually used — Jegadeesh and Titman ranked thousands of stocks, not sector funds.

THE BIAS THIS EXISTS TO AVOID. Alpaca lists 19,194 delisted US equities against 14,233 live
ones. Testing only survivors omits every bankruptcy, delisting and failed acquisition, which
inflates returns by an amount that cannot be estimated after the fact. Verified that Alpaca
serves history for delisted names and that each series terminates at its delisting date, so
the graveyard is includable.

POINT-IN-TIME MEMBERSHIP. A symbol is eligible on a date only if it actually traded on that
date and has enough prior history for the lookback. A delisted name therefore participates
until it dies and then stops, exactly as it would have live. This is why the test cannot use
the production strategy, which requires a fixed universe fresh through every evaluation date.

SELECTION RULE, fixed in advance: compare against SPY buy-and-hold over the identical window
and require the Sharpe difference to clear 1.96 standard errors. Cumulative trials in this
project now stand near 45, so a raw comparison is not a verdict.

Usage:
    uv run python scripts/cycle10_breadth.py [SYMBOL_BUDGET]
"""

from __future__ import annotations

import math
import os
import random
import statistics
import sys
from collections import defaultdict

import httpx

PERIODS_PER_YEAR = 252
LOOKBACK = 252
SKIP = 21
HOLD = 20  # top names held, equal weight
REBALANCE = 21  # sessions
COST_PER_SIDE = 0.0005
MIN_PRICE = 5.0  # excludes penny-stock artefacts that dominate naive momentum screens
START = "2016-01-01T00:00:00Z"
END = "2026-08-15T00:00:00Z"
MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}


def _headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
    }


def _universe(budget: int) -> list[str]:
    """Half survivors, half delisted, both from major exchanges only."""
    picked: list[str] = []
    for status in ("active", "inactive"):
        assets = httpx.get(
            "https://paper-api.alpaca.markets/v2/assets",
            params={"status": status, "asset_class": "us_equity"},
            headers=_headers(), timeout=180,
        ).json()
        eligible = [
            a["symbol"] for a in assets
            if a.get("exchange") in MAJOR_EXCHANGES and "/" not in a["symbol"]
            and len(a["symbol"]) <= 5 and a["symbol"].isalpha()
        ]
        random.seed(11 if status == "active" else 13)
        picked.extend(random.sample(eligible, min(budget // 2, len(eligible))))
    return sorted(set(picked) | {"SPY"})


def _bars(symbols: list[str]) -> dict[str, dict[str, float]]:
    """Fetch daily closes, paginating to exhaustion in batches."""
    closes: dict[str, dict[str, float]] = defaultdict(dict)
    batch_size = 200
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start : start + batch_size]
        token = None
        while True:
            params = {
                "symbols": ",".join(batch), "timeframe": "1Day", "start": START, "end": END,
                "feed": "sip", "limit": "10000", "sort": "asc",
            }
            if token:
                params["page_token"] = token
            payload = httpx.get(
                "https://data.alpaca.markets/v2/stocks/bars",
                params=params, headers=_headers(), timeout=300,
            ).json()
            for symbol, rows in (payload.get("bars") or {}).items():
                for row in rows:
                    closes[symbol][row["t"][:10]] = float(row["c"])
            token = payload.get("next_page_token")
            if not token:
                break
        print(f"  fetched {min(start + batch_size, len(symbols))}/{len(symbols)} symbols")
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


def main() -> int:
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    symbols = _universe(budget)
    print(f"universe requested: {len(symbols)} symbols (half survivors, half delisted)")
    closes = _bars(symbols)
    have = {s: d for s, d in closes.items() if len(d) > LOOKBACK + SKIP + REBALANCE}
    print(f"symbols with usable history: {len(have)}")
    if "SPY" not in have:
        print("SPY missing; cannot benchmark")
        return 1

    dates = sorted(have["SPY"])
    delisted = sum(1 for s, d in have.items() if max(d) < dates[-1])
    print(f"of those, {delisted} stop trading before the end — the graveyard is present")

    start_index = LOOKBACK + SKIP
    strat_returns: list[float] = []
    spy_returns: list[float] = []
    held: dict[str, float] = {}
    eligible_counts: list[int] = []

    for index in range(start_index, len(dates) - 1):
        today, tomorrow = dates[index], dates[index + 1]
        if (index - start_index) % REBALANCE == 0:
            scored: list[tuple[float, str]] = []
            for symbol, series in have.items():
                if symbol == "SPY":
                    continue
                # Point-in-time: must trade today, and have priced history at both ends of
                # the momentum window. A delisted name simply stops qualifying.
                past_i = dates[index - LOOKBACK]
                skip_i = dates[index - SKIP]
                if today not in series or past_i not in series or skip_i not in series:
                    continue
                if series[today] < MIN_PRICE:
                    continue
                scored.append((series[skip_i] / series[past_i] - 1, symbol))
            eligible_counts.append(len(scored))
            picks = [s for _, s in sorted(scored, reverse=True)[:HOLD]]
            target = {s: 1 / len(picks) for s in picks} if picks else {}
        else:
            target = held

        turnover = sum(
            abs(target.get(s, 0.0) - held.get(s, 0.0)) for s in set(target) | set(held)
        )
        gross = 0.0
        for symbol, weight in target.items():
            series = have[symbol]
            if today in series and tomorrow in series and series[today] > 0:
                gross += weight * (series[tomorrow] / series[today] - 1)
        strat_returns.append(gross - turnover * COST_PER_SIDE)
        spy = have["SPY"]
        spy_returns.append(
            spy[tomorrow] / spy[today] - 1 if today in spy and tomorrow in spy else 0.0
        )
        held = target

    years = len(strat_returns) / PERIODS_PER_YEAR
    print(f"\nsimulated {dates[start_index]} .. {dates[-1]}  ({years:.1f} years)")
    print(f"mean eligible names per rebalance: {statistics.fmean(eligible_counts):.0f}")
    print(f"holding top {HOLD}, rebalanced every {REBALANCE} sessions, "
          f"{COST_PER_SIDE * 10000:.0f}bps/side, min price ${MIN_PRICE:.0f}\n")

    def report(label: str, returns: list[float]) -> tuple[float, float]:
        total = math.prod(1 + r for r in returns) - 1
        cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
        sharpe = _sharpe(returns)
        print(f"  {label:22}{total * 100:>10.1f}%{cagr * 100:>8.2f}%{sharpe:>8.2f}"
              f"{_drawdown(returns) * 100:>8.1f}%")
        return sharpe, cagr

    print(f"  {'strategy':22}{'total':>11}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}")
    print("  " + "-" * 57)
    wide_sharpe, _ = report(f"wide momentum ({len(have)})", strat_returns)
    spy_sharpe, _ = report("SPY buy & hold", spy_returns)

    se = math.sqrt(PERIODS_PER_YEAR / len(strat_returns))
    sigmas = (wide_sharpe - spy_sharpe) / se
    print(f"\n  standard error {se:.3f}  ->  difference {wide_sharpe - spy_sharpe:+.2f} "
          f"= {sigmas:+.2f} sigma, p ~ {math.erfc(abs(sigmas) / math.sqrt(2)):.3f}")
    verdict = "BEATS SPY" if sigmas >= 1.96 else "NOT significant"
    print(f"  verdict: {verdict}")
    print("  ~45 cumulative trials in this project raise the bar further than 1.96 alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
