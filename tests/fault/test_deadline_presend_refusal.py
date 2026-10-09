"""Known deadline refusal is a PAPER entry pause; unknown faults still hard-stop."""

from datetime import UTC, date, datetime

import pytest
from tests.integration.test_run_once import (
    FakeDataSync,
    FakeRecovery,
    FakeStrategy,
    _clear,
    _identity,
    _recovery,
)

from quantbot.learner.paper_deadlines import DeadlineEntryBlocked
from quantbot.operations.cycle import RunOnceCycle
from quantbot.operations.deployment import RuntimeContext, RuntimeRole
from quantbot.operations.metrics import OperationalMetrics
from quantbot.risk import OrderPurpose
from quantbot.storage import Database, StorageRepository


class EntryStrategy(FakeStrategy):
    async def evaluate(self):
        result = await super().evaluate()
        return result.model_copy(
            update={
                "submissions": tuple(
                    r.model_copy(update={"purpose": OrderPurpose.ENTRY}) for r in result.submissions
                )
            }
        )


class RefusingSubmitter:
    async def submit(self, *args, **kwargs):
        raise DeadlineEntryBlocked(("DEADLINE_SUPERVISOR_STALE",))


async def test_known_deadline_refusal_pauses_paper_entries_without_global_stop(tmp_path):
    database = Database.create(tmp_path / "operational.db")
    _clear(database)
    calls = []
    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "cycle.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=FakeDataSync(calls),
        strategy=EntryStrategy(calls),
        submitter=RefusingSubmitter(),
        metrics=OperationalMetrics(),
        runtime=RuntimeContext(
            role=RuntimeRole.COORDINATOR,
            store_identity=database.store_identity,
            trading_mode="PAPER",
            process_started_at=datetime(2026, 8, 14, 21, 5, tzinfo=UTC),
            git_commit="abc123",
        ),
    )
    try:
        result = await cycle.run(
            "known-refusal",
            started_at=datetime(2026, 8, 14, 21, 5, tzinfo=UTC),
            trading_date=date(2026, 8, 14),
        )
        assert result.status == "BLOCKED"
        assert result.reasons == ("DEADLINE_SUPERVISOR_STALE",)
        assert result.orders_submitted == 0
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert not repo.get_kill_switch_state().engaged
            assert repo.list_incidents(unresolved_only=True) == []
    finally:
        database.close()


@pytest.mark.parametrize(
    "mode,error,purpose",
    [
        ("LIVE", DeadlineEntryBlocked(("DEADLINE_SUPERVISOR_STALE",)), OrderPurpose.ENTRY),
        ("PAPER", ValueError("unknown failure"), OrderPurpose.ENTRY),
        ("PAPER", DeadlineEntryBlocked(("DEADLINE_SUPERVISOR_STALE",)), OrderPurpose.STRATEGY_EXIT),
    ],
)
async def test_live_exit_and_unknown_failures_still_engage_stop(tmp_path, mode, error, purpose):
    database = Database.create(tmp_path / "operational.db")
    _clear(database)
    calls = []

    class Strategy(FakeStrategy):
        async def evaluate(self):
            result = await super().evaluate()
            return result.model_copy(
                update={
                    "submissions": tuple(
                        r.model_copy(update={"purpose": purpose}) for r in result.submissions
                    )
                }
            )

    class Submitter:
        async def submit(self, *args, **kwargs):
            raise error

    cycle = RunOnceCycle(
        database=database,
        lock_path=tmp_path / "cycle.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=FakeDataSync(calls),
        strategy=Strategy(calls),
        submitter=Submitter(),
        metrics=OperationalMetrics(),
        runtime=RuntimeContext(
            role=RuntimeRole.COORDINATOR,
            store_identity=database.store_identity,
            trading_mode=mode,
            process_started_at=datetime(2026, 8, 14, 21, 5, tzinfo=UTC),
            git_commit="abc123",
        ),
    )
    try:
        with pytest.raises(ValueError):
            await cycle.run(
                "hard-failure",
                started_at=datetime(2026, 8, 14, 21, 5, tzinfo=UTC),
                trading_date=date(2026, 8, 14),
            )
        with database.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_kill_switch_state().engaged
            assert [r.kind for r in repo.list_incidents(unresolved_only=True)] == [
                "UNHANDLED_OPERATIONAL_EXCEPTION"
            ]
    finally:
        database.close()
