"""Acquiring literature without letting an outage read as an absence of literature.

Every fixture in `tests/fixtures/arxiv_*` is a real response captured from
`export.arxiv.org`, including the two that matter most: the feed arXiv returns when a query
matches nothing, and the one it returns when the query itself is bad -- which is an ordinary
looking `<entry>` titled "Error". A parser that does not know the second one stores a paper
called Error and cites it. `arxiv_rate_exceeded.txt` is what the host actually sent when this
connector's fixtures were being collected too quickly.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from quantbot.research.scout import (
    ARXIV_MIN_INTERVAL_SECONDS,
    MAX_RESPONSE_BYTES,
    ArxivScout,
    HttpxScoutTransport,
    LiteratureSearch,
    SearchNotRecordable,
    SearchOutcome,
    SourceProtocolError,
    SourceUnavailable,
    parse_feed,
    record_literature_outage,
    record_literature_search,
    searched_before,
)
from quantbot.research.sources import (
    EpistemicStatus,
    Source,
    SourceHealth,
    SourceIndex,
    SourceKind,
    cite,
)
from quantbot.storage import Database

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


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / "scout.db")
    yield database
    database.close()


def test_a_completed_search_survives_the_process_that_ran_it(store) -> None:
    """#4: the connector's care about outages lasted exactly one run and then evaporated.

    Storing the results with each source's identifier and content hash is what makes a citation
    traceable to bytes that were actually acquired, rather than to a title somebody typed.
    """
    scout = ArxivScout(ScriptedTransport(read("arxiv_qfin_entry.xml")), now=lambda: FETCHED)
    search = scout.search("all:cat:q-fin.PM")

    with store.transaction() as session:
        outcome = record_literature_search(session, search, search_id="LS-1")

    assert outcome is SearchOutcome.RESULTS

    with store.transaction() as session:
        assert searched_before(session, "all:cat:q-fin.PM") is True
        rows = session.execute(
            text(
                "SELECT connector, query, outcome, total_matched, results_json "
                "FROM literature_searches"
            )
        ).all()

    assert len(rows) == 1
    connector, query, stored_outcome, matched, results_json = rows[0]
    assert (connector, query, stored_outcome) == ("arxiv", "all:cat:q-fin.PM", "RESULTS")
    assert matched == search.total_matched
    stored = json.loads(results_json)
    assert [item["content_hash"] for item in stored] == [
        source.content_hash for source in search.results
    ]


def test_a_zero_result_search_and_an_outage_are_stored_as_different_facts(store) -> None:
    """The distinction the connector was written to preserve, made durable.

    A gap in the evidence and a gap in the search look identical once both are gone. A system
    that cannot tell them apart eventually reports "no literature supports this" when it means
    "arXiv returned 503 twice".
    """
    scout = ArxivScout(ScriptedTransport(read("arxiv_no_results.xml")), now=lambda: FETCHED)
    empty = scout.search('all:"zzqxjwl nonexistent phrase"')

    with store.transaction() as session:
        looked = record_literature_search(session, empty, search_id="LS-EMPTY")
        could_not_look = record_literature_outage(
            session,
            search_id="LS-DOWN",
            connector="arxiv",
            query="all:volatility-risk-premium",
            attempted_at=FETCHED,
            detail="export.arxiv.org returned 503 twice",
        )

    assert looked is SearchOutcome.NO_RESULTS
    assert could_not_look is SearchOutcome.OUTAGE

    with store.transaction() as session:
        # We looked for this one, so the literature has genuinely been checked.
        assert searched_before(session, 'all:"zzqxjwl nonexistent phrase"') is True
        # We never managed to look for this one, so it has not.
        assert searched_before(session, "all:volatility-risk-premium") is False


def test_an_outage_cannot_carry_a_match_count_or_an_empty_explanation(store) -> None:
    """Two ways an outage record would become a small fiction.

    A match count against a request that never completed is a number nobody received, and an
    unexplained outage is only marginally more useful than no record at all.
    """
    with store.transaction() as session:
        with pytest.raises(SearchNotRecordable, match="say what failed"):
            record_literature_outage(
                session,
                search_id="LS-BLANK",
                connector="arxiv",
                query="all:momentum",
                attempted_at=FETCHED,
                detail="   ",
            )

    # And the database refuses it too, so a future writer bypassing the helper still cannot.
    with pytest.raises(IntegrityError):
        with store.transaction() as session:
            session.execute(
                text(
                    "INSERT INTO literature_searches (search_id, connector, query, outcome, "
                    "attempted_at, total_matched, results_json, detail) VALUES "
                    "('LS-LIE', 'arxiv', 'all:momentum', 'OUTAGE', '2026-08-20T23:59:04Z', "
                    "42, '[]', 'invented')"
                )
            )


def test_a_search_record_cannot_be_replaced_with_a_better_one(store) -> None:
    """Append-only, for the same reason `search_runs` is.

    A disappointing search that can be overwritten is a search that never constrains anything.
    """
    scout = ArxivScout(ScriptedTransport(read("arxiv_no_results.xml")), now=lambda: FETCHED)
    empty = scout.search('all:"zzqxjwl nonexistent phrase"')

    with store.transaction() as session:
        record_literature_search(session, empty, search_id="LS-1")

    with pytest.raises(SearchNotRecordable, match="already recorded"):
        with store.transaction() as session:
            record_literature_search(session, empty, search_id="LS-1")
