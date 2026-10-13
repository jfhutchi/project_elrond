"""Versioned PAPER relative-clock evidence. No clock writes or LIVE authority.

Provider wall time is retained unchanged. A bounded request bracket establishes
relative consistency, not UTC accuracy. Price/feature/fill gates remain separate.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Literal, Protocol
from uuid import uuid4

from quantbot.domain import BrokerEnvironment, MarketClock

ClockContract = Literal["STRICT_V0", "RELATIVE_V1"]
MAX_RELATIVE_SKEW_SECONDS = 2.0
ACQUISITION_BUDGET_SECONDS = 5.0
MAX_EVIDENCE_AGE_SECONDS = 120.0
MAX_SYNC_AGE_SECONDS = 2048.0  # Two existing 1024s poll periods, with no OS setting change.
WALL_MONOTONIC_CONTINUITY_SECONDS = 0.05
EXPECTED_SYNC_SOURCE = "time.windows.com,0x9"
_PROCESS_ID = str(uuid4())


@dataclass(frozen=True)
class SynchronizationHealth:
    checked_at: datetime
    last_sync_at: datetime
    leap_indicator: int
    stratum: int
    last_sync_error: int
    poll_seconds: int
    source: str


@dataclass(frozen=True)
class ClockSample:
    wall_before: datetime
    wall_after: datetime
    monotonic_before: float
    monotonic_after: float
    market: MarketClock


@dataclass(frozen=True)
class ClockEvidence:
    market: MarketClock
    checked_at: datetime
    monotonic_at: float
    offset_lower_seconds: float
    offset_upper_seconds: float
    process_id: str
    samples: tuple[ClockSample, ...]
    synchronization: SynchronizationHealth
    version: Literal["RELATIVE_V1"] = "RELATIVE_V1"

    def valid_at(self, now: datetime, *, monotonic_now: float) -> bool:
        return (
            self.process_id == _PROCESS_ID
            and _aware(now)
            and math.isfinite(monotonic_now)
            and 0
            <= (now - self.synchronization.last_sync_at).total_seconds()
            <= min(MAX_SYNC_AGE_SECONDS, 2 * self.synchronization.poll_seconds)
            and 0 <= (now - self.checked_at).total_seconds() <= MAX_EVIDENCE_AGE_SECONDS
            and 0 <= monotonic_now - self.monotonic_at <= MAX_EVIDENCE_AGE_SECONDS
            and abs((now - self.checked_at).total_seconds() - (monotonic_now - self.monotonic_at))
            <= WALL_MONOTONIC_CONTINUITY_SECONDS
        )

    def time_bounds(self, local_at: datetime) -> tuple[datetime, datetime]:
        return (
            local_at + timedelta(seconds=self.offset_lower_seconds),
            local_at + timedelta(seconds=self.offset_upper_seconds),
        )

    def audit(self) -> dict[str, object]:
        return {
            "version": self.version,
            "checked_at": self.checked_at.isoformat(),
            "broker_timestamp": self.market.timestamp.isoformat(),
            "offset_lower_seconds": self.offset_lower_seconds,
            "offset_upper_seconds": self.offset_upper_seconds,
            "sync_checked_at": self.synchronization.checked_at.isoformat(),
            "last_sync_at": self.synchronization.last_sync_at.isoformat(),
            "leap_indicator": self.synchronization.leap_indicator,
            "stratum": self.synchronization.stratum,
            "sync_source": self.synchronization.source,
            "last_sync_error": self.synchronization.last_sync_error,
            "poll_seconds": self.synchronization.poll_seconds,
            "is_open": self.market.is_open,
            "next_open": self.market.next_open.isoformat(),
            "next_close": self.market.next_close.isoformat(),
            "samples": [
                {
                    "before": s.wall_before.isoformat(),
                    "after": s.wall_after.isoformat(),
                    "broker_timestamp": s.market.timestamp.isoformat(),
                    "request_seconds": s.monotonic_after - s.monotonic_before,
                }
                for s in self.samples
            ],
        }


@dataclass(frozen=True)
class ClockCheck:
    evidence: ClockEvidence | None
    reasons: tuple[str, ...]


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def assess_paper_clock(
    samples: tuple[ClockSample, ...],
    health: SynchronizationHealth | None,
    *,
    now: datetime,
    monotonic_now: float,
    started_monotonic: float,
) -> ClockCheck:
    def refused(code: str) -> ClockCheck:
        return ClockCheck(None, (code,))

    if not _aware(now) or not all(math.isfinite(v) for v in (monotonic_now, started_monotonic)):
        return refused("PAPER_CLOCK_UNVERIFIED")
    if not 0 <= monotonic_now - started_monotonic <= ACQUISITION_BUDGET_SECONDS:
        return refused("PAPER_CLOCK_ACQUISITION_TIMEOUT")
    if (
        health is None
        or not _aware(health.checked_at)
        or not _aware(health.last_sync_at)
        or health.last_sync_at > health.checked_at
        or health.leap_indicator != 0
        or not 1 <= health.stratum <= 15
        or health.last_sync_error != 0
        or health.source.casefold() != EXPECTED_SYNC_SOURCE.casefold()
        or not 1 <= health.poll_seconds <= 1024
        or not 0 <= (now - health.checked_at).total_seconds() <= ACQUISITION_BUDGET_SECONDS
        or not 0
        <= (now - health.last_sync_at).total_seconds()
        <= min(MAX_SYNC_AGE_SECONDS, 2 * health.poll_seconds)
    ):
        return refused("PAPER_CLOCK_SYNC_UNVERIFIED")
    if len(samples) != 2:
        return refused("PAPER_CLOCK_SAMPLES_UNAVAILABLE")
    lower, upper = -MAX_RELATIVE_SKEW_SECONDS, MAX_RELATIVE_SKEW_SECONDS
    prior: ClockSample | None = None
    for sample in samples:
        if not all(
            _aware(value)
            for value in (
                sample.wall_before,
                sample.wall_after,
                sample.market.timestamp,
                sample.market.next_open,
                sample.market.next_close,
            )
        ):
            return refused("PAPER_CLOCK_UNVERIFIED")
        if not all(math.isfinite(v) for v in (sample.monotonic_before, sample.monotonic_after)):
            return refused("PAPER_CLOCK_UNVERIFIED")
        duration = sample.monotonic_after - sample.monotonic_before
        wall_duration = (sample.wall_after - sample.wall_before).total_seconds()
        if (
            not started_monotonic
            <= sample.monotonic_before
            <= sample.monotonic_after
            <= monotonic_now
            or sample.wall_after > now
            or wall_duration < 0
            or abs(wall_duration - duration) > WALL_MONOTONIC_CONTINUITY_SECONDS
        ):
            return refused("PAPER_CLOCK_DISCONTINUITY")
        sample_lower = (sample.market.timestamp - sample.wall_after).total_seconds()
        sample_upper = (sample.market.timestamp - sample.wall_before).total_seconds()
        if (
            not -MAX_RELATIVE_SKEW_SECONDS
            <= sample_lower
            <= sample_upper
            <= MAX_RELATIVE_SKEW_SECONDS
        ):
            return refused("PAPER_CLOCK_RELATIVE_SKEW_EXCEEDED")
        lower, upper = max(lower, sample_lower), min(upper, sample_upper)
        if lower > upper:
            return refused("PAPER_CLOCK_SAMPLES_INCONSISTENT")
        if prior is not None and (
            sample.monotonic_before < prior.monotonic_after
            or sample.wall_before < prior.wall_after
            or sample.market.timestamp <= prior.market.timestamp
            or sample.market.is_open != prior.market.is_open
            or sample.market.next_open != prior.market.next_open
            or sample.market.next_close != prior.market.next_close
        ):
            return refused("PAPER_CLOCK_SAMPLES_INCONSISTENT")
        prior = sample
    return ClockCheck(
        ClockEvidence(
            samples[-1].market, now, monotonic_now, lower, upper, _PROCESS_ID, samples, health
        ),
        (),
    )


def parse_windows_sync_status(
    output: str, *, checked_at: datetime, local_timezone: tzinfo
) -> SynchronizationHealth:
    fields = {
        line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
        for line in output.splitlines()
        if ":" in line
    }

    def number(name: str) -> int:
        match = re.match(r"^(0[xX][0-9a-fA-F]+|\d+)(?=\s|\(|$)", fields.get(name, ""))
        if match is None:
            raise ValueError("PAPER_CLOCK_SYNC_UNVERIFIED")
        token = match[1]
        return int(token, 16 if token.lower().startswith("0x") else 10)

    last = datetime.strptime(fields.get("Last Successful Sync Time", ""), "%m/%d/%Y %I:%M:%S %p")
    exponent = number("Poll Interval")
    if exponent > 10:
        raise ValueError("PAPER_CLOCK_SYNC_UNVERIFIED")
    return SynchronizationHealth(
        checked_at,
        last.replace(tzinfo=local_timezone).astimezone(UTC),
        number("Leap Indicator"),
        number("Stratum"),
        number("Last Sync Error"),
        2**exponent,
        fields["Source"],
    )


class SynchronizationReader(Protocol):
    async def read(self) -> SynchronizationHealth | None: ...


class WindowsSynchronizationReader:
    """Ordinary-token query only. Denial becomes unknown; no elevation or resync."""

    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.clock = clock

    def _read(self) -> SynchronizationHealth | None:
        if os.name != "nt":
            return None
        try:
            result = subprocess.run(
                ["w32tm.exe", "/query", "/status", "/verbose"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode or len(result.stdout) > 16384:
                return None
            timezone = datetime.now().astimezone().tzinfo
            assert timezone is not None
            return parse_windows_sync_status(
                result.stdout, checked_at=self.clock(), local_timezone=timezone
            )
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
            return None

    async def read(self) -> SynchronizationHealth | None:
        return await asyncio.to_thread(self._read)


class PaperClockBroker(Protocol):
    @property
    def environment(self) -> str: ...
    async def get_market_clock(self) -> MarketClock: ...


class ClockVerifier(Protocol):
    monotonic: Callable[[], float]

    async def verify(self) -> ClockCheck: ...


class PaperClockProbe:
    def __init__(
        self,
        broker: PaperClockBroker,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        synchronization: SynchronizationReader | None = None,
    ) -> None:
        self.broker, self.clock, self.monotonic = broker, clock, monotonic
        self.synchronization = synchronization or WindowsSynchronizationReader(clock=clock)

    async def _collect(self, started: float) -> ClockCheck:
        health = await self.synchronization.read()
        if health is None:
            return ClockCheck(None, ("PAPER_CLOCK_SYNC_UNVERIFIED",))
        samples = []
        for _ in range(2):
            wall_before = self.clock()
            mono_before = self.monotonic()
            market = await self.broker.get_market_clock()
            mono_after = self.monotonic()
            wall_after = self.clock()
            samples.append(ClockSample(wall_before, wall_after, mono_before, mono_after, market))
        return assess_paper_clock(
            tuple(samples),
            health,
            now=self.clock(),
            monotonic_now=self.monotonic(),
            started_monotonic=started,
        )

    async def verify(self) -> ClockCheck:
        if self.broker.environment != BrokerEnvironment.PAPER.value:
            return ClockCheck(None, ("PAPER_CLOCK_MODE_REQUIRED",))
        started = self.monotonic()
        try:
            async with asyncio.timeout(ACQUISITION_BUDGET_SECONDS):
                return await self._collect(started)
        except TimeoutError:
            return ClockCheck(None, ("PAPER_CLOCK_ACQUISITION_TIMEOUT",))
        except Exception:
            return ClockCheck(None, ("PAPER_CLOCK_PROVIDER_UNAVAILABLE",))
