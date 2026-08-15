"""Walk-forward, ablation, neighborhood and holdout study over the research history.

Discipline enforced here:

* The holdout is the final slice of history and is never consulted while choosing a
  candidate. Exactly one candidate is evaluated on it, once, at the end.
* Candidates are compared on per-fold windows, not on one aggregate number, so a result
  driven by a single lucky stretch is visible rather than hidden.
* The selection rule is declared before results are seen: highest median fold Sharpe,
  ties broken by lower median maximum drawdown.
* Every input is hashed into an immutable manifest.

Usage:
    uv run python scripts/run_research_experiment.py [DB_PATH] [INITIAL_CASH]
"""

from __future__ import annotations

import statistics
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import (
    BacktestEngine,
    BacktestMetrics,
    BenchmarkVariant,
    ComponentSwitches,
    EquityObservation,
    ExperimentManifest,
    ExperimentPeriod,
    TradeOutcome,
    calculate_metrics,
    component_switches_for,
    write_experiment_manifest,
)
from quantbot.domain import Bar
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, bar_set_hash, configuration_hash, load_strategy_config

DEFAULT_DB = Path("research") / "bars.db"
HOLDOUT_FRACTION = Decimal("0.20")
FOLDS = 3

#: Components switched off one at a time, from the full strategy.
ABLATIONS = (
    "asset_trend",
    "market_regime",
    "donchian_entry",
    "donchian_exit",
    "atr_risk",
    "trailing_stop",
    "roster_exit",
)

#: One-at-a-time parameter neighbourhood around the shipped values.
NEIGHBOURHOOD: dict[str, tuple[int, ...]] = {
    "momentum_long": (189, 252, 315),
    "entry_period": (34, 55, 89),
    "exit_period": (13, 20, 34),
    "trend_period": (150, 200, 250),
    "roster_size": (5, 10, 15),
}


class Candidate:
    def __init__(
        self,
        name: str,
        config: StrategyConfig,
        switches: ComponentSwitches,
        variant: BenchmarkVariant = BenchmarkVariant.FULL_STRATEGY,
    ) -> None:
        self.name = name
        self.config = config
        self.switches = switches
        self.variant = variant
        self.equity: tuple[EquityObservation, ...] = ()
        self.trades: tuple[TradeOutcome, ...] = ()


def _window_metrics(
    equity: Sequence[EquityObservation],
    trades: Sequence[TradeOutcome],
    start: datetime,
    end: datetime,
) -> BacktestMetrics | None:
    """Metrics restricted to one window, priced off the equity at the window's open."""
    inside = [point for point in equity if start <= point.timestamp <= end]
    if len(inside) < 2:
        return None
    opening = [point for point in equity if point.timestamp <= start]
    initial = opening[-1].equity if opening else inside[0].equity
    if initial <= 0:
        return None
    closed = tuple(trade for trade in trades if start <= trade.closed_at <= end)
    return calculate_metrics(
        tuple(inside),
        closed,
        initial_equity_override=initial,
        total_costs_override=sum((trade.costs for trade in closed), Decimal("0")),
    )


def _config_with(config: StrategyConfig, **updates: object) -> StrategyConfig | None:
    payload = config.model_dump(mode="python")
    payload.update(updates)
    try:
        return StrategyConfig.model_validate(payload)
    except ValueError:
        return None  # Combination violates a config invariant; skip it.


