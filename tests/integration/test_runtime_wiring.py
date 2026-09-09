"""End-to-end proof that the composition root produces a runnable cycle.

Every collaborator except the network is real: real storage, real reconciliation, real
strategy, real risk sizing, real execution coordinator. Only the two HTTP transports are
replaced by deterministic fakes serving Alpaca-shaped payloads.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from quantbot.brokers import (
    AlpacaPaperBrokerAdapter,
    BrokerTransportResponse,
    HttpxBrokerTransport,
)
from quantbot.config import Settings
from quantbot.execution import ExecutionCoordinator, StartupRecovery
from quantbot.market_data import (
    AdjustmentMode,
    AlpacaMarketCalendar,
    AlpacaMarketDataClient,
    MarketDataFeed,
    TransportResponse,
)
from quantbot.operations import (
    AdaptiveMomentumRunner,
    CachedSessionCalendar,
    KillSwitchController,
    LedgerSyncingRecovery,
    MarketDataSettings,
    MarketDataSync,
    OperationalMetrics,
    ReadinessEvidence,
    RunOnceCycle,
)
from quantbot.operations.runners import StrategyDataError
from quantbot.risk import PortfolioVetoPolicy
from quantbot.runtime import (
    RuntimeConfigurationError,
    build_runtime,
    load_or_create_identity,
)
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, load_strategy_config
from tests.support import measured

ACCOUNT_ID = "paper-account-1"
UNIVERSE = ("SPY", "QQQ", "TLT", "GLD")
# Enough to fill the 120-session correlation window (#58). Raised from 90 rather than shrinking
# the window: 0.90 was measured over 120 sessions, and tuning the rule so a fixture passes would
# be choosing the threshold to fit the test rather than the data.
SESSION_COUNT = 150
NOW = datetime(2026, 8, 14, 21, 5, tzinfo=UTC)


def _config_payload() -> dict[str, Any]:
    """A structurally identical but fast-warming variant of the shipped V1 config."""
    return {
        "strategy_name": "adaptive-momentum",
        "version": "1.0.0",
        "calendar": "XNYS",
        "universe": list(UNIVERSE),
        "benchmark_symbol": "SPY",
        "regime_symbol": "SPY",
        "roster_size": 2,
        "momentum_long": 20,
        "momentum_skip": 2,
        "trend_period": 15,
        "entry_period": 10,
        "exit_period": 5,
        "atr_period": 5,
        "initial_stop_atr": 2,
        "trailing_stop_atr": 3,
        "rotation_frequency": "monthly",
        "positive_momentum_required": True,
        "market_regime_blocks_entries_only": True,
        "risk_per_trade_bps": 50,
        "max_open_risk_bps": 500,
        "max_position_value_bps": 1000,
        "max_gross_exposure_bps": 10000,
        "max_positions": 20,
        "drawdown_thresholds_bps": [500, 1000, 1500, 2000],
        "drawdown_multipliers_bps": [10000, 7500, 5000, 0, 0],
        "slippage_bps": 5,
        "commission_per_order": 0,
        "idle_cash_rate_bps": 0,
        "allow_fractional_shares": False,
        "allow_pyramiding": False,
    }


def _write_config(tmp_path: Path) -> Path:
    target = tmp_path / "strategy-test.yaml"
    target.write_text(yaml.safe_dump(_config_payload()), encoding="utf-8")
    return target


def _session_dates(count: int, last: date) -> list[date]:
    """Contiguous weekday sessions ending on ``last``."""
    dates: list[date] = []
    cursor = last
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(dates)


def _future_sessions(count: int, first_after: date) -> list[date]:
    dates: list[date] = []
    cursor = first_after + timedelta(days=1)
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)
    return dates


SESSION_DATES = _session_dates(SESSION_COUNT, NOW.date())
# The calendar must reach into the month after the acting session so the monthly roster
# can resolve its expiry, which is why the runtime looks 60 days ahead by default.
CALENDAR_DATES = SESSION_DATES + _future_sessions(40, NOW.date())


def _calendar_payload() -> list[dict[str, str]]:
    return [
        {"date": value.isoformat(), "open": "09:30", "close": "16:00"} for value in CALENDAR_DATES
    ]


def _price(symbol: str, index: int) -> Decimal:
    """Deterministic, strictly rising series so momentum and breakouts are well defined."""
    base = {
        "SPY": Decimal("400"),
        "QQQ": Decimal("300"),
        "TLT": Decimal("90"),
        "GLD": Decimal("180"),
    }
    slope = {"SPY": Decimal("1"), "QQQ": Decimal("2"), "TLT": Decimal("0.1"), "GLD": Decimal("0.5")}
    return base[symbol] + slope[symbol] * Decimal(index)


def _bars_payload(symbols: tuple[str, ...]) -> dict[str, Any]:
    bars: dict[str, list[dict[str, Any]]] = {}
    for symbol in symbols:
        series: list[dict[str, Any]] = []
        for index, session_date in enumerate(SESSION_DATES):
            close = _price(symbol, index)
            series.append(
                {
                    "t": datetime.combine(session_date, time(4, 0), tzinfo=UTC).isoformat(),
                    "o": str(close - Decimal("0.5")),
                    "h": str(close + Decimal("1")),
                    "l": str(close - Decimal("1")),
                    "c": str(close),
                    "v": "1000000",
                }
            )
        bars[symbol] = series
    return {"bars": bars, "next_page_token": None}


def _account_payload(equity: str = "100000") -> dict[str, Any]:
    return {
        "id": ACCOUNT_ID,
        "account_number": "PA123",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": equity,
        "buying_power": equity,
        "equity": equity,
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
    }


class FakeMarketDataTransport:
    """Serve calendar and bar payloads without touching the network."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> TransportResponse:
        self.requests.append(url)
        if "calendar" in url:
            payload: Any = _calendar_payload()
        else:
            requested = tuple(params["symbols"].split(","))
            payload = _bars_payload(requested)
        return TransportResponse(
            status_code=200,
            headers={},
            content=json.dumps(payload).encode("utf-8"),
        )


