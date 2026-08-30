"""Was there a public reason for this, and did anyone actually manage to look? (#56)

Enrichment is earned rather than assumed: the routing gate measured a 2.18x information-density
lift at a 1.76% selection rate before any of this was built, which is why it exists now.

**The three states are the substance, and two of them look identical from a distance.**

* `CATALYST_FOUND` -- a qualifying item was published before the event and was retrieved.
* `NO_PUBLIC_CATALYST_FOUND` -- every configured source was **successfully** consulted and none
  had anything. A real finding: unusual activity with no public explanation is the interesting
  case, and the one worth escalating.
* `CATALYST_UNKNOWN` -- somebody could not look. An outage, a rate limit, a stale feed, or an
  item whose publication time cannot be established.

**Never convert an outage into "no news".** That single confusion would turn every provider
incident into a discovery, and the discoveries would cluster exactly when the provider was
struggling -- which is when markets are busiest. So a single unhealthy source is enough to make
the whole assessment `UNKNOWN`, even when other sources returned items.

**A catalyst published after the event cannot explain it.** This is look-ahead wearing the
costume of explanation, and it is the easiest error to make here because the article genuinely is
about the move. Items published after the observation are excluded and counted, never silently
dropped.

**An item with no publication time is not a catalyst, it is an unknown.** It cannot be placed
relative to the event, so treating it as an explanation would be assuming the answer to the
question being asked.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: How far back an item may have been published and still count as this event's catalyst. A
#: project default under #72: long enough to catch an overnight release that moved the next
#: session, short enough that unrelated week-old news cannot be credited with today's move.
DEFAULT_LOOKBACK = timedelta(hours=48)


class CatalystOutcome(StrEnum):
    """What the lookup established. Two of these are findings; one is an absence of one."""

    CATALYST_FOUND = "CATALYST_FOUND"
    #: Every configured source was successfully consulted and none had anything qualifying.
    #: Unusual activity with no public explanation is the interesting case, not a null result.
    NO_PUBLIC_CATALYST_FOUND = "NO_PUBLIC_CATALYST_FOUND"
    #: Somebody could not look, or an item could not be placed in time. Never a finding.
    CATALYST_UNKNOWN = "CATALYST_UNKNOWN"


class SourceHealth(StrEnum):
    """Whether a source actually answered. Only `HEALTHY` permits a negative finding."""

    HEALTHY = "HEALTHY"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    #: Answered, but with data too old to cover the window asked about.
    STALE = "STALE"
    TIMEOUT = "TIMEOUT"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CatalystCandidate(FrozenModel):
    """One item a source returned, with the two timestamps kept apart.

    `published_at` is when the world learned it; `retrieved_at` is when Elrond did. Collapsing
    them is how a point-in-time system quietly stops being one -- the second says nothing about
    whether the item could have explained an earlier move.
    """

    source: Text
    external_id: Text
    #: `None` when the source did not supply one. That makes the item unplaceable rather than
    #: recent, and unplaceable is `CATALYST_UNKNOWN`.
    published_at: datetime | None
    retrieved_at: datetime
    headline: Text
    url: str | None = None
    #: Hash of the text this candidate was built from, so a later revision is detectable rather
    #: than silent. #83 asks for it because a second provider makes it necessary: two sources
    #: reporting the same story produce two candidates, and without a content identity there is
    #: no way to tell a genuine second report from the same wire copy counted twice.
    content_hash: Text
    #: Which parser produced this, and at what version. A re-parse under a new version is a new
    #: reading of the same item, and pooling the two would hide that the reading changed.
    parser: Text
    parser_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        for moment in (self.published_at, self.retrieved_at):
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError(
                    "catalyst timestamps must be timezone-aware; a naive one cannot be ordered "
                    "against an event observed in UTC"
                )
        return self


class SourceResult(FrozenModel):
    """What one source returned, and whether it was in a position to return anything."""

    source: Text
    health: SourceHealth
    candidates: tuple[CatalystCandidate, ...] = ()
    detail: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.health is not SourceHealth.HEALTHY and self.candidates:
            raise ValueError(
                f"{self.source} is {self.health.value} but returned candidates; a source that "
                "could not be consulted properly cannot also have answered completely"
            )
        if self.health is not SourceHealth.HEALTHY and not self.detail:
            raise ValueError(
                f"{self.source} is {self.health.value} without saying why; an unexplained "
                "outage is indistinguishable from a source nobody configured"
            )
        return self


class CatalystAssessment(FrozenModel):
    """Whether this anomaly had a public explanation, frozen and content-addressed."""

    version: int = Field(default=1, ge=1)
    anomaly_event_id: Text
    observed_at: datetime
    lookback_seconds: int = Field(gt=0)
    outcome: CatalystOutcome
    #: Every source consulted and how it fared. The denominator of the finding.
    sources: tuple[SourceResult, ...] = Field(min_length=1)
    #: Items that qualified. Only ever populated on `CATALYST_FOUND`.
    catalysts: tuple[CatalystCandidate, ...] = ()
    #: Items excluded because they were published after the event, counted rather than dropped.
    #: A non-zero count here beside `NO_PUBLIC_CATALYST_FOUND` is worth an operator's attention:
    #: the world explained the move, just not before it.
    published_after_event: int = 0
    #: Items with no usable publication time. Any of these forces `CATALYST_UNKNOWN`.
    unplaceable: int = 0
    detail: Text

    @model_validator(mode="after")
    def validate_assessment(self) -> Self:
        if self.outcome is CatalystOutcome.CATALYST_FOUND and not self.catalysts:
            raise ValueError("CATALYST_FOUND must name what was found")
        if self.outcome is not CatalystOutcome.CATALYST_FOUND and self.catalysts:
            raise ValueError(
                f"{self.outcome.value} carries catalysts; only a positive finding may name one"
            )
        unhealthy = [item for item in self.sources if item.health is not SourceHealth.HEALTHY]
        if unhealthy and self.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND:
            raise ValueError(
                f"{[item.source for item in unhealthy]} did not answer, so 'no public catalyst' "
                "is a claim about the sources rather than about the world; that confusion turns "
                "every provider outage into a discovery"
            )
        if self.unplaceable and self.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND:
            raise ValueError(
                "an item with no publication time cannot be placed relative to the event, so it "
                "cannot support a negative finding"
            )
        return self

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"catalyst-v{self.version}-{digest[:16]}"

    def as_row(self) -> dict[str, object]:
        return {
            "assessment_id": self.identity,
            "anomaly_event_id": self.anomaly_event_id,
            "observed_at": self.observed_at.isoformat(),
            "lookback_seconds": self.lookback_seconds,
            "outcome": self.outcome.value,
            "sources_consulted": ",".join(item.source for item in self.sources),
            "unhealthy_sources": ",".join(
                f"{item.source}={item.health.value}"
                for item in self.sources
                if item.health is not SourceHealth.HEALTHY
            ),
            "catalyst_ids": ",".join(item.external_id for item in self.catalysts),
            "published_after_event": self.published_after_event,
            "unplaceable": self.unplaceable,
            "detail": self.detail,
        }


def assess_catalysts(
    *,
    anomaly_event_id: str,
    observed_at: datetime,
    sources: Sequence[SourceResult],
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> CatalystAssessment:
    """Decide which of the three states this anomaly is in, from what the sources returned.

    The order of the checks is the design. Health is examined before content, because a source
    that could not answer makes the question unanswerable regardless of what the others found --
    and the tempting shortcut, "well, nobody found anything", is exactly the confusion that turns
    an outage into a discovery.
    """
    if not sources:
        raise ValueError(
            "an assessment with no sources consulted is not a negative finding; it is a lookup "
            "that never happened"
        )

    unhealthy = [item for item in sources if item.health is not SourceHealth.HEALTHY]
    everything = [item for result in sources for item in result.candidates]
    unplaceable = [item for item in everything if item.published_at is None]
    placeable = [item for item in everything if item.published_at is not None]
    qualifying = [
        item
        for item in placeable
        if item.published_at is not None
        and observed_at - lookback <= item.published_at <= observed_at
    ]
    later = [
        item
        for item in placeable
        if item.published_at is not None and item.published_at > observed_at
    ]

    if qualifying:
        return CatalystAssessment(
            anomaly_event_id=anomaly_event_id,
            observed_at=observed_at,
            lookback_seconds=int(lookback.total_seconds()),
            outcome=CatalystOutcome.CATALYST_FOUND,
            sources=tuple(sources),
            catalysts=tuple(sorted(qualifying, key=lambda item: item.external_id)),
            published_after_event=len(later),
            unplaceable=len(unplaceable),
            detail=(
                f"{len(qualifying)} item(s) published within {lookback} before the event"
            ),
        )

    if unhealthy:
        return CatalystAssessment(
            anomaly_event_id=anomaly_event_id,
            observed_at=observed_at,
            lookback_seconds=int(lookback.total_seconds()),
            outcome=CatalystOutcome.CATALYST_UNKNOWN,
            sources=tuple(sources),
            published_after_event=len(later),
            unplaceable=len(unplaceable),
            detail=(
                f"{', '.join(f'{item.source} is {item.health.value}' for item in unhealthy)}; "
                "no negative finding is available because the sources were not all consulted"
            ),
        )

    if unplaceable:
        return CatalystAssessment(
            anomaly_event_id=anomaly_event_id,
            observed_at=observed_at,
            lookback_seconds=int(lookback.total_seconds()),
            outcome=CatalystOutcome.CATALYST_UNKNOWN,
            sources=tuple(sources),
            published_after_event=len(later),
            unplaceable=len(unplaceable),
            detail=(
                f"{len(unplaceable)} item(s) carry no publication time and cannot be placed "
                "relative to the event, so whether a catalyst preceded it is unknown"
            ),
        )

    return CatalystAssessment(
        anomaly_event_id=anomaly_event_id,
        observed_at=observed_at,
        lookback_seconds=int(lookback.total_seconds()),
        outcome=CatalystOutcome.NO_PUBLIC_CATALYST_FOUND,
        sources=tuple(sources),
        published_after_event=len(later),
        unplaceable=0,
        detail=(
            f"{len(sources)} source(s) were consulted successfully and none published anything "
            f"qualifying within {lookback} before the event"
            + (
                f"; {len(later)} item(s) appeared afterwards and cannot explain it"
                if later
                else ""
            )
        ),
    )


__all__ = [
    "DEFAULT_LOOKBACK",
    "CatalystAssessment",
    "CatalystCandidate",
    "CatalystOutcome",
    "SourceHealth",
    "SourceResult",
    "assess_catalysts",
]
