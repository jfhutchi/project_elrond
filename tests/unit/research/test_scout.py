"""Acquiring literature without letting an outage read as an absence of literature.

Every fixture in `tests/fixtures/arxiv_*` is a real response captured from
`export.arxiv.org`, including the two that matter most: the feed arXiv returns when a query
matches nothing, and the one it returns when the query itself is bad -- which is an ordinary
looking `<entry>` titled "Error". A parser that does not know the second one stores a paper
called Error and cites it. `arxiv_rate_exceeded.txt` is what the host actually sent when this
connector's fixtures were being collected too quickly.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from quantbot.research.scout import (
    ARXIV_MIN_INTERVAL_SECONDS,
    MAX_RESPONSE_BYTES,
    ArxivScout,
    HttpxScoutTransport,
    LiteratureSearch,
    SourceProtocolError,
    SourceUnavailable,
    parse_feed,
)
from quantbot.research.sources import (
    EpistemicStatus,
    Source,
    SourceHealth,
    SourceIndex,
    SourceKind,
    cite,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FETCHED = datetime(2026, 8, 20, 23, 59, 4, tzinfo=UTC)


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def parsed(body: bytes, *, query: str = "cat:q-fin.ST") -> LiteratureSearch:
    return parse_feed(
        body,
        query=query,
        connector="arxiv",
        retrieved_at=FETCHED,
        parser_version="arxiv-atom-v1",
    )


class ScriptedTransport:
    """Answers with a captured response. Records what it was asked for."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch(self, url: str, params: Mapping[str, str], *, timeout_seconds: float) -> bytes:
        self.calls.append((url, dict(params)))
        return self.body


class ExplodingTransport:
    """Fails the way a network fails."""

    def fetch(self, url: str, params: Mapping[str, str], *, timeout_seconds: float) -> bytes:
        raise SourceUnavailable("source transport failed")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def has_epistemic_status_field(model: type[BaseModel]) -> bool:
    return any(field.annotation is EpistemicStatus for field in model.model_fields.values())


def test_a_fetched_paper_records_what_was_retrieved_and_when() -> None:
    """A citation with no retrieval timestamp is not provenance.

    Event time and retrieval time are separate fields and both are filled from what actually
    happened: `published_at` from the feed, `retrieved_at` from the clock at fetch time.
    """
    transport = ScriptedTransport(read("arxiv_qfin_entry.xml"))
    scout = ArxivScout(transport, now=lambda: FETCHED)

    search = scout.search("cat:q-fin.ST", max_results=1)

    assert search.connector == "arxiv"
    assert search.query == "cat:q-fin.ST"
    assert search.searched_at == FETCHED
    assert search.total_matched == 4332
    assert len(search.results) == 1

    paper = search.results[0]
    assert paper.source_id == "arxiv:2012.10145v1"
    assert paper.kind is SourceKind.PAPER
    assert paper.uri == "http://arxiv.org/abs/2012.10145v1"
    assert paper.title == "Heavy tailed distributions in closing auctions"
    assert paper.publisher == "arXiv"
    assert paper.author == "M. Derksen, B. Kleijn, R. de Vilder"
    # Event time, from the feed. Six years before the retrieval, and separately recorded.
    assert paper.published_at == datetime(2020, 12, 18, 10, 12, 41, tzinfo=UTC)
    # Retrieval time, from the clock. This is the field that makes it provenance.
    assert paper.retrieved_at == FETCHED
    assert paper.published_at < paper.retrieved_at
    assert paper.known_by(datetime(2021, 1, 1, tzinfo=UTC)) is True
    assert paper.known_by(datetime(2020, 1, 1, tzinfo=UTC)) is False
    assert len(paper.content_hash) == 64
    assert paper.parser_version == "arxiv-atom-v1"
    # The publisher's own abstract, verbatim. Not a summary of it.
    assert paper.excerpt.startswith("We study the tails of closing auction return distributions")
    assert "limit orders are submitted so as to counter existing market order" in paper.excerpt
    assert paper.derived is False
    assert paper.health is SourceHealth.OK
    assert paper.citable is True

    url, params = transport.calls[0]
    assert url.endswith("/api/query")
    assert params["search_query"] == "cat:q-fin.ST"
    assert params["max_results"] == "1"


