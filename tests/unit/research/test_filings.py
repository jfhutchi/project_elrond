"""SEC filings, against four real responses sec.gov actually sent (#4, #17).

Every fixture here was captured from `www.sec.gov`, including the three that matter most and
that no invented XML would have taught this parser about:

* `edgar_no_results.xml` -- a valid feed for a real company with **zero** matching filings.
* `edgar_unknown_cik.xml` -- **HTML, served 200**, for a CIK that does not exist. A parser
  expecting atom either crashes or counts no entries and reports "this company has filed
  nothing" about a company that is not there.
* `edgar_undeclared_tool.html` -- **HTML 403**, titled "Your Request Originates from an
  Undeclared Automated Tool", which SEC returns to any client that does not name a contact.
  Reading that as an absence of filings would be the most consequential version of the mistake
  this module exists to avoid, because it is the one that happens on every request.

The success fixture was fetched with a contact supplied by the operator, and any echo of that
contact was scrubbed before the file reached the repository.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.research.filings import (
    CONTACT_ENV,
    ContactNotDeclared,
    HttpxFilingTransport,
    SecFilingScout,
    contact_from_environment,
    parse_filings,
)
from quantbot.research.scout import SourceProtocolError
from quantbot.research.sources import SourceKind

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FETCHED = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class ScriptedTransport:
    """Returns one captured response, and records what was asked for."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch(self, url: str, params: dict[str, str], *, timeout_seconds: float) -> bytes:
        self.calls.append((url, dict(params)))
        return self.body


def test_a_filing_records_when_it_was_filed_and_claims_nothing_about_the_period() -> None:
    """The point-in-time gap this connector exists for, and the claim it refuses to make.

    A fiscal year ending in September is described by a 10-K filed in late October. Using
    September's numbers in September is using a document that did not exist, and the effect is
    invisible in results -- it just makes the strategy look prescient.

    The feed carries `filing-date` and no period-covered field, verified against the real
    capture. So `published_at` is the filing date and there is no period claim at all. Inferring
    one from the form type is right often enough to be dangerous and wrong for every transition
    period, fiscal-year change and late filer.
    """
    sources = parse_filings(
        read("edgar_10k_aapl.xml"),
        cik="0000320193",
        retrieved_at=FETCHED,
        parser_version="edgar-atom-v1",
    )

    assert sources, "the capture holds real 10-K filings"
    first = sources[0]
    assert first.kind is SourceKind.SEC_FILING
    assert first.source_id.startswith("sec:")
    assert "10-K" in first.title
    assert first.published_at < first.retrieved_at
    # No period claim anywhere: the excerpt is empty rather than a guessed fiscal year.
    assert first.excerpt == ""
    # And it is citable evidence, not a derived artifact.
    assert first.citable is True
    assert first.derived is False


def test_two_filings_hash_differently_so_a_revision_is_a_new_source() -> None:
    """The content hash covers the entry as it arrived, so an amendment cannot overwrite."""
    sources = parse_filings(
        read("edgar_10k_aapl.xml"),
        cik="0000320193",
        retrieved_at=FETCHED,
        parser_version="edgar-atom-v1",
    )

    hashes = {source.content_hash for source in sources}
    assert len(hashes) == len(sources), "distinct filings must not share a hash"


def test_a_company_with_no_matching_filings_is_a_record_of_having_looked() -> None:
    """Zero filings is evidence of absence. It must stay distinct from the two failures below."""
    scout = SecFilingScout(
        ScriptedTransport(read("edgar_no_results.xml")), now=lambda: FETCHED
    )

    search = scout.search("0000320193", form_type="N-CSR")

    assert search.found_nothing is True
    assert search.results == ()
    assert search.total_matched == 0
    # The record still says what was asked and when, which is the whole point of it existing.
    assert search.query == "CIK 0000320193 N-CSR"
    assert search.searched_at == FETCHED
    assert search.connector == "sec-edgar"


