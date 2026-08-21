"""The first connector that actually looks (#4).

`sources.py` has the provenance model -- `Source`, `Citation`, `EvidenceBasis`, the rule that a
derived artifact may never stand in for the thing it summarised. Nothing filled it. Every
hypothesis registered so far declares `DATA_DRIVEN_NO_EXTERNAL_SOURCE` whether or not literature
exists, because there was no way to find out. arXiv is the first place to look: a documented
public API, no credentials, and q-fin is where this project's subject matter lives.

Four rules, each guarding a way an acquisition layer manufactures a false research finding.

**A failed fetch is never an empty result.** A connector that returns `[]` on a timeout lets a
transient outage be recorded as "no literature exists" -- which is a research finding, and a
wrong one. Every failure path here raises `ScoutError`. There is deliberately no code path in
this module that turns an exception into an empty search.

**"We looked and found nothing" is a record, not silence.** `search()` returns a
`LiteratureSearch`, never a bare tuple, so a zero-result search still carries the query, the
connector and the time it ran. `LiteratureSearch` has no default for `query` or `searched_at`,
so "we never looked" cannot be constructed at all: its only expression is the absence of a
record, which is what keeps the two facts different.

**A connector may not raise epistemic status.** `search()` yields `Source` objects. Turning one
into a `Citation` stays `cite()`, at a call site that has to name the claim and say whether the
source supports or refutes it. Once something is fetching, source count is the cheapest thing in
this system to manufacture, and a connector that could stamp `LITERATURE_SUPPORTED` on twenty
hits would be manufacturing exactly that.

**Fetched text is data, never instruction.** Nothing here interprets, summarises or paraphrases
what arXiv returned. Title, authors and abstract are recorded as sent; the content hash covers
the entry as it arrived, so a later revision is a different source rather than a silent edit. A
paper whose text is addressed to whatever is reading it gets stored and quoted like any other.

arXiv asks for one request every three seconds. `HttpxScoutTransport` enforces that itself
rather than trusting callers, because the alternative is being cut off from a free public
service -- and the cut-off arrives as a plain `Rate exceeded.` in place of the feed, which this
module treats as a protocol failure rather than as an absence of papers.
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import Field, ValidationError

from quantbot.research.sources import FrozenModel, Source, SourceKind

#: HTTPS rather than the documented http:// base. Same service, and a plaintext hop is a
#: place for someone else's bytes to become our provenance.
ARXIV_QUERY_URL = "https://export.arxiv.org/api/query"

#: arXiv's published guidance is one request every three seconds. Enforced, not documented.
ARXIV_MIN_INTERVAL_SECONDS = 3.0

#: A real identity with somewhere to complain to. A free public API is entitled to know who is
#: calling it.
USER_AGENT = "elrond-source-scout/1.0 (+https://github.com/jfhutchi/project_elrond)"

#: A search of 100 abstracts is well under a megabyte. The cap is a bound on what an untrusted
#: host can make this process allocate, not a tuning knob.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

#: arXiv reports a bad query as an ordinary-looking `<entry>` titled "Error". A parser that does
#: not know this stores a paper called Error, with an abstract, and cites it.
ARXIV_ERROR_MARKER = "arxiv.org/api/errors#"

_WHITESPACE = re.compile(r"\s+")


class ScoutError(RuntimeError):
    """Base for every way source acquisition fails. Never an empty result set."""


class SourceUnavailable(ScoutError):
    """The source could not be reached. Transient, and emphatically not "no literature".

    Separate from `SourceProtocolError` because the two mean different things to a caller: this
    one says try again, and neither one says anything about what has been published.
    """


class SourceProtocolError(ScoutError):
    """The source answered with something that is not the schema it documents."""


class ScoutTransport(Protocol):
    """Injected so parsing and rate limiting are exercised without a network.

    Bytes rather than text: the content hash has to cover what arrived, and a decode step
    between the wire and the hash is a place for the two to stop matching.
    """

    def fetch(self, url: str, params: Mapping[str, str], *, timeout_seconds: float) -> bytes: ...


class HttpxScoutTransport:
    """Map the scout's request contract onto httpx, and hold arXiv's rate limit.

    The clock and the sleep are injected so the throttle is asserted in tests rather than
    waited out. `min_interval_seconds` applies per transport instance, which is per host in
    practice because a transport is constructed for one connector.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        user_agent: str = USER_AGENT,
        min_interval_seconds: float = ARXIV_MIN_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client or httpx.Client()
        self._user_agent = user_agent
        self._min_interval = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None

    def fetch(self, url: str, params: Mapping[str, str], *, timeout_seconds: float) -> bytes:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._wait_out_rate_limit()
        try:
            response = self._client.get(
                url,
                params=dict(params),
                headers={"User-Agent": self._user_agent},
                timeout=timeout_seconds,
            )
        except httpx.TransportError as error:
            # Not `return b""`. A timeout that reads as an empty search is a false finding.
            raise SourceUnavailable(f"source transport failed for {url}") from error
        finally:
            # A request that failed still reached the host, so it still spends the interval.
            self._last_request = self._monotonic()
        if response.status_code != 200:
            raise SourceUnavailable(f"{url} answered HTTP {response.status_code}")
        return response.content

    def _wait_out_rate_limit(self) -> None:
        if self._last_request is None:
            return
        remaining = self._min_interval - (self._monotonic() - self._last_request)
        if remaining > 0:
            self._sleep(remaining)

    def close(self) -> None:
        self._client.close()


