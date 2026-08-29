"""Whether market data is current, judged against the exchange calendar (#74).

`STALE_MARKET_DATA` shipped as a single wall-clock ceiling, and the code said plainly that four
days was a coarse proxy needing the exchange calendar. That was tolerable while one strategy
decided once a session on daily bars. It stops being tolerable the moment #56 adds an intraday
engine, because no single elapsed-time threshold can serve both:

* four days is what a **daily** feed needs to survive a long weekend, and it would wave through an
  intraday feed that died at the opening bell;
* four minutes would be right for a minute feed, and it would refuse every daily entry.

**Elapsed time is the wrong measurement.** What matters is whether the data the market has
actually produced is present. A Friday close is perfectly current all weekend; a Tuesday close is
stale on Wednesday afternoon even though far less time has passed. The calendar knows which of
those is true and a clock cannot.

**`MARKET_CLOSED` is not `FEED_STALE`.** Conflating them is how a quiet feed during a holiday
reads as an outage, and how a genuine outage overnight reads as normal. They are different facts
about the world and this reports them as different outcomes.

**Everything fails closed.** No calendar, no observation, no cadence for an intraday feed: each
rejects. An unmeasurable freshness is not a fresh one, which is the same rule the rest of the risk
layer runs on.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantbot.market_data.calendar import latest_completed_session
from quantbot.risk.shadow import session_of
from quantbot.strategy import XNYSSession


class DataCadence(StrEnum):
    """How often the feed under judgement is expected to produce an observation."""

    #: One bar per session, stamped at the close. Freshness is a question about *sessions*.
    DAILY_BAR = "DAILY_BAR"
    #: Bars or quotes during the session. Freshness is a question about *seconds*, and only
    #: while the market is open.
    INTRADAY = "INTRADAY"


class MarketState(StrEnum):
    """Whether the venue was open at the moment of the decision."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class FreshnessOutcome(StrEnum):
    """What the assessment found. Only `FRESH` permits an order."""

    FRESH = "FRESH"
    #: A session has completed and the feed has no observation for it. True regardless of how
    #: little time has passed -- a missing Tuesday is missing on Wednesday morning.
    EXPECTED_SESSION_MISSING = "EXPECTED_SESSION_MISSING"
    #: The market is open and the feed has stopped producing. Only reachable intraday, because a
    #: daily feed is *supposed* to be silent during the session.
    FEED_STALE = "FEED_STALE"
    #: Something needed to answer was absent. Rejects, like every unmeasurable input here.
    FRESHNESS_METADATA_UNAVAILABLE = "FRESHNESS_METADATA_UNAVAILABLE"


