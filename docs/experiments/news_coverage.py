"""Does the primary news source cover the universe Elrond actually trades? (#56, #83 Part B)

`NO_PUBLIC_CATALYST_FOUND` is defined as a *finding*: every source answered and none had
anything, which for unusual activity is the interesting case. That definition assumes the source
would have had something if something existed.

Elrond's universe is ETFs. Alpaca's news feed is Benzinga, whose tagging is overwhelmingly
company-level. If ETF coverage is sparse, then "no public catalyst" for XLU is a fact about
Benzinga's tagging rather than about the world -- and the enrichment would emit a systematically
false finding, constantly, for the entire universe.

This measures coverage before any second provider is considered, because "add MarketAux" is only
a sensible response to a coverage number, and the number might instead say that catalyst
enrichment is inapplicable to an ETF universe at all.
"""

from __future__ import annotations

import json
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

# The v1-3 universe, plus two large single names as a control. Without the control a low ETF
# count could equally mean "the window was quiet" or "the credentials are limited"; the control
# separates a coverage limit from an outage.
UNIVERSE = [
    "SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "VNQ",
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV",
]
CONTROL = ["AAPL", "NVDA"]

END = datetime(2026, 8, 29, tzinfo=UTC)
START = END - timedelta(days=30)


def fetch(symbol: str) -> tuple[int, list[str]]:
    query = urllib.parse.urlencode(
        {
            "symbols": symbol,
            "start": START.isoformat().replace("+00:00", "Z"),
            "end": END.isoformat().replace("+00:00", "Z"),
            "limit": 50,
        }
    )
    request = urllib.request.Request(
        f"https://data.alpaca.markets/v1beta1/news?{query}",
        headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
            "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    items = payload.get("news", [])
    return len(items), [str(item.get("headline", ""))[:60] for item in items[:2]]


def main() -> int:
    print(f"window {START.date()} .. {END.date()} (30 days), limit 50/page\n")
    results: dict[str, int] = {}
    for group, symbols in (("universe", UNIVERSE), ("control", CONTROL)):
        print(f"--- {group} ---")
        for symbol in symbols:
            try:
                count, sample = fetch(symbol)
            except urllib.error.HTTPError as error:
                print(f"  {symbol:5} HTTP {error.code}")
                continue
            except Exception as error:  # noqa: BLE001
                print(f"  {symbol:5} failed: {error!r}")
                continue
            results[symbol] = count
            marker = "" if count else "   <- no items at all"
            print(f"  {symbol:5} {count:3} item(s){marker}")
            for line in sample:
                print(f"        {line}")
            time.sleep(0.4)  # deliberate: this is a coverage probe, not a load test
        print()

    covered = [results[s] for s in UNIVERSE if s in results]
    control = [results[s] for s in CONTROL if s in results]
    if covered:
        empty = sum(1 for value in covered if value == 0)
        print(
            f"universe: median {statistics.median(covered):.0f}, "
            f"mean {statistics.fmean(covered):.1f}, "
            f"{empty}/{len(covered)} symbols with zero items"
        )
    if control:
        print(f"control : median {statistics.median(control):.0f} (single names)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
