"""A pretrained model reading old text already knows how the story ended (#37, #44).

The defect is invisible in the output, which is what makes it worth a mechanism rather than a
rule. A 2024-cutoff model scoring 2019 sentiment produces a *better* signal than an honest one,
because it is scoring against known outcomes. Nothing downstream flags it; it just looks good.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from quantbot.research.hindsight import (
    HindsightRefused,
    assess_hindsight,
    require_no_hindsight,
)
from quantbot.research.sources import Source, SourceKind

CUTOFF = date(2024, 6, 1)


def source(source_id: str, published: date) -> Source:
    moment = datetime(published.year, published.month, published.day, tzinfo=UTC)
    return Source(
        source_id=source_id,
        kind=SourceKind.SEC_FILING,
        uri=f"https://example.invalid/{source_id}",
        title="10-K",
        publisher="SEC EDGAR",
        published_at=moment,
        retrieved_at=datetime(2026, 8, 22, tzinfo=UTC),
        content_hash="c" * 64,
        parser_version="v1",
    )


def test_text_published_after_the_cutoff_is_usable() -> None:
    """The forward path, which is where this component is genuinely valuable.

    Analysing text as it arrives is not contaminated: the model cannot know what it never saw.
    """
    recent = [source("post-1", date(2025, 3, 1)), source("post-2", date(2026, 1, 15))]

    assessment = assess_hindsight(recent, model="fingpt-1.0", cutoff=CUTOFF)

    assert assessment.usable_as_evidence is True
    assert assessment.contaminated == ()
    assert len(assessment.clean) == 2
    assert "postdates" in assessment.reason


def test_text_published_before_the_cutoff_is_contaminated() -> None:
    """The observed hazard: the model already knows which theses were vindicated."""
    old = [source("pre-1", date(2019, 4, 1)), source("post-1", date(2025, 3, 1))]

    assessment = assess_hindsight(old, model="fingpt-1.0", cutoff=CUTOFF)

    assert assessment.usable_as_evidence is False
    assert assessment.contaminated == ("pre-1",)
    assert assessment.clean == ("post-1",)
    # The denominator travels with the numerator: one of two is a different fact from one of two
    # hundred, and a report giving only the count invites reading them as the same.
    assert "1 of 2 sources" in assessment.reason


def test_a_source_published_exactly_on_the_cutoff_is_contaminated() -> None:
    """The boundary resolves toward caution.

    A cutoff is an approximate, self-reported boundary on a training corpus, not a precise
    instant. Treating same-day text as clean would be trusting that approximation in the one
    direction where being wrong costs evidence.
    """
    assessment = assess_hindsight(
        [source("edge", CUTOFF)], model="fingpt-1.0", cutoff=CUTOFF
    )

    assert assessment.contaminated == ("edge",)


def test_an_unrecorded_cutoff_contaminates_everything_rather_than_nothing() -> None:
    """Absent is not exempt, and the direction is the whole point.

    If a missing cutoff read as clean, forgetting to record one would be the cheapest way to get
    evidence — a gap that rewards the omission. It costs evidentiary weight instead.
    """
    recent = [source("post-1", date(2026, 1, 15))]

    assessment = assess_hindsight(recent, model="mystery-model", cutoff=None)

    assert assessment.usable_as_evidence is False
    assert assessment.contaminated == ("post-1",)
    assert "no recorded training cutoff" in assessment.reason


def test_the_confirmatory_path_refuses_rather_than_marking() -> None:
    """Two callers, two behaviours.

    A caller that will mark its result exploratory uses `assess_hindsight` and continues. A
    caller on the confirmatory path must not proceed at all, because a result that reaches the
    registry is one somebody will later cite.
    """
    mixed = [source("pre-1", date(2019, 4, 1)), source("post-1", date(2025, 3, 1))]

    with pytest.raises(HindsightRefused, match="published before"):
        require_no_hindsight(mixed, model="fingpt-1.0", cutoff=CUTOFF)

    # And it returns the assessment when clean, so the caller can record what was checked.
    clean = require_no_hindsight(
        [source("post-1", date(2025, 3, 1))], model="fingpt-1.0", cutoff=CUTOFF
    )
    assert clean.usable_as_evidence is True


def test_publication_rather_than_retrieval_decides() -> None:
    """When a document was fetched says nothing about whether the model saw it in training.

    Every source here was retrieved in 2026. Only publication distinguishes them, and comparing
    against `retrieved_at` would mark a 2019 filing clean because we downloaded it yesterday --
    which is the defect this module exists to prevent, inverted.
    """
    old = source("pre-1", date(2019, 4, 1))
    assert old.retrieved_at.year == 2026

    assessment = assess_hindsight([old], model="fingpt-1.0", cutoff=CUTOFF)

    assert assessment.contaminated == ("pre-1",)


def test_the_model_spec_can_carry_a_cutoff() -> None:
    """The rule is only enforceable if the fact has somewhere durable to live."""
    from quantbot.research.models import CostClass, ModelCapabilities, ModelSpec

    spec = ModelSpec(
        provider="openai-compatible",
        model="fingpt",
        version="1.0",
        endpoint="http://localhost:11434/v1",
        capabilities=ModelCapabilities(
            context_tokens=32768,
            structured_output=True,
            local=True,
            cost_class=CostClass.LOCAL,
        ),
        training_cutoff="2024-06-01",
        training_cutoff_source="model card",
    )

    assert spec.training_cutoff == "2024-06-01"
    # Absent by default, so existing specs stay valid and are treated as unknown rather than
    # silently clean.
    bare = spec.model_copy(update={"training_cutoff": None, "training_cutoff_source": ""})
    assert bare.training_cutoff is None


def test_analysing_nothing_is_not_clean_analysis() -> None:
    """An empty corpus has no contaminated sources and is not therefore evidence.

    Surfaced by a surviving mutation: with sources present, "no cutoff" and "everything
    contaminated" coincide, so dropping the cutoff requirement from `usable_as_evidence` changed
    nothing observable. With *no* sources they come apart -- nothing is contaminated because
    nothing was read.

    Both halves matter. A model with no cutoff analysing nothing must not be usable, and neither
    must a known-cutoff model that read nothing: an assessment over an empty corpus is a
    statement about zero documents, and the honest answer is that no evidence was established.
    """
    unknown_model = assess_hindsight([], model="mystery-model", cutoff=None)
    assert unknown_model.contaminated == ()
    assert unknown_model.usable_as_evidence is False, "no cutoff and no sources is not evidence"

    known_model = assess_hindsight([], model="fingpt-1.0", cutoff=CUTOFF)
    assert known_model.contaminated == ()
    assert known_model.clean == ()
    assert known_model.usable_as_evidence is False, "reading nothing establishes nothing"


def test_a_cutoff_without_a_source_is_refused() -> None:
    """A cutoff matters most when it is wrong in the unsafe direction.

    Guessed *earlier* than the truth, text believed post-cutoff was in training after all, and
    the contamination check passes while the contamination is real. Recording where the number
    came from is the difference between a cutoff and a guess, and this project has repeatedly
    found that an input accepted without provenance is one somebody eventually sets to whatever
    is convenient.
    """
    from pydantic import ValidationError

    from quantbot.research.models import CostClass, ModelCapabilities, ModelSpec

    def spec(**overrides):
        fields = {
            "provider": "openai-compatible",
            "model": "fingpt",
            "version": "1.0",
            "endpoint": "http://localhost:11434/v1",
            "capabilities": ModelCapabilities(
                context_tokens=32768,
                structured_output=True,
                local=True,
                cost_class=CostClass.LOCAL,
            ),
        }
        fields.update(overrides)
        return ModelSpec(**fields)

    with pytest.raises(ValidationError, match="where it came from"):
        spec(training_cutoff="2024-06-01")

    attributed = spec(training_cutoff="2024-06-01", training_cutoff_source="model card")
    assert attributed.training_cutoff_source == "model card"

    # A spec with no cutoff at all stays valid -- it is treated as unknown by `hindsight.py`,
    # which is the conservative reading, rather than being blocked from existing.
    assert spec().training_cutoff is None


def test_cutoff_status_reaches_the_manifest() -> None:
    """#45 and #18: a result downgraded for contamination and one never checked look the same
    afterwards unless the bundle says which.

    That distinction is what a later reader needs to tell an evidentiary downgrade from an
    absence of assessment, and it is exactly the information that vanishes if only the verdict
    is stored.
    """
    mixed = [source("pre-1", date(2019, 4, 1)), source("post-1", date(2025, 3, 1))]

    entry = assess_hindsight(
        mixed, model="fingpt-1.0", cutoff=CUTOFF, cutoff_source="model card"
    ).manifest_entry()

    assert entry["hindsight_model"] == "fingpt-1.0"
    assert entry["hindsight_cutoff"] == "2024-06-01"
    assert entry["hindsight_cutoff_source"] == "model card"
    assert entry["hindsight_sources_contaminated"] == "1"
    assert entry["hindsight_sources_clean"] == "1"
    assert entry["hindsight_usable_as_evidence"] == "false"

    # An unassessed model records that it was unrecorded rather than omitting the key, so a
    # bundle without the field means "this predates the rule", not "this was checked and clean".
    never = assess_hindsight(mixed, model="mystery", cutoff=None).manifest_entry()
    assert never["hindsight_cutoff"] == "unrecorded"
    assert never["hindsight_cutoff_source"] == "unattributed"
