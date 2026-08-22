from datetime import UTC, date, datetime
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


@pytest.mark.asyncio
async def test_unresolved_restart_recovery_halts_before_data_or_orders(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "quantbot.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(ready=False), calls),
        data_sync=FakeDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )

    result = await cycle.run("run-recovery", started_at=NOW, trading_date=date(2026, 8, 14))

    assert result.status == "HALTED"
    assert "RECONCILIATION_REQUIRED" in result.reasons
    assert calls == ["recover"]
    assert KillSwitchController(database).get().engaged is True
    database.close()


class ExplodingDataSync(FakeDataSync):
    async def sync(self) -> DataSyncResult:
        self.calls.append("sync")
        raise RuntimeError("credential-looking-value-must-not-be-persisted")


@pytest.mark.asyncio
async def test_unhandled_exception_is_captured_without_message_and_re_raised(
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
        data_sync=ExplodingDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )

    with pytest.raises(RuntimeError, match="credential-looking"):
        await cycle.run("run-error", started_at=NOW, trading_date=date(2026, 8, 14))

    with database.transaction() as session:
        repository = StorageRepository(session)
        run = repository.get_run("run-error")
        incidents = repository.list_incidents(unresolved_only=True)
        assert run is not None and run.status == "ERROR"
        assert incidents[0].message == "Unhandled exception during operational cycle"
        assert "credential-looking" not in str(incidents[0].detail)
        # The origin is derived from code rather than from data, so it cannot carry a credential
        # whatever the exception held -- and it is what makes the incident diagnosable once the
        # journal has rotated. A halt that says only "StateConflictError" happened on 2026-08-20
        # and was not enough to find the cause.
        assert str(incidents[0].detail["origin"]).startswith("cycle.py:")
    assert KillSwitchController(database).get().engaged is True
    database.close()
