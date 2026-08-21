"""SEC filings: a second source family, with a provenance problem arXiv does not have (#4, #17).

A second paper API would exercise the same provenance semantics twice and prove nothing. Filings
bring a genuinely different one, and it is the one that matters for backtesting: a company's
fiscal year ends in September and the 10-K describing it is filed in late October or November.
Using September's numbers in September is using a document that did not exist, and the effect is
invisible in results -- it simply makes the strategy look prescient. This is the same class of
error as the FRED release-hour defect, one asset class over and an order of magnitude larger.

**What this connector will and will not tell you about that gap.**

The EDGAR browse-edgar atom feed carries `filing-date` and no period-covered field. Verified
against real captured responses rather than assumed: `period` does not appear anywhere in the
feed for a 10-K. So a `Source` produced here has an honest `published_at` -- the filing date, the
first moment the document could be known -- and **no claim at all about the period it describes**.

Inferring the period from the form type would be the tempting move and the wrong one. "10-K means
the fiscal year ending some months earlier" is right often enough to be dangerous and wrong for
every transition period, every fiscal-year change and every late filer. A fabricated period on a
point-in-time source is worse than an absent one, because the whole purpose of the record is that
a later reader can tell what was knowable when.

**Four real responses this parser has to survive**, all captured from sec.gov:

1. A normal feed with filings.
2. A valid feed for a real company with **zero** matching filings. That is evidence of absence
   and must stay distinguishable from the others.
3. An **HTML** page, served 200, for a CIK that does not exist. A parser expecting atom either
   crashes or finds no entries and reports "this company has filed nothing" about a company that
   is not there. Same trap as arXiv's `<entry>` titled "Error", in a different costume.
4. An **HTML 403** titled "Your Request Originates from an Undeclared Automated Tool", which SEC
   returns to any client that does not declare a contact. Reading that as an absence of filings
   would be the most consequential version of the mistake this module exists to avoid.

3 and 4 are why the parser checks that it received an atom feed before it counts entries, rather
than counting entries and inferring.

**The contact is required at construction.** SEC's fair-access policy asks for a contact in the
User-Agent and refuses service without one, so a scout built without it is a scout that will only
ever produce 403s. Making it a required argument turns that into an error at the point somebody
can fix it. It is read from `QUANTBOT_SEC_CONTACT` by the composition helper rather than being
written into the source, because a personal address in a repository is a separate problem from a
personal address in a request header.
"""

from __future__ import annotations

import hashlib
import os
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx

from quantbot.research.scout import (
    MAX_RESPONSE_BYTES,
    LiteratureSearch,
    ScoutError,
    ScoutTransport,
    SourceProtocolError,
    SourceUnavailable,
)
from quantbot.research.sources import Source, SourceKind

EDGAR_HOST = "www.sec.gov"
EDGAR_BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

#: SEC's published fair-access ceiling is ten requests a second. This connector asks for one
#: every 200ms, which is half of that. The margin is deliberate: the penalty for exceeding it is
#: being blocked, and a research connector has no deadline that a 5/s rate fails to meet.
EDGAR_MIN_INTERVAL_SECONDS = 0.2

ATOM = "{http://www.w3.org/2005/Atom}"

#: Environment variable holding the contact SEC requires. Not defaulted: a wrong contact is worse
#: than none, because it names somebody who did not make the request.
CONTACT_ENV = "QUANTBOT_SEC_CONTACT"


class ContactNotDeclared(ScoutError):
    """Raised when no SEC contact is configured, before any request is attempted."""


