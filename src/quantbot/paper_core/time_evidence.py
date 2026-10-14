"""PAPER_TIME_V2 relative provider proof; Windows text grants no authority."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from quantbot.brokers.base import (
    BrokerModel,
    BrokerProtocolError,
    BrokerRequestError,
    BrokerTransportError,
)
from quantbot.paper_core.contracts import (
    CoreConfig,
    Evidence,
    Observation,
    ProtectionPlan,
    TimeProjection,
)


class Clocks(Protocol):
    process_id: str

    def sample(self) -> tuple[datetime, float]: ...


class SystemClocks:
    def __init__(self) -> None:
        self.process_id = str(uuid4())

    def sample(self) -> tuple[datetime, float]:
        return datetime.now(UTC), time.monotonic()


class BrokerTime(BrokerModel):
    timestamp: datetime
    account_id: str
    provider: str
    session_id: str
    session_open: datetime
    session_close: datetime
    resolution_seconds: float


class TimeBroker(Protocol):
    account_id: str
    provider: str

    async def get_clock(self) -> BrokerTime: ...


class TimeSample(BrokerModel):
    provider: BrokerTime
    wall_start: datetime
    wall_end: datetime
    mono_start: float
    mono_end: float
    offset_lower: float
    offset_upper: float


class TimeEvidence(Evidence):
    contract: str = "PAPER_TIME_V2"
    process_id: str
    samples: tuple[TimeSample, ...]
    acquired_monotonic: float
    wall_anchor: datetime
    offset_lower: float
    offset_upper: float
    objections: tuple[str, ...]


async def acquire_time_evidence(broker: TimeBroker, clocks: Clocks) -> TimeEvidence:
    start_wall, start_mono = clocks.sample()
    samples: list[TimeSample] = []
    reasons: list[str] = []
    try:
        async with asyncio.timeout(5):
            for _ in range(2):
                w0, m0 = clocks.sample()
                p = await broker.get_clock()
                w1, m1 = clocks.sample()
                samples.append(
                    TimeSample(
                        provider=p,
                        wall_start=w0,
                        wall_end=w1,
                        mono_start=m0,
                        mono_end=m1,
                        offset_lower=(p.timestamp - w1).total_seconds(),
                        offset_upper=(p.timestamp - w0).total_seconds(),
                    )
                )
    except (TimeoutError, OSError, BrokerRequestError, BrokerTransportError):
        reasons.append("TIME_ACQUISITION_RETRYABLE")
    except (ValueError, TypeError, AttributeError, BrokerProtocolError):
        reasons.append("TIME_PROTOCOL_INVALID")
    wall, mono = clocks.sample()
    if mono < start_mono or mono - start_mono > 5:
        reasons.append("TIME_ACQUISITION_BUDGET")
    if abs((wall - start_wall).total_seconds() - (mono - start_mono)) > 0.050000001:
        reasons.append("TIME_DISCONTINUITY")
    for sample in samples:
        p = sample.provider
        if (
            sample.mono_end < sample.mono_start
            or abs(
                (sample.wall_end - sample.wall_start).total_seconds()
                - (sample.mono_end - sample.mono_start)
            )
            > 0.050000001
        ):
            reasons.append("TIME_DISCONTINUITY")
        if sample.offset_lower < -2 or sample.offset_upper > 2:
            reasons.append("TIME_SKEW_OUT_OF_BOUNDS")
        if (
            p.account_id != broker.account_id
            or p.provider != broker.provider
            or not p.session_open < p.session_close
            or not math.isfinite(p.resolution_seconds)
            or p.resolution_seconds <= 0
        ):
            reasons.append("TIME_PROTOCOL_INVALID")
    if len(samples) != 2:
        reasons.append("TIME_SAMPLES_INCOMPLETE")
    else:
        first, second = samples
        if second.provider.timestamp <= first.provider.timestamp:
            reasons.append("TIME_SAMPLES_NOT_ADVANCING")
        if (
            first.provider.account_id,
            first.provider.provider,
            first.provider.session_id,
            first.provider.session_open,
            first.provider.session_close,
        ) != (
            second.provider.account_id,
            second.provider.provider,
            second.provider.session_id,
            second.provider.session_open,
            second.provider.session_close,
        ):
            reasons.append("TIME_IDENTITY_CHANGED")
        if max(s.offset_lower for s in samples) > min(s.offset_upper for s in samples):
            reasons.append("TIME_SAMPLES_DISAGREE")
    return TimeEvidence(
        recorded_at_utc=wall,
        process_id=clocks.process_id,
        samples=tuple(samples),
        acquired_monotonic=mono,
        wall_anchor=wall,
        offset_lower=min((s.offset_lower for s in samples), default=0),
        offset_upper=max((s.offset_upper for s in samples), default=0),
        objections=tuple(dict.fromkeys(reasons)),
    )


def _projection_at(
    evidence: TimeEvidence, *, wall: datetime, mono: float, process_id: str
) -> TimeProjection:
    reasons = list(evidence.objections)
    if process_id != evidence.process_id:
        reasons.append("TIME_PROCESS_CHANGED")
    age = mono - evidence.acquired_monotonic
    if age < 0 or age > 120:
        reasons.append("TIME_EVIDENCE_EXPIRED")
    if abs((wall - evidence.wall_anchor).total_seconds() - age) > 0.050000001:
        reasons.append("TIME_DISCONTINUITY")
    return TimeProjection(
        evidence_id=evidence.id,
        process_id=process_id,
        wall_at=wall,
        monotonic_at=mono,
        provider_earliest=wall + timedelta(seconds=evidence.offset_lower),
        provider_latest=wall + timedelta(seconds=evidence.offset_upper),
        objections=tuple(dict.fromkeys(reasons)),
    )


def project_time_evidence(evidence: TimeEvidence, clocks: Clocks) -> TimeProjection:
    wall, mono = clocks.sample()
    return _projection_at(evidence, wall=wall, mono=mono, process_id=clocks.process_id)


def evidence_objections(evidence: TimeEvidence, clocks: Clocks) -> tuple[str, ...]:
    return project_time_evidence(evidence, clocks).objections


def provider_interval(evidence: TimeEvidence, clocks: Clocks) -> tuple[datetime, datetime]:
    wall, _ = clocks.sample()
    return wall + timedelta(seconds=evidence.offset_lower), wall + timedelta(
        seconds=evidence.offset_upper
    )


def entry_time_objections(
    observation: Observation,
    evidence: TimeEvidence,
    config: CoreConfig,
    *,
    projection: TimeProjection | None = None,
) -> tuple[str, ...]:
    """Check decision interval at observation receipt; live lifetime is checked at send."""
    reasons = list(evidence.objections)
    if projection is not None:
        if projection != _projection_at(
            evidence,
            wall=projection.wall_at,
            mono=projection.monotonic_at,
            process_id=projection.process_id,
        ):
            reasons.append("TIME_PROJECTION_INVALID")
        reasons.extend(projection.objections)
    if not evidence.samples:
        return tuple(reasons or ["TIME_SAMPLES_INCOMPLETE"])
    p = evidence.samples[-1].provider
    lower = (
        projection.provider_earliest
        if projection
        else evidence.wall_anchor + timedelta(seconds=evidence.offset_lower)
    )
    upper = (
        projection.provider_latest
        if projection
        else evidence.wall_anchor + timedelta(seconds=evidence.offset_upper)
    )
    if p.account_id != config.account_id:
        reasons.append("TIME_ACCOUNT_MISMATCH")
    if lower < p.session_open or upper >= p.session_close - timedelta(seconds=120):
        reasons.append("TIME_SESSION_BOUNDARY")
    if p.session_close != observation.session_close:
        reasons.append("TIME_CALENDAR_MISMATCH")
    if lower < observation.source_end or upper > observation.source_end + timedelta(seconds=120):
        reasons.append("TIME_OBSERVATION_BOUNDARY")
    return tuple(dict.fromkeys(reasons))


def exit_due(
    plan: ProtectionPlan, evidence: TimeEvidence | None, elapsed_seconds: float | None
) -> bool:
    if elapsed_seconds is not None:
        if plan.monotonic_expiry is not None and plan.monotonic_anchor is not None:
            if plan.monotonic_anchor + elapsed_seconds >= plan.monotonic_expiry:
                return True
        else:
            # Old V2 payloads lack an explicit expiry. Convert the local audit
            # origin to the latest possible provider origin under the approved
            # +2s bound. Guardian uses the retained exact bracket when available.
            origin = (
                plan.recorded_at_utc + timedelta(seconds=2)
                if plan.clock_profile == "PAPER_TIME_V2"
                else plan.recorded_at_utc
            )
            duration = max(0, min(1800, (plan.deadline - origin).total_seconds()))
            if elapsed_seconds >= duration:
                return True
    # A valid interval overlapping the deadline triggers a quantity-gated exit.
    return bool(
        evidence
        and not evidence.objections
        and evidence.wall_anchor + timedelta(seconds=evidence.offset_upper) >= plan.deadline
    )
