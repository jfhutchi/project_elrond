"""Was there a public reason, and did anyone manage to look? (#56 catalyst enrichment)

Two of the three states look identical from a distance, and conflating them is the single most
consequential error available here: an outage becoming "no news" turns every provider incident
into a discovery, and those incidents cluster exactly when markets are busiest.

The other error is subtler and more tempting. An article about the move published *after* it is
genuinely about the move, which makes crediting it as the cause feel reasonable. It is
look-ahead wearing the costume of explanation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quantbot.anomaly.catalyst import (
    DEFAULT_LOOKBACK,
    CatalystAssessment,
    CatalystCandidate,
    CatalystOutcome,
    SourceHealth,
    SourceResult,
    assess_catalysts,
)

OBSERVED = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


def candidate(
    published: datetime | None, *, external_id: str = "news-1", source: str = "alpaca-news"
) -> CatalystCandidate:
    return CatalystCandidate(
        source=source,
        external_id=external_id,
        published_at=published,
        retrieved_at=OBSERVED,
        headline="Something happened",
    )


def healthy(*candidates: CatalystCandidate, source: str = "alpaca-news") -> SourceResult:
    return SourceResult(source=source, health=SourceHealth.HEALTHY, candidates=candidates)


def down(health: SourceHealth = SourceHealth.UNAVAILABLE) -> SourceResult:
    return SourceResult(source="alpaca-news", health=health, detail="provider returned 503")


SEC = SourceResult(source="sec-edgar", health=SourceHealth.HEALTHY)


def assess(sources: list[SourceResult], **overrides: object) -> CatalystAssessment:
    fields: dict[str, object] = {
        "anomaly_event_id": "anomaly-2026-08-28-abcd",
        "observed_at": OBSERVED,
        "sources": sources,
    }
    fields.update(overrides)
    return assess_catalysts(**fields)  # type: ignore[arg-type]


# --- an outage is never a finding ---------------------------------------------------------------


def test_a_source_that_could_not_answer_makes_the_question_unanswerable() -> None:
    """The error that would turn every provider incident into a discovery.

    "Nobody found anything" is the tempting shortcut, and it is wrong in a way that clusters:
    outages happen when providers are under load, which is when markets are busiest, which is
    when anomalies fire.
    """
    for health in (
        SourceHealth.UNAVAILABLE,
        SourceHealth.RATE_LIMITED,
        SourceHealth.STALE,
        SourceHealth.TIMEOUT,
    ):
        result = assess([down(health), SEC])
        assert result.outcome is CatalystOutcome.CATALYST_UNKNOWN, health
        assert "not all consulted" in result.detail


def test_one_unhealthy_source_is_enough_even_when_another_answered() -> None:
    """A partial consultation is not a complete one, however much the healthy source returned."""
    result = assess([down(), healthy(source="sec-edgar")])

    assert result.outcome is CatalystOutcome.CATALYST_UNKNOWN


def test_a_negative_finding_cannot_be_constructed_over_an_unhealthy_source() -> None:
    """Structural, because this is the confusion the whole module exists to prevent."""
    with pytest.raises(ValueError, match="every provider outage into a discovery"):
        CatalystAssessment(
            anomaly_event_id="anomaly-1",
            observed_at=OBSERVED,
            lookback_seconds=3600,
            outcome=CatalystOutcome.NO_PUBLIC_CATALYST_FOUND,
            sources=(down(),),
            detail="claiming no news while a source was down",
        )


def test_an_unhealthy_source_must_say_why() -> None:
    """An unexplained outage is indistinguishable from a source nobody configured."""
    with pytest.raises(ValueError, match="without saying why"):
        SourceResult(source="alpaca-news", health=SourceHealth.TIMEOUT)


def test_an_unhealthy_source_cannot_also_have_returned_results() -> None:
    """Otherwise a partial answer is recorded as though it were complete."""
    with pytest.raises(ValueError, match="cannot also have answered completely"):
        SourceResult(
            source="alpaca-news",
            health=SourceHealth.RATE_LIMITED,
            candidates=(candidate(OBSERVED - timedelta(hours=1)),),
            detail="429",
        )


def test_a_lookup_with_no_sources_is_not_a_negative_finding() -> None:
    """It is a lookup that never happened."""
    with pytest.raises(ValueError, match="never happened"):
        assess([])


# --- time is the other trap ---------------------------------------------------------------------


def test_an_item_published_after_the_event_cannot_explain_it() -> None:
    """Look-ahead wearing the costume of explanation.

    The article really is about the move, which is what makes crediting it feel reasonable. It
    is excluded and *counted*, because "the world explained this, just not beforehand" is worth
    an operator seeing.
    """
    result = assess([healthy(candidate(OBSERVED + timedelta(hours=2))), SEC])

    assert result.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND
    assert result.published_after_event == 1
    assert not result.catalysts
    assert "cannot explain it" in result.detail


def test_an_item_with_no_publication_time_is_unknown_rather_than_recent() -> None:
    """It cannot be placed relative to the event, so it cannot support either finding."""
    result = assess([healthy(candidate(None)), SEC])

    assert result.outcome is CatalystOutcome.CATALYST_UNKNOWN
    assert result.unplaceable == 1
    assert "cannot be placed" in result.detail


def test_an_unplaceable_item_cannot_support_a_negative_finding() -> None:
    """Structural counterpart to the test above."""
    with pytest.raises(ValueError, match="cannot be placed relative to the event"):
        CatalystAssessment(
            anomaly_event_id="anomaly-1",
            observed_at=OBSERVED,
            lookback_seconds=3600,
            outcome=CatalystOutcome.NO_PUBLIC_CATALYST_FOUND,
            sources=(healthy(),),
            unplaceable=1,
            detail="claiming no news over an item nobody could date",
        )


def test_an_item_older_than_the_lookback_is_not_this_events_catalyst() -> None:
    """Week-old news must not be credited with today's move."""
    stale = assess([healthy(candidate(OBSERVED - timedelta(days=7))), SEC])
    recent = assess([healthy(candidate(OBSERVED - timedelta(hours=3))), SEC])

    assert stale.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND
    assert recent.outcome is CatalystOutcome.CATALYST_FOUND


