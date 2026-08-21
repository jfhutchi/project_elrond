"""Carry a candidate from discovery into registration, without inventing what it does not say.

This is the last structural link in the chain. #13 could produce a `Candidate` and #5 could
freeze a `HypothesisDraft`, and nothing turned the first into the second -- so a generated
proposal could never reach the gates that judge it.

**The conversion is not total, and that is a finding rather than an inconvenience.** A candidate
says what to test; a draft says what would count as an effect worth acting on. Those are
different claims and the schema for the first has no field for the second:

* `EffectSpecification` -- the expected effect, its units, its dependence structure and its
  costs. A candidate proposing "dispersion predicts drawdowns" says nothing about how large the
  relationship must be to matter, and the power gate cannot run without that number.
* `DataWindow` -- which dataset ranges, in which roles. A candidate names `required_datasets`
  but not the spans, and the span is what determines whether the sample can resolve anything.
* `available_observations` -- a count from the data, which discovery never touched.

Nothing here guesses any of them. Deriving an effect size from a candidate's prose would put a
fabricated number into the one input the power gate exists to check, and every gate downstream
would then be measuring a claim nobody made. `draft_from_candidate` requires them and refuses
without them.

What it does carry across is everything the candidate genuinely established: the question, the
universe, the features, the confounders, the falsification criterion, the citations and their
epistemic status, and -- importantly -- the search cardinality and its origin, so the
multiple-testing burden follows the candidate rather than being restated by whoever registers it.
"""

from __future__ import annotations

from collections.abc import Sequence

from quantbot.research.discovery import Candidate, DiscoveryMode, admissible
from quantbot.research.power import EffectSpecification
from quantbot.research.registry import (
    DataWindow,
    HypothesisDraft,
    SearchOrigin,
    WindowConsumption,
)


class CandidateNotRegistrable(ValueError):
    """Raised when a candidate cannot become a draft without something being invented."""


def draft_from_candidate(
    candidate: Candidate,
    *,
    effect: EffectSpecification,
    windows: Sequence[DataWindow],
    available_observations: int,
    hypothesis_id: str,
    consumption: Sequence[WindowConsumption] = (),
    forward_evidence: bool = False,
    attested_by: str | None = None,
) -> HypothesisDraft:
    """Turn an admissible candidate into a registrable draft.

    `effect`, `windows` and `available_observations` are required because the candidate does not
    contain them and this must not make them up. The caller supplying them is the caller that
    looked at the data, which is the same boundary `verify_for_execution` draws.
    """
    allowed, why = admissible(candidate, consumption=consumption, forward_evidence=forward_evidence)
    if not allowed:
        # Checked before anything else because it is the cheap gate: a candidate whose data
        # cannot support a confirmatory answer should not reach the power gate or the critic,
        # both of which cost compute.
        raise CandidateNotRegistrable(f"{candidate.candidate_id} is not admissible: {why}")

    if not windows:
        raise CandidateNotRegistrable(
            f"{candidate.candidate_id} names datasets {list(candidate.required_datasets)} but "
            "no window spans; a dataset without a range says nothing about whether the sample "
            "can resolve the question"
        )

    declared = {window.dataset for window in windows}
    missing = sorted(set(candidate.required_datasets) - declared)
    if missing:
        raise CandidateNotRegistrable(
            f"{candidate.candidate_id} requires {', '.join(missing)} and no window covers it; "
            "registering against a subset would freeze a different question"
        )

    origin, attester = _origin_for(candidate, attested_by)
    return HypothesisDraft(
        hypothesis_id=hypothesis_id,
        family_id=candidate.family_id,
        question=candidate.question,
        # The candidate's expected direction is the prediction. The effect *size* comes from the
        # caller, and the two are deliberately separate: a direction is a claim discovery can
        # make, a magnitude is not.
        prediction=candidate.expected_direction,
        null_hypothesis=f"there is no relationship: {candidate.question}",
        falsified_if=candidate.simplest_refutation,
        universe=candidate.universe,
        features=candidate.features,
        target=f"{candidate.horizon_observations}-observation forward outcome",
        windows=tuple(windows),
        primary_estimand=effect.estimand.value,
        effect=effect,
        available_observations=available_observations,
        # Carried, not restated. If whoever registers the candidate declared this themselves,
        # a search of five hundred could arrive as a search of one and the multiple-testing
        # burden would silently under-count (#23).
        search_cardinality=candidate.search_cardinality,
        search_origin=origin,
        search_attested_by=attester,
        confounders=candidate.confounders,
        materially_different=candidate.why_different,
        basis=candidate.basis,
        proposed_by=candidate.proposed_by,
    )


def _origin_for(candidate: Candidate, attested_by: str | None) -> tuple[SearchOrigin, str | None]:
    """Where the candidate's trial count came from, in the registry's vocabulary.

    A candidate that searched nothing carries the origin that says so. A candidate that searched
    data needs a human to stand behind the number, because an agent's unattested self-report is
    the one origin #23 refuses -- and discovery is an agent.
    """
    if candidate.search_cardinality == 1 and candidate.mode is not DiscoveryMode.DATA_ANOMALY:
        return SearchOrigin.NO_DATA_DEPENDENT_SEARCH, None
    if attested_by is None:
        raise CandidateNotRegistrable(
            f"{candidate.candidate_id} reports a search of {candidate.search_cardinality} "
            "candidates. An agent may not self-report a trial count, so this needs an operator "
            "attestation naming who is standing behind it"
        )
    return SearchOrigin.OPERATOR_ATTESTED, attested_by


__all__ = ["CandidateNotRegistrable", "draft_from_candidate"]
