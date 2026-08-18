"""Measure realised execution cost from actual fills, so the cost model can stop being a guess.

The backtest charges 5bps of slippage. That number was assumed, never measured, and it is
load-bearing: REFUTED.md #8 and #9 both died on execution cost rather than on signal quality,
so an assumption that is wrong by a factor of five would have refuted ideas for the wrong
reason.

Two independent estimates now disagree with it, both far lower:

  * quoted NBBO, sampled 2026-08-17: SPY 0.26bps, universe median ~1.1bps
  * realised fills on this account: -0.3bps median against the opening auction

There is a structural reason to expect the low number rather than to distrust it. The strategy
evaluates on a completed close and submits DAY orders, so they execute in the opening auction,
where a single clearing price means there is no spread to cross.

This script does NOT change the cost model. Three fills cannot justify that, and lowering an
assumed cost is the direction that flatters every backtest — precisely the move this project
has learned to distrust. It exists so the evidence accumulates until the recalibration is
defensible, and it reports the sample size next to the estimate so nobody reads a median of
three as settled.

Measured against the OPEN, not the prior close. The gap from the prior close is overnight market
movement, which is noise the strategy is exposed to by design, not a cost of trading.

Usage:
    uv run python scripts/slippage_report.py
"""

from __future__ import annotations

import os
import statistics
from decimal import Decimal

import httpx

TRADING = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2"
ASSUMED_BPS = 5.0
#: Below this the median is reported but must not be used to justify a config change. A handful
#: of fills in one market regime says almost nothing about execution cost in another.
CREDIBLE_SAMPLE = 30


def _headers() -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
    }


def main() -> int:
    headers = _headers()
    orders = httpx.get(
        f"{TRADING}/orders", headers=headers,
        params={"status": "all", "limit": 500}, timeout=60,
    ).json()
    filled = [o for o in orders if o["status"] == "filled" and o.get("filled_avg_price")]

    print(f"filled orders on the account: {len(filled)}")
    if not filled:
        print("no fills yet — nothing to measure")
        return 0

    print()
    print(f"{'symbol':6} {'side':5} {'fill':>10} {'open':>10} {'slippage':>11}")
    slippage: list[float] = []
    for order in sorted(filled, key=lambda o: o["submitted_at"]):
        symbol = order["symbol"]
        day = order["filled_at"][:10]
        bars = httpx.get(
            f"{DATA}/stocks/{symbol}/bars", headers=headers,
            params={"timeframe": "1Day", "start": day, "end": day, "feed": "sip"},
            timeout=60,
        ).json().get("bars") or []
        if not bars:
            continue
        fill = Decimal(order["filled_avg_price"])
        open_price = Decimal(str(bars[-1]["o"]))
        # Signed so a cost is positive: paying above the open to buy, below it to sell.
        drift = (fill - open_price) / open_price * 10_000
        cost = float(drift if order["side"] == "buy" else -drift)
        slippage.append(cost)
        print(f"{symbol:6} {order['side']:5} {fill:10} {open_price:10} {cost:10.1f}bp")

    if not slippage:
        print("\nno fills could be matched to an opening price")
        return 0

    median = statistics.median(slippage)
    print()
    print(f"  measured median execution cost  {median:+.2f} bps   (n={len(slippage)})")
    print(f"  assumed in the backtest         {ASSUMED_BPS:+.2f} bps")
    if len(slippage) >= 2:
        print(f"  spread of observations          {min(slippage):+.1f} to {max(slippage):+.1f} bps")

    print()
    if len(slippage) < CREDIBLE_SAMPLE:
        print(f"  NOT ENOUGH EVIDENCE to change the cost model. Needs n>={CREDIBLE_SAMPLE};")
        print(f"  have {len(slippage)}. The direction agrees with the quoted-spread measurement,")
        print("  but a median of a handful of fills in one regime is not a calibration.")
    else:
        print("  Sample is large enough to propose a recalibration. Do it as a pre-registered")
        print("  change with a new strategy version, not by editing the assumption in place —")
        print("  lowering an assumed cost improves every historical result at once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
