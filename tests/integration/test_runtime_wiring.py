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
from quantbot.runtime import (
    RuntimeConfigurationError,
    build_runtime,
    load_or_create_identity,
)
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, load_strategy_config

ACCOUNT_ID = "paper-account-1"
UNIVERSE = ("SPY", "QQQ", "TLT", "GLD")
SESSION_COUNT = 90
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


async def test_wired_cycle_reconciles_syncs_data_and_submits_a_risk_sized_order(
    tmp_path: Path,
) -> None:
    config = load_strategy_config(_write_config(tmp_path))
    database = Database(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness",
        evidence=_ready_evidence(),
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


async def test_rerunning_the_same_trading_day_never_submits_a_duplicate_order(
    tmp_path: Path,
) -> None:
    """Duplicate-order protection: a re-run of the same signal day submits nothing new."""
    config = load_strategy_config(_write_config(tmp_path))
    database = Database(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="test fixture readiness",
        evidence=_ready_evidence(),
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
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(tmp_path / "quantbot.lock"))
    monkeypatch.setenv("QUANTBOT_GIT_COMMIT", "0123456789abcdef")

    runtime = build_runtime(_ready_settings())

    assert isinstance(runtime.broker_transport, HttpxBrokerTransport)
    assert runtime.identity.git_commit == "0123456789abcdef"
    assert runtime.identity.configuration_hash
    assert runtime.cycle() is not None
    runtime.database.close()
