"""Real cycle/storage regressions; synthetic collaborators never contact a broker."""

from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.test_run_once import (
    NOW,
    FakeDataSync,
    FakeRecovery,
    FakeStrategy,
    FakeSubmitter,
    _clear,
    _identity,
    _recovery,
)

from quantbot.operations.cycle import DataSyncResult, RunOnceCycle
from quantbot.operations.deployment import RuntimeContext, RuntimeRole
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.metrics import OperationalMetrics
from quantbot.storage import Database, StorageRepository


class ResultSync(FakeDataSync):
    def __init__(self, calls: list[str], result: DataSyncResult) -> None:
        super().__init__(calls)
        self.result = result

    async def sync(self) -> DataSyncResult:
        self.calls.append("sync")
        return self.result


def _data(**changes: object) -> DataSyncResult:
    return DataSyncResult(
        healthy=True, completed_through=NOW, bars_stored=1, stale_symbols=(), reasons=()
    ).model_copy(update=changes)


def _cycle(
    database: Database,
    tmp_path: Path,
    calls: list[str],
    sync: ResultSync,
    *,
    mode: str = "PAPER",
    recovery: FakeRecovery | None = None,
) -> RunOnceCycle:
    return RunOnceCycle(
        database=database,
        lock_path=tmp_path / "writer.lock",
        strategy_identity=_identity(),
        recovery=recovery or FakeRecovery(_recovery(), calls),
        data_sync=sync,
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
        runtime=RuntimeContext(
            role=RuntimeRole.COORDINATOR,
            store_identity=database.store_identity,
            trading_mode=mode,
            process_started_at=NOW,
            git_commit="abc123",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["MARKET_DATA_STALE", "MARKET_DATA_INCOMPLETE_UNIVERSE", "PAPER_PREDICTION_STALE"]
)
async def test_explicit_paper_data_pause_rechecks_and_resumes_without_clearing_switch(
    tmp_path: Path,
    reason: str,
) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    before = KillSwitchController(database).get()
    calls: list[str] = []
    sync = ResultSync(calls, _data(healthy=False, reasons=(reason,)))
    cycle = _cycle(database, tmp_path, calls, sync)
    for n in range(2):
        paused = await cycle.run(f"pause-{n}", started_at=NOW, trading_date=NOW.date())
        assert paused.status == "BLOCKED"
        assert paused.orders_submitted == 0 and paused.signals_evaluated == 0
        assert paused.reasons == (reason,)
    assert calls == ["recover", "sync", "recover", "sync"]
    assert KillSwitchController(database).get() == before
    with database.transaction() as session:
        repository = StorageRepository(session)
        run = repository.get_run("pause-1")
        assert run is not None and run.status == "BLOCKED"
        assert run.detail["blocking_scope"] == "STRATEGY_ENTRIES"
        assert run.detail["retryable"] is True
        assert not repository.list_incidents(unresolved_only=True)
        day = repository.get_qualification_day(_identity().strategy_id, NOW.date())
        assert day is not None and day.qualified is False
    sync.result = _data()
    fresh = await cycle.run("fresh", started_at=NOW, trading_date=NOW.date())
    assert fresh.status == "SUCCEEDED" and fresh.orders_submitted == 1
    assert calls[-4:] == ["recover", "sync", "strategy", "submit"]
    assert KillSwitchController(database).get() == before
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"risk_healthy": False}, "RISK_ENGINE_UNHEALTHY"),
        ({"stream_connected": False}, "TRADE_STREAM_DISCONNECTED"),
        ({"reasons": ("MARKET_DATA_STALE", "UNKNOWN_DATA_FAULT")}, "UNKNOWN_DATA_FAULT"),
        ({"completed_through": NOW + timedelta(seconds=1)}, "MARKET_DATA_FROM_FUTURE"),
        ({"reasons": ()}, "MARKET_DATA_UNHEALTHY"),
    ],
)
async def test_hard_fault_wins_over_transient_paper_data_pause(
    tmp_path: Path,
    changes: dict[str, object],
    expected: str,
) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    calls: list[str] = []
    data = _data(healthy=False, reasons=("MARKET_DATA_STALE",)).model_copy(update=changes)
    cycle = _cycle(database, tmp_path, calls, ResultSync(calls, data))
    result = await cycle.run("mixed", started_at=NOW, trading_date=NOW.date())
    assert result.status == "HALTED" and result.orders_submitted == 0
    assert expected in result.reasons
    switch = KillSwitchController(database).get()
    assert switch.engaged and switch.reason == expected
    assert calls == ["recover", "sync"]
    database.close()


