"""Frozen, machine-readable hypothesis registrations (#5) behind the power gate (#19).

A registration is a prediction written down *before* the measurement, hashed, and stored so it
cannot be restated afterwards. Cycle 12 is the worked example: "the growth-optimal leverage is
below 2x" measured 3.25x. Because the prediction was frozen it is logged as a miss. Unfrozen it
would have become "an interior optimum exists" -- true, unfalsifiable, and worthless.

Four things are refused here rather than left to judgement, because software can prove them:

1. **A false holdout claim.** A `PROTECTED_EVALUATION` window may not overlap a date range
   another registration still holds on the same dataset, nor this registration's own discovery
   or validation ranges. This is the check that makes the project's structural limit visible
   instead of leaving it in a paragraph: SIP data begins 2016-01-04 and cycles 2-10 consumed
   all of it, so any confirmatory claim on that window is in-sample whatever the schema says.
   "Still holds" is the whole of #22: registering *reserves* a window and the reservation
   lapses on its own, because abandonment is the absence of events and nothing observes it;
   `consume` spends it at the moment the data is handed to an experiment, and that is
   permanent. A hypothesis that is frozen and then dropped no longer sterilises the holdout.
2. **An unresolvable question.** `UNDERPOWERED`, computed by `power.py` from the declared
   estimand and its dependence structure. Not the same verdict as `REFUTED`.
3. **A question not worth resolving.** `UNECONOMIC`: the smallest effect worth acting on cannot
   pay its own annual trading cost.
3b. **A question already asked.** `DUPLICATE`: a candidate overlapping a prior hypothesis on
   universe and features, unless the registrant writes down what is materially different.
   Momentum has now returned null on three structurally independent universes because each new
   one had a plausible excuse; without a written record, universe four gets proposed next week.
3c. **A question the critic blocked.** `CRITIQUE_BLOCKS`: a registration cannot be frozen
   without a critic verdict on that exact version, and a `REVISE` or `REJECT` stops it. The
   critique is frozen *inside* the registration, so a critic cannot rewrite one afterwards.
4. **A declared rather than counted multiple-testing burden.** The trial count is computed from
   durable state -- everything this project has ever spent, seeded at what cycles 1-17 used --
   not taken from the registrant. Deflating against one cycle's trials instead of the project's
   is the standard way this goes wrong.

Every one of those decisions is recorded as a `PowerAssessment`, at registration and again
before each confirmatory run, so a later agent can tell "we could not test this" from "we
tested this and it failed". An operator may override an underpowered registration; the override
is audited and the verdict stays `OVERRIDDEN` rather than becoming `POWERED`.

Scope boundary. #6 owns refutation memory and the structured migration of `REFUTED.md`; nothing
here rewrites that record.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.research.critic import Critique
from quantbot.research.datasets import equivalent_datasets
from quantbot.research.manifest import canonical_result_json
from quantbot.research.power import (
    EffectSpecification,
    PowerAssessment,
    PowerOverride,
    PowerVerdict,
    assess,
    explain,
    luck_threshold,
)
from quantbot.research.sources import EvidenceBasis
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import (
    hypotheses,
    hypothesis_data_windows,
    power_assessments,
    search_runs,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Candidate evaluations this project ran before the registry existed. Derived from the bar
#: cycle 17 applied (2.90 = sqrt(2 ln N), so N ~= 67) and consistent with `REFUTED.md`'s
#: "~43 candidate evaluations" at cycle 10. Seeded rather than assumed zero: a registry that
#: started counting from one would hand back a luck bar the project has already spent.
PRIOR_TRIALS = 68

REGISTRATION = "REGISTRATION"
EXECUTION = "EXECUTION"

#: Jaccard overlap on universe + features at or above which a candidate is treated as a repeat
#: of something already asked. Set so swapping one symbol does not read as a new idea.
DUPLICATE_OVERLAP = 0.8

#: Trials against one dataset range beyond which it is treated as spent. Cycles 2-10 put roughly
#: 68 candidate evaluations against 2016-2026 US equities, which is why that window is exhausted
#: and why the number sits here.
EXHAUSTED_TRIALS = 40

#: How long a registration's claim on its declared windows blocks other registrations (#22).
#: Long enough that a hypothesis actually being worked on keeps its holdout, short enough that an
#: abandoned one stops sterilising the data within a research cycle. A reservation must lapse
#: rather than wait to be released: nothing observes abandonment, so an explicitly-released-only
#: reservation is the old permanent-consumption behaviour wearing a state column.
RESERVATION_DAYS = 90


class SearchOrigin(StrEnum):
    """Where a registration's trial count came from (#23).

    `search_cardinality` was previously a bare self-report added straight into the
    multiple-testing burden, so a miner that evaluated 5,000 candidates could declare 1 and
    permanently understate the project's budget. `workers.py` already refuses to trust an
    external engine that will not disclose its cardinality -- `WorkerTrust.EXPLORATORY_ONLY` --
    and the registry simply did not participate in that policy. These are the origins it accepts.

    An agent's unattested self-report is deliberately absent. That is the laundering path.
    """

    #: Theory or literature. Nothing data-dependent was searched, so the count is pinned to 1.
    NO_DATA_DEPENDENT_SEARCH = "NO_DATA_DEPENDENT_SEARCH"
    #: A human states the number and is recorded as having stated it. Auditable, and what keeps
    #: the gate from paralysing research before a discovery engine exists to measure it (#13).
    OPERATOR_ATTESTED = "OPERATOR_ATTESTED"
    #: Derived from a durable `search_runs` row. Refused outright until 0009, because while
    #: nothing emitted search telemetry a registration claiming MEASURED was claiming provenance
    #: that could not exist -- which would have reopened the laundering path one enum value
    #: across from where #23 closed it. `SubprocessWorker` now discloses a cardinality and
    #: `record_search_run` persists it, so the registry checks that the row exists *and* that
    #: the declared count matches it. Naming a real search and registering a smaller number is
    #: the original defect wearing a reference, so both halves are verified.
    MEASURED = "MEASURED"
    #: Rows that predate this distinction. Never selectable by a new registration; it exists so
    #: the 0007 backfill does not have to call those rows attested or measured, which would
    #: launder exactly the claim the origin was added to stop.
    LEGACY_SELF_REPORTED = "LEGACY_SELF_REPORTED"


#: Origins a new registration may declare. `LEGACY_SELF_REPORTED` is readable and never
#: writable, and is refused by `HypothesisDraft.validate_draft` with its own explanation.
REGISTRABLE_ORIGINS = frozenset(
    {
        SearchOrigin.NO_DATA_DEPENDENT_SEARCH,
        SearchOrigin.OPERATOR_ATTESTED,
        # Reachable since 0009: `SubprocessWorker` emits a disclosed cardinality and
        # `record_search_run` persists it, so the count can be derived from a row rather than
        # asserted. The registry verifies the row exists and matches before accepting it.
        SearchOrigin.MEASURED,
    }
)


class WindowState(StrEnum):
    """Whether a declared window is merely claimed or actually spent (#22)."""

    #: Claimed at registration. Blocks overlap only while live, and lapses on its own, because
    #: abandonment is the absence of events rather than an event anyone will report.
    RESERVED = "RESERVED"
    #: The data was handed to an experiment. Permanent: contamination is a fact about what was
    #: seen, not about a record, so no expiry brings it back.
    CONSUMED = "CONSUMED"
    #: Given up before the data was read. Stops blocking.
    RELEASED = "RELEASED"


class DataRole(StrEnum):
    """What a dataset is allowed to be used for, per the v0.2 protected-data model."""

    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"
    PROTECTED_EVALUATION = "PROTECTED_EVALUATION"
    FORWARD_PAPER = "FORWARD_PAPER"


class RefusalReason(StrEnum):
    CONTAMINATED_WINDOW = "CONTAMINATED_WINDOW"
    DUPLICATE = "DUPLICATE"
    CRITIQUE_BLOCKS = "CRITIQUE_BLOCKS"
    CRITIQUE_MISMATCHED = "CRITIQUE_MISMATCHED"
    UNDERPOWERED = "UNDERPOWERED"
    UNECONOMIC = "UNECONOMIC"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    NOT_REGISTERED = "NOT_REGISTERED"
    TAMPERED = "TAMPERED"
    UNVERIFIED_SAMPLE = "UNVERIFIED_SAMPLE"
    #: A window lifecycle transition asked for from a state that does not permit it (#22).
    WINDOW_NOT_RESERVED = "WINDOW_NOT_RESERVED"


#: A blocking power verdict, mapped to the refusal it becomes.
_POWER_REFUSALS = {
    PowerVerdict.UNDERPOWERED: RefusalReason.UNDERPOWERED,
    PowerVerdict.UNECONOMIC: RefusalReason.UNECONOMIC,
}


class RegistrationRefused(ValueError):
    """Raised when a hypothesis may not be registered, or may not proceed to execution.

    Carries the `PowerAssessment` when one was produced. The transaction that raised this has
    already rolled back by the time anyone sees it, so the registry cannot have persisted the
    refusal itself -- the caller records it in its own transaction via `record_assessment`,
    which is how an `UNDERPOWERED` outcome survives as research memory rather than vanishing.
    """

    def __init__(
        self,
        reason: RefusalReason,
        detail: str,
        *,
        assessment: PowerAssessment | None = None,
    ) -> None:
        self.reason = reason
        self.detail = detail
        self.assessment = assessment
        super().__init__(f"{reason.value}: {detail}")


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DataWindow(FrozenModel):
    """One contiguous date range of one dataset vintage, used for one purpose."""

    #: Stable join key across registrations; overlap is only meaningful within one dataset.
    dataset: Text
    #: Vintage/snapshot identifier, so a re-pulled dataset is not confused with the original.
    snapshot: Text
    role: DataRole
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end < self.start:
            raise ValueError("data window end cannot precede start")
        return self

    def overlaps(self, other: DataWindow) -> bool:
        return self.dataset == other.dataset and self.start <= other.end and other.start <= self.end


class HypothesisDraft(FrozenModel):
    """Everything the registrant declares. Computed fields are added at registration."""

    hypothesis_id: Text
    version: int = Field(default=1, ge=1)
    family_id: Text
    parent_hypothesis_id: str | None = None
    parent_version: int | None = Field(default=None, ge=1)

    question: Text
    prediction: Text
    null_hypothesis: Text
    #: The whole point of the freeze. A registration without it cannot be falsified.
    falsified_if: Text

    universe: tuple[Text, ...] = Field(min_length=1)
    features: tuple[Text, ...] = Field(min_length=1)
    target: Text
    portfolio_interpretation: str | None = None

    windows: tuple[DataWindow, ...] = Field(min_length=1)

    primary_estimand: Text
    secondary_diagnostics: tuple[Text, ...] = ()
    #: The claimed effect, its units, its dependence structure, and its costs. See `power.py`.
    effect: EffectSpecification
    #: Observations the requested windows actually supply, in the estimand's sampling unit.
    available_observations: int = Field(ge=1)
    #: How many candidates were inspected to arrive at this one, counting itself. 1 when the
    #: candidate came from theory or literature rather than from mining data. This is the
    #: registration's whole cost against the project's multiple-testing budget.
    search_cardinality: int = Field(default=1, ge=1)
    #: Where that count came from. Defaults to the only origin whose count is self-evident: a
    #: candidate that searched no data has a cardinality of 1 by construction, so declaring
    #: nothing claims nothing. Any other count has to say where it came from (#23).
    search_origin: SearchOrigin = SearchOrigin.NO_DATA_DEPENDENT_SEARCH
    #: Who attested the count. Required for, and only for, `OPERATOR_ATTESTED`.
    search_attested_by: str | None = None
    #: The durable `search_runs` record this count was derived from. Required for, and only
    #: for, `MEASURED`. The registry checks the row exists and that the count matches it (#23).
    search_run_id: str | None = None

    confounders: tuple[Text, ...] = Field(min_length=1)
    #: Required only when this candidate overlaps a prior hypothesis. Writing down what is
    #: different is the whole gate: it is cheap when the difference is real and impossible to
    #: fill in honestly when it is not.
    materially_different: str | None = None
    #: What backs the question before anything is measured: citations, or an explicit statement
    #: that there are none (#4). No default, because silence would then be the common answer
    #: and "we did not look" would be indistinguishable from "there is nothing".
    basis: EvidenceBasis
    proposed_by: Text
    prompt_version: str | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        if (self.parent_hypothesis_id is None) != (self.parent_version is None):
            raise ValueError("a parent reference needs both id and version")
        if self.search_origin is SearchOrigin.LEGACY_SELF_REPORTED:
            raise ValueError(
                "LEGACY_SELF_REPORTED describes rows that predate the search-origin distinction "
                "and cannot be declared by a new registration"
            )
        if (self.search_origin is SearchOrigin.MEASURED) != (self.search_run_id is not None):
            raise ValueError(
                "MEASURED derives its count from a durable search record, so it must name one "
                "and only it may. Accepting the origin without a record would reopen the "
                "laundering path #23 closed: an agent forbidden from self-reporting a count "
                "could relabel it as measured and be believed on less evidence"
            )
        if (self.search_origin is SearchOrigin.OPERATOR_ATTESTED) != (
            self.search_attested_by is not None
        ):
            raise ValueError(
                "an operator-attested trial count must name the operator attesting it, and only "
                "an attested count may name one -- an unsigned attestation is a self-report"
            )
        if (
            self.search_origin is SearchOrigin.NO_DATA_DEPENDENT_SEARCH
            and self.search_cardinality != 1
        ):
            raise ValueError(
                f"a candidate from theory or literature searched nothing, so it cannot claim "
                f"{self.search_cardinality} trials; declare where the count came from"
            )
        seen = {(window.dataset, window.role) for window in self.windows}
        if len(seen) != len(self.windows):
            raise ValueError("declare one contiguous window per dataset and role")
        return self

    def windows_for(self, *roles: DataRole) -> tuple[DataWindow, ...]:
        return tuple(window for window in self.windows if window.role in roles)


class Registration(FrozenModel):
    """A draft plus the power decision that let it through, frozen together under one hash."""

    draft: HypothesisDraft
    registered_at: datetime
    #: Prior spend plus this one's search cardinality. Counted, not declared.
    cumulative_trials: int
    power: PowerAssessment
    #: Whether the registration-time power assessment ran against a counted sample or the
    #: registrant's planning figure (#35). False is not a defect -- it is the ordinary case for a
    #: hypothesis filed before anyone loaded data -- but it changes what the assessment means,
    #: and a reader of a frozen registration should not have to guess which one they are holding.
    sample_verified_at_registration: bool = False
    #: The critique that let this through, frozen with it. Immutable for the same reason the
    #: prediction is: a review that can be revised after the result is not a review.
    critique: Critique
    #: When this registration's claim on its declared windows lapses (#22). A reservation has to
    #: expire on its own: abandonment is the absence of events, so nothing can be relied upon to
    #: release it, and a reservation that only ends by explicit call never ends at all.
    reserved_until: date

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical_result_json(self).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WindowConsumption:
    """How much of one dataset range has already been searched, and by whom."""

    dataset: str
    #: UNTOUCHED, PARTIALLY_CONSUMED, or EXHAUSTED for the requested range.
    #:
    #: Derived from the TOTAL across every family, and deliberately so. #14 asks for statistical
    #: budget keyed to dataset, window and family "rather than one misleading global number",
    #: and `by_family` below is that view -- but exhaustion itself is not family-scoped, because
    #: the multiple-testing burden is not. Forty hypotheses tested on 2016-2026 raise the luck
    #: bar for the forty-first whatever family it belongs to, and letting a new family start
    #: from zero on a spent range would hand out fresh significance by renaming the question.
    status: str
    trials: int
    consumers: tuple[str, ...]
    #: Trials spent on this range per family. The keyed view #14 asks for: it says who spent the
    #: budget, which is what an operator needs to decide where to look next, without letting any
    #: family escape the total.
    by_family: Mapping[str, int] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return self.status != "EXHAUSTED"


@dataclass(frozen=True, slots=True)
class NoveltyReport:
    """Both questions at once: is the idea new, and is there data left to test it on.

    Novelty detection that compares only ideas misses the scarcer resource. A genuinely novel
    hypothesis tested against a window already searched 68 times is not new evidence.
    """

    hypothesis_id: str
    #: Highest Jaccard overlap with any prior hypothesis, over universe and features together.
    highest_overlap: float
    nearest: tuple[str, ...]
    consumption: tuple[WindowConsumption, ...]

    @property
    def novel(self) -> bool:
        return self.highest_overlap < DUPLICATE_OVERLAP

    @property
    def has_usable_data(self) -> bool:
        return all(entry.usable for entry in self.consumption)


@dataclass(frozen=True, slots=True)
class ExecutionClearance:
    """The re-check performed at execution, against state as it is *now*."""

    hypothesis_id: str
    version: int
    registration_hash: str
    registered_trials: int
    power: PowerAssessment
    #: The verified count this clearance was granted against, for the manifest.
    observed: ObservedSample


class ObservedSample(FrozenModel):
    """How many observations a dataset actually supplies, and how that was established.

    `HypothesisDraft.available_observations` is what the registrant *claims*. This is what someone
    counted, with enough provenance to audit the count later. The two are deliberately separate
    types so that a declared figure cannot be passed where a verified one is required -- the
    original defect was that `verify_for_execution` accepted the declared number as a default, so
    the gate re-checked the multiple-testing burden against durable state and then read sample size
    off the proposal.

    The registry does not compute this. It takes a session and writes rows; giving it data-provider
    dependencies would make the gate untestable offline and couple statistical policy to storage
    layout. The count arrives from the caller that owns the immutable input snapshot.
    """

    #: Raw count in the estimand's sampling unit, before any dependence adjustment.
    observations: int = Field(ge=0)
    #: The dataset the count was taken from, matching the registered window's dataset.
    dataset: Text
    #: The window actually counted, which may be shorter than the window requested.
    start_date: date
    end_date: date
    #: How it was counted, for the manifest. Free text on purpose: "XNYS sessions with SPY bars
    #: present after point-in-time availability" is more useful than any enum.
    method: Text
    counted_at: datetime

    @model_validator(mode="after")
    def validate_sample(self) -> Self:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if self.counted_at.tzinfo is None or self.counted_at.utcoffset() is None:
            raise ValueError("counted_at must be timezone-aware")
        return self


class HypothesisRegistry:
    """Freeze, retrieve, and re-verify registrations inside one caller-owned transaction."""

    def __init__(self, session: Session, *, prior_trials: int = PRIOR_TRIALS) -> None:
        self._session = session
        self._prior_trials = prior_trials

    def register(
        self,
        draft: HypothesisDraft,
        *,
        now: datetime,
        critique: Critique,
        override: PowerOverride | None = None,
        observed: ObservedSample | None = None,
    ) -> Registration:
        """Freeze a hypothesis, or refuse it. Refusal is the useful half.

        `override` lets an operator accept an underpowered hypothesis. It is audited in the
        assessment record and leaves the verdict at `OVERRIDDEN`, so the evidence stays
        labelled wherever it is displayed. It cannot rescue an `UNECONOMIC` verdict: an effect
        that cannot pay its own costs is not a measurement problem.

        `critique` is required. A hypothesis reaching protected data without an adversarial
        review is the situation #7 exists to prevent, and making the argument optional would
        make it the first thing skipped under time pressure.
        """
        if (critique.hypothesis_id, critique.hypothesis_version) != (
            draft.hypothesis_id,
            draft.version,
        ):
            raise RegistrationRefused(
                RefusalReason.CRITIQUE_MISMATCHED,
                f"the critique reviews {critique.hypothesis_id} "
                f"v{critique.hypothesis_version}, not {draft.hypothesis_id} v{draft.version}",
            )
        if critique.blocking:
            raise RegistrationRefused(
                RefusalReason.CRITIQUE_BLOCKS,
                f"{critique.critic} returned {critique.verdict.value}: "
                + "; ".join(objection.finding for objection in critique.objections),
            )

        existing = self.get(draft.hypothesis_id, draft.version)
        if existing is not None:
            raise RegistrationRefused(
                RefusalReason.ALREADY_REGISTERED,
                f"{draft.hypothesis_id} v{draft.version} is frozen at {existing.content_hash};"
                " material changes require a new version",
            )

        contamination = self._contamination(draft, now=now.date())
        if contamination:
            raise RegistrationRefused(
                RefusalReason.CONTAMINATED_WINDOW,
                "protected evaluation window is already claimed by " + "; ".join(contamination),
            )

        novelty = self.novelty_report(draft)
        if not novelty.novel and draft.materially_different is None:
            raise RegistrationRefused(
                RefusalReason.DUPLICATE,
                f"{draft.hypothesis_id} overlaps {', '.join(novelty.nearest)} by "
                f"{novelty.highest_overlap:.2f} on universe and features; state what is "
                "materially different or register it as a new version of that hypothesis",
            )

        self._verify_measured_search(draft)
        trials = self._cumulative_trials(draft.search_cardinality)

        # #35, resolved: a verified count is *accepted* here and not *required*.
        #
        # Sol argued registration should demand one; I argued the declared figure is right at a
        # gate where nobody has counted anything yet. Both positions had a real cost behind them
        # and the resolution takes the cost seriously rather than splitting the difference.
        #
        # Requiring it makes filing a question mean loading a dataset, and an expensive
        # pre-registration step is one agents route around -- which loses more validity than the
        # overstatement it prevents. Never accepting it leaves a real hole: an inflated
        # `available_observations` gets an underpowered hypothesis past this gate, and every
        # registration permanently raises the luck bar for the ones after it, so the trial budget
        # is spent on a hypothesis that will die at execution clearance anyway. That harm is
        # *not* bounded by reservation expiry, which is the part of my original argument that was
        # wrong.
        #
        # So: a caller that already holds the snapshot passes the count and gets the stronger
        # gate. A caller filing a question does not, and the registration records that its power
        # was assessed provisionally. Nobody gains anything by overstating -- the execution gate
        # still refuses without a verified count -- and nobody pays to pre-register.
        if observed is not None:
            self._verify_sample_against(draft.windows, observed, draft.hypothesis_id)
        observations = (
            draft.available_observations if observed is None else observed.observations
        )
        assessment = assess(
            hypothesis_id=draft.hypothesis_id,
            version=draft.version,
            stage=REGISTRATION,
            specification=draft.effect,
            observations_available=observations,
            cumulative_trials=trials,
            assessed_at=now,
            override=override,
        )
        if not assessment.cleared:
            raise RegistrationRefused(
                _POWER_REFUSALS[assessment.verdict],
                explain(assessment),
                assessment=assessment,
            )

        registration = Registration(
            draft=draft,
            registered_at=now,
            cumulative_trials=trials,
            power=assessment,
            sample_verified_at_registration=observed is not None,
            critique=critique,
            reserved_until=now.date() + timedelta(days=RESERVATION_DAYS),
        )
        self._insert(registration)
        self.record_assessment(assessment)
        return registration

    def get(self, hypothesis_id: str, version: int) -> Registration | None:
        row = self._row(hypothesis_id, version)
        return None if row is None else Registration.model_validate_json(str(row["document_json"]))

    def list_registrations(self, *, family_id: str | None = None) -> list[Registration]:
        statement = select(hypotheses).order_by(
            hypotheses.c.registered_at, hypotheses.c.hypothesis_id, hypotheses.c.version
        )
        if family_id is not None:
            statement = statement.where(hypotheses.c.family_id == family_id)
        return [
            Registration.model_validate_json(str(row["document_json"]))
            for row in self._session.execute(statement).mappings()
        ]

    def record_assessment(self, assessment: PowerAssessment) -> None:
        """Persist one power decision, whether it passed or refused.

        Append-only. A refusal recorded here is what stops a later agent from reading an
        untestable hypothesis as a tested-and-failed one.
        """
        override = assessment.override
        self._session.execute(
            power_assessments.insert().values(
                hypothesis_id=assessment.hypothesis_id,
                version=assessment.version,
                stage=assessment.stage,
                assessed_at=encode_utc(assessment.assessed_at),
                verdict=assessment.verdict.value,
                estimand=assessment.estimand.value,
                cumulative_trials=assessment.cumulative_trials,
                luck_threshold_z=str(assessment.luck_threshold_z),
                observations_required=assessment.observations_required,
                observations_available=assessment.observations_available,
                minimum_detectable_effect=str(assessment.minimum_detectable_effect),
                overridden_by=None if override is None else override.authorized_by,
                document_json=assessment.model_dump_json(),
            )
        )

    def list_assessments(
        self, hypothesis_id: str, version: int | None = None
    ) -> list[PowerAssessment]:
        statement = (
            select(power_assessments)
            .where(power_assessments.c.hypothesis_id == hypothesis_id)
            .order_by(power_assessments.c.assessment_id)
        )
        if version is not None:
            statement = statement.where(power_assessments.c.version == version)
        return [
            PowerAssessment.model_validate_json(str(row["document_json"]))
            for row in self._session.execute(statement).mappings()
        ]

    def verify_for_execution(
        self,
        hypothesis_id: str,
        version: int,
        *,
        now: datetime,
        observed: ObservedSample | None = None,
    ) -> ExecutionClearance:
        """Re-check a frozen registration against current state before a confirmatory run.

        The registered numbers are not trusted: every registration since raises the bar, so a
        hypothesis that was adequately powered when frozen can be underpowered by the time it
        runs. Refuses rather than downgrades. An override recorded at registration is carried
        forward, because the operator accepted the shortfall, not one particular number.

        `observed` is mandatory. This previously defaulted to the registrant's declared
        `available_observations`, which meant the gate failed *open* on the single input a
        registrant most benefits from overstating -- a real mechanism wired to the wrong source,
        reporting a clean pass. An operator override does not excuse it either: an override is a
        judgement that a known shortfall is acceptable, not permission to skip measuring.
        """
        row = self._row(hypothesis_id, version)
        if row is None:
            raise RegistrationRefused(
                RefusalReason.NOT_REGISTERED,
                f"{hypothesis_id} v{version} has no frozen registration",
            )

        stored_hash = str(row["content_hash"])
        registration = Registration.model_validate_json(str(row["document_json"]))
        if registration.content_hash != stored_hash:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"{hypothesis_id} v{version} hashes to {registration.content_hash}"
                f" but was frozen as {stored_hash}",
            )

        if observed is None:
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                f"{hypothesis_id} v{version} cannot be cleared for execution without a verified"
                " observation count; the declared figure is a planning number, not evidence",
            )
        self._verify_sample_against(
            registration.draft.windows, observed, f"{hypothesis_id} v{version}"
        )
        observations = observed.observations
        assessment = assess(
            hypothesis_id=hypothesis_id,
            version=version,
            stage=EXECUTION,
            specification=registration.draft.effect,
            observations_available=observations,
            # This registration is already counted, so its own search is not added again.
            cumulative_trials=self._cumulative_trials(0),
            assessed_at=now,
            override=registration.power.override,
        )
        if not assessment.cleared:
            raise RegistrationRefused(
                _POWER_REFUSALS[assessment.verdict],
                f"{hypothesis_id} v{version} needed "
                f"{registration.power.observations_required} observations at registration; "
                + explain(assessment),
                assessment=assessment,
            )
        self.record_assessment(assessment)
        return ExecutionClearance(
            hypothesis_id=hypothesis_id,
            version=version,
            registration_hash=stored_hash,
            registered_trials=registration.cumulative_trials,
            power=assessment,
            observed=observed,
        )

    def consume(
        self,
        hypothesis_id: str,
        version: int,
        *,
        dataset: str,
        role: DataRole,
        now: datetime,
    ) -> None:
        """Spend a reserved window, because its data is about to be handed to an experiment.

        Permanent, and deliberately recorded at the *handoff* rather than when a result comes
        back. An experiment that reads the holdout and then crashes has still seen it; marking
        consumption on success would make failing a way to look at protected data for free.

        The registry does not decide when this happens, because it cannot see the handoff. It
        exposes the transition and the caller that delivers the data performs it -- the same
        boundary `verify_for_execution` draws by taking a counted `ObservedSample` instead of
        reaching for data itself. The corollary is worth stating plainly: **a caller that hands
        over protected data without calling this leaves the window reserved, and it will lapse
        as though the experiment never ran.** Nothing here can detect that.

        Refuses a window that is not `RESERVED`, naming the state it is actually in, and -- for
        a protected window -- refuses when anything else has claimed the range in the meantime,
        which is the case a lapsed reservation opens: the holdout may have been re-let while
        this hypothesis sat idle, and two owners of one untouched window is the thing #22 must
        not create while fixing the thing it does.
        """
        row = self._window_row(hypothesis_id, version, dataset, role)
        if row is None:
            raise RegistrationRefused(
                RefusalReason.WINDOW_NOT_RESERVED,
                f"{hypothesis_id} v{version} registered no {role.value} window on {dataset!r}",
            )
        state = WindowState(str(row["state"]))
        if state is not WindowState.RESERVED:
            since = f" since {row['consumed_at']}" if state is WindowState.CONSUMED else ""
            raise RegistrationRefused(
                RefusalReason.WINDOW_NOT_RESERVED,
                f"{hypothesis_id} v{version}'s {role.value} window on {dataset!r} is"
                f" {state.value}{since}, not RESERVED",
            )
        if role is DataRole.PROTECTED_EVALUATION:
            rivals = [
                _describe_claim(other)
                for other in self._blocking(
                    dataset,
                    date.fromisoformat(str(row["start_date"])),
                    date.fromisoformat(str(row["end_date"])),
                    now=now.date(),
                )
                if not _own_unspent_claim(other, hypothesis_id)
            ]
            if rivals:
                raise RegistrationRefused(
                    RefusalReason.CONTAMINATED_WINDOW,
                    f"{hypothesis_id} v{version} may not be handed a protected window claimed"
                    " by " + "; ".join(rivals),
                )
        # Conditional on the state that was just read, in one statement, so two callers racing
        # for the same window cannot both be told they got it: exactly one UPDATE matches.
        updated = self._session.connection().execute(
            update(hypothesis_data_windows)
            .where(
                hypothesis_data_windows.c.hypothesis_id == hypothesis_id,
                hypothesis_data_windows.c.version == version,
                hypothesis_data_windows.c.dataset == dataset,
                hypothesis_data_windows.c.role == role.value,
                hypothesis_data_windows.c.state == WindowState.RESERVED.value,
            )
            .values(state=WindowState.CONSUMED.value, consumed_at=encode_utc(now))
        )
        if updated.rowcount != 1:
            raise RegistrationRefused(
                RefusalReason.WINDOW_NOT_RESERVED,
                f"{hypothesis_id} v{version}'s {role.value} window on {dataset!r} stopped being"
                " RESERVED between the check and the write; another caller took it",
            )

    def release(self, hypothesis_id: str, version: int, *, dataset: str, role: DataRole) -> None:
        """Give up a reservation before the data was read, for when someone knows it is dead.

        Available, and depended upon by nothing. That is the point: an abandoned hypothesis has
        no one left to call this, which is why `reserved_until` exists. Release only makes the
        lapse immediate in the cases where abandonment is actually known.

        A `CONSUMED` window cannot be released. Unlooking at data is not an operation.
        """
        updated = self._session.connection().execute(
            update(hypothesis_data_windows)
            .where(
                hypothesis_data_windows.c.hypothesis_id == hypothesis_id,
                hypothesis_data_windows.c.version == version,
                hypothesis_data_windows.c.dataset == dataset,
                hypothesis_data_windows.c.role == role.value,
                hypothesis_data_windows.c.state == WindowState.RESERVED.value,
            )
            .values(state=WindowState.RELEASED.value)
        )
        if updated.rowcount == 1:
            return
        row = self._window_row(hypothesis_id, version, dataset, role)
        raise RegistrationRefused(
            RefusalReason.WINDOW_NOT_RESERVED,
            f"{hypothesis_id} v{version} registered no {role.value} window on {dataset!r}"
            if row is None
            else f"{hypothesis_id} v{version}'s {role.value} window on {dataset!r} is"
            f" {row['state']}, not RESERVED",
        )

    def novelty_report(self, draft: HypothesisDraft) -> NoveltyReport:
        """Is the idea new, and is there untouched data left to test it on."""
        signature = _signature(draft.universe, draft.features)
        overlaps: list[tuple[float, str]] = []
        for row in self._session.execute(select(hypotheses)).mappings():
            if str(row["hypothesis_id"]) == draft.hypothesis_id:
                continue
            prior = Registration.model_validate_json(str(row["document_json"])).draft
            overlaps.append(
                (
                    _jaccard(signature, _signature(prior.universe, prior.features)),
                    f"{prior.hypothesis_id} v{prior.version}",
                )
            )
        highest = max(overlaps, default=(0.0, ""))[0]
        nearest = tuple(
            name for score, name in sorted(overlaps, reverse=True) if score == highest and name
        )
        return NoveltyReport(
            hypothesis_id=draft.hypothesis_id,
            highest_overlap=highest,
            nearest=nearest,
            consumption=tuple(
                self.window_consumption(window.dataset, window.start, window.end)
                for window in draft.windows
            ),
        )

    def window_consumption(self, dataset: str, start: date, end: date) -> WindowConsumption:
        """Whether a dataset range is untouched, partially consumed, or spent.

        This is the question `REFUTED.md` answers in a paragraph -- "cycles 2-10 consumed every
        out-of-sample window and SIP data begins 2016-01-04" -- made queryable, because a
        constraint that only exists in prose is one an agent may or may not read.

        Only `CONSUMED` windows count (#22). A reservation is a trial someone intends to spend,
        not one they have spent: counting it made a registered-and-abandoned hypothesis push a
        range to `EXHAUSTED`, which `discovery.admissible` then reads as "this can only ever be
        exploratory". That is the same capacity loss as the contamination block, one function
        over. No clock is needed here because a live reservation and a lapsed one are both
        simply not evidence.
        """
        rows = self._session.execute(
            select(
                hypothesis_data_windows,
                hypotheses.c.search_cardinality,
                hypotheses.c.family_id,
            )
            .join(
                hypotheses,
                (hypothesis_data_windows.c.hypothesis_id == hypotheses.c.hypothesis_id)
                & (hypothesis_data_windows.c.version == hypotheses.c.version),
            )
            .where(
                # Every dataset name observing the same series, not just the one asked about
                # (#40). Matching the literal name is a gate keyed on a string the registrant
                # chooses: `nexustrade-us-equities-daily` over 2016-2026 would report UNTOUCHED
                # while `sip-us-equities-daily` over the same span is EXHAUSTED. The burden
                # attaches to the observations, not to the vendor.
                hypothesis_data_windows.c.dataset.in_(equivalent_datasets(dataset)),
                hypothesis_data_windows.c.start_date <= end.isoformat(),
                hypothesis_data_windows.c.end_date >= start.isoformat(),
                hypothesis_data_windows.c.state == WindowState.CONSUMED.value,
            )
        ).mappings()
        consumers: list[str] = []
        by_family: dict[str, int] = {}
        trials = 0
        for row in rows:
            consumers.append(f"{row['hypothesis_id']} v{row['version']}")
            spent = int(row["search_cardinality"])
            trials += spent
            family = str(row["family_id"])
            by_family[family] = by_family.get(family, 0) + spent
        if trials >= EXHAUSTED_TRIALS:
            status = "EXHAUSTED"
        elif trials:
            status = "PARTIALLY_CONSUMED"
        else:
            status = "UNTOUCHED"
        return WindowConsumption(
            dataset=dataset,
            status=status,
            trials=trials,
            consumers=tuple(sorted(set(consumers))),
            by_family=dict(sorted(by_family.items())),
        )

    def _row(self, hypothesis_id: str, version: int) -> RowMapping | None:
        return (
            self._session.execute(
                select(hypotheses).where(
                    hypotheses.c.hypothesis_id == hypothesis_id,
                    hypotheses.c.version == version,
                )
            )
            .mappings()
            .one_or_none()
        )

    def _window_row(
        self, hypothesis_id: str, version: int, dataset: str, role: DataRole
    ) -> RowMapping | None:
        return (
            self._session.execute(
                select(hypothesis_data_windows).where(
                    hypothesis_data_windows.c.hypothesis_id == hypothesis_id,
                    hypothesis_data_windows.c.version == version,
                    hypothesis_data_windows.c.dataset == dataset,
                    hypothesis_data_windows.c.role == role.value,
                )
            )
            .mappings()
            .one_or_none()
        )

    @staticmethod
    def _verify_sample_against(
        windows: Sequence[DataWindow], observed: ObservedSample, subject: str
    ) -> None:
        """Check a counted sample against the windows a hypothesis actually declared (#19, #35).

        Shared by registration and execution clearance rather than written twice. Two copies of a
        rule this specific drift, and the direction they drift is toward the more permissive one
        -- whichever copy somebody edits while chasing a failing test.

        Matching the dataset is not enough: the count has to come from the range that was frozen.
        A hypothesis registered on 2016-2026 must not be satisfied by a count taken over
        1990-2026 of the same dataset, which is the declared-sample defect wearing a verified
        label.

        Containment rather than equality, deliberately. Counting *fewer* observations than the
        window spans is the honest case -- data missing at the edges, a feature needing warm-up, a
        symbol listing late -- and surfacing that is what verification is for. Counting more is
        the attack.
        """
        declared = {window.dataset for window in windows}
        if observed.dataset not in declared:
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                f"{subject} counted {observed.observations} observations from"
                f" {observed.dataset!r}, which is not among its registered datasets"
                f" {sorted(declared)}",
            )
        covering = [
            window
            for window in windows
            if window.dataset == observed.dataset
            and window.start <= observed.start_date
            and observed.end_date <= window.end
        ]
        if not covering:
            spans = ", ".join(
                f"{window.role.value} {window.start.isoformat()}..{window.end.isoformat()}"
                for window in windows
                if window.dataset == observed.dataset
            )
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                f"{subject} counted {observed.observations} observations over"
                f" {observed.start_date.isoformat()}..{observed.end_date.isoformat()} on"
                f" {observed.dataset!r}, which no registered window covers ({spans})",
            )

    def _verify_measured_search(self, draft: HypothesisDraft) -> None:
        """A MEASURED count must match the record it names (#23).

        Checking only that the id exists would leave the number itself declared: a draft could
        cite a real search of five thousand candidates and register a cardinality of one, which
        is the original defect wearing a reference. The row is the authority, and a mismatch is
        refused rather than silently corrected to the recorded value -- a registration whose
        stated burden differs from its evidence is a mistake someone should see.
        """
        if draft.search_origin is not SearchOrigin.MEASURED:
            return
        row = self._session.execute(
            select(search_runs.c.candidates_evaluated).where(
                search_runs.c.search_id == draft.search_run_id
            )
        ).one_or_none()
        if row is None:
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                f"{draft.hypothesis_id} cites search run {draft.search_run_id!r}, which is not "
                "recorded; a MEASURED origin naming a search nobody logged is a stronger claim "
                "on less evidence than the self-report it replaced",
            )
        recorded = int(row[0])
        if recorded != draft.search_cardinality:
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                f"{draft.hypothesis_id} declares {draft.search_cardinality} trials against "
                f"search run {draft.search_run_id!r}, which recorded {recorded}",
            )

    def _cumulative_trials(self, search_cardinality: int) -> int:
        """Everything this project has ever spent, plus what the incoming draft spends."""
        spent = self._session.execute(
            select(func.coalesce(func.sum(hypotheses.c.search_cardinality), 0))
        ).scalar_one()
        return self._prior_trials + int(spent) + search_cardinality

    def _blocking(self, dataset: str, start: date, end: date, *, now: date) -> list[RowMapping]:
        """Every window row that still holds a claim over this range, as of `now` (#22).

        The two states are asymmetric on purpose. `CONSUMED` is unconditional: contamination is
        a fact about what was seen, and no elapsed time makes an inspected holdout clean again.
        `RESERVED` blocks only while live, because a registration is a claim on data nobody has
        looked at yet, and the only registrations that release themselves are the ones that
        lapse -- an abandoned hypothesis never announces that it was abandoned. `RELEASED` never
        blocks.

        A `RESERVED` row with no expiry cannot be written by this class, but if one exists it is
        treated as blocking rather than as lapsed: the conservative reading of a missing date.
        """
        state = hypothesis_data_windows.c.state
        return list(
            self._session.execute(
                select(hypothesis_data_windows).where(
                    hypothesis_data_windows.c.dataset == dataset,
                    hypothesis_data_windows.c.start_date <= end.isoformat(),
                    hypothesis_data_windows.c.end_date >= start.isoformat(),
                    (state == WindowState.CONSUMED.value)
                    | (
                        (state == WindowState.RESERVED.value)
                        & (
                            hypothesis_data_windows.c.reserved_until.is_(None)
                            | (hypothesis_data_windows.c.reserved_until >= now.isoformat())
                        )
                    ),
                )
            )
            .mappings()
            .all()
        )

    def _contamination(self, draft: HypothesisDraft, *, now: date) -> list[str]:
        """Name every live claim on a window this draft calls protected evaluation.

        Only `PROTECTED_EVALUATION` claims are blocked. A shared `FORWARD_PAPER` window is
        genuine multiple testing rather than contamination, so it is carried by the trial
        burden instead -- blocking it would make the paper account single-use forever.

        `now` is a parameter rather than a clock read here, so the lapse is testable at an
        arbitrary date and a registration cannot be decided against a different instant than
        the one it is recorded under.
        """
        conflicts: list[str] = []
        for window in draft.windows_for(DataRole.PROTECTED_EVALUATION):
            conflicts.extend(
                f"its own {other.role.value} window on {other.dataset}"
                for other in draft.windows_for(DataRole.DISCOVERY, DataRole.VALIDATION)
                if window.overlaps(other)
            )
            conflicts.extend(
                _describe_claim(row)
                for row in self._blocking(window.dataset, window.start, window.end, now=now)
                if not _own_unspent_claim(row, draft.hypothesis_id)
            )
        return conflicts

    def _insert(self, registration: Registration) -> None:
        draft = registration.draft
        self._session.execute(
            hypotheses.insert().values(
                hypothesis_id=draft.hypothesis_id,
                version=draft.version,
                family_id=draft.family_id,
                parent_hypothesis_id=draft.parent_hypothesis_id,
                parent_version=draft.parent_version,
                registered_at=encode_utc(registration.registered_at),
                content_hash=registration.content_hash,
                search_cardinality=draft.search_cardinality,
                search_origin=draft.search_origin.value,
                search_attested_by=draft.search_attested_by,
                cumulative_trials=registration.cumulative_trials,
                luck_threshold_z=str(registration.power.luck_threshold_z),
                estimand=draft.effect.estimand.value,
                expected_effect=str(draft.effect.expected),
                power_verdict=registration.power.verdict.value,
                required_observations=registration.power.observations_required,
                available_observations=draft.available_observations,
                document_json=registration.model_dump_json(),
            )
        )
        for window in draft.windows:
            self._session.execute(
                hypothesis_data_windows.insert().values(
                    hypothesis_id=draft.hypothesis_id,
                    version=draft.version,
                    dataset=window.dataset,
                    role=window.role.value,
                    start_date=window.start.isoformat(),
                    end_date=window.end.isoformat(),
                    # Registration reserves; only handing the data to an experiment consumes it.
                    state=WindowState.RESERVED.value,
                    reserved_until=registration.reserved_until.isoformat(),
                    consumed_at=None,
                )
            )


