"""What the detector froze, and everything needed to reproduce why it fired (#56 phase 1).

Two rules shape every model here, both carried over from the risk layer rather than reinvented.

**An event is content-addressed, never wall-clock addressed.** A retry, a replay or a restarted
worker re-deriving the same observation must produce the same `event_id`, or the queue behind it
counts one piece of information twice. #57 deduplicates on exactly this seam.

**Unmeasurable is not normal.** Every field the detector could not establish is recorded as
absent with a reason, and an absent input refuses the trigger that needed it. A detector that
substituted a default would emit a confident event resting on a number nobody measured.

**The score is a routing signal, not a direction and not a confidence.** #56 is explicit that
anomaly detection and trade direction stay separate, and that engineering completion is not
evidence of alpha. So components are persisted individually and there is deliberately no single
opaque score field that downstream could mistake for a probability.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.anomaly.capability import VolumeCoverage, volume_coverage
from quantbot.market_data.base import MarketDataFeed
from quantbot.risk.freshness import DataCadence

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SessionSegment(StrEnum):
    """The part of the session an observation belongs to.

    #56 requires comparing like with like: minute `m` against minute `m` of prior sessions,
    because volume is structurally high near the open and close and a whole-day average
    manufactures false positives at both ends.

    Daily bars have exactly one segment. Naming it rather than special-casing it means the
    baseline keying is already correct when minute bars arrive, instead of being retrofitted --
    and the segment is part of the baseline identity either way.
    """

    FULL_SESSION = "FULL_SESSION"
    OPENING = "OPENING"
    MIDDAY = "MIDDAY"
    CLOSING = "CLOSING"


class TriggerReason(StrEnum):
    """Why an event fired. Plural per event: one observation can trip several."""

    VOLUME_SHOCK = "VOLUME_SHOCK"
    PRICE_VELOCITY = "PRICE_VELOCITY"
    RANGE_EXPANSION = "RANGE_EXPANSION"


class DataQuality(StrEnum):
    """Whether the detector could measure what it needed."""

    OK = "OK"
    #: Fewer comparable prior observations than the configured window. A short baseline is
    #: different evidence, not weaker evidence of the same kind.
    INSUFFICIENT_BASELINE = "INSUFFICIENT_BASELINE"
    #: The baseline does not vary, so a robust z is undefined rather than large.
    NON_VARYING_BASELINE = "NON_VARYING_BASELINE"
    #: The feed cannot support the measurement the trigger needs. See `capability`.
    FEED_UNFIT = "FEED_UNFIT"


class AnomalyDetectorConfig(BaseModel):
    """The frozen rule. Every value that changes what fires is a field, and every field is hashed.

    #56 is explicit that the seed thresholds are research seeds rather than production truth, so
    they must be replaceable only through the registered process -- which requires that two
    detectors with different numbers be two different objects, not one object edited.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=1, ge=1)
    #: Comparable prior observations required before any trigger may fire.
    baseline_sessions: int = Field(default=60, ge=10)
    #: Seed thresholds. Labelled as seeds in #56 and treated as such: they are in the identity,
    #: so nothing measured under one can be pooled with anything measured under another.
    volume_robust_z: Decimal = Field(default=Decimal("4.0"), gt=0)
    interval_rvol: Decimal = Field(default=Decimal("3.0"), gt=1)
    price_velocity_sigma: Decimal = Field(default=Decimal("4.0"), gt=0)
    #: The feed the detector is reading, and the cadence it expects. Both change what the
    #: numbers mean, so both are identity.
    feed: MarketDataFeed = MarketDataFeed.SIP
    cadence: DataCadence = DataCadence.DAILY_BAR

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"anomaly-detector-v{self.version}-{digest[:16]}"

    @property
    def volume_coverage(self) -> VolumeCoverage:
        return volume_coverage(self.feed)


class VolumeObservation(BaseModel):
    """Observed against expected, kept as separate fields rather than only their ratio.

    A ratio alone cannot be audited: 3.0 could be a quiet baseline or a real surge, and the two
    call for different follow-up. #56 asks for the components to be persisted individually.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed: Decimal = Field(ge=0)
    #: `None` when the baseline could not be established. Refuses the volume trigger.
    expected: Decimal | None = None
    robust_z: Decimal | None = None

    @property
    def rvol(self) -> Decimal | None:
        if self.expected is None or self.expected <= 0:
            return None
        return self.observed / self.expected


class PriceObservation(BaseModel):
    """Return and range against their own trailing dispersion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_return: Decimal
    #: Trailing realised dispersion of returns. `None` refuses the price trigger.
    trailing_sigma: Decimal | None = None
    range_fraction: Decimal | None = None
    expected_range_fraction: Decimal | None = None

    @property
    def vol_normalized_move(self) -> Decimal | None:
        if self.trailing_sigma is None or self.trailing_sigma <= 0:
            return None
        return self.session_return / self.trailing_sigma