class FakeBrokerTransport:
    """Minimal Alpaca paper trading API with an in-memory order book."""

    def __init__(self, open_orders: list[dict[str, Any]] | None = None) -> None:
        self.submitted: list[Mapping[str, object]] = []
        self.open_orders: list[dict[str, Any]] = open_orders if open_orders is not None else []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: Decimal,
    ) -> BrokerTransportResponse:
        payload: Any
        if url.endswith("/v2/orders:by_client_order_id"):
            requested = params["client_order_id"]
            payload = next(
                (order for order in self.open_orders if order["client_order_id"] == requested),
                None,
            )
            if payload is None:
                return BrokerTransportResponse(status_code=404, headers={}, content=b"{}")
        elif url.endswith("/v2/account"):
            payload = _account_payload()
        elif url.endswith("/v2/positions"):
            payload = []
        elif url.endswith("/v2/account/activities"):
            payload = []
        elif url.endswith("/v2/clock"):
            payload = {
                "timestamp": NOW.isoformat(),
                "is_open": False,
                "next_open": (NOW + timedelta(hours=12)).isoformat(),
                "next_close": (NOW + timedelta(hours=19)).isoformat(),
            }
        elif url.endswith("/v2/orders") and method == "POST":
            assert json_body is not None
            self.submitted.append(json_body)
            payload = {
                "id": f"broker-{len(self.open_orders) + 1}",
                "client_order_id": json_body["client_order_id"],
                "symbol": json_body["symbol"],
                "side": json_body["side"],
                "type": json_body["type"],
                "time_in_force": json_body["time_in_force"],
                "qty": json_body["qty"],
                "filled_qty": "0",
                "status": "accepted",
                "submitted_at": NOW.isoformat(),
                "filled_avg_price": None,
                "limit_price": None,
                "stop_price": None,
                "extended_hours": False,
            }
            self.open_orders.append(payload)
        elif url.endswith("/v2/orders"):
            payload = list(self.open_orders)
        else:
            raise AssertionError(f"unexpected broker request: {method} {url}")
        return BrokerTransportResponse(
            status_code=200,
            headers={},
            content=json.dumps(payload).encode("utf-8"),
        )


