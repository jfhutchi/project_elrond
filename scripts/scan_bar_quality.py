"""Report bars whose extremes their own open and close do not corroborate.

`validate_bar_batch` now rejects these at ingestion, but that only protects data fetched from now
on. Every bar already cached predates the check, so this scans an existing database and reports
what is in it. It reports; it does not modify. Repairing a bar means re-fetching it from the
provider, which is an operator action -- a backtest silently running on a value this tool invented
would be worse than one running on the provider's error.

Found on the research history 2026-08-19: SPY 2026-02-02, O=685.90 H=693.21 L=68.64 C=691.70. One
bar in 96,370. It cost 20.1% of equity in a single session of a SPY-only backtest by tripping a
stop at a price no trade ever occurred at, on a day SPY closed up.

Usage:
    uv run python scripts/scan_bar_quality.py ../../../research/bars.db
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

from quantbot.market_data.validation import IMPLAUSIBLE_HIGH_RATIO, IMPLAUSIBLE_LOW_RATIO


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    if not path.exists():
        print(f"no database at {path}")
        return 1

    connection = sqlite3.connect(path)
    rows = connection.execute(
        "SELECT symbol, timestamp, provider, open, high, low, close FROM bars"
    ).fetchall()
    connection.close()

    flagged = []
    closest = None
    for symbol, timestamp, provider, raw_open, high, low, close in rows:
        values = [Decimal(str(value)) for value in (raw_open, high, low, close)]
        bar_open, bar_high, bar_low, bar_close = values
        if bar_open <= 0 or bar_close <= 0:
            continue
        ratio = bar_low / min(bar_open, bar_close)
        if bar_low < IMPLAUSIBLE_LOW_RATIO * min(bar_open, bar_close) or (
            bar_high > IMPLAUSIBLE_HIGH_RATIO * max(bar_open, bar_close)
        ):
            flagged.append((symbol, str(timestamp)[:10], provider, values, ratio))
        elif closest is None or ratio < closest[1]:
            closest = ((symbol, str(timestamp)[:10]), ratio)

    print(f"scanned {len(rows)} bars in {path}")
    print(f"flagged {len(flagged)}")
    for symbol, day, provider, (o, h, low, c), ratio in flagged:
        print(
            f"  {symbol:6} {day}  O={o:>9} H={h:>9} L={low:>9} C={c:>9}"
            f"  low/min(open,close)={ratio:.4f}  {provider}"
        )
    if closest is not None:
        (symbol, day), ratio = closest
        print(
            f"\nclosest unflagged bar: {symbol} {day} at ratio {ratio:.4f} "
            f"(threshold {IMPLAUSIBLE_LOW_RATIO}) -- the margin between real data and the "
            f"threshold, which is what keeps this from firing on volatile names"
        )
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