def _fmt(value: Decimal | None, places: int = 2, percent: bool = False) -> str:
    if value is None:
        return "n/a"
    scaled = value * Decimal("100") if percent else value
    return f"{scaled:.{places}f}{'%' if percent else ''}"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    initial_cash = Decimal(sys.argv[2]) if len(sys.argv) > 2 else Decimal("100")

    base_config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    provider_key = MarketDataCache.provider_key(
        feed.provider, feed.feed, feed.adjustment, feed.timeframe
    )
    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {
            symbol: repository.list_bars(symbol=symbol, provider=provider_key)
            for symbol in base_config.universe
        }
    database.close()
    if any(not bars for bars in raw.values()):
        print("research database is missing symbols; run fetch_research_bars.py first")
        return 1

    common = set.intersection(*({bar.timestamp for bar in bars} for bars in raw.values()))
    histories: dict[str, list[Bar]] = {
        symbol: [bar for bar in bars if bar.timestamp in common] for symbol, bars in raw.items()
    }
    sessions = sorted(common)
    warmup = base_config.momentum_long + base_config.momentum_skip
    if len(sessions) <= warmup + FOLDS * 30:
        print("not enough research history for a walk-forward study")
        return 1

    holdout_count = int(len(sessions) * HOLDOUT_FRACTION)
    holdout_start = sessions[-holdout_count]
    selection = sessions[warmup:-holdout_count]
    fold_size = len(selection) // FOLDS
    folds = [
        (selection[index * fold_size], selection[(index + 1) * fold_size - 1])
        for index in range(FOLDS)
    ]

    print(f"sessions        {len(sessions)}  {sessions[0].date()} .. {sessions[-1].date()}")
    print(f"warmup          {warmup} sessions (indicators undefined before this)")
    print(f"selection       {selection[0].date()} .. {selection[-1].date()} ({len(selection)})")
    for index, (start, end) in enumerate(folds, start=1):
        print(f"  fold {index}        {start.date()} .. {end.date()}")
    print(f"holdout         {holdout_start.date()} .. {sessions[-1].date()} ({holdout_count})")
    print(f"initial cash    ${initial_cash}")
    print()

    candidates: list[Candidate] = []
    for variant in BenchmarkVariant:
        candidates.append(
            Candidate(
                f"benchmark:{variant.value}",
                base_config,
                component_switches_for(variant),
                variant,
            )
        )
    for component in ABLATIONS:
        candidates.append(
            Candidate(
                f"ablate:{component}",
                base_config,
                ComponentSwitches.full().model_copy(update={component: False}),
            )
        )
    for parameter, values in NEIGHBOURHOOD.items():
        for value in values:
            if getattr(base_config, parameter) == value:
                continue
            varied = _config_with(base_config, **{parameter: value})
            if varied is None:
                continue
            candidates.append(
                Candidate(f"param:{parameter}={value}", varied, ComponentSwitches.full())
            )

    print(f"evaluating {len(candidates)} candidates over the selection region ...")
    for candidate in candidates:
        engine = BacktestEngine(candidate.config, initial_cash=initial_cash)
        result = engine.run(histories, candidate.variant, component_switches=candidate.switches)
        candidate.equity = result.equity_curve
        candidate.trades = result.trades

    header = (
        f"{'candidate':34}{'fold1 Sh':>10}{'fold2 Sh':>10}{'fold3 Sh':>10}"
        f"{'med Sh':>9}{'med DD':>9}{'med ret':>9}"
    )
    print()
    print(header)
    print("-" * len(header))
    scored: list[tuple[Decimal, Decimal, str, Candidate]] = []
    for candidate in candidates:
        fold_metrics = [
            _window_metrics(candidate.equity, candidate.trades, start, end) for start, end in folds
        ]
        sharpes = [m.sharpe for m in fold_metrics if m is not None and m.sharpe is not None]
        drawdowns = [m.maximum_drawdown for m in fold_metrics if m is not None]
        returns = [m.total_return for m in fold_metrics if m is not None]
        if len(sharpes) < FOLDS or len(drawdowns) < FOLDS:
            print(f"{candidate.name:34}{'insufficient window coverage':>48}")
            continue
        median_sharpe = statistics.median(sharpes)
        median_drawdown = statistics.median(drawdowns)
        median_return = statistics.median(returns)
        cells = "".join(f"{_fmt(value):>10}" for value in sharpes)
        print(
            f"{candidate.name:34}{cells}{_fmt(median_sharpe):>9}"
            f"{_fmt(median_drawdown, percent=True):>9}{_fmt(median_return, percent=True):>9}"
        )
        scored.append((median_sharpe, -median_drawdown, candidate.name, candidate))

    strategy_scored = [row for row in scored if not row[2].startswith("benchmark:")]
    if not strategy_scored:
        print("no strategy candidate produced complete fold coverage")
        return 1
    strategy_scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    winner = strategy_scored[0][3]
    print()
    print("selection rule: highest median fold Sharpe, then lower median drawdown")
    print(f"selected: {winner.name}")

    holdout_metrics = _window_metrics(
        winner.equity, winner.trades, holdout_start, sessions[-1]
    )
    buy_hold = next(c for c in candidates if c.name == "benchmark:SPY_BUY_AND_HOLD")
    holdout_benchmark = _window_metrics(
        buy_hold.equity, buy_hold.trades, holdout_start, sessions[-1]
    )
    print()
    print("HOLDOUT (evaluated once, never used for selection)")
    for label, metrics in (("selected", holdout_metrics), ("SPY buy & hold", holdout_benchmark)):
        if metrics is None:
            print(f"  {label:16} insufficient data")
            continue
        trades = metrics.winners + metrics.losers
        print(
            f"  {label:16} return {_fmt(metrics.total_return, percent=True):>9}"
            f"  maxDD {_fmt(metrics.maximum_drawdown, percent=True):>8}"
            f"  Sharpe {_fmt(metrics.sharpe):>6}  trades {trades:>4}"
        )

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    results: dict[str, dict[str, str]] = {}
    for _, _, name, candidate in scored:
        window = _window_metrics(candidate.equity, candidate.trades, selection[0], selection[-1])
        if window is None:
            continue
        results[name] = {
            "selection_total_return": _fmt(window.total_return, 6),
            "selection_sharpe": _fmt(window.sharpe, 6),
            "selection_max_drawdown": _fmt(window.maximum_drawdown, 6),
            "selection_trades": str(window.winners + window.losers),
        }
    if holdout_metrics is not None:
        results[f"HOLDOUT:{winner.name}"] = {
            "holdout_total_return": _fmt(holdout_metrics.total_return, 6),
            "holdout_sharpe": _fmt(holdout_metrics.sharpe, 6),
            "holdout_max_drawdown": _fmt(holdout_metrics.maximum_drawdown, 6),
            "holdout_trades": str(holdout_metrics.winners + holdout_metrics.losers),
        }

    manifest = ExperimentManifest(
        experiment_id=f"walkforward-{sessions[-1].date().isoformat()}",
        git_commit=commit or "unknown",
        data_hash=bar_set_hash(histories),
        configuration_hash=configuration_hash(base_config),
        costs={
            "slippage_bps": str(base_config.slippage_bps),
            "commission_per_order": str(base_config.commission_per_order),
        },
        periods=(
            ExperimentPeriod(
                name="selection", start=selection[0].date(), end=selection[-1].date()
            ),
        ),
        walk_forward_splits=tuple(
            ExperimentPeriod(name=f"fold{index}", start=start.date(), end=end.date())
            for index, (start, end) in enumerate(folds, start=1)
        ),
        holdout=ExperimentPeriod(
            name="holdout", start=holdout_start.date(), end=sessions[-1].date()
        ),
        neighborhood_grid=NEIGHBOURHOOD,
        ablations=ABLATIONS,
        results=results,
    )
    path = Path("reports") / "research" / f"{manifest.experiment_id}.json"
    created = write_experiment_manifest(path, manifest)
    print()
    print(f"{'wrote' if created else 'confirmed identical'} {path}")
    print(f"manifest hash {manifest.manifest_hash[:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
