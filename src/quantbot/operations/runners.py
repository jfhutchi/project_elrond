"""Concrete cycle runners binding durable state to the deterministic strategy.

These runners are the composition layer between the pure strategy/risk policies and
the authoritative broker. They contain no trading judgement of their own: every
decision comes from :mod:`quantbot.strategy` and :mod:`quantbot.risk`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session

from quantbot.anomaly import (
    AnomalyDetectorConfig,
    SessionObservation,
    dependence_contexts,
    detect,
)
from quantbot.anomaly.catalyst import DEFAULT_LOOKBACK, assess_catalysts
from quantbot.anomaly.catalyst import SourceResult as CatalystSourceResult
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
from quantbot.ingestion import (
    CursorAdvance,
    CursorConflict,
    IngestionControlPlane,
    IngestionCursor,
    IngestionError,
    IngestionSource,
    RecordCounts,
    RunMode,
)
from quantbot.market_data import (
    AdjustmentMode,
    BarQuery,
    BarSource,
    MarketDataCache,
    MarketDataFeed,
    align_daily_bars_to_session_closes,
    validate_bar_batch,
)
from quantbot.market_data.base import MarketDataProtocolError
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
    CandidateDirection,
    DrawdownState,
    EntrySizingRequest,
    OrderPurpose,
    PortfolioVetoDecision,
    PortfolioVetoPolicy,
    PositionRiskInput,
    RejectedCandidate,
    RejectionReason,
    RiskApproval,
    RiskReservation,
    as_rows,
    build_portfolio_risk_state,
    build_veto_inputs,
    calculate_drawdown,
    candidate_id,
    evaluate_portfolio_vetoes,
    observation_id,
    size_entry,
)
from quantbot.risk.counterfactual import (
    HypotheticalExecutionPolicy,
    evaluate_counterfactual,
)
from quantbot.risk.dependence import DependenceSnapshot, build_dependence_snapshot
from quantbot.risk.exposure import ExposureClassification, load_exposure_classification
from quantbot.risk.regime import build_market_regime_snapshot
from quantbot.risk.shadow import CounterfactualState, session_of
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
    tranche_weights,
    wilder_atr,
)

Clock = Callable[[], datetime]


class CatalystSource(Protocol):
    """A news source the runner may consult about an anomaly.

    Narrow on purpose: the runner needs one question answered and must not be able to reach a
    provider for anything else.
    """

    async def fetch(
        self,
        symbols: Sequence[str],
        *,
        window_start: datetime,
        observed_at: datetime,
        retrieved_at: datetime,
    ) -> CatalystSourceResult: ...


@dataclass(frozen=True, slots=True)
class ResearchContext:
    """What the market looked like when a decision was made. Context, never authority.

    Bound onto refusals so a later study can condition on the conditions rather than pooling
    every rejection into one undifferentiated set (#56, #59). Nothing in the decision path reads
    it, which is the property that keeps a regime label from ever relaxing a limit.

    Absent stays absent. A missing regime is `None` rather than a neutral label, because a
    substituted context is indistinguishable from a measured one once it is inside an analysis.
    """

    #: Symbol to the anomaly event fired for it this cycle, if any.
    anomaly_event_ids: Mapping[str, str] = field(default_factory=dict)
    #: One regime reading for the whole cycle: it describes the market, not a symbol.
    regime_snapshot_id: str | None = None
    #: Symbol to the catalyst assessment recorded for its anomaly, if one fired and was assessed.
    catalyst_ids: Mapping[str, str] = field(default_factory=dict)


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


def sequence_for(config: StrategyConfig, sessions: tuple[XNYSSession, ...]) -> XNYSSessionSequence:
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


#: The single stream this collector checkpoints. It fetches the whole universe in one query, so
#: one checkpoint describes one fetch; per-symbol cursors would claim a granularity the fetch
#: does not have.
UNIVERSE_STREAM = "universe"

#: How long a `RUNNING` collection row may stay open before a later process closes it as
#: abandoned. **Project default (#198).** A market-data sync completes in seconds and the daemon
#: schedules one per session, so six hours is far beyond any honest run and far inside a cycle
#: -- an unclosed row older than this belongs to a process that is not coming back. The single
#: writer lock already prevents two syncs overlapping, so this cannot close a live run.
ABANDON_RUNS_AFTER = timedelta(hours=6)

# Project default: bound the entire calendar/pagination/retry operation, including Retry-After
# sleeps. Individual Alpaca requests already have their own timeout and two retries.
COLLECTION_TIMEOUT_SECONDS = 120.0


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
        veto_policy: PortfolioVetoPolicy | None = None,
        clock: Clock = _utcnow,
        collection_timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS,
    ) -> None:
        if not math.isfinite(collection_timeout_seconds) or collection_timeout_seconds <= 0:
            raise ValueError("collection timeout must be positive and finite")
        self._collection_timeout_seconds = collection_timeout_seconds
        self._database = database
        self._client = client
        self._sessions = sessions
        self._config = config
        self._market_data = market_data or MarketDataSettings()
        #: The same frozen policy the runner evaluates vetoes against, so the fetched window and
        #: the measured window cannot drift apart (#84). They were separate arguments held in
        #: step by a comment, which is a convention rather than an invariant.
        self._veto_policy = veto_policy or PortfolioVetoPolicy()
        self._clock = clock

    @property
    def source_id(self) -> str:
        """Stable identity for this collector.

        The provider is part of the id rather than only of the row, so switching provider
        registers a *new* source with its own cursor instead of failing the cycle or silently
        continuing a checkpoint measured against different data.
        """
        return f"market-data:{self._market_data.provider}:{self._market_data.timeframe}"

    def _configuration_hash(self) -> str:
        """What this source is configured to collect.

        The universe is included deliberately. Adding a symbol changes what the stream contains,
        and a checkpoint carried silently across that change describes a window that was never
        collected for the new name. The change is recorded, not acted on: `register_source`
        writes a `CONFIGURATION_CHANGED` event and leaves the cursor alone.
        """
        payload = json.dumps(
            {
                "adjustment": self._market_data.adjustment.value,
                "calendar": self._config.calendar,
                "feed": self._market_data.feed.value,
                "provider": self._market_data.provider,
                "timeframe": self._market_data.timeframe,
                "universe": sorted(self._config.universe),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def sync(self) -> DataSyncResult:
        now = self._clock().astimezone(UTC)
        configuration_hash = self._configuration_hash()

        # The attempt is committed *before* the fetch, on purpose. A run recorded only on
        # success cannot distinguish "collection never started" from "collection started and the
        # process died", and the second is the case #198 exists to make visible. The price is a
        # `RUNNING` row nothing will close from the inside, which is why the previous ones are
        # abandoned here by the process that outlived them.
        with self._database.transaction() as session:
            plane = IngestionControlPlane(session)
            run_id = plane.next_run_id(self.source_id, now)
            source = plane.register_source(
                source_id=self.source_id,
                provider=self._market_data.provider,
                source_kind="daily-bars",
                configuration_hash=configuration_hash,
                now=now,
            )
            existing = plane.cursor(self.source_id, UNIVERSE_STREAM)
            resume_from = None if existing is None else existing.last_observation_at
            if existing is None:
                # Legacy stores may contain real observations without ingestion control rows.
                # Each symbol contributes its own position; the slowest stream sets recovery.
                cache = MarketDataCache(StorageRepository(session))
                positions = [
                    values[-1].timestamp
                    for symbol in self._config.universe
                    if (
                        values := cache.load(
                            symbol=symbol,
                            provider=self._market_data.provider,
                            feed=self._market_data.feed,
                            adjustment=self._market_data.adjustment,
                            timeframe=self._market_data.timeframe,
                        )
                    )
                ]
                resume_from = min(positions, default=None)
                start = plane.earliest_requested_start(self.source_id)
                if start is not None:
                    resume_from = start if resume_from is None else min(start, resume_from)
            plane.abandon_stale_runs(
                older_than=now - ABANDON_RUNS_AFTER,
                now=now,
                source_id=self.source_id,
            )
            plane.begin_run(
                ingestion_run_id=run_id,
                source_id=self.source_id,
                mode=RunMode.LIVE,
                now=now,
            )
        try:
            async with asyncio.timeout(self._collection_timeout_seconds):
                return await self._collect(
                    run_id,
                    now=now,
                    existing=existing,
                    resume_from=resume_from,
                    source=source,
                )
        except (Exception, asyncio.CancelledError) as error:
            # Recorded and re-raised. The cycle still fails the way it always did; what changes
            # is that the failure survives the process, so a feed that has been broken since
            # Tuesday is answerable rather than merely absent.
            with self._database.transaction() as session:
                IngestionControlPlane(session).fail_run(
                    run_id,
                    error_code=type(error).__name__,
                    now=self._clock().astimezone(UTC),
                    detail={"error": str(error)[:500]},
                )
            raise

    async def _collect(
        self,
        run_id: str,
        *,
        now: datetime,
        existing: IngestionCursor | None,
        resume_from: datetime | None,
        source: IngestionSource,
    ) -> DataSyncResult:
        calendar_sessions = await self._sessions.sessions()
        completed = latest_completed_session(calendar_sessions, now)
        if completed is None:
            raise StrategyDataError("broker calendar contains no completed session")
        if resume_from is not None and resume_from > completed.close_at:
            raise StrategyDataError("durable collection position is ahead of the calendar")

        # The strategy's warmup is not the only consumer of history any more. The correlation
        # veto measures dependence over its own window, and a store holding only what the signal
        # needs would leave that veto permanently unmeasurable -- refusing every order, correctly
        # and uselessly. Whichever consumer needs more sessions decides how many are fetched.
        needed = max(warmup_sessions(self._config), self._veto_policy.correlation_lookback_sessions)
        history = [
            session for session in calendar_sessions if session.close_at <= completed.close_at
        ]
        if len(history) < needed:
            raise StrategyDataError(
                f"broker calendar provides {len(history)} completed sessions; {needed} required"
            )
        window = tuple(history[-needed:])
        mode = RunMode.LIVE
        if resume_from is not None and resume_from.date() < window[0].session_date:
            if resume_from.date() < history[0].session_date:
                raise StrategyDataError(
                    "RECOVERY_CALENDAR_TOO_SHORT: calendar cannot cover the durable position"
                )
            window = tuple(item for item in history if item.session_date >= resume_from.date())
            mode = RunMode.BACKFILL
        query = BarQuery(
            symbols=self._config.universe,
            start=datetime.combine(window[0].session_date, time.min, tzinfo=UTC),
            end=completed.close_at,
            timeframe=self._market_data.timeframe,
            feed=self._market_data.feed,
            adjustment=self._market_data.adjustment,
            limit=10000,
        )
        with self._database.transaction() as session:
            IngestionControlPlane(session).plan_run(
                run_id, requested_start=query.start, requested_end=query.end, mode=mode
            )
        batch = await self._client.get_historical_bars(query)
        if (
            batch.provider != self._market_data.provider
            or batch.feed != query.feed
            or batch.adjustment != query.adjustment
            or batch.timeframe != query.timeframe
            or set(batch.requested_symbols) != set(query.symbols)
            or batch.start != query.start
            or batch.end != query.end
        ):
            raise MarketDataProtocolError("provider response identity does not match the query")
        aligned = align_daily_bars_to_session_closes(
            batch,
            sessions=session_closes(tuple(calendar_sessions), calendar=self._config.calendar),
        )
        expected = tuple(session.close_at for session in window)
        in_window = tuple(bar for bar in aligned.bars if bar.timestamp >= expected[0])
        unique: dict[tuple[str, datetime], Bar] = {}
        for bar in in_window:
            key = (bar.symbol, bar.timestamp)
            if key in unique and unique[key] != bar:
                raise MarketDataProtocolError(f"conflicting duplicate bar for {bar.symbol}")
            unique[key] = bar
        report = validate_bar_batch(
            aligned.model_copy(update={"bars": tuple(unique.values())}),
            expected_timestamps=expected,
            completed_through=completed.close_at,
        )
        # The observations and the checkpoint that points past them are one transaction. That
        # is the whole guarantee: a cursor that survived a crash its bars did not would make the
        # next run start after data nobody stored, and the gap would be permanent and silent.
        with self._database.transaction() as session:
            plane = IngestionControlPlane(session)
            current_source = plane.get_source(self.source_id)
            if current_source is None or not current_source.enabled:
                raise IngestionError("source was disabled during collection")
            if current_source != source:
                raise CursorConflict("source configuration changed during collection")
            current = plane.cursor(self.source_id, UNIVERSE_STREAM)
            # A reset deletes the row and can recreate version 1. Compare the whole snapshot
            # so a stale fetch cannot mistake that replacement for the original checkpoint.
            if current != existing:
                raise CursorConflict("another collector moved the checkpoint during the fetch")
            stored = MarketDataCache(StorageRepository(session)).store(report)
            counts = RecordCounts(
                seen=len(in_window),
                inserted=stored,
                duplicate=len(in_window) - len(unique) + len(report.valid_bars) - stored,
                rejected=len(unique) - len(report.valid_bars),
            )
            finished_at = self._clock().astimezone(UTC)
            detail = {
                "completed_through": completed.close_at.isoformat(),
                "ineligible_symbols": list(report.ineligible_symbols),
                "gap_count": len(report.gaps),
            }
            if report.ineligible_symbols:
                plane.fail_run(
                    run_id,
                    counts=counts,
                    now=finished_at,
                    error_code="MARKET_DATA_INCOMPLETE_UNIVERSE",
                    detail=detail,
                )
            else:
                plane.complete_run(
                    run_id,
                    counts=counts,
                    now=finished_at,
                    cursor=CursorAdvance(
                        stream_key=UNIVERSE_STREAM,
                        cursor_value=completed.close_at.isoformat(),
                        last_observation_at=(
                            max(bar.timestamp for bar in report.valid_bars)
                            if report.valid_bars
                            else None
                        ),
                        expected_version=None if existing is None else existing.version,
                    ),
                    detail=detail,
                )
                if mode is RunMode.LIVE:
                    plane.resolve_incidents(
                        self.source_id, now=finished_at, detail="complete live collection recovered"
                    )

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
        veto_policy: PortfolioVetoPolicy | None = None,
        exposure_classification: ExposureClassification | None = None,
        anomaly_detector: AnomalyDetectorConfig | None = None,
        catalyst_sources: Sequence[CatalystSource] = (),
        counterfactual_policy: HypotheticalExecutionPolicy | None = None,
        clock: Clock = _utcnow,
    ) -> None:
        self._database = database
        self._broker = broker
        self._config = config
        self._identity = identity
        self._sessions = sessions
        self._market_data = market_data or MarketDataSettings()
        # Defaults rather than optional: an absent policy must mean the shipped limits, not no
        # limits. `PortfolioVetoPolicy` refuses to be constructed with an unimplemented veto
        # enabled, so the default cannot claim protection it does not have (#58).
        self._veto_policy = veto_policy or PortfolioVetoPolicy()
        # Loaded once, here, so `EXPOSURE_CONCENTRATION` has the table it needs at decision time
        # (#75). Without it the veto is correct and useless: an absent classification rejects
        # every order, so enabling the control while leaving it unwired would have halted paper
        # trading in the name of protecting it.
        self._exposure = exposure_classification or load_exposure_classification()
        # #56's detector, running on the feed this runtime actually reads. Its capability gate
        # decides which families may fire: on a venue feed the volume family is refused and the
        # price family is not, which is why the config takes the feed rather than assuming one.
        self._anomaly = anomaly_detector or AnomalyDetectorConfig(feed=self._market_data.feed)
        # A sequence, not one source (#83). `assess_catalysts` has always taken several and
        # applied the rule that matters -- one unhealthy source makes the whole assessment
        # UNKNOWN, however many others answered -- but the runner could only ever supply one, so
        # that rule had nothing to act on.
        #
        # Empty means no source is configured, and enrichment is then simply not attempted.
        # Deliberately not a stub returning an empty result: an empty result from a source nobody
        # configured is indistinguishable from a source that answered and found nothing, which is
        # the exact confusion `catalyst.py` exists to prevent.
        self._catalyst_sources = tuple(catalyst_sources)
        # #57. Frozen before any outcome is computed, and an input to evaluation rather than a
        # parameter of it: choosing an execution assumption after seeing returns is how a
        # hypothetical fill becomes a convenient one. Two outcomes under different policies are
        # two datasets, so the identity travels with every result.
        self._counterfactual = counterfactual_policy or HypotheticalExecutionPolicy()
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

    def _record_anomalies(
        self,
        sliced: Mapping[str, list[Bar]],
        dependence: DependenceSnapshot | None,
        *,
        observed_at: datetime,
    ) -> Mapping[str, str]:
        """Detect and persist anomalies for this cycle, returning each symbol's event id.

        Deliberately separate from the decision path and deliberately unable to affect it. The
        events are written, the dependence grouping is written beside them as its own relation
        (#94), and nothing in this runner reads either when deciding what to submit. The returned
        ids are bound onto refusals as context (#56, #59) and are read by nothing else.

        Failures here are contained -- an anomaly is research context, and losing a cycle of it
        is a gap in a dataset, while letting it stop the cycle would let an observability feature
        halt trading. The containment is real this time: the first version of this method said
        exactly that in its docstring and had no `except` anywhere in it, so a detector raising
        would have taken the cycle down while the comment above it promised otherwise.

        Contained is not silent. A swallowed failure that leaves no trace produces a dataset with
        invisible holes, which is worse for research than a loud one, so the gap is recorded as
        an incident.
        """
        try:
            return self._detect_and_record_anomalies(sliced, dependence, observed_at=observed_at)
        except Exception as error:  # noqa: BLE001 -- deliberately broad; see above
            self._record_contained_failure(
                kind="anomaly-detection",
                message=f"anomaly detection failed for this cycle: {error!r}",
                occurred_at=observed_at,
            )
            return {}

    def _record_contained_failure(self, *, kind: str, message: str, occurred_at: datetime) -> None:
        """Record a contained observability failure, and never raise from doing so.

        A failure inside the handler for a failure must not be the thing that stops the cycle --
        that would reintroduce exactly the coupling containment exists to remove.
        """
        try:
            with self._database.transaction() as session:
                StorageRepository(session).create_incident(
                    incident_id=f"{kind}:{occurred_at.isoformat()}",
                    severity="WARNING",
                    kind=kind,
                    message=message[:2000],
                    occurred_at=occurred_at,
                )
        except Exception:  # noqa: BLE001 -- see above
            return

    def _detect_and_record_anomalies(
        self,
        sliced: Mapping[str, list[Bar]],
        dependence: DependenceSnapshot | None,
        *,
        observed_at: datetime,
    ) -> dict[str, str]:
        """The body of `_record_anomalies`, separated so containment wraps all of it."""
        events = []
        for symbol, bars in sorted(sliced.items()):
            history = [
                SessionObservation(
                    session_date=bar.timestamp.date(),
                    volume=bar.volume,
                    close=bar.close,
                    high=bar.high,
                    low=bar.low,
                )
                for bar in bars
            ]
            event = detect(
                symbol=symbol,
                history=history,
                config=self._anomaly,
                observed_at=observed_at,
            )
            if event is not None:
                events.append(event)
        if not events:
            return {}

        contexts = (
            dependence_contexts(
                events,
                {
                    symbol: f"cluster-{index:02d}"
                    for index, cluster in enumerate(
                        dependence.clusters(
                            sorted(dependence.symbols),
                            threshold=self._veto_policy.correlation_threshold,
                        )
                    )
                    for symbol in cluster
                },
                snapshot_identity=dependence.identity,
            )
            if dependence is not None
            else ()
        )

        with self._database.transaction() as session:
            repository = StorageRepository(session)
            for event in events:
                repository.record_anomaly_event(event.as_row())
            for context in contexts:
                repository.record_anomaly_dependence_context(context.as_row())
        # One event per symbol per cycle by construction: `detect` returns at most one.
        return {event.symbol: event.event_id for event in events}

    async def _record_catalysts(
        self,
        events: Mapping[str, str],
        *,
        observed_at: datetime,
    ) -> dict[str, str]:
        """Ask whether each anomaly had a public explanation, and record the answer.

        Returns symbol to assessment id. Empty when no source is configured, which is the honest
        state: `assess_catalysts` refuses an empty source list because a lookup that never
        happened is not a negative finding, and inventing a `CATALYST_UNKNOWN` row for every
        event would fill the table with records of nothing having been attempted.

        Contained like the other two research recorders, and for the same reason -- this
        describes a decision rather than making one, so a provider outage must cost a row of
        context and never a trading cycle. Contained is not silent: the gap becomes an incident.
        """
        if not self._catalyst_sources or not events:
            return {}

        recorded: dict[str, str] = {}
        try:
            for symbol, event_id in sorted(events.items()):
                # Every configured source is consulted, and all of their results go into one
                # assessment. That is what makes the denominator of a negative finding real: an
                # outage at any one of them makes the whole answer UNKNOWN, rather than the
                # remaining sources quietly standing in for the one that could not answer.
                results = [
                    await source.fetch(
                        [symbol],
                        window_start=observed_at - DEFAULT_LOOKBACK,
                        observed_at=observed_at,
                        retrieved_at=observed_at,
                    )
                    for source in self._catalyst_sources
                ]
                assessment = assess_catalysts(
                    anomaly_event_id=event_id,
                    observed_at=observed_at,
                    sources=results,
                )
                with self._database.transaction() as session:
                    StorageRepository(session).record_catalyst_assessment(assessment.as_row())
                recorded[symbol] = assessment.identity
        except Exception as error:  # noqa: BLE001 -- contained; see above
            self._record_contained_failure(
                kind="catalyst-enrichment",
                message=f"catalyst enrichment failed for this cycle: {error!r}",
                occurred_at=observed_at,
            )
        return recorded

    def _record_capacity_assessment(
        self,
        session: Session,
        *,
        trading_date: date,
        account_id: str | None,
    ) -> str:
        """Run the capital-capacity ladder and persist what it found (#51).

        `capacity_production` has been correct and callerless since it was written. Every
        construction site for `CapacityAssessment` and every call to
        `record_capacity_assessment` in this repository was a test, so `assess_milestone` read an
        empty table and reported the capacity question `UNASSESSED` -- permanently, and not
        because the answer was unknown but because nothing ever asked.

        That is the thirteenth instance of this shape on this branch and the one with the
        longest fuse: `milestone.py` already says `UNASSESSED` means *no production artifact
        exists*, which was true and would have stayed true after the evidence arrived.

        **A refusal is recorded, not skipped.** With no measured benchmark-relative excess the
        ladder returns `UNASSESSED` with `blocking` naming why, and that row is written. Storing
        it is the point: a persisted refusal says the question was asked and could not be
        answered, which is a different fact from silence, and it is the one that lets an operator
        see the measurement start working the day enough sessions exist.

        Contained like the other research recorders. A capacity number is context for judging
        promotion, nothing in the decision path below reads one, and a failure here must cost
        that context rather than a trading cycle.
        """
        from quantbot.research.capacity_production import (
            assess_from_inputs,
            assessment_row,
            build_capacity_inputs,
        )
        from quantbot.research.capital import CostModel
        from quantbot.research.mandate import load_economic_objective

        objective = load_economic_objective()
        cost_model = CostModel()
        provider_key = MarketDataCache.provider_key(
            self._market_data.provider,
            self._market_data.feed,
            self._market_data.adjustment,
            self._market_data.timeframe,
        )
        inputs = build_capacity_inputs(
            session,
            strategy_id=self._identity.strategy_id,
            configuration_hash=self._identity.configuration_hash,
            config=self._config,
            objective=objective,
            cost_model=cost_model,
            as_of=trading_date,
            market_data_source=provider_key,
            account_id=account_id,
            provider=provider_key,
        )
        assessment = assess_from_inputs(inputs, objective=objective, cost_model=cost_model)
        row = assessment_row(assessment, as_of=trading_date)
        StorageRepository(session).record_capacity_assessment(row)
        return str(row["assessment_id"])

    def _evaluate_matured_counterfactuals(
        self,
        repository: StorageRepository,
        calendar_sessions: Sequence[XNYSSession],
        *,
        now: datetime,
    ) -> int:
        """Answer what each elapsed refusal would have cost. Returns how many were answered.

        #57 built `evaluate_counterfactual` and the outcomes table, and nothing ever called
        either -- so even once horizons elapsed, no refusal would have been evaluated. The
        blocker was recorded as "no horizons have elapsed", which was true and hid that the pass
        did not exist. Time was only half of it.

        Deliberately re-derived from stored bars rather than from the cycle's `sliced` window:
        a horizon reaches back further than a decision window, and an evaluator restricted to
        what today's cycle happened to read would silently skip the older refusals.

        Nothing here can affect a decision. It reads refusals, writes outcomes, and returns a
        count; the trading path never reads an outcome.
        """
        sessions = [item.session_date for item in calendar_sessions]
        if not sessions:
            return 0

        benchmark_closes = {
            bar.timestamp.date(): bar.close
            for bar in repository.list_bars(symbol=self._counterfactual.benchmark_symbol)
        }
        closes_by_symbol: dict[str, dict[date, Decimal]] = {}
        answered = 0

        # Skip refusals already answered *under this policy*. Deliberately not keyed on
        # `rejected_candidates.state`: that column never advances, because #57 is explicit that an
        # outcome references the observation rather than amending it -- the refusal keeps saying
        # what was known when it was made. A filter on it would therefore never filter, and every
        # historical refusal would be re-evaluated on every cycle, at a cost growing with exactly
        # the history this is meant to accumulate. (My first version did that, and the test that
        # was supposed to catch it passed on the repository's idempotency instead.)
        #
        # Policy-scoped rather than global, and that is a feature: a different execution policy
        # is a different dataset, so its outcomes have to be computed rather than inherited.
        already = {
            str(row["observation_id"])
            for row in repository.list_counterfactual_outcomes(
                policy_identity=self._counterfactual.identity
            )
        }

        for parent, _reasons in repository.list_rejected_candidates(
            strategy_id=self._identity.strategy_id
        ):
            if str(parent["observation_id"]) in already:
                continue
            symbol = str(parent["symbol"])
            if symbol not in closes_by_symbol:
                closes_by_symbol[symbol] = {
                    bar.timestamp.date(): bar.close for bar in repository.list_bars(symbol=symbol)
                }
            outcome = evaluate_counterfactual(
                observation_id=str(parent["observation_id"]),
                direction=CandidateDirection(str(parent["direction"])),
                reference_price=Decimal(str(parent["reference_price"])),
                decision_session=date.fromisoformat(str(parent["session_date"])),
                policy=self._counterfactual,
                sessions=sessions,
                symbol_closes=closes_by_symbol[symbol],
                benchmark_closes=benchmark_closes,
                evaluated_at=now,
            )
            # `UNAVAILABLE` means the horizon has not elapsed. Storing that would record a
            # non-answer as an answer and make "not yet" indistinguishable from "measured".
            if outcome.state is CounterfactualState.UNAVAILABLE:
                continue
            if repository.record_counterfactual_outcome(outcome.as_row()):
                answered += 1
        return answered

    def _record_regime(
        self,
        sliced: Mapping[str, list[Bar]],
        *,
        as_of_session: date,
        observed_at: datetime,
    ) -> str | None:
        """Record what kind of market this cycle happened in. Returns the snapshot id.

        Contained for the same reason anomaly detection is: this describes the decision, it does
        not make it, so a failure here must cost a row of context rather than a trading cycle.

        `None` on failure or on a benchmark with too little history -- never a default label. A
        regime study that read missing context as ordinary context would find effects in its own
        gaps, which is the specific way this artifact could manufacture a result.
        """
        benchmark = sliced.get(self._config.regime_symbol)
        if not benchmark:
            return None
        try:
            snapshot = build_market_regime_snapshot(
                as_of_session=as_of_session,
                benchmark_symbol=self._config.regime_symbol,
                source=(
                    f"{self._market_data.provider}:{self._market_data.feed.value}"
                    f":{self._market_data.adjustment.value}:1day"
                ),
                benchmark_closes=[bar.close for bar in benchmark],
                universe_closes={
                    symbol: [bar.close for bar in bars]
                    for symbol, bars in sliced.items()
                    if symbol != self._config.regime_symbol and bars
                },
            )
            with self._database.transaction() as session:
                StorageRepository(session).record_market_regime_snapshot(snapshot.as_row())
        except Exception as error:  # noqa: BLE001 -- contained; see above
            self._record_contained_failure(
                kind="market-regime",
                message=f"regime snapshot failed for this cycle: {error!r}",
                occurred_at=observed_at,
            )
            return None
        return snapshot.identity

    def _bind_anomalies_to_regime(
        self,
        anomaly_event_ids: Mapping[str, str],
        snapshot_id: str | None,
        *,
        observed_at: datetime,
    ) -> None:
        """Attach every anomaly from this cycle to its contemporaneous regime snapshot.

        Both records are immutable and may exist independently. The relation is written only
        when both sides exist; a missing regime remains missing rather than becoming a neutral
        label. Binding is research context, so a storage failure is contained and recorded as an
        incident instead of stopping the trading cycle.
        """
        if snapshot_id is None or not anomaly_event_ids:
            return
        try:
            with self._database.transaction() as session:
                repository = StorageRepository(session)
                for anomaly_event_id in sorted(set(anomaly_event_ids.values())):
                    repository.bind_anomaly_regime(
                        anomaly_event_id=anomaly_event_id,
                        snapshot_id=snapshot_id,
                    )
        except Exception as error:  # noqa: BLE001 -- contained research context; see above
            self._record_contained_failure(
                kind="anomaly-regime-binding",
                message=f"anomaly/regime binding failed for this cycle: {error!r}",
                occurred_at=observed_at,
            )

    def _dependence_snapshot(
        self, histories: Mapping[str, list[Bar]], session_window: Sequence[date]
    ) -> DependenceSnapshot | None:
        """Pairwise dependence over an exact calendar window, from point-in-time bars.

        The window comes from the exchange calendar rather than from the stored bars (#84). If
        it were derived from the data, a feed that dropped a session would quietly redefine the
        window it is supposed to be judged against, and every symbol would still look complete.
        """
        if len(session_window) < self._veto_policy.correlation_lookback_sessions:
            return None
        closes: dict[str, dict[date, Decimal]] = {
            symbol: {bar.timestamp.date(): bar.close for bar in bars}
            for symbol, bars in histories.items()
            if bars
        }
        if not closes:
            return None
        return build_dependence_snapshot(
            closes=closes,
            session_window=session_window,
            source=MarketDataCache.provider_key(
                self._market_data.provider,
                self._market_data.feed,
                self._market_data.adjustment,
                self._market_data.timeframe,
            ),
        )

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

    def _tranche_rosters(
        self,
        histories: Mapping[str, list[Bar]],
        calendar_sessions: tuple[XNYSSession, ...],
        acting_index: int,
    ) -> tuple[MonthlyRoster, ...]:
        """One roster per tranche, each evaluated on that tranche's own rebalance session.

        With a single tranche this returns exactly what `_roster` returns, because tranche 0's
        rebalance session is the final session of the month. That equivalence is what lets the
        capability land while switched off.

        Tranches whose first rebalance has not happened yet are omitted rather than represented
        as empty. An empty roster and an unstarted one mean different things — the first says
        "hold nothing", the second says "no opinion yet" — and blending them together would
        quietly dilute every weight during the ramp-up month.
        """
        tranches = self._config.rebalance_tranches
        if tranches == 1:
            return (self._roster(histories, calendar_sessions, acting_index),)

        sequence = sequence_for(self._config, calendar_sessions)
        schedule = sequence.tranche_schedule_map(tranches)
        rosters: list[MonthlyRoster] = []
        for tranche in range(tranches):
            owned = [
                index
                for index, owner in schedule.items()
                if owner == tranche and index < acting_index
            ]
            if not owned:
                continue  # this tranche has not had its first rebalance
            evaluation_index = max(owned)
            evaluation_at = calendar_sessions[evaluation_index].close_at
            rosters.append(
                build_monthly_roster(
                    self._slice(histories, evaluation_at),
                    evaluation_at,
                    calendar_sessions[evaluation_index + 1].open_at,
                    self._config,
                    session_sequence=sequence,
                    tranche=tranche,
                )
            )
        if not rosters:
            raise StrategyDataError("no tranche has reached its first rebalance session")
        return tuple(rosters)

    def _target_fractions(self, rosters: tuple[MonthlyRoster, ...]) -> dict[str, Decimal]:
        """Fractional membership across tranches: held by 3 of 5 means three fifths of a size.

        This is the whole mechanism. Membership stops being binary, which is what averages away
        the arbitrary choice of rebalance date — worth $61.24 of terminal wealth per $100 in
        dispersion that carries no information.
        """
        return tranche_weights([roster.symbols for roster in rosters], Decimal(1))

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
    ) -> tuple[Decimal, Decimal, int] | None:
        """Return the (initial_stop, active_stop, tranche) already durable for this position.

        The tranche defaults to 0 when absent so state written before tranching existed still
        loads. Treating a missing tranche as an error would strand every position opened by a
        prior version, including the ones the live account is holding right now.
        """
        for record in reversed(repository.list_signals(symbol=symbol)):
            state = record.payload.get("position_state")
            if not isinstance(state, dict):
                continue
            if str(state.get("entered_at")) != entered_at.isoformat():
                continue
            return (
                Decimal(str(state["initial_stop"])),
                Decimal(str(state["active_stop"])),
                int(state.get("tranche", 0)),
            )
        return None

    def _position_context(
        self,
        repository: StorageRepository,
        position: Position,
        entered_at: datetime,
        atr: Decimal | None,
    ) -> PositionContext:
        stored = self._prior_position_state(repository, position.symbol, entered_at)
        tranche = 0
        if stored is not None:
            initial_stop, active_stop, tranche = stored
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
            tranche=tranche,
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
        # One snapshot per cycle, from the bars this cycle already read. Built from `sliced` so
        # it sees only what was knowable at the decision, and `None` when the window cannot be
        # filled -- `CORRELATION_CONCENTRATION` then refuses rather than assuming (#58/#59).
        dependence = self._dependence_snapshot(
            sliced,
            [session.session_date for session in calendar_sessions[: evaluation_index + 1]][
                -self._veto_policy.correlation_lookback_sessions :
            ],
        )
        # #56. Observation only: an anomaly is a routing signal for research and holds no
        # broker authority, so nothing below reads these events when deciding what to submit.
        # Built from `sliced` for the same reason the dependence snapshot is -- it must see only
        # what was knowable at the decision.
        anomaly_event_ids = self._record_anomalies(sliced, dependence, observed_at=now)
        regime_snapshot_id = self._record_regime(
            sliced,
            as_of_session=calendar_sessions[evaluation_index].session_date,
            observed_at=now,
        )
        self._bind_anomalies_to_regime(
            anomaly_event_ids,
            regime_snapshot_id,
            observed_at=now,
        )
        research = ResearchContext(
            anomaly_event_ids=anomaly_event_ids,
            regime_snapshot_id=regime_snapshot_id,
            # Enrichment runs only for symbols that actually produced an anomaly. The routing
            # gate measured a 2.18x information-density lift at a 1.76% selection rate, and that
            # 98% saving is the entire reason enrichment is affordable -- asking a news provider
            # about every symbol every cycle would spend it.
            catalyst_ids=await self._record_catalysts(anomaly_event_ids, observed_at=now),
        )
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
            equity_history[-self._config.drawdown_lookback_sessions :]
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
            # #57. Answer any refusal whose horizon has now elapsed, before this cycle records
            # new ones. Contained like the other research recorders: what a refusal would have
            # cost is context for judging the gates, and nothing in the decision path below reads
            # an outcome. A failure here must cost that context and never a trading cycle.
            try:
                self._evaluate_matured_counterfactuals(repository, calendar_sessions, now=now)
            except Exception as error:  # noqa: BLE001 -- contained; see above
                self._record_contained_failure(
                    kind="counterfactual-evaluation",
                    message=f"counterfactual evaluation failed for this cycle: {error!r}",
                    occurred_at=now,
                )
            # #51. Assess capital capacity and persist the answer, including a refusal. The
            # ladder had no production caller at all, so the milestone gate read an empty table
            # and reported the question unassessed forever.
            try:
                self._record_capacity_assessment(
                    session,
                    trading_date=now.date(),
                    account_id=account_snapshot.account.account_id,
                )
            except Exception as error:  # noqa: BLE001 -- contained; see above
                self._record_contained_failure(
                    kind="capacity-assessment",
                    message=f"capacity assessment failed for this cycle: {error!r}",
                    occurred_at=now,
                )
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
                            # Content and provenance of this particular candidate, established
                            # before any gate rules on it. Rerun-stable, and it changes when the
                            # evidence the decision saw changes (#57).
                            candidate_key=decision.bar_set_hash,
                            calendar_sessions=calendar_sessions,
                            dependence=dependence,
                            research=research,
                        )

                if (
                    request is not None
                    and repository.get_order_intent(request.intent.intent_id) is not None
                ):
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
                        # Written unconditionally, including the untranched 0, so state is
                        # self-describing rather than relying on absence to mean anything.
                        "tranche": context.tranche,
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
        candidate_key: str,
        calendar_sessions: Sequence[XNYSSession],
        dependence: DependenceSnapshot | None,
        research: ResearchContext,
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
        # Vetoed **before** the reservation, and the ordering is the fix for a defect this had
        # when first written: reserving first meant a refused order still consumed a position
        # slot and its gross exposure for the rest of the cycle, so a veto on one symbol
        # silently starved every symbol evaluated after it. A reservation is a claim on capacity
        # for an order that is going to be submitted, and a vetoed order is not one.
        #
        # Evaluated here rather than in the coordinator because this is the only place that
        # holds a reference price: a market-order intent carries no notional, and a
        # participation or net-exposure rule cannot be computed without one (#58).
        vetoes = evaluate_portfolio_vetoes(
            build_veto_inputs(
                symbol=symbol,
                equity=account.equity,
                positions=list(broker_positions.values()),
                order_quantity=sized.quantity,
                order_price=decision_price,
                equity_marks=[
                    (snapshot.captured_at, snapshot.equity)
                    for snapshot in repository.list_equity_snapshots(account_id=account.account_id)
                ],
                symbol_bars=[
                    (bar.timestamp, bar.volume) for bar in repository.list_bars(symbol=symbol)
                ],
                observed_at=created_at,
                volume_sessions=self._veto_policy.minimum_volume_sessions,
                classification=self._exposure,
                # The calendar the cycle already read. `STALE_MARKET_DATA` judges the feed
                # against the sessions the market actually completed rather than against a
                # wall clock, so without this it refuses (#74).
                sessions=calendar_sessions,
                dependence=dependence,
                correlation_threshold=self._veto_policy.correlation_threshold,
            ),
            self._veto_policy,
        )
        if not vetoes.allowed:
            # Refused, not resized. A cap that binds returns a smaller order; a veto returns
            # none, because downsizing past a concentration limit still adds the exposure the
            # limit exists to bound. Nothing is reserved, so the next symbol sees the capacity
            # this order did not take.
            #
            # The refusal is kept (#57). Until now a rejection survived only as a free-text
            # string inside the signal payload, which cannot answer "how often did this rule
            # refuse, and at what settings" -- so nothing could tell a valuable veto from an
            # unnecessary one. `COUNTERFACTUAL_UNAVAILABLE` is the honest state: what would have
            # happened is not knowable until the horizon elapses.
            self._record_rejection(
                repository,
                symbol=symbol,
                reference_price=decision_price,
                observed_at=created_at,
                candidate_key=candidate_key,
                vetoes=vetoes,
                research=research,
            )
            return None, vetoes.reasons
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
                vetoes=vetoes,
            ),
            (),
        )

    def _record_rejection(
        self,
        repository: StorageRepository,
        *,
        symbol: str,
        reference_price: Decimal,
        observed_at: datetime,
        candidate_key: str,
        vetoes: PortfolioVetoDecision,
        research: ResearchContext,
    ) -> None:
        """Keep one refusal and every rule that refused it, so #57 can eventually judge them.

        One event, many reasons. A candidate refused by three vetoes is one observation, not
        three: counting reasons as observations would inflate the sample threefold, which is the
        manufacture of sample size that killed cycle 11's overnight result.

        Idempotent by construction. The id is content-addressed on strategy, symbol, session and
        the refusing gates, so re-running a session cannot record the same refusal twice.
        """
        session_date = session_of(observed_at)
        # Read from the promotion record, never from a caller. The recurring defect in this
        # project is a gate accepting the identity it is meant to constrain, and a shadow record
        # that took its claim lineage as an argument would reintroduce it one table over (#57).
        promotion = repository.get_promotion(self._identity.strategy_id)
        reasons = [
            RejectionReason(
                gate_id=rejection.veto.value,
                gate_version=rejection.policy_version,
                rule_parameters_hash=rejection.rule_parameters_hash,
                detail=rejection.reason,
                # The veto assembles its own evidence (#85). Reading its fields here would be a
                # second place to update, and the forgotten one fails silently: the rejection
                # still stores, just without the basis it was made on.
                evidence=rejection.decision_evidence(),
            )
            for rejection in vetoes.rejections
        ]
        candidate = candidate_id(
            strategy_id=self._identity.strategy_id,
            configuration_hash=self._identity.configuration_hash,
            symbol=symbol,
            session_date=session_date,
            candidate_key=candidate_key,
        )
        observation = RejectedCandidate(
            observation_id=observation_id(
                candidate=candidate,
                policy_identity=self._veto_policy.identity,
            ),
            candidate_id=candidate,
            strategy_id=self._identity.strategy_id,
            configuration_hash=self._identity.configuration_hash,
            symbol=symbol,
            # Every entry this strategy proposes is long; a short would need its own direction
            # rather than inheriting this one.
            direction=CandidateDirection.LONG,
            observed_at=observed_at,
            session_date=session_date,
            reference_price=reference_price,
            reasons=tuple(reasons),
            hypothesis_id=None if promotion is None else promotion.hypothesis_id,
            # What the market looked like, bound at the moment of refusal (#56, #59). The
            # columns and the model fields for these existed and nothing ever filled them, so
            # every refusal on record was uncontextualised while the schema implied otherwise.
            # Absent stays `None`: a substituted context cannot be told from a measured one.
            market_regime_snapshot_id=research.regime_snapshot_id,
            anomaly_event_id=research.anomaly_event_ids.get(symbol),
            catalyst_snapshot_id=research.catalyst_ids.get(symbol),
        )
        parent, reason_rows = as_rows(observation)
        repository.record_rejected_candidate(parent, reason_rows)

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
