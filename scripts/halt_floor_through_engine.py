"""What the drawdown-halt floor actually does, through the engine that would run it.

`STATUS.md` carries this as the standing severity-1: once equity is 15% below its high-water
mark `entry_halted` blocks new positions, but a strategy that cannot open positions cannot earn
back the drawdown. The halt is triggered by a loss and then forbids the only mechanism that
could undo it. `drawdown_halt_floor_bps` is built, defaults to 0 (off), and deploying it is an
operator decision because it changes live behaviour and restarts the qualification window.

That decision has been blocked on evidence, and the evidence was missing for a specific reason:
`scripts/halt_policy_study.py` is a standalone simulation that never calls `BacktestEngine`.
Commit `f77ea08` records what that costs — a standalone halt study concluded a floor turned $100
into $252.43 against $126.80, deterministic and reproducible and **wrong about this system**,
because it measured the policy against SPY-200-day returns while the engine runs the momentum
rotation.

So this measures the same switch through `BacktestEngine`, with the deployed config's own
component selection applied, on the SIP window. Under #8's rules the standalone study is a
`RESEARCH_SCRIPT` result and this is a `PRODUCTION_ENGINE` one; only the second is about Elrond.

Classification `EXPLORATORY`: the window is consumed, this carries no registration and runs no
adversarial probes. It is a measurement of a *mechanism the operator must decide about*, not a
test of a hypothesis, and it spends no statistical budget because it searches nothing.

Usage:
    QUANTBOT_MARKET_DATA_FEED=sip uv run python scripts/halt_floor_through_engine.py \
        ../../../research/bars.db 100
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import (
    BacktestEngine,
    BacktestResult,
    BenchmarkVariant,
    switches_for_config,
)
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import configuration_hash, load_strategy_config

#: The value `STATUS.md` says was chosen from the standalone study. Measured here rather than
#: assumed, alongside a shallower and a deeper one so the shape is visible instead of one point.
FLOORS_BPS: tuple[int, ...] = (0, 1000, 2500, 5000)


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * Decimal('100'):.2f}%"


def _ratio(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    cash = Decimal(sys.argv[2]) if len(sys.argv) > 2 else Decimal("100")

    config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)

    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {s: repository.list_bars(symbol=s, provider=key) for s in config.universe}
    database.close()

    missing = sorted(symbol for symbol, bars in raw.items() if not bars)
    if missing:
        print(f"no research bars for: {', '.join(missing)}")
        return 1

    common = set.intersection(*({bar.timestamp for bar in bars} for bars in raw.values()))
    histories = {s: [b for b in v if b.timestamp in common] for s, v in raw.items()}
    sessions = sorted(common)

    switches = switches_for_config(config.components)
    print(f"strategy      {config.strategy_name} {config.version}")
    print(f"config hash   {configuration_hash(config)[:16]}")
    print(f"sessions      {len(sessions)}  {sessions[0].date()} .. {sessions[-1].date()}")
    print(f"thresholds    {config.drawdown_thresholds_bps} bps")
    print(f"deployed floor {config.drawdown_halt_floor_bps} bps (0 = the absorbing halt)")
    print()

    header = (
        f"{'halt floor':>12}{'total':>10}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}"
        f"{'trades':>8}{'exposure':>10}{'final $':>10}"
    )
    print(header)
    print("-" * len(header))

    results: dict[int, BacktestResult] = {}
    for floor in FLOORS_BPS:
        # The only field that differs. Everything else is the deployed configuration.
        variant_config = config.model_copy(update={"drawdown_halt_floor_bps": floor})
        engine = BacktestEngine(variant_config, initial_cash=cash)
        result = engine.run(
            histories, BenchmarkVariant.FULL_STRATEGY, component_switches=switches
        )
        results[floor] = result
        metrics = result.metrics
        label = "0 (absorbing)" if floor == 0 else f"{floor}"
        print(
            f"{label:>12}{_pct(metrics.total_return):>10}{_pct(metrics.cagr_equivalent):>8}"
            f"{_pct(metrics.maximum_drawdown):>8}{_ratio(metrics.sharpe):>8}"
            f"{metrics.winners + metrics.losers:>8}{_pct(metrics.exposure_fraction):>10}"
            f"{float(cash) * (1 + float(metrics.total_return or 0)):>10.2f}"
        )

    baseline = results[0].metrics
    print()
    print("The decision this informs is capital safety, not return. `REFUTED.md` records that")
    print("releasing a strategy with no edge simply exposes more capital to it, so a floor that")
    print("raises exposure and lowers Sharpe is the expected and acceptable outcome -- the")
    print("justification is that a 15% drawdown must not lock the account out permanently.")
    print()
    print(f"baseline (absorbing): {_pct(baseline.cagr_equivalent)} CAGR, "
          f"Sharpe {_ratio(baseline.sharpe)}, {_pct(baseline.exposure_fraction)} exposure, "
          f"{baseline.winners + baseline.losers} trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
