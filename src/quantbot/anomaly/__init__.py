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
    dependence_contexts,
    detect,
    refusal,
)
from quantbot.anomaly.models import (
    IMPLEMENTED_TRIGGERS,
    AnomalyDependenceContext,
    AnomalyDetectorConfig,
    DataQuality,
    MarketAnomalyEvent,
    PriceObservation,
    SessionSegment,
    TriggerReason,
    VolumeObservation,
)
from quantbot.anomaly.routing import (
    MINIMUM_EFFECTIVE_OBSERVATIONS,
    InformationProxy,
    ProxyAvailability,
    ProxyResult,
    RoutingSample,
    RoutingValueReport,
    effective_observations,
    evaluate_routing_value,
)

__all__ = [
    "FEED_COVERAGE",
    "MINIMUM_EFFECTIVE_OBSERVATIONS",
    "IMPLEMENTED_TRIGGERS",
    "AnomalyDependenceContext",
    "AnomalyDetectorConfig",
    "DataQuality",
    "InformationProxy",
    "MarketAnomalyEvent",
    "PriceObservation",
    "ProxyAvailability",
    "ProxyResult",
    "RoutingSample",
    "RoutingValueReport",
    "SessionObservation",
    "SessionSegment",
    "TriggerReason",
    "VolumeCoverage",
    "VolumeObservation",
    "detect",
    "effective_observations",
    "evaluate_routing_value",
    "dependence_contexts",
    "price_trigger_refusal",
    "refusal",
    "supports_price_trigger",
    "supports_volume_trigger",
    "volume_coverage",
    "volume_trigger_refusal",
]
