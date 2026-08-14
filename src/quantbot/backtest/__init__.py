"""Public deterministic historical-research API."""

from quantbot.backtest.benchmarks import (
    AssetRanking,
    BenchmarkVariant,
    ComponentSwitches,
    component_switches_for,
    equal_weight_targets,
    rank_assets,
    spy_target,
)
from quantbot.backtest.engine import (
    BacktestCostComparison,
    BacktestEngine,
    BacktestInputError,
    BacktestPosition,
    BacktestResult,
)
from quantbot.backtest.experiment import (
    ExperimentManifest,
    ExperimentPeriod,
    canonical_result_json,
    write_experiment_manifest,
)
from quantbot.backtest.fill_model import (
    BacktestLookaheadError,
    BacktestOrderPurpose,
    ExecutionCosts,
    SimulatedFill,
    SimulatedOrder,
    fill_order_at_next_open,
    fill_stop_order,
)
from quantbot.backtest.metrics import (
    BacktestMetrics,
    EquityObservation,
    TradeOutcome,
    calculate_metrics,
)

__all__ = [
    "AssetRanking",
    "BacktestEngine",
    "BacktestCostComparison",
    "BacktestInputError",
    "BacktestLookaheadError",
    "BacktestMetrics",
    "BacktestOrderPurpose",
    "BacktestPosition",
    "BacktestResult",
    "BenchmarkVariant",
    "ComponentSwitches",
    "EquityObservation",
    "ExecutionCosts",
    "ExperimentManifest",
    "ExperimentPeriod",
    "SimulatedFill",
    "SimulatedOrder",
    "TradeOutcome",
    "calculate_metrics",
    "canonical_result_json",
    "component_switches_for",
    "equal_weight_targets",
    "fill_order_at_next_open",
    "fill_stop_order",
    "rank_assets",
    "spy_target",
    "write_experiment_manifest",
]
