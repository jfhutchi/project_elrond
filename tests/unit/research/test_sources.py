"""External evidence, and the three ways it must not be allowed to become measurement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from quantbot.research.sources import (
    Citation,
    DerivedArtifactCited,
    EpistemicStatus,
    EvidenceBasis,
    Source,
    SourceHealth,
    SourceIndex,
    SourceKind,
    cite,
    contradicted_by_literature,
    gates_promotion,
    worth_testing,
)

PUBLISHED = datetime(2024, 3, 1, tzinfo=UTC)
FETCHED = datetime(2026, 8, 18, tzinfo=UTC)


def source(**overrides: object) -> Source:
    fields: dict[str, object] = {
        "source_id": "paper-momentum-2024",
        "kind": SourceKind.PAPER,
        "uri": "https://example.org/momentum-2024.pdf",
        "title": "Cross-sectional momentum after publication",
        "publisher": "Journal of Finance",
        "published_at": PUBLISHED,
        "retrieved_at": FETCHED,
        "content_hash": "a" * 64,
        "parser_version": "pdf-v3",
    }
    fields.update(overrides)
    return Source(**fields)


def test_a_summary_can_never_be_cited_in_place_of_its_source() -> None:
    """A derived artifact that reads authoritatively stops the search.

    This project believed a Corwin-Schultz estimate of 34bps for SPY against a real 0.26bps
    until someone pulled real quotes.
    """
    summary = source(
        source_id="summary-momentum-2024",
        derived=True,
        derived_from="paper-momentum-2024",
        content_hash="b" * 64,
    )
    with pytest.raises(DerivedArtifactCited, match="cite the source, not the summary"):
        cite(
            summary,
            claim="momentum decays after publication",
            locator="section 4",
            status=EpistemicStatus.LITERATURE_SUPPORTED,
        )
    assert not summary.citable

    # The underlying paper cites fine.
    citation = cite(
        source(),
        claim="momentum decays after publication",
        locator="section 4",
        status=EpistemicStatus.LITERATURE_SUPPORTED,
    )
    assert citation.source_id == "paper-momentum-2024"


def test_a_derived_artifact_must_name_what_it_derives_from() -> None:
    with pytest.raises(ValueError, match="must name the source it came from"):
        source(derived=True)
    with pytest.raises(ValueError, match="only a derived artifact"):
        source(derived_from="paper-momentum-2024")


def test_an_unverifiable_source_cannot_back_a_claim() -> None:
    broken = source(health=SourceHealth.UNVERIFIABLE)
    assert not broken.citable
    with pytest.raises(DerivedArtifactCited, match="unverifiable"):
        cite(broken, claim="x", locator="p1", status=EpistemicStatus.LITERATURE_SUPPORTED)


def test_event_time_is_separate_from_retrieval_time_and_ordered() -> None:
    """Look-ahead is invisible in results: a leaky backtest looks like a discovery."""
    paper = source()
    assert paper.published_at < paper.retrieved_at
    assert paper.known_by(PUBLISHED)
    assert not paper.known_by(PUBLISHED - timedelta(days=1))

    with pytest.raises(ValueError, match="cannot be retrieved before it was published"):
        source(published_at=FETCHED, retrieved_at=PUBLISHED)


def test_only_measurement_gates_promotion_never_literature() -> None:
    """REFUTED.md #13 was rejected on published evidence; that is a different fact."""
    literature = EvidenceBasis(
        citations=(
            cite(
                source(),
                claim="WSB sentiment produces no alpha",
                locator="table 3",
                status=EpistemicStatus.LITERATURE_REFUTED,
            ),
        ),
        status=EpistemicStatus.LITERATURE_REFUTED,
    )
    assert not literature.gates_promotion
    assert not gates_promotion(EpistemicStatus.LITERATURE_SUPPORTED)
    assert not gates_promotion(EpistemicStatus.LITERATURE_REFUTED)
    assert not gates_promotion(EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE)
    assert gates_promotion(EpistemicStatus.MEASURED)

    # And a basis can never be MEASURED: that is what an experiment finds, not what a
    # question starts with, so no amount of reading can produce it.
    with pytest.raises(ValueError, match="not what a hypothesis starts with"):
        EvidenceBasis(citations=(), status=EpistemicStatus.MEASURED)