def test_a_search_that_found_nothing_is_still_a_record_of_having_looked() -> None:
    """Zero results is a finding. It has to survive as one, with the query and the time."""
    scout = ArxivScout(ScriptedTransport(read("arxiv_no_results.xml")), now=lambda: FETCHED)

    search = scout.search('all:"zzqxjwl nonexistent phrase"')

    assert isinstance(search, LiteratureSearch)
    assert search.found_nothing is True
    assert search.results == ()
    assert search.total_matched == 0
    # The record still says what was asked and when, which is the whole point of it existing.
    assert search.query == 'all:"zzqxjwl nonexistent phrase"'
    assert search.searched_at == FETCHED
    assert search.connector == "arxiv"


def test_never_having_looked_cannot_be_expressed_as_a_search() -> None:
    """'We did not look' and 'we looked and found nothing' must stay different facts.

    The second is a `LiteratureSearch` with empty results. The first has no representation at
    all: neither the query nor the time it ran has a default, so a record cannot be conjured
    for a search that never happened.
    """
    with pytest.raises(ValidationError) as captured:
        LiteratureSearch(connector="arxiv", total_matched=0)  # type: ignore[call-arg]

    message = str(captured.value)
    assert "query" in message
    assert "searched_at" in message


def test_a_network_failure_raises_rather_than_reporting_no_literature() -> None:
    """A connector returning [] on a timeout records an outage as "no literature exists".

    That is a research finding, and a false one. Both the transport and the scout above it
    have to raise instead.
    """

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(offline)) as client:
        transport = HttpxScoutTransport(client=client)
        with pytest.raises(SourceUnavailable, match="transport failed"):
            transport.fetch("https://export.arxiv.org/api/query", {}, timeout_seconds=5.0)

    # And the scout does not absorb it into an empty result on the way back up.
    with pytest.raises(SourceUnavailable):
        ArxivScout(ExplodingTransport()).search("cat:q-fin.ST")


def test_a_non_200_response_is_not_an_empty_search() -> None:
    """An unavailable host says nothing about what has been published."""

    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"maintenance")

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        transport = HttpxScoutTransport(client=client)
        with pytest.raises(SourceUnavailable, match="HTTP 503"):
            transport.fetch("https://export.arxiv.org/api/query", {}, timeout_seconds=5.0)


def test_a_throttle_message_is_a_protocol_failure_not_an_absence_of_papers() -> None:
    """The exact body arXiv returned when these fixtures were collected too quickly."""
    with pytest.raises(SourceProtocolError, match="not XML"):
        parsed(read("arxiv_rate_exceeded.txt"))


def test_an_arxiv_error_row_does_not_become_a_paper_called_error() -> None:
    """arXiv reports a bad query as a result, not as a failure.

    The captured response has `totalResults` of 1 and one `<entry>` titled "Error". Storing it
    would invent a paper and let a malformed query read as literature. The reported reason has
    to reach the caller, so the match here is on arXiv's own words rather than on any message
    a later parse step would have produced anyway.
    """
    with pytest.raises(SourceProtocolError, match="incorrect id format for nonsense-id-xyz"):
        parsed(read("arxiv_error_entry.xml"))


def test_consecutive_requests_wait_out_the_arxiv_rate_limit() -> None:
    """arXiv asks for one request every three seconds. Enforced here, not left to callers."""
    clock = FakeClock()

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read("arxiv_no_results.xml"))

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        transport = HttpxScoutTransport(client=client, sleep=clock.sleep, monotonic=clock.monotonic)
        transport.fetch("https://export.arxiv.org/api/query", {}, timeout_seconds=5.0)
        # Nothing to wait for on the first call.
        assert clock.slept == []
        transport.fetch("https://export.arxiv.org/api/query", {}, timeout_seconds=5.0)

    assert clock.slept == [ARXIV_MIN_INTERVAL_SECONDS]


