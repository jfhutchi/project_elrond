"""Research-side state: hypotheses frozen before measurement.

Kept separate from `quantbot.domain` on purpose. This package holds evidence, not trading
state, and nothing in it has authority over the broker, the risk engine, or the kill switch.
"""

from quantbot.research.registry import (
    PRIOR_TRIALS,
    DataRole,
    DataWindow,
    EpistemicStatus,
    ExecutionClearance,
    HypothesisDraft,
    HypothesisRegistry,
    RefusalReason,
    Registration,
    RegistrationRefused,
    luck_threshold,
    minimum_detectable_sharpe,
    required_sessions,
    summarize,
)

__all__ = [
    "PRIOR_TRIALS",
    "DataRole",
    "DataWindow",
    "EpistemicStatus",
    "ExecutionClearance",
    "HypothesisDraft",
    "HypothesisRegistry",
    "RefusalReason",
    "Registration",
    "RegistrationRefused",
    "luck_threshold",
    "minimum_detectable_sharpe",
    "required_sessions",
    "summarize",
]
