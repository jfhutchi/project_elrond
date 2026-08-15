"""Composition root: build the real paper-trading system from environment settings.

Everything above this module is deliberately dependency-injected and side-effect free.
This is the one place where concrete transports, credentials, and filesystem paths are
bound together, so a single import graph produces the runnable application.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import FrameType

from pydantic import SecretStr

from quantbot.backtest import BacktestEngine, BenchmarkVariant
from quantbot.brokers import AlpacaPaperBrokerAdapter, BrokerError, HttpxBrokerTransport
from quantbot.cli import CLIContext, CommandHandler
from quantbot.config import Settings, validate_trading_safety
from quantbot.domain import (
    Bar,
    OrderIntent,
    OrderSide,
    OrderType,
    ReconciliationStatus,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.domain.ids import build_client_order_id
from quantbot.execution import ExecutionCoordinator, StartupRecovery
from quantbot.market_data import (
    AdjustmentMode,
    AlpacaMarketCalendar,
    AlpacaMarketDataClient,
    HttpxMarketDataTransport,
    MarketDataCache,
    MarketDataError,
    MarketDataFeed,
)
from quantbot.operations import (
    AdaptiveMomentumRunner,
    CachedSessionCalendar,
    DaemonRunner,
    DataFreshness,
    HealthInputs,
    LedgerSyncingRecovery,
    MarketDataSettings,
    MarketDataSync,
    OperationalMetrics,
    QualificationTracker,
    ReadinessEvidence,
    RunOnceCycle,
    StrategyDataError,
    evaluate_operational_health,
)
from quantbot.reporting import (
    ProjectStatusContext,
    WeeklyReportRequest,
    build_project_status,
    build_weekly_report,
)
from quantbot.risk import OrderPurpose, calculate_drawdown
from quantbot.storage import Database, StateConflictError, StorageRepository
from quantbot.strategy import (
    StrategyConfig,
    build_strategy_identity,
    load_strategy_config,
    strategy_id_for,
)

DEFAULT_CONFIG_PATH = "config/strategy-v1.yaml"
DEFAULT_DATABASE_PATH = "quantbot.db"
DEFAULT_LOCK_PATH = "quantbot.lock"
DEFAULT_REPORTS_DIR = "reports"
DEFAULT_MAX_DATA_AGE_SECONDS = 86_400
CREDENTIALS_MISSING = "PAPER_CREDENTIALS_NOT_CONFIGURED"


class RuntimeConfigurationError(RuntimeError):
    """Raised when the process environment cannot produce a runnable system."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    config: Path
    database: Path
    lock: Path
    reports: Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default))


def runtime_paths() -> RuntimePaths:
    return RuntimePaths(
        config=_env_path("QUANTBOT_CONFIG", DEFAULT_CONFIG_PATH),
        database=_env_path("QUANTBOT_DB_PATH", DEFAULT_DATABASE_PATH),
        lock=_env_path("QUANTBOT_LOCK_PATH", DEFAULT_LOCK_PATH),
        reports=_env_path("QUANTBOT_REPORTS_DIR", DEFAULT_REPORTS_DIR),
    )


def market_data_settings() -> MarketDataSettings:
    return MarketDataSettings(
        feed=MarketDataFeed(os.environ.get("QUANTBOT_MARKET_DATA_FEED", MarketDataFeed.IEX.value)),
        adjustment=AdjustmentMode(
            os.environ.get("QUANTBOT_MARKET_DATA_ADJUSTMENT", AdjustmentMode.ALL.value)
        ),
    )


def max_data_age_seconds() -> int:
    raw = os.environ.get("QUANTBOT_MAX_DATA_AGE_SECONDS", str(DEFAULT_MAX_DATA_AGE_SECONDS))
    age = int(raw)
    if age <= 0:
        raise RuntimeConfigurationError("QUANTBOT_MAX_DATA_AGE_SECONDS must be positive")
    return age


def resolve_git_commit() -> str:
    """Return the deployed commit, preferring an explicit environment pin."""
    pinned = os.environ.get("QUANTBOT_GIT_COMMIT", "").strip()
    if pinned:
        return pinned
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError(
            "cannot determine the deployed git commit; set QUANTBOT_GIT_COMMIT"
        ) from error
    return completed.stdout.strip()