def _ready_settings() -> Settings:
    return Settings(
        EXPECTED_ACCOUNT_ID=ACCOUNT_ID,
        ALPACA_PAPER_API_KEY="paper-key",
        ALPACA_PAPER_API_SECRET="paper-secret",
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=True,
    )


def _ready_evidence() -> ReadinessEvidence:
    return ReadinessEvidence(
        paper_mode=True,
        account_verified=True,
        broker_healthy=True,
        data_healthy=True,
        risk_healthy=True,
        reconciliation_successful=True,
    )


def _build_cycle(
    tmp_path: Path,
    config: StrategyConfig,
    database: Database,
    broker_transport: FakeBrokerTransport | None = None,
    entry_buying_power_reserve_bps: int = 0,
) -> tuple[RunOnceCycle, AdaptiveMomentumRunner, FakeBrokerTransport]:
    settings = _ready_settings()
    identity = load_or_create_identity(database, config, now=NOW)
    broker_transport = broker_transport or FakeBrokerTransport()
    data_transport = FakeMarketDataTransport()
    broker = AlpacaPaperBrokerAdapter(
        api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
        api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
        transport=broker_transport,
    )
    market_data = MarketDataSettings(feed=MarketDataFeed.IEX, adjustment=AdjustmentMode.ALL)
    sessions = CachedSessionCalendar(
        AlpacaMarketCalendar(
            api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
            api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
            transport=data_transport,
        ),
        clock=lambda: NOW,
    )
    strategy = AdaptiveMomentumRunner(
        database=database,
        broker=broker,
        config=config,
        identity=identity,
        sessions=sessions,
        market_data=market_data,
        clock=lambda: NOW,
        entry_buying_power_reserve_bps=entry_buying_power_reserve_bps,
    )
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "quantbot.lock",
        strategy_identity=identity,
        recovery=LedgerSyncingRecovery(
            database,
            broker,
            StartupRecovery(database, broker, settings),
        ),
        data_sync=MarketDataSync(
            database=database,
            client=AlpacaMarketDataClient(
                api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
                api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
                transport=data_transport,
            ),
            sessions=sessions,
            config=config,
            market_data=market_data,
            clock=lambda: NOW,
        ),
        strategy=strategy,
        submitter=ExecutionCoordinator(database, broker, settings),
        metrics=OperationalMetrics(),
        max_data_age_seconds=86_400,
    )
    return cycle, strategy, broker_transport


async def test_the_fetched_window_and_the_measured_window_cannot_drift_apart(
    tmp_path: Path,
) -> None:
    """#84: one policy field feeds both consumers, so neither can be changed alone.

    They were separate constructor arguments held in step by a comment. Fetching fewer sessions
    than the veto measures over leaves that veto permanently unmeasurable -- refusing every
    order, correctly and uselessly -- and measuring over fewer than the threshold was calibrated
    against silently changes the rule.

    Raising the policy past what the calendar can supply must therefore be visible on *both*
    sides from the single change: the sync refuses to run, and the snapshot refuses to build.
    """
    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness", measured=measured(), updated_at=NOW
    )
    beyond = PortfolioVetoPolicy(correlation_lookback_sessions=SESSION_COUNT + 10)

    settings = _ready_settings()
    data_transport = FakeMarketDataTransport()
    sessions = CachedSessionCalendar(
        AlpacaMarketCalendar(
            api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
            api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
            transport=data_transport,
        ),
        clock=lambda: NOW,
    )
    sync = MarketDataSync(
        database=database,
        client=AlpacaMarketDataClient(
            api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
            api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
            transport=data_transport,
        ),
        sessions=sessions,
        config=config,
        veto_policy=beyond,
        clock=lambda: NOW,
    )

    with pytest.raises(StrategyDataError, match=str(SESSION_COUNT + 10)):
        await sync.sync()

    # The same single field, read by the other consumer. A window the calendar cannot fill is
    # no snapshot, and `CORRELATION_CONCENTRATION` then refuses rather than measuring short.
    cycle, strategy, _ = _build_cycle(tmp_path, config, database)
    strategy._veto_policy = beyond  # noqa: SLF001 - the point is that one field moves both
    calendar = await sessions.sessions()
    assert (
        strategy._dependence_snapshot(  # noqa: SLF001
            {}, [session.session_date for session in calendar]
        )
        is None
    )
    database.close()


