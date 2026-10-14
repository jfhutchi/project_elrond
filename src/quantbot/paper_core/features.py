"""M1 checks causal synthetic features; production feature mathematics remains unchanged."""

import math

from quantbot.paper_core.contracts import Observation


def observation_objections(
    observation: Observation, *, dependency_freshness: bool = False
) -> tuple[str, ...]:
    reasons = []
    now = observation.recorded_at_utc
    if not observation.source_start < observation.source_end <= observation.available_at <= now:
        reasons.append("NONCAUSAL_OBSERVATION")
    if len(observation.dependencies) != len(observation.dependency_available_at) or any(
        t is None or t > now or (dependency_freshness and (now - t).total_seconds() > 120)
        for t in observation.dependency_available_at
    ):
        reasons.append("DEPENDENCY_UNAVAILABLE")
    if (
        not observation.features
        or len({n for n, _ in observation.features}) != len(observation.features)
        or any(not math.isfinite(v) for _, v in observation.features)
    ):
        reasons.append("INVALID_FEATURES")
    if (now - observation.source_end).total_seconds() > 120:
        reasons.append("OBSERVATION_STALE")
    return tuple(reasons)