def paper_credentials(settings: Settings) -> tuple[SecretStr, SecretStr]:
    key = settings.ALPACA_PAPER_API_KEY
    secret = settings.ALPACA_PAPER_API_SECRET
    if key is None or secret is None or not key.get_secret_value() or not secret.get_secret_value():
        raise RuntimeConfigurationError(CREDENTIALS_MISSING)
    return key, secret


def load_or_create_identity(
    database: Database,
    config: StrategyConfig,
    *,
    now: datetime,
) -> StrategyIdentity:
    """Reuse the immutable deployment identity so restarts never re-version a strategy."""
    strategy_id = strategy_id_for(config)
    with database.transaction() as session:
        stored = StorageRepository(session).get_strategy_deployment(strategy_id, config.version)
    if stored is not None:
        return stored
    identity = build_strategy_identity(
        config,
        git_commit=resolve_git_commit(),
        deployment_timestamp=now,
    )
    with database.transaction() as session:
        StorageRepository(session).save_strategy_deployment(identity)
    return identity


@dataclass(slots=True)
class Runtime:
    """Every concrete collaborator the operational commands need."""

    settings: Settings
    config: StrategyConfig
    identity: StrategyIdentity
    paths: RuntimePaths
    database: Database
    broker: AlpacaPaperBrokerAdapter
    market_data: AlpacaMarketDataClient
    sessions: CachedSessionCalendar
    data_sync: MarketDataSync
    strategy: AdaptiveMomentumRunner
    recovery: LedgerSyncingRecovery
    coordinator: ExecutionCoordinator
    metrics: OperationalMetrics
    broker_transport: HttpxBrokerTransport
    data_transport: HttpxMarketDataTransport

    def cycle(self) -> RunOnceCycle:
        return RunOnceCycle(
            database=self.database,
            lock_path=self.paths.lock,
            strategy_identity=self.identity,
            recovery=self.recovery,
            data_sync=self.data_sync,
            strategy=self.strategy,
            submitter=self.coordinator,
            metrics=self.metrics,
            max_data_age_seconds=max_data_age_seconds(),
        )

    async def aclose(self) -> None:
        await self.broker_transport.aclose()
        await self.data_transport.aclose()


def build_runtime(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    now: datetime | None = None,
) -> Runtime:
    """Bind credentials, transports, storage, and policies into one runnable system."""
    active_settings = settings or Settings()
    paths = runtime_paths()
    config = load_strategy_config(paths.config)
    key, secret = paper_credentials(active_settings)
    active_database = database or Database(paths.database)
    identity = load_or_create_identity(
        active_database,
        config,
        now=now or datetime.now(UTC),
    )

    broker_transport = HttpxBrokerTransport()
    data_transport = HttpxMarketDataTransport()
    broker = AlpacaPaperBrokerAdapter(
        api_key=key,
        api_secret=secret,
        transport=broker_transport,
    )
    market_data = AlpacaMarketDataClient(
        api_key=key,
        api_secret=secret,
        transport=data_transport,
    )
    sessions = CachedSessionCalendar(
        AlpacaMarketCalendar(api_key=key, api_secret=secret, transport=data_transport)
    )
    settings_for_data = market_data_settings()
    return Runtime(
        settings=active_settings,
        config=config,
        identity=identity,
        paths=paths,
        database=active_database,
        broker=broker,
        market_data=market_data,
        sessions=sessions,
        data_sync=MarketDataSync(
            database=active_database,
            client=market_data,
            sessions=sessions,
            config=config,
            market_data=settings_for_data,
        ),
        strategy=AdaptiveMomentumRunner(
            database=active_database,
            broker=broker,
            config=config,
            identity=identity,
            sessions=sessions,
            market_data=settings_for_data,
        ),
        recovery=LedgerSyncingRecovery(
            active_database,
            broker,
            StartupRecovery(active_database, broker, active_settings),
        ),
        coordinator=ExecutionCoordinator(active_database, broker, active_settings),
        metrics=OperationalMetrics(),
        broker_transport=broker_transport,
        data_transport=data_transport,
    )


