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

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.research.discovery import Candidate, DiscoveryMode, admissible
from quantbot.research.power import EffectSpecification
from quantbot.research.registry import (
    DataWindow,
    HypothesisDraft,
    SearchOrigin,
    WindowConsumption,
)
from quantbot.storage.schema import search_runs


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
    session: Session | None = None,
    search_run_id: str | None = None,
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

    origin, attester, cardinality = _origin_for(
        candidate, session=session, search_run_id=search_run_id, attested_by=attested_by
    )
    return HypothesisDraft(
        hypothesis_id=hypothesis_id,
        family_id=candidate.family_id,
        graph=candidate.graph,
        lineage=candidate.lineage,
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
        # Read from the durable search record when one is named, and only carried from the
        # candidate otherwise. If whoever registers the candidate declared this themselves, a
        # search of five hundred could arrive as a search of one and the multiple-testing burden
        # would silently under-count (#23) -- so on the measured path the payload does not get a
        # vote, and editing `candidate.search_cardinality` cannot lower what gets registered.
        search_cardinality=cardinality,
        search_origin=origin,
        search_attested_by=attester,
        search_run_id=search_run_id if origin is SearchOrigin.MEASURED else None,
        confounders=candidate.confounders,
        materially_different=candidate.why_different,
        basis=candidate.basis,
        proposed_by=candidate.proposed_by,
    )


def _origin_for(
    candidate: Candidate,
    *,
    session: Session | None,
    search_run_id: str | None,
    attested_by: str | None,
) -> tuple[SearchOrigin, str | None, int]:
    """Where the trial count came from, in the registry's vocabulary, and what it is.

    **Measured telemetry is the normal path for a data-dependent candidate (#73).** The rule this
    replaces required a human to stand behind any data-dependent count, on the grounds that an
    agent's unattested self-report is the one origin #23 refuses. That was correct when nothing
    emitted search telemetry. It is now stale and, worse, backwards: a durable `search_runs` row
    written by the process that did the searching is *stronger* evidence than a person recalling
    a number, and requiring the weaker one dead-ended autonomous discovery on a signature.

    What #23 actually refuses is a count an agent can choose. That is still refused here, by
    reading the count from the row rather than from the candidate: on the measured path the
    payload does not get a vote, so editing `search_cardinality` cannot lower what is registered.

    A candidate with no disclosed burden is **not** blocked pending an attestation. It registers
    with the origin it can support and is confirmatorily inadmissible on that basis -- the
    honest outcome, and one nobody has to be woken up for.
    """
    if candidate.search_cardinality == 1 and candidate.mode is not DiscoveryMode.DATA_ANOMALY:
        return SearchOrigin.NO_DATA_DEPENDENT_SEARCH, None, candidate.search_cardinality

    if search_run_id is not None:
        if session is None:
            raise CandidateNotRegistrable(
                f"{candidate.candidate_id} names search run {search_run_id!r} but no session was "
                "given to read it; a measured origin has to actually read the measurement"
            )
        recorded = session.execute(
            select(search_runs.c.candidates_evaluated).where(
                search_runs.c.search_id == search_run_id
            )
        ).scalar_one_or_none()
        if recorded is None:
            # Fail closed. A MEASURED origin naming a run nobody logged is a stronger claim than
            # a self-report, made on less evidence.
            raise CandidateNotRegistrable(
                f"{candidate.candidate_id} cites search run {search_run_id!r}, which is not "
                "recorded; a measured count must come from a row that exists"
            )
        return SearchOrigin.MEASURED, None, int(recorded)

    if attested_by is not None:
        # Retained for backfill and for a human who genuinely did the searching. No longer the
        # route an autonomous discovery run is expected to take.
        return SearchOrigin.OPERATOR_ATTESTED, attested_by, candidate.search_cardinality

    raise CandidateNotRegistrable(
        f"{candidate.candidate_id} reports a search of {candidate.search_cardinality} candidates "
        "with no durable search run behind it. An agent may not self-report a trial count, so "
        "the discovery path must record its search (#23) rather than have someone vouch for the "
        "number afterwards"
    )


__all__ = ["CandidateNotRegistrable", "draft_from_candidate"]