@pytest.mark.asyncio
async def test_data_recovery_cannot_clear_manual_stop(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    calls: list[str] = []
    sync = ResultSync(calls, _data(healthy=False, reasons=("MARKET_DATA_STALE",)))
    cycle = _cycle(database, tmp_path, calls, sync)
    await cycle.run("paused", started_at=NOW, trading_date=NOW.date())
    before = KillSwitchController(database).engage(reason="operator stop", updated_at=NOW)
    sync.result = _data()
    stopped = await cycle.run("manual", started_at=NOW, trading_date=NOW.date())
    assert stopped.status == "HALTED" and stopped.reasons == ("KILL_SWITCH_ENGAGED",)
    assert KillSwitchController(database).get() == before
    assert calls == ["recover", "sync", "recover"]
    database.close()


@pytest.mark.asyncio
async def test_reconciliation_failure_after_pause_still_engages_hard_stop(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    calls: list[str] = []
    recovery = FakeRecovery(_recovery(), calls)
    sync = ResultSync(calls, _data(healthy=False, reasons=("MARKET_DATA_STALE",)))
    cycle = _cycle(database, tmp_path, calls, sync, recovery=recovery)
    paused = await cycle.run("paused", started_at=NOW, trading_date=NOW.date())
    assert paused.status == "BLOCKED"
    sync.result = _data()
    recovery.result = _recovery(ready=False)
    failed = await cycle.run("bad-recon", started_at=NOW, trading_date=NOW.date())
    assert failed.status == "HALTED" and failed.orders_submitted == 0
    assert KillSwitchController(database).get().reason == "RECONCILIATION_FAILED"
    assert calls == ["recover", "sync", "recover"]
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["LIVE"])
async def test_paper_pause_never_changes_other_mode_fault_policy(tmp_path: Path, mode: str) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    calls: list[str] = []
    cycle = _cycle(
        database,
        tmp_path,
        calls,
        ResultSync(calls, _data(healthy=False, reasons=("MARKET_DATA_STALE",))),
        mode=mode,
    )
    result = await cycle.run("other-mode", started_at=NOW, trading_date=NOW.date())
    assert result.status == "HALTED" and result.orders_submitted == 0
    assert KillSwitchController(database).get().engaged
    database.close()


@pytest.mark.asyncio
async def test_pause_requires_broker_paper_proof(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    calls: list[str] = []
    result = _recovery()
    result = result.model_copy(
        update={"broker_health": result.broker_health.model_copy(update={"environment": "LIVE"})}
    )
    cycle = _cycle(
        database,
        tmp_path,
        calls,
        ResultSync(calls, _data(healthy=False, reasons=("MARKET_DATA_STALE",))),
        recovery=FakeRecovery(result, calls),
    )
    stopped = await cycle.run("wrong-broker", started_at=NOW, trading_date=NOW.date())
    assert stopped.status == "HALTED" and stopped.orders_submitted == 0
    assert KillSwitchController(database).get().engaged
    assert calls == ["recover", "sync"]
    database.close()


@pytest.mark.asyncio
async def test_data_pause_does_not_latch_other_strategy(tmp_path: Path) -> None:
    database = Database.create(tmp_path / "paper.db")
    _clear(database)
    calls: list[str] = []
    paused_cycle = _cycle(
        database,
        tmp_path,
        calls,
        ResultSync(calls, _data(healthy=False, reasons=("MARKET_DATA_STALE",))),
    )
    paused = await paused_cycle.run("affected", started_at=NOW, trading_date=NOW.date())
    assert paused.status == "BLOCKED" and paused.orders_submitted == 0
    other = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "writer.lock",
        strategy_identity=_identity().model_copy(update={"strategy_id": "other"}),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=FakeDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )
    result = await other.run("independent", started_at=NOW, trading_date=NOW.date())
    assert result.status == "SUCCEEDED" and result.orders_submitted == 1
    assert not KillSwitchController(database).get().engaged
    database.close()