def _run_id(prefix: str, moment: datetime) -> str:
    return f"{prefix}-{moment.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def write_project_status(runtime: Runtime, *, next_priority: str) -> Path:
    """Refresh PROJECT_STATUS.md from durable evidence only."""
    account_id = runtime.settings.EXPECTED_ACCOUNT_ID or "UNVERIFIED_ACCOUNT"
    status = build_project_status(
        runtime.database,
        ProjectStatusContext(
            settings=runtime.settings,
            strategy_id=runtime.identity.strategy_id,
            account_id=account_id,
            current_phase="PAPER_OBSERVATION",
            known_limitations=(
                "Fills are ingested from the broker REST ledger once per cycle; "
                "the push trade stream is not held open between cycles",
            ),
            research_underway=(),
            next_priority=next_priority,
            generated_at=datetime.now(UTC),
        ),
    )
    target = Path("PROJECT_STATUS.md")
    target.write_text(status.render_markdown(), encoding="utf-8")
    return target


async def _doctor(runtime: Runtime) -> dict[str, object]:
    health = await runtime.broker.health_check()
    sessions = await runtime.sessions.sessions()
    clock = await runtime.broker.get_market_clock()
    with runtime.database.transaction() as session:
        repository = StorageRepository(session)
        latest = repository.get_latest_reconciliation()
        kill_switch = repository.get_kill_switch_state()
    bars = tuple(bar.timestamp for bar in _durable_regime_bars(runtime))
    checked_at = datetime.now(UTC)
    completed_through = bars[-1] if bars else checked_at
    operational = evaluate_operational_health(
        HealthInputs(
            broker=health,
            data=DataFreshness(
                completed_through=min(completed_through, checked_at),
                checked_at=checked_at,
                max_age_seconds=max_data_age_seconds(),
            ),
            stream_connected=True,
            risk_healthy=True,
            reconciliation_status=(
                latest.status
                if latest is not None
                else ReconciliationStatus.RECONCILIATION_REQUIRED
            ),
            account_verified=(
                health.account_id is not None
                and health.account_id == runtime.settings.EXPECTED_ACCOUNT_ID
            ),
            kill_switch_engaged=kill_switch.engaged,
        )
    )
    write_project_status(runtime, next_priority="Resolve outstanding doctor reasons")
    return {
        "ok": operational.ready,
        "command": "doctor",
        "health": operational.safe_summary(),
        "reasons": list(operational.reasons),
        "calendar_sessions": len(sessions),
        "market_open": clock.is_open,
        "durable_regime_bars": len(bars),
        "safety": list(validate_trading_safety(runtime.settings, health.account_id).reasons),
    }


def _provider_key() -> str:
    settings_for_data = market_data_settings()
    return MarketDataCache.provider_key(
        settings_for_data.provider,
        settings_for_data.feed,
        settings_for_data.adjustment,
        settings_for_data.timeframe,
    )


def _durable_regime_bars(runtime: Runtime) -> list[Bar]:
    with runtime.database.transaction() as session:
        return StorageRepository(session).list_bars(
            symbol=runtime.config.regime_symbol,
            provider=_provider_key(),
        )


async def _sync_data(runtime: Runtime) -> dict[str, object]:
    result = await runtime.data_sync.sync()
    return {
        "ok": result.healthy and not result.stale_symbols,
        "command": "sync-data",
        "bars_stored": result.bars_stored,
        "completed_through": result.completed_through.isoformat(),
        "stale_symbols": list(result.stale_symbols),
        "reasons": list(result.reasons),
    }


async def _reconcile(runtime: Runtime) -> dict[str, object]:
    started_at = datetime.now(UTC)
    run_id = _run_id("reconcile", started_at)
    result = await runtime.recovery.recover(f"{run_id}:reconciliation", started_at=started_at)
    return {
        "ok": result.ready,
        "command": "reconcile",
        "status": result.reconciliation.status.value,
        "diff_count": len(result.reconciliation.diffs),
        "reasons": list(result.reasons),
        "unresolved_intents": list(result.unresolved_intent_ids),
    }


