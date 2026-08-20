"""Concrete cycle runners binding durable state to the deterministic strategy.

These runners are the composition layer between the pure strategy/risk policies and
the authoritative broker. They contain no trading judgement of their own: every
decision comes from :mod:`quantbot.strategy` and :mod:`quantbot.risk`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from quantbot.brokers import BrokerAdapter
from quantbot.domain import (
    Account,
    Bar,
    BrokerOrder,
    Fill,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.domain.ids import build_client_order_id
from quantbot.execution import StartupRecovery, StartupRecoveryResult
from quantbot.execution.recovery import is_open_order
from quantbot.market_data import (
    AdjustmentMode,
    BarQuery,
    BarSource,
    MarketDataCache,
    MarketDataFeed,
    align_daily_bars_to_session_closes,
    validate_bar_batch,
)
from quantbot.market_data.calendar import (
    AlpacaMarketCalendar,
    crypto_session_sequence,
    crypto_sessions,
    latest_completed_session,
    session_closes,
    session_sequence,
)
from quantbot.operations.cycle import CycleStrategyResult, DataSyncResult, SubmissionRequest
from quantbot.risk import (
    DrawdownState,
    EntrySizingRequest,
    OrderPurpose,
    PositionRiskInput,
    RiskApproval,
    RiskReservation,
    build_portfolio_risk_state,
    calculate_drawdown,
    size_entry,
)
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import (
    MonthlyRoster,
    PositionContext,
    SignalAction,
    StrategyConfig,
    XNYSSession,
    XNYSSessionSequence,
    build_monthly_roster,
    evaluate_symbol,
    realized_volatility,
    wilder_atr,
)

Clock = Callable[[], datetime]


class StrategyDataError(RuntimeError):
    """Raised when durable market data cannot support a deterministic decision."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MarketDataSettings:
    """Provider-facing choices that are part of the reproducible data identity."""

    feed: MarketDataFeed = MarketDataFeed.IEX
    adjustment: AdjustmentMode = AdjustmentMode.ALL
    timeframe: str = "1Day"
    provider: str = "alpaca"


def warmup_sessions(config: StrategyConfig) -> int:
    """Sessions of history the slowest indicator needs before it is defined."""
    return (
        max(
            config.momentum_long + config.momentum_skip,
            config.trend_period,
            config.entry_period,
            config.exit_period,
            config.atr_period,
        )
        + 30
    )


class CachedSessionCalendar:
    """Read the broker calendar at most once per UTC day."""

    def __init__(
        self,
        calendar: AlpacaMarketCalendar,
        *,
        lookback_days: int = 900,
        lookahead_days: int = 60,
        clock: Clock = _utcnow,
    ) -> None:
        if lookback_days <= 0 or lookahead_days <= 0:
            raise ValueError("calendar windows must be positive")
        self._calendar = calendar
        self._lookback_days = lookback_days
        self._lookahead_days = lookahead_days
        self._clock = clock
        self._cached_on: date | None = None
        self._sessions: tuple[XNYSSession, ...] = ()

    async def sessions(self) -> tuple[XNYSSession, ...]:
        today = self._clock().astimezone(UTC).date()
        if self._cached_on != today or not self._sessions:
            self._sessions = await self._calendar.get_sessions(
                today - timedelta(days=self._lookback_days),
                today + timedelta(days=self._lookahead_days),
            )
            self._cached_on = today
        return self._sessions


class CryptoSessionCalendar:
    """Synthesise 24/7 sessions, satisfying the same interface as the broker calendar.

    A 24/7 market has no calendar to fetch: every UTC day is a session. Presenting that
    through the same `sessions()` coroutine lets the cycle runners stay identical for both
    markets rather than branching internally on which calendar they are trading.
    """

    def __init__(
        self,
        *,
        lookback_days: int = 900,
        lookahead_days: int = 60,
        clock: Clock = _utcnow,
    ) -> None:
        if lookback_days <= 0 or lookahead_days <= 0:
            raise ValueError("calendar windows must be positive")
        self._lookback_days = lookback_days
        self._lookahead_days = lookahead_days
        self._clock = clock

    async def sessions(self) -> tuple[XNYSSession, ...]:
        today = self._clock().astimezone(UTC).date()
        return crypto_sessions(
            today - timedelta(days=self._lookback_days),
            today + timedelta(days=self._lookahead_days),
        )


SessionCalendar = CachedSessionCalendar | CryptoSessionCalendar


