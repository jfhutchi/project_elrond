from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.domain import MarketClock
from quantbot.operations.daemon import DaemonRunner, WakeKind, next_cycle_at
from quantbot.operations.metrics import OperationalMetrics
from quantbot.storage import AlreadyLockedError, SingleWriterLock

NOW = datetime(2026, 8, 14, 14, 0, tzinfo=UTC)


class StopFlag:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped


class FakeClockProvider:
    """A clock that is open by default; pass `is_open=False` for the after-hours path."""

    def __init__(self, *, is_open: bool = True) -> None:
        self.is_open = is_open

    async def get_market_clock(self) -> MarketClock:
        return MarketClock(
            timestamp=NOW,
            is_open=self.is_open,
            next_open=NOW + timedelta(days=3),
            next_close=NOW + timedelta(hours=2),
        )


def test_next_cycle_uses_authoritative_market_close_plus_delay() -> None:
    clock = MarketClock(
        timestamp=NOW,
        is_open=True,
        next_open=NOW + timedelta(days=3),
        next_close=NOW + timedelta(hours=2),
    )

    assert next_cycle_at(clock, NOW) == NOW + timedelta(hours=2, minutes=5)


def test_next_cycle_rejects_a_before_close_schedule() -> None:
    clock = MarketClock(
        timestamp=NOW,
        is_open=True,
        next_open=NOW + timedelta(days=3),
        next_close=NOW + timedelta(hours=2),
    )

    with pytest.raises(ValueError, match="nonnegative"):
        next_cycle_at(clock, NOW, after_close_delay=timedelta(seconds=-1))


@pytest.mark.asyncio
async def test_daemon_runs_sequentially_and_stops_after_current_cycle(tmp_path: Path) -> None:
    """The after-hours path, whose cadence #143 deliberately did not change.

    Closed market: sleep until the next close plus five minutes, then run the trading cycle.
    Exactly what the daemon always did.
    """
    stop = StopFlag()
    calls: list[str] = []

    async def sleeper(delay: Decimal) -> None:
        assert delay == Decimal("7500.0")
        calls.append("sleep")

    async def cycle(_scheduled_at: datetime, kind: WakeKind) -> None:
        assert kind is WakeKind.CYCLE
        calls.append("cycle")
        stop.stopped = True

    metrics = OperationalMetrics()
    runner = DaemonRunner(
        clock_provider=FakeClockProvider(is_open=False),
        cycle=cycle,
        sleeper=sleeper,
        metrics=metrics,
        lock_path=tmp_path / "daemon.lock",
    )

    result = await runner.run(stop, now=lambda: NOW)

    assert result.cycles_completed == 1
    assert result.stopped_cleanly is True
    assert calls == ["sleep", "cycle"]
    assert "quantbot_daemon_cycles_total 1" in metrics.render_prometheus()


@pytest.mark.asyncio
async def test_shutdown_during_wait_prevents_a_new_cycle(tmp_path: Path) -> None:
    stop = StopFlag()
    calls: list[str] = []

    async def sleeper(_delay: Decimal) -> None:
        calls.append("sleep")
        stop.stopped = True

    async def cycle(_scheduled_at: datetime, _kind: WakeKind) -> None:
        calls.append("cycle")

    runner = DaemonRunner(
        clock_provider=FakeClockProvider(),
        cycle=cycle,
        sleeper=sleeper,
        metrics=OperationalMetrics(),
        lock_path=tmp_path / "daemon.lock",
    )

    result = await runner.run(stop, now=lambda: NOW)

    assert result.cycles_completed == 0
    assert calls == ["sleep"]


@pytest.mark.asyncio
async def test_daemon_refuses_a_second_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / "daemon.lock"
    runner = DaemonRunner(
        clock_provider=FakeClockProvider(),
        cycle=lambda _scheduled_at: _completed(),
        sleeper=lambda _delay: _completed(),
        metrics=OperationalMetrics(),
        lock_path=lock_path,
    )
    stop = StopFlag()

    with SingleWriterLock(lock_path), pytest.raises(AlreadyLockedError):
        await runner.run(stop, now=lambda: NOW)


async def _completed() -> None:
    return None


@pytest.mark.asyncio
async def test_an_open_session_wakes_to_observe_rather_than_to_trade(tmp_path: Path) -> None:
    """The #143 change, at the daemon level.

    An open market used to mean sleeping 7500 seconds to the close. It now means a 900-second
    observation wake, and the kind says so -- which is what stops the market-hours pass from
    being routed into the trading cycle.
    """
    stop = StopFlag()
    seen: list[tuple[Decimal, WakeKind]] = []

    async def sleeper(delay: Decimal) -> None:
        seen.append((delay, WakeKind.OBSERVE))

    async def cycle(_scheduled_at: datetime, kind: WakeKind) -> None:
        seen.append((Decimal("0"), kind))
        stop.stopped = True

    runner = DaemonRunner(
        clock_provider=FakeClockProvider(is_open=True),
        cycle=cycle,
        sleeper=sleeper,
        metrics=OperationalMetrics(),
        lock_path=tmp_path / "daemon.lock",
    )

    await runner.run(stop, now=lambda: NOW)

    assert seen[0][0] == Decimal("900.0"), "an open session is observed, not slept through"
    assert seen[1][1] is WakeKind.OBSERVE
