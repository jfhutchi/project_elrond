from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.test_run_once import (
    FakeDataSync,
    FakeRecovery,
    FakeStrategy,
    FakeSubmitter,
    _clear,
    _identity,
    _recovery,
)

from quantbot.operations.cycle import DataSyncResult, RunOnceCycle
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.metrics import OperationalMetrics
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 14, 21, 5, tzinfo=UTC)


class StaleDataSync(FakeDataSync):
    async def sync(self) -> DataSyncResult:
        self.calls.append("sync")
        return DataSyncResult(
            healthy=False,
            completed_through=NOW - timedelta(days=1),
            bars_stored=0,
            stale_symbols=("SPY",),
            reasons=("MARKET_DATA_STALE",),
        )


class DisconnectedStreamSync(FakeDataSync):
    async def sync(self) -> DataSyncResult:
        self.calls.append("sync")
        return DataSyncResult(
            healthy=True,
            completed_through=NOW,
            bars_stored=23,
            stale_symbols=(),
            reasons=(),
            stream_connected=False,
        )


@pytest.mark.asyncio
async def test_stale_data_halts_before_strategy_and_engages_kill_switch(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "quantbot.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=StaleDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )

    result = await cycle.run("run-stale", started_at=NOW, trading_date=date(2026, 8, 14))

    assert result.status == "HALTED"
    assert calls == ["recover", "sync"]
    assert KillSwitchController(database).get().engaged is True
    with database.transaction() as session:
        incidents = StorageRepository(session).list_incidents(unresolved_only=True)
        assert incidents[0].kind == "MARKET_DATA_STALE"
    database.close()


@pytest.mark.asyncio
async def test_disconnected_trade_stream_halts_before_strategy(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "quantbot.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=DisconnectedStreamSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )

    result = await cycle.run("run-stream", started_at=NOW, trading_date=date(2026, 8, 14))

    assert result.status == "HALTED"
    assert "TRADE_STREAM_DISCONNECTED" in result.reasons
    assert calls == ["recover", "sync"]
    database.close()