class MarketAnomalyEvent(BaseModel):
    """One frozen observation that something unusual happened. Not a trading signal.

    Persisted before any enrichment runs, so a downstream failure cannot erase what was actually
    observed -- and so the routing-value measurement can compare what the detector woke up
    against what a naive scan would have.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: Text
    session_date: date
    segment: SessionSegment
    observed_at: datetime
    detector_identity: Text
    #: Feed semantics, carried explicitly. #56 requires that a venue feed can never be silently
    #: read as full-market volume, and the honest place for that is on the event.
    feed: MarketDataFeed
    volume_coverage: VolumeCoverage
    cadence: DataCadence
    #: The exact prior sessions the baseline was built from. Same reasoning as
    #: `DependenceSnapshot.session_window` (#84): a count is not a sample, and a baseline
    #: reconstructed later is not the one the detector used.
    baseline_sessions: tuple[date, ...]
    volume: VolumeObservation
    price: PriceObservation
    #: Every reason that fired, sorted. Empty is not a valid event.
    triggers: tuple[TriggerReason, ...] = Field(min_length=1)
    data_quality: DataQuality = DataQuality.OK
    #: Symbols whose events share one underlying move. Set from the #58 dependence clustering
    #: so a market-wide shock is one piece of information rather than dozens of pseudo-
    #: independent samples -- a multiple-testing problem that looks like a queueing problem.
    #: `None` when no dependence snapshot was available, which is recorded rather than assumed.
    group_key: str | None = None
    dependence_snapshot_identity: str | None = None

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if list(self.triggers) != sorted(set(self.triggers)):
            raise ValueError(
                "triggers must be sorted and unique; an unstable order would give the same "
                "observation two different event ids"
            )
        if list(self.baseline_sessions) != sorted(set(self.baseline_sessions)):
            raise ValueError("baseline_sessions must be strictly ascending and free of duplicates")
        if any(day >= self.session_date for day in self.baseline_sessions):
            raise ValueError(
                "a baseline session at or after the observed session is look-ahead; the "
                "baseline must be built only from sessions that had already completed"
            )
        if TriggerReason.VOLUME_SHOCK in self.triggers and self.volume.expected is None:
            raise ValueError(
                "a volume shock cannot fire without an expected volume to compare against"
            )
        return self

    @property
    def event_id(self) -> str:
        """Replay-stable identity, derived from content rather than from when it was computed.

        `observed_at` is deliberately excluded. Re-deriving the same session's observation on a
        retry must produce the same id, and a wall-clock component would mint a second event for
        one piece of information -- which is precisely the sample inflation #56 warns about.
        """
        payload = {
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "segment": self.segment.value,
            "detector_identity": self.detector_identity,
            "feed": self.feed.value,
            "volume_coverage": self.volume_coverage.value,
            "cadence": self.cadence.value,
            "baseline_sessions": [day.isoformat() for day in self.baseline_sessions],
            "volume": json.loads(self.volume.model_dump_json()),
            "price": json.loads(self.price.model_dump_json()),
            "triggers": [item.value for item in self.triggers],
            "data_quality": self.data_quality.value,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"anomaly-{self.session_date.isoformat()}-{digest[:16]}"

    def as_row(self) -> Mapping[str, object]:
        """Flatten for storage, keeping `storage` free of any import from this package."""
        return {
            "event_id": self.event_id,
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "segment": self.segment.value,
            "observed_at": self.observed_at.isoformat(),
            "detector_identity": self.detector_identity,
            "feed": self.feed.value,
            "volume_coverage": self.volume_coverage.value,
            "cadence": self.cadence.value,
            "baseline_first_session": self.baseline_sessions[0].isoformat(),
            "baseline_last_session": self.baseline_sessions[-1].isoformat(),
            "baseline_session_count": len(self.baseline_sessions),
            "observed_volume": str(self.volume.observed),
            "expected_volume": None if self.volume.expected is None else str(self.volume.expected),
            "volume_robust_z": (
                None if self.volume.robust_z is None else str(self.volume.robust_z)
            ),
            "session_return": str(self.price.session_return),
            "trailing_sigma": (
                None if self.price.trailing_sigma is None else str(self.price.trailing_sigma)
            ),
            "triggers": ",".join(item.value for item in self.triggers),
            "data_quality": self.data_quality.value,
            "group_key": self.group_key,
            "dependence_snapshot_identity": self.dependence_snapshot_identity,
        }


__all__ = [
    "AnomalyDetectorConfig",
    "DataQuality",
    "MarketAnomalyEvent",
    "PriceObservation",
    "SessionSegment",
    "TriggerReason",
    "VolumeObservation",
]
