"""Time-of-session baselines, and the refusals that stop them manufacturing events (#56 phase 1).

The detector answers "something unusual is happening", never "buy this". #56 is explicit that
anomaly detection and trade direction stay separate, and that a high score is a routing signal
for research rather than statistical confidence in continuation.

**Robust statistics, because volume is heavy-tailed.** A mean-and-standard-deviation baseline
lets one prior extreme session dominate its own comparison set: the outlier inflates the mean and
the deviation, and the next genuine shock scores lower than it should. Median and scaled MAD do
not move much for one point in sixty, which is the property wanted from a baseline.

**Log volume, not raw.** Volume is roughly lognormal, so a symmetric z on raw counts treats a
half-volume session as far less unusual than a double-volume one. Taking logs first makes the
two comparable, which is what "unusual" should mean here.

**Every refusal is explicit.** A short baseline, a non-varying one, or a feed that cannot carry
the measurement each produce no event and a recorded reason, rather than an event resting on a
substituted number.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

from quantbot.anomaly.capability import (
    price_trigger_refusal,
    supports_price_trigger,
    supports_volume_trigger,
    volume_trigger_refusal,
)
from quantbot.anomaly.models import (
    AnomalyDependenceContext,
    AnomalyDetectorConfig,
    DataQuality,
    MarketAnomalyEvent,
    PriceObservation,
    SessionSegment,
    TriggerReason,
    VolumeObservation,
)

#: Converts a MAD into a standard-deviation-equivalent scale for a normal distribution, so the
#: robust z and a conventional z are on the same footing and one threshold means one thing.
MAD_TO_SIGMA = 1.4826


class SessionObservation:
    """One symbol's completed session, as the detector sees it."""

    __slots__ = ("session_date", "volume", "close", "high", "low")

    def __init__(
        self,
        *,
        session_date: date,
        volume: Decimal,
        close: Decimal,
        high: Decimal | None = None,
        low: Decimal | None = None,
    ) -> None:
        self.session_date = session_date
        self.volume = volume
        self.close = close
        self.high = high
        self.low = low


def _robust_z(values: Sequence[float], observed: float) -> tuple[float | None, float | None]:
    """Median-and-MAD z of `observed` against `values`, plus the median itself.

    Returns `(None, median)` when the comparison set does not vary: a z is then undefined rather
    than infinite, and reporting a large number would be inventing a measurement.
    """
    if not values:
        return None, None
    median = statistics.median(values)
    mad = statistics.median([abs(value - median) for value in values])
    if mad == 0:
        return None, median
    return (observed - median) / (MAD_TO_SIGMA * mad), median


def detect(
    *,
    symbol: str,
    history: Sequence[SessionObservation],
    config: AnomalyDetectorConfig,
    observed_at: datetime,
    segment: SessionSegment = SessionSegment.FULL_SESSION,
) -> MarketAnomalyEvent | None:
    """Judge the most recent observation in `history` against the ones before it.

    `history` is ordered oldest-first and its **last** element is the session under judgement;
    everything before it is the baseline. Passing the observation inside its own history rather
    than separately is deliberate -- it makes a look-ahead slip impossible to express, because
    there is no second argument that could accidentally contain a later session.

    Returns `None` when nothing fired or when the measurement could not be made. A caller that
    needs to know *why* nothing fired should read `refusal`, which answers the same question
    without constructing an event that did not happen.
    """
    if len(history) < 2:
        return None
    current = history[-1]
    baseline = list(history[:-1])[-config.baseline_sessions :]

    family_quality: dict[TriggerReason, DataQuality] = {}
    triggers: list[TriggerReason] = []

    # --- volume ------------------------------------------------------------------------------
    expected: Decimal | None = None
    robust_z: Decimal | None = None
    if TriggerReason.VOLUME_SHOCK not in config.enabled_triggers:
        pass
    elif not supports_volume_trigger(config.feed):
        family_quality[TriggerReason.VOLUME_SHOCK] = DataQuality.FEED_UNFIT
    elif len(baseline) < config.baseline_sessions:
        family_quality[TriggerReason.VOLUME_SHOCK] = DataQuality.INSUFFICIENT_BASELINE
    else:
        logs = [math.log(float(item.volume)) for item in baseline if item.volume > 0]
        if len(logs) == len(baseline) and current.volume > 0:
            score, median = _robust_z(logs, math.log(float(current.volume)))
            if median is not None:
                expected = Decimal(str(round(math.exp(median), 4)))
            if score is None:
                family_quality[TriggerReason.VOLUME_SHOCK] = DataQuality.NON_VARYING_BASELINE
            else:
                family_quality[TriggerReason.VOLUME_SHOCK] = DataQuality.OK
                robust_z = Decimal(str(round(score, 4)))
                rvol = None if expected is None or expected <= 0 else current.volume / expected
                if robust_z >= config.volume_robust_z or (
                    rvol is not None and rvol >= config.interval_rvol
                ):
                    triggers.append(TriggerReason.VOLUME_SHOCK)
        else:
            family_quality[TriggerReason.VOLUME_SHOCK] = DataQuality.INSUFFICIENT_BASELINE

    # --- price -------------------------------------------------------------------------------
    returns = [
        math.log(float(baseline[index].close) / float(baseline[index - 1].close))
        for index in range(1, len(baseline))
        if baseline[index].close > 0 and baseline[index - 1].close > 0
    ]
    session_return = Decimal("0")
    trailing_sigma: Decimal | None = None
    if baseline and baseline[-1].close > 0 and current.close > 0:
        session_return = Decimal(
            str(round(math.log(float(current.close) / float(baseline[-1].close)), 6))
        )
    price_enabled = (
        TriggerReason.PRICE_VELOCITY in config.enabled_triggers
        and supports_price_trigger(config.feed)
    )
    if TriggerReason.PRICE_VELOCITY in config.enabled_triggers and not supports_price_trigger(
        config.feed
    ):
        family_quality[TriggerReason.PRICE_VELOCITY] = DataQuality.FEED_UNFIT
    elif price_enabled and (len(returns) < config.baseline_sessions - 1 or len(returns) < 2):
        family_quality[TriggerReason.PRICE_VELOCITY] = DataQuality.INSUFFICIENT_BASELINE
    elif price_enabled:
        sigma = statistics.pstdev(returns)
        if sigma == 0:
            family_quality[TriggerReason.PRICE_VELOCITY] = DataQuality.NON_VARYING_BASELINE
        else:
            family_quality[TriggerReason.PRICE_VELOCITY] = DataQuality.OK
            trailing_sigma = Decimal(str(round(sigma, 6)))
            if abs(session_return / trailing_sigma) >= config.price_velocity_sigma:
                triggers.append(TriggerReason.PRICE_VELOCITY)

    if not triggers:
        return None

    return MarketAnomalyEvent(
        symbol=symbol,
        session_date=current.session_date,
        segment=segment,
        observed_at=observed_at,
        detector_identity=config.identity,
        feed=config.feed,
        volume_coverage=config.volume_coverage,
        cadence=config.cadence,
        baseline_sessions=tuple(item.session_date for item in baseline),
        volume=VolumeObservation(
            observed=current.volume, expected=expected, robust_z=robust_z
        ),
        price=PriceObservation(session_return=session_return, trailing_sigma=trailing_sigma),
        triggers=tuple(sorted(set(triggers))),
        family_quality=dict(family_quality),
    )


