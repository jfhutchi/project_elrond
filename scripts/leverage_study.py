"""What leverage actually does to the strategies we measured.

Leverage is the one honest lever left for return velocity once the search for edge has
failed. It is also the one most often described arithmetically and experienced
geometrically: levering daily returns by k does NOT multiply the compound result by k,
because volatility drag compounds against you. That gap only appears if you simulate the
path day by day, which is what this does.

Also reports what the literature calls the part people skip: the drawdown, the leverage at
which the account is wiped out entirely, and whether the configured 20% halt would have
fired.

Usage:
    uv run python scripts/leverage_study.py [DB_PATH]
"""

from __future__ import annotations

import math
import statistics
import sys
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import BacktestEngine, BenchmarkVariant, ComponentSwitches
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, load_strategy_config

PERIODS_PER_YEAR = 252
#: Retail margin costs money. Broadly current retail rates; the levered portion only.
ANNUAL_BORROW_RATE = 0.06
LEVERAGES = (1.0, 1.5, 2.0, 3.0)
HALT_THRESHOLD = 0.20  # the configured drawdown halt


def _levered_path(returns: list[float], leverage: float) -> tuple[list[float], bool]:
    """Compound the levered path day by day, charging borrow on the levered portion.

    Returns the equity path and whether the account was wiped out. A levered account that
    loses more than 1/leverage in a single session is gone; there is no recovery from zero,
    which is the asymmetry arithmetic on averages hides.
    """
    daily_borrow = ANNUAL_BORROW_RATE / PERIODS_PER_YEAR * max(leverage - 1.0, 0.0)
    equity = 1.0
    path = [1.0]
    for value in returns:
        equity *= 1 + leverage * value - daily_borrow
        if equity <= 0:
            return path + [0.0], True
        path.append(equity)
    return path, False


def _drawdown(path: list[float]) -> float:
    peak, worst = path[0], 0.0
    for value in path:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def _cagr(path: list[float], observations: int) -> float:
    years = observations / PERIODS_PER_YEAR
    if years <= 0 or path[-1] <= 0:
        return -1.0
    return path[-1] ** (1 / years) - 1


def _years_to_double(cagr: float) -> str:
    if cagr <= 0:
        return "never"
    return f"{math.log(2) / math.log(1 + cagr):.1f}y"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    base = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)
    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {s: repository.list_bars(symbol=s, provider=key) for s in base.universe}
    database.close()
    stamps = set.intersection(*({b.timestamp for b in v} for v in raw.values()))
    histories = {s: [b for b in raw[s] if b.timestamp in stamps] for s in base.universe}

    def curve_returns(curve: list[float]) -> list[float]:
        return [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0]

    engine = BacktestEngine(base, initial_cash=Decimal("1000"))
    series: dict[str, list[float]] = {}
    for label, variant in (
        ("SPY buy & hold", BenchmarkVariant.SPY_BUY_AND_HOLD),
        ("momentum + trend", BenchmarkVariant.MOMENTUM_TREND),
    ):
        series[label] = curve_returns(
            [float(p.equity) for p in engine.run(histories, variant).equity_curve]
        )

    sleeves = {
        "equity": (("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLB", "XLE", "XLF",
                    "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"), 3),
        "bonds": (("TLT", "IEF", "TIP", "LQD", "HYG"), 2),
        "real": (("DBC", "GLD", "VNQ"), 1),
    }
    switches = ComponentSwitches(
        momentum=True, asset_trend=False, market_regime=False, donchian_entry=False,
        donchian_exit=False, atr_risk=True, trailing_stop=False, roster_exit=True,
    )
    sleeve_series = []
    for universe, hold in sleeves.values():
        payload = base.model_dump(mode="python")
        payload.update({"version": "1.2.0", "universe": universe, "roster_size": hold})
        config = StrategyConfig.model_validate(payload)
        sleeve_series.append(curve_returns([
            float(p.equity)
            for p in BacktestEngine(config, initial_cash=Decimal("1000")).run(
                histories, BenchmarkVariant.FULL_STRATEGY, component_switches=switches
            ).equity_curve
        ]))
    length = min(len(s) for s in sleeve_series)
    series["sleeve ensemble"] = [
        statistics.fmean([s[i] for s in sleeve_series]) for i in range(length)
    ]

    print(f"$1000 start, {len(stamps)} sessions, borrow {ANNUAL_BORROW_RATE * 100:.0f}%/yr "
          f"on the levered portion\n")
    header = (f"  {'strategy':18}{'lev':>5}{'final':>13}{'CAGR':>9}{'maxDD':>9}"
              f"{'2x?':>6}{'halt?':>7}{'to double':>11}")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for name, returns in series.items():
        for leverage in LEVERAGES:
            path, wiped = _levered_path(returns, leverage)
            final = 1000 * path[-1]
            cagr = _cagr(path, len(returns))
            drawdown = _drawdown(path)
            halted = "YES" if drawdown >= HALT_THRESHOLD else "no"
            note = "WIPED" if wiped else ""
            print(f"  {name:18}{leverage:>4.1f}x${final:>12,.0f}{cagr * 100:>8.2f}%"
                  f"{drawdown * 100:>8.1f}%{note:>6}{halted:>7}{_years_to_double(cagr):>11}")
        print()

    print("  Growth-optimal leverage, searched empirically (ignores ruin risk entirely):")
    for name, returns in series.items():
        best, best_cagr = 1.0, -1.0
        step = 0.1
        probe = 0.1
        while probe <= 5.0:
            path, wiped = _levered_path(returns, probe)
            value = -1.0 if wiped else _cagr(path, len(returns))
            if value > best_cagr:
                best, best_cagr = probe, value
            probe += step
        path, _ = _levered_path(returns, best)
        print(f"    {name:18}{best:>4.1f}x  CAGR {best_cagr * 100:>6.2f}%  "
              f"drawdown {_drawdown(path) * 100:>5.1f}%")
    print()
    print("  Drawdown is the number that decides whether a path is survivable, not CAGR.")
    print("  A levered account that hits -100% on any single day is gone permanently;")
    print("  averages conceal that because there is no recovery from zero.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
