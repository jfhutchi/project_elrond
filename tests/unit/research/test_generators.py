"""The literature generator: what it can propose, and what it structurally cannot reach."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quantbot.research.discovery import ContaminatedGenerator, DiscoveryMode
from quantbot.research.generators import (
    LITERATURE_PROMPT,
    LiteratureGenerator,
    refuse_data_modes,
)
from quantbot.research.models import (
    CostClass,
    ModelCapabilities,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    RoleRouting,
)
from quantbot.research.scout import LiteratureSearch
from quantbot.research.sources import EpistemicStatus, Source, SourceKind

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

ANSWER = {
    "question": "Does dispersion in analyst revisions predict one-month index drawdowns?",
    "overlooked": "revision dispersion is studied per-name, rarely aggregated to the index",
    "why_unpriced": "aggregation requires per-name data most index traders do not hold",
    "expected_direction": "higher dispersion precedes larger drawdowns",
    "horizon_observations": 21,
    "universe": ["SPY"],
    "features": ["analyst_revision_dispersion"],
    "required_datasets": ["ibes-revisions-monthly"],
    "confounders": ["dispersion rises mechanically in high-volatility regimes"],
    "simplest_refutation": "regress next-month drawdown on dispersion; no relation refutes it",
    "why_different": "prior work is cross-sectional; this is an index-level timing claim",
}


def spec(model: str, *, structured: bool = True) -> ModelSpec:
    return ModelSpec(
        provider="ollama",
        model=model,
        version="1",
        endpoint="http://localhost:11434/v1",
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(
            context_tokens=32768,
            structured_output=structured,
            local=True,
            cost_class=CostClass.LOCAL,
        ),
    )


class FakeChat:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def complete(self, model_spec: ModelSpec, prompt: str, *, timeout_seconds: float):
        self.prompts.append(prompt)
        return self.answer, 100, 50


def runtime(answer: str) -> tuple[ModelRuntime, FakeChat]:
    chat = FakeChat(answer)
    routing = RoleRouting(
        chains={ModelRole.GENERATOR: (spec("gen"),), ModelRole.CRITIC: (spec("critic"),)}
    )
    return ModelRuntime(routing, chat), chat


def source(source_id: str = "arxiv:2401.00001v1") -> Source:
    return Source(
        source_id=source_id,
        kind=SourceKind.PAPER,
        uri=f"https://arxiv.org/abs/{source_id}",
        title="Analyst Revision Dispersion and Index Returns",
        publisher="arXiv",
        published_at=datetime(2024, 1, 2, tzinfo=UTC),
        retrieved_at=NOW,
        content_hash="abc123",
        parser_version="arxiv-atom-v1",
    )


def search(*sources: Source) -> LiteratureSearch:
    return LiteratureSearch(
        connector="arxiv",
        query="analyst revision dispersion",
        searched_at=NOW,
        total_matched=len(sources),
        results=sources,
    )


def test_a_proposed_candidate_declares_that_it_inspected_no_data() -> None:
    """#13 criterion 4, satisfied by construction rather than by containment.

    This generator is handed a literature search and a model. It is never given market data, a
    dataset path or a registry session, so it cannot inspect a protected window -- there is
    nothing to contain. That is why #13 does not need #21 for this mode.
    """
    model, chat = runtime(json.dumps(ANSWER))
    generator = LiteratureGenerator(
        model, search(source()), proposed_by="claude-opus-5", family_id="revisions"
    )

    (candidate,) = generator.propose(now=NOW)

    assert candidate.inspected_roles == frozenset()
    assert candidate.inspected_protected_data is False
    assert candidate.mode is DiscoveryMode.LITERATURE_GAP
    # The prompt carries titles, never data.
    assert "Analyst Revision Dispersion" in chat.prompts[0]


def test_a_literature_candidate_cannot_claim_a_measured_result() -> None:
    """A paper is a reason to look, never a result.

    `MEASURED` is what Elrond establishes for itself. The `Citation` validator refuses it
    outright, and this asserts the generator does not attempt it either.
    """
    model, _ = runtime(json.dumps(ANSWER))
    (candidate,) = LiteratureGenerator(
        model, search(source()), proposed_by="claude", family_id="revisions"
    ).propose(now=NOW)

    assert candidate.basis.status is EpistemicStatus.LITERATURE_SUPPORTED
    assert candidate.basis.citations
    for citation in candidate.basis.citations:
        assert citation.status is not EpistemicStatus.MEASURED


def test_point_in_time_availability_is_declared_false_rather_than_assumed() -> None:
    """The generator has read abstracts, not checked availability.

    `point_in_time_available` is the one field the compiler trusts about look-ahead. Asserting
    True here would put an unverified claim into it; #17's machinery decides this downstream,
    with data.
    """
    model, _ = runtime(json.dumps(ANSWER))
    (candidate,) = LiteratureGenerator(
        model, search(source()), proposed_by="claude", family_id="revisions"
    ).propose(now=NOW)

    assert candidate.point_in_time_available is False


def test_an_empty_search_is_not_something_to_propose_from() -> None:
    """ "We looked and found nothing" is a finding to record, not a prompt to fill.

    Generating anyway would produce a candidate whose stated basis is literature that does not
    exist -- a fabricated provenance, which is worse than no candidate.
    """
    model, _ = runtime(json.dumps(ANSWER))

    with pytest.raises(ContaminatedGenerator, match="returned nothing"):
        LiteratureGenerator(model, search(), proposed_by="claude", family_id="revisions")


def test_a_data_mining_mode_is_refused_rather_than_relabelled() -> None:
    """`DATA_ANOMALY` genuinely needs #21; refusing it keeps that dependency real.

    Letting a mode label imply a capability this module does not have is how a blocked
    dependency gets quietly declared satisfied.
    """
    refuse_data_modes(DiscoveryMode.LITERATURE_GAP)

    for mode in (DiscoveryMode.DATA_ANOMALY,):
        with pytest.raises(ContaminatedGenerator, match="requires reading data"):
            refuse_data_modes(mode)


def test_the_search_cardinality_is_what_was_considered_not_what_was_kept() -> None:
    """One question asked, one proposed, one trial declared (#23).

    A generator that sampled the model repeatedly and returned its favourite would have searched
    more than it admits, which is exactly the laundering the search-origin work exists to stop.
    """
    model, chat = runtime(json.dumps(ANSWER))
    (candidate,) = LiteratureGenerator(
        model, search(source(), source("arxiv:2402.00002v1")), proposed_by="c", family_id="f"
    ).propose(now=NOW)

    assert candidate.search_cardinality == 1
    assert len(chat.prompts) == 1, "one model call, so one candidate considered"


def test_a_model_that_answers_with_prose_produces_no_candidate() -> None:
    """A partial proposal is not returned. Structured output is validated or it fails."""
    model, _ = runtime("I think revision dispersion is interesting, honestly")
    generator = LiteratureGenerator(
        model, search(source()), proposed_by="claude", family_id="revisions"
    )

    with pytest.raises(Exception, match="did not return JSON"):
        generator.propose(now=NOW)


def test_the_prompt_is_versioned_so_a_candidate_can_name_what_produced_it() -> None:
    """Provenance: the same question from a different prompt version is a different proposal."""
    assert LITERATURE_PROMPT.version
    assert LITERATURE_PROMPT.template_hash
    assert "must not claim any result holds" in LITERATURE_PROMPT.text
