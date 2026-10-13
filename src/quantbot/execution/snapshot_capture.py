"""Bounded stable broker acquisition; never a reconciliation or order authority.

Local response times establish acquisition bounds, not the provider's quote as-of time.
The caller must still run the unchanged comparison against its original durable state,
fill-ledger checks, broker restrictions, clock/market/risk/ownership and kill-switch gates.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, TypeVar

from quantbot.brokers.base import (
    BrokerAccountSnapshot,
    BrokerRequestError,
    BrokerTransportTimeoutError,
)
from quantbot.domain import Position
from quantbot.execution.reconciliation import (
    ReconciliationPolicy,
    ReconciliationSnapshot,
)

T = TypeVar("T")
CAPTURE_BUDGET_SECONDS = 5.0


class ReadOnlyBroker(Protocol):
    async def get_account(self) -> BrokerAccountSnapshot: ...
    async def get_positions(self) -> Sequence[Position]: ...


@dataclass(frozen=True)
class Attempt:
    started_at: datetime
    completed_at: datetime | None = None
    before: BrokerAccountSnapshot | None = None
    after: BrokerAccountSnapshot | None = None
    positions: tuple[Position, ...] | None = None
    response_times: tuple[datetime, ...] = ()
    reason: str | None = None
    error_type: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class Capture:
    status: str
    snapshot: ReconciliationSnapshot | None
    received_at: datetime | None
    attempts: tuple[Attempt, ...]
    provider_as_of_verified: bool = False
    transient_failure: bool = False

    def evidence(self) -> dict[str, object]:
        """Private durable diagnostics; raw provider exceptions are never retained."""
        return {
            "status": self.status,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "provider_as_of_verified": False,
            "transient_failure": self.transient_failure,
            "attempts": [
                {
                    "started_at": a.started_at.isoformat(),
                    "completed_at": a.completed_at.isoformat() if a.completed_at else None,
                    "response_times": [t.isoformat() for t in a.response_times],
                    "before": a.before.model_dump(mode="json") if a.before else None,
                    "after": a.after.model_dump(mode="json") if a.after else None,
                    "positions": [p.model_dump(mode="json") for p in a.positions]
                    if a.positions is not None
                    else None,
                    "reason": a.reason,
                    "error_type": a.error_type,
                    "http_status": a.http_status,
                }
                for a in self.attempts
            ],
        }

    def diagnostic_snapshot(self) -> ReconciliationSnapshot | None:
        """A stable rejected candidate may classify real diffs, never authorize entry."""
        if self.snapshot is not None:
            return self.snapshot
        if not self.attempts:
            return None
        last = self.attempts[-1]
        if last.before is None or last.after != last.before or last.positions is None:
            return None
        try:
            return ReconciliationSnapshot(account=last.before.account, positions=last.positions)
        except ValueError:
            return None


def validate_capture_freshness(
    capture: Capture, *, now: datetime, clock_healthy: bool
) -> str | None:
    if not clock_healthy:
        return "CLOCK_UNVERIFIED"
    if capture.status != "ACQUIRED" or capture.received_at is None:
        return "ACQUISITION_UNAVAILABLE"
    if now.tzinfo is None or capture.received_at.tzinfo is None:
        return "CLOCK_UNVERIFIED"
    age = (now - capture.received_at).total_seconds()
    if age < 0:
        return "CLOCK_REVERSED"
    if age > 120:
        return "STALE_CAPTURE"
    return None


async def capture_account_positions(
    broker: ReadOnlyBroker,
    *,
    clock: Callable[[], datetime],
    clock_healthy: Callable[[datetime], bool],
    max_attempts: int = 2,
    total_timeout_seconds: float = 5,
) -> Capture:
    if (
        type(max_attempts) is not int
        or not 1 <= max_attempts <= 2
        or not math.isfinite(total_timeout_seconds)
        or not 0 < total_timeout_seconds <= 5
    ):
        raise ValueError("maximum two attempts and five seconds total")
    attempts: list[Attempt] = []
    transient_failure = False
    reading = False

    def stopped(status: str) -> Capture:
        return Capture(status, None, None, tuple(attempts), transient_failure=transient_failure)

    async def acquire() -> Capture:
        for _ in range(max_attempts):
            start = clock()
            if not clock_healthy(start):
                return stopped("CLOCK_UNVERIFIED")
            attempts.append(Attempt(start))
            last = start

            async def read(
                request: Callable[[], Awaitable[T]], stage: str
            ) -> tuple[T | None, str | None]:
                nonlocal last, transient_failure, reading
                try:
                    reading = True
                    value = await request()
                except Exception as exc:
                    # Failed data acquisition only: never ambiguous transport errors,
                    # schema failures or a timeout following an unstable first sample.
                    transient_failure = (
                        isinstance(exc, (TimeoutError, BrokerTransportTimeoutError))
                        or (
                            isinstance(exc, BrokerRequestError)
                            and exc.status_code is not None
                            and (exc.status_code == 429 or 500 <= exc.status_code <= 599)
                        )
                    ) and not any(a.reason for a in attempts)
                    attempts[-1] = replace(
                        attempts[-1],
                        reason=stage + "_READ_FAILED",
                        error_type=type(exc).__name__,
                        http_status=exc.status_code
                        if isinstance(exc, BrokerRequestError)
                        else None,
                    )
                    return None, "ACQUISITION_FAILED"
                finally:
                    # Cancellation by the capture budget deliberately leaves this set.
                    task = asyncio.current_task()
                    if task is None or not task.cancelling():
                        reading = False
                received = clock()
                attempts[-1] = replace(
                    attempts[-1],
                    completed_at=received,
                    response_times=(*attempts[-1].response_times, received),
                )
                if not clock_healthy(received):
                    return None, "CLOCK_UNVERIFIED"
                if received < last:
                    return None, "CLOCK_REVERSED"
                last = received
                return value, None

            before, error = await read(broker.get_account, "ACCOUNT")
            if error:
                return stopped(error)
            assert before is not None
            attempts[-1] = replace(attempts[-1], before=before)
            positions, error = await read(broker.get_positions, "POSITION")
            if error:
                return stopped(error)
            assert positions is not None
            attempts[-1] = replace(attempts[-1], positions=tuple(positions))
            after, error = await read(broker.get_account, "ACCOUNT")
            if error:
                return stopped(error)
            assert after is not None
            attempts[-1] = replace(attempts[-1], after=after)
            if before != after:
                attempts[-1] = replace(attempts[-1], reason="ACCOUNT_CHANGED_DURING_CAPTURE")
                continue
            try:
                snapshot = ReconciliationSnapshot(account=after.account, positions=tuple(positions))
            except ValueError:
                attempts[-1] = replace(attempts[-1], reason="MALFORMED_SNAPSHOT")
                return stopped("ACQUISITION_FAILED")
            tolerance = ReconciliationPolicy().money_tolerance
            marked = sum((p.market_value for p in snapshot.positions), snapshot.account.cash)
            if abs(snapshot.account.equity - marked) >= tolerance or any(
                p.quantity == 0
                or p.quantity * p.market_value <= 0
                or abs(p.market_value - p.quantity * p.market_price) >= tolerance
                for p in snapshot.positions
            ):
                attempts[-1] = replace(attempts[-1], reason="INCONSISTENT_EQUITY_MARKS")
                continue
            return Capture("ACQUIRED", snapshot, last, tuple(attempts))
        return stopped("ACQUISITION_UNSTABLE")

    try:
        return await asyncio.wait_for(acquire(), timeout=total_timeout_seconds)
    except TimeoutError:
        transient_failure = reading and not any(a.reason for a in attempts)
        if attempts:
            attempts[-1] = replace(attempts[-1], reason="TOTAL_CAPTURE_TIMEOUT")
        return stopped("ACQUISITION_TIMEOUT")