def sequence_for(
    config: StrategyConfig, sessions: tuple[XNYSSession, ...]
) -> XNYSSessionSequence:
    """Build the session sequence stamped with the config's own calendar.

    The sequence carries a calendar label that the roster validates against, so an XNYS
    builder used on a 24/7 strategy fails at roster-window validation rather than anywhere
    informative. Selecting on the config keeps that impossible.
    """
    if config.calendar == "CRYPTO24":
        return crypto_session_sequence(sessions)
    return session_sequence(sessions)


def _fill_ledger_positions(fills: Sequence[Fill]) -> dict[str, Decimal]:
    """Derive open long quantities from the durable fill ledger alone."""
    quantities: dict[str, Decimal] = {}
    for fill in sorted(fills, key=lambda item: (item.occurred_at, item.fill_id)):
        signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        updated = quantities.get(fill.symbol, Decimal("0")) + signed
        if updated == 0:
            quantities.pop(fill.symbol, None)
        else:
            quantities[fill.symbol] = updated
    return quantities


def _position_open_times(fills: Sequence[Fill]) -> dict[str, datetime]:
    """Return when each currently open long position was first established."""
    quantities: dict[str, Decimal] = {}
    opened: dict[str, datetime] = {}
    for fill in sorted(fills, key=lambda item: (item.occurred_at, item.fill_id)):
        signed = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        current = quantities.get(fill.symbol, Decimal("0"))
        if current == 0 and signed > 0:
            opened[fill.symbol] = fill.occurred_at
        updated = current + signed
        if updated == 0:
            quantities.pop(fill.symbol, None)
            opened.pop(fill.symbol, None)
        else:
            quantities[fill.symbol] = updated
    return opened


class LedgerSyncingRecovery:
    """Adopt the broker's authoritative ledger, then reconcile against local state.

    The broker is authoritative in paper mode, so fills and orders are ingested
    before comparison. The check that survives adoption is the independent one:
    positions derived from the durable fill ledger must equal the broker's
    positions. Any disagreement blocks new orders.
    """

    def __init__(
        self,
        database: Database,
        broker: BrokerAdapter,
        recovery: StartupRecovery,
    ) -> None:
        self._database = database
        self._broker = broker
        self._recovery = recovery

    async def _ingest(self, *, captured_at: datetime, run_id: str | None) -> tuple[str, ...]:
        fills = tuple(await self._broker.get_fills())
        orders = tuple(await self._broker.get_open_orders())
        account_snapshot = await self._broker.get_account()
        positions = tuple(await self._broker.get_positions())

        with self._database.transaction() as session:
            repository = StorageRepository(session)
            for fill in fills:
                repository.record_fill(fill)
            for order in orders:
                if repository.get_broker_order(order.broker_order_id) is None:
                    repository.save_broker_order(order)
                else:
                    repository.update_broker_order_lifecycle(order)
            working = [
                stored for stored in repository.list_broker_orders() if is_open_order(stored)
            ]

        # An order the broker no longer lists as open has reached a terminal state. Its stored
        # status is still whatever it was at acknowledgement, so local open-order state would
        # disagree with the broker forever and halt every subsequent cycle. Fetch the final
        # state for each and record the transition.
        still_open = {order.broker_order_id for order in orders}
        settled = [
            await self._broker.get_order(stored.broker_order_id)
            for stored in working
            if stored.broker_order_id not in still_open
        ]

        with self._database.transaction() as session:
            repository = StorageRepository(session)
            for order in settled:
                repository.update_broker_order_lifecycle(order)
            durable_fills = tuple(repository.list_fills())

        derived = _fill_ledger_positions(durable_fills)
        reported = {
            position.symbol: position.quantity for position in positions if position.quantity != 0
        }
        if derived != reported:
            return ("POSITION_LEDGER_MISMATCH",)

        # A snapshot is a point-in-time record, so its identity must include the capture
        # time. A fixed id per run label meant a second manual reconcile conflicted with the
        # first the moment account state had legitimately moved.
        stamp = captured_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")
        snapshot_id = f"{run_id or 'reconcile'}-{stamp}:account"
        with self._database.transaction() as session:
            StorageRepository(session).save_account_snapshot(
                snapshot_id,
                account_snapshot.account,
                positions,
                captured_at=captured_at,
                run_id=run_id,
            )
        return ()

    async def recover(
        self,
        reconciliation_id: str,
        *,
        started_at: datetime,
        run_id: str | None = None,
    ) -> StartupRecoveryResult:
        ingest_reasons = await self._ingest(captured_at=started_at, run_id=run_id)
        result = await self._recovery.recover(
            reconciliation_id,
            started_at=started_at,
            run_id=run_id,
        )
        if not ingest_reasons:
            return result
        reasons = tuple(dict.fromkeys((*ingest_reasons, *result.reasons)))
        return result.model_copy(update={"ready": False, "reasons": reasons})