def test_every_request_identifies_itself_with_a_contactable_user_agent() -> None:
    """A free public API is entitled to know who is calling it and where to complain."""
    seen: list[str] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, content=read("arxiv_no_results.xml"))

    with httpx.Client(transport=httpx.MockTransport(capture)) as client:
        HttpxScoutTransport(client=client).fetch(
            "https://export.arxiv.org/api/query", {}, timeout_seconds=5.0
        )

    assert seen[0].startswith("elrond-source-scout/")
    assert "github.com/jfhutchi/project_elrond" in seen[0]
    # Not httpx's default, which tells the host nothing.
    assert "python-httpx" not in seen[0]


def test_a_connector_returns_sources_and_cannot_assign_epistemic_status() -> None:
    """Fetching must not become a way to manufacture support.

    Source count is the cheapest thing in this system to fabricate once something is
    downloading. So a connector yields `Source` objects and nothing else; the step that says
    `LITERATURE_SUPPORTED` stays `cite()`, at a call site that has to name the claim.
    """
    search = parsed(read("arxiv_qfin_entry.xml"))
    paper = search.results[0]

    assert isinstance(paper, Source)
    assert not has_epistemic_status_field(Source)
    assert not has_epistemic_status_field(LiteratureSearch)
    assert not hasattr(search, "status")

    citation = cite(
        paper,
        claim="closing auction returns have heavy tails",
        locator="abstract",
        status=EpistemicStatus.LITERATURE_SUPPORTED,
    )
    assert citation.status is EpistemicStatus.LITERATURE_SUPPORTED
    assert citation.source_id == paper.source_id


def test_a_revised_paper_is_a_new_source_and_does_not_overwrite_the_cited_vintage() -> None:
    """arXiv versions are part of the identity, so v2 lands beside v1 rather than over it."""
    revision = parsed(read("arxiv_versioned_entry.xml")).results[0]
    assert revision.source_id == "arxiv:0805.1965v2"

    prior = revision.model_copy(update={"source_id": "arxiv:0805.1965v1", "content_hash": "0" * 64})
    assert revision.source_id != prior.source_id

    index = SourceIndex([prior])
    assert index.add(revision) is False
    assert index.get("arxiv:0805.1965v1") is prior
    assert index.get("arxiv:0805.1965v1").content_hash == "0" * 64  # type: ignore[union-attr]
    assert index.revised("arxiv:0805.1965v1", content_hash=revision.content_hash) is True


def test_the_content_hash_covers_the_entry_as_it_arrived() -> None:
    """Reproducible for the same bytes, different for changed ones, or it detects nothing."""
    body = read("arxiv_qfin_entry.xml")
    first = parsed(body).results[0]
    again = parsed(body).results[0]
    assert first.content_hash == again.content_hash

    edited = body.replace(b"Heavy tailed distributions", b"Light tailed distributions")
    assert edited != body
    assert parsed(edited).results[0].content_hash != first.content_hash


def test_a_dtd_is_refused_before_elementtree_expands_it() -> None:
    """Untrusted XML with a DTD is a decompression bomb this parser would otherwise expand."""
    bomb = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE feed [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        b'<feed xmlns="http://www.w3.org/2005/Atom">&lol2;</feed>'
    )
    # The guard is load-bearing rather than decorative: the stdlib parser does expand this.
    assert "lollol" in (ElementTree.fromstring(bomb).text or "")

    with pytest.raises(SourceProtocolError, match="DOCTYPE"):
        parsed(bomb)


def test_an_oversized_response_is_refused_before_it_is_parsed() -> None:
    """A bound on what an untrusted host can make this process allocate."""
    with pytest.raises(SourceProtocolError, match="cap"):
        parsed(b"x" * (MAX_RESPONSE_BYTES + 1))
