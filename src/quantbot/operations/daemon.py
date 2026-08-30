"""Sequential broker-calendar daemon loop with cooperative shutdown."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
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


class WakeKind(StrEnum):
    """What the daemon woke up to do."""

    #: A market-hours pass over the authoritative safety checks. Cannot trade -- see
    #: `operations/observation.py`, where the inability is structural rather than a flag.
    OBSERVE = "OBSERVE"
    #: The after-close cycle that evaluates the strategy and may submit orders. Its cadence is
    #: deliberately unchanged: the deployed strategy was qualified on daily bars evaluated after
    #: the close, and evaluating it intraday would be a different strategy carrying forward
    #: qualification evidence it never earned.
    CYCLE = "CYCLE"


DaemonCycle = Callable[[datetime, WakeKind], Awaitable[object]]

#: How often the daemon looks at a live session.
#:
#: `PROJECT_DEFAULT` v1, chosen 2026-08-30 under CLAUDE.md's rule that an unspecified operator
#: preference is not a blocker. Not an operator ratification.
#:
#: The tiebreak did not resolve it from an existing value, so the reasoning is recorded instead.
#: `DEFAULT_MAX_DATA_AGE_SECONDS` is 86,400 -- a bound on daily bars, not an intraday one, so
#: sampling against it would give a twelve-hour interval and detect nothing within a session. 15
#: minutes gives 26 passes over a 6.5-hour session at a handful of read-only API calls each,
#: which is far below any broker rate limit and fine enough that an outage is visible within the
#: session rather than after it. Lower costs calls for resolution nothing needs; much higher and
#: a session can end before anything notices it went wrong.
OBSERVATION_INTERVAL = timedelta(minutes=15)


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


@dataclass(frozen=True, slots=True)
class Wake:
    """When the daemon should next act, and which act it is."""

    at: datetime
    kind: WakeKind


def next_wake(
    clock: MarketClock,
    now: datetime,
    *,
    after_close_delay: timedelta = timedelta(minutes=5),
    observation_interval: timedelta = OBSERVATION_INTERVAL,
) -> Wake:
    """The next thing to do: watch the open session, or run the after-close cycle.

    While the market is closed this is exactly the old behaviour -- sleep until after the next
    close. That is deliberate: the trading cycle's cadence does not change, and a daemon started
    overnight behaves as it always did.

    While the market is open the daemon wakes at `observation_interval` instead, unless the close
    arrives first. The close always wins the comparison, so an observation can never displace the
    cycle or push it past its slot.
    """
    if observation_interval <= timedelta(0):
        raise ValueError("observation_interval must be positive")
    close_target = next_cycle_at(clock, now, after_close_delay=after_close_delay)
    if not clock.is_open:
        return Wake(at=close_target, kind=WakeKind.CYCLE)
    observe_at = now.astimezone(UTC) + observation_interval
    if observe_at < close_target:
        return Wake(at=observe_at, kind=WakeKind.OBSERVE)
    return Wake(at=close_target, kind=WakeKind.CYCLE)


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
                wake = next_wake(clock, current)
                delay = Decimal(str((wake.at - current.astimezone(UTC)).total_seconds()))
                await self._sleeper(delay)
                if stop.is_set():
                    break
                await self._cycle(wake.at, wake.kind)
                completed += 1
                self._metrics.increment("quantbot_daemon_cycles_total")
        return DaemonResult(cycles_completed=completed, stopped_cleanly=True)
