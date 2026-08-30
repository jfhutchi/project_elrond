"""An outage must never become "no news" (#56).

`catalyst.py` gets the three-state logic right, and every one of its guarantees can be destroyed
by an adapter that reports a provider failure as an empty successful result. So these tests are
almost entirely about failure paths: the one success case is short, and the rest establish that
each way of not-answering produces a health state rather than silence.

Verified against the live endpoint 2026-08-30 -- the paper account **is** entitled to
`/v1beta1/news`, which had been assumed unavailable. That is what made this buildable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from quantbot.anomaly.catalyst import (
    CatalystOutcome,
    SourceHealth,
    assess_catalysts,
)
from quantbot.anomaly.news import (
    SOURCE_NAME,
    AlpacaNewsSource,
    candidates_from_payload,
)

OBSERVED = datetime(2026, 3, 16, 21, 0, tzinfo=UTC)
RETRIEVED = datetime(2026, 3, 16, 21, 5, tzinfo=UTC)


class Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()


class Transport:
    """Records every call, and answers with a sequence.

    A sequence rather than one response because an empty lookback now provokes a second,
    different question -- "does this source cover this symbol at all" -- and a stub answering both
    identically could not tell a silence from a coverage gap, which is the distinction under test.
    """

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, str]] = []

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def request(self, method: str, url: str, *, headers, params, timeout_seconds) -> Any:
        self.requests.append(dict(params))
        response = (
            self._responses[len(self.requests) - 1]
            if len(self.requests) <= len(self._responses)
            else self._responses[-1]
        )
        if isinstance(response, Exception):
            raise response
        return response


def source(*responses: object) -> tuple[AlpacaNewsSource, Transport]:
    transport = Transport(*responses)
    return (
        AlpacaNewsSource(
            transport, api_key="k", api_secret="s", timeout_seconds=Decimal("5")
        ),
        transport,
    )


def item(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 12345,
        "headline": "Company announces something",
        "source": "benzinga",
        "created_at": "2026-03-16T15:11:54Z",
        "url": "https://example.invalid/story",
    }
    payload.update(overrides)
    return payload


async def fetch(news: AlpacaNewsSource, symbols=("AAPL",)):
    return await news.fetch(
        list(symbols), window_start=OBSERVED - timedelta(days=2),
        observed_at=OBSERVED, retrieved_at=RETRIEVED,
    )


# --- the request cannot ask for the future ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_query_is_bounded_at_the_observation() -> None:
    """Look-ahead prevented at the request, not only in the filter.

    `assess_catalysts` excludes later items and that is the tested guard, but an adapter that
    fetched a whole day and filtered afterwards is one refactor away from leaking the future.
    Bounding the request means the mistake has to be made twice.
    """
    news, transport = source(Response(200, {"news": []}))

    await fetch(news)

    assert transport.requests[0]["end"] == "2026-03-16T21:00:00Z"
    assert transport.requests[0]["start"] == "2026-03-14T21:00:00Z"
    # The coverage probe is bounded at the observation too. A baseline running past the event
    # would establish coverage using items the decision could not have seen.
    assert transport.requests[1]["end"] == "2026-03-16T21:00:00Z"


# --- every way of not answering ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rate_limit_is_a_health_state_rather_than_an_empty_result() -> None:
    """The case the whole three-state design exists for.

    Rate limits cluster when markets are busy, which is when anomalies fire. Reading one as "no
    news" would manufacture discoveries exactly when the provider was struggling.
    """
    news, _ = source(Response(429, {"message": "too many requests"}))

    result = await fetch(news)

    assert result.health is SourceHealth.RATE_LIMITED
    assert result.candidates == ()
    assert "429" in (result.detail or "")


@pytest.mark.asyncio
async def test_an_unreachable_provider_is_a_health_state() -> None:
    """A transport failure must not propagate: the caller is recording context beside a trading
    cycle. But it must not vanish either, so it is carried in the result.
    """
    news, _ = source(ConnectionError("no route to host"))

    result = await fetch(news)

    assert result.health is SourceHealth.TIMEOUT
    assert "no route to host" in (result.detail or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
async def test_any_non_success_status_refuses_to_be_a_finding(status: int) -> None:
    """Including 401 and 403, which is how a lapsed entitlement would present.

    An entitlement that quietly expired would otherwise turn every anomaly into "no public
    catalyst" -- a systematic false discovery with no error anywhere.
    """
    news, _ = source(Response(status, {"message": "nope"}))

    result = await fetch(news)

    assert result.health is SourceHealth.UNAVAILABLE
    assert str(status) in (result.detail or "")


@pytest.mark.asyncio
async def test_an_unreadable_body_is_not_an_empty_one() -> None:
    """A 200 carrying junk is a provider fault, and looks exactly like silence if trusted."""
    news, _ = source(Response(200, b"<html>gateway error</html>"))

    result = await fetch(news)

    assert result.health is SourceHealth.UNAVAILABLE


@pytest.mark.asyncio
async def test_asking_about_no_symbols_is_not_a_lookup() -> None:
    """Nothing was asked, so nothing may be concluded -- and the provider is not contacted."""
    news, transport = source(Response(200, {"news": []}))

    result = await news.fetch(
        [], window_start=OBSERVED - timedelta(days=2),
        observed_at=OBSERVED, retrieved_at=RETRIEVED,
    )

    assert result.health is SourceHealth.UNAVAILABLE
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_a_healthy_empty_answer_is_a_finding_when_the_symbol_is_covered() -> None:
    """The one case that may support `NO_PUBLIC_CATALYST_FOUND`.

    Asked, answered, nothing in the window -- and the source demonstrably covers the symbol, so
    its silence is about the world rather than about its own tagging.
    """
    news, transport = source(Response(200, {"news": []}), Response(200, {"news": [item()]}))

    result = await fetch(news)

    assert result.health is SourceHealth.HEALTHY
    assert result.candidates == ()
    assert transport.calls == 2, "the coverage probe ran"

    assessment = assess_catalysts(
        anomaly_event_id="anomaly-1", observed_at=OBSERVED, sources=[result]
    )
    assert assessment.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND


# --- mapping the payload ------------------------------------------------------------------------


def test_publication_and_retrieval_times_stay_apart() -> None:
    """Collapsing them is how a point-in-time system quietly stops being one."""
    candidates = candidates_from_payload({"news": [item()]}, retrieved_at=RETRIEVED)

    assert len(candidates) == 1
    assert candidates[0].published_at == datetime(2026, 3, 16, 15, 11, 54, tzinfo=UTC)
    assert candidates[0].retrieved_at == RETRIEVED
    assert candidates[0].source == "benzinga"


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-03-16T15:11:54", 17])
def test_an_unusable_publication_time_becomes_unplaceable(value: object) -> None:
    """Never the retrieval time, which would date the item to when Elrond looked.

    The naive-timestamp case is deliberate: `2026-03-16T15:11:54` parses fine and is simply
    ambiguous, so accepting it would order an item against a UTC event using a wall clock from
    nowhere in particular.
    """
    candidates = candidates_from_payload(
        {"news": [item(created_at=value)]}, retrieved_at=RETRIEVED
    )

    assert candidates[0].published_at is None


def test_an_unplaceable_item_forces_unknown_rather_than_silence() -> None:
    """End to end: the mapping decision reaches the outcome it is supposed to reach."""
    candidates = candidates_from_payload(
        {"news": [item(created_at=None)]}, retrieved_at=RETRIEVED
    )
    from quantbot.anomaly.catalyst import SourceResult

    assessment = assess_catalysts(
        anomaly_event_id="anomaly-1",
        observed_at=OBSERVED,
        sources=[
            SourceResult(
                source=SOURCE_NAME, health=SourceHealth.HEALTHY, candidates=candidates
            )
        ],
    )

    assert assessment.outcome is CatalystOutcome.CATALYST_UNKNOWN
    assert assessment.unplaceable == 1


def test_an_item_with_no_identifier_is_not_a_candidate() -> None:
    """Nothing that can be cited later is not evidence of anything now."""
    candidates = candidates_from_payload(
        {"news": [item(id=None), item(headline="   "), item()]}, retrieved_at=RETRIEVED
    )

    assert len(candidates) == 1


def test_a_body_without_a_news_list_yields_nothing() -> None:
    """A shape change fails here rather than three layers away."""
    assert candidates_from_payload({}, retrieved_at=RETRIEVED) == ()
    assert candidates_from_payload({"news": "unexpected"}, retrieved_at=RETRIEVED) == ()


# --- coverage: a source that never mentions a symbol cannot be silent about it ----------------
#
# Measured 2026-08-30 over the live v1-3 ETF universe. Items returned in a trailing year:
# XLU 50 (page cap), EEM 33, VNQ 27, EFA 3, MDY 1. Single-name controls hit the cap in a month.
# Without the gate below, NO_PUBLIC_CATALYST_FOUND would be emitted for every MDY anomaly
# forever -- and since that outcome is defined as the *interesting* case, the least covered
# instruments would generate the most apparent discoveries.


@pytest.mark.asyncio
async def test_an_uncovered_symbol_cannot_support_a_negative_finding() -> None:
    """The measurement turned into a mechanism.

    An empty lookback *and* an empty baseline means this provider does not cover the symbol.
    Reporting that as silence would be a claim about a vendor's tagging dressed as a claim about
    the world.
    """
    news, transport = source(Response(200, {"news": []}), Response(200, {"news": []}))

    result = await fetch(news, symbols=("MDY",))

    assert result.health is SourceHealth.STALE
    assert "MDY" in (result.detail or "")
    assert "90 days" in (result.detail or "")
    assert transport.calls == 2

    assessment = assess_catalysts(
        anomaly_event_id="anomaly-1", observed_at=OBSERVED, sources=[result]
    )
    assert assessment.outcome is CatalystOutcome.CATALYST_UNKNOWN


@pytest.mark.asyncio
async def test_the_coverage_probe_looks_back_the_detectors_own_baseline() -> None:
    """Reused rather than chosen, which is what stops it becoming a tuned parameter.

    "Long enough to characterise this symbol" means the same thing here as in the detector's
    baseline, and a number picked for this purpose alone would be one more knob nobody could
    justify later.
    """
    news, transport = source(Response(200, {"news": []}), Response(200, {"news": []}))

    await fetch(news)

    assert transport.requests[1]["start"] == "2025-12-16T21:00:00Z"
    assert transport.requests[1]["limit"] == "1", "existence, not enumeration"


@pytest.mark.asyncio
async def test_a_failed_coverage_probe_is_unavailable_rather_than_uncovered() -> None:
    """An unverifiable claim about the provider must not become a finding about the world.

    Both branches assert something. If the probe cannot complete, neither has been established,
    and the honest answer is that this lookup did not conclude.
    """
    news, _ = source(Response(200, {"news": []}), Response(503, {"message": "down"}))

    result = await fetch(news)

    assert result.health is SourceHealth.UNAVAILABLE
    assert "coverage could not be checked" in (result.detail or "")


@pytest.mark.asyncio
async def test_a_found_catalyst_costs_no_extra_request() -> None:
    """The probe is paid only where the answer changes what may be concluded.

    A non-empty lookback has already established both coverage and content, so asking again would
    be provider load bought with nothing.
    """
    news, transport = source(Response(200, {"news": [item()]}))

    result = await fetch(news)

    assert result.health is SourceHealth.HEALTHY
    assert len(result.candidates) == 1
    assert transport.calls == 1


# --- content identity (#83) -------------------------------------------------------------------


def test_two_different_items_hash_differently() -> None:
    """The point of the hash: telling a genuine second report from the same wire copy twice.

    #83 asks for a content identity because a second provider makes it necessary. Two sources
    carrying the same story produce two candidates, and without this there is no way to tell
    corroboration from double-counting -- which would inflate exactly the evidence a negative
    finding is measured against.
    """
    candidates = candidates_from_payload(
        {"news": [item(id=1, headline="First thing"), item(id=2, headline="Second thing")]},
        retrieved_at=RETRIEVED,
    )

    assert len({c.content_hash for c in candidates}) == 2


def test_the_same_item_hashes_the_same_on_a_re_fetch() -> None:
    """Otherwise every re-fetch would look like a revision, and nothing could be deduplicated."""
    first = candidates_from_payload({"news": [item()]}, retrieved_at=RETRIEVED)
    later = candidates_from_payload(
        {"news": [item()]}, retrieved_at=RETRIEVED + timedelta(hours=6)
    )

    assert first[0].content_hash == later[0].content_hash, (
        "the retrieval time is not part of the content"
    )


def test_a_revised_story_hashes_differently() -> None:
    """A provider correcting a headline has changed the item, and that has to be detectable."""
    original = candidates_from_payload({"news": [item()]}, retrieved_at=RETRIEVED)
    revised = candidates_from_payload(
        {"news": [item(headline="Company announces something, corrected")]},
        retrieved_at=RETRIEVED,
    )

    assert original[0].content_hash != revised[0].content_hash
    assert original[0].external_id == revised[0].external_id, "same item, different content"


def test_a_reordered_payload_does_not_look_like_a_revision() -> None:
    """Hashed over the fields that carry meaning, not the raw JSON.

    A provider adding a field or reordering its response has not revised the story, and a hash
    that moved on either would report a revision that did not happen -- noise that would then be
    indistinguishable from a real correction.
    """
    plain = candidates_from_payload({"news": [item()]}, retrieved_at=RETRIEVED)
    padded = candidates_from_payload(
        {"news": [{**item(), "images": [{"url": "x"}], "author": "somebody"}]},
        retrieved_at=RETRIEVED,
    )

    assert plain[0].content_hash == padded[0].content_hash


def test_every_candidate_names_the_parser_that_built_it() -> None:
    """A re-parse under a new version is a new reading of the same item, and pooling the two
    would hide that the reading changed rather than the story.
    """
    from quantbot.anomaly.news import PARSER_VERSION

    candidate = candidates_from_payload({"news": [item()]}, retrieved_at=RETRIEVED)[0]

    assert candidate.parser == SOURCE_NAME
    assert candidate.parser_version == PARSER_VERSION


def test_a_re_dated_item_hashes_differently() -> None:
    """A corrected publication time is a revision, and one that matters more than most.

    The qualifying window is bounded at the observation, so an item re-dated by a provider can
    move across that boundary -- from explaining the move to postdating it, or the reverse. If
    the hash ignored the timestamp, that change would be invisible while altering whether the
    item counts as a catalyst at all.
    """
    original = candidates_from_payload({"news": [item()]}, retrieved_at=RETRIEVED)
    redated = candidates_from_payload(
        {"news": [item(created_at="2026-03-16T18:45:00Z")]}, retrieved_at=RETRIEVED
    )

    assert original[0].external_id == redated[0].external_id
    assert original[0].published_at != redated[0].published_at
    assert original[0].content_hash != redated[0].content_hash
