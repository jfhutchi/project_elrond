"""Narrow retail PAPER reuse. Quantity pagination is never all-in cost authority."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter, _object_payload
from quantbot.brokers.base import BrokerProtocolError
from quantbot.domain import Bar, IntentState, OrderIntent, OrderSide, Position, TimeInForce
from quantbot.learner.five_minute import (
    FEATURE_NAMES,
    HORIZON,
    _index,
    _values,
    build_features,
    build_opening_features,
)
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.base import AdjustmentMode, BarQuery, MarketDataError, MarketDataFeed
from quantbot.market_data.calendar import AlpacaMarketCalendar, session_sequence
from quantbot.market_data.crypto import ALPACA_CRYPTO_QUOTES_URL, AlpacaCryptoDataClient
from quantbot.paper_core.contracts import (
    BrokerEvent,
    CostItem,
    LearningCalendar,
    Observation,
    ObservationCohort,
    OpeningQuoteSource,
    ReconciliationProof,
    RoundTrip,
)
from quantbot.paper_core.costs import current_costs
from quantbot.paper_core.entry_policy import AssetEligibility
from quantbot.paper_core.operational_costs import pending_cost_revision_source
from quantbot.paper_core.release import RuntimeConfig
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.paper_core.time_evidence import BrokerTime, Clocks
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage import StateConflictError, StorageRepository

if TYPE_CHECKING:
    from quantbot.paper_core.discovery import DiscoveryStore
    from quantbot.paper_core.portfolio import EntryQuoteEvidence


class AlpacaCoreBroker:
    provider = "alpaca"
    environment = "PAPER"
    endpoint = "https://paper-api.alpaca.markets"
    cost_revision_batch_size = 128

    def __init__(
        self,
        adapter: AlpacaPaperBrokerAdapter,
        market: AlpacaMarketDataClient,
        calendar: AlpacaMarketCalendar,
        config: RuntimeConfig,
        clocks: Clocks,
        *,
        account_id: str,
        crypto_market: AlpacaCryptoDataClient | None = None,
    ) -> None:
        if type(adapter) is not AlpacaPaperBrokerAdapter or adapter.base_url != self.endpoint:
            raise ValueError("exact existing retail PAPER adapter required")
        self.adapter, self.market, self.calendar = adapter, market, calendar
        self.crypto_market = crypto_market
        self.config, self.clocks, self.account_id = config, clocks, account_id
        self._marks: dict[str, tuple[Decimal, datetime]] = {}
        self.store: CoreStore | None = None
        self.pending_progress: tuple[str, str, str | None, str | None] | None = None
        self.inventory_diagnostic_trace: list[tuple[str, float]] = []
        self.entry_source_objections: tuple[str, ...] = ()
        self.closed_session_window: tuple[datetime, datetime] | None = None
        self._opening_daily_session: str | None = None
        self._opening_daily_cache: dict[str, Bar] = {}
        self._discovery_state: DiscoveryStore | None = None
        self._research_state: DiscoveryStore | None = None
        self._crypto_metadata_failures: dict[str, str] = {}
        self._crypto_metadata_retry_at: dict[str, float] = {}

    @property
    def crypto_symbols(self) -> tuple[str, ...]:
        policy = self.config.entry_policy
        return policy.crypto.symbols if policy is not None and policy.crypto is not None else ()

    @property
    def crypto_metadata_objections(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._crypto_metadata_failures.items()))

    async def get_positions(self) -> tuple[Position, ...]:
        # Every role reads positions. Missing identity metadata retries here without
        # making unrelated stock protection depend on crypto entry permission.
        if self._crypto_metadata_failures:
            await self.initialize_crypto_assets()
        return tuple(await self.adapter.get_positions())

    @property
    def discovery_state(self) -> DiscoveryStore | None:
        policy = self.config.entry_policy
        if policy is not None and policy.dynamic and self._discovery_state is None:
            from quantbot.paper_core.discovery import DiscoveryStore

            self._discovery_state = DiscoveryStore(
                Path(self.config.store_path).with_suffix(".discovery.db")
            )
        return self._discovery_state

    @property
    def research_state(self) -> DiscoveryStore | None:
        """Use the existing observational journal, including during crypto-only hours."""
        if self._research_state is None:
            path = Path(self.config.store_path).with_suffix(".research.db")
            if path.is_file():
                from quantbot.paper_core.discovery import DiscoveryStore

                self._research_state = DiscoveryStore(path)
        return self._research_state

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    @property
    def now(self) -> datetime:
        return self.clocks.sample()[0]

    def mark(self, symbol: str) -> tuple[Decimal, datetime] | None:
        return self._marks.get(symbol)

    def reserve_submission(self, client_order_id: str) -> None:
        from quantbot.paper_core.account_control import BudgetedTransport

        if isinstance(self.adapter._transport, BudgetedTransport):
            self.adapter._transport.reserve_send(client_order_id)

    async def entry_price_evidence(self, symbol: str) -> EntryQuoteEvidence:
        from quantbot.paper_core.portfolio import EntryQuoteEvidence, entry_quote_time_bounds

        policy = self.config.entry_policy
        if self.store is None or not (
            policy.permits(symbol) if policy else symbol in self.store.entry_symbols
        ):
            raise ValueError("ENTRY_QUOTE_SCOPE_UNAVAILABLE")
        crypto = symbol in self.crypto_symbols
        if crypto and self.crypto_market is None:
            raise ValueError("CRYPTO_MARKET_DATA_UNAVAILABLE")
        async with asyncio.timeout(5):
            market = self.crypto_market if crypto else self.market
            assert market is not None
            rows = await market.get_latest_quotes((symbol,))
            # Receipt ordering supplies a fresh provider-domain check while the
            # retained two-sample proof remains the release authority below.
            provider_at = (await self.adapter.get_market_clock()).timestamp
        raw = rows.get(symbol)
        if raw is None:
            raise ValueError("ENTRY_PRICE_UNAVAILABLE_OR_STALE")
        ask, bid = Decimal(str(raw.get("ap"))), Decimal(str(raw.get("bp")))
        at = datetime.fromisoformat(str(raw.get("t")).replace("Z", "+00:00"))
        lower, upper = entry_quote_time_bounds(self.store, now=self.now)
        if (
            not ask.is_finite()
            or not bid.is_finite()
            or min(ask, bid) <= 0
            or ask < bid
            or at.tzinfo is None
            or provider_at.tzinfo is None
            or not 0 <= (provider_at - at).total_seconds() <= 5
            or not at <= lower <= upper <= at + timedelta(seconds=5)
        ):
            raise ValueError("ENTRY_PRICE_UNAVAILABLE_OR_STALE")
        if policy is not None and policy.dynamic and (ask - bid) / bid * 10000 > 10:
            raise ValueError("DISCOVERY_SPREAD_EXCEEDS_COST_RESERVE")
        document = EntryQuoteEvidence(
            recorded_at_utc=self.now,
            evidence_lane="BROKER_PAPER",
            release_id=self.store.config.release_id,
            account_id=self.account_id,
            store_identity=self.store.database.store_identity,
            config_id=self.store.config_id,
            policy_id=self.store.config.policy_id,
            symbol=symbol,
            ask=ask,
            bid=bid,
            source_at=at,
            raw_digest=content_id(raw),
            **({"feed": "crypto", "endpoint": ALPACA_CRYPTO_QUOTES_URL} if crypto else {}),
        )
        self.store.record("core_provider_acquisitions", document)
        return document

    async def refresh_marks(self, symbols: tuple[str, ...]) -> None:
        if not symbols:
            return
        crypto = tuple(s for s in symbols if s.endswith("/USD"))
        equities = tuple(s for s in symbols if s not in crypto)
        acquisitions = []
        if crypto:
            acquisitions.append(self._refresh_crypto_marks(crypto))
        if equities:
            acquisitions.append(self._refresh_equity_marks(equities))
        results = await asyncio.gather(*acquisitions, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _refresh_equity_marks(self, symbols: tuple[str, ...]) -> None:
        now = self.now
        end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        async with asyncio.timeout(5):
            batch = await self.market.get_historical_bars(
                BarQuery(
                    symbols=symbols,
                    start=end - timedelta(minutes=10),
                    end=end - timedelta(microseconds=1),
                    timeframe="5Min",
                    feed=MarketDataFeed.IEX,
                    adjustment=AdjustmentMode.ALL,
                    limit=max(4, 4 * len(symbols)),
                )
            )
        for symbol in symbols:
            bars = [
                b
                for b in batch.bars
                if b.symbol == symbol and b.timestamp + timedelta(minutes=5) <= end
            ]
            if not bars:
                continue  # A missing symbol never receives a fabricated fresh mark.
            last = max(bars, key=lambda b: b.timestamp)
            self._marks[symbol] = (last.close, last.timestamp + timedelta(minutes=5))

    async def _refresh_crypto_marks(self, symbols: tuple[str, ...]) -> None:
        if self.crypto_market is None:
            raise ValueError("CRYPTO_MARKET_DATA_UNAVAILABLE")
        async with asyncio.timeout(5):
            quotes = await self.crypto_market.get_latest_quotes(symbols)
        now = self.now
        for symbol in symbols:
            raw = quotes.get(symbol)
            if raw is None:
                continue
            try:
                bid, ask = Decimal(str(raw.get("bp"))), Decimal(str(raw.get("ap")))
                at = datetime.fromisoformat(str(raw.get("t")).replace("Z", "+00:00"))
                valid = (
                    bid.is_finite()
                    and ask.is_finite()
                    and 0 < bid <= ask
                    and at.tzinfo is not None
                    and 0 <= (now - at).total_seconds() <= 5
                )
            except (ValueError, TypeError, ArithmeticError):
                continue
            if valid:
                # Protective stops need a current executable bid throughout the
                # five-minute research interval, not an increasingly stale bar.
                self._marks[symbol] = (bid, at)

    async def refresh_session_capital(self, proof: ReconciliationProof) -> str | None:
        from quantbot.paper_core.capital import acquire_session_capital

        if self.store is None:
            raise ValueError("SESSION_CAPITAL_STORE_UNAVAILABLE")
        async with asyncio.timeout(5):
            return await acquire_session_capital(self, self.store, proof)

    async def get_clock(self) -> BrokerTime:
        self.closed_session_window = None
        clock = await self.adapter.get_market_clock()
        sessions = await self.calendar.get_sessions(
            clock.timestamp.date() - timedelta(days=7), clock.timestamp.date() + timedelta(days=7)
        )
        matching = [s for s in sessions if s.open_at <= clock.timestamp < s.close_at]
        if not matching:
            matching = [s for s in sessions if s.open_at > clock.timestamp]
        if not matching:
            raise BrokerProtocolError("CLOCK_CALENDAR_SESSION_UNAVAILABLE")
        session = matching[0]
        previous_closes = [s.close_at for s in sessions if s.close_at <= clock.timestamp]
        if not clock.is_open and clock.timestamp < session.open_at and previous_closes:
            self.closed_session_window = (max(previous_closes), session.open_at)
        return BrokerTime(
            timestamp=clock.timestamp,
            account_id=self.account_id,
            provider=self.provider,
            session_id=session.session_date.isoformat(),
            session_open=session.open_at,
            session_close=session.close_at,
            resolution_seconds=0.000001,
        )

    async def learning_calendar(self) -> LearningCalendar:
        sessions = await self.calendar.get_sessions(
            self.config.learning_origin.date(), self.now.date() + timedelta(days=7)
        )
        return LearningCalendar(
            sequence=session_sequence(sessions), authority="ALPACA_PAPER_XNYS_V1"
        )

    async def observation(self) -> Observation:
        captured = await self._acquire_cohort((self.config.core.symbol,), verify_assets=False)
        if not captured.observations:
            raise ValueError("FRESH_COMPLETE_BAR_UNAVAILABLE")
        return captured.observations[0]

    async def observation_cohort(self) -> ObservationCohort:
        if not self.crypto_symbols:
            return await self._equity_observation_cohort()
        from quantbot.paper_core.crypto_policy import account_risk_day_bounds

        now = self.now
        boundary = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        equity, crypto = await asyncio.gather(
            self._equity_observation_cohort(),
            self.acquire_crypto_cohort(),
            return_exceptions=True,
        )
        observations: list[Observation] = []
        unavailable: list[tuple[str, str]] = []
        gaps: list[str] = []
        discovery = None
        if isinstance(equity, BaseException):
            if not isinstance(equity, Exception):
                raise equity
            gaps.append("EQUITY:" + self._market_failure_reason(equity))
        else:
            boundary = equity.source_end
            observations.extend(equity.observations)
            unavailable.extend(equity.unavailable)
            discovery = equity.discovery
        if isinstance(crypto, BaseException):
            if not isinstance(crypto, Exception):
                raise crypto
            reason = self._market_failure_reason(crypto)
            gaps.append("CRYPTO:" + reason)
            unavailable.extend((symbol, reason) for symbol in self.crypto_symbols)
        elif not isinstance(equity, BaseException) and crypto.source_end != boundary:
            # Opening equity quotes retain their true receipt boundary. Do not shift
            # completed crypto bars or mislabel their availability to force a match.
            reason = "SOURCE_BOUNDARY_MISMATCH"
            gaps.append("CRYPTO:" + reason)
            unavailable.extend((symbol, reason) for symbol in self.crypto_symbols)
        else:
            if isinstance(equity, BaseException):
                boundary = crypto.source_end
            observations.extend(crypto.observations)
            unavailable.extend(crypto.unavailable)
        opening, closing = account_risk_day_bounds(boundary)
        return ObservationCohort(
            source_end=boundary,
            session_open=opening,
            session_close=closing,
            observations=tuple(observations),
            unavailable=tuple(unavailable),
            discovery=discovery,
            crypto_symbols=self.crypto_symbols,
            market_gaps=tuple(gaps),
        )

    @staticmethod
    def _market_failure_reason(error: Exception) -> str:
        # Protocol reason tokens are safe and useful; never copy an arbitrary raw
        # response or transport exception into a retained observation envelope.
        reason = str(error)
        if reason and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in reason):
            return reason
        return type(error).__name__

    async def _equity_observation_cohort(self) -> ObservationCohort:
        if self.discovery_state is not None:
            evidence = self.discovery_state.select_candidates(
                now=self.now, boundary=str(int(self.now.timestamp() // 300))
            )
            cohort = await self._acquire_cohort(evidence.symbols, verify_assets=True)
            from quantbot.paper_core.discovery_research import research_context

            try:
                if self._research_state is None:
                    from quantbot.paper_core.discovery import DiscoveryStore

                    self._research_state = DiscoveryStore(
                        Path(self.config.store_path).with_suffix(".research.db")
                    )
                refs, gaps = research_context(self._research_state, evidence.symbols, now=self.now)
            except Exception as error:
                # Observational research never grants or removes stock entry authority.
                refs, gaps = (), ("RESEARCH_UNAVAILABLE:" + type(error).__name__,)
            evidence = evidence.model_copy(update={"research_refs": refs, "research_gaps": gaps})
            return cohort.model_copy(update={"discovery": evidence})
        return await self._acquire_cohort(self.config.entry_symbols, verify_assets=True)

    async def entry_asset_eligibility(self, symbol: str) -> AssetEligibility:
        policy = self.config.entry_policy
        if policy is None or not policy.permits(symbol):
            raise ValueError("ASSET_ELIGIBILITY_POLICY_MISMATCH")
        payload = _object_payload(
            await self.adapter._request("GET", "/v2/assets/" + quote(symbol, safe=""))
        )
        crypto = symbol in self.crypto_symbols
        if (
            payload.get("symbol") != symbol
            or payload.get("class") != ("crypto" if crypto else "us_equity")
            or payload.get("status") != "active"
            or payload.get("tradable") is not True
            or type(payload.get("fractionable")) is not bool
        ):
            raise ValueError("ASSET_ELIGIBILITY_INVALID")
        terms: dict[str, Any] = {}
        digest: Any = payload
        if crypto:
            terms.update(self._crypto_asset_terms(payload))
            account = _object_payload(await self.adapter._request("GET", "/v2/account"))
            if (
                account.get("id") != self.account_id
                or account.get("status") != "ACTIVE"
                or account.get("crypto_status") != "ACTIVE"
                or any(
                    account.get(name) is not False
                    for name in ("trading_blocked", "account_blocked", "trade_suspended_by_user")
                )
            ):
                raise ValueError("CRYPTO_ACCOUNT_INELIGIBLE")
            terms.update(
                asset_class="crypto", time_in_force=TimeInForce.GTC, crypto_status="ACTIVE"
            )
            digest = {
                "asset": payload,
                "account_eligibility": {
                    key: account[key]
                    for key in (
                        "id",
                        "status",
                        "crypto_status",
                        "trading_blocked",
                        "account_blocked",
                        "trade_suspended_by_user",
                    )
                },
            }
            self.adapter.register_crypto_asset(payload)
            self._crypto_metadata_failures.pop(symbol, None)
            self._crypto_metadata_retry_at.pop(symbol, None)
        return AssetEligibility(
            symbol=symbol,
            fractionable=payload["fractionable"],
            observed_at=self.now,
            raw_digest=content_id(digest),
            entry_policy_id=policy.id,
            **terms,
        )

    @staticmethod
    def _crypto_asset_terms(payload: Mapping[str, object]) -> dict[str, Decimal]:
        if payload.get("fractionable") is not True:
            raise ValueError("ASSET_ELIGIBILITY_INVALID")
        try:
            terms = {
                name: Decimal(str(payload[source]))
                for name, source in (
                    ("minimum_quantity", "min_order_size"),
                    ("quantity_step", "min_trade_increment"),
                    ("price_increment", "price_increment"),
                )
            }
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise ValueError("ASSET_ELIGIBILITY_INVALID") from error
        if any(not value.is_finite() or value <= 0 for value in terms.values()):
            raise ValueError("ASSET_ELIGIBILITY_INVALID")
        return terms

    async def initialize_crypto_assets(self) -> tuple[tuple[str, str], ...]:
        """Capture exact broker identity; per-symbol failures never abort role startup.

        Concurrent requests are individually capped at one second. Failed symbols
        wait thirty monotonic seconds before another read-side attempt. Cached
        identity never replaces the fresh entry eligibility check.
        """
        monotonic = self.clocks.sample()[1]
        aliases = self.adapter.crypto_symbol_aliases

        async def capture(symbol: str) -> None:
            self._crypto_metadata_retry_at[symbol] = monotonic + 30
            try:
                async with asyncio.timeout(1):
                    payload = _object_payload(
                        await self.adapter._request("GET", "/v2/assets/" + quote(symbol, safe=""))
                    )
                if payload.get("symbol") != symbol or payload.get("class") != "crypto":
                    raise ValueError("CRYPTO_ASSET_IDENTITY_INVALID")
                self.adapter.register_crypto_asset_identity(payload)
            except (ValueError, OSError, TimeoutError, RuntimeError) as error:
                self._crypto_metadata_failures[symbol] = (
                    "CRYPTO_ASSET_METADATA_UNAVAILABLE:" + type(error).__name__
                )
            else:
                self._crypto_metadata_failures.pop(symbol, None)
                self._crypto_metadata_retry_at.pop(symbol, None)

        await asyncio.gather(
            *(
                capture(symbol)
                for symbol in self.crypto_symbols
                if symbol not in aliases
                and monotonic >= self._crypto_metadata_retry_at.get(symbol, monotonic)
            )
        )
        return self.crypto_metadata_objections

    async def acquire_crypto_cohort(
        self, symbols: tuple[str, ...] | None = None
    ) -> ObservationCohort:
        """Acquire the bounded spot universe independently of equity exchange hours."""
        from quantbot.paper_core.crypto_policy import CryptoMarketSession, crypto_session_bounds

        symbols = self.crypto_symbols if symbols is None else symbols
        if (
            not symbols
            or len(set(symbols)) != len(symbols)
            or not set(symbols) <= set(self.crypto_symbols)
        ):
            raise ValueError("CRYPTO_COHORT_SCOPE_INVALID")
        if self.crypto_market is None:
            raise ValueError("CRYPTO_MARKET_DATA_UNAVAILABLE")
        now = self.now
        end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        opening, closing = crypto_session_bounds(end)
        batch = await self.crypto_market.get_historical_bars(
            BarQuery(
                symbols=tuple(sorted({*symbols, "BTC/USD"})),
                start=end - timedelta(minutes=35),
                end=end - timedelta(microseconds=1),
                timeframe="5Min",
                feed=MarketDataFeed.CRYPTO,
                adjustment=AdjustmentMode.RAW,
                limit=max(100, 8 * (len(symbols) + 1)),
            )
        )
        results = await asyncio.gather(*(self._policy_asset_result(s) for s in symbols))
        received = self.now
        indexed = _index(batch.bars)
        stamps = tuple(end - timedelta(minutes=35 - i * 5) for i in range(7))
        observations, unavailable = [], []
        for symbol, (reason, asset) in zip(symbols, results, strict=True):
            if reason or asset is None:
                unavailable.append((symbol, reason or "ASSET_ELIGIBILITY_UNAVAILABLE"))
                continue
            if not 0 <= (received - end).total_seconds() <= 120:
                unavailable.append((symbol, "CRYPTO_OBSERVATION_STALE"))
                continue
            if any((name, at) not in indexed for name in {symbol, "BTC/USD"} for at in stamps):
                unavailable.append((symbol, "CRYPTO_CONTIGUOUS_HISTORY_UNAVAILABLE"))
                continue
            values = _values(
                tuple(indexed[symbol, at] for at in stamps),
                tuple(indexed["BTC/USD", at] for at in stamps),
            )
            last = indexed[symbol, stamps[-1]]
            observation = Observation(
                recorded_at_utc=received,
                evidence_lane="BROKER_PAPER",
                release_id=self.store.config.release_id
                if self.store
                else self.config.core.release_id,
                symbol=symbol,
                feed="alpaca-crypto",
                source_start=stamps[0],
                source_end=end,
                available_at=received,
                capture_received_at=received,
                price=last.close,
                raw_data_digest=content_id(batch),
                **({"price_basis": last.price_basis} if last.price_basis is not None else {}),
                feature_version="crypto-five-minute-price-alpha-v1",
                feature_context="CRYPTO_CONTINUOUS_V1",
                features=tuple(zip(FEATURE_NAMES, values, strict=True)),
                dependencies=("BTC/USD",),
                dependency_available_at=(received,),
                sequence=int(end.timestamp() // 300),
                session_open=opening,
                session_close=closing,
                asset_eligibility=asset,
                crypto_session=CryptoMarketSession(
                    symbol=symbol,
                    benchmark="BTC/USD",
                    session_open=opening,
                    session_close=closing,
                    observed_at=end,
                    asset_eligibility_id=asset.id,
                ),
            )
            observations.append(observation)
            self._marks[symbol] = (last.close, end)
        return ObservationCohort(
            source_end=end,
            session_open=opening,
            session_close=closing,
            observations=tuple(observations),
            unavailable=tuple(unavailable),
        )

    async def _policy_asset_result(self, symbol: str) -> tuple[str | None, AssetEligibility | None]:
        try:
            return None, await self.entry_asset_eligibility(symbol)
        except (ValueError, OSError, TimeoutError, RuntimeError) as error:
            return "ASSET_ELIGIBILITY_UNAVAILABLE:" + type(error).__name__, None

    async def _asset_objection(self, symbol: str) -> str | None:
        try:
            payload = _object_payload(
                await self.adapter._request("GET", "/v2/assets/" + quote(symbol, safe=""))
            )
        except (ValueError, OSError, TimeoutError, RuntimeError) as error:
            return "ASSET_ELIGIBILITY_UNAVAILABLE:" + type(error).__name__
        if payload.get("symbol") != symbol or payload.get("class") != "us_equity":
            return "ASSET_CONTRACT_MISMATCH"
        if payload.get("status") != "active" or payload.get("tradable") is not True:
            return "ASSET_NOT_TRADABLE"
        if payload.get("fractionable") is not True:
            return "ASSET_NOT_FRACTIONABLE"
        return None

    async def _acquire_cohort(
        self,
        symbols: tuple[str, ...],
        *,
        verify_assets: bool,
    ) -> ObservationCohort:
        now = self.now
        clock = await self.adapter.get_market_clock()
        if not clock.is_open:
            raise ValueError("MARKET_CLOSED")
        sessions = await self.calendar.get_sessions(
            now.date() - timedelta(days=7), now.date() + timedelta(days=7)
        )
        sequence = session_sequence(sessions)
        end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        session = next(s for s in sessions if s.open_at <= end < s.close_at)
        if end + HORIZON > session.close_at:
            return ObservationCohort(
                source_end=end,
                session_open=session.open_at,
                session_close=session.close_at,
                observations=(),
                unavailable=tuple(
                    (symbol, "STOCK_SIGNAL_HORIZON_EXCEEDS_SESSION") for symbol in symbols
                ),
            )
        query = BarQuery(
            symbols=tuple(sorted({*symbols, "SPY"})),
            start=end - timedelta(minutes=40),
            end=end - timedelta(microseconds=1),
            timeframe="5Min",
            feed=MarketDataFeed.IEX,
            adjustment=AdjustmentMode.ALL,
            limit=max(100, 8 * (len(symbols) + 1)),
        )
        batch = await self.market.get_historical_bars(query)
        received = self.now
        opening = end < session.open_at + timedelta(minutes=35)
        restrictions: list[str | None] = (
            await asyncio.gather(*(self._asset_objection(s) for s in symbols))
            if verify_assets and self.config.entry_policy is None
            else [None] * len(symbols)
        )
        asset_receipts: dict[str, AssetEligibility] = {}
        if self.config.entry_policy is not None:
            results = await asyncio.gather(*(self._policy_asset_result(s) for s in symbols))
            restrictions = [reason for reason, _ in results]
            asset_receipts = {
                s: receipt
                for s, (_, receipt) in zip(symbols, results, strict=True)
                if receipt is not None
            }
            received = self.now
        observations = []
        unavailable = []
        for symbol, restriction in zip(symbols, restrictions, strict=True):
            if restriction:
                unavailable.append((symbol, restriction))
                continue
            try:
                build = build_opening_features if opening else build_features
                rows = build(
                    tuple(b for b in batch.bars if b.symbol in {symbol, "SPY"}),
                    sequence,
                    decision_at=received,
                    receipt_at=received,
                    symbols=(symbol,),
                    source_snapshot_hash=content_id(batch),
                )
                matching = [r for r in rows if r.feature_at == end]
                if not matching:
                    reason = (
                        "OPENING_HISTORY_UNAVAILABLE"
                        if opening
                        else "FRESH_COMPLETE_BAR_UNAVAILABLE"
                    )
                    unavailable.append((symbol, reason))
                    continue
                row = matching[-1]
                latest = max(
                    (b for b in batch.bars if b.symbol == symbol and b.timestamp < end),
                    key=lambda b: b.timestamp,
                )
            except ValueError:
                unavailable.append((symbol, "FEATURE_INPUT_INVALID"))
                continue
            self._marks[symbol] = (latest.close, end)
            observations.append(
                Observation(
                    recorded_at_utc=received,
                    evidence_lane="BROKER_PAPER",
                    release_id=self.store.config.release_id
                    if self.store
                    else self.config.core.release_id,
                    symbol=symbol,
                    feed="alpaca",
                    source_start=end - timedelta(minutes=35),
                    source_end=end,
                    available_at=received,
                    capture_received_at=received,
                    price=latest.close,
                    raw_data_digest=content_id(batch),
                    feature_version="five-minute-price-alpha-v1",
                    **({"feature_context": "OPENING_CONTIGUOUS_V1"} if opening else {}),
                    features=tuple(zip(FEATURE_NAMES, row.values, strict=True)),
                    dependencies=("SPY",),
                    dependency_available_at=(received,),
                    sequence=int(end.timestamp() // 300),
                    session_open=session.open_at,
                    session_close=session.close_at,
                    **(
                        {"asset_eligibility": asset_receipts[symbol]}
                        if symbol in asset_receipts
                        else {}
                    ),
                )
            )
        if (
            opening
            and not observations
            and any(reason == "OPENING_HISTORY_UNAVAILABLE" for _, reason in unavailable)
        ):
            return await self._opening_quote_cohort(
                symbols, sessions, session, restrictions, asset_receipts
            )
        return ObservationCohort(
            source_end=end,
            session_open=session.open_at,
            session_close=session.close_at,
            observations=tuple(observations),
            unavailable=tuple(unavailable),
        )

    async def _opening_quote_cohort(
        self,
        symbols: tuple[str, ...],
        sessions: Any,
        session: Any,
        restrictions: list[str | None],
        asset_receipts: dict[str, AssetEligibility],
    ) -> ObservationCohort:
        """Independent opening rule inputs: previous completed day and current quotes."""
        previous = max(
            (s for s in sessions if s.close_at < session.open_at),
            key=lambda s: s.close_at,
            default=None,
        )
        required = tuple(sorted({*symbols, "SPY"}))
        if previous is not None:
            previous_key = previous.session_date.isoformat()
            if previous_key != self._opening_daily_session:
                self._opening_daily_cache.clear()
                self._opening_daily_session = previous_key
            missing = tuple(s for s in required if s not in self._opening_daily_cache)
            if missing:
                start = datetime.combine(
                    previous.session_date, datetime.min.time(), tzinfo=ZoneInfo("America/New_York")
                ).astimezone(UTC)
                daily = await self.market.get_historical_bars(
                    BarQuery(
                        symbols=missing,
                        start=start,
                        end=previous.close_at,
                        timeframe="1Day",
                        feed=MarketDataFeed.IEX,
                        adjustment=AdjustmentMode.ALL,
                        limit=max(100, len(missing)),
                    )
                )
                for symbol in missing:
                    matches = [
                        bar
                        for bar in daily.bars
                        if bar.symbol == symbol
                        and bar.timestamp.astimezone(ZoneInfo("America/New_York")).date()
                        == previous.session_date
                        and bar.timestamp < previous.close_at
                    ]
                    if len(matches) == 1 and matches[0].close > 0:
                        self._opening_daily_cache[symbol] = matches[0]
                while len(self._opening_daily_cache) > 512:
                    self._opening_daily_cache.pop(next(iter(self._opening_daily_cache)))
        quotes = {}
        if "SPY" in self._opening_daily_cache and any(
            symbol in self._opening_daily_cache for symbol in symbols
        ):
            try:
                quotes = await self.market.get_latest_quotes(required)
            except MarketDataError:
                # Failure is recorded per symbol below; it never authorizes an entry.
                pass
        captured = self.now
        # This provider receipt only screens acquisition. The independent two-sample
        # PAPER_TIME_V2 proof still authorizes evaluation, claim and send.
        provider_at = (await self.adapter.get_market_clock()).timestamp if quotes else captured
        observations: list[Observation] = []
        unavailable: list[tuple[str, str]] = []
        for symbol, restriction in zip(symbols, restrictions, strict=True):
            if restriction:
                unavailable.append((symbol, restriction))
                continue
            if not session.open_at <= captured < session.open_at + timedelta(minutes=35):
                unavailable.append((symbol, "OPENING_WINDOW_ENDED"))
                continue
            if previous is None or any(s not in self._opening_daily_cache for s in (symbol, "SPY")):
                unavailable.append((symbol, "OPENING_HISTORY_UNAVAILABLE"))
                continue
            sources = []
            try:
                for name in (symbol, "SPY"):
                    raw = quotes[name]
                    at = datetime.fromisoformat(str(raw["t"]).replace("Z", "+00:00"))
                    bid, ask = Decimal(str(raw["bp"])), Decimal(str(raw["ap"]))
                    if (
                        at.tzinfo is None
                        or at < session.open_at
                        or provider_at.tzinfo is None
                        or not 0 <= (provider_at - at).total_seconds() <= 5
                        or not bid.is_finite()
                        or not ask.is_finite()
                        or not 0 < bid <= ask
                    ):
                        raise ValueError("invalid current quote")
                    bar = self._opening_daily_cache[name]
                    sources.append(
                        OpeningQuoteSource(
                            symbol=name,
                            previous_session_open=previous.open_at,
                            previous_session_close=previous.close_at,
                            previous_bar_timestamp=bar.timestamp,
                            previous_close=bar.close,
                            quote_at=at,
                            bid=bid,
                            ask=ask,
                        )
                    )
            except (KeyError, ValueError, TypeError, ArithmeticError):
                unavailable.append((symbol, "OPENING_QUOTES_UNAVAILABLE"))
                continue
            asset, market = sources
            gap = (asset.bid + asset.ask) / 2 / asset.previous_close - 1
            market_gap = (market.bid + market.ask) / 2 / market.previous_close - 1
            spread = (asset.ask - asset.bid) / asset.bid * 10000
            self._marks[symbol] = (asset.bid, asset.quote_at)
            observations.append(
                Observation(
                    recorded_at_utc=captured,
                    evidence_lane="BROKER_PAPER",
                    release_id=self.store.config.release_id
                    if self.store
                    else self.config.core.release_id,
                    symbol=symbol,
                    feed="alpaca",
                    source_start=previous.close_at,
                    source_end=captured,
                    available_at=captured,
                    capture_received_at=captured,
                    price=asset.ask,
                    raw_data_digest=content_id(
                        dict(
                            sources=sources,
                            quotes={name: quotes[name] for name in (symbol, "SPY")},
                            feed="iex",
                            adjustment="all",
                        )
                    ),
                    feature_version="opening-quote-gap-v1",
                    feature_context="OPENING_QUOTE_GAP_V1",
                    opening_quote_sources=(asset, market),
                    features=(
                        ("gap_return", float(gap)),
                        ("market_gap_return", float(market_gap)),
                        ("relative_gap", float(gap - market_gap)),
                        ("spread_bps", float(spread)),
                    ),
                    dependencies=("SPY",),
                    dependency_available_at=(market.quote_at,),
                    sequence=int(captured.timestamp() // 300),
                    session_open=session.open_at,
                    session_close=session.close_at,
                    **(
                        {"asset_eligibility": asset_receipts[symbol]}
                        if symbol in asset_receipts
                        else {}
                    ),
                )
            )
        return ObservationCohort(
            # With no observed quotes this is only a failed boundary acquisition.
            # Preserve its per-symbol reasons without inventing a quote timestamp.
            source_end=captured
            if observations
            else captured.replace(minute=captured.minute // 5 * 5, second=0, microsecond=0),
            session_open=session.open_at,
            session_close=session.close_at,
            observations=tuple(observations),
            unavailable=tuple(unavailable),
        )

    async def refresh_mark(self, symbol: str) -> None:
        # Stop observations have their own narrow acquisition; entry waits do not prevent it.
        if symbol.endswith("/USD"):
            await self._refresh_crypto_marks((symbol,))
            return
        now = self.now
        end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        batch = await self.market.get_historical_bars(
            BarQuery(
                symbols=(symbol,),
                start=end - timedelta(minutes=10),
                end=end - timedelta(microseconds=1),
                timeframe="5Min",
                feed=MarketDataFeed.IEX,
                adjustment=AdjustmentMode.ALL,
                limit=4,
            )
        )
        if batch.bars:
            last = max(batch.bars, key=lambda b: b.timestamp)
            self._marks[symbol] = (last.close, last.timestamp + timedelta(minutes=5))

    async def owned_events(self, store: CoreStore) -> tuple[BrokerEvent, ...]:
        from quantbot.paper_core.authority import commission_in_session
        from quantbot.storage import StorageRepository

        with store.database.transaction() as session:
            session.execute(text("BEGIN"))
            commission = commission_in_session(store, session)
            repo = StorageRepository(session)
            progress = (
                session.execute(
                    select(table("core_provider_progress")).where(
                        table("core_provider_progress").c.account_id == self.account_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            cursor = (
                datetime.fromisoformat(progress["watermark"])
                if progress
                else commission.approved_at
            )
        # Replay overlaps creation dates to retain late fees. No empty-day zero inference.
        until = min(self.now, cursor + timedelta(hours=6))
        after = max(commission.approved_at - timedelta(days=2), cursor - timedelta(days=2))
        async with asyncio.timeout(5):
            rows = await self.adapter.get_performance_activities(after=after, until=until)
            fills = tuple(
                self.adapter._fill_from_payload(self.adapter.normalize_crypto_payload(r))
                for r in rows
                if r.get("activity_type") == "FILL"
            )
            # An order may be durably created/sent while activities are in flight.
            # Snapshot intent state after those activities, so every newly visible
            # owned fill is eligible for its normal paired acknowledgement reads.
            with store.database.transaction() as session:
                session.execute(text("BEGIN"))
                repo = StorageRepository(session)
                relevant = session.execute(
                    select(table("core_intent_links").c.intent_id)
                    .join(
                        table("order_intents"),
                        table("order_intents").c.intent_id
                        == table("core_intent_links").c.intent_id,
                    )
                    .where(
                        (
                            table("order_intents").c.created_at
                            >= (self.now - timedelta(days=2)).isoformat()
                        )
                        | table("order_intents").c.state.not_in(
                            ("FILLED", "CANCELLED", "EXPIRED", "REJECTED", "ERROR")
                        )
                        | table("core_intent_links").c.lot_id.not_in(
                            select(table("core_round_trips").c.id)
                        )
                    )
                ).scalars()
                from quantbot.paper_core.settlement import definitively_unsent_many

                current_intents = repo.get_order_intents(list(relevant))
                unsent = definitively_unsent_many(
                    repo, [intent.intent_id for intent in current_intents if intent is not None]
                )
                intents = [
                    intent
                    for intent in current_intents
                    if intent is None or not unsent[intent.intent_id]
                ]
                settled = {
                    intent.intent_id: event
                    for intent in intents
                    if intent is not None
                    and (event := self._settled_order_event(store, session, intent)) is not None
                }
            events = []
            pending_owned_orders = []
            batch_reads = None
            if store.parallel_entries:
                active = [
                    i
                    for i in intents
                    if i is not None
                    and i.intent_id not in settled
                    and i.state not in {IntentState.RISK_APPROVED, IntentState.ORDER_CREATED}
                ]
                if active:
                    lower = min(i.created_at for i in active) - timedelta(seconds=1)
                    first_batch = await self.adapter.get_orders(after=lower)
                    second_batch = await self.adapter.get_orders(after=lower)
                    if len({o.client_order_id for o in first_batch}) != len(first_batch) or len(
                        {o.client_order_id for o in second_batch}
                    ) != len(second_batch):
                        raise BrokerProtocolError("DUPLICATE_BULK_ORDER_CLIENT_ID")
                    batch_reads = (
                        dict((o.client_order_id, o) for o in first_batch),
                        dict((o.client_order_id, o) for o in second_batch),
                    )
            for intent in intents:
                assert intent is not None
                contradiction = None
                if intent.intent_id in settled:
                    historical = settled[intent.intent_id]
                    acquired = (
                        [batch.get(intent.client_order_id) for batch in batch_reads]
                        if batch_reads
                        else []
                    )
                    if all(order is None or order == historical.order for order in acquired):
                        # Replay proof only when no actually acquired order contradicts it.
                        # A contradiction must reach the normal scoped import/quarantine path.
                        events.append(historical)
                        continue
                    contradiction = next(
                        order
                        for order in acquired
                        if order is not None and order != historical.order
                    )
                if store.parallel_entries:
                    if intent.state in {IntentState.RISK_APPROVED, IntentState.ORDER_CREATED}:
                        continue  # Retained pre-send lifecycle proves no order exists yet.
                    first = batch_reads[0].get(intent.client_order_id) if batch_reads else None
                    second = batch_reads[1].get(intent.client_order_id) if batch_reads else None
                    if first is None and second is not None:
                        first = second
                        second = None  # One acknowledgement never establishes stable completion.
                    if contradiction is not None:
                        first, second = contradiction, None
                else:
                    first = await self.adapter.get_order_by_client_id(
                        intent.client_order_id, expected=intent
                    )
                    second = (
                        await self.adapter.get_order_by_client_id(
                            intent.client_order_id, expected=intent
                        )
                        if first is not None
                        else None
                    )
                if first is None:
                    continue  # Recovery keeps its original reservation and client ID.
                owned = tuple(f for f in fills if f.broker_order_id == first.broker_order_id)
                complete = (
                    first == second
                    and sum((f.quantity for f in owned), Decimal(0)) == (first.filled_quantity)
                    and all(f.occurred_at <= until for f in owned)
                )
                if first == second and (
                    sum((f.quantity for f in owned), Decimal(0)) < first.filled_quantity
                ):
                    pending_owned_orders.append(first)
                at = self.now
                events.append(
                    BrokerEvent(
                        recorded_at_utc=at,
                        occurred_at=at,
                        evidence_lane="BROKER_PAPER",
                        release_id=store.config.release_id,
                        account_id=self.account_id,
                        provider="alpaca",
                        endpoint=self.endpoint + "/v2/orders",
                        client_order_id=intent.client_order_id,
                        event_id=content_id({"order": first, "complete": complete}),
                        raw_digest=content_id(first),
                        order=first,
                        activity_complete=complete,
                        activity_watermark=until if complete else None,
                        activity_fill_ids=tuple(f.fill_id for f in owned),
                    )
                )
            # Activities are independent of the order-poll selection. In particular,
            # a reused fill ID under another order must reach immutable conflict checks.
            acquired_orders = {
                event.order.broker_order_id: event.order for event in events if event.order
            }
            with store.database.transaction() as session:
                repo = StorageRepository(session)
                clients = set(
                    session.execute(
                        select(table("order_intents").c.client_order_id)
                        .join(
                            table("core_intent_links"),
                            table("core_intent_links").c.intent_id
                            == table("order_intents").c.intent_id,
                        )
                        .where(table("order_intents").c.strategy_id == store.config.owner)
                    ).scalars()
                )
                for fill in fills:
                    canonical = repo.get_fill(fill.fill_id)
                    original = (
                        repo.get_broker_order(canonical.broker_order_id) if canonical else None
                    )
                    incoming = acquired_orders.get(fill.broker_order_id) or repo.get_broker_order(
                        fill.broker_order_id
                    )
                    order = next(
                        (o for o in (original, incoming) if o and o.client_order_id in clients),
                        None,
                    )
                    if order is None:
                        continue  # Inherited/foreign activity retains its separate owner path.
                    events.append(
                        BrokerEvent(
                            recorded_at_utc=self.now,
                            occurred_at=fill.occurred_at,
                            evidence_lane="BROKER_PAPER",
                            release_id=store.config.release_id,
                            account_id=self.account_id,
                            provider="alpaca",
                            endpoint=self.endpoint + "/v2/account/activities",
                            client_order_id=order.client_order_id,
                            event_id=fill.fill_id,
                            raw_digest=content_id(fill),
                            fill=fill,
                        )
                    )
        from quantbot.paper_core.crypto_fee_allocation import (
            acquire_order_fee_sources,
            retain_order_fee_allocations,
        )

        fee_sources = await acquire_order_fee_sources(self, store, until, rows=tuple(rows))
        document = {
            "account_id": self.account_id,
            "endpoint": self.endpoint,
            "after": after,
            "until": until,
            "rows": rows,
            "pending_owned_orders": tuple(pending_owned_orders),
            "cost_authority": "UNKNOWN_ALL_IN_COMPONENTS",
            "cash_authority": "NOT_CERTIFIED",
            "release_id": store.config.release_id,
            **({"bulk_order_reads": batch_reads} if batch_reads is not None else {}),
        }
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            repo = StorageRepository(session)
            costs = current_costs(session, cutoff=self.now)
            contract_conflicted = self._revise_conflicted_contract(
                store, session, document, progress, costs, events
            )
            document_id = content_id(document)
            put(session, "core_provider_acquisitions", document_id, document)
            self.pending_progress = (
                until.isoformat(),
                document_id,
                progress["watermark"] if progress else None,
                progress["document_id"] if progress else None,
            )
            for raw in rows:
                if raw.get("activity_type") == "FILL":
                    continue
                if raw.get("activity_type") in {"FEE", "CFEE"}:
                    from quantbot.paper_core.crypto_costs import (
                        parse_fee_activity,
                        retain_fee_activity,
                    )

                    fee = retain_fee_activity(
                        session,
                        parse_fee_activity(
                            raw,
                            symbol_aliases=self.adapter.crypto_symbol_aliases,
                            source_document_id=document_id,
                        ),
                        account_id=self.account_id,
                        received_at=self.now,
                    )
                    if fee.effective_at is not None and fee.effective_at > until:
                        raise BrokerProtocolError("FEE_ACTIVITY_EFFECTIVE_TIME_AFTER_WATERMARK")
                    if fee.activity_type == "CFEE":
                        quantity_event = fee.external_quantity_event(
                            account_id=self.account_id, baseline_at=commission.approved_at
                        )
                        if quantity_event is not None:
                            self.record_external(session, quantity_event)
                        # Date-only/unknown-symbol coin debits remain retained evidence,
                        # never a guessed lot fee or fabricated cash movement. Exact
                        # custody reconciliation still refuses an unexplained shortfall.
                        if fee.net_cash == 0:
                            continue
                if "net_amount" not in raw or not raw.get("id"):
                    raise BrokerProtocolError("NONTRADE_CASH_ACTIVITY_SCHEMA_UNVERIFIED")
                amount = Decimal(str(raw["net_amount"]))
                if not amount.is_finite():
                    raise BrokerProtocolError("NONTRADE_CASH_ACTIVITY_NONFINITE")
                # NonTradeActivity supplies cash independently of the account balance.
                creation = raw.get("created_at") or raw.get("date")
                if creation is None:
                    raise BrokerProtocolError("NONTRADE_CASH_CREATION_TIME_UNVERIFIED")
                occurred = datetime.fromisoformat(str(creation).replace("Z", "+00:00"))
                if occurred.tzinfo is None:
                    # A date-only activity cannot establish the intraday baseline boundary.
                    if raw["activity_type"] in {"FEE", "CFEE"}:
                        self.record_external(
                            session,
                            dict(
                                event_id=str(raw["id"]),
                                symbol="",
                                owner="ACCOUNT_NONTRADE",
                                quantity_delta="0",
                                net_cash=None,
                                source_document=document_id,
                                account_id=self.account_id,
                                occurred_at=None,
                                raw_creation=creation,
                                activity_type=str(raw["activity_type"]),
                                cash_authority_reason="NONTRADE_CASH_BASELINE_BOUNDARY_UNVERIFIED",
                            ),
                        )
                        continue
                    raise BrokerProtocolError("NONTRADE_CASH_BASELINE_BOUNDARY_UNVERIFIED")
                if occurred > commission.approved_at:
                    self.record_external(
                        session,
                        dict(
                            event_id=str(raw["id"]),
                            symbol="",
                            owner="ACCOUNT_NONTRADE",
                            quantity_delta="0",
                            net_cash=str(amount),
                            source_document=document_id,
                            account_id=self.account_id,
                            occurred_at=occurred.isoformat(),
                            activity_type=str(raw["activity_type"]),
                        ),
                    )
            retain_order_fee_allocations(
                store, session, fee_sources, recorded_at=self.now, observed_rows=tuple(rows)
            )
            for fill in fills:
                if fill.occurred_at <= commission.approved_at:
                    continue
                core_order = repo.get_broker_order(fill.broker_order_id)
                linked_core = (
                    core_order is not None
                    and session.execute(
                        select(table("core_intent_links").c.intent_id)
                        .join(
                            table("order_intents"),
                            table("order_intents").c.intent_id
                            == table("core_intent_links").c.intent_id,
                        )
                        .where(
                            table("order_intents").c.client_order_id == core_order.client_order_id
                        )
                    ).first()
                )
                if not linked_core:
                    # Only retained order/intent provenance identifies an external exit owner.
                    owner = dict(commission.legacy_owners).get(fill.symbol)
                    raw = next(r for r in rows if r.get("id") == fill.fill_id)
                    recorded_owner = (
                        None
                        if core_order is None
                        else session.execute(
                            select(table("order_intents").c.strategy_id).where(
                                table("order_intents").c.client_order_id
                                == core_order.client_order_id
                            )
                        ).scalar_one_or_none()
                    )
                    if owner and recorded_owner == owner:
                        external = dict(
                            event_id=fill.fill_id,
                            symbol=fill.symbol,
                            owner=owner,
                            quantity_delta=str(
                                fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
                            ),
                            net_cash=str(raw["net_amount"]) if "net_amount" in raw else None,
                            order_id=fill.broker_order_id,
                            owner_source="RETAINED_DURABLE_INTENT",
                            source_document=document_id,
                            account_id=self.account_id,
                            occurred_at=fill.occurred_at,
                        )
                        self.record_external(session, external)
                    else:
                        document["external_owner_blocker"] = (
                            "INHERITED_ORDER_OWNER_PROVENANCE_UNVERIFIED"
                        )
                    continue
                if (
                    self.config.cost_contract is not None
                    and fill.symbol not in self.adapter.crypto_symbol_aliases.values()
                ):
                    item = self.config.cost_contract.fee(
                        fill,
                        account_id=self.account_id,
                        at=self.now,
                        release_id=store.config.release_id,
                    )
                    if (
                        not contract_conflicted
                        and item is not None
                        and (fill.fill_id, "FEE") not in costs
                    ):
                        events.append(
                            BrokerEvent(
                                recorded_at_utc=self.now,
                                occurred_at=self.now,
                                evidence_lane="BROKER_PAPER",
                                release_id=store.config.release_id,
                                account_id=self.account_id,
                                provider="alpaca",
                                endpoint=self.endpoint + "/cost-contract",
                                client_order_id=core_order.client_order_id if core_order else "",
                                event_id=item.id,
                                raw_digest=content_id(item),
                                cost=item,
                            )
                        )
        return tuple(events)

    def _settled_order_event(
        self, store: CoreStore, session: Session, intent: OrderIntent
    ) -> BrokerEvent | None:
        """Reuse only terminal evidence already establishing an owned closed outcome."""
        from quantbot.execution.coordinator import _assert_ack_matches_intent
        from quantbot.paper_core.outcomes import (
            _source_id,
            quarantined_lots,
            settled_zero_entry,
            terminal_evidence,
        )

        repo = StorageRepository(session)
        if intent.strategy_id != store.config.owner or intent.state not in {
            IntentState.FILLED,
            IntentState.CANCELLED,
            IntentState.EXPIRED,
            IntentState.REJECTED,
        }:
            return None
        zero = settled_zero_entry(store, session, intent, cutoff=self.now)
        if zero is not None:
            return zero
        link = (
            session.execute(
                select(table("core_intent_links")).where(
                    table("core_intent_links").c.intent_id == intent.intent_id
                )
            )
            .mappings()
            .one()
        )
        encoded = session.execute(
            select(table("core_round_trips").c.payload).where(
                table("core_round_trips").c.id == link["lot_id"]
            )
        ).scalar_one_or_none()
        if encoded is None or link["lot_id"] in quarantined_lots(session):
            return None
        trip = RoundTrip.model_validate_json(encoded)
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
        if (
            order is None
            or trip.lot_id != link["lot_id"]
            or trip.decision_id != link["decision_id"]
            or trip.release_id != store.config.release_id
            or trip.evidence_lane != store.config.evidence_lane
            or trip.open_quantity != 0
            or not trip.entry_fill_ids
            or not trip.exit_fill_ids
            or trip.available_at is None
            or trip.available_at > self.now
            or order.broker_order_id not in trip.terminal_order_ids
        ):
            return None
        try:
            _assert_ack_matches_intent(intent, order)
        except BrokerProtocolError:
            return None
        fills = repo.list_fills(broker_order_id=order.broker_order_id)
        membership = trip.entry_fill_ids if link["role"] == "ENTRY" else trip.exit_fill_ids
        if not {f.fill_id for f in fills} <= set(membership):
            return None
        identity = terminal_evidence(session, order, cutoff=self.now)
        if identity is None or identity not in trip.source_refs:
            return None
        payload = session.execute(
            select(table("core_broker_event_sources").c.payload).where(
                table("core_broker_event_sources").c.id == identity
            )
        ).scalar_one()
        event = BrokerEvent.model_validate_json(payload)
        if (
            _source_id(event) != identity
            or event.account_id != store.config.account_id
            or event.evidence_lane != store.config.evidence_lane
            or event.release_id != store.config.release_id
            or event.provider != self.provider
            or event.endpoint != self.endpoint + "/v2/orders"
            or event.client_order_id != intent.client_order_id
            or event.order != order
            or event.correction_of is not None
            or event.raw_digest != content_id(order)
        ):
            return None
        return event

    @staticmethod
    def record_external(session: Session, external: dict[str, Any]) -> None:
        identity = content_id(
            {
                "account": external["account_id"],
                "event": external["event_id"],
                "owner": external["owner"],
            }
        )
        target = table("core_external_owner_events")
        previous = session.execute(
            select(target.c.payload).where(target.c.id == identity)
        ).scalar_one_or_none()
        if previous is not None:
            import json

            old = json.loads(previous)
            current = json.loads(canonical_result_json(external))
            old.pop("source_document", None)
            current.pop("source_document", None)
            if old != current:
                raise StateConflictError("EXTERNAL_OWNER_ACTIVITY_CONFLICT")
            return
        put(session, "core_external_owner_events", identity, external)

    def _revise_conflicted_contract(
        self,
        store: CoreStore,
        session: Session,
        document: dict[str, Any],
        progress: Any,
        costs: dict[tuple[str, str], CostItem],
        events: list[BrokerEvent],
    ) -> bool:
        """A fee contradicts its exact contract, irrespective of the FILL overlap."""
        contract = self.config.cost_contract
        if contract is None:
            return False
        cursor = table("core_source_cursors")
        from quantbot.paper_core.operational_costs import cost_contract_conflict_source

        conflict_key = cost_contract_conflict_source(self.account_id, contract.id)
        latch = (
            session.execute(select(cursor).where(cursor.c.source_id == conflict_key))
            .mappings()
            .one_or_none()
        )
        from quantbot.paper_core.crypto_costs import nonzero_fee_activity

        observed_conflict = any(nonzero_fee_activity(r) for r in document["rows"])
        if not observed_conflict and latch is None:
            return False
        previous_doc = (
            {}
            if progress is None
            else json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == progress["document_id"]
                    )
                ).scalar_one()
            )
        )
        first_doc = (
            None
            if latch is None
            else json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == latch["event_id"]
                    )
                ).scalar_one()
            )
        )
        first_seen = self.now if latch is None else datetime.fromisoformat(latch["available_at"])
        high = (
            session.execute(select(func.max(table("core_cost_items").c.id))).scalar_one() or ""
            if first_doc is None
            else first_doc["fee_scan_high"]
        )
        after = (
            previous_doc.get("fee_scan_after", "")
            if previous_doc.get("cost_conflict_contract_id") == contract.id
            else ""
        )
        original_costs = (
            select(table("core_cost_items"))
            .where(
                table("core_cost_items").c.id <= high,
                func.json_extract(table("core_cost_items").c.payload, "$.source")
                == "CONTRACTUAL_ZERO",
                func.json_extract(table("core_cost_items").c.payload, "$.document_id")
                == contract.id,
                func.json_extract(table("core_cost_items").c.payload, "$.component") == "FEE",
            )
            .order_by(table("core_cost_items").c.id)
        )
        batch = (
            session.execute(
                original_costs.where(table("core_cost_items").c.id > after).limit(
                    self.cost_revision_batch_size
                )
            )
            .mappings()
            .all()
        )
        scan_after = str(batch[-1]["id"]) if batch else after
        complete = scan_after >= high or not batch
        repo = StorageRepository(session)
        outcome_after = (
            previous_doc.get("fee_outcome_scan_after", "")
            if previous_doc.get("cost_conflict_contract_id") == contract.id
            else ""
        )
        outcome_complete = False
        if complete:
            checked = (
                session.execute(
                    original_costs.where(table("core_cost_items").c.id > outcome_after).limit(
                        self.cost_revision_batch_size
                    )
                )
                .mappings()
                .all()
            )
            waiting = False
            for row in checked:
                old = CostItem.model_validate_json(row["payload"])
                current = costs.get((old.fill_id, "FEE"))
                fill = repo.get_fill(old.fill_id)
                order = None if fill is None else repo.get_broker_order(fill.broker_order_id)
                owned_lot = (
                    None
                    if order is None
                    else session.execute(
                        select(table("core_intent_links").c.lot_id)
                        .join(
                            table("order_intents"),
                            table("order_intents").c.intent_id
                            == table("core_intent_links").c.intent_id,
                        )
                        .where(table("order_intents").c.client_order_id == order.client_order_id)
                    ).scalar_one_or_none()
                )
                applicable = (
                    fill is not None
                    and (
                        fill.symbol in self.adapter.crypto_symbol_aliases.values()
                        or contract.fee(
                            fill,
                            account_id=self.account_id,
                            at=first_seen,
                            release_id=store.config.release_id,
                        )
                        is not None
                    )
                    and owned_lot is not None
                )
                if applicable:
                    payload = session.execute(
                        select(table("core_outcome_revisions").c.payload)
                        .join(
                            table("core_outcome_index"),
                            table("core_outcome_index").c.outcome_id
                            == table("core_outcome_revisions").c.id,
                        )
                        .where(table("core_outcome_index").c.lot_id == owned_lot)
                    ).scalar_one_or_none()
                    outcome = None if payload is None else json.loads(payload)
                    waiting = (
                        current is None
                        or current.id == old.id
                        or (
                            outcome is not None
                            and (
                                current.id not in outcome["cost_item_ids"]
                                or (
                                    current.source == "UNKNOWN"
                                    and (
                                        outcome["status"] != "COSTS_PENDING"
                                        or outcome["actual_fees"] is not None
                                        or outcome["net_pnl"] is not None
                                    )
                                )
                            )
                        )
                    )
                    if waiting:
                        break
                outcome_after = str(row["id"])
            outcome_complete = not waiting and (outcome_after >= high or not checked)
        document.update(
            cost_conflict_contract_id=contract.id,
            fee_scan_high=high,
            fee_scan_after=scan_after,
            fee_scan_complete=complete,
            fee_outcome_scan_after=outcome_after,
            fee_outcome_scan_complete=outcome_complete,
        )
        document_id = content_id(document)
        put(session, "core_provider_acquisitions", document_id, document)
        if latch is None:
            session.execute(
                cursor.insert().values(
                    source_id=conflict_key,
                    available_at=first_seen.isoformat(),
                    event_id=document_id,
                )
            )
        first_source = document_id if latch is None else str(latch["event_id"])
        pending = pending_cost_revision_source(self.account_id)
        if (
            batch
            and session.execute(
                select(cursor.c.source_id).where(cursor.c.source_id == pending)
            ).first()
            is None
        ):
            session.execute(
                cursor.insert().values(
                    source_id=pending, available_at=first_seen.isoformat(), event_id=first_source
                )
            )
        for row in batch:
            previous = CostItem.model_validate_json(row["payload"])
            current = costs.get((previous.fill_id, "FEE"))
            if (
                previous.id != row["id"]
                or previous.component != "FEE"
                or previous.source != "CONTRACTUAL_ZERO"
                or previous.document_id != contract.id
                or current is None
                or current.id != previous.id
            ):
                continue
            fill = repo.get_fill(previous.fill_id)
            order = None if fill is None else repo.get_broker_order(fill.broker_order_id)
            if (
                fill is None
                or order is None
                or (
                    fill.symbol not in self.adapter.crypto_symbol_aliases.values()
                    and contract.fee(
                        fill,
                        account_id=self.account_id,
                        at=first_seen,
                        release_id=store.config.release_id,
                    )
                    is None
                )
            ):
                continue
            owned = session.execute(
                select(table("core_intent_links").c.intent_id)
                .join(
                    table("order_intents"),
                    table("order_intents").c.intent_id == table("core_intent_links").c.intent_id,
                )
                .where(table("order_intents").c.client_order_id == order.client_order_id)
            ).first()
            if owned is None:
                continue
            unknown = CostItem(
                recorded_at_utc=first_seen,
                evidence_lane="BROKER_PAPER",
                release_id=store.config.release_id,
                fill_id=fill.fill_id,
                source="UNKNOWN",
                effective_start=previous.effective_start,
                effective_end=previous.effective_end,
                revision=previous.revision + 1,
                supersedes=previous.id,
                source_refs=(first_source,),
            )
            events.append(
                BrokerEvent(
                    recorded_at_utc=first_seen,
                    occurred_at=first_seen,
                    evidence_lane="BROKER_PAPER",
                    release_id=store.config.release_id,
                    account_id=self.account_id,
                    provider="alpaca",
                    endpoint=self.endpoint + "/conflicting-fee",
                    client_order_id=order.client_order_id,
                    event_id=unknown.id,
                    raw_digest=content_id(unknown),
                    cost=unknown,
                )
            )
        return True

    def accept_activity_cursor(self, store: CoreStore) -> None:
        if self.pending_progress is None:
            return
        watermark, document_id, previous, previous_document = self.pending_progress
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            progress = table("core_provider_progress")
            document = json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == document_id
                    )
                ).scalar_one()
            )
            actual = session.execute(
                select(progress.c.watermark, progress.c.document_id).where(
                    progress.c.account_id == self.account_id
                )
            ).one_or_none()
            if (None if actual is None else tuple(actual)) == (
                None if previous is None else (previous, previous_document)
            ):
                values = dict(watermark=watermark, document_id=document_id)
                if actual is None:
                    session.execute(progress.insert().values(account_id=self.account_id, **values))
                else:
                    session.execute(
                        update(progress)
                        .where(progress.c.account_id == self.account_id)
                        .values(**values)
                    )
                if document.get("fee_scan_complete") and document.get("fee_outcome_scan_complete"):
                    session.execute(
                        table("core_source_cursors")
                        .delete()
                        .where(
                            table("core_source_cursors").c.source_id
                            == pending_cost_revision_source(self.account_id)
                        )
                    )
        self.pending_progress = None