def test_an_unknown_company_is_a_protocol_failure_not_a_company_that_filed_nothing() -> None:
    """EDGAR serves HTML with status 200 for a CIK that does not exist.

    A parser that counted entries first would find none and report an absence of filings about
    a company that is not there -- which would then be stored as evidence. This is the same trap
    as arXiv's `<entry>` titled "Error", in a different costume.
    """
    scout = SecFilingScout(
        ScriptedTransport(read("edgar_unknown_cik.xml")), now=lambda: FETCHED
    )

    with pytest.raises(SourceProtocolError) as caught:
        scout.search("0009999999", form_type="10-K")

    assert "absence of filings" in str(caught.value)


def test_a_block_is_a_protocol_failure_and_never_an_absence() -> None:
    """SEC returns this to any client that does not declare a contact.

    It arrives on *every* request from a misconfigured connector, so reading it as "no filings
    exist" would not be an occasional error -- it would be a research system quietly concluding
    that no company has ever filed anything.
    """
    scout = SecFilingScout(
        ScriptedTransport(read("edgar_undeclared_tool.html")), now=lambda: FETCHED
    )

    with pytest.raises(SourceProtocolError):
        scout.search("0000320193", form_type="10-K")


def test_a_doctype_is_refused_before_elementtree_expands_it() -> None:
    """Stdlib ElementTree expands internal entities, so a DTD is a bomb waiting to be parsed.

    Same rule as `scout.parse_feed`. EDGAR's atom feed never carries one, so refusing costs
    nothing and closes the hole without adding a third-party parser.
    """
    bomb = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE feed [<!ENTITY a "AAAAAAAAAA">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
        b'<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>'
    )

    with pytest.raises(SourceProtocolError, match="DOCTYPE"):
        parse_filings(bomb, cik="1", retrieved_at=FETCHED, parser_version="v1")


def test_a_connector_without_a_declared_contact_fails_before_it_makes_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC refuses service without one, so a scout built without it only ever produces 403s.

    Failing at construction turns that into an error where somebody can fix it, rather than a
    stream of blocks that the layer above has to be careful not to read as absences.
    """
    monkeypatch.delenv(CONTACT_ENV, raising=False)

    with pytest.raises(ContactNotDeclared, match=CONTACT_ENV):
        contact_from_environment()

    with pytest.raises(ContactNotDeclared):
        HttpxFilingTransport("   ")


def test_the_contact_reaches_the_user_agent_and_the_cik_is_zero_padded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SEC matches on a zero-padded ten-digit CIK; an unpadded one silently matches nothing."""
    monkeypatch.setenv(CONTACT_ENV, "someone@example.com")
    transport = HttpxFilingTransport(contact_from_environment())

    assert "someone@example.com" in transport.user_agent
    assert transport.user_agent.startswith("project_elrond/")

    scripted = ScriptedTransport(read("edgar_no_results.xml"))
    SecFilingScout(scripted, now=lambda: FETCHED).search("320193", form_type="10-K")

    _, params = scripted.calls[0]
    assert params["CIK"] == "0000320193"
    assert params["output"] == "atom"


def test_a_cik_that_is_not_a_number_records_nothing() -> None:
    scout = SecFilingScout(ScriptedTransport(read("edgar_no_results.xml")), now=lambda: FETCHED)

    with pytest.raises(ValueError, match="a CIK is a number"):
        scout.search("apple inc", form_type="10-K")


def test_xml_that_is_not_a_feed_is_refused_on_its_root_element() -> None:
    """The check that had no test until a mutation showed it had none.

    Both HTML captures are caught by the DOCTYPE guard first, so removing the root-element check
    left the whole suite green -- a guard nobody had exercised, which is indistinguishable from
    a guard that does not work. EDGAR can serve well-formed XML that is not a feed, and counting
    `<entry>` elements in it would find none and report an absence.
    """
    not_a_feed = (
        b'<?xml version="1.0"?>\n'
        b"<error><message>Invalid parameter</message></error>"
    )

    with pytest.raises(SourceProtocolError, match="rather than an atom feed"):
        parse_filings(not_a_feed, cik="1", retrieved_at=FETCHED, parser_version="v1")
