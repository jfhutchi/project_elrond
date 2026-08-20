"""Sequential broker-calendar daemon loop with cooperative shutdown."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from quantbot.domain import MarketClock
from quantbot.operations.health import OperationsModel
from quantbot.operations.metrics import OperationalMetrics
from quantbot.storage import SingleWriterLock


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


class ClockProvider(Protocol):
    async def get_market_clock(self) -> MarketClock: ...


DaemonSleeper = Callable[[Decimal], Awaitable[None]]
DaemonCycle = Callable[[datetime], Awaitable[object]]


class DaemonResult(OperationsModel):
    cycles_completed: int
    stopped_cleanly: bool


def next_cycle_at(
    clock: MarketClock,
    now: datetime,
    *,
    after_close_delay: timedelta = timedelta(minutes=5),
) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if after_close_delay < timedelta(0):
        raise ValueError("after_close_delay must be nonnegative")
    normalized = now.astimezone(UTC)
    target = clock.next_close + after_close_delay
    if target <= normalized:
        raise ValueError("broker market clock does not provide a future close")
    return target


class DaemonRunner:
    """Runs one awaited cycle at a time; it never creates worker/background tasks."""

    def __init__(
        self,
        *,
        clock_provider: ClockProvider,
        cycle: DaemonCycle,
        sleeper: DaemonSleeper,
        metrics: OperationalMetrics,
        lock_path: str | Path,
    ) -> None:
        """`lock_path` must NOT be the writer lock.

        Two different exclusions are needed and conflating them breaks both. Only one
        scheduler should run, which is what this lock enforces for the daemon's whole
        lifetime. Only one writer should touch the ledger at a time, which the cycle
        enforces for its own duration. Holding the writer lock across the daemon's sleep
        made every cycle fail on a non-reentrant re-acquisition, and blocked manual
        run-once and reconcile for as long as the daemon lived.
        """
        self._clock_provider = clock_provider
        self._cycle = cycle
        self._sleeper = sleeper
        self._metrics = metrics
        self._lock_path = Path(lock_path)

    async def run(self, stop: StopSignal, *, now: Callable[[], datetime]) -> DaemonResult:
        completed = 0
        with SingleWriterLock(self._lock_path):
            while not stop.is_set():
                clock = await self._clock_provider.get_market_clock()
                current = now()
                target = next_cycle_at(clock, current)
                delay = Decimal(str((target - current.astimezone(UTC)).total_seconds()))
                await self._sleeper(delay)
                if stop.is_set():
                    break
                await self._cycle(target)
                completed += 1
                self._metrics.increment("quantbot_daemon_cycles_total")
        return DaemonResult(cycles_completed=completed, stopped_cleanly=True)
