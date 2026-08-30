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


#: Trigger families with a detector behind them. `RANGE_EXPANSION` is declared in #56 and not
#: yet computed, so it cannot be enabled.
IMPLEMENTED_TRIGGERS: frozenset[TriggerReason] = frozenset(
    {TriggerReason.VOLUME_SHOCK, TriggerReason.PRICE_VELOCITY}
)


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

    #: Which trigger families this detector may emit. Explicit and versioned, because "which
    #: questions the detector is allowed to ask" changes the event stream as much as any
    #: threshold does, and two detectors emitting different families are two detectors.
    #:
    #: A family listed here is still subject to the feed's measured capability -- enabling one
    #: is a statement of intent, not a grant. `RANGE_EXPANSION` is deliberately absent: it is
    #: declared in #56 but nothing computes it yet, and a family that is enabled by default
    #: while unimplemented is the shape of control #75 existed to remove.
    enabled_triggers: frozenset[TriggerReason] = frozenset(
        {TriggerReason.VOLUME_SHOCK, TriggerReason.PRICE_VELOCITY}
    )

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        if not self.enabled_triggers:
            raise ValueError(
                "a detector with no enabled trigger families can never emit an event; disable "
                "the detector rather than configuring one that silently does nothing"
            )
        unimplemented = self.enabled_triggers - IMPLEMENTED_TRIGGERS
        if unimplemented:
            # Same rule as the risk layer's `UNIMPLEMENTED` veto set: a declared-but-absent
            # control reads as protection that is not there.
            raise ValueError(
                f"{sorted(item.value for item in unimplemented)} has no detector behind it, so "
                "enabling it would name a trigger that can never fire"
            )
        return self

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        # Sorted, because a set serialises in an arbitrary order and an identity that depended
        # on it would differ between two identical detectors.
        payload["enabled_triggers"] = sorted(payload["enabled_triggers"])
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
    #: Whether each **enabled** family could be measured, per family rather than for the event
    #: as a whole. A single verdict was wrong in a specific way: on IEX the volume family cannot
    #: run, and a scalar `FEED_UNFIT` then described a perfectly valid price observation as
    #: unusable, so downstream routing would discard a real event for a reason that applies to
    #: a different question.
    family_quality: Mapping[TriggerReason, DataQuality]

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
        unmeasured = [
            item.value
            for item in self.triggers
            if self.family_quality.get(item, DataQuality.OK) is not DataQuality.OK
        ]
        if unmeasured:
            raise ValueError(
                f"{sorted(unmeasured)} fired while recorded as unmeasurable; a family that "
                "could not be measured cannot also have triggered"
            )
        if not set(self.triggers) <= set(self.family_quality):
            raise ValueError("every fired family must report its measurement quality")
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
            "family_quality": {
                family.value: quality.value
                for family, quality in sorted(
                    self.family_quality.items(), key=lambda pair: pair[0].value
                )
            },
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
            "family_quality": ",".join(
                f"{family.value}={quality.value}"
                for family, quality in sorted(
                    self.family_quality.items(), key=lambda pair: pair[0].value
                )
            ),
        }


class AnomalyDependenceContext(BaseModel):
    """How one frozen dependence model grouped one raw anomaly observation (#56).

    Kept as its own artifact rather than as fields on the event, because the two answer
    different questions and change on different clocks:

    * the **raw observation** is what happened -- a symbol, a session, an expected against an
      observed. It is fixed the moment it is measured;
    * the **dependence context** is what one particular correlation matrix, computed at one
      as-of date under one method, said about which other symbols were moving with it. Re-run
      the matrix over a different window and the grouping legitimately changes while nothing
      about the observation does.

    Folding the second into the first broke content-addressing in a way that would have become a
    storage defect: `group_events` produced an event with a different `group_key` and the *same*
    `event_id`, so two materially different payloads shared one supposedly content-addressed
    identity. Keying `market_anomaly_events` on that would have silently deduplicated them.

    Splitting it also removes the need to redefine a historical observation whenever a dependence
    model is revised. A new matrix produces a new context; the observation it describes is
    untouched, which is the same rule #72 fixed for policy versions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method_version: int = Field(default=1, ge=1)
    #: The raw observation this describes. A relation, not a copy.
    anomaly_event_id: Text
    #: The exact frozen matrix that produced the grouping. Required: an unnamed matrix cannot be
    #: checked or reproduced, and a cluster without one is an ungrounded claim about what moves
    #: together.
    dependence_snapshot_identity: Text
    #: The cluster this symbol landed in, or `None` when the snapshot could measure the symbol
    #: but placed it alone -- or could not measure it at all. Recorded rather than defaulted,
    #: because "this matrix had nothing for it" is a different fact from "no matrix ran", and
    #: the presence of the context row is what distinguishes them.
    group_key: str | None = None

    @property
    def identity(self) -> str:
        payload = {
            "method_version": self.method_version,
            "anomaly_event_id": self.anomaly_event_id,
            "dependence_snapshot_identity": self.dependence_snapshot_identity,
            "group_key": self.group_key,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"anomaly-dependence-v{self.method_version}-{digest[:16]}"

    def as_row(self) -> Mapping[str, object]:
        return {
            "context_id": self.identity,
            "anomaly_event_id": self.anomaly_event_id,
            "dependence_snapshot_identity": self.dependence_snapshot_identity,
            "group_key": self.group_key,
            "method_version": self.method_version,
        }


__all__ = [
    "IMPLEMENTED_TRIGGERS",
    "AnomalyDependenceContext",
    "AnomalyDetectorConfig",
    "DataQuality",
    "MarketAnomalyEvent",
    "PriceObservation",
    "SessionSegment",
    "TriggerReason",
    "VolumeObservation",
]
