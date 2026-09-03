"""Immutable, preregistered hostile-validation scenarios (#159).

Scenario names and worker prose are not evidence. A campaign freezes exact windows, selection
provenance, minimum activity, regime diagnostics and comparison burden before a result is
visible. Measurements return numbers only; this module derives every scenario verdict.
Resampled paths remain useful stress diagnostics but never become fresh market observations.

**Provenance.** The design here is selectively recovered from unfinished work on the
`codex/validation-campaign-159` branch, which was audited read-only and left untouched. What was
carried over is the modelling: content-addressed identity, deterministic selection, the
three-way regime distinction, thin-window semantics, and the walk-forward and resampling
boundaries. What was **not** carried over is its grader.

That grader asked ``abs(test_statistic) >= luck_threshold_z``, which is the defect #212 removed
from three sites elsewhere in this package. It predates that fix, and it would have reintroduced
it in the one module whose entire purpose is hostile validation -- a scenario measured at t=-9
against a positive claim would have been graded ``SURVIVED``, so the gate would have passed
precisely the results it exists to catch. Grading here calls
`power.supports_registered_direction`, the same authority the primary and artifact graders use.
It is deliberately not reimplemented locally: one signed comparison, one place to change it if
two-sided hypotheses ever become expressible.

**Scope.** This module is the campaign model and its grader, nothing else. It writes no
`search_runs` row, touches no schema, and is wired into no runner -- see the PR body for what is
deferred. `trial_charge` and `selection_search_id` exist so the later registry slice has
something stable to charge, and they are inert here: this module contains no accounting
mechanism of its own, because a second one would be a second authority.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.manifest import ExperimentPeriod, content_id
from quantbot.research.power import supports_registered_direction

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

#: Versioned so a change to how generated windows are laid out is visible as a different
#: campaign identity rather than as the same campaign quietly meaning something else.
CONTIGUOUS_ROTATION_POLICY_VERSION = "contiguous-calendar-v1"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScenarioKind(StrEnum):
    WALK_FORWARD = "WALK_FORWARD"
    REGIME = "REGIME"
    EXECUTION_STRESS = "EXECUTION_STRESS"
    LIQUIDITY_STRESS = "LIQUIDITY_STRESS"
    GENERALIZATION = "GENERALIZATION"
    RESAMPLED_PATH = "RESAMPLED_PATH"


class ScenarioSelectionMethod(StrEnum):
    """How the scenario windows were chosen, which is what decides whether they were searched."""

    #: Exact windows supplied by the registrant. Reproducible because they are literal.
    DECLARED = "DECLARED"
    #: Every window a frozen step produces over the eligible period. No choice is exercised.
    DETERMINISTIC_SWEEP = "DETERMINISTIC_SWEEP"
    #: A fixed number of non-overlapping blocks drawn from a frozen seed.
    SEEDED_RANDOM = "SEEDED_RANDOM"


class ScenarioEvidenceSource(StrEnum):
    OBSERVED_MARKET = "OBSERVED_MARKET"
    RESAMPLED_PATH = "RESAMPLED_PATH"


class ResamplingMethod(StrEnum):
    """Dependence-aware resampling methods this evidence model is willing to name.

    Named rather than free text because an i.i.d. bootstrap of a serially correlated return
    series manufactures independence that is not there, and the method is the only thing that
    says whether the dependence was respected.
    """

    STATIONARY_BLOCK_BOOTSTRAP = "STATIONARY_BLOCK_BOOTSTRAP"


class ConstraintOperator(StrEnum):
    AT_LEAST = "AT_LEAST"
    AT_MOST = "AT_MOST"


class RegimeConstraint(FrozenModel):
    """A measured diagnostic required for a configured regime label to be truthful."""

    metric: Text
    operator: ConstraintOperator
    threshold: Decimal = Field(allow_inf_nan=False)

    def satisfied_by(self, value: Decimal) -> bool:
        if self.operator is ConstraintOperator.AT_LEAST:
            return value >= self.threshold
        return value <= self.threshold


def select_rotation_periods(
    eligible_period: ExperimentPeriod,
    *,
    method: ScenarioSelectionMethod,
    window_days: int,
    count: int | None = None,
    step_days: int | None = None,
    seed: int | None = None,
) -> tuple[ExperimentPeriod, ...]:
    """Select reproducible, non-overlapping calendar windows without looking at outcomes.

    Declared periods are already exact inputs and do not pass through this helper. Generated
    periods use a versioned calendar-day policy: sweeps advance by a frozen step, while seeded
    draws sample from a fixed grid of non-overlapping contiguous blocks.

    **The function receives no returns and no outcome metrics**, so it cannot search for a
    favourable window. That is the property, and it is enforced by the signature rather than by
    intention.
    """
    if method is ScenarioSelectionMethod.DECLARED:
        raise ValueError("declared scenario periods are supplied exactly, not generated")
    if window_days < 1:
        raise ValueError("rotation window_days must be positive")
    eligible_days = (eligible_period.end - eligible_period.start).days + 1
    if window_days > eligible_days:
        raise ValueError("rotation window is longer than the eligible period")

    offsets: list[int]
    prefix: str
    if method is ScenarioSelectionMethod.DETERMINISTIC_SWEEP:
        if seed is not None or count is not None:
            raise ValueError("a deterministic sweep permits neither a seed nor a draw count")
        if step_days is None or step_days < window_days:
            raise ValueError("a sweep step must be at least one window to avoid dependence")
        offsets = list(range(0, eligible_days - window_days + 1, step_days))
        prefix = "sweep"
    else:
        if seed is None or count is None or count < 1:
            raise ValueError("a seeded rotation requires a seed and positive draw count")
        if step_days is not None:
            raise ValueError("a seeded rotation does not accept a sweep step")
        block_offsets = list(range(0, eligible_days - window_days + 1, window_days))
        if count > len(block_offsets):
            raise ValueError("seeded rotation requests more non-overlapping windows than exist")
        offsets = sorted(random.Random(seed).sample(block_offsets, count))
        prefix = "seeded"

    return tuple(
        ExperimentPeriod(
            name=f"{prefix}-{index:03d}",
            start=eligible_period.start + timedelta(days=offset),
            end=eligible_period.start + timedelta(days=offset + window_days - 1),
        )
        for index, offset in enumerate(offsets, start=1)
    )


class ValidationScenario(FrozenModel):
    """One exact, pre-outcome scenario; display text is excluded from behavior identity."""

    schema_version: int = Field(default=1, ge=1, le=1)
    kind: ScenarioKind
    dataset: Text
    data_role: Literal["VALIDATION", "PROTECTED_EVALUATION", "FORWARD_PAPER"]
    period: ExperimentPeriod
    fit_period: ExperimentPeriod | None = None
    #: Deliberately excludes PROTECTED_EVALUATION and FORWARD_PAPER: fitting on either would
    #: spend the window that exists to judge the fit.
    fit_data_role: Literal["DISCOVERY", "VALIDATION"] | None = None
    required: bool = True
    adverse: bool = False
    minimum_trades: int = Field(ge=1)
    minimum_effective_observations: int = Field(ge=1)
    regime_constraints: tuple[RegimeConstraint, ...] = ()
    evidence_source: ScenarioEvidenceSource = ScenarioEvidenceSource.OBSERVED_MARKET
    resampling_method: ResamplingMethod | None = None
    #: Operator-facing only. Excluded from `identity_payload` so renaming a scenario cannot
    #: present the same comparison as a new one.
    display_name: Text | None = None

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        if self.kind is ScenarioKind.WALK_FORWARD:
            if self.fit_period is None or self.fit_data_role is None:
                raise ValueError(
                    "a walk-forward scenario requires its exact fit period and data role"
                )
            if self.fit_period.end >= self.period.start:
                raise ValueError("walk-forward fitting must end before evaluation begins")
        elif self.fit_period is not None or self.fit_data_role is not None:
            raise ValueError("only walk-forward scenarios may declare fitting data")
        if self.kind is ScenarioKind.REGIME and not self.regime_constraints:
            raise ValueError("a regime scenario requires measured label constraints")
        resampled = self.evidence_source is ScenarioEvidenceSource.RESAMPLED_PATH
        if resampled != (self.kind is ScenarioKind.RESAMPLED_PATH):
            raise ValueError("resampled evidence must be explicitly typed as a resampled path")
        if resampled != (self.resampling_method is not None):
            raise ValueError(
                "resampled paths require a named dependence-aware resampling method"
            )
        if self.kind in {ScenarioKind.EXECUTION_STRESS, ScenarioKind.LIQUIDITY_STRESS} and (
            not self.adverse
        ):
            raise ValueError("execution and liquidity stresses must be declared adverse")
        metrics = [constraint.metric for constraint in self.regime_constraints]
        if len(metrics) != len(set(metrics)):
            raise ValueError("a scenario cannot constrain the same regime metric twice")
        return self

    def identity_payload(self) -> dict[str, object]:
        """Everything that changes what this scenario *tests*, and nothing that does not.

        `display_name` is absent, and periods contribute their dates rather than their
        `ExperimentPeriod.name`. Both are cosmetic, and a comparison that can be renamed into a
        fresh identity is a comparison whose burden can be reset for free.
        """
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "dataset": self.dataset,
            "data_role": self.data_role,
            "period": {"start": self.period.start, "end": self.period.end},
            "fit_period": (
                None
                if self.fit_period is None
                else {"start": self.fit_period.start, "end": self.fit_period.end}
            ),
            "fit_data_role": self.fit_data_role,
            "required": self.required,
            "adverse": self.adverse,
            "minimum_trades": self.minimum_trades,
            "minimum_effective_observations": self.minimum_effective_observations,
            "regime_constraints": self.regime_constraints,
            "evidence_source": self.evidence_source,
            "resampling_method": self.resampling_method,
        }

    @property
    def scenario_id(self) -> str:
        return content_id(self.identity_payload())


class ValidationCampaign(FrozenModel):
    """A bounded scenario selection, frozen before any outcome is visible.

    **Not bound to a hypothesis id here, deliberately.** A campaign is frozen *inside* the
    registration it belongs to, so containment is the binding; carrying a duplicate id would
    create a second copy that could disagree with its container. The registry slice that stores
    campaigns owns that relationship.
    """

    schema_version: int = Field(default=1, ge=1, le=1)
    selection_method: ScenarioSelectionMethod
    selection_policy_version: Text
    selection_seed: int | None = None
    rotation_window_days: int | None = Field(default=None, ge=1)
    rotation_window_count: int | None = Field(default=None, ge=1)
    rotation_step_days: int | None = Field(default=None, ge=1)
    eligible_period: ExperimentPeriod
    regime_classifier_version: Text
    stress_policy_version: Text
    neighborhood_grid: dict[Text, tuple[int, ...]]
    ablations: tuple[Text, ...] = ()
    scenarios: tuple[ValidationScenario, ...] = Field(min_length=2)
    maximum_underpowered_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_campaign(self) -> Self:
        declared = self.selection_method is ScenarioSelectionMethod.DECLARED
        seeded = self.selection_method is ScenarioSelectionMethod.SEEDED_RANDOM
        sweep = self.selection_method is ScenarioSelectionMethod.DETERMINISTIC_SWEEP
        if declared and any(
            value is not None
            for value in (
                self.selection_seed,
                self.rotation_window_days,
                self.rotation_window_count,
                self.rotation_step_days,
            )
        ):
            raise ValueError("declared selection permits no generated-rotation settings")
        if seeded != (self.selection_seed is not None):
            raise ValueError("only seeded-random selection requires and permits a seed")
        if seeded and (
            self.rotation_window_days is None
            or self.rotation_window_count is None
            or self.rotation_step_days is not None
        ):
            raise ValueError("seeded rotation requires window days/count and no sweep step")
        if sweep and (
            self.rotation_window_days is None
            or self.rotation_step_days is None
            or self.rotation_window_count is not None
        ):
            raise ValueError("a deterministic sweep requires window/step days and no count")
        if not declared and self.selection_policy_version != CONTIGUOUS_ROTATION_POLICY_VERSION:
            raise ValueError(
                "generated rotation must name the implemented contiguous-calendar-v1 policy"
            )
        ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("a validation campaign cannot repeat an equivalent scenario")
        if not self.neighborhood_grid or any(
            not values for values in self.neighborhood_grid.values()
        ):
            raise ValueError("campaign parameter-neighborhood entries must be nonempty")
        if len(self.ablations) != len(set(self.ablations)):
            raise ValueError("campaign ablations must be unique")
        if not any(scenario.required and scenario.adverse for scenario in self.scenarios):
            raise ValueError("a campaign requires at least one mandatory adverse scenario")
        if not declared:
            # Recomputed, not trusted. A campaign that names a seed and then lists windows the
            # seed does not produce is a search wearing a reproducible label.
            expected_ranges = {
                (period.start, period.end)
                for period in select_rotation_periods(
                    self.eligible_period,
                    method=self.selection_method,
                    window_days=self.rotation_window_days or 0,
                    count=self.rotation_window_count,
                    step_days=self.rotation_step_days,
                    seed=self.selection_seed,
                )
            }
            observed_ranges = {(item.period.start, item.period.end) for item in self.scenarios}
            if observed_ranges != expected_ranges:
                raise ValueError(
                    "generated scenario windows do not match the frozen rotation inputs"
                )
        for scenario in self.scenarios:
            if (
                scenario.period.start < self.eligible_period.start
                or scenario.period.end > self.eligible_period.end
            ):
                raise ValueError("every scenario must lie inside the frozen eligible period")
            if seeded and scenario.kind is ScenarioKind.REGIME:
                raise ValueError(
                    "seeded random selection cannot search outcome-derived regime labels"
                )
            if scenario.fit_period is not None and (
                scenario.fit_period.start < self.eligible_period.start
                or scenario.fit_period.end > self.eligible_period.end
            ):
                raise ValueError("walk-forward fitting must lie inside the eligible period")
        return self

    @property
    def campaign_id(self) -> str:
        return content_id(
            {
                "schema_version": self.schema_version,
                "selection_method": self.selection_method,
                "selection_policy_version": self.selection_policy_version,
                "selection_seed": self.selection_seed,
                "rotation_window_days": self.rotation_window_days,
                "rotation_window_count": self.rotation_window_count,
                "rotation_step_days": self.rotation_step_days,
                "eligible_period": {
                    "start": self.eligible_period.start,
                    "end": self.eligible_period.end,
                },
                "regime_classifier_version": self.regime_classifier_version,
                "stress_policy_version": self.stress_policy_version,
                "neighborhood_grid": self.neighborhood_grid,
                "ablations": self.ablations,
                "scenario_ids": sorted(scenario.scenario_id for scenario in self.scenarios),
                "maximum_underpowered_fraction": self.maximum_underpowered_fraction,
            }
        )

    @property
    def selection_search_id(self) -> str:
        """The identity a later registry slice will charge selection freedom against.

        Content-addressed, so redrawing or re-seeding a selection produces a different id. It is
        a *name* here and nothing more: this module writes no ledger row. See the module
        docstring.
        """
        return f"validation:{self.campaign_id}"

    @property
    def trial_charge(self) -> int:
        """Charge selection freedom once; scenario dispersion is one campaign result.

        Twelve scenarios chosen by one frozen rule are one declared comparison, not twelve
        opportunities to report a winner. Redrawing after seeing outcomes is the thing that costs
        more, and it costs more because it produces a different `selection_search_id`.
        """
        return 1


class ScenarioMeasured(FrozenModel):
    """Numbers reported for one frozen scenario. Carries no verdict, by construction.

    A measurement that could name its own verdict would put `SURVIVED` in the gift of the code
    being judged, which is the defect `Measured` was shaped to prevent for primary experiments.
    """

    scenario_id: Digest
    effect: Decimal | None = Field(default=None, allow_inf_nan=False)
    confidence_low: Decimal | None = Field(default=None, allow_inf_nan=False)
    confidence_high: Decimal | None = Field(default=None, allow_inf_nan=False)
    test_statistic: Decimal | None = Field(default=None, allow_inf_nan=False)
    trades: int = Field(ge=0)
    effective_observations: int = Field(ge=0)
    regime_metrics: dict[Text, Decimal] = Field(default_factory=dict)
    detail: Text | None = None

    @model_validator(mode="after")
    def finite_regime_metrics(self) -> Self:
        if any(not value.is_finite() for value in self.regime_metrics.values()):
            raise ValueError("regime diagnostics must be finite")
        return self


class ScenarioVerdict(StrEnum):
    SURVIVED = "SURVIVED"
    REFUTED = "REFUTED"
    #: Ran, and the window could not resolve the claim. Never collapsed into REFUTED.
    UNDERPOWERED = "UNDERPOWERED"
    #: The measured diagnostics contradict the configured regime label.
    MISLABELED = "MISLABELED"
    #: The diagnostics needed to check the label were not reported. Not agreement.
    UNVERIFIED = "UNVERIFIED"
    #: Nothing readable came back: absent, duplicated, or without a statistic.
    UNASSESSED = "UNASSESSED"


class CampaignVerdict(StrEnum):
    SURVIVED = "SURVIVED"
    REFUTED = "REFUTED"
    UNDERPOWERED = "UNDERPOWERED"
    #: An integrity defect stands over the campaign, so it says nothing either way.
    INCONCLUSIVE = "INCONCLUSIVE"


class ScenarioOutcome(FrozenModel):
    scenario_id: Digest
    verdict: ScenarioVerdict
    effect: Decimal | None = Field(default=None, allow_inf_nan=False)
    confidence_low: Decimal | None = Field(default=None, allow_inf_nan=False)
    confidence_high: Decimal | None = Field(default=None, allow_inf_nan=False)
    test_statistic: Decimal | None = Field(default=None, allow_inf_nan=False)
    trades: int = Field(ge=0)
    effective_observations: int = Field(ge=0)
    regime_metrics: dict[Text, Decimal] = Field(default_factory=dict)
    #: True only for a resolved observed-market result on a protected window. Resampled paths
    #: and unresolved scenarios are never fresh evidence, whatever their numbers look like.
    fresh_evidence: bool
    detail: Text


class CampaignOutcome(FrozenModel):
    campaign_id: Digest
    scenarios: tuple[ScenarioOutcome, ...]
    verdict: CampaignVerdict
    underpowered_count: int = Field(ge=0)
    underpowered_fraction: Decimal = Field(ge=0, le=1, allow_inf_nan=False)
    #: Listed rather than scored. A mandatory adverse failure that can be averaged against a
    #: favourable mean is not a mandatory failure.
    integrity_defects: tuple[Text, ...] = ()
    detail: Text


def _scenario_verdict(
    scenario: ValidationScenario,
    measured: ScenarioMeasured,
    luck_threshold_z: Decimal,
) -> tuple[ScenarioVerdict, str]:
    """Derive one scenario's verdict. Ordering below is the protection, not a formality."""
    missing_metrics = [
        constraint.metric
        for constraint in scenario.regime_constraints
        if constraint.metric not in measured.regime_metrics
    ]
    contradicted = [
        constraint.metric
        for constraint in scenario.regime_constraints
        if constraint.metric in measured.regime_metrics
        and not constraint.satisfied_by(measured.regime_metrics[constraint.metric])
    ]
    # Regime first: a result measured in a window that is not the regime it claims is a result
    # about a different question, however strong its statistic.
    if contradicted:
        detail = []
        if missing_metrics:
            detail.append("missing regime diagnostics: " + ", ".join(missing_metrics))
        detail.append("measured regime contradicts label: " + ", ".join(contradicted))
        return ScenarioVerdict.MISLABELED, "; ".join(detail)
    if missing_metrics:
        return (
            ScenarioVerdict.UNVERIFIED,
            "regime unavailable; missing diagnostics: " + ", ".join(missing_metrics),
        )
    # Activity before significance. A window with no trades has not tested anything, and a
    # statistic computed over nothing must not read as "no difference found".
    if (
        measured.trades < scenario.minimum_trades
        or measured.effective_observations < scenario.minimum_effective_observations
    ):
        return (
            ScenarioVerdict.UNDERPOWERED,
            f"activity {measured.trades} trades/{measured.effective_observations} effective "
            f"observations is below the frozen {scenario.minimum_trades}/"
            f"{scenario.minimum_effective_observations}",
        )
    if measured.test_statistic is None:
        return ScenarioVerdict.UNASSESSED, "the scenario reported no test statistic"
    # Signed, via the shared authority. See the module docstring for what this replaced.
    if supports_registered_direction(measured.test_statistic, luck_threshold_z):
        return (
            ScenarioVerdict.SURVIVED,
            f"t={measured.test_statistic} cleared the {luck_threshold_z} bar",
        )
    if measured.test_statistic <= -luck_threshold_z:
        return (
            ScenarioVerdict.REFUTED,
            f"t={measured.test_statistic} is significant in the direction opposite to the "
            "registered claim, which is evidence against it rather than for it",
        )
    return (
        ScenarioVerdict.REFUTED,
        f"t={measured.test_statistic} did not clear the {luck_threshold_z} bar",
    )