async def _run_once(runtime: Runtime, *, moment: datetime | None = None) -> dict[str, object]:
    started_at = moment or datetime.now(UTC)
    run_id = _run_id("run", started_at)
    runtime.strategy.bind_run(run_id)
    result = await runtime.cycle().run(
        run_id,
        started_at=started_at,
        trading_date=started_at.astimezone(UTC).date(),
    )
    write_project_status(
        runtime,
        next_priority=(
            "Accumulate paper observations toward the 30-day qualification window"
            if result.status == "SUCCEEDED"
            else f"Resolve cycle halt: {', '.join(result.reasons) or 'unknown'}"
        ),
    )
    return {
        "ok": result.status == "SUCCEEDED",
        "command": "run-once",
        "run_id": result.run_id,
        "status": result.status,
        "reasons": list(result.reasons),
        "signals_evaluated": result.signals_evaluated,
        "orders_submitted": result.orders_submitted,
    }


class _SignalStop:
    """Cooperative stop flag driven by SIGINT/SIGTERM."""

    def __init__(self) -> None:
        self._event = threading.Event()
        for name in ("SIGINT", "SIGTERM"):
            handled = getattr(signal, name, None)
            if handled is not None:
                signal.signal(handled, self._handle)

    def _handle(self, _signum: int, _frame: FrameType | None) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


async def _daemon(runtime: Runtime) -> dict[str, object]:
    async def cycle(moment: datetime) -> object:
        return await _run_once(runtime, moment=moment)

    async def sleeper(delay: Decimal) -> None:
        await asyncio.sleep(max(float(delay), 0.0))

    result = await DaemonRunner(
        clock_provider=runtime.broker,
        cycle=cycle,
        sleeper=sleeper,
        metrics=runtime.metrics,
        lock_path=runtime.paths.lock,
    ).run(_SignalStop(), now=lambda: datetime.now(UTC))
    return {
        "ok": result.stopped_cleanly,
        "command": "daemon",
        "cycles_completed": result.cycles_completed,
    }


def _backtest(runtime: Runtime, args: argparse.Namespace) -> dict[str, object]:
    provider_key = _provider_key()
    with runtime.database.transaction() as session:
        repository = StorageRepository(session)
        histories = {
            symbol: repository.list_bars(symbol=symbol, provider=provider_key)
            for symbol in runtime.config.universe
        }
    missing = sorted(symbol for symbol, bars in histories.items() if not bars)
    if missing:
        return {
            "ok": False,
            "command": "backtest",
            "reason": "DURABLE_BARS_MISSING",
            "symbols": missing,
        }
    engine = BacktestEngine(
        runtime.config,
        initial_cash=Decimal(str(getattr(args, "initial_cash", "100000"))),
    )
    requested = getattr(args, "variant", None)
    variants = (
        (BenchmarkVariant(requested),) if requested else tuple(BenchmarkVariant)
    )
    results = {
        variant.value: engine.run(histories, variant).metrics.model_dump(mode="json")
        for variant in variants
    }
    return {"ok": True, "command": "backtest", "results": results}


def _report_weekly(runtime: Runtime, args: argparse.Namespace) -> dict[str, object]:
    today = datetime.now(UTC).date()
    default_year, default_week, _ = (today - timedelta(days=7)).isocalendar()
    iso_year = int(getattr(args, "iso_year", None) or default_year)
    iso_week = int(getattr(args, "iso_week", None) or default_week)
    account_id = runtime.settings.EXPECTED_ACCOUNT_ID or "UNVERIFIED_ACCOUNT"
    report = build_weekly_report(
        runtime.database,
        WeeklyReportRequest(
            iso_year=iso_year,
            iso_week=iso_week,
            generated_at=datetime.now(UTC),
            strategy_id=runtime.identity.strategy_id,
            account_id=account_id,
        ),
    )
    target = runtime.paths.reports / "weekly" / f"{iso_year}-{iso_week:02d}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.render_markdown(), encoding="utf-8")
    return {
        "ok": True,
        "command": "report-weekly",
        "path": str(target),
        "observed_days": report.observed_days,
    }