def refusal(
    config: AnomalyDetectorConfig,
    history: Sequence[SessionObservation],
    trigger: TriggerReason = TriggerReason.VOLUME_SHOCK,
) -> str | None:
    """Why this configuration cannot produce `trigger` here, or `None` when it can.

    Kept separate from `detect` so a caller can report *"the detector is running but structurally
    cannot fire"* -- a different operational state from *"nothing unusual happened"*, and the two
    look identical from an empty event stream.

    Per family, because the families do not share a fate: on a venue feed the volume trigger is
    refused and the price trigger is not, and a single answer would have to be wrong about one
    of them.
    """
    if trigger not in config.enabled_triggers:
        return f"{trigger.value} is not an enabled trigger family for {config.identity}"
    unfit = (
        volume_trigger_refusal(config.feed)
        if trigger is TriggerReason.VOLUME_SHOCK
        else price_trigger_refusal(config.feed)
    )
    if unfit is not None:
        return unfit
    if len(history) - 1 < config.baseline_sessions:
        return (
            f"{len(history) - 1} comparable prior sessions available and "
            f"{config.baseline_sessions} required; a short baseline is different evidence "
            "rather than weaker evidence of the same kind"
        )
    return None


def dependence_contexts(
    events: Sequence[MarketAnomalyEvent],
    clusters: Mapping[str, str],
    *,
    snapshot_identity: str,
) -> tuple[AnomalyDependenceContext, ...]:
    """Relate each raw observation to the cluster one frozen matrix put it in (#56 item 4).

    On a macro day every symbol's volume rises together, so a per-symbol detector turns one
    piece of information into dozens of events. Counting those as independent samples is a
    multiple-testing error wearing the costume of a queueing problem, and the grouping has to
    survive into whatever consumes the events.

    The clustering comes from the #58 dependence snapshot rather than a second notion of "these
    move together" invented here. A symbol the snapshot could not measure gets no group, which
    is recorded rather than defaulted to its own.

    Returns **new relations** rather than modified events. Grouping used to be stamped onto the
    event, which broke content-addressing: the grouped copy carried a different `group_key` and
    the same `event_id`, so two materially different payloads shared one identity and a store
    keyed on it would have deduplicated them silently.

    The split also matches what the two things are. The observation is fixed when it is measured;
    the grouping is one matrix's opinion about it, computed over a chosen window, and re-running
    that matrix legitimately changes the opinion without changing what happened. A context row is
    emitted for every event, including symbols the matrix placed alone -- its presence is what
    separates "this matrix had nothing for it" from "no matrix ran".
    """
    if not snapshot_identity.strip():
        raise ValueError(
            "grouping requires the identity of the dependence snapshot that produced the "
            "clusters; an unnamed matrix cannot be checked or reproduced"
        )
    return tuple(
        AnomalyDependenceContext(
            anomaly_event_id=event.event_id,
            dependence_snapshot_identity=snapshot_identity,
            group_key=clusters.get(event.symbol),
        )
        for event in events
    )


__all__ = [
    "MAD_TO_SIGMA",
    "SessionObservation",
    "dependence_contexts",
    "detect",
    "refusal",
]