def grade_campaign(
    campaign: ValidationCampaign,
    measurements: tuple[ScenarioMeasured, ...],
    *,
    luck_threshold_z: Decimal,
) -> CampaignOutcome:
    """Derive lossless outcomes for every frozen scenario, including missing/duplicate output.

    Every frozen scenario appears in the result, whether or not a measurement arrived for it.
    A scenario that quietly vanished because nobody reported it is the cheapest way to make a
    mandatory adverse test disappear.
    """
    if not luck_threshold_z.is_finite() or luck_threshold_z <= 0:
        raise ValueError("scenario luck threshold must be positive and finite")
    known = {scenario.scenario_id for scenario in campaign.scenarios}
    unknown = sorted({item.scenario_id for item in measurements} - known)
    integrity_defects = (
        ("measurements named unknown scenario ids: " + ", ".join(unknown),) if unknown else ()
    )
    supplied: dict[str, ScenarioMeasured] = {}
    duplicates: set[str] = set()
    for measurement in measurements:
        if measurement.scenario_id in supplied:
            duplicates.add(measurement.scenario_id)
        else:
            supplied[measurement.scenario_id] = measurement

    outcomes: list[ScenarioOutcome] = []
    for scenario in campaign.scenarios:
        measured = supplied.get(scenario.scenario_id)
        if measured is None or scenario.scenario_id in duplicates:
            outcomes.append(
                ScenarioOutcome(
                    scenario_id=scenario.scenario_id,
                    verdict=ScenarioVerdict.UNASSESSED,
                    trades=0 if measured is None else measured.trades,
                    effective_observations=(
                        0 if measured is None else measured.effective_observations
                    ),
                    fresh_evidence=False,
                    detail=(
                        "the frozen scenario reported no result"
                        if measured is None
                        else "the frozen scenario reported duplicate, ambiguous results"
                    ),
                )
            )
            continue
        verdict, explanation = _scenario_verdict(scenario, measured, luck_threshold_z)
        outcomes.append(
            ScenarioOutcome(
                scenario_id=scenario.scenario_id,
                verdict=verdict,
                effect=measured.effect,
                confidence_low=measured.confidence_low,
                confidence_high=measured.confidence_high,
                test_statistic=measured.test_statistic,
                trades=measured.trades,
                effective_observations=measured.effective_observations,
                regime_metrics=dict(measured.regime_metrics),
                fresh_evidence=(
                    scenario.evidence_source is ScenarioEvidenceSource.OBSERVED_MARKET
                    and scenario.data_role == "PROTECTED_EVALUATION"
                    and verdict in {ScenarioVerdict.SURVIVED, ScenarioVerdict.REFUTED}
                ),
                detail=explanation,
            )
        )

    unresolved = [
        outcome
        for outcome in outcomes
        if outcome.verdict
        in {
            ScenarioVerdict.MISLABELED,
            ScenarioVerdict.UNASSESSED,
            ScenarioVerdict.UNVERIFIED,
        }
    ]
    if unresolved:
        integrity_defects = (
            *integrity_defects,
            "scenario integrity unresolved: "
            + ", ".join(outcome.scenario_id for outcome in unresolved),
        )
    underpowered = [
        outcome for outcome in outcomes if outcome.verdict is ScenarioVerdict.UNDERPOWERED
    ]
    underpowered_fraction = Decimal(len(underpowered)) / Decimal(len(outcomes))
    required_ids = {scenario.scenario_id for scenario in campaign.scenarios if scenario.required}
    required_underpowered = [
        outcome for outcome in underpowered if outcome.scenario_id in required_ids
    ]
    required_refuted = [
        outcome
        for outcome in outcomes
        if outcome.scenario_id in required_ids and outcome.verdict is ScenarioVerdict.REFUTED
    ]

    # Precedence, worst first. An integrity defect outranks a number because a number measured
    # under an unresolved defect is not a number about this campaign.
    if integrity_defects:
        campaign_verdict = CampaignVerdict.INCONCLUSIVE
        detail = "; ".join(integrity_defects)
    elif required_underpowered:
        campaign_verdict = CampaignVerdict.UNDERPOWERED
        detail = "required scenarios were underpowered: " + ", ".join(
            outcome.scenario_id for outcome in required_underpowered
        )
    elif underpowered_fraction > campaign.maximum_underpowered_fraction:
        campaign_verdict = CampaignVerdict.UNDERPOWERED
        detail = (
            f"underpowered share {underpowered_fraction} exceeds the frozen ceiling "
            f"{campaign.maximum_underpowered_fraction}"
        )
    elif required_refuted:
        campaign_verdict = CampaignVerdict.REFUTED
        detail = "required scenarios failed: " + ", ".join(
            outcome.scenario_id for outcome in required_refuted
        )
    else:
        campaign_verdict = CampaignVerdict.SURVIVED
        detail = "every required scenario passed and unresolved activity stayed within policy"

    return CampaignOutcome(
        campaign_id=campaign.campaign_id,
        scenarios=tuple(outcomes),
        verdict=campaign_verdict,
        underpowered_count=len(underpowered),
        underpowered_fraction=underpowered_fraction,
        integrity_defects=integrity_defects,
        detail=detail,
    )


__all__ = [
    "CONTIGUOUS_ROTATION_POLICY_VERSION",
    "CampaignOutcome",
    "CampaignVerdict",
    "ConstraintOperator",
    "RegimeConstraint",
    "ResamplingMethod",
    "ScenarioEvidenceSource",
    "ScenarioKind",
    "ScenarioMeasured",
    "ScenarioOutcome",
    "ScenarioSelectionMethod",
    "ScenarioVerdict",
    "ValidationCampaign",
    "ValidationScenario",
    "grade_campaign",
    "select_rotation_periods",
]
