"""Was a rule actually progressing, and can that be said without it becoming a trade? (#59)

The issue is explicit about the flow this must never support:

    bill advanced committee -> BUY SYMBOL

and equally explicit about the one it should:

    policy event -> theme context -> optional hypothesis enrichment -> independent candidate

So almost everything here is about *not* concluding. A policy event records what an authoritative
source published and when; it does not say which symbols are affected, and it does not say which
way. Both of those are inferences, and inference is precisely where a policy record turns into a
trading signal.

**Three timestamps, kept apart.** `published_at` is when the world could know. `effective_on` is
when the rule actually bites, which is often months later and sometimes never. `retrieved_at` is
when Elrond looked. Collapsing any two is how a point-in-time system stops being one -- and the
specific failure the issue names first is a stale article about an old action being read as a
fresh one, which is exactly what `published_at` distinguishes.

**Stage is not a detail.** A proposed rule is a request for comment that may never become
anything; a final rule is law with a date. The issue lists the distinction among its failure
modes because treating them alike is how legislative noise becomes apparent signal.

**No model reads this.** The parsing is deterministic, so #45's contamination problem -- a model
asked about a 2020 rule already knows how it turned out -- cannot arise here. Any later component
that *does* infer from these records has to declare itself and be registered separately.

Source: the Federal Register API, which is authoritative for US agency rulemaking, needs no
credential, and publishes the dates rather than leaving them to be inferred. Verified reachable
2026-08-30. Congress.gov would add legislative stages and needs a key nobody has supplied; the
artifact is shaped so that source drops in beside this one rather than replacing it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCE_NAME = "federal-register"
DOCUMENTS_URL = "https://www.federalregister.gov/api/v1/documents.json"

#: Bumped when the parsing changes. In the identity, so a re-parse under a new version is a new
#: record rather than a silent revision of an existing one -- the rule #84 set for dependence and
#: #59 sets for regime, applied here.
PARSER_VERSION = 1


class PolicyStage(StrEnum):
    """How far along an action is. The distinction the issue names as a failure mode."""

    #: Published for comment. May never become anything, and frequently does not.
    PROPOSED_RULE = "PROPOSED_RULE"
    #: Final and dated. The only stage that is actually law.
    FINAL_RULE = "FINAL_RULE"
    #: An announcement that is neither: meetings, findings, requests for information.
    NOTICE = "NOTICE"
    #: A presidential document.
    PRESIDENTIAL = "PRESIDENTIAL"
    #: The source said something this parser does not recognise. Never guessed at.
    UNRECOGNISED = "UNRECOGNISED"


class PolicyDirection(StrEnum):
    """Which way an action cuts. `UNKNOWN` is the only value this module ever assigns.

    Populating this is inference, and inference is where a policy record becomes a trading
    signal. The field exists because the issue specifies it and because a later, separately
    registered component may fill it -- not because anything here is entitled to.
    """

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


_STAGE_BY_TYPE: Mapping[str, PolicyStage] = {
    "Rule": PolicyStage.FINAL_RULE,
    "Proposed Rule": PolicyStage.PROPOSED_RULE,
    "Notice": PolicyStage.NOTICE,
    "Presidential Document": PolicyStage.PRESIDENTIAL,
}


class PolicyThemeEvent(BaseModel):
    """One authoritative policy action, recorded without being interpreted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    #: The publisher's own stable identifier, so the record can be fetched again and checked.
    source_document_id: str
    source_uri: str
    title: str
    agencies: tuple[str, ...]
    stage: PolicyStage
    #: When the world could know. The date staleness is judged against.
    published_at: date
    #: When it starts to bite. `None` for anything not yet dated, which includes every proposed
    #: rule -- and the gap between this and `published_at` is often months.
    effective_on: date | None
    #: When Elrond looked. Never a substitute for `published_at`.
    retrieved_at: datetime
    parser_version: int = Field(default=PARSER_VERSION, ge=1)
    #: Always `UNKNOWN` from this module. See `PolicyDirection`.
    inferred_direction: PolicyDirection = PolicyDirection.UNKNOWN
    #: Deliberately empty. Mapping an agency to sectors is the over-assignment the issue warns
    #: about, and a mapping that tagged every technology name would make every anomaly look
    #: policy-adjacent.
    potentially_affected_sectors: tuple[str, ...] = ()
    potentially_affected_symbols: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if self.effective_on is not None and self.effective_on < self.published_at:
            raise ValueError(
                "a rule cannot take effect before it was published; check which date the source "
                "supplied, because reading one as the other is how a stale action reads as fresh"
            )
        if self.inferred_direction is not PolicyDirection.UNKNOWN and not self.reasoned:
            raise ValueError(
                "a direction without a declared method behind it is a trading opinion wearing a "
                "policy record's clothes; register the inference separately"
            )
        if self.potentially_affected_symbols and not self.potentially_affected_sectors:
            raise ValueError(
                "symbols without the sector reasoning that produced them cannot be audited for "
                "over-assignment, which is the failure this field is most prone to"
            )
        return self

    @property
    def reasoned(self) -> bool:
        """Whether anything justifies a direction. Nothing in this module ever sets one."""
        return False

    def is_stale(self, *, as_of: date, freshness_days: int) -> bool:
        """Whether this action is too old to be treated as news on `as_of`.

        The issue's first named failure mode: a recycled article about an old committee action.
        Judged on `published_at` rather than on when the record was fetched, because the fetch
        date is a fact about Elrond and the publication date is a fact about the world.
        """
        return (as_of - self.published_at).days > freshness_days

    @property
    def identity(self) -> str:
        """Content-addressed over the source's identifier and this parser's version."""
        return f"policy-{self.source}-v{self.parser_version}-{self.source_document_id}"

    def as_row(self) -> dict[str, object]:
        return {
            "policy_event_id": self.identity,
            "source": self.source,
            "source_document_id": self.source_document_id,
            "source_uri": self.source_uri,
            "title": self.title,
            "agencies": ",".join(self.agencies),
            "stage": self.stage.value,
            "published_at": self.published_at.isoformat(),
            "effective_on": None if self.effective_on is None else self.effective_on.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "parser_version": self.parser_version,
            "inferred_direction": self.inferred_direction.value,
            "potentially_affected_sectors": ",".join(self.potentially_affected_sectors),
            "potentially_affected_symbols": ",".join(self.potentially_affected_symbols),
        }