async def test_wired_cycle_reconciles_syncs_data_and_submits_a_risk_sized_order(
    tmp_path: Path,
) -> None:
    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness",
        measured=measured(),
        updated_at=NOW,
    )
    cycle, strategy, broker_transport = _build_cycle(tmp_path, config, database)
    strategy.bind_run("run-1")

    result = await cycle.run("run-1", started_at=NOW, trading_date=NOW.date())

    assert result.reasons == ()
    assert result.status == "SUCCEEDED"
    assert result.signals_evaluated == len(UNIVERSE)
    assert result.orders_submitted == len(broker_transport.submitted)
    assert broker_transport.submitted, "a rising universe must produce at least one entry"

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.list_bars(symbol="SPY")
        signals = repository.list_signals(run_id="run-1")
        reconciliation = repository.get_latest_reconciliation()
        snapshot = repository.get_latest_account_snapshot(ACCOUNT_ID)
    assert {record.symbol for record in signals} == set(UNIVERSE)
    assert reconciliation is not None and reconciliation.diffs == ()
    assert snapshot is not None and snapshot.account.equity == Decimal("100000")
    database.close()


async def test_entry_cash_reserve_limits_the_real_cycle_submission_batch(tmp_path: Path) -> None:
    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness", measured=measured(), updated_at=NOW,
    )
    cycle, strategy, broker = _build_cycle(
        tmp_path, config, database, entry_buying_power_reserve_bps=9000,
    )
    strategy.bind_run("budgeted-run")
    try:
        result = await cycle.run("budgeted-run", started_at=NOW, trading_date=NOW.date())
        notional = sum(
            Decimal(str(order["qty"])) * _price(str(order["symbol"]), SESSION_COUNT - 1)
            for order in broker.submitted
        )
        assert result.status == "SUCCEEDED"
        assert Decimal("9000") < notional <= Decimal("10000")
        # Accepted orders already bind the next account snapshot. The runner must mark them
        # as broker-reserved, while continuing to reserve their exposure and position slots.
        pending = strategy._pending_reservations(  # noqa: SLF001
            await strategy._broker.get_open_orders(),  # noqa: SLF001
            strategy._load_histories(), Decimal("100000"),  # noqa: SLF001
        )
        assert pending and all(item.buying_power_already_reserved for item in pending)
    finally:
        database.close()


async def test_rerunning_the_same_trading_day_never_submits_a_duplicate_order(
    tmp_path: Path,
) -> None:
    """Duplicate-order protection: a re-run of the same signal day submits nothing new."""
    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness",
        measured=measured(),
        updated_at=NOW,
    )
    cycle, strategy, broker = _build_cycle(tmp_path, config, database)
    strategy.bind_run("run-1")
    first = await cycle.run("run-1", started_at=NOW, trading_date=NOW.date())
    submitted_after_first = len(broker.submitted)

    cycle_two, strategy_two, _ = _build_cycle(tmp_path, config, database, broker)
    strategy_two.bind_run("run-2")
    second = await cycle_two.run("run-2", started_at=NOW, trading_date=NOW.date())

    assert first.orders_submitted == submitted_after_first > 0
    assert second.status == "SUCCEEDED"
    assert second.orders_submitted == 0
    assert len(broker.submitted) == submitted_after_first
    with database.transaction() as session:
        signals = StorageRepository(session).list_signals(run_id="run-2")
    assert any(
        "DUPLICATE_INTENT_ALREADY_DURABLE" in record.payload["risk_rejection"]  # type: ignore[operator]
        for record in signals
    )
    database.close()


def test_build_runtime_fails_closed_without_paper_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUANTBOT_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))
    # `build_runtime` opens the configured store and no longer creates it (#86).
    Database.create(tmp_path / "quantbot.db").close()
    # Keep the test hermetic: a developer machine may have real credentials exported.
    monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_API_SECRET", raising=False)

    with pytest.raises(RuntimeConfigurationError, match="PAPER_CREDENTIALS_NOT_CONFIGURED"):
        build_runtime(Settings())