def test_a_citation_cannot_claim_elrond_measured_something() -> None:
    """A citation records what a source says. Measurement is what this system found."""
    with pytest.raises(ValueError, match="cannot come from a citation"):
        Citation(
            claim="momentum works",
            source_id="paper-momentum-2024",
            locator="abstract",
            status=EpistemicStatus.MEASURED,
        )
    with pytest.raises(ValueError, match="not what a hypothesis starts with"):
        EvidenceBasis(
            citations=(
                cite(
                    source(),
                    claim="momentum works",
                    locator="abstract",
                    status=EpistemicStatus.LITERATURE_SUPPORTED,
                ),
            ),
            status=EpistemicStatus.MEASURED,
        )


def test_a_hypothesis_must_cite_something_or_say_it_cites_nothing() -> None:
    """ "We did not look" and "we looked and there is nothing" are different facts."""
    with pytest.raises(ValueError, match="DATA_DRIVEN_NO_EXTERNAL_SOURCE"):
        EvidenceBasis(citations=(), status=EpistemicStatus.LITERATURE_SUPPORTED)

    mined = EvidenceBasis(citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE)
    assert not mined.gates_promotion

    with pytest.raises(ValueError, match="pick the one that is true"):
        EvidenceBasis(
            citations=(
                cite(
                    source(),
                    claim="x",
                    locator="p1",
                    status=EpistemicStatus.LITERATURE_SUPPORTED,
                ),
            ),
            status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE,
        )


def test_agreement_decides_whether_to_test_not_what_the_answer_is() -> None:
    """Published quant findings replicate poorly, and the bias runs toward positive results."""
    supporting = [
        Citation(
            claim=f"anomaly {index}",
            source_id=f"paper-{index}",
            locator="abstract",
            status=EpistemicStatus.LITERATURE_SUPPORTED,
        )
        for index in range(20)
    ]
    one = supporting[:1]

    # Twenty papers and one paper give the same answer: worth testing. There is deliberately
    # no function here that turns twenty into a stronger prior than one.
    assert worth_testing(supporting) is worth_testing(one) is True
    assert worth_testing([]) is False

    refuting = [
        Citation(
            claim="social sentiment produces alpha",
            source_id="paper-wsb",
            locator="table 3",
            status=EpistemicStatus.LITERATURE_REFUTED,
        )
    ]
    assert worth_testing(refuting) is False
    assert contradicted_by_literature(refuting) == ("social sentiment produces alpha",)


def test_the_same_work_is_stored_once_however_it_arrives() -> None:
    index = SourceIndex([source()])
    assert len(index) == 1

    # Same bytes under a different id.
    assert index.add(source(source_id="mirror", uri="https://mirror.example/p.pdf")) is False
    # Same work, different PDF build, so a different hash but the same title and publisher.
    assert (
        index.add(source(source_id="preprint", content_hash="c" * 64, uri="https://arxiv.org/x"))
        is False
    )
    # A genuinely different work is stored.
    assert index.add(
        source(
            source_id="other",
            title="Time-series momentum in commodities",
            content_hash="d" * 64,
        )
    )
    assert len(index) == 2
    assert index.duplicate_of(source(source_id="mirror")) == "paper-momentum-2024"


def test_a_revision_does_not_silently_replace_the_vintage_an_experiment_used() -> None:
    """A revised macro release is a different source, not an update to the old one."""
    index = SourceIndex([source(kind=SourceKind.MACRO_RELEASE, source_id="cpi-2026-08")])
    assert index.revised("cpi-2026-08", content_hash="a" * 64) is False
    assert index.revised("cpi-2026-08", content_hash="z" * 64) is True

    held = index.get("cpi-2026-08")
    assert held is not None
    assert held.content_hash == "a" * 64


def test_an_experiment_can_name_exactly_the_sources_it_read() -> None:
    index = SourceIndex([source()])
    before = index.snapshot()
    index.add(source(source_id="other", title="Another paper", content_hash="e" * 64))
    assert index.snapshot() != before
    assert len(before) == 64


def test_sources_are_filterable_to_what_existed_at_a_point_in_time() -> None:
    index = SourceIndex(
        [
            source(),
            source(
                source_id="later",
                title="A later paper",
                content_hash="f" * 64,
                published_at=datetime(2026, 1, 5, tzinfo=UTC),
            ),
        ]
    )
    assert len(index.knowable_at(datetime(2025, 1, 1, tzinfo=UTC))) == 1
    assert len(index.knowable_at(FETCHED)) == 2
