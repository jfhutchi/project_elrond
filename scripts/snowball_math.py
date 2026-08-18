"""The snowball question, answered with measured numbers rather than intuition.

Two distinct claims live inside "sell, take profits, reinvest, repeat":

1. Realising a gain and rebuying accelerates compounding. It does not, and this shows why:
   compounding is driven by the return rate, not by how often a gain is booked. Holding and
   round-tripping produce identical exposure, so the round trip differs only by its cost.

2. More trades per unit time means more compounding events. This one is TRUE, and is the real
   version of the idea — but each round trip must clear its spread first. That turns the
   question into an arithmetic one with a measurable answer.

Costs are the real NBBO measured on 2026-08-17, not assumed: SPY 0.26bps, median ~1.1bps.

Usage:
    uv run python scripts/snowball_math.py
"""

from __future__ import annotations

SPY_SPREAD_BPS = 0.26
MEDIAN_SPREAD_BPS = 1.10
SESSIONS = 252


def compounded(rate: float, periods: int, start: float = 100.0) -> float:
    return start * (1 + rate) ** periods


def main() -> int:
    print("=" * 88)
    print("CLAIM 1 — does selling and rebuying accelerate the snowball?")
    print("=" * 88)
    daily = 0.15 / SESSIONS  # ~15% a year, the measured SPY rate
    print(f"  A position earning {daily * 100:.4f}% a session, over one year:\n")
    held = compounded(daily, SESSIONS)
    print(f"  {'hold, never realise':38} ${held:8.2f}")
    for label, bps in (("round-trip daily, SPY spread", SPY_SPREAD_BPS),
                       ("round-trip daily, median universe", MEDIAN_SPREAD_BPS)):
        net = compounded(daily - bps / 10_000, SESSIONS)
        print(f"  {label:38} ${net:8.2f}   {net - held:+7.2f}")
    print("\n  Identical growth, minus the spread paid each round trip. Realising a gain does")
    print("  not compound it — the gain is already compounding while the position is held,")
    print("  because the next position is sized as a percentage of the larger equity.")

    print("\n" + "=" * 88)
    print("CLAIM 2 — more trades DOES mean more compounding, if each one clears its cost")
    print("=" * 88)
    print("  Break-even edge: what each trade must earn, on average, just to pay the spread.\n")
    print(f"  {'trades/session':>15} {'trades/year':>12} {'break-even @0.26bps':>20} "
          f"{'break-even @1.1bps':>20}")
    for per_session in (1 / 21, 1 / 5, 1, 4, 20, 100):
        per_year = per_session * SESSIONS
        label = f"{per_session:.3f}" if per_session < 1 else f"{per_session:.0f}"
        print(f"  {label:>15} {per_year:12,.0f} {SPY_SPREAD_BPS:19.2f}bp "
              f"{MEDIAN_SPREAD_BPS:19.2f}bp")

    print("\n  The break-even per trade does not change with frequency — but the TOTAL cost")
    print("  does, and it is what has to be beaten:\n")
    print(f"  {'trades/year':>12} {'annual cost @0.26bps':>22} {'annual cost @1.1bps':>21}")
    for per_year in (12, 50, 252, 1_008, 5_040, 25_200):
        print(f"  {per_year:12,} {per_year * SPY_SPREAD_BPS / 100:21.2f}% "
              f"{per_year * MEDIAN_SPREAD_BPS / 100:20.2f}%")

    print("\n" + "=" * 88)
    print("WHAT THE TARGET ACTUALLY REQUIRES")
    print("=" * 88)
    for label, days in (("double in 3 days", 3), ("double in 30 days", 30),
                        ("double in 1 year", 252)):
        rate = 2 ** (1 / days) - 1
        print(f"\n  {label}: {rate * 100:.3f}% NET per session")
        for per_session, tag in ((1, "1 trade/session"), (20, "20 trades/session")):
            per_trade = (1 + rate) ** (1 / per_session) - 1
            gross = per_trade + MEDIAN_SPREAD_BPS / 10_000
            print(f"    at {tag:18} each trade must net {per_trade * 100:7.3f}% "
                  f"(gross {gross * 100:7.3f}%)")

    print("\n" + "=" * 88)
    print("THE MEASURED COMPARISON")
    print("=" * 88)
    print("  Best per-trade edge this project has ever measured, from REFUTED.md #8:")
    print("    1-day reversal, +427.9% GROSS over the research history — the strongest raw")
    print("    signal found — and it nets -28.8% at 5bps. It was retested down to an")
    print("    unachievable 0.5bps and still never beat the slow signal.")
    print()
    print("  That is the whole difficulty in one line: the edge was real and large, and the")
    print("  frequency needed to harvest it destroyed it. Trading more often multiplies the")
    print("  cost with certainty and the edge only on average.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