def summarize(registration: Registration) -> dict[str, object]:
    """Flat operator-facing view: what was claimed, and what bar it has to clear."""
    draft = registration.draft
    power = registration.power
    return {
        "hypothesis_id": draft.hypothesis_id,
        "version": draft.version,
        "family_id": draft.family_id,
        "question": draft.question,
        "falsified_if": draft.falsified_if,
        "registered_at": encode_utc(registration.registered_at),
        "content_hash": registration.content_hash,
        "cumulative_trials": registration.cumulative_trials,
        "luck_threshold_z": str(power.luck_threshold_z),
        "estimand": power.estimand.value,
        "power_verdict": power.verdict.value,
        "critic": registration.critique.critic,
        "critic_verdict": registration.critique.verdict.value,
        "unassessed_by_critic": list(registration.critique.unassessed),
        "overridden_by": None if power.override is None else power.override.authorized_by,
        "expected_effect": str(power.expected_effect),
        "minimum_detectable_effect": str(power.minimum_detectable_effect),
        "required_observations": power.observations_required,
        "available_observations": power.observations_available,
        "variance_inflation": str(power.variance_inflation),
        "windows": [
            f"{w.dataset}@{w.snapshot} {w.role.value} {w.start.isoformat()}..{w.end.isoformat()}"
            for w in draft.windows
        ],
    }