class MarketFreshnessPolicy(BaseModel):
    """The freshness rule itself, versioned and content-addressed (#74).

    A single `max_age_seconds` could not be keyed by venue or cadence, so there was nothing to
    name when a decision was later questioned. This can be: `identity` hashes the cadence, the
    calendar it applies to and the tolerated interval count, which is what #57 needs in order to
    compare refusals under one rule against refusals under another.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=1, ge=1)
    #: The venue whose calendar decides what a completed session is.
    calendar: str = Field(default="XNYS", min_length=1)
    cadence: DataCadence = DataCadence.DAILY_BAR
    #: Expected seconds between observations. Required for intraday, meaningless for daily.
    expected_interval_seconds: int | None = Field(default=None, gt=0)
    #: How many expected intervals an open-session feed may miss before it counts as stopped.
    tolerated_intervals: int = Field(default=3, gt=0)

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.cadence is DataCadence.INTRADAY and self.expected_interval_seconds is None:
            # "How stale is too stale" has no safe default for an unknown feed, so an intraday
            # policy that cannot say is refused rather than given one.
            raise ValueError(
                "an intraday freshness policy must declare its expected observation interval; "
                "without it there is no cadence to measure a stopped feed against"
            )
        return self

    @property
    def identity(self) -> str:
        payload = {
            "version": self.version,
            "calendar": self.calendar,
            "cadence": self.cadence.value,
            "expected_interval_seconds": self.expected_interval_seconds,
            "tolerated_intervals": self.tolerated_intervals,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"freshness-policy-v{self.version}-{digest[:16]}"


@dataclass(frozen=True, slots=True)
class FreshnessExpectation:
    """What the calendar says should exist by now.

    Derived from sessions rather than from a clock, and passed *in* to the assessment for the
    reason every other risk input is: a rule that fetched its own calendar would decide what it
    was allowed to see.
    """

    policy_identity: str
    cadence: DataCadence
    market_state: MarketState
    #: The last session whose close has passed. `None` when the calendar covers nothing, which
    #: is unmeasurable rather than permissive.
    last_completed_session: date | None
    #: The session in progress, when the market is open.
    open_session: date | None
    #: How often an intraday feed should produce. `None` for daily, required for intraday.
    expected_interval_seconds: int | None


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """The verdict and everything a rejection has to be able to say (#74 criterion 8)."""

    outcome: FreshnessOutcome
    policy_identity: str | None
    cadence: DataCadence
    market_state: MarketState
    expected_session: date | None
    observed_session: date | None
    latest_observation_at: datetime | None
    detail: str

    @property
    def fresh(self) -> bool:
        return self.outcome is FreshnessOutcome.FRESH


def expected_freshness(
    *,
    sessions: Sequence[XNYSSession],
    observed_at: datetime,
    policy: MarketFreshnessPolicy,
) -> FreshnessExpectation | None:
    """Read the calendar for what the feed owes us at `observed_at`.

    Returns `None` when the calendar cannot answer, so the caller rejects rather than assuming.
    An intraday cadence with no declared interval is also unanswerable: "how stale is too stale"
    has no default that is safe for an unknown feed.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not sessions:
        return None

    ordered = tuple(sessions)
    completed = latest_completed_session(ordered, observed_at)
    live = next(
        (
            session
            for session in ordered
            if session.open_at <= observed_at < session.close_at
        ),
        None,
    )
    return FreshnessExpectation(
        policy_identity=policy.identity,
        cadence=policy.cadence,
        market_state=MarketState.OPEN if live is not None else MarketState.CLOSED,
        last_completed_session=None if completed is None else completed.session_date,
        open_session=None if live is None else live.session_date,
        expected_interval_seconds=policy.expected_interval_seconds,
    )


def assess_freshness(
    *,
    expectation: FreshnessExpectation | None,
    latest_observation_at: datetime | None,
    observed_at: datetime,
    symbol: str,
    tolerated_intervals: int,
) -> FreshnessAssessment:
    """Judge the newest observation against what the market actually produced.

    The daily rule asks a question about sessions and never about elapsed time, so a long weekend
    and a holiday need no allowance and a missing session is caught immediately.
    """
    if expectation is None:
        return _metadata_unavailable(
            f"no exchange calendar was available for {symbol}, so what the feed owes is "
            "unknown; an unmeasurable freshness is not a fresh one",
            latest_observation_at,
        )
    if latest_observation_at is None:
        return _metadata_unavailable(
            f"no market observation is recorded for {symbol}", None, expectation
        )
    if latest_observation_at > observed_at:
        # A future stamp is a clock or ingestion defect, and treating it as the freshest possible
        # reading is the fail-open direction.
        return _metadata_unavailable(
            f"the newest {symbol} observation is stamped after the decision clock, which is a "
            "data defect rather than fresh data",
            latest_observation_at,
            expectation,
        )
    if expectation.last_completed_session is None:
        return _metadata_unavailable(
            f"the calendar records no completed session before this decision for {symbol}",
            latest_observation_at,
            expectation,
        )

    observed_session = session_of(latest_observation_at)
    expected_session = expectation.last_completed_session

    if observed_session < expected_session:
        return FreshnessAssessment(
            outcome=FreshnessOutcome.EXPECTED_SESSION_MISSING,
            policy_identity=expectation.policy_identity,
            cadence=expectation.cadence,
            market_state=expectation.market_state,
            expected_session=expected_session,
            observed_session=observed_session,
            latest_observation_at=latest_observation_at,
            detail=(
                f"the newest {symbol} observation is from the {observed_session.isoformat()} "
                f"session and {expected_session.isoformat()} has already closed; a session the "
                "market completed and the feed did not deliver is missing data, however little "
                "time has passed"
            ),
        )

    if (
        expectation.cadence is DataCadence.INTRADAY
        and expectation.market_state is MarketState.OPEN
    ):
        interval = expectation.expected_interval_seconds or 0
        ceiling = interval * tolerated_intervals
        age = int((observed_at - latest_observation_at).total_seconds())
        if age > ceiling:
            return FreshnessAssessment(
                outcome=FreshnessOutcome.FEED_STALE,
                policy_identity=expectation.policy_identity,
                cadence=expectation.cadence,
                market_state=expectation.market_state,
                expected_session=expected_session,
                observed_session=observed_session,
                latest_observation_at=latest_observation_at,
                detail=(
                    f"the {expectation.open_session} session is open and the newest {symbol} "
                    f"observation is {age}s old against an expected {interval}s cadence "
                    f"({tolerated_intervals} intervals tolerated); the feed has stopped while "
                    "the market has not"
                ),
            )

    return FreshnessAssessment(
        outcome=FreshnessOutcome.FRESH,
        policy_identity=expectation.policy_identity,
        cadence=expectation.cadence,
        market_state=expectation.market_state,
        expected_session=expected_session,
        observed_session=observed_session,
        latest_observation_at=latest_observation_at,
        detail=(
            f"{symbol} carries the {observed_session.isoformat()} session and the market is "
            f"{expectation.market_state.value.lower()}"
        ),
    )


def _metadata_unavailable(
    detail: str,
    latest_observation_at: datetime | None,
    expectation: FreshnessExpectation | None = None,
) -> FreshnessAssessment:
    return FreshnessAssessment(
        outcome=FreshnessOutcome.FRESHNESS_METADATA_UNAVAILABLE,
        policy_identity=None if expectation is None else expectation.policy_identity,
        cadence=DataCadence.DAILY_BAR if expectation is None else expectation.cadence,
        market_state=MarketState.CLOSED if expectation is None else expectation.market_state,
        expected_session=None if expectation is None else expectation.last_completed_session,
        observed_session=(
            None if latest_observation_at is None else session_of(latest_observation_at)
        ),
        latest_observation_at=latest_observation_at,
        detail=detail,
    )


__all__ = [
    "DataCadence",
    "MarketFreshnessPolicy",
    "FreshnessAssessment",
    "FreshnessExpectation",
    "FreshnessOutcome",
    "MarketState",
    "assess_freshness",
    "expected_freshness",
]