def test_build_runtime_assembles_every_operational_collaborator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QUANTBOT_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))
    # `build_runtime` opens the configured store and no longer creates it (#86).
    Database.create(tmp_path / "quantbot.db").close()
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(tmp_path / "quantbot.lock"))
    monkeypatch.setenv("QUANTBOT_GIT_COMMIT", "0123456789abcdef")

    runtime = build_runtime(_ready_settings())

    assert isinstance(runtime.broker_transport, HttpxBrokerTransport)
    assert runtime.identity.git_commit == "0123456789abcdef"
    assert runtime.identity.configuration_hash
    assert runtime.cycle() is not None
    assert runtime.strategy._entry_buying_power_reserve_bps == 500  # noqa: SLF001
    runtime.database.close()


async def test_a_cycle_records_anomalies_without_letting_them_touch_the_decision(
    tmp_path: Path,
) -> None:
    """#56 reaching production, and staying out of the way once it gets there.

    The detector was correct and unreachable: nothing outside its own package called `detect`.
    That is the failure this branch has met repeatedly -- a control that is right in isolation
    and never wired -- so the assertion is that a real cycle *writes events*, not that the code
    exists.

    The second half matters as much. An anomaly is a routing signal for research and holds no
    broker authority, so the cycle must still submit the order it would have submitted anyway.
    An observability feature that could change what gets traded is not observability.
    """
    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness", measured=measured(), updated_at=NOW
    )
    cycle, strategy, broker_transport = _build_cycle(tmp_path, config, database)
    strategy.bind_run("run-1")

    result = await cycle.run("run-1", started_at=NOW, trading_date=NOW.date())

    with database.transaction() as session:
        repository = StorageRepository(session)
        events = repository.list_anomaly_events()
        contexts = [
            context
            for event in events
            for context in repository.list_anomaly_dependence_contexts(
                anomaly_event_id=str(event["event_id"])
            )
        ]
    database.close()

    assert events, "a cycle over this fixture must record anomalies; the detector is unwired"
    assert result.status == "SUCCEEDED"
    assert broker_transport.submitted, "detection must not suppress the order the cycle would place"

    # The fixture reads IEX, so the per-family capability gate must be visible in what was
    # stored: price fires, volume is refused, and the event is still valid.
    for event in events:
        assert event["feed"] == "iex"
        assert event["volume_coverage"] == "SINGLE_VENUE"
        assert "PRICE_VELOCITY" in str(event["triggers"])
        assert "VOLUME_SHOCK" not in str(event["triggers"])
        assert "VOLUME_SHOCK=FEED_UNFIT" in str(event["family_quality"])

    # Each event carries the matrix that grouped it, as a relation rather than a field (#94).
    assert len(contexts) == len(events)
    assert all(row["dependence_snapshot_identity"] for row in contexts)


async def test_replaying_a_cycle_does_not_record_the_same_anomaly_twice(
    tmp_path: Path,
) -> None:
    """Replay safety where it actually matters: the production path, not a unit fixture.

    `event_id` excludes the wall clock precisely so a re-run is idempotent. If that only held in
    isolation, a restarted daemon would double every event and inflate the rate the routing
    experiment reports.
    """
    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness", measured=measured(), updated_at=NOW
    )
    cycle, strategy, _ = _build_cycle(tmp_path, config, database)

    strategy.bind_run("run-1")
    await cycle.run("run-1", started_at=NOW, trading_date=NOW.date())
    with database.transaction() as session:
        first = len(StorageRepository(session).list_anomaly_events())

    strategy.bind_run("run-2")
    await cycle.run("run-2", started_at=NOW, trading_date=NOW.date())
    with database.transaction() as session:
        second = len(StorageRepository(session).list_anomaly_events())
    database.close()

    assert first > 0
    assert second == first, "re-running the same session must not mint a second set of events"


