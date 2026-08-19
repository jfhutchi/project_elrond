"""External evidence, kept distinguishable from evidence this project measured (#4).

Three rules, each hardened past "metadata" because each guards a way to manufacture a false
edge.

**A derived artifact may never be cited in place of the source.** An LLM summary that reads
authoritatively is worse than no artifact, because it stops the search. This project has the
failure at one remove: a Corwin-Schultz spread estimator gave 34bps for SPY against a real
0.26bps -- wrong by two orders of magnitude, believed until someone pulled real quotes. So a
`derived` source raises when cited, rather than being discouraged in a docstring.

**Event time and retrieval time are separate fields.** Look-ahead is the most reliable way to
manufacture a fake edge and it is invisible in results: a leaky backtest looks like a discovery,
not a bug. Storing when a thing *happened* apart from when we *fetched* it is what makes the
mechanical check in #7 possible at all, and a source whose event time follows its retrieval time
is refused outright.

**Agreement decides whether a question is worth testing, never what its answer is.** Twenty
papers reporting an anomaly is twenty papers, and published quant findings have a documented
replication problem biased toward positive results -- which is the direction that costs money.
`worth_testing()` returns a boolean about spending trials; there is deliberately no function
here that turns source count into a prior, a weight, or a confidence.

`REFUTED.md` #13 is the standing case for the epistemic split: social-sentiment strategies were
rejected on published evidence rather than measurement, which was a defensible call and is a
different fact from the other 23 entries. `MEASURED` is the only status that satisfies a
promotion gate; the literature statuses never do, anywhere.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SourceKind(StrEnum):
    PAPER = "PAPER"
    SEC_FILING = "SEC_FILING"
    MACRO_RELEASE = "MACRO_RELEASE"
    MARKET_DATA = "MARKET_DATA"
    NEWS = "NEWS"
    #: Anything the operator explicitly approved. Not a catch-all for scraping.
    ALTERNATIVE = "ALTERNATIVE"


class EpistemicStatus(StrEnum):
    """Where a claim's support comes from. Only one of these is Elrond's own evidence."""

    LITERATURE_SUPPORTED = "LITERATURE_SUPPORTED"
    LITERATURE_REFUTED = "LITERATURE_REFUTED"
    #: Elrond measured it. The only status that may satisfy a promotion gate.
    MEASURED = "MEASURED"
    UNTESTED = "UNTESTED"
    #: The candidate came from mining data rather than from any external source. An honest
    #: answer to "what backs this", and the only alternative to a citation.
    DATA_DRIVEN_NO_EXTERNAL_SOURCE = "DATA_DRIVEN_NO_EXTERNAL_SOURCE"


#: Statuses that are not measurement and must never gate promotion.
NOT_MEASURED = frozenset(
    {
        EpistemicStatus.LITERATURE_SUPPORTED,
        EpistemicStatus.LITERATURE_REFUTED,
        EpistemicStatus.UNTESTED,
        EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE,
    }
)


class SourceHealth(StrEnum):
    OK = "OK"
    #: Fetched long enough ago that the underlying record may have moved.
    STALE = "STALE"
    #: The publisher revised it after we stored this vintage.
    REVISED = "REVISED"
    #: The URI no longer resolves, or the hash no longer matches.
    UNVERIFIABLE = "UNVERIFIABLE"


class DerivedArtifactCited(ValueError):
    """Raised when a summary is offered in place of the thing it summarised."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Source(FrozenModel):
    """One external artifact, with enough provenance to fetch it again and check it."""

    source_id: Text
    kind: SourceKind
    uri: Text
    title: Text
    publisher: Text
    author: str = ""
    #: When the thing described actually happened or was published. Not when we saw it.
    published_at: datetime
    #: When Elrond fetched it. A backtest may not use this source before `published_at`.
    retrieved_at: datetime
    #: Hash of the bytes as stored, so a later revision is detectable rather than silent.
    content_hash: Text
    license: Text = "unrecorded"
    parser_version: Text
    excerpt: str = ""
    health: SourceHealth = SourceHealth.OK
    #: True for anything produced *from* a source -- an LLM summary, a translation, an
    #: extraction. Such an artifact may be stored and read; it may not be cited as evidence.
    derived: bool = False
    derived_from: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if self.retrieved_at < self.published_at:
            raise ValueError(
                "a source cannot be retrieved before it was published; check the event time"
            )
        if self.derived and self.derived_from is None:
            raise ValueError("a derived artifact must name the source it came from")
        if not self.derived and self.derived_from is not None:
            raise ValueError("only a derived artifact has a source it came from")
        return self

    @property
    def citable(self) -> bool:
        """Whether this may back a claim. A summary never can."""
        return not self.derived and self.health is not SourceHealth.UNVERIFIABLE

    def known_by(self, as_of: datetime) -> bool:
        """Whether this was knowable at a point in time, for the #7 look-ahead check."""
        return self.published_at <= as_of


class Citation(FrozenModel):
    """One claim, and the source that backs it."""

    claim: Text
    source_id: Text
    #: Where in the source. A page, a section, a line -- anything a reader can follow.
    locator: Text
    status: EpistemicStatus

    @model_validator(mode="after")
    def validate_citation(self) -> Self:
        if self.status is EpistemicStatus.MEASURED:
            raise ValueError(
                "a citation records what a source claims; MEASURED is what Elrond found, and "
                "cannot come from a citation"
            )
        return self


