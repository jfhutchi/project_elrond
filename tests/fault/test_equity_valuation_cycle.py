from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from tests.fault.test_reconciliation_halt import RecoveryBroker, _prepare_database, _settings
from tests.integration.test_run_once import NOW, FakeDataSync, FakeSubmitter, _identity
from tests.unit.execution.test_equity_valuation_reconciliation import _snapshot

from quantbot.brokers import BrokerAdapter
from quantbot.execution import StartupRecovery
from quantbot.operations import OperationalMetrics, RunOnceCycle
from quantbot.operations.cycle import CycleStrategyResult
from quantbot.storage import StorageRepository


class NoOrders:
    async def evaluate(self) -> CycleStrategyResult:
        return CycleStrategyResult(submissions=(), signal_count=0)


@pytest.mark.parametrize("difference", ["valuation", "cash", "unexplained_equity"])
async def test_real_recovery_only_halts_cycle_for_unexplained_state(
    tmp_path: Path, difference: str
) -> None:
    local = _snapshot()
    actual = _snapshot("494.19", "492.143166")
    if difference == "cash":
        actual = actual.model_copy(
            update={
                "account": actual.account.model_copy(update={"cash": Decimal("2.06")}),
            }
        )
    elif difference == "unexplained_equity":
        actual = actual.model_copy(update={"positions": local.positions})
    database = _prepare_database(tmp_path, local.account, local.positions)
    broker = RecoveryBroker(account=actual.account, positions=actual.positions)
    calls: list[str] = []
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "cycle.lock",
        strategy_identity=_identity(),
        recovery=StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
        data_sync=FakeDataSync(calls),
        strategy=NoOrders(),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )

    result = await cycle.run("valuation-test", started_at=NOW, trading_date=NOW.date())

    explained = difference == "valuation"
    assert result.status == ("SUCCEEDED" if explained else "HALTED")
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.get_kill_switch_state().engaged is not explained
        record = repository.get_reconciliation("valuation-test:reconciliation")
        assert record is not None
        if explained:
            assert record.diffs == ()
            evidence = record.summary["valuation_changes"]
            assert isinstance(evidence, list) and len(evidence) == 1
            assert evidence[0]["expected"]["value"] == "494.18"
            assert evidence[0]["actual"]["value"] == "494.19"
        else:
            assert record.diffs
            assert "valuation_changes" not in record.summary
    assert broker.mutations == []
    database.close()
