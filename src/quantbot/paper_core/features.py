"""M1 checks causal synthetic features; production feature mathematics remains unchanged."""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from quantbot.paper_core.contracts import Observation, OpeningQuoteSource


def _opening_quote_objections(
    observation: Observation,
    evaluated_at: datetime,
    provider_interval: tuple[datetime, datetime] | None,
) -> tuple[str, ...]:
    sources = observation.opening_quote_sources
    if (
        observation.feature_context != "OPENING_QUOTE_GAP_V1"
        or observation.feature_version != "opening-quote-gap-v1"
        or sources is None
        or len(sources) != 2
        or not all(isinstance(s, OpeningQuoteSource) for s in sources)
    ):
        return ("OPENING_CONTEXT_INVALID",)
    asset, market = sources
    capture = observation.recorded_at_utc
    timestamps = (
        evaluated_at,
        capture,
        observation.source_start,
        observation.source_end,
        observation.available_at,
        observation.capture_received_at,
        observation.session_open,
        observation.session_close,
        *(
            t
            for s in sources
            for t in (
                s.previous_session_open,
                s.previous_session_close,
                s.previous_bar_timestamp,
                s.quote_at,
            )
        ),
    )
    if any(not isinstance(t, datetime) or t.tzinfo is None for t in timestamps):
        return ("OPENING_CONTEXT_INVALID",)
    ny = ZoneInfo("America/New_York")
    if (
        (asset.symbol, market.symbol) != (observation.symbol, "SPY")
        or observation.dependencies != ("SPY",)
        or observation.dependency_available_at != (market.quote_at,)
        or not capture
        == observation.source_end
        == observation.available_at
        == observation.capture_received_at
        or not observation.session_open
        <= capture
        < min(observation.session_close, observation.session_open + timedelta(minutes=35))
        or observation.source_start != asset.previous_session_close
        or (asset.previous_session_open, asset.previous_session_close)
        != (market.previous_session_open, market.previous_session_close)
        or any(
            not s.previous_session_open < s.previous_session_close < observation.session_open
            or s.previous_bar_timestamp > s.previous_session_open
            or s.previous_bar_timestamp.astimezone(ny).date()
            != s.previous_session_open.astimezone(ny).date()
            or not all(v.is_finite() and v > 0 for v in (s.previous_close, s.bid, s.ask))
            or s.bid > s.ask
            for s in sources
        )
        or observation.price != asset.ask
    ):
        return ("OPENING_CONTEXT_INVALID",)
    lower, upper = provider_interval or (evaluated_at, evaluated_at)
    if (
        any(t.tzinfo is None for t in (lower, upper))
        or lower > upper
        or not 0 <= (evaluated_at - capture).total_seconds() <= 5
        or any(
            not observation.session_open <= s.quote_at <= lower
            or not 0 <= (upper - s.quote_at).total_seconds() <= 5
            for s in sources
        )
    ):
        return ("OPENING_QUOTES_UNAVAILABLE",)
    gap = (asset.bid + asset.ask) / 2 / asset.previous_close - 1
    market_gap = (market.bid + market.ask) / 2 / market.previous_close - 1
    expected = (
        ("gap_return", float(gap)),
        ("market_gap_return", float(market_gap)),
        ("relative_gap", float(gap - market_gap)),
        ("spread_bps", float((asset.ask - asset.bid) / asset.bid * 10000)),
    )
    if len(observation.features) != len(expected) or any(
        name != expected_name
        or not math.isclose(value, expected_value, rel_tol=1e-12, abs_tol=1e-12)
        for (name, value), (expected_name, expected_value) in zip(
            observation.features, expected, strict=False
        )
    ):
        return ("OPENING_CONTEXT_INVALID",)
    return ()


def observation_objections(
    observation: Observation,
    *,
    dependency_freshness: bool = False,
    evaluation_at: datetime | None = None,
    provider_interval: tuple[datetime, datetime] | None = None,
) -> tuple[str, ...]:
    reasons = []
    now = observation.recorded_at_utc
    if (
        observation.feature_context == "OPENING_QUOTE_GAP_V1"
        or observation.feature_version == "opening-quote-gap-v1"
        or observation.opening_quote_sources is not None
    ):
        objections = _opening_quote_objections(observation, evaluation_at or now, provider_interval)
        if objections:
            return objections
    if getattr(observation, "feature_context", "REGULAR_SESSION_V1") == "OPENING_CONTIGUOUS_V1":
        from quantbot.learner.five_minute import FEATURE_NAMES

        if (
            observation.feature_version != "five-minute-price-alpha-v1"
            or observation.session_open is None
            or not observation.session_open
            <= observation.source_end
            < observation.session_open + timedelta(minutes=35)
            or observation.source_start != observation.source_end - timedelta(minutes=35)
            or observation.source_end.timestamp() % 300 != 0
            or tuple(name for name, _ in observation.features) != FEATURE_NAMES
        ):
            reasons.append("OPENING_CONTEXT_INVALID")
    if not observation.source_start < observation.source_end <= observation.available_at <= now:
        reasons.append("NONCAUSAL_OBSERVATION")
    if observation.feature_context != "OPENING_QUOTE_GAP_V1" and (
        len(observation.dependencies) != len(observation.dependency_available_at)
        or any(
            t is None or t > now or (dependency_freshness and (now - t).total_seconds() > 120)
            for t in observation.dependency_available_at
        )
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
