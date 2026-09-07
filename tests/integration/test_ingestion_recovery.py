"""Recovery through the real collector and SQLite, with only network I/O replaced."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from quantbot.domain import Bar
from quantbot.ingestion import (
    CursorConflict,
    IngestionControlPlane,
    IngestionError,
    RunMode,
    RunStatus,
    SourceHealth,
)
from quantbot.market_data import AlpacaMarketDataClient, BarQuery, TransportResponse
from quantbot.market_data.base import (
    MarketDataBatch,
    MarketDataProtocolError,
    MarketDataRequestError,
)
from quantbot.operations import MarketDataSync
from quantbot.operations.runners import COLLECTION_TIMEOUT_SECONDS, StrategyDataError
from quantbot.risk import PortfolioVetoPolicy
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, XNYSSession
from tests.integration.test_runtime_wiring import SESSION_DATES, _config_payload


class Calendar:
    def __init__(self) -> None:
        self.values = tuple(
            XNYSSession(
                session_date=day,
                open_at=datetime.combine(day, time(13, 30), tzinfo=UTC),
                close_at=datetime.combine(day, time(20), tzinfo=UTC),
            )
            for day in SESSION_DATES
        )

    async def sessions(self) -> tuple[XNYSSession, ...]:
        return self.values


class Source:
    def __init__(self, calendar: Calendar) -> None:
        self.calendar = calendar
        self.queries: list[BarQuery] = []
        self.empty = False
        self.duplicate = False
        self.conflict = False
        self.omit_symbol: str | None = None
        self.provider = "alpaca"
        self.block = False
        self.omit_at: datetime | None = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_historical_bars(self, query: BarQuery) -> MarketDataBatch:
        self.queries.append(query)
        self.entered.set()
        if self.block:
            await self.release.wait()
        bars = tuple(
            Bar(
                symbol=symbol,
                timestamp=session.close_at,
                open=Decimal(100),
                high=Decimal(101),
                low=Decimal(99),
                close=Decimal(100),
                volume=Decimal(1000),
                adjustment=Decimal(1),
            )
            for session in self.calendar.values
            if query.start <= session.close_at <= query.end and session.close_at != self.omit_at
            for symbol in query.symbols
            if not self.empty and symbol != self.omit_symbol
        )
        if self.duplicate and bars:
            extra = bars[-1]
            if self.conflict:
                extra = extra.model_copy(update={"volume": Decimal(999)})
            bars = (*bars, extra)
        return MarketDataBatch(
            provider=self.provider,
            feed=query.feed,
            adjustment=query.adjustment,
            timeframe=query.timeframe,
            requested_symbols=query.symbols,
            start=query.start,
            end=query.end,
            bars=bars,
            page_count=1,
        )


class Harness:
    def __init__(self, path: Path) -> None:
        self.path = path
        target = path / "ingestion.db"
        self.database = Database(target) if target.exists() else Database.create(target)
        self.calendar = Calendar()
        self.source = Source(self.calendar)
        self.now = self.calendar.values[60].close_at + timedelta(minutes=5)

    def collector(
        self, *, collection_timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS
    ) -> MarketDataSync:
        return MarketDataSync(
            database=self.database,
            client=self.source,
            sessions=self.calendar,
            config=StrategyConfig.model_validate(_config_payload()),
            veto_policy=PortfolioVetoPolicy(correlation_lookback_sessions=30),
            clock=lambda: self.now,
            collection_timeout_seconds=collection_timeout_seconds,
        )


@pytest.fixture
def h(tmp_path: Path):
    harness = Harness(tmp_path)
    yield harness
    harness.database.close()


@pytest.mark.parametrize("empty", [False, True])
async def test_incomplete_attempt_is_failed_and_preserves_checkpoint(h: Harness, empty: bool):
    sync = h.collector()
    await sync.sync()
    with h.database.transaction() as session:
        before = IngestionControlPlane(session).cursor(sync.source_id, "universe")
    h.source.empty = empty
    h.source.omit_symbol = "SPY"
    result = await sync.sync()
    assert not result.healthy
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.latest_run(sync.source_id).status is RunStatus.FAILED
        assert plane.cursor(sync.source_id, "universe") == before
        assert (
            plane.status(sync.source_id, stream_key="universe", expected_success_after=h.now).health
            is SourceHealth.FAILED
        )
        assert plane.open_incidents(sync.source_id)
    h.source.empty = False
    h.source.omit_symbol = None
    assert (await h.collector().sync()).healthy
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert not plane.open_incidents(sync.source_id)
        assert plane.latest_run(sync.source_id).counts.inserted == 0


async def test_restart_recovers_gap_older_than_warmup_and_tags_backfill(h: Harness):
    sync = h.collector()
    await sync.sync()
    previous = h.now
    anchor = h.calendar.values[60].close_at
    h.now = h.calendar.values[130].close_at + timedelta(minutes=5)
    assert (await h.collector().sync()).healthy
    assert h.source.queries[-1].start <= anchor
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        run = plane.latest_run(sync.source_id)
        assert run.mode is RunMode.BACKFILL
        assert run.requested_start == h.source.queries[-1].start.isoformat()
        assert run.requested_end == h.source.queries[-1].end.isoformat()
        assert plane.cursor(sync.source_id, "universe").last_success_at == previous
        bars = StorageRepository(session).list_bars(symbol="SPY")
        assert h.calendar.values[80].close_at in {bar.timestamp for bar in bars}
    assert (await h.collector().sync()).healthy
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.latest_run(sync.source_id).mode is RunMode.LIVE
        assert plane.cursor(sync.source_id, "universe").last_success_at == h.now


async def test_restart_without_cursor_uses_existing_observations(h: Harness):
    sync = h.collector()
    await sync.sync()
    with h.database.transaction() as session:
        IngestionControlPlane(session).reset_cursor(
            sync.source_id, "universe", now=h.now, reason="simulate legacy store"
        )
    h.now = h.calendar.values[130].close_at + timedelta(minutes=5)
    await h.collector().sync()
    assert h.source.queries[-1].start <= h.calendar.values[60].close_at


async def test_unplanned_failure_cannot_erase_an_unfinished_recovery_range(h: Harness):
    sync = h.collector()
    await sync.sync()
    with h.database.transaction() as session:
        IngestionControlPlane(session).reset_cursor(
            sync.source_id, "universe", now=h.now, reason="legacy recovery"
        )
    missing = h.calendar.values[65].close_at
    h.source.omit_at = missing
    h.now = h.calendar.values[130].close_at + timedelta(minutes=5)
    assert not (await sync.sync()).healthy
    calendar = h.calendar.values
    h.calendar.values = ()
    with pytest.raises(StrategyDataError):
        await sync.sync()
    h.calendar.values = calendar
    h.source.omit_at = None
    assert (await h.collector().sync()).healthy
    with h.database.transaction() as session:
        assert missing in {bar.timestamp for bar in StorageRepository(session).list_bars()}


async def test_exact_provider_duplicates_are_idempotent_but_conflicts_fail(h: Harness):
    h.source.duplicate = True
    sync = h.collector()
    assert (await sync.sync()).healthy
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        run = plane.latest_run(sync.source_id)
        assert run.counts.duplicate == 1
        before = plane.cursor(sync.source_id, "universe")
    h.source.conflict = True
    with pytest.raises(MarketDataProtocolError, match="conflicting duplicate"):
        await sync.sync()
    with h.database.transaction() as session:
        assert IngestionControlPlane(session).cursor(sync.source_id, "universe") == before


async def test_response_identity_cannot_change_the_configured_stream(h: Harness):
    h.source.provider = "other-provider"
    sync = h.collector()
    with pytest.raises(MarketDataProtocolError, match="identity"):
        await sync.sync()
    with h.database.transaction() as session:
        assert not StorageRepository(session).list_bars()


async def test_cursor_version_is_captured_before_fetch(h: Harness):
    slow = h.collector()
    h.source.block = True
    pending = asyncio.create_task(slow.sync())
    await h.source.entered.wait()
    h.source.block = False
    assert (await h.collector().sync()).healthy
    h.source.release.set()
    with pytest.raises(CursorConflict):
        await pending
    with h.database.transaction() as session:
        cursor = IngestionControlPlane(session).cursor(slow.source_id, "universe")
        assert cursor.version == 1


async def test_reset_and_recreated_cursor_cannot_be_overwritten_by_old_fetch(h: Harness):
    h.now = h.calendar.values[60].close_at - timedelta(seconds=30)
    sync = h.collector()
    await sync.sync()
    h.source.entered.clear()
    h.source.block = True
    pending = asyncio.create_task(sync.sync())
    await h.source.entered.wait()
    with h.database.transaction() as session:
        IngestionControlPlane(session).reset_cursor(
            sync.source_id, "universe", now=h.now, reason="operator recovery"
        )
    h.now += timedelta(seconds=60)
    h.source.block = False
    await h.collector().sync()
    with h.database.transaction() as session:
        replacement = IngestionControlPlane(session).cursor(sync.source_id, "universe")
        assert replacement.version == 1
    h.source.release.set()
    with pytest.raises(CursorConflict):
        await pending
    with h.database.transaction() as session:
        assert IngestionControlPlane(session).cursor(sync.source_id, "universe") == replacement


async def test_provider_deadline_records_failure_without_checkpoint(h: Harness):
    h.source.block = True
    sync = h.collector(collection_timeout_seconds=0.02)
    with pytest.raises(TimeoutError):
        await sync.sync()
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.latest_run(sync.source_id).status is RunStatus.FAILED
        assert plane.cursor(sync.source_id, "universe") is None


async def test_source_pause_during_fetch_refuses_the_result(h: Harness):
    h.source.block = True
    sync = h.collector()
    pending = asyncio.create_task(sync.sync())
    await h.source.entered.wait()
    with h.database.transaction() as session:
        IngestionControlPlane(session).set_enabled(
            sync.source_id, enabled=False, now=h.now, reason="pause during provider outage"
        )
    h.source.release.set()
    with pytest.raises(IngestionError, match="disabled"):
        await pending
    with h.database.transaction() as session:
        assert not StorageRepository(session).list_bars()


async def test_old_calendar_cannot_move_a_checkpoint_backwards(h: Harness):
    sync = h.collector()
    await sync.sync()
    h.now -= timedelta(days=5)
    with pytest.raises(StrategyDataError, match="ahead of the calendar"):
        await h.collector().sync()
    with h.database.transaction() as session:
        assert IngestionControlPlane(session).cursor(sync.source_id, "universe").version == 1


async def test_cancellation_is_durable_and_still_propagates(h: Harness):
    h.source.block = True
    sync = h.collector()
    task = asyncio.create_task(sync.sync())
    await h.source.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.latest_run(sync.source_id).status is RunStatus.FAILED
        assert plane.cursor(sync.source_id, "universe") is None


async def test_disabled_source_makes_no_provider_request(h: Harness):
    sync = h.collector()
    await sync.sync()
    with h.database.transaction() as session:
        IngestionControlPlane(session).set_enabled(
            sync.source_id, enabled=False, now=h.now, reason="operator pause"
        )
    requested = len(h.source.queries)
    with pytest.raises(IngestionError, match="disabled"):
        await h.collector().sync()
    assert len(h.source.queries) == requested
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert (
            plane.status(sync.source_id, stream_key="universe", expected_success_after=h.now).health
            is SourceHealth.DISABLED
        )


async def test_recovery_refuses_a_calendar_that_cannot_cover_the_gap(h: Harness):
    sync = h.collector()
    await sync.sync()
    h.now = h.calendar.values[130].close_at + timedelta(minutes=5)
    h.calendar.values = h.calendar.values[70:]
    requested = len(h.source.queries)
    with pytest.raises(StrategyDataError, match="RECOVERY_CALENDAR_TOO_SHORT"):
        await h.collector().sync()
    assert len(h.source.queries) == requested
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.cursor(sync.source_id, "universe").version == 1
        assert plane.latest_run(sync.source_id).status is RunStatus.FAILED


async def test_killed_writer_rolls_back_bars_and_restart_replays_without_loss(h: Harness):
    # Exit after SQL inserts but before the enclosing transaction commits. This is a process
    # death, not a caught exception pretending to be a crash.
    script = """
