"""Event-driven market anomaly discovery (#56).

Phase 1 only: deterministic time-of-session detection and a frozen, content-addressed
`MarketAnomalyEvent`. No enrichment, no news, no model workers, and deliberately no broker
authority -- an anomaly is a routing signal for research, never a direction or a confidence.
"""

from quantbot.anomaly.capability import (
    FEED_COVERAGE,
    VolumeCoverage,
    price_trigger_refusal,
    supports_price_trigger,
    supports_volume_trigger,
    volume_coverage,
    volume_trigger_refusal,
)
from quantbot.anomaly.detector import (
    SessionObservation,
    detect,
    group_events,
    refusal,
)
from quantbot.anomaly.models import (
    IMPLEMENTED_TRIGGERS,
    AnomalyDetectorConfig,
    DataQuality,
    MarketAnomalyEvent,
    PriceObservation,
    SessionSegment,
    TriggerReason,
    VolumeObservation,
)

__all__ = [
    "FEED_COVERAGE",
    "IMPLEMENTED_TRIGGERS",
    "AnomalyDetectorConfig",
    "DataQuality",
    "MarketAnomalyEvent",
    "PriceObservation",
    "SessionObservation",
    "SessionSegment",
    "TriggerReason",
    "VolumeCoverage",
    "VolumeObservation",
    "detect",
    "group_events",
    "price_trigger_refusal",
    "refusal",
    "supports_price_trigger",
    "supports_volume_trigger",
    "volume_coverage",
    "volume_trigger_refusal",
]