class PolicyTransport(Protocol):
    """The same injected-transport shape the other sources use."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> Any: ...


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def events_from_payload(
    payload: Mapping[str, object], *, retrieved_at: datetime
) -> tuple[PolicyThemeEvent, ...]:
    """Map a Federal Register response onto events, dropping what cannot be placed in time.

    An item with no publication date is discarded rather than dated to the retrieval, for the
    same reason an undated news item is `CATALYST_UNKNOWN` rather than recent: a record that
    cannot be ordered against a decision cannot inform one, and giving it Elrond's clock would
    silently make it look contemporaneous with whatever asked.
    """
    results = payload.get("results")
    if not isinstance(results, list):
        return ()
    events = []
    for item in results:
        if not isinstance(item, dict):
            continue
        published = _parse_date(item.get("publication_date"))
        identifier = item.get("document_number")
        title = item.get("title")
        if published is None or not identifier or not isinstance(title, str):
            continue
        effective = _parse_date(item.get("effective_on"))
        if effective is not None and effective < published:
            # The source occasionally carries an effective date from a corrected earlier
            # document. Dropping the field is honest; keeping it would fail validation and
            # discard an otherwise usable record.
            effective = None
        agencies = tuple(
            str(agency.get("name", "")).strip()
            for agency in item.get("agencies", [])
            if isinstance(agency, dict) and agency.get("name")
        )
        events.append(
            PolicyThemeEvent(
                source=SOURCE_NAME,
                source_document_id=str(identifier),
                source_uri=str(item.get("html_url") or ""),
                title=title.strip()[:2000],
                agencies=agencies,
                stage=_STAGE_BY_TYPE.get(
                    str(item.get("type", "")), PolicyStage.UNRECOGNISED
                ),
                published_at=published,
                effective_on=effective,
                retrieved_at=retrieved_at,
            )
        )
    return tuple(events)


class FederalRegisterSource:
    """Authoritative US agency rulemaking, with the dates published rather than inferred."""

    name = SOURCE_NAME

    def __init__(
        self, transport: PolicyTransport, *, timeout_seconds: Decimal = Decimal("15")
    ) -> None:
        self._transport = transport
        self._timeout = timeout_seconds

    async def fetch(
        self, *, since: date, as_of: date, retrieved_at: datetime, limit: int = 50
    ) -> tuple[PolicyThemeEvent, ...]:
        """Fetch actions published in a window, or return nothing and say nothing.

        Bounded at `as_of` at the request, so the query itself cannot ask for the future -- the
        same construction the news source uses, and for the same reason: filtering afterwards is
        one refactor away from look-ahead.

        Returns an empty tuple on any failure. Unlike catalyst enrichment, an empty result here
        is not load-bearing: nothing concludes 'no policy activity' from silence, because nothing
        concludes anything from this artifact at all. It is context for a hypothesis a person or
        a registered component will write.
        """
        params = {
            "per_page": str(min(limit, 100)),
            "order": "newest",
            "conditions[publication_date][gte]": since.isoformat(),
            "conditions[publication_date][lte]": as_of.isoformat(),
        }
        try:
            response = await self._transport.request(
                "GET", DOCUMENTS_URL, headers={}, params=params, timeout_seconds=self._timeout
            )
            if getattr(response, "status_code", 0) != 200:
                return ()
            payload = json.loads(getattr(response, "content", b"") or b"{}")
        except Exception:  # noqa: BLE001 -- context, never a finding; see the docstring
            return ()
        if not isinstance(payload, dict):
            return ()
        return events_from_payload(payload, retrieved_at=retrieved_at)


def fresh_events(
    events: Sequence[PolicyThemeEvent], *, as_of: date, freshness_days: int
) -> tuple[PolicyThemeEvent, ...]:
    """Only the actions recent enough to be treated as news on `as_of`.

    Separated from fetching so the staleness rule is testable without a transport, and so the
    window is a decision a caller makes explicitly rather than one buried in a query string.
    """
    return tuple(
        event
        for event in events
        if not event.is_stale(as_of=as_of, freshness_days=freshness_days)
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "DOCUMENTS_URL",
    "PARSER_VERSION",
    "SOURCE_NAME",
    "FederalRegisterSource",
    "PolicyDirection",
    "PolicyStage",
    "PolicyThemeEvent",
    "PolicyTransport",
    "events_from_payload",
    "fresh_events",
    "utc_now",
]