def test_the_lookback_window_is_part_of_the_finding() -> None:
    """A different window is a different question, so the assessment records which was asked."""
    narrow = assess(
        [healthy(candidate(OBSERVED - timedelta(hours=6))), SEC], lookback=timedelta(hours=1)
    )
    wide = assess([healthy(candidate(OBSERVED - timedelta(hours=6))), SEC])

    assert narrow.outcome is CatalystOutcome.NO_PUBLIC_CATALYST_FOUND
    assert wide.outcome is CatalystOutcome.CATALYST_FOUND
    assert narrow.lookback_seconds == 3600
    assert wide.lookback_seconds == int(DEFAULT_LOOKBACK.total_seconds())
    assert narrow.identity != wide.identity


def test_a_naive_timestamp_is_refused() -> None:
    """It cannot be ordered against an event observed in UTC."""
    with pytest.raises(ValueError, match="timezone-aware"):
        CatalystCandidate(
            source="alpaca-news",
            external_id="news-1",
            published_at=datetime(2026, 8, 28, 20, 0),
            retrieved_at=OBSERVED,
            headline="Something happened",
        )


def test_publication_and_retrieval_times_stay_separate() -> None:
    """Collapsing them is how a point-in-time system quietly stops being one.

    When Elrond learned something says nothing about whether it could have explained an earlier
    move, which is the only question being asked.
    """
    item = candidate(OBSERVED - timedelta(hours=3))

    assert item.published_at != item.retrieved_at
    assert item.published_at is not None and item.published_at < item.retrieved_at


# --- the positive finding -----------------------------------------------------------------------


def test_a_qualifying_item_is_found_and_named() -> None:
    """A finding that could not say what it found would not be checkable."""
    result = assess(
        [
            healthy(
                candidate(OBSERVED - timedelta(hours=3), external_id="news-a"),
                candidate(OBSERVED - timedelta(hours=1), external_id="news-b"),
            ),
            SEC,
        ]
    )

    assert result.outcome is CatalystOutcome.CATALYST_FOUND
    assert [item.external_id for item in result.catalysts] == ["news-a", "news-b"]
    assert result.as_row()["catalyst_ids"] == "news-a,news-b"


def test_only_a_positive_finding_may_name_a_catalyst() -> None:
    """Otherwise an `UNKNOWN` row carries something a reader would quote as a cause."""
    with pytest.raises(ValueError, match="only a positive finding"):
        CatalystAssessment(
            anomaly_event_id="anomaly-1",
            observed_at=OBSERVED,
            lookback_seconds=3600,
            outcome=CatalystOutcome.CATALYST_UNKNOWN,
            sources=(down(),),
            catalysts=(candidate(OBSERVED - timedelta(hours=1)),),
            detail="unknown, with a cause attached anyway",
        )

    with pytest.raises(ValueError, match="must name what was found"):
        CatalystAssessment(
            anomaly_event_id="anomaly-1",
            observed_at=OBSERVED,
            lookback_seconds=3600,
            outcome=CatalystOutcome.CATALYST_FOUND,
            sources=(healthy(),),
            detail="found nothing in particular",
        )


def test_the_row_records_which_sources_failed_and_how() -> None:
    """An operator reading `UNKNOWN` needs to know whether to retry or to fix a provider."""
    row = assess([down(SourceHealth.RATE_LIMITED), SEC]).as_row()

    assert row["outcome"] == "CATALYST_UNKNOWN"
    assert row["sources_consulted"] == "alpaca-news,sec-edgar"
    assert row["unhealthy_sources"] == "alpaca-news=RATE_LIMITED"


def test_the_identity_moves_with_everything_that_changes_the_finding() -> None:
    """Two assessments over different sources or windows are two assessments."""
    base = assess([healthy(candidate(OBSERVED - timedelta(hours=3))), SEC])

    assert base.identity.startswith("catalyst-v1-")
    assert base.identity != assess(
        [healthy(candidate(OBSERVED - timedelta(hours=4))), SEC]
    ).identity
    assert base.identity != assess([healthy(candidate(OBSERVED - timedelta(hours=3)))]).identity
