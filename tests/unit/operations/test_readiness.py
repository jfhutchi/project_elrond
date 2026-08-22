"""Clearing the kill switch against measurement rather than against a dotenv (#38).

Three of the six clearance conditions used to be environment variables. `BROKER_HEALTHY=true` in
a file is a claim by whoever last edited it, and the gate meant to establish that resuming is
safe was reading its input from a place the thing being gated could set. These tests are about
the probes replacing that, and each asserts the *named* condition rather than "some refusal
happened" -- a refusal for the wrong reason is indistinguishable from the check working.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.brokers.base import BrokerHealth
from quantbot.domain import Bar, ReconciliationStatus
from quantbot.operations.kill_switch import (
    KillSwitchClearBlocked,
    KillSwitchController,
    MeasuredReadiness,
    ReadinessEvidence,
)
from quantbot.operations.readiness import measure_readiness
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
ACCOUNT = "paper-1"


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "quantbot.db")
    yield db
    db.close()


class FakeBroker:
    """Answers a health check the way the adapter would. No network, real code path."""

    def __init__(self, *, healthy: bool = True, account_id: str | None = ACCOUNT) -> None:
        self._healthy = healthy
        self._account_id = account_id
        self.calls = 0

    async def health_check(self) -> BrokerHealth:
        self.calls += 1
        return BrokerHealth(
            healthy=self._healthy,
            provider="alpaca",
            environment="paper",
            account_id=self._account_id,
            reasons=() if self._healthy else ("BROKER_UNAVAILABLE",),
        )


def seed(database: Database, *, reconciled_at: datetime, bar_at: datetime) -> None:
    """A ledger that looks like one the daemon has been running against."""
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_reconciliation(
            "reconciliation-1",
            status=ReconciliationStatus.RECONCILED,
            started_at=reconciled_at - timedelta(minutes=1),
            completed_at=reconciled_at,
            diffs=(),
        )
        repository.save_bars(
            [
                Bar(
                    symbol="SPY",
                    timestamp=bar_at,
                    open="100",
                    high="110",
                    low="95",
                    close="105",
                    volume="1000",
                    adjustment="1",
                )
            ],
            provider="alpaca",
        )


async def measure(database: Database, broker: object | None, **overrides: object):
    fields: dict[str, object] = {
        "paper_mode": True,
        "expected_account_id": ACCOUNT,
        "risk_caps_valid": True,
        "now": NOW,
    }
    fields.update(overrides)
    return await measure_readiness(database, broker, **fields)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_fully_probed_ledger_clears_the_switch(database: Database) -> None:
    """The permitting case, without which every refusal below could be a blanket denial."""
    seed(database, reconciled_at=NOW - timedelta(hours=2), bar_at=NOW - timedelta(hours=20))
    broker = FakeBroker()

    readiness = await measure(database, broker)

    assert readiness.ready, readiness.blocking
    assert broker.calls == 1, "the broker was actually asked, not assumed"
    assert readiness.unprobed == ()
    state = KillSwitchController(database).clear(
        reason="conditions verified", measured=readiness, updated_at=NOW
    )
    assert state.engaged is False


@pytest.mark.asyncio
async def test_an_unreachable_broker_blocks_the_clear(database: Database) -> None:
    """The condition that used to be read from BROKER_HEALTHY in a dotenv."""
    seed(database, reconciled_at=NOW - timedelta(hours=2), bar_at=NOW - timedelta(hours=20))

    readiness = await measure(database, FakeBroker(healthy=False))

    assert "BROKER_UNHEALTHY" in readiness.blocking
    with pytest.raises(KillSwitchClearBlocked) as blocked:
        KillSwitchController(database).clear(
            reason="resume anyway", measured=readiness, updated_at=NOW
        )
    assert "BROKER_UNHEALTHY" in blocked.value.reasons


@pytest.mark.asyncio
async def test_a_stale_reconciliation_blocks_the_clear(database: Database) -> None:
    """New behaviour: a clean reconciliation from three weeks ago used to be sufficient.

    A ledger that matched three weeks ago is not a ledger that matches. The record is RECONCILED
    with no diffs here, so the *only* thing that can refuse it is the freshness window.
    """
    seed(database, reconciled_at=NOW - timedelta(days=21), bar_at=NOW - timedelta(hours=20))

    readiness = await measure(database, FakeBroker())

    assert "RECONCILIATION_REQUIRED" in readiness.blocking
    assert any("21d old" in line for line in readiness.detail), readiness.detail


@pytest.mark.asyncio
async def test_a_broker_reporting_a_different_account_blocks_the_clear(
    database: Database,
) -> None:
    """"Is an account id configured?" cannot catch a key that opens a different account."""
    seed(database, reconciled_at=NOW - timedelta(hours=2), bar_at=NOW - timedelta(hours=20))

    readiness = await measure(database, FakeBroker(account_id="someone-elses-account"))

    assert "ACCOUNT_VERIFICATION_REQUIRED" in readiness.blocking
    assert any("different account" in line for line in readiness.detail), readiness.detail


@pytest.mark.asyncio
async def test_stale_market_data_blocks_the_clear(database: Database) -> None:
    """Resuming against a ledger whose newest bar is a fortnight old is trading blind."""
    seed(database, reconciled_at=NOW - timedelta(hours=2), bar_at=NOW - timedelta(days=14))

    readiness = await measure(database, FakeBroker())

    assert "MARKET_DATA_UNHEALTHY" in readiness.blocking


@pytest.mark.asyncio
async def test_an_unprobed_condition_blocks_the_clear_and_says_it_was_unprobed(
    database: Database,
) -> None:
    """Failing closed on what could not be measured, which is the whole design.

    An unmeasured condition reported as satisfied is the exact defect this module removes, so
    "we could not check the broker" has to refuse just as firmly as "the broker is down" -- and
    the two must remain distinguishable, because they call for different operator actions.
    """
    seed(database, reconciled_at=NOW - timedelta(hours=2), bar_at=NOW - timedelta(hours=20))

    readiness = await measure(database, None)

    assert "BROKER" in readiness.unprobed
    assert "BROKER_UNVERIFIED" in readiness.blocking
    assert not readiness.ready
    # Distinguishable: an unprobed broker must not masquerade as one that answered badly.
    # "restart the broker" and "configure an adapter" are different operator actions.
    assert "BROKER_UNHEALTHY" not in readiness.blocking
    assert any("no broker adapter" in line for line in readiness.detail), readiness.detail


def test_readiness_assembled_by_a_caller_cannot_clear_the_switch(database: Database) -> None:
    """The mechanism, not the convention.

    `ReadinessEvidence` is an ordinary value object and anyone can build one with six `True`s.
    That is precisely how the environment variables got to decide clearance. `clear` takes a
    type only the measuring path can produce.
    """
    hand_built = ReadinessEvidence(
        paper_mode=True,
        account_verified=True,
        broker_healthy=True,
        data_healthy=True,
        risk_healthy=True,
        reconciliation_successful=True,
    )

    with pytest.raises(TypeError, match="not a measurement"):
        KillSwitchController(database).clear(
            reason="looks fine to me",
            measured=hand_built,  # type: ignore[arg-type]
            updated_at=NOW,
        )

    # Nor by constructing the wrapper directly around the same claim.
    with pytest.raises(TypeError, match="cannot be constructed, only measured"):
        MeasuredReadiness(
            evidence=hand_built,
            measured_at=NOW,
            probes=(),
            unprobed=(),
            detail=(),
            token=object(),
        )

    assert KillSwitchController(database).get().engaged is True


def test_only_the_readiness_module_mints_measured_readiness() -> None:
    """The boundary the test helper relies on, checked rather than trusted.

    `issue_measured_readiness` is importable, so test code can mint readiness that was never
    probed -- which keeps unrelated fixtures from having to seed a broker and a reconciliation
    just to reach the case they are about. The production guarantee therefore does not rest on
    the issuer being hard to reach. It rests on this: nothing in `src/` calls it except the
    module that does the probing.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "quantbot"
    callers = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "issue_measured_readiness(" in path.read_text(encoding="utf-8")
        and path.name != "kill_switch.py"
    ]

    assert callers == ["operations/readiness.py"], (
        "only the probing module may mint measured readiness, or the type stops meaning "
        f"'this was measured': {callers}"
    )