class LiteratureSearch(FrozenModel):
    """One search that ran, including one that found nothing.

    `query` and `searched_at` have no defaults on purpose. A search that returned no papers is
    this record with an empty `results`; a search that was never run has no record at all. There
    is no third shape, so the two cannot be confused for one another downstream.
    """

    connector: str
    query: str
    searched_at: datetime
    #: What the source said it holds, which may exceed what it returned in this page.
    total_matched: int = Field(ge=0)
    results: tuple[Source, ...] = ()

    @property
    def found_nothing(self) -> bool:
        """We looked. There was nothing. Not the same fact as never having looked."""
        return not self.results


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ArxivScout:
    """Search arXiv, and record what came back with enough provenance to check it later."""

    connector = "arxiv"
    #: Pins how an entry is canonicalised before hashing, so a stored hash stays reproducible.
    parser_version = "arxiv-atom-v1"

    def __init__(
        self,
        transport: ScoutTransport,
        *,
        url: str = ARXIV_QUERY_URL,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._transport = transport
        self._url = url
        self._timeout = timeout_seconds
        self._now = now

    def search(self, query: str, *, max_results: int = 10) -> LiteratureSearch:
        """Run one query. Raises on any failure; never reports an outage as an absence."""
        if not query.strip():
            raise ValueError("a search needs a query; an empty one records nothing")
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")
        body = self._transport.fetch(
            self._url,
            {
                "search_query": query,
                "start": "0",
                "max_results": str(max_results),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
            timeout_seconds=self._timeout,
        )
        return parse_feed(
            body,
            query=query,
            connector=self.connector,
            retrieved_at=self._now(),
            parser_version=self.parser_version,
        )


def parse_feed(
    body: bytes,
    *,
    query: str,
    connector: str,
    retrieved_at: datetime,
    parser_version: str,
) -> LiteratureSearch:
    """Turn an arXiv Atom feed into sources, refusing anything that is not one."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceProtocolError(
            f"response is {len(body)} bytes, over the {MAX_RESPONSE_BYTES} cap"
        )
    # ElementTree expands internal entities, so a DTD is a decompression bomb waiting to be
    # parsed. arXiv never sends one; refusing costs nothing and closes the hole without a
    # third-party parser.
    if b"<!DOCTYPE" in body[:1024].upper():
        raise SourceProtocolError("response carries a DOCTYPE; refusing to expand it")
    try:
        feed = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        # `Rate exceeded.` arrives here. A throttle is not an absence of papers.
        raise SourceProtocolError("response is not XML") from error
    if feed.tag != f"{ATOM}feed":
        raise SourceProtocolError(f"expected an Atom feed, got {feed.tag}")

    entries = feed.findall(f"{ATOM}entry")
    for entry in entries:
        _refuse_error_entry(entry)

    return LiteratureSearch(
        connector=connector,
        query=query,
        searched_at=retrieved_at,
        total_matched=_total_matched(feed),
        results=tuple(
            _source_from_entry(entry, retrieved_at=retrieved_at, parser_version=parser_version)
            for entry in entries
        ),
    )


def _refuse_error_entry(entry: ElementTree.Element) -> None:
    """arXiv reports query errors as a result row. Storing one would invent a paper."""
    identifier = entry.findtext(f"{ATOM}id", default="")
    if ARXIV_ERROR_MARKER in identifier:
        reported = _collapse(entry.findtext(f"{ATOM}summary", default="")) or identifier
        raise SourceProtocolError(f"arXiv reported an error rather than results: {reported}")


def _total_matched(feed: ElementTree.Element) -> int:
    raw = feed.findtext(f"{OPENSEARCH}totalResults")
    if raw is None:
        raise SourceProtocolError("feed omits opensearch:totalResults")
    try:
        return int(raw.strip())
    except ValueError as error:
        raise SourceProtocolError(f"unreadable totalResults {raw!r}") from error


def _source_from_entry(
    entry: ElementTree.Element, *, retrieved_at: datetime, parser_version: str
) -> Source:
    uri = _collapse(_required(entry, f"{ATOM}id", "entry id"))
    identifier = uri.rsplit("/abs/", 1)[-1] if "/abs/" in uri else ""
    if not identifier:
        raise SourceProtocolError(f"entry id {uri!r} is not an arXiv abstract URI")
    published = _timestamp(_required(entry, f"{ATOM}published", f"published time for {uri}"))
    authors = tuple(
        _collapse(name)
        for author in entry.findall(f"{ATOM}author")
        if (name := author.findtext(f"{ATOM}name")) is not None and name.strip()
    )
    try:
        return Source(
            # The version suffix is part of the identity: v2 is a different artifact from v1,
            # so a revision lands beside the vintage an experiment cited instead of over it.
            source_id=f"arxiv:{identifier}",
            kind=SourceKind.PAPER,
            uri=uri,
            title=_collapse(_required(entry, f"{ATOM}title", f"title for {uri}")),
            publisher="arXiv",
            author=", ".join(authors),
            published_at=published,
            retrieved_at=retrieved_at,
            # Over the entry as it arrived, canonicalised by `parser_version`. Any change to
            # the title, the abstract or the DOI shows up as a different hash.
            content_hash=hashlib.sha256(ElementTree.tostring(entry, encoding="utf-8")).hexdigest(),
            parser_version=parser_version,
            # The publisher's own abstract, verbatim. Not a summary of it -- a derived artifact
            # here would be uncitable by `cite()`, and rightly so.
            excerpt=_collapse(entry.findtext(f"{ATOM}summary", default="")),
        )
    except ValidationError as error:
        raise SourceProtocolError(f"entry {uri} is not usable provenance: {error}") from error


def _required(entry: ElementTree.Element, tag: str, what: str) -> str:
    value = entry.findtext(tag)
    if value is None or not value.strip():
        raise SourceProtocolError(f"entry omits {what}")
    return value


def _timestamp(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.strip()).astimezone(UTC)
    except ValueError as error:
        raise SourceProtocolError(f"unreadable timestamp {raw!r}") from error


def _collapse(value: str) -> str:
    """Fold the wrapping arXiv puts in titles and abstracts. Whitespace only -- no rewording."""
    return _WHITESPACE.sub(" ", value).strip()


__all__ = [
    "ARXIV_MIN_INTERVAL_SECONDS",
    "ARXIV_QUERY_URL",
    "MAX_RESPONSE_BYTES",
    "USER_AGENT",
    "ArxivScout",
    "HttpxScoutTransport",
    "LiteratureSearch",
    "ScoutError",
    "ScoutTransport",
    "SourceProtocolError",
    "SourceUnavailable",
    "parse_feed",
]