async def _paper_smoke(runtime: Runtime) -> dict[str, object]:
    """Submit exactly one 1-share paper order to prove the order lifecycle end to end.

    Reached only after :func:`authorize_paper_smoke` has passed every safety gate and the
    operator supplied the exact one-shot acknowledgement. The acknowledgement is the risk
    approval for this single share; it is not a strategy signal and is recorded as such.
    """
    now = datetime.now(UTC)
    account = await runtime.broker.get_account()
    drawdown = calculate_drawdown(account.account.equity, None, runtime.config)
    signal_date = now.date()
    client_order_id = build_client_order_id(
        runtime.config.version,
        runtime.config.benchmark_symbol,
        signal_date,
        OrderSide.BUY,
        0,
    )
    intent = OrderIntent(
        intent_id=f"smoke-{client_order_id}",
        client_order_id=client_order_id,
        strategy_id=runtime.identity.strategy_id,
        signal_date=signal_date,
        symbol=runtime.config.benchmark_symbol,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("1"),
        created_at=now,
    )
    with runtime.database.transaction() as session:
        StorageRepository(session).create_incident(
            f"paper-smoke-{client_order_id}",
            severity="INFO",
            kind="PAPER_SMOKE_ORDER",
            message="Operator-acknowledged one-share paper smoke order",
            occurred_at=now,
            detail={"client_order_id": client_order_id, "quantity": "1"},
        )
    result = await runtime.coordinator.submit(
        intent,
        purpose=OrderPurpose.ENTRY,
        market_eligible=True,
        drawdown=drawdown,
        risk_approved=True,
    )
    return {
        "ok": True,
        "command": "paper-smoke",
        "client_order_id": result.broker_order.client_order_id,
        "broker_order_id": result.broker_order.broker_order_id,
        "status": result.broker_order.status,
        "recovered_after_ambiguity": result.recovered_after_ambiguity,
    }


def _guarded(command: str, handler: CommandHandler) -> CommandHandler:
    """Report expected operational failures as structured output instead of a traceback.

    The durable fail-closed effects (kill switch, halted run, incident) have already been
    applied by the time these propagate; only the operator-facing rendering changes.
    """

    def guarded(args: argparse.Namespace) -> Mapping[str, object]:
        try:
            return handler(args)
        except (BrokerError, MarketDataError, StrategyDataError, StateConflictError) as error:
            return {
                "ok": False,
                "command": command,
                "reason": type(error).__name__,
                "detail": str(error),
            }

    return guarded


def build_cli_context(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
) -> CLIContext:
    """Assemble the CLI context with every operational handler wired to real collaborators."""
    active = runtime or build_runtime(settings)
    raw: dict[str, CommandHandler] = {
        "doctor": lambda _args: asyncio.run(_doctor(active)),
        "sync-data": lambda _args: asyncio.run(_sync_data(active)),
        "reconcile": lambda _args: asyncio.run(_reconcile(active)),
        "run-once": lambda _args: asyncio.run(_run_once(active)),
        "daemon": lambda _args: asyncio.run(_daemon(active)),
        "backtest": lambda args: _backtest(active, args),
        "report-weekly": lambda args: _report_weekly(active, args),
        "paper-smoke": lambda _args: asyncio.run(_paper_smoke(active)),
    }
    handlers = {name: _guarded(name, handler) for name, handler in raw.items()}
    with active.database.transaction() as session:
        latest = StorageRepository(session).get_latest_reconciliation()
    reconciled = (
        latest is not None and latest.status is ReconciliationStatus.RECONCILED and not latest.diffs
    )
    return CLIContext(
        settings=active.settings,
        database=active.database,
        handlers=handlers,
        reported_account_id=active.settings.EXPECTED_ACCOUNT_ID,
        clearance_evidence=ReadinessEvidence(
            paper_mode=active.settings.TRADING_MODE.value == "PAPER",
            account_verified=active.settings.EXPECTED_ACCOUNT_ID is not None,
            broker_healthy=active.settings.BROKER_HEALTHY,
            data_healthy=active.settings.MARKET_DATA_HEALTHY,
            risk_healthy=active.settings.RISK_ENGINE_HEALTHY,
            reconciliation_successful=reconciled,
        ),
    )


def qualification_tracker(runtime: Runtime) -> QualificationTracker:
    return QualificationTracker(runtime.database, runtime.identity.strategy_id)


def last_completed_iso_week(today: date) -> tuple[int, int]:
    year, week, _ = (today - timedelta(days=7)).isocalendar()
    return year, week
