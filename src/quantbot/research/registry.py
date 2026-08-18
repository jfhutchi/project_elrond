"""Frozen, machine-readable hypothesis registrations (#5) behind the power gate (#19).

A registration is a prediction written down *before* the measurement, hashed, and stored so it
cannot be restated afterwards. Cycle 12 is the worked example: "the growth-optimal leverage is
below 2x" measured 3.25x. Because the prediction was frozen it is logged as a miss. Unfrozen it
would have become "an interior optimum exists" -- true, unfalsifiable, and worthless.

Four things are refused here rather than left to judgement, because software can prove them:

1. **A false holdout claim.** A `PROTECTED_EVALUATION` window may not overlap any date range a
   previous registration already recorded on the same dataset, nor this registration's own
   discovery or validation ranges. This is the check that makes the project's structural limit
   visible instead of leaving it in a paragraph: SIP data begins 2016-01-04 and cycles 2-10
   consumed all of it, so any confirmatory claim on that window is in-sample whatever the
   schema says.
2. **An unresolvable question.** `UNDERPOWERED`, computed by `power.py` from the declared
   estimand and its dependence structure. Not the same verdict as `REFUTED`.
3. **A question not worth resolving.** `UNECONOMIC`: the smallest effect worth acting on cannot
   pay its own annual trading cost.
3b. **A question already asked.** `DUPLICATE`: a candidate overlapping a prior hypothesis on
   universe and features, unless the registrant writes down what is materially different.
   Momentum has now returned null on three structurally independent universes because each new
   one had a plausible excuse; without a written record, universe four gets proposed next week.
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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

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
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import hypotheses, hypothesis_data_windows, power_assessments

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


class DataRole(StrEnum):
    """What a dataset is allowed to be used for, per the v0.2 protected-data model."""

    DISCOVERY = "DISCOVERY"
    VALIDATION = "VALIDATION"
    PROTECTED_EVALUATION = "PROTECTED_EVALUATION"
    FORWARD_PAPER = "FORWARD_PAPER"


class EpistemicStatus(StrEnum):
    LITERATURE_SUPPORTED = "LITERATURE_SUPPORTED"
    LITERATURE_REFUTED = "LITERATURE_REFUTED"
    MEASURED = "MEASURED"
    UNTESTED = "UNTESTED"


class RefusalReason(StrEnum):
    CONTAMINATED_WINDOW = "CONTAMINATED_WINDOW"
    DUPLICATE = "DUPLICATE"
    UNDERPOWERED = "UNDERPOWERED"
    UNECONOMIC = "UNECONOMIC"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    NOT_REGISTERED = "NOT_REGISTERED"
    TAMPERED = "TAMPERED"


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

    confounders: tuple[Text, ...] = Field(min_length=1)
    #: Required only when this candidate overlaps a prior hypothesis. Writing down what is
    #: different is the whole gate: it is cheap when the difference is real and impossible to
    #: fill in honestly when it is not.
    materially_different: str | None = None
    epistemic_status: EpistemicStatus = EpistemicStatus.UNTESTED
    proposed_by: Text
    prompt_version: str | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        if (self.parent_hypothesis_id is None) != (self.parent_version is None):
            raise ValueError("a parent reference needs both id and version")
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

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical_result_json(self).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class WindowConsumption:
    """How much of one dataset range has already been searched, and by whom."""

    dataset: str
    #: UNTOUCHED, PARTIALLY_CONSUMED, or EXHAUSTED for the requested range.
    status: str
    trials: int
    consumers: tuple[str, ...]

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
        override: PowerOverride | None = None,
    ) -> Registration:
        """Freeze a hypothesis, or refuse it. Refusal is the useful half.

        `override` lets an operator accept an underpowered hypothesis. It is audited in the
        assessment record and leaves the verdict at `OVERRIDDEN`, so the evidence stays
        labelled wherever it is displayed. It cannot rescue an `UNECONOMIC` verdict: an effect
        that cannot pay its own costs is not a measurement problem.
        """
        existing = self.get(draft.hypothesis_id, draft.version)
        if existing is not None:
            raise RegistrationRefused(
                RefusalReason.ALREADY_REGISTERED,
                f"{draft.hypothesis_id} v{draft.version} is frozen at {existing.content_hash};"
                " material changes require a new version",
            )

        contamination = self._contamination(draft)
        if contamination:
            raise RegistrationRefused(
                RefusalReason.CONTAMINATED_WINDOW,
                "protected evaluation window was already consumed by " + "; ".join(contamination),
            )

        novelty = self.novelty_report(draft)
        if not novelty.novel and draft.materially_different is None:
            raise RegistrationRefused(
                RefusalReason.DUPLICATE,
                f"{draft.hypothesis_id} overlaps {', '.join(novelty.nearest)} by "
                f"{novelty.highest_overlap:.2f} on universe and features; state what is "
                "materially different or register it as a new version of that hypothesis",
            )

        trials = self._cumulative_trials(draft.search_cardinality)
        assessment = assess(
            hypothesis_id=draft.hypothesis_id,
            version=draft.version,
            stage=REGISTRATION,
            specification=draft.effect,
            observations_available=draft.available_observations,
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
        available_observations: int | None = None,
    ) -> ExecutionClearance:
        """Re-check a frozen registration against current state before a confirmatory run.

        The registered numbers are not trusted: every registration since raises the bar, so a
        hypothesis that was adequately powered when frozen can be underpowered by the time it
        runs. Refuses rather than downgrades. An override recorded at registration is carried
        forward, because the operator accepted the shortfall, not one particular number.
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

        observations = (
            registration.draft.available_observations
            if available_observations is None
            else available_observations
        )
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
        """
        rows = self._session.execute(
            select(hypothesis_data_windows, hypotheses.c.search_cardinality)
            .join(
                hypotheses,
                (hypothesis_data_windows.c.hypothesis_id == hypotheses.c.hypothesis_id)
                & (hypothesis_data_windows.c.version == hypotheses.c.version),
            )
            .where(
                hypothesis_data_windows.c.dataset == dataset,
                hypothesis_data_windows.c.start_date <= end.isoformat(),
                hypothesis_data_windows.c.end_date >= start.isoformat(),
            )
        ).mappings()
        consumers: list[str] = []
        trials = 0
        for row in rows:
            consumers.append(f"{row['hypothesis_id']} v{row['version']}")
            trials += int(row["search_cardinality"])
        if trials >= EXHAUSTED_TRIALS:
            status = "EXHAUSTED"
        elif trials:
            status = "PARTIALLY_CONSUMED"
        else:
            status = "UNTOUCHED"
        return WindowConsumption(
            dataset=dataset, status=status, trials=trials, consumers=tuple(sorted(set(consumers)))
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

    def _cumulative_trials(self, search_cardinality: int) -> int:
        """Everything this project has ever spent, plus what the incoming draft spends."""
        spent = self._session.execute(
            select(func.coalesce(func.sum(hypotheses.c.search_cardinality), 0))
        ).scalar_one()
        return self._prior_trials + int(spent) + search_cardinality

    def _contamination(self, draft: HypothesisDraft) -> list[str]:
        """Name every prior consumer of a window this draft calls protected evaluation.

        Only `PROTECTED_EVALUATION` claims are blocked. A shared `FORWARD_PAPER` window is
        genuine multiple testing rather than contamination, so it is carried by the trial
        burden instead -- blocking it would make the paper account single-use forever.
        """
        conflicts: list[str] = []
        for window in draft.windows_for(DataRole.PROTECTED_EVALUATION):
            conflicts.extend(
                f"its own {other.role.value} window on {other.dataset}"
                for other in draft.windows_for(DataRole.DISCOVERY, DataRole.VALIDATION)
                if window.overlaps(other)
            )
            rows = self._session.execute(
                select(hypothesis_data_windows).where(
                    hypothesis_data_windows.c.dataset == window.dataset,
                    hypothesis_data_windows.c.start_date <= window.end.isoformat(),
                    hypothesis_data_windows.c.end_date >= window.start.isoformat(),
                )
            ).mappings()
            conflicts.extend(
                f"{row['hypothesis_id']} v{row['version']} ({row['role']}"
                f" {row['start_date']}..{row['end_date']})"
                for row in rows
                if (row["hypothesis_id"], row["version"]) != (draft.hypothesis_id, draft.version)
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
    "DataRole",
    "DataWindow",
    "EpistemicStatus",
    "ExecutionClearance",
    "HypothesisDraft",
    "HypothesisRegistry",
    "NoveltyReport",
    "RefusalReason",
    "Registration",
    "RegistrationRefused",
    "WindowConsumption",
    "luck_threshold",
    "summarize",
    "unresolved_questions",
]
