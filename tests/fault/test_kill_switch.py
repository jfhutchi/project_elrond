from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.operations.kill_switch import (
    KillSwitchClearBlocked,
    KillSwitchController,
    ReadinessEvidence,
)
from quantbot.storage import Database

NOW = datetime(2026, 8, 14, 21, 5, tzinfo=UTC)


def _ready(**changes: bool) -> ReadinessEvidence:
    evidence = ReadinessEvidence(
        paper_mode=True,
        account_verified=True,
        broker_healthy=True,
        data_healthy=True,
        risk_healthy=True,
        reconciliation_successful=True,
    )
    return evidence.model_copy(update=changes)


def test_new_database_starts_engaged_and_clear_cannot_bypass_other_gates(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "quantbot.db")
    controller = KillSwitchController(database)

    assert controller.get().engaged is True
    with pytest.raises(KillSwitchClearBlocked) as error:
        controller.clear(
            reason="operator requested",
            evidence=_ready(data_healthy=False),
            updated_at=NOW,
        )

    assert "MARKET_DATA_UNHEALTHY" in error.value.reasons
    assert controller.get().engaged is True
    database.close()


def test_clear_and_hard_failure_engagement_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "quantbot.db"
    database = Database(path)
    controller = KillSwitchController(database)
    controller.clear(reason="all paper gates verified", evidence=_ready(), updated_at=NOW)
    assert controller.get().engaged is False
    controller.engage_hard_failure("RECONCILIATION_FAILED", updated_at=NOW)
    assert controller.get().engaged is True
    database.close()

    restarted = Database(path)
    assert KillSwitchController(restarted).get().engaged is True
    assert KillSwitchController(restarted).get().reason == "RECONCILIATION_FAILED"
    restarted.close()
