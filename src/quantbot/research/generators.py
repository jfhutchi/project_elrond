"""A concrete discovery generator: propose candidates from literature, never from the data (#13).

`discovery.py` defined the `Generator` protocol, the `Candidate` schema and the admission gates,
and nothing implemented the protocol. So the discovery layer could validate a proposal but could
not produce one.

**On #21.** #13 has been treated as blocked on OS-enforced sandbox isolation, on the reading that
a discovery agent needs untrusted code execution. This generator does not, and that changes what
the blocker actually covers. Its inputs are a literature search and a language model. It is never
handed market data, a dataset path, or a registry session, so criterion 4 -- "discovery agents
cannot access protected evaluation data through direct files, APIs, cached artifacts or tools" --
holds by construction rather than by containment. There is nothing to contain.

That is the stronger form of the guarantee and the same argument as `ProtectedAccess` in
`runner.py`: a component that is never given a capability cannot misuse it, whereas a component
that has it and is asked not to use it depends on a sandbox being correct.

A generator that mines data for anomalies is a different thing and genuinely does need #21. This
one is deliberately not that, and `DiscoveryMode.DATA_ANOMALY` is refused here for exactly that
reason rather than left to a policy note.

What it may and may not claim: it reads papers and proposes questions. It cannot assert that
anything works. `EpistemicStatus.MEASURED` is unreachable from here -- that status is what Elrond
measures for itself, and a citation is a reason to look, never a result.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from quantbot.research.discovery import (
    Candidate,
    ContaminatedGenerator,
    DiscoveryMode,
)
from quantbot.research.models import (
    ModelRole,
    ModelRuntime,
    PromptTemplate,
)
from quantbot.research.scout import LiteratureSearch
from quantbot.research.sources import Citation, EpistemicStatus, EvidenceBasis

#: The prompt is versioned and hashed like any other, so a candidate can name what produced it.
LITERATURE_PROMPT = PromptTemplate(
    name="propose-from-literature",
    version="2026-08-21",
    text=(
        "You are proposing a falsifiable market research question from published work.\n"
        "You have NOT seen any market data and must not claim any result holds.\n\n"
        "Papers found for the query {query}:\n{papers}\n\n"
        "Propose one question. Return JSON with keys: question, family_id, overlooked, "
        "why_unpriced, expected_direction, horizon_observations, universe, features, "
        "required_datasets, confounders, simplest_refutation, why_different.\n"
        "universe, features, required_datasets and confounders are arrays of strings. "
        "horizon_observations is an integer number of observations.\n"
        "simplest_refutation must name the cheapest experiment that would refute the question."
    ),
)


class _Proposal(BaseModel):
    """The JSON shape the model is asked for. Validated, never trusted as prose."""

    question: str = Field(min_length=1)
    family_id: str = ""
    overlooked: str = Field(min_length=1)
    why_unpriced: str = Field(min_length=1)
    expected_direction: str = Field(min_length=1)
    horizon_observations: int = Field(ge=1)
    universe: list[str] = Field(min_length=1)
    features: list[str] = Field(min_length=1)
    required_datasets: list[str] = Field(min_length=1)
    confounders: list[str] = Field(min_length=1)
    simplest_refutation: str = Field(min_length=1)
    why_different: str | None = None


class LiteratureGenerator:
    """Propose candidates from a literature search, using a model that never sees market data.

    Satisfies `discovery.Generator`. The mode is `LITERATURE_GAP` and cannot be anything else:
    the other modes make claims about data this generator is structurally unable to have read.
    """

    mode = DiscoveryMode.LITERATURE_GAP

    def __init__(
        self,
        runtime: ModelRuntime,
        search: LiteratureSearch,
        *,
        proposed_by: str,
        family_id: str,
    ) -> None:
        if not search.results:
            # An empty search is a real and useful outcome -- "we looked and found nothing" --
            # but it is not material to propose from. Generating anyway would produce a
            # candidate whose stated basis is literature that does not exist.
            raise ContaminatedGenerator(
                f"the search for {search.query!r} returned nothing, so there is no literature "
                "to propose from; that is a finding to record, not a prompt to fill"
            )
        self._runtime = runtime
        self._search = search
        self._proposed_by = proposed_by
        self._family_id = family_id

    def propose(self, *, now: datetime) -> tuple[Candidate, ...]:
        """Ask for one candidate and build it, or fail. Never returns a partial proposal."""
        parsed, response = self._runtime.call_structured(
            ModelRole.GENERATOR,
            LITERATURE_PROMPT,
            {"query": self._search.query, "papers": _describe(self._search)},
            _Proposal,
            now=now,
        )

        # The claim is what the candidate takes from the paper, and the locator points a reader
        # at it. Neither is invented: the claim names the question this proposal came from and
        # the locator is the paper's own title, so a reader can follow it back.
        citations = tuple(
            Citation(
                claim=f"motivates: {parsed.question}",
                source_id=source.source_id,
                locator=source.title,
                # LITERATURE_SUPPORTED, never MEASURED. The `Citation` validator refuses
                # MEASURED outright -- a citation records what a source claims, and what Elrond
                # found is a different kind of fact.
                status=EpistemicStatus.LITERATURE_SUPPORTED,
            )
            for source in self._search.results
        )
        candidate = Candidate(
            candidate_id=f"C-{response.response_hash[:16]}",
            mode=self.mode,
            question=parsed.question,
            family_id=self._family_id,
            overlooked=parsed.overlooked,
            why_unpriced=parsed.why_unpriced,
            expected_direction=parsed.expected_direction,
            horizon_observations=parsed.horizon_observations,
            universe=tuple(parsed.universe),
            features=tuple(parsed.features),
            required_datasets=tuple(parsed.required_datasets),
            # Declared False rather than assumed True. This generator has not checked point-in-
            # time availability of anything -- it has read abstracts -- and asserting otherwise
            # would put an unverified claim into the one field the compiler trusts about
            # look-ahead. #17's availability machinery decides this, downstream and with data.
            point_in_time_available=False,
            # Empty, and that is the whole point: it read no dataset in any role. The `Candidate`
            # validator refuses any protected role here, but this generator cannot supply one
            # even by mistake because it was never given data to inspect.
            inspected_roles=frozenset(),
            # One question considered, one proposed. A generator that sampled the model
            # repeatedly and returned the best answer would have searched more than it admits,
            # and that is the laundering #23 exists to stop.
            search_cardinality=1,
            confounders=tuple(parsed.confounders),
            simplest_refutation=parsed.simplest_refutation,
            prior_work=tuple(source.title for source in self._search.results),
            why_different=parsed.why_different,
            basis=EvidenceBasis(
                citations=citations,
                # Not MEASURED, which is unreachable from here by design: that status is what
                # Elrond establishes for itself. A paper is a reason to look, not a result.
                status=EpistemicStatus.LITERATURE_SUPPORTED,
            ),
            estimated_trials=1,
            proposed_by=self._proposed_by,
            proposed_at=now,
        )
        _refuse_protected_inspection(candidate)
        return (candidate,)


def _describe(search: LiteratureSearch) -> str:
    """Render the papers for the prompt. Titles and identifiers only.

    Abstracts are deliberately excluded. They are long, they push a small local model past its
    context, and a truncated abstract is a misquoted source -- the model would then propose from
    half a paper while the citation says it read the whole one.
    """
    return "\n".join(f"- {source.title} ({source.source_id})" for source in search.results)


def _refuse_protected_inspection(candidate: Candidate) -> None:
    """Belt and braces: the schema already refuses this, and it is cheap to say so twice."""
    if candidate.inspected_roles:
        raise ContaminatedGenerator(
            f"{candidate.candidate_id} declares inspected data roles, which a literature "
            "generator cannot have read"
        )


def refuse_data_modes(mode: DiscoveryMode) -> None:
    """Reject a mode this generator cannot honestly serve.

    `DATA_ANOMALY` mines data by definition, so it genuinely does need the OS-enforced isolation
    #21 describes. Refusing it here keeps that dependency real rather than letting a mode label
    imply a capability this module does not have.
    """
    if mode is not DiscoveryMode.LITERATURE_GAP:
        raise ContaminatedGenerator(
            f"{mode.value} requires reading data this generator is never given; "
            "it can only propose from literature"
        )


__all__ = [
    "LITERATURE_PROMPT",
    "LiteratureGenerator",
    "refuse_data_modes",
]
