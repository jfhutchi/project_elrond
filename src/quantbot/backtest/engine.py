"""Sequential, next-session backtest engine shared by all benchmark variants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.backtest.benchmarks import (
    BenchmarkVariant,
    ComponentSwitches,
    component_switches_for,
    equal_weight_targets,
    rank_assets,
    spy_target,
)
from quantbot.backtest.experiment import canonical_result_json
from quantbot.backtest.fill_model import (
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
from quantbot.domain import Bar, OrderSide
from quantbot.risk import calculate_drawdown, quantize_quantity
from quantbot.strategy import (
    StrategyConfig,
    bar_set_hash,
    configuration_hash,
    donchian_entry_level,
    donchian_exit_level,
    highest_high_since,
    momentum_12_1,
    sma,
    wilder_atr,
)


class BacktestInputError(ValueError):
    """Raised for input data that cannot be simulated without hidden assumptions."""


class BacktestModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class BacktestPosition(BacktestModel):
    symbol: str
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    average_cost: Decimal = Field(gt=0, allow_inf_nan=False)
    average_base_price: Decimal = Field(gt=0, allow_inf_nan=False)
    entered_at: datetime
    entry_costs: Decimal = Field(ge=0, allow_inf_nan=False)
    initial_stop: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    active_stop: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_position(self) -> BacktestPosition:
        if (
            not self.symbol
            or self.symbol != self.symbol.strip()
            or self.symbol != self.symbol.upper()
        ):
            raise ValueError("backtest position symbol must be uppercase")
        if (self.initial_stop is None) != (self.active_stop is None):
            raise ValueError("initial_stop and active_stop must be present together")
        if (
            self.initial_stop is not None
            and self.active_stop is not None
            and self.active_stop < self.initial_stop
        ):
            raise ValueError("active stop cannot loosen below initial stop")
        return self


class BacktestCostComparison(BacktestModel):
    gross_final_equity: Decimal = Field(ge=0, allow_inf_nan=False)
    net_final_equity: Decimal = Field(ge=0, allow_inf_nan=False)
    gross_total_return: Decimal = Field(allow_inf_nan=False)
    net_total_return: Decimal = Field(allow_inf_nan=False)
    total_costs: Decimal = Field(ge=0, allow_inf_nan=False)


class BacktestResult(BacktestModel):
    variant: BenchmarkVariant
    component_switches: ComponentSwitches
    initial_cash: Decimal = Field(gt=0, allow_inf_nan=False)
    final_equity: Decimal = Field(ge=0, allow_inf_nan=False)
    final_cash: Decimal = Field(ge=0, allow_inf_nan=False)
    positions: tuple[BacktestPosition, ...]
    fills: tuple[SimulatedFill, ...]
    trades: tuple[TradeOutcome, ...]
    equity_curve: tuple[EquityObservation, ...]
    metrics: BacktestMetrics
    cost_comparison: BacktestCostComparison
    input_hash: str = Field(min_length=64, max_length=64)
    configuration_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_result(self) -> BacktestResult:
        if not self.equity_curve:
            raise ValueError("backtest result requires an equity curve")
        if self.final_equity != self.equity_curve[-1].equity:
            raise ValueError("final_equity must match the last equity observation")
        if self.cost_comparison.net_final_equity != self.final_equity:
            raise ValueError("cost comparison must use result final equity")
        if self.cost_comparison.net_total_return != self.metrics.total_return:
            raise ValueError("cost comparison net return must match result metrics")
        symbols = [position.symbol for position in self.positions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("backtest positions must have unique symbols")
        order_ids = [fill.order_id for fill in self.fills]
        if len(order_ids) != len(set(order_ids)):
            raise ValueError("simulated fill order IDs must be unique")
        return self

    def canonical_json(self) -> str:
        return canonical_result_json(self)


@dataclass(frozen=True, slots=True)
class _PendingTargets:
    signal_at: datetime
    weights: dict[str, Decimal]
    force_rebalance: bool


@dataclass(frozen=True, slots=True)
class _FullCandidate:
    symbol: str
    momentum: Decimal
    stop_distance: Decimal | None


@dataclass(frozen=True, slots=True)
class _PendingFull:
    signal_at: datetime
    exits: tuple[str, ...]
    candidates: tuple[_FullCandidate, ...]
    drawdown_multiplier: Decimal
    liquidation: bool


def _validate_histories(histories: Mapping[str, Sequence[Bar]]) -> None:
    if "SPY" not in histories or not histories["SPY"]:
        raise BacktestInputError("SPY history is required")
    for symbol, bars in histories.items():
        previous: datetime | None = None
        for bar in bars:
            if bar.symbol != symbol:
                raise BacktestInputError(f"history key {symbol} contains a {bar.symbol} bar")
            if previous is not None and bar.timestamp <= previous:
                raise BacktestInputError("bar histories must be strictly increasing")
            previous = bar.timestamp


class BacktestEngine:
    """Run long-only benchmark and full-strategy simulations over adjusted bars."""

    def __init__(self, config: StrategyConfig, *, initial_cash: Decimal) -> None:
        if not initial_cash.is_finite() or initial_cash <= 0:
            raise ValueError("initial_cash must be positive and finite")
        self._config = config
        self._initial_cash = initial_cash
        self._costs = ExecutionCosts(
            slippage_bps=config.slippage_bps,
            commission_per_order=config.commission_per_order,
        )

    def _affordable(self, quantity: Decimal, unit_cost: Decimal, cash: Decimal) -> Decimal:
        """Largest acceptable quantity whose estimated cost fits the available cash.

        Solved directly rather than by decrementing: a whole-share decrement drives any
        sub-one-share order negative, which silently skipped every cash-bound trade once
        quantities became fractional.
        """
        if unit_cost <= 0:
            return Decimal("0")
        budget = cash - self._costs.commission_per_order
        if budget <= 0:
            return Decimal("0")
        return min(quantity, quantize_quantity(budget / unit_cost, self._config))

    def run(
        self,
        histories: Mapping[str, Sequence[Bar]],
        variant: BenchmarkVariant,
        *,
        component_switches: ComponentSwitches | None = None,
    ) -> BacktestResult:
        _validate_histories(histories)
        switches = component_switches or component_switches_for(variant)
        bars_by_symbol = {
            symbol: {bar.timestamp: bar for bar in bars} for symbol, bars in histories.items()
        }
        sessions = tuple(bar.timestamp for bar in histories["SPY"])
        sliced: dict[str, list[Bar]] = {symbol: [] for symbol in histories}
        positions: dict[str, BacktestPosition] = {}
        fills: list[SimulatedFill] = []
        trades: list[TradeOutcome] = []
        equity_curve: list[EquityObservation] = []
        cash = self._initial_cash
        pending_targets: _PendingTargets | None = (
            _PendingTargets(
                signal_at=sessions[0] - timedelta(microseconds=1),
                weights={self._config.benchmark_symbol: Decimal("1")},
                force_rebalance=False,
            )
            if variant is BenchmarkVariant.SPY_BUY_AND_HOLD
            else None
        )
        pending_full: _PendingFull | None = None
        active_roster: tuple[str, ...] = ()
        high_water = self._initial_cash
        order_sequence = 0

        def next_order_id(timestamp: datetime, symbol: str, purpose: str) -> str:
            nonlocal order_sequence
            order_sequence += 1
            return f"{variant.value}:{timestamp.isoformat()}:{symbol}:{purpose}:{order_sequence}"

        def current_bar(symbol: str, timestamp: datetime) -> Bar:
            bar = bars_by_symbol.get(symbol, {}).get(timestamp)
            if bar is None:
                raise BacktestInputError(
                    f"held or ordered symbol {symbol} is missing a session bar"
                )
            return bar

        def apply_buy(fill: SimulatedFill, stop_distance: Decimal | None = None) -> None:
            nonlocal cash
            existing = positions.get(fill.symbol)
            total_cost = fill.commission + fill.slippage_cost
            cash += fill.cash_delta
            if existing is None:
                stop = fill.price - stop_distance if stop_distance is not None else None
                if stop is not None and stop <= 0:
                    raise BacktestInputError("initial stop must remain positive")
                positions[fill.symbol] = BacktestPosition(
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    average_cost=fill.price,
                    average_base_price=fill.base_price,
                    entered_at=fill.occurred_at,
                    entry_costs=total_cost,
                    initial_stop=stop,
                    active_stop=stop,
                )
                return
            new_quantity = existing.quantity + fill.quantity
            positions[fill.symbol] = existing.model_copy(
                update={
                    "quantity": new_quantity,
                    "average_cost": (
                        existing.average_cost * existing.quantity + fill.price * fill.quantity
                    )
                    / new_quantity,
                    "average_base_price": (
                        existing.average_base_price * existing.quantity
                        + fill.base_price * fill.quantity
                    )
                    / new_quantity,
                    "entry_costs": existing.entry_costs + total_cost,
                }
            )

        def apply_sell(fill: SimulatedFill) -> None:
            nonlocal cash
            position = positions[fill.symbol]
            if fill.quantity > position.quantity:
                raise BacktestInputError("sell fill exceeds held quantity")
            fraction = fill.quantity / position.quantity
            allocated_entry_costs = position.entry_costs * fraction
            exit_costs = fill.commission + fill.slippage_cost
            gross_pnl = (fill.base_price - position.average_base_price) * fill.quantity
            # Derive net from the same total the trade reports. Subtracting the two cost
            # components separately associates differently and, once quantities are
            # fractional, disagrees with gross - costs in the last decimal place.
            total_costs = allocated_entry_costs + exit_costs
            trades.append(
                TradeOutcome(
                    symbol=fill.symbol,
                    opened_at=position.entered_at,
                    closed_at=fill.occurred_at,
                    gross_pnl=gross_pnl,
                    net_pnl=gross_pnl - total_costs,
                    costs=total_costs,
                    notional=position.average_base_price * fill.quantity,
                )
            )
            cash += fill.cash_delta
            remaining = position.quantity - fill.quantity
            if remaining == 0:
                positions.pop(fill.symbol)
            else:
                positions[fill.symbol] = position.model_copy(
                    update={
                        "quantity": remaining,
                        "entry_costs": position.entry_costs - allocated_entry_costs,
                    }
                )

        for index, timestamp in enumerate(sessions):
            traded_notional = Decimal("0")

            if variant is BenchmarkVariant.FULL_STRATEGY:
                for symbol in sorted(tuple(positions)):
                    position = positions[symbol]
                    if position.active_stop is None:
                        continue
                    stop_order = SimulatedOrder(
                        order_id=next_order_id(timestamp, symbol, "STOP"),
                        symbol=symbol,
                        side=OrderSide.SELL,
                        quantity=position.quantity,
                        signal_at=sessions[index - 1] if index > 0 else position.entered_at,
                        purpose=BacktestOrderPurpose.STOP,
                        stop_price=position.active_stop,
                    )
                    stop_fill = fill_stop_order(
                        stop_order,
                        current_bar(symbol, timestamp),
                        self._costs,
                    )
                    if stop_fill is not None:
                        apply_sell(stop_fill)
                        fills.append(stop_fill)
                        traded_notional += stop_fill.base_price * stop_fill.quantity

            if pending_targets is not None:
                open_equity = cash + sum(
                    (
                        position.quantity * current_bar(symbol, timestamp).open
                        for symbol, position in positions.items()
                    ),
                    Decimal("0"),
                )
                target_quantities: dict[str, Decimal] = {}
                for symbol, weight in pending_targets.weights.items():
                    target_bar = bars_by_symbol.get(symbol, {}).get(timestamp)
                    if target_bar is None:
                        continue
                    target_quantities[symbol] = quantize_quantity(
                        open_equity * weight / target_bar.open,
                        self._config,
                    )
                for symbol in sorted(tuple(positions)):
                    target = target_quantities.get(symbol, Decimal("0"))
                    current = positions[symbol].quantity
                    if not pending_targets.force_rebalance and symbol in pending_targets.weights:
                        continue
                    if current <= target:
                        continue
                    order = SimulatedOrder(
                        order_id=next_order_id(timestamp, symbol, "SELL"),
                        symbol=symbol,
                        side=OrderSide.SELL,
                        quantity=current - target,
                        signal_at=pending_targets.signal_at,
                        purpose=BacktestOrderPurpose.REBALANCE,
                    )
                    fill = fill_order_at_next_open(
                        order,
                        current_bar(symbol, timestamp),
                        self._costs,
                    )
                    apply_sell(fill)
                    fills.append(fill)
                    traded_notional += fill.base_price * fill.quantity
                for symbol in sorted(target_quantities):
                    current = positions[symbol].quantity if symbol in positions else Decimal("0")
                    if not pending_targets.force_rebalance and current > 0:
                        continue
                    desired = target_quantities[symbol]
                    quantity = desired - current
                    if quantity <= 0:
                        continue
                    bar = current_bar(symbol, timestamp)
                    unit_cost = bar.open * (
                        Decimal("1") + Decimal(self._costs.slippage_bps) / Decimal("10000")
                    )
                    quantity = self._affordable(quantity, unit_cost, cash)
                    if quantity <= 0:
                        continue
                    order = SimulatedOrder(
                        order_id=next_order_id(timestamp, symbol, "BUY"),
                        symbol=symbol,
                        side=OrderSide.BUY,
                        quantity=quantity,
                        signal_at=pending_targets.signal_at,
                        purpose=BacktestOrderPurpose.REBALANCE,
                    )
                    fill = fill_order_at_next_open(order, bar, self._costs)
                    apply_buy(fill)
                    fills.append(fill)
                    traded_notional += fill.base_price * fill.quantity
                pending_targets = None

            if pending_full is not None:
                for symbol in pending_full.exits:
                    if symbol not in positions:
                        continue
                    position = positions[symbol]
                    purpose = (
                        BacktestOrderPurpose.DRAWDOWN_LIQUIDATION
                        if pending_full.liquidation
                        else BacktestOrderPurpose.EXIT
                    )
                    order = SimulatedOrder(
                        order_id=next_order_id(timestamp, symbol, purpose.value),
                        symbol=symbol,
                        side=OrderSide.SELL,
                        quantity=position.quantity,
                        signal_at=pending_full.signal_at,
                        purpose=purpose,
                    )
                    fill = fill_order_at_next_open(
                        order,
                        current_bar(symbol, timestamp),
                        self._costs,
                    )
                    apply_sell(fill)
                    fills.append(fill)
                    traded_notional += fill.base_price * fill.quantity
                if not pending_full.liquidation:
                    for candidate in pending_full.candidates:
                        if (
                            candidate.symbol in positions
                            or len(positions) >= self._config.max_positions
                        ):
                            continue
                        bar = current_bar(candidate.symbol, timestamp)
                        open_equity = cash + sum(
                            (
                                position.quantity * current_bar(symbol, timestamp).open
                                for symbol, position in positions.items()
                            ),
                            Decimal("0"),
                        )
                        gross = sum(
                            (
                                position.quantity * current_bar(symbol, timestamp).open
                                for symbol, position in positions.items()
                            ),
                            Decimal("0"),
                        )
                        open_risk = sum(
                            (
                                position.quantity
                                * max(
                                    current_bar(symbol, timestamp).open
                                    - (position.active_stop or current_bar(symbol, timestamp).open),
                                    Decimal("0"),
                                )
                                for symbol, position in positions.items()
                            ),
                            Decimal("0"),
                        )
                        adverse_entry_price = bar.open * (
                            Decimal("1") + Decimal(self._costs.slippage_bps) / Decimal("10000")
                        )
                        if switches.atr_risk:
                            if candidate.stop_distance is None:
                                continue
                            risk_budget = (
                                open_equity
                                * Decimal(self._config.risk_per_trade_bps)
                                / Decimal("10000")
                                * pending_full.drawdown_multiplier
                            )
                            remaining_risk = max(
                                Decimal("0"),
                                open_equity
                                * Decimal(self._config.max_open_risk_bps)
                                / Decimal("10000")
                                - open_risk,
                            )
                            risk_quantity = (
                                min(risk_budget, remaining_risk) / candidate.stop_distance
                            )
                        else:
                            risk_quantity = open_equity / adverse_entry_price
                        quantity = min(
                            risk_quantity,
                            open_equity
                            * Decimal(self._config.max_position_value_bps)
                            / Decimal("10000")
                            / adverse_entry_price,
                            max(
                                Decimal("0"),
                                open_equity
                                * Decimal(self._config.max_gross_exposure_bps)
                                / Decimal("10000")
                                - gross,
                            )
                            / adverse_entry_price,
                            max(Decimal("0"), cash) / adverse_entry_price,
                        )
                        quantity = quantize_quantity(quantity, self._config)
                        if (
                            candidate.stop_distance is not None
                            and adverse_entry_price <= candidate.stop_distance
                        ):
                            continue
                        quantity = self._affordable(quantity, adverse_entry_price, cash)
                        if quantity <= 0:
                            continue
                        order = SimulatedOrder(
                            order_id=next_order_id(timestamp, candidate.symbol, "ENTRY"),
                            symbol=candidate.symbol,
                            side=OrderSide.BUY,
                            quantity=quantity,
                            signal_at=pending_full.signal_at,
                            purpose=BacktestOrderPurpose.ENTRY,
                        )
                        fill = fill_order_at_next_open(order, bar, self._costs)
                        apply_buy(fill, candidate.stop_distance)
                        fills.append(fill)
                        traded_notional += fill.base_price * fill.quantity
                        position = positions[candidate.symbol]
                        if position.active_stop is not None:
                            same_day_stop = SimulatedOrder(
                                order_id=next_order_id(
                                    timestamp,
                                    candidate.symbol,
                                    "ENTRY_STOP",
                                ),
                                symbol=candidate.symbol,
                                side=OrderSide.SELL,
                                quantity=position.quantity,
                                signal_at=pending_full.signal_at,
                                purpose=BacktestOrderPurpose.STOP,
                                stop_price=position.active_stop,
                            )
                            stop_fill = fill_stop_order(same_day_stop, bar, self._costs)
                            if stop_fill is not None:
                                apply_sell(stop_fill)
                                fills.append(stop_fill)
                                traded_notional += stop_fill.base_price * stop_fill.quantity
                pending_full = None

            for symbol, bars in sliced.items():
                available_bar = bars_by_symbol[symbol].get(timestamp)
                if available_bar is not None:
                    bars.append(available_bar)

            close_equity = cash + sum(
                (
                    position.quantity * current_bar(symbol, timestamp).close
                    for symbol, position in positions.items()
                ),
                Decimal("0"),
            )
            gross_exposure = sum(
                (
                    position.quantity * current_bar(symbol, timestamp).close
                    for symbol, position in positions.items()
                ),
                Decimal("0"),
            )
            equity_curve.append(
                EquityObservation(
                    timestamp=timestamp,
                    equity=close_equity,
                    cash=cash,
                    gross_exposure=gross_exposure,
                    traded_notional=traded_notional,
                )
            )
            high_water = max(high_water, close_equity)
            if index == len(sessions) - 1:
                continue
            next_timestamp = sessions[index + 1]
            is_month_end = timestamp.month != next_timestamp.month

            if variant is BenchmarkVariant.FULL_STRATEGY:
                if is_month_end:
                    rankings = rank_assets(
                        sliced,
                        self._config,
                        # Roster construction runs monthly, so filtering on trend here locks a
                        # symbol out for a whole month over one month-end close. With
                        # trend_gate_per_session the condition is re-checked every session at
                        # engine.py:689 instead, which is where a hold-while-true rule belongs.
                        require_trend=(
                            switches.asset_trend and not self._config.trend_gate_per_session
                        ),
                        require_positive=(
                            switches.momentum and self._config.positive_momentum_required
                        ),
                        use_momentum=switches.momentum,
                        as_of=timestamp,
                    )
                    active_roster = tuple(
                        ranking.symbol for ranking in rankings[: self._config.roster_size]
                    )
                drawdown = calculate_drawdown(close_equity, high_water, self._config)
                exits: list[str] = []
                candidates: list[_FullCandidate] = []
                spy_history = sliced.get(self._config.regime_symbol, [])
                spy_trend = sma(spy_history, self._config.trend_period)
                regime_on = (
                    bool(spy_history)
                    and spy_trend is not None
                    and spy_history[-1].close > spy_trend
                )
                for symbol in sorted(tuple(positions)):
                    history = sliced.get(symbol, [])
                    if not history:
                        exits.append(symbol)
                        continue
                    trend = sma(history, self._config.trend_period)
                    exit_level = donchian_exit_level(history, self._config.exit_period)
                    atr = wilder_atr(history, self._config.atr_period)
                    position = positions[symbol]
                    updated_stop = position.active_stop
                    if switches.trailing_stop and atr is not None and updated_stop is not None:
                        highest = highest_high_since(history, position.entered_at)
                        if highest is not None:
                            updated_stop = max(
                                updated_stop,
                                highest - self._config.trailing_stop_atr * atr,
                            )
                            positions[symbol] = position.model_copy(
                                update={"active_stop": updated_stop}
                            )
                    exit_required = drawdown.liquidation_required
                    if switches.roster_exit and symbol not in active_roster:
                        exit_required = True
                    if switches.asset_trend and trend is not None and history[-1].close <= trend:
                        exit_required = True
                    if (
                        switches.donchian_exit
                        and exit_level is not None
                        and history[-1].close < exit_level
                    ):
                        exit_required = True
                    if updated_stop is not None and history[-1].close <= updated_stop:
                        exit_required = True
                    if exit_required:
                        exits.append(symbol)
                        continue
                if not drawdown.entry_halted and not drawdown.liquidation_required:
                    ranking_by_symbol = {
                        ranking.symbol: ranking
                        for ranking in rank_assets(
                            sliced,
                            self._config,
                            require_trend=switches.asset_trend,
                            require_positive=(
                                switches.momentum and self._config.positive_momentum_required
                            ),
                            use_momentum=switches.momentum,
                            as_of=timestamp,
                        )
                    }
                    for symbol in active_roster:
                        if symbol in positions:
                            continue
                        history = sliced.get(symbol, [])
                        if not history:
                            continue
                        trend = sma(history, self._config.trend_period)
                        entry_level = donchian_entry_level(history, self._config.entry_period)
                        atr = wilder_atr(history, self._config.atr_period)
                        momentum = momentum_12_1(
                            history,
                            self._config.momentum_long,
                            self._config.momentum_skip,
                        )
                        if atr is None and (switches.atr_risk or switches.trailing_stop):
                            continue
                        if switches.market_regime and not regime_on:
                            continue
                        if switches.asset_trend and (trend is None or history[-1].close <= trend):
                            continue
                        if switches.momentum and (momentum is None or momentum <= 0):
                            continue
                        if switches.donchian_entry and (
                            entry_level is None or history[-1].close <= entry_level
                        ):
                            continue
                        ranking = ranking_by_symbol.get(symbol)
                        score = ranking.momentum if ranking is not None else Decimal("0")
                        candidates.append(
                            _FullCandidate(
                                symbol=symbol,
                                momentum=score,
                                stop_distance=(
                                    self._config.initial_stop_atr * atr if atr is not None else None
                                ),
                            )
                        )
                pending_full = _PendingFull(
                    signal_at=timestamp,
                    exits=tuple(sorted(exits)),
                    candidates=tuple(
                        sorted(candidates, key=lambda item: (-item.momentum, item.symbol))
                    ),
                    drawdown_multiplier=drawdown.new_risk_multiplier,
                    liquidation=drawdown.liquidation_required,
                )
                continue

            if variant in {
                BenchmarkVariant.SPY_BUY_AND_HOLD,
                BenchmarkVariant.SPY_SMA200,
                BenchmarkVariant.SPY_DONCHIAN,
            }:
                weights = spy_target(
                    variant,
                    sliced[self._config.benchmark_symbol],
                    self._config,
                    currently_held=self._config.benchmark_symbol in positions,
                )
                current_symbols = set(positions)
                if set(weights) != current_symbols:
                    pending_targets = _PendingTargets(timestamp, weights, False)
                continue

            if is_month_end:
                rankings = rank_assets(
                    sliced,
                    self._config,
                    require_trend=variant is BenchmarkVariant.MOMENTUM_TREND,
                    require_positive=False,
                    as_of=timestamp,
                )
                weights = equal_weight_targets(rankings, self._config)
                pending_targets = _PendingTargets(timestamp, weights, True)

        total_costs = sum(
            (fill.commission + fill.slippage_cost for fill in fills),
            Decimal("0"),
        )
        curve = tuple(equity_curve)
        outcomes = tuple(trades)
        metrics = calculate_metrics(
            curve,
            outcomes,
            total_costs_override=total_costs,
            initial_equity_override=self._initial_cash,
        )
        gross_final_equity = curve[-1].equity + total_costs
        cost_comparison = BacktestCostComparison(
            gross_final_equity=gross_final_equity,
            net_final_equity=curve[-1].equity,
            gross_total_return=gross_final_equity / self._initial_cash - Decimal("1"),
            net_total_return=metrics.total_return,
            total_costs=total_costs,
        )
        return BacktestResult(
            variant=variant,
            component_switches=switches,
            initial_cash=self._initial_cash,
            final_equity=curve[-1].equity,
            final_cash=cash,
            positions=tuple(sorted(positions.values(), key=lambda item: item.symbol)),
            fills=tuple(fills),
            trades=outcomes,
            equity_curve=curve,
            metrics=metrics,
            cost_comparison=cost_comparison,
            input_hash=bar_set_hash(histories),
            configuration_hash=configuration_hash(self._config),
        )