class EvidenceBasis(FrozenModel):
    """What backs a hypothesis before anything has been measured.

    Either citations, or an explicit statement that there are none. Silence is not an option:
    "we did not look" and "we looked and there is nothing" are different facts.
    """

    citations: tuple[Citation, ...] = ()
    status: EpistemicStatus

    @model_validator(mode="after")
    def validate_basis(self) -> Self:
        if self.status is EpistemicStatus.MEASURED:
            raise ValueError(
                "MEASURED is what an experiment found, not what a hypothesis starts with; a "
                "basis records what backs the question before anything was measured"
            )
        if self.status is EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE:
            if self.citations:
                raise ValueError(
                    "a data-driven candidate cannot also carry citations; pick the one that is "
                    "true"
                )
            return self
        if not self.citations:
            raise ValueError(
                "a hypothesis must carry attributable source evidence or be explicitly "
                "classified DATA_DRIVEN_NO_EXTERNAL_SOURCE"
            )
        return self

    @property
    def gates_promotion(self) -> bool:
        """Always False. A basis is what a question started from, never what it proved.

        Kept as a property rather than omitted so the answer is explicit at the call site: a
        hypothesis's sources do not become its evidence, no matter how many there are.
        """
        return False


def cite(source: Source, *, claim: str, locator: str, status: EpistemicStatus) -> Citation:
    """Build a citation, refusing a derived artifact.

    The hard gate. A summary that reads authoritatively is worse than no artifact because it
    stops the search -- the Corwin-Schultz estimate was believed at 34bps against a real
    0.26bps until someone pulled real quotes.
    """
    if source.derived:
        raise DerivedArtifactCited(
            f"{source.source_id} is derived from {source.derived_from}; cite the source, not "
            "the summary of it"
        )
    if source.health is SourceHealth.UNVERIFIABLE:
        raise DerivedArtifactCited(
            f"{source.source_id} is unverifiable and cannot back a claim"
        )
    return Citation(claim=claim, source_id=source.source_id, locator=locator, status=status)


def gates_promotion(status: EpistemicStatus) -> bool:
    """Whether a status may satisfy a promotion gate. Only Elrond's own measurement may."""
    return status not in NOT_MEASURED


def worth_testing(citations: Sequence[Citation]) -> bool:
    """Whether published work makes a question worth spending trials on.

    Deliberately a boolean. There is no function here returning a weight, a prior or a
    confidence from source count: published quant findings replicate poorly and the bias runs
    toward positive results, which is the direction that costs money. Twenty papers make a
    question worth asking; they do not make its answer more likely to be yes.
    """
    return any(
        citation.status is EpistemicStatus.LITERATURE_SUPPORTED for citation in citations
    )


def contradicted_by_literature(citations: Sequence[Citation]) -> tuple[str, ...]:
    """Claims some source says are already refuted, which is cheaper to learn than to measure."""
    return tuple(
        citation.claim
        for citation in citations
        if citation.status is EpistemicStatus.LITERATURE_REFUTED
    )


_PUNCTUATION = re.compile(r"[^a-z0-9]+")


def _normalized(title: str) -> str:
    return _PUNCTUATION.sub(" ", title.lower()).strip()


def fingerprint(source: Source) -> str:
    """Near-duplicate key: same work, different host or different PDF build."""
    return hashlib.sha256(
        f"{_normalized(source.title)}|{_normalized(source.publisher)}".encode()
    ).hexdigest()


class SourceIndex:
    """Stores sources once, and knows when two of them are the same work."""

    def __init__(self, sources: Iterable[Source] = ()) -> None:
        self._by_id: dict[str, Source] = {}
        self._by_content: dict[str, str] = {}
        self._by_fingerprint: dict[str, str] = {}
        for source in sources:
            self.add(source)

    def __len__(self) -> int:
        return len(self._by_id)

    def add(self, source: Source) -> bool:
        """Store a source. Returns False when it duplicates one already held."""
        if source.source_id in self._by_id:
            return False
        if source.content_hash in self._by_content:
            return False
        if fingerprint(source) in self._by_fingerprint:
            return False
        self._by_id[source.source_id] = source
        self._by_content[source.content_hash] = source.source_id
        self._by_fingerprint[fingerprint(source)] = source.source_id
        return True

    def get(self, source_id: str) -> Source | None:
        return self._by_id.get(source_id)

    def duplicate_of(self, source: Source) -> str | None:
        return self._by_content.get(source.content_hash) or self._by_fingerprint.get(
            fingerprint(source)
        )

    def knowable_at(self, as_of: datetime) -> tuple[Source, ...]:
        """Sources that existed at a point in time, for anti-look-ahead checks."""
        return tuple(
            source for source in self._by_id.values() if source.known_by(as_of)
        )

    def snapshot(self) -> str:
        """Content address of the whole set, so an experiment can name what it read."""
        digest = hashlib.sha256()
        for source_id in sorted(self._by_id):
            digest.update(self._by_id[source_id].content_hash.encode("utf-8"))
        return digest.hexdigest()

    def revised(self, source_id: str, *, content_hash: str) -> bool:
        """Whether a publisher changed something under a vintage we already stored.

        A revised macro series does not overwrite the vintage a historical experiment used;
        it is a different source, and this reports the divergence rather than hiding it.
        """
        held = self._by_id.get(source_id)
        return held is not None and held.content_hash != content_hash


__all__ = [
    "NOT_MEASURED",
    "Citation",
    "DerivedArtifactCited",
    "EpistemicStatus",
    "EvidenceBasis",
    "Source",
    "SourceHealth",
    "SourceIndex",
    "SourceKind",
    "cite",
    "contradicted_by_literature",
    "fingerprint",
    "gates_promotion",
    "worth_testing",
]
