"""Watching a session without trading in it (#143).

The failure this exists to prevent is in the operator's own store: a cycle halted 2026-08-19, the
kill switch was cleared 2026-08-20 after verifying the condition had gone, and nothing ran for
the ten days after. `incidents` recorded nothing, because a system that has stopped produces no
problems -- so ten days of silence and ten days of health looked identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from quantbot.domain import MarketClock
from quantbot.operations.cycle import DataSyncResult
from quantbot.operations.daemon import (
    OBSERVATION_INTERVAL,
    WakeKind,
    next_wake,
)
from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.observation import (
    ObservationOutcome,
    SessionObserver,
    observation_id,
)
from quantbot.storage import Database
from quantbot.storage.schema import session_observations

NOW = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path) -> Database:  # type: ignore[no-untyped-def]
    """A fresh store at the current schema. `create` is the only route that bootstraps one."""
    with Database.create(tmp_path / "observations.db") as store:
        yield store


@dataclass
class FakeRecovery:
    ready: bool = True
    reasons: tuple[str, ...] = ()
    calls: int = 0

    async def recover(self, reconciliation_id: str, *, started_at: datetime, run_id=None):  # type: ignore[no-untyped-def]
        self.calls += 1

        @dataclass(frozen=True)
        class _Result:
            ready: bool
            reasons: tuple[str, ...]

        return _Result(self.ready, self.reasons)


@dataclass
class FakeDataSync:
    completed_through: datetime = NOW
    healthy: bool = True
    stream_connected: bool = True
    risk_healthy: bool = True
    stale_symbols: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    bars_stored: int = 0

    async def sync(self) -> DataSyncResult:
        return DataSyncResult(
            completed_through=self.completed_through,
            bars_stored=self.bars_stored,
            healthy=self.healthy,
            stream_connected=self.stream_connected,
            risk_healthy=self.risk_healthy,
            stale_symbols=self.stale_symbols,
            reasons=self.reasons,
        )


@dataclass
class FakeKillSwitch:
    engaged: bool = False

    def get(self) -> object:
        return self


def observer(database, **kwargs) -> SessionObserver:  # type: ignore[no-untyped-def]
    return SessionObserver(
        database=database,
        recovery=kwargs.pop("recovery", FakeRecovery()),
        data_sync=kwargs.pop("data_sync", FakeDataSync()),
        kill_switch=kwargs.pop("kill_switch", FakeKillSwitch()),
        metrics=kwargs.pop("metrics", OperationalMetrics()),
        max_data_age_seconds=kwargs.pop("max_data_age_seconds", 86_400),
    )


def stored(database) -> list[tuple[str, str, str]]:  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        return [
            (row.observation_id, row.outcome, row.reasons)
            for row in session.execute(
                select(
                    session_observations.c.observation_id,
                    session_observations.c.outcome,
                    session_observations.c.reasons,
                ).order_by(session_observations.c.observed_at)
            )
        ]


# --- the heartbeat itself ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_healthy_pass_is_recorded_too(database: Database) -> None:
    """The whole point. A monitor that only records problems cannot tell silence from health.

    That is exactly what happened: ten days with no incidents, because a stopped system raises
    none. If this test is deleted and healthy rows stop being written, the table becomes
    `incidents` with extra steps and the outage it exists to reveal becomes invisible again.
    """
    result = await observer(database).observe(now=NOW, market_open=True)

    assert result.outcome is ObservationOutcome.HEALTHY
    assert result.reasons == ()
    assert stored(database) == [(observation_id(NOW), "HEALTHY", "")]


@pytest.mark.asyncio
async def test_a_failing_check_is_recorded_with_its_reason(database: Database) -> None:
    result = await observer(
        database,
        recovery=FakeRecovery(ready=False, reasons=("POSITION_LEDGER_MISMATCH",)),
    ).observe(now=NOW, market_open=True)

    assert result.outcome is ObservationOutcome.DEGRADED
    assert result.reasons == ("POSITION_LEDGER_MISMATCH",)
    assert stored(database) == [
        (observation_id(NOW), "DEGRADED", "POSITION_LEDGER_MISMATCH")
    ]


@pytest.mark.asyncio
async def test_an_engaged_kill_switch_is_halted_not_degraded(database: Database) -> None:
    """A deliberate stop is not a fault, and conflating them is dangerous rather than untidy.

    An operator who sees a kill switch reported as a fault is being invited to clear it to make
    the alert go away -- which is the one thing CLAUDE.md forbids outright.
    """
    recovery = FakeRecovery()
    result = await observer(
        database, kill_switch=FakeKillSwitch(engaged=True), recovery=recovery
    ).observe(now=NOW, market_open=True)

    assert result.outcome is ObservationOutcome.HALTED
    assert result.reasons == ("KILL_SWITCH_ENGAGED",)
    assert recovery.calls == 0, (
        "a halted system is not expected to be reconciled; running the check anyway piles "
        "derived faults on top of the one fact that matters"
    )


@pytest.mark.asyncio
async def test_stale_market_data_is_a_reason(database: Database) -> None:
    result = await observer(
        database,
        data_sync=FakeDataSync(completed_through=NOW - timedelta(days=3)),
        max_data_age_seconds=3600,
    ).observe(now=NOW, market_open=True)

    assert result.outcome is ObservationOutcome.DEGRADED
    assert "MARKET_DATA_STALE" in result.reasons
    assert result.market_data_age_seconds == Decimal("259200.0")


@pytest.mark.asyncio
async def test_the_same_instant_records_once(database: Database) -> None:
    """Restart must not turn one pass into two.

    #143 requires that recovery not reinterpret an old event as a new independent one. The id is
    content-addressed from the instant precisely so a restarted daemon re-deriving it collides
    instead of appending.
    """
    watcher = observer(database)
    await watcher.observe(now=NOW, market_open=True)
    await watcher.observe(now=NOW, market_open=True)

    assert len(stored(database)) == 1


@pytest.mark.asyncio
async def test_the_observer_has_no_route_to_an_order(database: Database) -> None:
    """The safety property, asserted structurally rather than behaviourally.

    "It did not submit an order" is a claim about one run. "It has no submitter" is a claim about
    every run, including ones a future edit adds. `RunOnceCycle` takes `submitter` and
    `strategy`; this takes neither, and if that changes this test fails.
    """
    watcher = observer(database)
    fields = set(SessionObserver.__dataclass_fields__)

    assert "submitter" not in fields
    assert "strategy" not in fields
    assert not hasattr(watcher, "_submitter")
    assert not any("submit" in name.lower() for name in dir(watcher))


def test_a_naive_timestamp_is_refused(database: Database) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        import asyncio

        asyncio.run(
            observer(database).observe(
                now=datetime(2026, 8, 30, 15, 0), market_open=True
            )
        )


# --- the wake schedule ---------------------------------------------------------------------


def clock(*, is_open: bool, close_in: timedelta) -> MarketClock:
    return MarketClock(
        timestamp=NOW,
        is_open=is_open,
        next_open=NOW + timedelta(hours=18),
        next_close=NOW + close_in,
    )


def test_a_closed_market_behaves_exactly_as_before() -> None:
    """A daemon started overnight must schedule the after-close cycle, as it always did."""
    wake = next_wake(clock(is_open=False, close_in=timedelta(hours=20)), NOW)

    assert wake.kind is WakeKind.CYCLE
    assert wake.at == NOW + timedelta(hours=20, minutes=5)


def test_an_open_session_is_observed_on_the_interval() -> None:
    """The change. Before this, an open market meant sleeping until after the close."""
    wake = next_wake(clock(is_open=True, close_in=timedelta(hours=4)), NOW)

    assert wake.kind is WakeKind.OBSERVE
    assert wake.at == NOW + OBSERVATION_INTERVAL


def test_the_close_always_wins_the_comparison() -> None:
    """An observation can never displace the trading cycle or push it past its slot.

    With five minutes of session left, the next thing to do is the cycle -- not an observation
    fifteen minutes from now, which would land after the close it was supposed to precede.
    """
    wake = next_wake(clock(is_open=True, close_in=timedelta(minutes=5)), NOW)

    assert wake.kind is WakeKind.CYCLE
    assert wake.at == NOW + timedelta(minutes=10)


def test_the_trading_cadence_is_unchanged_by_this_issue() -> None:
    """The deployed strategy was qualified on daily bars evaluated after the close.

    Evaluating it every fifteen minutes would not be the same strategy watched more closely; it
    would be a different strategy carrying forward qualification evidence it never earned. So an
    open session produces `OBSERVE` wakes and the `CYCLE` wake stays exactly where it was.
    """
    open_session = next_wake(clock(is_open=True, close_in=timedelta(hours=6)), NOW)
    closed = next_wake(clock(is_open=False, close_in=timedelta(hours=6)), NOW)

    assert open_session.kind is WakeKind.OBSERVE
    assert closed.kind is WakeKind.CYCLE
    assert closed.at == NOW + timedelta(hours=6, minutes=5)


def test_a_nonpositive_interval_is_refused() -> None:
    """A zero interval is a busy loop against a broker API."""
    with pytest.raises(ValueError, match="observation_interval must be positive"):
        next_wake(
            clock(is_open=True, close_in=timedelta(hours=4)),
            NOW,
            observation_interval=timedelta(0),
        )
