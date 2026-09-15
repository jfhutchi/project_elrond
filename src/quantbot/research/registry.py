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
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
    model_validator,
)
from sqlalchemy import func, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.research.artifact_testing import (
    ArtifactTestDraft,
    ArtifactTestRefused,
    ArtifactTestRegistration,
)
from quantbot.research.critic import Critique
from quantbot.research.datasets import equivalent_datasets, observation_identity
from quantbot.research.hypothesis_graph import (
    GraphArtifactKind,
    HypothesisGraph,
    HypothesisLineage,
    validate_lineage,
)
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
from quantbot.research.validation_campaign import (
    ValidationCampaign,
)
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import (
    artifact_test_registrations,
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

#: Consumption that happened before this registry existed, as (dataset, start, end, trials).
#:
#: The counterpart to `PRIOR_TRIALS`, and it was missing. `PRIOR_TRIALS` correctly seeds the
#: luck bar so an empty registry still reports 68 cumulative trials -- but `window_consumption`
#: counted only rows, so the window cycles 2-10 spent reported `UNTOUCHED`. The bar was right
#: and the capacity was wrong: a new hypothesis would have been judged against the correct
#: 2.905 threshold while being told the exhausted window was fresh.
#:
#: Recorded as a constant rather than as synthetic registrations. The trials were really spent,
#: so recording them is bookkeeping; inventing hypothesis rows for experiments this schema never
#: saw would be fabricated provenance, and later agents would read them as real.
PRIOR_WINDOW_CONSUMPTION: tuple[tuple[str, date, date, int], ...] = (
    ("sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 21), 68),
)

#: How a pre-registry consumer is named in `WindowConsumption.consumers`. Deliberately not
#: shaped like a hypothesis id, so nobody can mistake it for a row they can go and read.
PRIOR_CONSUMER = "cycles 2-10 (pre-v0.2, see REFUTED.md)"

#: How long a registration's claim on its declared windows blocks other registrations (#22).
#: Long enough that a hypothesis actually being worked on keeps its holdout, short enough that an
#: abandoned one stops sterilising the data within a research cycle. A reservation must lapse
#: rather than wait to be released: nothing observes abandonment, so an explicitly-released-only
#: reservation is the old permanent-consumption behaviour wearing a state column.
RESERVATION_DAYS = 90

#: How a validation campaign's selection appears in `search_runs`. Campaign burden is counted by
#: matching this actor, so the ledger itself distinguishes a campaign selection from a
#: candidate-generation search without a second table or a new column to disagree with it.
VALIDATION_CAMPAIGN_ACTOR = "validation-campaign"
VALIDATION_CAMPAIGN_ACTOR_VERSION = "1"

#: The reserved search-id namespace for validation campaigns, matching
#: `ValidationCampaign.selection_search_id` (#215). Campaign burden is discovered by this prefix
#: rather than by `actor`, because a row's search identity is what puts it in the namespace while
#: `actor` is an ordinary column that can be made to disagree with it -- and a row that stopped
#: being counted because a column changed would take real selection freedom off the luck bar.
VALIDATION_SEARCH_NAMESPACE = "validation:"

#: Every supervised learner fit reserves this namespace before fitting. Selection burden is
#: discovered from the identity rather than the mutable actor label, for the same reason as
#: validation campaigns: renaming an actor must not erase a model attempt that already happened.
LEARNER_SEARCH_NAMESPACE = "learner:"

#: Which role a campaign's ledger row is attributed to when its scenarios span several. The most
#: protected role wins, because the burden that matters is the one spent on the data that cannot
#: be spent twice. Ordering, not a set: a campaign touching a protected window is a claim on that
#: window whatever else it also reads.
_ROLE_PROTECTION_ORDER: tuple[str, ...] = (
    "VALIDATION",
    "PROTECTED_EVALUATION",
    "FORWARD_PAPER",
)


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
    INVALID_LINEAGE = "INVALID_LINEAGE"
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

    #: Optional for legacy registrations, required as a pair for componentized ones. The graph
    #: describes behaviour; lineage describes provenance and deliberately carries no verdict.
    graph: HypothesisGraph | None = None
    lineage: HypothesisLineage | None = None

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
    #: The hostile-validation scenarios frozen with this question (#159). Selection freedom is
    #: charged once through an append-only `search_runs` row; twelve scenarios chosen by one
    #: frozen rule are one comparison, and redrawing after an outcome is what costs more.
    validation_campaign: ValidationCampaign | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Omit `validation_campaign` entirely when absent, so old hashes do not move.

        `content_hash` is `sha256(canonical_result_json(self))`, and `canonical_result_json`
        serialises through `model_dump`, which includes fields left at `None`. Adding an
        ordinary optional field would therefore have inserted `"validation_campaign":null` into
        every registration's canonical form and changed the content hash of **every already
        stored registration** -- which `_load` verifies against the stored value, so every one of
        them would have failed to load.

        Measured before writing this, not assumed: the draft used by `tests/unit/research/
        test_runner.py` hashes to `7cd29b70...` on the commit before this field existed, and
        `test_a_campaignless_draft_hashes_exactly_as_it_did_before_this_field_existed` pins that
        literal. A registration made before #159 means exactly what it meant when it was made.

        Omission rather than exclusion: `Field(exclude=True)` would drop the campaign from the
        hash even when one is present, which would let the frozen selection change without
        changing the registration's identity -- the opposite of what this field is for.
        """
        data = dict(handler(self))
        if self.validation_campaign is None:
            data.pop("validation_campaign", None)
        return data

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        if (self.parent_hypothesis_id is None) != (self.parent_version is None):
            raise ValueError("a parent reference needs both id and version")
        if (self.graph is None) != (self.lineage is None):
            raise ValueError("a component graph and its lineage must be declared together")
        if self.graph is not None and self.lineage is not None:
            if self.parent_hypothesis_id is not None:
                raise ValueError(
                    "componentized hypotheses declare all parents in lineage; the legacy "
                    "single-parent fields would create a second source of truth"
                )
            validate_lineage(self.graph, self.lineage)
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
    #: Highest overlap with any prior hypothesis: legacy universe/features or exact component
    #: content, whichever says more of the proposed behaviour has already been seen.
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
class GraphOverlap:
    """Exact behavioral overlap with prior registered component graphs."""

    highest_overlap: float
    nearest: tuple[str, ...]


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

    def __init__(
        self,
        session: Session,
        *,
        prior_trials: int = PRIOR_TRIALS,
        prior_windows: tuple[tuple[str, date, date, int], ...] = PRIOR_WINDOW_CONSUMPTION,
    ) -> None:
        self._session = session
        self._prior_trials = prior_trials
        self._prior_windows = prior_windows

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

        self._verify_lineage(draft)

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
        self._verify_campaign_frozen(draft)
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

    def _load_intact_registration(
        self, hypothesis_id: str, version: int
    ) -> tuple[Registration, str]:
        """The stored registration, proven intact against the hash frozen beside it (#159).

        `get()` parses `document_json` and stops there, which is fine for a read and is not
        enough for an execution path. The store keeps two things about a registration: the
        document, and the `content_hash` column recorded when it was frozen. Trusting the
        document alone makes an edit to it invisible -- the parsed object is self-consistent, so
        its own `content_hash` property agrees with the edited content, and a caller presenting
        that same edited hash matches perfectly. Nothing in that chain ever consults the number
        the store actually froze.

        So both are read, and the recomputed hash must equal the stored column before the
        registration is handed to anything. A disagreement is refused, never repaired: the two
        values disagreeing is precisely the evidence that one of them is not what was frozen,
        and nothing in here can tell which.

        This is the single authoritative loader. `verify_for_execution` and `resolve_for_run`
        both go through it rather than each spelling out what an intact stored registration is,
        because two definitions of that is how one execution path comes to accept what the other
        refuses.

        Returns the registration and the store's own frozen hash, which is the value callers
        should compare against and report -- not the recomputed one, even though the two are
        proven equal here.
        """
        row = self._row(hypothesis_id, version)
        if row is None:
            raise RegistrationRefused(
                RefusalReason.NOT_REGISTERED,
                f"{hypothesis_id} v{version} has no frozen registration in this store, so "
                "there is no claim for a run to be about",
            )
        stored_hash = str(row["content_hash"])
        registration = Registration.model_validate_json(str(row["document_json"]))
        if registration.content_hash != stored_hash:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"{hypothesis_id} v{version} hashes to {registration.content_hash}"
                f" but was frozen as {stored_hash}",
            )
        return registration, stored_hash

    def resolve_for_run(
        self, hypothesis_id: str, version: int, *, content_hash: str
    ) -> Registration:
        """The *persisted* registration, proven to be the one the caller says it is (#159).

        `ExperimentRunner` was checking `registration.content_hash == plan.registration_hash`,
        and both of those objects come from the caller. Two consistent forgeries agree with each
        other perfectly: a caller could build registration B with the same hypothesis id and
        version, compile plan B from it, and the pair would validate while the durable store held
        registration A. Window consumption still addressed A's id, so A's holdout was spent to
        grade B's campaign.

        So the store is asked, through `_load_intact_registration` -- which proves the row is
        internally intact before any of it is believed -- and the run's presented hash is then
        checked against the **stored column**, not against the parsed document's own recomputed
        digest. Comparing the caller to the document would leave the tampered-document case
        matching itself; comparing the caller to what the store froze is what closes it. It is
        the returned object, not the caller's, that the rest of the run reads.

        The campaign freeze is re-verified through `_verify_campaign_frozen`, which is #216's
        authority unchanged. A campaign whose ledger row has since been tampered with cannot
        become evidence just because the registration document still names it.
        """
        persisted, stored_hash = self._load_intact_registration(hypothesis_id, version)
        if stored_hash != content_hash:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"{hypothesis_id} v{version} is stored at {stored_hash} and the run "
                f"presented {content_hash}; a result frozen against one claim cannot be reported "
                "against another, and the store is the authority on which claim exists",
            )
        self._verify_campaign_frozen(persisted.draft)
        return persisted

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

    def registrations_for_component(self, component_id: str) -> list[Registration]:
        """Every frozen claim that contained this exact component version.

        This is a representation lookup, not an evidence query. A returned registration may
        have survived, failed, or never run; none of those outcomes is attributed to the
        component here. Component evidence belongs to #188 and must identify the component.
        """
        return [
            registration
            for registration in self.list_registrations()
            if registration.draft.graph is not None
            and component_id in registration.draft.graph.component_ids
        ]

    def registrations_for_graph(self, graph_id: str) -> list[Registration]:
        """Every frozen claim with behaviourally equivalent graph content."""
        return [
            registration
            for registration in self.list_registrations()
            if registration.draft.graph is not None
            and registration.draft.graph.graph_id == graph_id
        ]

    def _expected_campaign_row(self, campaign: ValidationCampaign) -> dict[str, object]:
        """The one canonical shape a campaign's ledger row must have.

        Derived in a single place and used for both the insert and the verification, so there
        cannot be two slightly different notions of what a valid frozen campaign record is. A
        second notion is how a row comes to satisfy one check while failing the property the
        other check exists to guarantee.
        """
        datasets = {scenario.dataset for scenario in campaign.scenarios}
        if len(datasets) != 1:
            # One row carries one dataset, and that is what attributes a count to the window it
            # spent. Splitting a campaign across rows would charge selection freedom once per
            # dataset, and inventing a composite label would corrupt the attribution the column
            # exists for. Refused until there is a design decision, rather than guessed at.
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                "a validation campaign is recorded as one search over one dataset; this one "
                f"spans {', '.join(sorted(datasets))}",
            )
        return {
            "search_id": campaign.selection_search_id,
            "actor": VALIDATION_CAMPAIGN_ACTOR,
            "actor_version": VALIDATION_CAMPAIGN_ACTOR_VERSION,
            "candidates_evaluated": campaign.trial_charge,
            "dataset": datasets.pop(),
            "start_date": campaign.eligible_period.start.isoformat(),
            "end_date": campaign.eligible_period.end.isoformat(),
            "data_role": max(
                (scenario.data_role for scenario in campaign.scenarios),
                key=_ROLE_PROTECTION_ORDER.index,
            ),
            "configuration_hash": campaign.campaign_id,
        }

    def _verify_campaign_row(
        self, campaign: ValidationCampaign, row: RowMapping, expected: dict[str, object]
    ) -> None:
        """Refuse unless the row is a complete, authoritative record of *this* campaign.

        Matching `configuration_hash` alone was not enough, and the gap was a fail-open one.
        `campaign_trials_spent()` counts rows by `actor`, so a row carrying the right
        `search_id` and `configuration_hash` under some other actor would have satisfied the
        freeze requirement and the registration check while contributing **no selection burden
        at all** -- a campaign accepted as preregistered and charged nothing. The two checks have
        to agree about what a campaign row is, or one of them is decorative.

        So every field that makes the row mean "this exact campaign was frozen" is compared, and
        `detail_json` is parsed back and required to reproduce the same identity: a row whose
        stored design does not rebuild the campaign it claims cannot prove that campaign was the
        thing frozen.

        Nothing is repaired. A malformed row is a fact about what happened to the ledger, and
        silently correcting it would destroy the evidence that it was ever wrong.
        """
        mismatched = [
            f"{column}={row[column]!r} (expected {value!r})"
            for column, value in sorted(expected.items())
            if row[column] != value
        ]
        if mismatched:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {campaign.selection_search_id!r} is not an authoritative record of "
                f"campaign {campaign.campaign_id}: " + "; ".join(mismatched),
            )
        try:
            stored = ValidationCampaign.model_validate_json(str(row["detail_json"]))
        except ValueError as error:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {campaign.selection_search_id!r} does not hold a readable frozen "
                f"campaign: {error}",
            ) from error
        if stored.campaign_id != campaign.campaign_id:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {campaign.selection_search_id!r} stores a design that rebuilds as "
                f"{stored.campaign_id}, not {campaign.campaign_id}",
            )
        # Content, then chronology. The row now proves it describes this exact campaign; it must
        # also prove it was written before the outcome it claims to precede, because reusing it
        # is what excuses this campaign from the consumed-window barrier.
        self._verify_campaign_predates_consumption(campaign, row, str(expected["dataset"]))

    def freeze_validation_campaign(self, campaign: ValidationCampaign, *, now: datetime) -> str:
        """Record a campaign's scenario selection before any outcome can be seen (#159).

        **What is being charged is selection freedom, not scenario count.** Twelve scenarios
        produced by one frozen rule are one comparison: nobody chose them after seeing anything,
        and the dispersion across them is the result rather than twelve chances to find a winner.
        So one campaign is one row and one trial, whatever its length.

        What costs more is redrawing. Reseeding, adding or removing a scenario, or moving a
        window changes `campaign_id`, therefore changes `selection_search_id`, therefore writes a
        *second* append-only row -- and the burden the next registration is judged against goes
        up. That is the whole mechanism: the increment comes from the ledger having two rows, not
        from anybody declaring that a redraw happened.

        **Ordering is load-bearing.** An existing valid row is checked *before* the
        consumed-window barrier, because that row is itself the proof the campaign was frozen
        first. Checking contamination first would refuse to re-acknowledge a campaign that was
        legitimately preregistered and whose window has since been consumed -- reading its own
        evidence of innocence as evidence of guilt, and making a correct campaign impossible to
        re-verify exactly when its results exist.

        The barrier still stands where it matters: a redraw is a different `campaign_id` and
        therefore a different `search_id`, so it finds no row, falls through, and meets the
        contamination check as a genuinely new freeze.

        Returns the `search_id` the campaign is charged under.
        """
        expected = self._expected_campaign_row(campaign)
        search_id = campaign.selection_search_id
        existing = (
            self._session.execute(
                select(search_runs).where(search_runs.c.search_id == search_id)
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            # Complete verification, or refusal. Never a repair, and never a partial match.
            self._verify_campaign_row(campaign, existing, expected)
            return search_id

        # Only a genuinely new freeze reaches here.
        self._refuse_consumed_campaign_windows(campaign, str(expected["dataset"]))
        self._session.execute(
            search_runs.insert().values(
                **expected,
                started_at=encode_utc(now),
                finished_at=encode_utc(now),
                # The frozen design, so the selection can be reproduced from the ledger alone
                # rather than only from a registration that may not exist yet.
                detail_json=campaign.model_dump_json(),
            )
        )
        return search_id

    def _refuse_consumed_campaign_windows(
        self, campaign: ValidationCampaign, dataset: str
    ) -> None:
        """A campaign cannot be frozen over a window whose outcome has already been seen.

        This is the freeze-before-measurement property, and it is the one that makes the rest
        mean anything: without it the lifecycle *read the protected result, then write a campaign
        that fits it, then call the campaign preregistered* is available, and every burden count
        below would be arithmetic about a fiction.

        Only `CONSUMED` blocks, deliberately. `RESERVED` is a claim on data nobody has looked at,
        so a campaign frozen while a reservation is live is still frozen before measurement --
        and refusing there would make a second version of a hypothesis unable to declare
        scenarios at all.
        """
        consumed = [
            f"{row['dataset']} {row['start_date']}..{row['end_date']}"
            for row in self._consumed_overlaps(campaign, dataset)
        ]
        if consumed:
            raise RegistrationRefused(
                RefusalReason.CONTAMINATED_WINDOW,
                "a validation campaign cannot be frozen over an already-consumed window: "
                + "; ".join(sorted(set(consumed)))
                + ". A selection made after its outcome was visible is not a preregistration, "
                "whatever it is called",
            )

    def _consumed_overlaps(
        self, campaign: ValidationCampaign, dataset: str
    ) -> list[RowMapping]:
        """Every CONSUMED window row overlapping any scenario in this campaign.

        One query behind both the new-freeze barrier and the chronology check on an existing
        row, so the two cannot end up disagreeing about which windows are relevant -- the shape
        of defect this PR has already had twice.
        """
        return [
            row
            for scenario in campaign.scenarios
            for row in self._session.execute(
                select(hypothesis_data_windows).where(
                    hypothesis_data_windows.c.dataset == dataset,
                    hypothesis_data_windows.c.state == WindowState.CONSUMED.value,
                    hypothesis_data_windows.c.start_date <= scenario.period.end.isoformat(),
                    hypothesis_data_windows.c.end_date >= scenario.period.start.isoformat(),
                )
            ).mappings()
        ]

    def _campaign_frozen_at(self, campaign: ValidationCampaign, row: RowMapping) -> datetime:
        """When this campaign's freeze completed, decoded through the storage authority.

        Parsed with `decode_utc`, never compared as text. The column is a string, and string
        order agrees with chronological order only for one canonical representation -- an
        imported row carrying `+01:00` would sort as though it were an hour later than it is.
        `decode_utc` refuses a tz-naive value and normalises to UTC, so the comparison below is
        about instants rather than about spelling.

        Actor version 1 writes `started_at == finished_at == now`, so that equality is the
        invariant for this version and a row claiming a span under it is malformed. A later
        actor version that genuinely searches over time carries a different `actor_version`,
        which `_verify_campaign_row` has already pinned, so this branch will not silently apply
        to it.
        """
        try:
            started = decode_utc(str(row["started_at"]))
            finished = decode_utc(str(row["finished_at"]))
        except ValueError as error:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {campaign.selection_search_id!r} carries an unreadable freeze "
                f"timestamp: {error}",
            ) from error
        if (
            str(row["actor_version"]) == VALIDATION_CAMPAIGN_ACTOR_VERSION
            and started != finished
        ):
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {campaign.selection_search_id!r} spans "
                f"{row['started_at']}..{row['finished_at']}; actor version "
                f"{VALIDATION_CAMPAIGN_ACTOR_VERSION} freezes a selection at one instant",
            )
        return finished

    def _verify_campaign_predates_consumption(
        self, campaign: ValidationCampaign, row: RowMapping, dataset: str
    ) -> None:
        """The row must show the freeze happened no later than the outcome it precedes.

        The existing-row fast path is what lets an exact campaign stay reusable after its window
        is consumed, so that path has to carry the proof itself. Content identity alone does not:
        a row can name the right campaign and still have been written afterwards. Row existence
        is not chronology -- inferring order from it would make the fast path a way around the
        barrier rather than an exception justified by evidence.

        A `CONSUMED` window with no readable `consumed_at` cannot establish the ordering either
        way, so it refuses. Missing chronology is not safe chronology, and treating an absent
        timestamp as permission is the same fail-open shape as treating an absent regime
        diagnostic as agreement.
        """
        overlaps = self._consumed_overlaps(campaign, dataset)
        if not overlaps:
            return
        frozen_at = self._campaign_frozen_at(campaign, row)
        for window in overlaps:
            label = f"{window['dataset']} {window['start_date']}..{window['end_date']}"
            raw = window["consumed_at"]
            if raw is None:
                raise RegistrationRefused(
                    RefusalReason.CONTAMINATED_WINDOW,
                    f"window {label} is CONSUMED but records no consumption time, so search "
                    f"run {campaign.selection_search_id!r} cannot show it was frozen first",
                )
            try:
                consumed_at = decode_utc(str(raw))
            except ValueError as error:
                raise RegistrationRefused(
                    RefusalReason.CONTAMINATED_WINDOW,
                    f"window {label} records an unreadable consumption time ({raw!r}), so "
                    f"search run {campaign.selection_search_id!r} cannot show it was frozen "
                    f"first: {error}",
                ) from error
            if frozen_at > consumed_at:
                raise RegistrationRefused(
                    RefusalReason.CONTAMINATED_WINDOW,
                    f"search run {campaign.selection_search_id!r} was frozen at "
                    f"{encode_utc(frozen_at)}, after window {label} was consumed at "
                    f"{encode_utc(consumed_at)}; a selection recorded after its outcome was "
                    "visible is not a preregistration, whatever the ledger calls it",
                )

    def _campaign_from_row(self, row: RowMapping) -> ValidationCampaign:
        """Rebuild the campaign a reserved-namespace row claims to record.

        The row's `search_id` is the claim; `detail_json` is the design behind it. They have to
        agree, or the row is sitting in the namespace under an identity it cannot reconstruct.
        """
        search_id = str(row["search_id"])
        try:
            campaign = ValidationCampaign.model_validate_json(str(row["detail_json"]))
        except ValueError as error:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {search_id!r} occupies the reserved "
                f"{VALIDATION_SEARCH_NAMESPACE!r} namespace but holds no readable campaign "
                f"design, so the selection freedom it recorded cannot be counted: {error}",
            ) from error
        if campaign.selection_search_id != search_id:
            raise RegistrationRefused(
                RefusalReason.TAMPERED,
                f"search run {search_id!r} stores a design whose own identity is "
                f"{campaign.selection_search_id!r}",
            )
        return campaign

    def campaign_trials_spent(self) -> int:
        """Selection freedom already charged to validation campaigns, read from the ledger.

        **Discovered by reserved namespace, not by `actor`.** Every campaign search id is
        `validation:<campaign_id>` (#215), and that prefix is the one part of the row a campaign
        cannot occupy by accident. Summing `WHERE actor = VALIDATION_CAMPAIGN_ACTOR` looked
        equivalent and was not: Finding 1 established that `actor` can disagree with what the
        row's own search identity says it is, and under the actor query such a row contributed
        **zero**. Selection freedom that genuinely happened would then vanish from the global
        luck bar the moment its metadata was malformed -- and an unrelated hypothesis
        registering afterwards would be judged against a bar that had quietly dropped it.

        Failing closed rather than charging a guess. A malformed row in this namespace means the
        burden and its provenance cannot both be trusted, and the two ways of guessing are worse
        than refusing: charging the row's own number trusts a value that has already been shown
        wrong, and charging the reconstructed campaign's trusts a design the ledger contradicts.

        Verification is `_verify_campaign_row`, the same authority the freeze and the
        registration paths use. A fourth notion of a valid campaign row is how the three that
        exist come to disagree.
        """
        total = 0
        for row in (
            self._session.execute(
                select(search_runs).where(
                    search_runs.c.search_id.like(f"{VALIDATION_SEARCH_NAMESPACE}%")
                )
            )
            .mappings()
            .all()
        ):
            campaign = self._campaign_from_row(row)
            self._verify_campaign_row(campaign, row, self._expected_campaign_row(campaign))
            total += int(row["candidates_evaluated"])
        return total

    def learner_trials_spent(self) -> int:
        """Every supervised learner candidate attempted, including discarded fits.

        The learner writes its row before fitting, so a failed fit and a model rejected during
        comparison both consumed selection freedom. Rows are found by the reserved ``learner:``
        search-id namespace rather than by actor: actor names are descriptive metadata and may be
        renamed without making past work disappear from the project's luck bar.

        Malformed reserved rows fail closed. Counting a guessed value would trust a ledger entry
        whose attribution or chronology is already incoherent; skipping it would silently lower
        the burden.
        """
        total = 0
        rows = (
            self._session.execute(
                select(search_runs).where(
                    search_runs.c.search_id.like(f"{LEARNER_SEARCH_NAMESPACE}%")
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            search_id = str(row["search_id"])
            errors: list[str] = []
            identity = search_id.removeprefix(LEARNER_SEARCH_NAMESPACE)
            if not identity or identity != identity.strip():
                errors.append("search_id has no canonical learner identity")

            candidates = row["candidates_evaluated"]
            if not isinstance(candidates, int) or isinstance(candidates, bool) or candidates < 1:
                errors.append("candidates_evaluated must be a positive integer")

            dataset = str(row["dataset"])
            if not dataset or dataset != dataset.strip():
                errors.append("dataset must be a non-empty canonical source identity")

            raw_start, raw_end = str(row["start_date"]), str(row["end_date"])
            try:
                start, end = date.fromisoformat(raw_start), date.fromisoformat(raw_end)
                if raw_start != start.isoformat() or raw_end != end.isoformat() or end < start:
                    errors.append("search dates must be canonical and ordered")
            except ValueError:
                errors.append("search dates must be readable ISO dates")

            if str(row["data_role"]) not in {
                DataRole.DISCOVERY.value,
                DataRole.VALIDATION.value,
            }:
                errors.append("data_role must be DISCOVERY or VALIDATION")

            try:
                started = decode_utc(str(row["started_at"]))
                finished = decode_utc(str(row["finished_at"]))
                if finished < started:
                    errors.append("search timestamps must be ordered")
            except ValueError:
                errors.append("search timestamps must be readable and timezone-aware")

            configuration_hash = str(row["configuration_hash"])
            if len(configuration_hash) != 64 or any(
                character not in "0123456789abcdef" for character in configuration_hash
            ):
                errors.append("configuration_hash must be a lowercase SHA-256 content hash")

            if errors:
                raise RegistrationRefused(
                    RefusalReason.TAMPERED,
                    f"learner search run {search_id!r} is malformed: " + "; ".join(errors),
                )
            total += int(candidates)
        return total

    def _verify_campaign_frozen(self, draft: HypothesisDraft) -> None:
        """A registration carrying a campaign must name one the ledger already recorded.

        Fails closed. Without the row the campaign is a design nobody committed to before the
        outcome, which is the entire thing preregistration is for -- and its burden would not be
        charged, so an uncharged selection would be judged against a bar that never saw it.
        """
        campaign = draft.validation_campaign
        if campaign is None:
            return
        expected = self._expected_campaign_row(campaign)
        row = (
            self._session.execute(
                select(search_runs).where(
                    search_runs.c.search_id == campaign.selection_search_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RegistrationRefused(
                RefusalReason.UNVERIFIED_SAMPLE,
                f"{draft.hypothesis_id} carries validation campaign {campaign.campaign_id}, "
                "which has no frozen search record; freeze the campaign before registering "
                "against it, or the selection is preregistered only in the telling",
            )
        # The same authority the freeze uses. Two notions of a valid campaign record is how a
        # row satisfies one check while failing the property the other check guarantees.
        self._verify_campaign_row(campaign, row, expected)

    def register_artifact_test(
        self,
        registration: Registration,
        draft: ArtifactTestDraft,
        *,
        now: datetime,
        override: PowerOverride | None = None,
        observed: ObservedSample | None = None,
    ) -> ArtifactTestRegistration:
        """Freeze one identifying artifact claim before protected data is consumed.

        Each test is an additional comparison and is charged as one cumulative trial.  The
        target is checked against the exact registered graph and the arm structure is checked
        mechanically, so a free-form ablation label cannot later be presented as identification.
        """
        current = self.get(registration.draft.hypothesis_id, registration.draft.version)
        if current is None or current.content_hash != registration.content_hash:
            raise ArtifactTestRefused("artifact tests require the exact persisted registration")
        graph = registration.draft.graph
        if graph is None:
            raise ArtifactTestRefused("artifact tests require a componentized hypothesis graph")
        if draft.artifact_content_id not in graph.artifact_ids(draft.artifact_kind):
            raise ArtifactTestRefused(
                f"graph {graph.graph_id} has no {draft.artifact_kind.value.lower()} "
                f"{draft.artifact_content_id}"
            )
        graph_components = set(graph.component_ids)
        unknown = sorted(
            {
                component_id
                for arm in draft.arms
                for component_id in arm.included_component_ids
                if component_id not in graph_components
            }
        )
        if unknown:
            raise ArtifactTestRefused(
                "identification arms name components outside the registered graph: "
                + ", ".join(unknown)
            )
        self._verify_identifying_arms(graph, draft)

        for window in registration.draft.windows_for(DataRole.PROTECTED_EVALUATION):
            row = self._window_row(
                registration.draft.hypothesis_id,
                registration.draft.version,
                window.dataset,
                window.role,
            )
            if row is None or WindowState(str(row["state"])) is not WindowState.RESERVED:
                raise ArtifactTestRefused(
                    "artifact tests must be frozen before protected data is consumed"
                )

        if observed is not None:
            self._verify_sample_against(
                registration.draft.windows,
                observed,
                "artifact test for "
                f"{registration.draft.hypothesis_id} v{registration.draft.version}",
            )
        observations = (
            registration.draft.available_observations
            if observed is None
            else observed.observations
        )
        trials = self._cumulative_trials(1)
        assessment = assess(
            hypothesis_id=registration.draft.hypothesis_id,
            version=registration.draft.version,
            stage="ARTIFACT_REGISTRATION",
            specification=draft.effect,
            observations_available=observations,
            cumulative_trials=trials,
            assessed_at=now,
            override=override,
        )
        if not assessment.cleared:
            raise ArtifactTestRefused(explain(assessment), assessment=assessment)
        frozen = ArtifactTestRegistration(
            hypothesis_id=registration.draft.hypothesis_id,
            hypothesis_version=registration.draft.version,
            registration_hash=registration.content_hash,
            graph_hash=graph.graph_id,
            draft=draft,
            registered_at=now,
            cumulative_trials=trials,
            power=assessment,
        )
        if self.get_artifact_test(frozen.test_id) is not None:
            raise ArtifactTestRefused(f"artifact test {frozen.test_id} is already registered")
        self._session.execute(
            artifact_test_registrations.insert().values(
                test_id=frozen.test_id,
                content_hash=frozen.content_hash,
                hypothesis_id=frozen.hypothesis_id,
                hypothesis_version=frozen.hypothesis_version,
                registration_hash=frozen.registration_hash,
                graph_hash=frozen.graph_hash,
                artifact_kind=frozen.draft.artifact_kind.value,
                artifact_content_id=frozen.draft.artifact_content_id,
                trial_charge=1,
                power_verdict=frozen.power.verdict.value,
                registered_at=encode_utc(now),
                document_json=frozen.model_dump_json(),
            )
        )
        return frozen

    @staticmethod
    def _verify_identifying_arms(graph: HypothesisGraph, draft: ArtifactTestDraft) -> None:
        arm_sets = [set(arm.included_component_ids) for arm in draft.arms]
        if draft.artifact_kind is GraphArtifactKind.COMPONENT:
            target = draft.artifact_content_id
            with_target = {frozenset(arm - {target}) for arm in arm_sets if target in arm}
            without_target = {frozenset(arm) for arm in arm_sets if target not in arm}
            if not with_target & without_target:
                raise ArtifactTestRefused(
                    "a component contrast needs a matched component background both with and "
                    "without the target"
                )
            return
        relationship = next(
            relation
            for relation in graph.relationships
            if relation.relationship_id == draft.artifact_content_id
        )
        source = relationship.source_component_id
        target = relationship.target_component_id
        required = {(False, False), (True, False), (False, True), (True, True)}
        cells_by_background: dict[frozenset[str], set[tuple[bool, bool]]] = {}
        for arm in arm_sets:
            background = frozenset(arm - {source, target})
            cells_by_background.setdefault(background, set()).add(
                (source in arm, target in arm)
            )
        if not any(required <= cells for cells in cells_by_background.values()):
            raise ArtifactTestRefused(
                "an interaction contrast needs all four source/target factorial cells under "
                "one matched component background"
            )

    def get_artifact_test(self, test_id: str) -> ArtifactTestRegistration | None:
        row = (
            self._session.execute(
                select(artifact_test_registrations).where(
                    artifact_test_registrations.c.test_id == test_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        frozen = ArtifactTestRegistration.model_validate_json(str(row["document_json"]))
        if frozen.content_hash != str(row["content_hash"]):
            raise ArtifactTestRefused(f"artifact test {test_id} failed its content hash")
        return frozen

    def artifact_tests_for(
        self, hypothesis_id: str, version: int
    ) -> list[ArtifactTestRegistration]:
        rows = self._session.execute(
            select(artifact_test_registrations)
            .where(
                artifact_test_registrations.c.hypothesis_id == hypothesis_id,
                artifact_test_registrations.c.hypothesis_version == version,
            )
            .order_by(artifact_test_registrations.c.registered_at)
        ).mappings()
        return [
            ArtifactTestRegistration.model_validate_json(str(row["document_json"]))
            for row in rows
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
        registration, stored_hash = self._load_intact_registration(hypothesis_id, version)

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

    def commit_consumption(self) -> None:
        """Make a protected-window consumption durable before the data is handed over (#159).

        **This is not a general commit affordance and must not become one.** It exists for one
        caller and one moment: `ExperimentRunner._spend` has recorded `RESERVED -> CONSUMED` and
        is about to issue `ProtectedAccess`, after which the data is read and cannot be unread.
        Everything else in this class deliberately leaves commit timing to the caller who owns
        the transaction.

        It commits the caller's whole unit of work, not only the window rows, and that is forced
        rather than chosen. `hypothesis_data_windows` is foreign-keyed to `hypotheses`, so a
        consumption cannot be durable while the registration that owns it is not; and a second
        connection cannot see this transaction's uncommitted rows at all, so performing the
        consumption on a connection of its own would not find the registration to consume
        against. Measured, not assumed: a separate connection reads zero rows of an open
        transaction's work.

        The honest statement of the resulting contract is that **the protected handoff is a
        commit point.** A caller that does not want its earlier work made durable must not hand
        a holdout to a measurement in the same transaction as that work.
        """
        self._session.commit()

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
            signature_overlap = _jaccard(
                signature, _signature(prior.universe, prior.features)
            )
            component_overlap = 0.0
            if draft.graph is not None and prior.graph is not None:
                if draft.graph.graph_id == prior.graph.graph_id:
                    component_overlap = 1.0
                else:
                    component_overlap = _jaccard(
                        _graph_artifacts(draft.graph),
                        _graph_artifacts(prior.graph),
                    )
            overlaps.append(
                (
                    max(signature_overlap, component_overlap),
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

    def graph_overlap(
        self, graph: HypothesisGraph, *, exclude_hypothesis_id: str | None = None
    ) -> GraphOverlap:
        """Compare exact component and relationship behavior against frozen registrations."""
        overlaps: list[tuple[float, str]] = []
        artifacts = _graph_artifacts(graph)
        for registration in self.list_registrations():
            prior = registration.draft
            if prior.graph is None or prior.hypothesis_id == exclude_hypothesis_id:
                continue
            score = (
                1.0
                if graph.graph_id == prior.graph.graph_id
                else _jaccard(artifacts, _graph_artifacts(prior.graph))
            )
            overlaps.append((score, f"{prior.hypothesis_id} v{prior.version}"))
        highest = max(overlaps, default=(0.0, ""))[0]
        return GraphOverlap(
            highest_overlap=highest,
            nearest=tuple(
                name for score, name in sorted(overlaps, reverse=True) if score == highest and name
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
        artifact_charges = {
            (str(row["hypothesis_id"]), int(row["hypothesis_version"])): int(row["charge"])
            for row in self._session.execute(
                select(
                    artifact_test_registrations.c.hypothesis_id,
                    artifact_test_registrations.c.hypothesis_version,
                    func.sum(artifact_test_registrations.c.trial_charge).label("charge"),
                ).group_by(
                    artifact_test_registrations.c.hypothesis_id,
                    artifact_test_registrations.c.hypothesis_version,
                )
            ).mappings()
        }
        # Consumption that predates this registry, for any prior range overlapping the query.
        # Without this the equity window cycles 2-10 spent reported UNTOUCHED (#2).
        for name, prior_start, prior_end, prior_trials in self._prior_windows:
            if name in equivalent_datasets(dataset) and prior_start <= end and prior_end >= start:
                trials += prior_trials
                # Named in `consumers` so a reader can see who spent the window, but kept out
                # of `by_family`: that mapping attributes spend to a v0.2 research family, and
                # pre-registry work belongs to none of them. Inventing a family for it would
                # make a made-up attribution look like a measured one.
                consumers.append(PRIOR_CONSUMER)
        for row in rows:
            consumers.append(f"{row['hypothesis_id']} v{row['version']}")
            spent = int(row["search_cardinality"]) + artifact_charges.get(
                (str(row["hypothesis_id"]), int(row["version"])), 0
            )
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

    def _verify_lineage(self, draft: HypothesisDraft) -> None:
        """Bind every lineage claim to artifacts frozen in exact stored parent graphs.

        A model or external worker may propose lineage, but the registry is the authority that
        decides whether those parents and artifacts really exist. This check also prevents a
        child from naming the latest version of a parent while silently taking a component from
        an older one.
        """
        if draft.graph is None or draft.lineage is None:
            return
        parent_graphs: dict[tuple[str, int], HypothesisGraph] = {}
        for parent in draft.lineage.parents:
            row = self._row(parent.hypothesis_id, parent.version)
            if row is None:
                raise RegistrationRefused(
                    RefusalReason.INVALID_LINEAGE,
                    f"parent {parent.hypothesis_id} v{parent.version} is not registered",
                )
            registration = Registration.model_validate_json(str(row["document_json"]))
            if registration.content_hash != str(row["content_hash"]):
                raise RegistrationRefused(
                    RefusalReason.TAMPERED,
                    f"parent {parent.hypothesis_id} v{parent.version} hashes to "
                    f"{registration.content_hash} but was frozen as {row['content_hash']}",
                )
            graph = registration.draft.graph
            if graph is None:
                raise RegistrationRefused(
                    RefusalReason.INVALID_LINEAGE,
                    f"parent {parent.hypothesis_id} v{parent.version} has no component graph",
                )
            if graph.graph_id != parent.graph_id:
                raise RegistrationRefused(
                    RefusalReason.INVALID_LINEAGE,
                    f"parent {parent.hypothesis_id} v{parent.version} registered graph "
                    f"{graph.graph_id}, not {parent.graph_id}",
                )
            parent_graphs[(parent.hypothesis_id, parent.version)] = graph

        for change in draft.lineage.changes:
            if change.parent is None or change.parent_content_id is None:
                continue
            graph = parent_graphs[(change.parent.hypothesis_id, change.parent.version)]
            if change.parent_content_id not in graph.artifact_ids(change.artifact_kind):
                raise RegistrationRefused(
                    RefusalReason.INVALID_LINEAGE,
                    f"{change.parent.hypothesis_id} v{change.parent.version} graph "
                    f"{change.parent.graph_id} has no {change.artifact_kind.value.lower()} "
                    f"{change.parent_content_id}",
                )

    def cumulative_trials(self) -> int:
        """The multiple-testing burden as it stands right now.

        Public because the luck bar is not only a registration-time concern. Forward
        qualification deflates against the burden *at the moment it judges*, the same rule
        `verify_for_execution` applies: every registration since raises the bar, and a gate
        quoting the burden frozen months ago quotes a bar the project has already spent past.
        """
        return self._cumulative_trials(0)

    def _cumulative_trials(self, search_cardinality: int) -> int:
        """Everything this project has ever spent, plus what the incoming draft spends.

        A later registration may cite one learner row again through ``search_cardinality``. That
        deliberately overcounts the selected candidate rather than discounting or deduplicating a
        real attempt from the permanent learner ledger; no caller may lower historical burden.
        """
        hypothesis_spend = self._session.execute(
            select(func.coalesce(func.sum(hypotheses.c.search_cardinality), 0))
        ).scalar_one()
        artifact_spend = self._session.execute(
            select(func.coalesce(func.sum(artifact_test_registrations.c.trial_charge), 0))
        ).scalar_one()
        # Campaign selection freedom, counted from the append-only ledger rather than from a
        # column anyone can edit or a number a caller supplies (#159). A redraw is visible here
        # as a second row, so the bar rises for everything registered afterwards.
        return (
            self._prior_trials
            + int(hypothesis_spend)
            + int(artifact_spend)
            + self.campaign_trials_spent()
            + self.learner_trials_spent()
            + search_cardinality
        )

    def blocking_protected_windows(
        self, dataset: str, start: date, end: date, *, now: date
    ) -> tuple[RowMapping, ...]:
        """Live or consumed protected claims over an observation range, across known aliases.

        This is the public authority for consumers that must refuse protected reads without
        duplicating the registry's lifecycle predicate. Expired reservations and released rows
        are absent; consumed rows and reservations without an expiry remain blocking.
        """
        aliases = {*equivalent_datasets(dataset), observation_identity(dataset)}
        rows = (
            row
            for alias in aliases
            for row in self._blocking(alias, start, end, now=now)
            if row["role"] == DataRole.PROTECTED_EVALUATION.value
        )
        return tuple(
            sorted(
                rows,
                key=lambda row: (
                    str(row["hypothesis_id"]),
                    int(row["version"]),
                    str(row["dataset"]),
                ),
            )
        )

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
        "graph_id": None if draft.graph is None else draft.graph.graph_id,
        "lineage_id": None if draft.lineage is None else draft.lineage.lineage_id,
        "component_ids": [] if draft.graph is None else list(draft.graph.component_ids),
        "relationship_ids": [] if draft.graph is None else list(draft.graph.relationship_ids),
        "lineage_parents": (
            []
            if draft.lineage is None
            else [
                f"{parent.hypothesis_id} v{parent.version}@{parent.graph_id}"
                for parent in draft.lineage.parents
            ]
        ),
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


def _graph_artifacts(graph: HypothesisGraph) -> frozenset[str]:
    """Namespaced ids so an unlikely cross-kind digest collision cannot create overlap."""
    return frozenset(
        [f"component:{item}" for item in graph.component_ids]
        + [f"relationship:{item}" for item in graph.relationship_ids]
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
    "GraphOverlap",
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
