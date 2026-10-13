"""Real clearance measurement with immediate synthetic health checks only."""

from datetime import timedelta

import pytest

from quantbot.operations.kill_switch import KillSwitchClearBlocked, KillSwitchController
from quantbot.storage import Database
from tests.unit.operations.test_readiness import NOW, FakeBroker, measure, seed


@pytest.mark.parametrize(
    "reconciliation_us,bar_us,reason",
    [
        (1, 0, "RECONCILIATION_REQUIRED"),
        (0, 1, "MARKET_DATA_UNHEALTHY"),
        (0, 0, None),
    ],
)
def test_future_evidence_cannot_authorize_clearance(tmp_path, reconciliation_us, bar_us, reason):
    database = Database.create(tmp_path / "synthetic.db")
    seed(
        database,
        reconciled_at=NOW + timedelta(microseconds=reconciliation_us),
        bar_at=NOW + timedelta(microseconds=bar_us),
    )
    controller = KillSwitchController(database)
    before = controller.get()
    operation = measure(database, FakeBroker())
    try:
        # FakeBroker.health_check has no pending I/O; no event-loop socket or real
        # broker is needed to exercise this production measuring function.
        with pytest.raises(StopIteration) as complete:
            operation.send(None)
        readiness = complete.value.value
        if reason is None:
            assert readiness.ready
        else:
            assert reason in readiness.blocking
            assert any("future" in line for line in readiness.detail)
            with pytest.raises(KillSwitchClearBlocked):
                controller.clear(reason="SYNTHETIC_ONLY", measured=readiness, updated_at=NOW)
        assert controller.get() == before
    finally:
        operation.close()
        database.close()