async def test_the_live_collector_records_where_it_got_to_and_when_it_last_worked(
    tmp_path: Path,
) -> None:
    """#198 wired to its first real caller, on the path that actually collects.

    Infrastructure nothing calls is the failure mode this repository has written down twice:
    a control plane that only ever ran in its own unit tests would look finished and answer no
    operational question. So the assertions here are against `MarketDataSync` itself, and they
    are the three facts an operator could not previously get: which run happened, how much of
    what it saw was stored, and when live collection last succeeded.
    """
    from quantbot.ingestion import (
        IngestionControlPlane,
        RunMode,
        RunStatus,
        SourceHealth,
        ingestion_status,
    )
    from quantbot.operations.runners import UNIVERSE_STREAM

    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    settings = _ready_settings()
    data_transport = FakeMarketDataTransport()
    sync = MarketDataSync(
        database=database,
        client=AlpacaMarketDataClient(
            api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
            api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
            transport=data_transport,
        ),
        sessions=CachedSessionCalendar(
            AlpacaMarketCalendar(
                api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
                api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
                transport=data_transport,
            ),
            clock=lambda: NOW,
        ),
        config=config,
        clock=lambda: NOW,
    )

    result = await sync.sync()
    assert result.healthy

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        run = plane.latest_run(sync.source_id)
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        assert run.mode is RunMode.LIVE
        assert run.counts.seen > 0
        assert run.counts.inserted == result.bars_stored
        assert run.counts.unaccounted == 0, (
            "every bar the fetch saw is accounted for as inserted, duplicate or rejected"
        )

        cursor = plane.cursor(sync.source_id, UNIVERSE_STREAM)
        assert cursor is not None
        assert cursor.last_success_at == NOW
        assert cursor.version == 1

        status = plane.status(
            sync.source_id, stream_key=UNIVERSE_STREAM, expected_success_after=NOW
        )
        assert status.health is SourceHealth.HEALTHY

    # A second sync at the same frozen instant is a second attempt, not a duplicate id, and it
    # stores nothing new. That collision is what the first caller found.
    await sync.sync()
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        runs = plane.runs_for(sync.source_id)
        assert len(runs) == 2
        assert len({run.ingestion_run_id for run in runs}) == 2
        assert runs[0].counts.inserted == 0
        assert runs[0].counts.duplicate == runs[0].counts.seen

        cursor = plane.cursor(sync.source_id, UNIVERSE_STREAM)
        assert cursor is not None
        assert cursor.version == 2, "the checkpoint moved once per attempt, without conflict"

        report = ingestion_status(plane, now=NOW)
        assert report["unhealthy_streams"] == 0
        assert report["sources"][0]["streams"][0]["last_success_at"] == NOW.isoformat()  # type: ignore[index]

    database.close()