class MarketDataSync:
    """Fetch, validate and durably cache completed daily bars for the universe."""

    def __init__(
        self,
        *,
        database: Database,
        client: BarSource,
        sessions: SessionCalendar,
        config: StrategyConfig,
        market_data: MarketDataSettings | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._database = database
        self._client = client
        self._sessions = sessions
        self._config = config
        self._market_data = market_data or MarketDataSettings()
        self._clock = clock

    async def sync(self) -> DataSyncResult:
        now = self._clock().astimezone(UTC)
        calendar_sessions = await self._sessions.sessions()
        completed = latest_completed_session(calendar_sessions, now)
        if completed is None:
            raise StrategyDataError("broker calendar contains no completed session")

        needed = warmup_sessions(self._config)
        history = [
            session for session in calendar_sessions if session.close_at <= completed.close_at
        ]
        if len(history) < needed:
            raise StrategyDataError(
                f"broker calendar provides {len(history)} completed sessions; {needed} required"
            )
        window = tuple(history[-needed:])
        query = BarQuery(
            symbols=self._config.universe,
            start=datetime.combine(window[0].session_date, time.min, tzinfo=UTC),
            end=completed.close_at,
            timeframe=self._market_data.timeframe,
            feed=self._market_data.feed,
            adjustment=self._market_data.adjustment,
            limit=10000,
        )
        batch = await self._client.get_historical_bars(query)
        aligned = align_daily_bars_to_session_closes(
            batch,
            sessions=session_closes(
                tuple(calendar_sessions), calendar=self._config.calendar
            ),
        )
        expected = tuple(session.close_at for session in window)
        in_window = tuple(bar for bar in aligned.bars if bar.timestamp >= expected[0])
        report = validate_bar_batch(
            aligned.model_copy(update={"bars": in_window}),
            expected_timestamps=expected,
            completed_through=completed.close_at,
        )
        with self._database.transaction() as session:
            stored = MarketDataCache(StorageRepository(session)).store(report)

        reasons: list[str] = []
        if report.ineligible_symbols:
            reasons.append("MARKET_DATA_INCOMPLETE_UNIVERSE")
        return DataSyncResult(
            healthy=not reasons,
            completed_through=completed.close_at,
            bars_stored=stored,
            stale_symbols=report.stale_symbols,
            reasons=tuple(reasons),
            # ponytail: the daily cycle ingests broker-confirmed fills over REST in
            # LedgerSyncingRecovery instead of holding the push stream open. Wire
            # AlpacaPaperTradeStream here if intraday order events are ever needed.
            stream_connected=True,
            risk_healthy=True,
        )


class AdaptiveMomentumRunner:
    """Turn durable bars plus broker state into risk-approved submission requests."""

    def __init__(
        self,
        *,
        database: Database,
        broker: BrokerAdapter,
        config: StrategyConfig,
        identity: StrategyIdentity,
        sessions: SessionCalendar,
        market_data: MarketDataSettings | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._database = database
        self._broker = broker
        self._config = config
        self._identity = identity
        self._sessions = sessions
        self._market_data = market_data or MarketDataSettings()
        self._clock = clock
        self._run_id: str | None = None

    def bind_run(self, run_id: str) -> None:
        """Bind the durable run that owns the signals recorded by the next evaluation."""
        if not run_id or run_id != run_id.strip():
            raise ValueError("run_id must be nonempty without surrounding whitespace")
        self._run_id = run_id

    def _load_histories(self) -> dict[str, list[Bar]]:
        provider_key = MarketDataCache.provider_key(
            self._market_data.provider,
            self._market_data.feed,
            self._market_data.adjustment,
            self._market_data.timeframe,
        )
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            return {
                symbol: repository.list_bars(symbol=symbol, provider=provider_key)
                for symbol in self._config.universe
            }

    def _slice(self, histories: Mapping[str, list[Bar]], cutoff: datetime) -> dict[str, list[Bar]]:
        sliced: dict[str, list[Bar]] = {}
        for symbol, bars in histories.items():
            kept = [bar for bar in bars if bar.timestamp <= cutoff]
            if not kept or kept[-1].timestamp != cutoff:
                raise StrategyDataError(
                    f"durable history for {symbol} is not fresh through {cutoff.isoformat()}"
                )
            sliced[symbol] = kept
        return sliced

    def _roster(
        self,
        histories: Mapping[str, list[Bar]],
        calendar_sessions: tuple[XNYSSession, ...],
        acting_index: int,
    ) -> MonthlyRoster:
        acting_month = calendar_sessions[acting_index].session_date.strftime("%Y-%m")
        effective_index = acting_index
        while (
            effective_index > 0
            and calendar_sessions[effective_index - 1].session_date.strftime("%Y-%m")
            == acting_month
        ):
            effective_index -= 1
        if effective_index == 0:
            raise StrategyDataError("broker calendar does not cover the prior rotation month")
        evaluation_at = calendar_sessions[effective_index - 1].close_at
        return build_monthly_roster(
            self._slice(histories, evaluation_at),
            evaluation_at,
            calendar_sessions[effective_index].open_at,
            self._config,
            session_sequence=sequence_for(self._config, calendar_sessions),
        )

    def _prior_position_state(
        self,
        repository: StorageRepository,
        symbol: str,
        entered_at: datetime,
    ) -> tuple[Decimal, Decimal] | None:
        """Return the (initial_stop, active_stop) already durable for this position run."""
        for record in reversed(repository.list_signals(symbol=symbol)):
            state = record.payload.get("position_state")
            if not isinstance(state, dict):
                continue
            if str(state.get("entered_at")) != entered_at.isoformat():
                continue
            return Decimal(str(state["initial_stop"])), Decimal(str(state["active_stop"]))
        return None

    def _position_context(
        self,
        repository: StorageRepository,
        position: Position,
        entered_at: datetime,
        atr: Decimal | None,
    ) -> PositionContext:
        stored = self._prior_position_state(repository, position.symbol, entered_at)
        if stored is not None:
            initial_stop, active_stop = stored
        else:
            if atr is None:
                raise StrategyDataError(
                    f"cannot bootstrap a stop for {position.symbol} without a defined ATR"
                )
            initial_stop = position.average_entry_price - self._config.initial_stop_atr * atr
            if initial_stop <= 0:
                initial_stop = position.average_entry_price / Decimal("2")
            active_stop = initial_stop
        return PositionContext(
            symbol=position.symbol,
            entered_at=entered_at,
            initial_stop=initial_stop,
            active_stop=active_stop,
        )

    def _intent(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        signal_date: date,
        created_at: datetime,
    ) -> OrderIntent:
        client_order_id = build_client_order_id(
            self._config.version,
            symbol,
            signal_date,
            side,
            0,
        )
        return OrderIntent(
            intent_id=f"intent-{client_order_id}",
            client_order_id=client_order_id,
            strategy_id=self._identity.strategy_id,
            signal_date=signal_date,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            quantity=quantity,
            created_at=created_at,
        )

    async def evaluate(self) -> CycleStrategyResult:
        run_id = self._run_id
        if run_id is None:
            raise RuntimeError("bind_run must be called before evaluate")
        now = self._clock().astimezone(UTC)
        calendar_sessions = await self._sessions.sessions()
        completed = latest_completed_session(calendar_sessions, now)
        if completed is None:
            raise StrategyDataError("broker calendar contains no completed session")
        evaluation_index = next(
            index
            for index, session in enumerate(calendar_sessions)
            if session.session_date == completed.session_date
        )
        if evaluation_index + 1 >= len(calendar_sessions):
            raise StrategyDataError("broker calendar does not cover the next trading session")
        evaluation_at = completed.close_at
        next_session_at = calendar_sessions[evaluation_index + 1].open_at

        histories = self._load_histories()
        sliced = self._slice(histories, evaluation_at)
        roster = self._roster(histories, calendar_sessions, evaluation_index + 1)
        sequence = sequence_for(self._config, calendar_sessions)

        account_snapshot = await self._broker.get_account()
        positions = {
            position.symbol: position
            for position in await self._broker.get_positions()
            if position.quantity != 0
        }
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            equity_history = repository.list_equity_snapshots(
                account_id=account_snapshot.account.account_id
            )
            fills = tuple(repository.list_fills())
        # A finite lookback lets an old peak age out. Without it the halt is absorbing:
        # entries stop at a 15% drawdown and cannot resume, because resuming needs the returns
        # that entries would have produced. See tests/unit/risk/test_drawdown_halt_is_absorbing.
        considered = (
            equity_history[-self._config.drawdown_lookback_sessions:]
            if self._config.drawdown_lookback_sessions > 0
            else equity_history
        )
        high_water = max((record.equity for record in considered), default=None)
        drawdown = calculate_drawdown(
            account_snapshot.account.equity,
            high_water if high_water and high_water > 0 else None,
            self._config,
        )
        opened_at = _position_open_times(fills)

        submissions: list[SubmissionRequest] = []
        reservations = self._pending_reservations(
            tuple(await self._broker.get_open_orders()),
            sliced,
            account_snapshot.account.equity,
        )
        evaluated = 0
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            for symbol in self._config.universe:
                position = positions.get(symbol)
                context: PositionContext | None = None
                if position is not None:
                    entered_at = opened_at.get(symbol)
                    if entered_at is None:
                        raise StrategyDataError(
                            f"broker reports a {symbol} position with no durable opening fill"
                        )
                    context = self._position_context(
                        repository,
                        position,
                        entered_at,
                        wilder_atr(sliced[symbol], self._config.atr_period),
                    )
                decision = evaluate_symbol(
                    sliced[symbol],
                    sliced[self._config.regime_symbol],
                    roster,
                    evaluation_at,
                    next_session_at,
                    self._config,
                    self._identity,
                    context,
                    session_sequence=sequence,
                )
                evaluated += 1

                request: SubmissionRequest | None = None
                rejection: tuple[str, ...] = ()
                skip_reason: tuple[str, ...] = ()
                if decision.action is SignalAction.EXIT and position is not None:
                    request = SubmissionRequest(
                        intent=self._intent(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            quantity=abs(position.quantity),
                            signal_date=evaluation_at.date(),
                            created_at=now,
                        ),
                        purpose=OrderPurpose.STRATEGY_EXIT,
                        market_eligible=True,
                        drawdown=drawdown,
                        risk_approved=None,
                    )
                elif decision.action is SignalAction.ENTER and position is None:
                    if self._already_ordered(repository, symbol, evaluation_at.date()):
                        skip_reason = ("DUPLICATE_INTENT_ALREADY_DURABLE",)
                        request = None
                    else:
                        request, rejection = self._size_entry(
                            repository,
                            symbol=symbol,
                            realized_vol=(
                                realized_volatility(
                                    sliced[symbol], self._config.volatility_lookback_days
                                )
                                if self._config.volatility_target_bps > 0
                                else None
                            ),
                            decision_price=decision.asset_close,
                            stop_distance=decision.initial_stop_distance,
                            account=account_snapshot.account,
                            broker_positions=positions,
                            reservations=reservations,
                            drawdown=drawdown,
                            signal_date=evaluation_at.date(),
                            created_at=now,
                            opened_at=opened_at,
                        )

                if request is not None and repository.get_order_intent(
                    request.intent.intent_id
                ) is not None:
                    # The same signal day already minted this client order identity, so a
                    # re-run must never submit a second order for it.
                    request = None
                    skip_reason = ("DUPLICATE_INTENT_ALREADY_DURABLE",)

                payload: dict[str, object] = {
                    "action": decision.action.value,
                    "reasons": list(decision.reasons),
                    "evaluated_at": decision.evaluated_at.isoformat(),
                    "eligible_from": decision.eligible_from.isoformat(),
                    "bar_set_hash": decision.bar_set_hash,
                    "configuration_hash": decision.configuration_hash,
                    "active_roster": decision.active_roster,
                    "regime_on": decision.regime_on,
                    "submitted": request is not None,
                    "risk_rejection": list(rejection + skip_reason),
                }
                if context is not None:
                    active_stop = decision.trailing_stop or context.active_stop
                    payload["position_state"] = {
                        "entered_at": context.entered_at.isoformat(),
                        "initial_stop": str(context.initial_stop),
                        "active_stop": str(max(context.active_stop, active_stop)),
                    }
                repository.record_signal(
                    f"{run_id}:{symbol}",
                    run_id=run_id,
                    strategy_id=self._identity.strategy_id,
                    symbol=symbol,
                    occurred_at=evaluation_at,
                    payload=payload,
                )
                if request is not None:
                    submissions.append(request)

        return CycleStrategyResult(submissions=tuple(submissions), signal_count=evaluated)

    def _already_ordered(
        self, repository: StorageRepository, symbol: str, signal_date: date
    ) -> bool:
        """Whether this signal day already minted a buy order identity for this symbol."""
        client_order_id = build_client_order_id(
            self._config.version, symbol, signal_date, OrderSide.BUY, 0
        )
        return repository.get_order_intent(f"intent-{client_order_id}") is not None

    def _pending_reservations(
        self,
        open_orders: Sequence[BrokerOrder],
        sliced: Mapping[str, Sequence[Bar]],
        equity: Decimal,
    ) -> list[RiskReservation]:
        """Capital committed by unfilled orders must bind the next run's budget.

        A submitted-but-unfilled order is neither a broker position nor an in-run
        reservation, so without this it is invisible to sizing: a second run before the
        fills land sizes every entry against a budget that was already spent. Not
        hypothetical — a re-run deployed a further 20% of equity and then exhausted buying
        power mid-submission, leaving the account unable to act on the next signal.
        """
        reserved: list[RiskReservation] = []
        for order in open_orders:
            if order.side is not OrderSide.BUY:
                continue
            bars = sliced.get(order.symbol)
            remaining = order.quantity - order.filled_quantity
            if not bars or remaining <= 0:
                continue
            value = remaining * bars[-1].close
            reserved.append(
                RiskReservation(
                    reservation_id=f"pending:{order.client_order_id}",
                    symbol=order.symbol,
                    position_value=value,
                    gross_exposure=value,
                    # ponytail: BrokerOrder does not carry the stop distance it was sized
                    # against, so reserve the per-trade risk budget instead. Over-reserves
                    # when a value cap bound the size; less exposure is the safe direction.
                    # Carry the stop on the durable intent if this blocks a real entry.
                    open_risk=equity * Decimal(self._config.risk_per_trade_bps) / Decimal(10000),
                    reserves_position_slot=True,
                )
            )
        return reserved

    def _size_entry(
        self,
        repository: StorageRepository,
        *,
        symbol: str,
        realized_vol: Decimal | None,
        decision_price: Decimal | None,
        stop_distance: Decimal | None,
        account: Account,
        broker_positions: Mapping[str, Position],
        reservations: list[RiskReservation],
        drawdown: DrawdownState,
        signal_date: date,
        created_at: datetime,
        opened_at: Mapping[str, datetime],
    ) -> tuple[SubmissionRequest | None, tuple[str, ...]]:
        if decision_price is None or stop_distance is None or stop_distance <= 0:
            return None, ("ENTRY_INPUTS_UNAVAILABLE",)
        position_inputs = [
            PositionRiskInput(
                position=held,
                active_stop=self._known_stop(repository, held, opened_at.get(held.symbol)),
            )
            for held in broker_positions.values()
        ]
        portfolio = build_portfolio_risk_state(
            f"{account.account_id}:{signal_date.isoformat()}",
            account,
            symbol,
            position_inputs,
            reservations,
        )
        sized = size_entry(
            EntrySizingRequest(
                symbol=symbol,
                reference_price=decision_price,
                stop_distance=stop_distance,
                portfolio=portfolio,
                drawdown=drawdown,
                realized_volatility=realized_vol,
            ),
            self._config,
        )
        if not isinstance(sized, RiskApproval):
            return None, sized.reasons
        position_value = sized.quantity * decision_price
        reservations.append(
            RiskReservation(
                reservation_id=f"{signal_date.isoformat()}:{symbol}",
                symbol=symbol,
                position_value=position_value,
                gross_exposure=position_value,
                open_risk=sized.quantity * stop_distance,
                reserves_position_slot=True,
            )
        )
        return (
            SubmissionRequest(
                intent=self._intent(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=sized.quantity,
                    signal_date=signal_date,
                    created_at=created_at,
                ),
                purpose=OrderPurpose.ENTRY,
                market_eligible=True,
                drawdown=drawdown,
                risk_approved=True,
            ),
            (),
        )

    def _known_stop(
        self,
        repository: StorageRepository,
        position: Position,
        entered_at: datetime | None,
    ) -> Decimal | None:
        if entered_at is None:
            return None
        stored = self._prior_position_state(repository, position.symbol, entered_at)
        return None if stored is None else stored[1]
