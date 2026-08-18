"""Frozen, machine-readable hypothesis registrations (#5).

A registration is a prediction written down *before* the measurement, hashed, and stored so it
cannot be restated afterwards. Cycle 12 is the worked example: "the growth-optimal leverage is
below 2x" measured 3.25x. Because the prediction was frozen it is logged as a miss. Unfrozen it
would have become "an interior optimum exists" -- true, unfalsifiable, and worthless.

Three things are refused here rather than left to judgement, because software can prove them:

1. **A false holdout claim.** A `PROTECTED_EVALUATION` window may not overlap any date range a
   previous registration already recorded on the same dataset, nor this registration's own
   discovery or validation ranges. This is the check that makes the project's structural limit
   visible instead of leaving it in a paragraph: SIP data begins 2016-01-04 and cycles 2-10
   consumed all of it, so any confirmatory claim on that window is in-sample whatever the
   schema says.
2. **An unresolvable question.** Detecting an annualised Sharpe of `SR` at threshold `z` needs
   `(z / SR)^2` years. At the current bar that is ~93 years for SR 0.30 and ~8.4 for SR 1.00.
   A registration whose available sample is shorter than its own claimed effect requires is
   refused as `UNDERPOWERED`, which is not the same verdict as `REFUTED`.
3. **A declared rather than counted multiple-testing burden.** The trial count is computed from
   durable state -- every prior registration plus this one's own search cardinality -- not taken
   from the registrant. Deflating against one cycle's trials instead of the project's is the
   standard way this goes wrong.

Scope boundary. #19 owns power *policy* (estimands beyond Sharpe, and `UNDERPOWERED` as a
first-class research-memory state); this module implements the Sharpe arithmetic it needs, so
that the "recompute at execution" guarantee has something real to recompute. #6 owns refutation
memory and the structured migration of `REFUTED.md`; nothing here rewrites that record.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.backtest.experiment import canonical_result_json
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import hypotheses, hypothesis_data_windows

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Statistics are stored as text like every other number here, so the content hash is stable.
PRECISION = Decimal("0.000001")

#: Candidate evaluations this project ran before the registry existed. Derived from the bar
#: cycle 17 applied (2.90 = sqrt(2 ln N), so N ~= 67) and consistent with `REFUTED.md`'s
#: "~43 candidate evaluations" at cycle 10. Seeded rather than assumed zero: a registry that
#: started counting from one would hand back a luck bar the project has already spent.
PRIOR_TRIALS = 68


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
    UNDERPOWERED = "UNDERPOWERED"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"
    NOT_REGISTERED = "NOT_REGISTERED"
    TAMPERED = "TAMPERED"


class RegistrationRefused(ValueError):
    """Raised when a hypothesis may not be registered, or may not proceed to execution."""

    def __init__(self, reason: RefusalReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


def luck_threshold(trials: int) -> Decimal:
    """The t-statistic the best of `trials` skill-free candidates is expected to reach.

    `sqrt(2 ln N)`, the asymptotic maximum of N standard normals. It over-states the finite-N
    expectation, deliberately: in a project where every defect found so far flattered the
    result, a bar that errs high is the safe direction.

    Recorded rather than reconciled: cycles 15-17 quote this bar (62 trials -> 2.87, 68 -> 2.90)
    but cycle 10 quotes 2.22 for 43 trials, which is Bailey & Lopez de Prado's finite-N expected
    maximum -- about 0.5 lower at comparable N. Bars quoted before cycle 15 are therefore not
    comparable with bars quoted after it, and neither number is retro-fitted here.
    """
    if trials < 2:
        # No selection happened, so there is nothing to deflate: plain two-sided 5%.
        return Decimal("1.96")
    return _quantize(math.sqrt(2 * math.log(trials)))


def required_sessions(expected_sharpe: Decimal, threshold: Decimal, per_year: int) -> int:
    """Sessions needed for an annualised Sharpe of `expected_sharpe` to clear `threshold`.

    From t = SR * sqrt(years): years = (z / SR)^2.
    """
    if expected_sharpe <= 0:
        raise ValueError("expected_sharpe must be positive")
    if per_year <= 0:
        raise ValueError("per_year must be positive")
    return math.ceil(float(threshold / expected_sharpe) ** 2 * per_year)


def minimum_detectable_sharpe(sessions: int, threshold: Decimal, per_year: int) -> Decimal:
    """The smallest annualised Sharpe that `sessions` of data could separate from zero."""
    if sessions <= 0:
        raise ValueError("sessions must be positive")
    if per_year <= 0:
        raise ValueError("per_year must be positive")
    return _quantize(float(threshold) / math.sqrt(sessions / per_year))


def _quantize(value: float) -> Decimal:
    return Decimal(repr(value)).quantize(PRECISION, rounding=ROUND_HALF_EVEN)


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
    horizon_sessions: int = Field(ge=1)
    portfolio_interpretation: str | None = None

    windows: tuple[DataWindow, ...] = Field(min_length=1)

    primary_estimand: Text
    secondary_diagnostics: tuple[Text, ...] = ()
    #: Annualised Sharpe the registrant expects, and the smallest one worth acting on.
    expected_sharpe: Decimal = Field(gt=0, allow_inf_nan=False)
    minimum_practical_sharpe: Decimal = Field(gt=0, allow_inf_nan=False)
    effect_justification: Text
    available_sessions: int = Field(ge=1)
    sessions_per_year: int = Field(default=252, ge=1)
    #: Picks the correct test. Cycle 11 used unpaired t-tests on paired data and the overnight
    #: effect looked significant when it is not.
    comparison_structure: Literal["PAIRED", "UNPAIRED", "SINGLE_SAMPLE"]
    #: How many candidates were inspected to arrive at this one, counting itself. 1 when the
    #: candidate came from theory or literature rather than from mining data. This is the
    #: registration's whole cost against the project's multiple-testing budget.
    search_cardinality: int = Field(default=1, ge=1)

    costs: dict[str, str]
    confounders: tuple[Text, ...] = Field(min_length=1)
    epistemic_status: EpistemicStatus = EpistemicStatus.UNTESTED
    proposed_by: Text
    prompt_version: str | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> Self:
        if (self.parent_hypothesis_id is None) != (self.parent_version is None):
            raise ValueError("a parent reference needs both id and version")
        if not {"slippage_bps", "commission_per_order"} <= set(self.costs):
            raise ValueError("costs must state slippage_bps and commission_per_order")
        seen = {(window.dataset, window.role) for window in self.windows}
        if len(seen) != len(self.windows):
            raise ValueError("declare one contiguous window per dataset and role")
        if self.minimum_practical_sharpe > self.expected_sharpe:
            raise ValueError("expected effect cannot be below the minimum worth acting on")
        return self

    def windows_for(self, *roles: DataRole) -> tuple[DataWindow, ...]:
        return tuple(window for window in self.windows if window.role in roles)


class Registration(FrozenModel):
    """A draft plus what the registry computed, frozen together under one hash."""

    draft: HypothesisDraft
    registered_at: datetime
    #: Prior registrations plus this one's search cardinality. Counted, not declared.
    cumulative_trials: int
    luck_threshold_z: Decimal
    required_sessions: int
    minimum_detectable_sharpe: Decimal

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(canonical_result_json(self).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionClearance:
    """The re-check performed at execution, against state as it is *now*."""

    hypothesis_id: str
    version: int
    registration_hash: str
    registered_trials: int
    current_trials: int
    current_luck_threshold_z: Decimal
    current_required_sessions: int
    available_sessions: int


class HypothesisRegistry:
    """Freeze, retrieve, and re-verify registrations inside one caller-owned transaction."""

    def __init__(self, session: Session, *, prior_trials: int = PRIOR_TRIALS) -> None:
        self._session = session
        self._prior_trials = prior_trials

    def register(self, draft: HypothesisDraft, *, now: datetime) -> Registration:
        """Freeze a hypothesis, or refuse it. Refusal is the useful half."""
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

        trials = self._cumulative_trials(draft.search_cardinality)
        threshold = luck_threshold(trials)
        needed = required_sessions(draft.expected_sharpe, threshold, draft.sessions_per_year)
        if draft.available_sessions < needed:
            raise RegistrationRefused(
                RefusalReason.UNDERPOWERED,
                f"a Sharpe of {draft.expected_sharpe} needs {needed} sessions to clear the"
                f" t={threshold} bar for {trials} cumulative trials, and only"
                f" {draft.available_sessions} are available",
            )

        registration = Registration(
            draft=draft,
            registered_at=now,
            cumulative_trials=trials,
            luck_threshold_z=threshold,
            required_sessions=needed,
            minimum_detectable_sharpe=minimum_detectable_sharpe(
                draft.available_sessions, threshold, draft.sessions_per_year
            ),
        )
        self._insert(registration)
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

    def verify_for_execution(
        self,
        hypothesis_id: str,
        version: int,
        *,
        available_sessions: int | None = None,
    ) -> ExecutionClearance:
        """Re-check a frozen registration against current state before a confirmatory run.

        The registered numbers are not trusted: every registration since raises the bar, so a
        hypothesis that was adequately powered when frozen can be underpowered by the time it
        runs. Refuses rather than downgrades.
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

        sessions = (
            registration.draft.available_sessions
            if available_sessions is None
            else available_sessions
        )
        # This registration is already counted, so its own search is not added a second time.
        trials = self._cumulative_trials(0)
        threshold = luck_threshold(trials)
        needed = required_sessions(
            registration.draft.expected_sharpe, threshold, registration.draft.sessions_per_year
        )
        if sessions < needed:
            raise RegistrationRefused(
                RefusalReason.UNDERPOWERED,
                f"{hypothesis_id} v{version} needed {registration.required_sessions} sessions at"
                f" registration and needs {needed} now that {trials} trials have accumulated;"
                f" {sessions} are available",
            )
        return ExecutionClearance(
            hypothesis_id=hypothesis_id,
            version=version,
            registration_hash=stored_hash,
            registered_trials=registration.cumulative_trials,
            current_trials=trials,
            current_luck_threshold_z=threshold,
            current_required_sessions=needed,
            available_sessions=sessions,
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
                luck_threshold_z=str(registration.luck_threshold_z),
                expected_sharpe=str(draft.expected_sharpe),
                required_sessions=registration.required_sessions,
                available_sessions=draft.available_sessions,
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
    return {
        "hypothesis_id": draft.hypothesis_id,
        "version": draft.version,
        "family_id": draft.family_id,
        "question": draft.question,
        "falsified_if": draft.falsified_if,
        "registered_at": encode_utc(registration.registered_at),
        "content_hash": registration.content_hash,
        "cumulative_trials": registration.cumulative_trials,
        "luck_threshold_z": str(registration.luck_threshold_z),
        "expected_sharpe": str(draft.expected_sharpe),
        "required_sessions": registration.required_sessions,
        "available_sessions": draft.available_sessions,
        "minimum_detectable_sharpe": str(registration.minimum_detectable_sharpe),
        "windows": [
            f"{w.dataset}@{w.snapshot} {w.role.value} {w.start.isoformat()}..{w.end.isoformat()}"
            for w in draft.windows
        ],
    }