async def test_a_collection_that_fails_leaves_a_durable_failure_rather_than_silence(
    tmp_path: Path,
) -> None:
    """The gap between "collection never ran" and "collection ran and broke" (#198).

    Before this, a failing sync raised into the cycle and left nothing behind: the next person
    to ask why data stopped had a stack trace in a log at best. The exception still propagates
    -- the cycle fails exactly as it did -- and the attempt now survives the process.
    """
    from quantbot.ingestion import IngestionControlPlane, RunStatus, SourceHealth

    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    settings = _ready_settings()
    data_transport = FakeMarketDataTransport()
    sessions = CachedSessionCalendar(
        AlpacaMarketCalendar(
            api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
            api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
            transport=data_transport,
        ),
        clock=lambda: NOW,
    )
    sync = MarketDataSync(
        database=database,
        client=AlpacaMarketDataClient(
            api_key=settings.ALPACA_PAPER_API_KEY,  # type: ignore[arg-type]
            api_secret=settings.ALPACA_PAPER_API_SECRET,  # type: ignore[arg-type]
            transport=data_transport,
        ),
        sessions=sessions,
        config=config,
        # More history demanded than the calendar can supply, so the fetch never happens.
        veto_policy=PortfolioVetoPolicy(correlation_lookback_sessions=SESSION_COUNT + 10),
        clock=lambda: NOW,
    )

    with pytest.raises(StrategyDataError):
        await sync.sync()

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        run = plane.latest_run(sync.source_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.error_code == "StrategyDataError"

        assert plane.cursor(sync.source_id, "universe") is None, (
            "a failure must not check point past data it never fetched"
        )
        status = plane.status(
            sync.source_id, stream_key="universe", expected_success_after=None
        )
        assert status.health is SourceHealth.FAILED
        assert [event.event_kind.value for event in plane.open_incidents(sync.source_id)] == [
            "PROVIDER_FAILURE"
        ]

    database.close()


async def test_the_wired_cycle_records_what_it_is_running(tmp_path: Path) -> None:
    """#190's first caller: the coordinator states its own identity while it holds the lock.

    A record only its own unit tests wrote would answer no operational question, so this
    asserts against the wired path -- and specifically that the observation lands *before*
    anything can halt, because "what is running" and "what succeeded" are different questions
    and the first one matters most when the second one is failing.
    """
    from quantbot.operations.deployment import (
        DeploymentRegistry,
        DeploymentWarning,
        RuntimeContext,
        RuntimeRole,
    )

    config = load_strategy_config(_write_config(tmp_path))
    database = Database.create(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness",
        measured=measured(),
        updated_at=NOW,
    )
    cycle, strategy, broker = _build_cycle(tmp_path, config, database)
    cycle._runtime = RuntimeContext(  # noqa: SLF001 - the wired runtime supplies this
        role=RuntimeRole.COORDINATOR,
        store_identity=database.store_identity,
        trading_mode="PAPER",
        process_started_at=NOW,
        host_id="omen",
        process_id="pid-1",
        git_commit="c0ffee1",
    )
    strategy.bind_run("run-1")

    result = await cycle.run("run-1", started_at=NOW, trading_date=NOW.date())
    assert result.status == "SUCCEEDED"

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW)

    assert status.ok is True, status.reasons
    coordinator = status.latest_coordinator
    assert coordinator is not None
    assert coordinator.host_id == "omen"
    assert coordinator.git_commit == "c0ffee1"
    assert coordinator.strategy_id == strategy._identity.strategy_id  # noqa: SLF001
    assert coordinator.store_identity == database.store_identity
    assert status.last_successful_cycle_at is not None

    # A second cycle from the same process is the same coordinator, not a second one.
    cycle_two, strategy_two, _ = _build_cycle(tmp_path, config, database, broker)
    cycle_two._runtime = cycle._runtime  # noqa: SLF001
    strategy_two.bind_run("run-2")
    await cycle_two.run("run-2", started_at=NOW + timedelta(minutes=1), trading_date=NOW.date())

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW + timedelta(minutes=1))
    assert len(status.active_coordinators) == 1
    assert DeploymentWarning.MULTIPLE_ACTIVE_COORDINATORS not in status.warnings

    database.close()


def test_a_wired_runtime_describes_the_database_it_actually_writes_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The context is built where the settings and the path already live, for this reason.

    A cycle that worked out its own database path could describe a different store from the one
    it writes to, which is exactly the ambiguity the record exists to remove.
    """
    from quantbot.operations.checkout import running_checkout_commit
    from quantbot.operations.deployment import RuntimeRole

    monkeypatch.setenv("QUANTBOT_CONFIG", str(_write_config(tmp_path)))
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))
    Database.create(tmp_path / "quantbot.db").close()
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(tmp_path / "quantbot.lock"))
    monkeypatch.setenv("QUANTBOT_GIT_COMMIT", "0123456789abcdef")

    expected_commit = running_checkout_commit()
    runtime = build_runtime(_ready_settings())
    try:
        context = runtime.runtime_context()
        assert context.role is RuntimeRole.COORDINATOR
        assert context.trading_mode == "PAPER"

        # Observed on the real clock, not the fixture's frozen past one: the process really did
        # start just now, and using NOW would exercise the backwards-clock clamp instead of the
        # thing under test.
        observation = context.observe(now=datetime.now(UTC), identity=runtime.identity)
        assert observation.process_started_at <= observation.observed_at
        assert observation.git_commit == expected_commit
        assert len(observation.git_commit) == 40
        monkeypatch.setenv("QUANTBOT_GIT_COMMIT", "f" * 40)
        monkeypatch.setattr("quantbot.operations.checkout.git_read", lambda *args: "e" * 40)
        later = runtime.runtime_context().observe(now=datetime.now(UTC))
        assert later.git_commit == expected_commit
        assert observation.store_identity == runtime.database.store_identity
        assert observation.strategy_id == runtime.identity.strategy_id
        assert observation.configuration_hash == runtime.identity.configuration_hash
    finally:
        runtime.database.close()