def _describe_claim(row: RowMapping) -> str:
    """Name a blocking window *and the state that made it block*.

    The state belongs in the message. A refusal that says only "overlaps H-1 v1" reads the same
    whether the block came from a spent holdout or from a query with no state in it at all,
    which is exactly the failure this change exists to make impossible to reintroduce quietly.
    """
    held = (
        f"CONSUMED {row['consumed_at']}"
        if str(row["state"]) == WindowState.CONSUMED.value
        else f"{row['state']} until {row['reserved_until']}"
    )
    return (
        f"{row['hypothesis_id']} v{row['version']} ({row['role']}"
        f" {row['start_date']}..{row['end_date']}, {held})"
    )


def _own_unspent_claim(row: RowMapping, hypothesis_id: str) -> bool:
    """Whether a blocking row is this hypothesis's own reservation, which it may take over.

    A later version of a hypothesis inherits its own live reservation rather than being refused
    by it: the claim was never spent, and the alternative is that revising a hypothesis costs it
    the holdout it froze. Its own *consumption* still blocks, because what was seen was seen
    whichever version of whichever hypothesis looked at it.
    """
    return (
        str(row["hypothesis_id"]) == hypothesis_id
        and str(row["state"]) == WindowState.RESERVED.value
    )


def _signature(universe: Sequence[str], features: Sequence[str]) -> frozenset[str]:
    """What two hypotheses are compared on: the things they look at and what they measure."""
    return frozenset(
        [f"universe:{item.upper()}" for item in universe]
        + [f"feature:{item.lower()}" for item in features]
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def unresolved_questions(assessments: Sequence[PowerAssessment]) -> list[PowerAssessment]:
    """Everything this project declined to test, and why. Distinct from what it refuted."""
    return [
        assessment
        for assessment in assessments
        if assessment.verdict in (PowerVerdict.UNDERPOWERED, PowerVerdict.UNECONOMIC)
    ]


__all__ = [
    "DUPLICATE_OVERLAP",
    "EXECUTION",
    "EXHAUSTED_TRIALS",
    "PRIOR_TRIALS",
    "REGISTRATION",
    "RESERVATION_DAYS",
    "DataRole",
    "DataWindow",
    "ExecutionClearance",
    "HypothesisDraft",
    "HypothesisRegistry",
    "NoveltyReport",
    "RefusalReason",
    "Registration",
    "RegistrationRefused",
    "SearchOrigin",
    "WindowConsumption",
    "WindowState",
    "luck_threshold",
    "summarize",
    "unresolved_questions",
]