class HttpxFilingTransport:
    """Rate-limited HTTP transport for EDGAR, enforcing the interval itself.

    Self-enforced rather than trusted to callers, for the same reason `HttpxScoutTransport` does
    it: the failure mode is being cut off from a free public service, and it arrives as an HTML
    page rather than an error a caller would notice.
    """

    def __init__(
        self,
        contact: str,
        *,
        client: httpx.Client | None = None,
        min_interval_seconds: float = EDGAR_MIN_INTERVAL_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not contact.strip():
            raise ContactNotDeclared(
                f"SEC requires a contact in the User-Agent and refuses service without one; "
                f"set {CONTACT_ENV}"
            )
        self._contact = contact.strip()
        self._client = client or httpx.Client(follow_redirects=True)
        self._min_interval = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request: float | None = None

    @property
    def user_agent(self) -> str:
        return f"project_elrond/0.2 ({self._contact})"

    def fetch(self, url: str, params: dict[str, str], *, timeout_seconds: float) -> bytes:
        self._wait()
        try:
            response = self._client.get(
                url,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=timeout_seconds,
            )
        except httpx.HTTPError as error:
            # The message is not interpolated: it can carry the full URL, and the URL carries
            # the User-Agent's contact in some proxy error formats.
            raise SourceUnavailable(f"{EDGAR_HOST} could not be reached") from error
        self._last_request = self._monotonic()
        if response.status_code != 200:
            raise SourceUnavailable(f"{EDGAR_HOST} answered {response.status_code}")
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise SourceProtocolError(f"{EDGAR_HOST} returned an oversized response")
        return body

    def _wait(self) -> None:
        if self._last_request is None:
            return
        remaining = self._min_interval - (self._monotonic() - self._last_request)
        if remaining > 0:
            self._sleep(remaining)

    def close(self) -> None:
        self._client.close()


def contact_from_environment() -> str:
    """Read the declared contact, or say which variable is missing.

    Raises rather than returning a placeholder. A scout built with `"unknown"` in its User-Agent
    is a scout that gets 403s at request time instead of a clear failure at construction time.
    """
    contact = os.environ.get(CONTACT_ENV, "").strip()
    if not contact:
        raise ContactNotDeclared(
            f"{CONTACT_ENV} is not set. SEC's fair-access policy requires a contact in the "
            "User-Agent and refuses service without one, so this connector cannot run "
            "anonymously."
        )
    return contact


def _text(element: ElementTree.Element | None) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def _require_atom(body: bytes) -> ElementTree.Element:
    """Parse the response and prove it is an atom feed before anything counts its entries.

    EDGAR serves HTML with status 200 for an unknown CIK, and HTML with 403 for an undeclared
    client. Both parse as *something*; neither is a feed. Counting entries first and asking
    questions later is how "this company has filed nothing" gets reported about a company that
    does not exist, and how a block gets reported as an absence.

    Three refusals, in the order that matters:

    1. **Size**, before anything is parsed. The transport caps it too; this repeats the check
       because `parse_filings` is callable directly and a bound that only exists on one path is
       a bound with a way around it.
    2. **`<!DOCTYPE`**, following the same rule as `scout.parse_feed`. Stdlib ElementTree expands
       internal entities, so a DTD is a decompression bomb waiting to be parsed. EDGAR's atom
       feed never carries one, so refusing costs nothing and closes the hole without adding a
       third-party parser. It also happens to catch both HTML responses, since each begins with
       a DOCTYPE -- but the entity check is first because it is the one with teeth.
    3. **A root element that is not `feed`**, which catches anything shaped like XML but not
       like the documented schema.
    """
    if len(body) > MAX_RESPONSE_BYTES:
        raise SourceProtocolError(
            f"{EDGAR_HOST} returned {len(body)} bytes, over the {MAX_RESPONSE_BYTES} cap"
        )
    if b"<!DOCTYPE" in body[:1024].upper():
        raise SourceProtocolError(
            f"{EDGAR_HOST} returned a document with a DOCTYPE rather than an atom feed. That is "
            "either an HTML error page -- a block, or an identifier that does not exist -- or a "
            "DTD this parser will not expand. Neither is an absence of filings."
        )
    try:
        root = ElementTree.fromstring(body.decode("utf-8", "replace"))
    except ElementTree.ParseError as error:
        raise SourceProtocolError(f"{EDGAR_HOST} returned unparseable XML") from error
    if root.tag != f"{ATOM}feed":
        raise SourceProtocolError(
            f"{EDGAR_HOST} returned <{root.tag}> rather than an atom feed"
        )
    return root


def _entry_hash(entry: ElementTree.Element) -> str:
    """Hash the entry as it arrived, canonicalised only by ElementTree's own serialisation."""
    return hashlib.sha256(
        ElementTree.tostring(entry, encoding="utf-8", method="xml")
    ).hexdigest()


def parse_filings(
    body: bytes,
    *,
    cik: str,
    retrieved_at: datetime,
    parser_version: str,
) -> tuple[Source, ...]:
    """Turn one EDGAR atom response into sources, or raise. Never returns () on a failure."""
    root = _require_atom(body)
    sources: list[Source] = []
    for entry in root.findall(f"{ATOM}entry"):
        content = entry.find(f"{ATOM}content")
        if content is None:
            continue
        # The feed declares Atom as the *default* namespace, so every unprefixed descendant --
        # including EDGAR's own `<accession-number>` and friends -- inherits it. Looking them up
        # by bare tag finds nothing and reports a malformed entry for a perfectly good one.
        accession = _text(content.find(f"{ATOM}accession-number"))
        filed = _text(content.find(f"{ATOM}filing-date"))
        href = _text(content.find(f"{ATOM}filing-href"))
        form = _text(content.find(f"{ATOM}filing-type"))
        name = _text(content.find(f"{ATOM}form-name"))
        if not (accession and filed and href and form):
            raise SourceProtocolError(
                f"{EDGAR_HOST} returned a filing entry missing accession, date, href or type"
            )
        try:
            filing_date = date.fromisoformat(filed)
        except ValueError as error:
            raise SourceProtocolError(f"{EDGAR_HOST} returned unreadable filing date {filed!r}") \
                from error
        published_at = datetime.combine(filing_date, datetime.min.time(), tzinfo=UTC)
        if published_at > retrieved_at:
            # EDGAR stamps a filing date, not a filing instant, so a same-day filing lands at
            # midnight UTC and is fine. A *future* filing date is the source contradicting
            # itself, and `Source` would refuse it anyway -- caught here with a better message.
            raise SourceProtocolError(
                f"{EDGAR_HOST} reports {accession} filed {filed}, which is in the future"
            )
        sources.append(
            Source(
                source_id=f"sec:{accession}",
                kind=SourceKind.SEC_FILING,
                uri=href,
                title=f"{form} - {name}" if name else form,
                publisher=f"SEC EDGAR (CIK {cik})",
                published_at=published_at,
                retrieved_at=retrieved_at,
                content_hash=_entry_hash(entry),
                parser_version=parser_version,
                # Deliberately empty. The feed carries no period-covered field, and inferring one
                # from the form type would put a fabricated point-in-time claim on the record.
                excerpt="",
            )
        )
    return tuple(sources)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SecFilingScout:
    """Search EDGAR for a company's filings, with provenance a backtest can check."""

    connector = "sec-edgar"
    #: Pins how an entry is canonicalised before hashing, so a stored hash stays reproducible.
    parser_version = "edgar-atom-v1"

    def __init__(
        self,
        transport: ScoutTransport,
        *,
        url: str = EDGAR_BROWSE_URL,
        timeout_seconds: float = 30.0,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._transport = transport
        self._url = url
        self._timeout = timeout_seconds
        self._now = now

    def search(self, cik: str, *, form_type: str, max_results: int = 10) -> LiteratureSearch:
        """List one company's filings of one form type. Raises on any failure."""
        digits = cik.strip().lstrip("0")
        if not digits.isdigit():
            raise ValueError("a CIK is a number; an empty or non-numeric one records nothing")
        if not form_type.strip():
            raise ValueError("a filing search needs a form type")
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")

        padded = digits.zfill(10)
        searched_at = self._now()
        body = self._transport.fetch(
            self._url,
            {
                "action": "getcompany",
                "CIK": padded,
                "type": form_type.strip(),
                "dateb": "",
                "owner": "include",
                "count": str(max_results),
                "output": "atom",
            },
            timeout_seconds=self._timeout,
        )
        results = parse_filings(
            body, cik=padded, retrieved_at=searched_at, parser_version=self.parser_version
        )
        return LiteratureSearch(
            connector=self.connector,
            query=f"CIK {padded} {form_type.strip()}",
            searched_at=searched_at,
            # The feed reports what it returned, not a corpus-wide count, so this is what was
            # seen rather than a total claimed. Overstating it would inflate a novelty check.
            total_matched=len(results),
            results=results,
        )


__all__ = [
    "CONTACT_ENV",
    "EDGAR_BROWSE_URL",
    "EDGAR_HOST",
    "EDGAR_MIN_INTERVAL_SECONDS",
    "ContactNotDeclared",
    "HttpxFilingTransport",
    "SecFilingScout",
    "contact_from_environment",
    "parse_filings",
]