import asyncio, os, sys
from pathlib import Path
from tests.integration.test_ingestion_recovery import Harness
from quantbot.market_data import MarketDataCache
original = MarketDataCache.store
def crash(self, report):
    original(self, report)
    os._exit(71)
MarketDataCache.store = crash
asyncio.run(Harness(Path(sys.argv[1])).collector().sync())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(h.path)], capture_output=True, timeout=30
    )
    assert result.returncode == 71, result.stderr.decode(errors="replace")
    sync = h.collector()
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.latest_run(sync.source_id).status is RunStatus.RUNNING
        assert plane.cursor(sync.source_id, "universe") is None
        assert not StorageRepository(session).list_bars()
    h.now += timedelta(hours=7)
    collected = await sync.sync()
    assert collected.healthy
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert {run.status for run in plane.runs_for(sync.source_id)} == {
            RunStatus.ABANDONED,
            RunStatus.SUCCEEDED,
        }
        assert len(StorageRepository(session).list_bars()) == collected.bars_stored
    replay = await h.collector().sync()
    assert replay.healthy and replay.bars_stored == 0


async def test_rate_limit_retries_are_bounded_and_exhaustion_is_durable(h: Harness):
    calls = []
    sleeps = []

    class RateLimited:
        async def request(self, *args, **kwargs):
            calls.append(args)
            return TransportResponse(status_code=429, headers={"Retry-After": "1"}, content=b"")

    async def no_wait(delay):
        sleeps.append(delay)

    sync = h.collector()
    sync._client = AlpacaMarketDataClient(
        api_key=SecretStr("fixture"),
        api_secret=SecretStr("fixture"),
        transport=RateLimited(),
        sleeper=no_wait,
    )
    with pytest.raises(MarketDataRequestError, match="429"):
        await sync.sync()
    assert len(calls) == 3 and sleeps == [Decimal(1), Decimal(1)]
    with h.database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.latest_run(sync.source_id).status is RunStatus.FAILED
        assert plane.open_incidents(sync.source_id)
        assert plane.cursor(sync.source_id, "universe") is None
