"""A validation campaign is trustworthy mechanics, and these pin what makes it trustworthy.

Nothing here is evidence about a market. Every measurement is a synthetic fixture, so a green
suite means the grader derives the verdicts it claims to derive -- not that any strategy works.

The property that matters most is the one this module's predecessor got wrong. The unfinished
`codex/validation-campaign-159` grader asked `abs(test_statistic) >= luck_threshold_z`, so a
scenario measured at t=-9 against a positive claim graded `SURVIVED`. That is the defect #212
removed from three sites elsewhere, and it would have returned here -- in the module whose whole
purpose is hostile validation, where it would have passed exactly the results the campaign
exists to catch. `test_a_strong_opposite_direction_scenario_cannot_survive` fails if anyone
reintroduces it.

Every blocking case below is paired with a control that survives, so a grader that refused
everything would fail these as loudly as one that accepted everything.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from quantbot.research.manifest import ExperimentPeriod
from quantbot.research.validation_campaign import (
    CONTIGUOUS_ROTATION_POLICY_VERSION,
    CampaignOutcome,
    CampaignVerdict,
    ConstraintOperator,
    RegimeConstraint,
    ResamplingMethod,
    ScenarioEvidenceSource,
    ScenarioKind,
    ScenarioMeasured,
    ScenarioOutcome,
    ScenarioSelectionMethod,
    ScenarioVerdict,
    ValidationCampaign,
    ValidationScenario,
    grade_campaign,
    select_rotation_periods,
)

#: The bar every scenario below is graded against. Positive, because every hypothesis this
#: schema can express is a one-sided positive claim.
BAR = Decimal("2.0")

ELIGIBLE = ExperimentPeriod(name="eligible", start=date(2016, 1, 4), end=date(2026, 8, 14))


def period(name: str, start: date, end: date) -> ExperimentPeriod:
    return ExperimentPeriod(name=name, start=start, end=end)


def scenario(**overrides: object) -> ValidationScenario:
    """A required adverse execution stress. Every case varies one field from this."""
    fields: dict[str, object] = {
        "kind": ScenarioKind.EXECUTION_STRESS,
        "dataset": "sip-us-equities-daily",
        "data_role": "PROTECTED_EVALUATION",
        "period": period("stress", date(2018, 1, 2), date(2019, 12, 31)),
        "required": True,
        "adverse": True,
        "minimum_trades": 30,
        "minimum_effective_observations": 200,
    }
    fields.update(overrides)
    return ValidationScenario(**fields)


def campaign(**overrides: object) -> ValidationCampaign:
    """A declared two-scenario campaign: one required adverse stress, one generalization."""
    fields: dict[str, object] = {
        "selection_method": ScenarioSelectionMethod.DECLARED,
        "selection_policy_version": "declared-v1",
        "eligible_period": ELIGIBLE,
        "regime_classifier_version": "regime-v1",
        "stress_policy_version": "stress-v1",
        "neighborhood_grid": {"trend_period": (150, 200, 250)},
        "scenarios": (
            scenario(),
            scenario(
                kind=ScenarioKind.GENERALIZATION,
                adverse=False,
                period=period("general", date(2020, 1, 2), date(2021, 12, 31)),
            ),
        ),
        "maximum_underpowered_fraction": Decimal("0.5"),
    }
    fields.update(overrides)
    return ValidationCampaign(**fields)


def measured(target: ValidationScenario, **overrides: object) -> ScenarioMeasured:
    """A comfortably resolved, comfortably active measurement for `target`."""
    fields: dict[str, object] = {
        "scenario_id": target.scenario_id,
        "effect": Decimal("1.0"),
        "test_statistic": Decimal("9.0"),
        "trades": 60,
        "effective_observations": 400,
    }
    fields.update(overrides)
    return ScenarioMeasured(**fields)


def graded(subject: ValidationCampaign, *measurements: ScenarioMeasured):
    return grade_campaign(subject, tuple(measurements), luck_threshold_z=BAR)


def verdict_of(outcome, target: ValidationScenario) -> ScenarioVerdict:
    return next(
        item.verdict for item in outcome.scenarios if item.scenario_id == target.scenario_id
    )


# --- the #212 protection -------------------------------------------------------------------


def test_a_strong_opposite_direction_scenario_cannot_survive() -> None:
    """**The regression this module exists not to reintroduce.**

    t=-9 against a bar of +2 is overwhelming evidence *against* the registered direction. Under
    the predecessor's `abs(statistic) >= threshold` it graded `SURVIVED`, and because the
    scenario is required and adverse, the whole campaign then reported `SURVIVED` -- a hostile
    gate passing the exact result it exists to catch.

    The reason is asserted as well as the verdict: "significant the other way" and "not
    significant" are different findings, and only the reason distinguishes them.
    """
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(
        subject,
        measured(stress, test_statistic=Decimal("-9.0"), effect=Decimal("-1.0")),
        measured(general),
    )

    assert verdict_of(outcome, stress) is ScenarioVerdict.REFUTED
    assert outcome.verdict is CampaignVerdict.REFUTED
    detail = next(
        item.detail for item in outcome.scenarios if item.scenario_id == stress.scenario_id
    )
    assert "opposite to the registered claim" in detail
    assert "evidence against it" in detail


def test_a_strong_supporting_scenario_survives() -> None:
    """The control. Without it, a grader that refused every statistic would satisfy the test
    above and every other blocking case in this file."""
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(subject, measured(stress), measured(general))

    assert verdict_of(outcome, stress) is ScenarioVerdict.SURVIVED
    assert outcome.verdict is CampaignVerdict.SURVIVED


def test_the_bar_is_signed_at_both_boundaries() -> None:
    """Exactly the bar survives; exactly its negation does not. `-BAR` is the first value the
    old `abs()` rule wrongly certified, so it is the one that must have moved."""
    subject = campaign()
    stress, general = subject.scenarios

    on_bar = graded(subject, measured(stress, test_statistic=BAR), measured(general))
    assert verdict_of(on_bar, stress) is ScenarioVerdict.SURVIVED

    negated = graded(subject, measured(stress, test_statistic=-BAR), measured(general))
    assert verdict_of(negated, stress) is ScenarioVerdict.REFUTED


# --- deterministic selection ---------------------------------------------------------------


def test_the_same_seed_reproduces_the_same_windows_and_identity() -> None:
    """A seeded campaign is reproducible or it is a search with a reproducible-sounding label."""
    kwargs: dict[str, object] = {
        "method": ScenarioSelectionMethod.SEEDED_RANDOM,
        "window_days": 180,
        "count": 3,
        "seed": 20260903,
    }
    first = select_rotation_periods(ELIGIBLE, **kwargs)  # type: ignore[arg-type]
    second = select_rotation_periods(ELIGIBLE, **kwargs)  # type: ignore[arg-type]

    assert first == second
    assert len({(item.start, item.end) for item in first}) == 3

    seeded = _seeded_campaign(seed=20260903)
    assert seeded.campaign_id == _seeded_campaign(seed=20260903).campaign_id
    # A different seed is a different selection, so it must be a different campaign.
    assert _seeded_campaign(seed=20260904).campaign_id != seeded.campaign_id


def test_seeded_windows_that_do_not_match_the_seed_are_refused() -> None:
    """The scenarios are recomputed from the frozen inputs rather than trusted. A campaign that
    names a seed and lists windows the seed does not produce is a search."""
    seeded = _seeded_campaign(seed=20260903)
    moved = seeded.scenarios[0].model_copy(
        update={"period": period("moved", date(2022, 3, 1), date(2022, 8, 27))}
    )

    # Constructed directly, so the campaign validator actually runs. `model_copy` alone would
    # not re-validate, and a test that skipped validation would pass against a broken rule.
    with pytest.raises(ValueError, match="do not match the frozen rotation inputs"):
        campaign(
            selection_method=ScenarioSelectionMethod.SEEDED_RANDOM,
            selection_policy_version=CONTIGUOUS_ROTATION_POLICY_VERSION,
            selection_seed=20260903,
            rotation_window_days=180,
            rotation_window_count=3,
            scenarios=(moved, *seeded.scenarios[1:]),
        )


def test_a_deterministic_sweep_exercises_no_choice() -> None:
    """A sweep produces every window its frozen step allows, so there is nothing to choose."""
    swept = select_rotation_periods(
        ELIGIBLE,
        method=ScenarioSelectionMethod.DETERMINISTIC_SWEEP,
        window_days=180,
        step_days=365,
    )

    assert len(swept) > 1
    assert swept == select_rotation_periods(
        ELIGIBLE,
        method=ScenarioSelectionMethod.DETERMINISTIC_SWEEP,
        window_days=180,
        step_days=365,
    )
    with pytest.raises(ValueError, match="at least one window"):
        select_rotation_periods(
            ELIGIBLE,
            method=ScenarioSelectionMethod.DETERMINISTIC_SWEEP,
            window_days=180,
            step_days=30,
        )


def _seeded_campaign(*, seed: int) -> ValidationCampaign:
    periods = select_rotation_periods(
        ELIGIBLE,
        method=ScenarioSelectionMethod.SEEDED_RANDOM,
        window_days=180,
        count=3,
        seed=seed,
    )
    scenarios = tuple(
        scenario(
            kind=ScenarioKind.EXECUTION_STRESS if index == 0 else ScenarioKind.GENERALIZATION,
            adverse=index == 0,
            period=item,
        )
        for index, item in enumerate(periods)
    )
    return campaign(
        selection_method=ScenarioSelectionMethod.SEEDED_RANDOM,
        selection_policy_version=CONTIGUOUS_ROTATION_POLICY_VERSION,
        selection_seed=seed,
        rotation_window_days=180,
        rotation_window_count=3,
        scenarios=scenarios,
    )


# --- mandatory adverse scenarios block ------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_scenario", "expected_campaign"),
    [
        # A weak statistic in the registered direction: an ordinary failure.
        (
            {"test_statistic": Decimal("0.4")},
            ScenarioVerdict.REFUTED,
            CampaignVerdict.REFUTED,
        ),
        # Too few trades to have tested anything.
        ({"trades": 2}, ScenarioVerdict.UNDERPOWERED, CampaignVerdict.UNDERPOWERED),
        # Enough trades, too little effective sample after dependence.
        (
            {"effective_observations": 10},
            ScenarioVerdict.UNDERPOWERED,
            CampaignVerdict.UNDERPOWERED,
        ),
        # Ran, but reported nothing readable.
        ({"test_statistic": None}, ScenarioVerdict.UNASSESSED, CampaignVerdict.INCONCLUSIVE),
    ],
)
def test_a_required_adverse_scenario_blocks_campaign_survival(
    overrides: dict[str, object],
    expected_scenario: ScenarioVerdict,
    expected_campaign: CampaignVerdict,
) -> None:
    """No mandatory adverse failure may be averaged away by a favourable sibling.

    The generalization scenario is measured at t=9 in every case, so a grader that scored the
    campaign on a mean rather than on the mandatory result would report `SURVIVED` for all four.
    """
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(subject, measured(stress, **overrides), measured(general))

    assert verdict_of(outcome, stress) is expected_scenario
    assert verdict_of(outcome, general) is ScenarioVerdict.SURVIVED
    assert outcome.verdict is expected_campaign
    assert outcome.verdict is not CampaignVerdict.SURVIVED


def test_a_missing_required_scenario_cannot_quietly_disappear() -> None:
    """Reporting nothing for a mandatory adverse test is the cheapest way to delete it."""
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(subject, measured(general))

    assert verdict_of(outcome, stress) is ScenarioVerdict.UNASSESSED
    assert outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert len(outcome.scenarios) == 2


# --- regime integrity -----------------------------------------------------------------------


def _regime_campaign() -> tuple[ValidationCampaign, ValidationScenario, ValidationScenario]:
    regime = scenario(
        kind=ScenarioKind.REGIME,
        adverse=True,
        required=True,
        regime_constraints=(
            RegimeConstraint(
                metric="realised_volatility",
                operator=ConstraintOperator.AT_LEAST,
                threshold=Decimal("0.25"),
            ),
        ),
    )
    general = scenario(
        kind=ScenarioKind.GENERALIZATION,
        adverse=False,
        period=period("general", date(2020, 1, 2), date(2021, 12, 31)),
    )
    return campaign(scenarios=(regime, general)), regime, general


def test_a_measured_regime_that_contradicts_its_label_is_mislabeled() -> None:
    """A high-volatility stress measured in a calm window is a result about another question."""
    subject, regime, general = _regime_campaign()

    outcome = graded(
        subject,
        measured(regime, regime_metrics={"realised_volatility": Decimal("0.05")}),
        measured(general),
    )

    assert verdict_of(outcome, regime) is ScenarioVerdict.MISLABELED
    assert outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert outcome.integrity_defects


def test_an_unavailable_regime_diagnostic_is_unverified_not_agreement() -> None:
    """Missing is not agreement. Reading an absent diagnostic as a pass is how a label nobody
    checked becomes a label everybody trusts."""
    subject, regime, general = _regime_campaign()

    outcome = graded(subject, measured(regime), measured(general))

    assert verdict_of(outcome, regime) is ScenarioVerdict.UNVERIFIED
    assert outcome.verdict is CampaignVerdict.INCONCLUSIVE


def test_a_measured_regime_that_agrees_is_eligible_to_count() -> None:
    """The control for both cases above: agreement is graded on its statistic as normal."""
    subject, regime, general = _regime_campaign()

    outcome = graded(
        subject,
        measured(regime, regime_metrics={"realised_volatility": Decimal("0.40")}),
        measured(general),
    )

    assert verdict_of(outcome, regime) is ScenarioVerdict.SURVIVED
    assert outcome.verdict is CampaignVerdict.SURVIVED
    assert outcome.integrity_defects == ()


# --- thin windows ---------------------------------------------------------------------------


def test_zero_trades_is_underpowered_and_never_equivalence() -> None:
    """A window that traded nothing has not tested anything.

    The measurement even carries a statistic of exactly zero -- the shape "no difference found"
    takes -- and it must still not be read as a scenario the strategy came through.
    """
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(
        subject,
        measured(
            stress,
            trades=0,
            effective_observations=0,
            effect=Decimal("0"),
            test_statistic=Decimal("0"),
        ),
        measured(general),
    )

    assert verdict_of(outcome, stress) is ScenarioVerdict.UNDERPOWERED
    assert outcome.verdict is CampaignVerdict.UNDERPOWERED
    assert outcome.verdict is not CampaignVerdict.SURVIVED


def test_exceeding_the_frozen_underpowered_ceiling_blocks_survival() -> None:
    """The ceiling is frozen with the campaign, so an optimizer cannot widen it after the fact.

    The mandatory adverse scenario here *passes*, and the thin one is optional, so neither the
    required-refuted nor the required-underpowered rule can fire. Only the share blocks, which
    isolates the ceiling from the mandatory-scenario rules that would otherwise mask it.
    """
    stress = scenario()
    thin = scenario(
        kind=ScenarioKind.GENERALIZATION,
        required=False,
        adverse=False,
        period=period("thin", date(2020, 1, 2), date(2021, 12, 31)),
    )
    healthy = scenario(
        kind=ScenarioKind.GENERALIZATION,
        required=False,
        adverse=False,
        period=period("healthy", date(2022, 1, 3), date(2023, 12, 29)),
    )
    scenarios = (stress, thin, healthy)
    one_third = Decimal(1) / Decimal(3)

    subject = campaign(scenarios=scenarios, maximum_underpowered_fraction=Decimal("0.25"))
    outcome = graded(
        subject, measured(stress), measured(thin, trades=1), measured(healthy)
    )

    assert verdict_of(outcome, stress) is ScenarioVerdict.SURVIVED
    assert outcome.underpowered_count == 1
    assert outcome.underpowered_fraction == one_third
    assert outcome.verdict is CampaignVerdict.UNDERPOWERED

    # The control: the identical measurements under a ceiling that permits the same share.
    permitted = campaign(scenarios=scenarios, maximum_underpowered_fraction=Decimal("0.5"))
    assert graded(
        permitted, measured(stress), measured(thin, trades=1), measured(healthy)
    ).verdict is CampaignVerdict.SURVIVED


# --- walk-forward boundaries ------------------------------------------------------------------


def test_walk_forward_fitting_may_not_reach_or_overlap_evaluation() -> None:
    """No future observation may enter fitting. Both the overlapping and the exactly-adjacent
    case are refused, because a fit ending on the first evaluation day has seen it."""
    evaluation = period("evaluate", date(2020, 1, 2), date(2021, 12, 31))

    with pytest.raises(ValueError, match="must end before evaluation begins"):
        scenario(
            kind=ScenarioKind.WALK_FORWARD,
            period=evaluation,
            fit_period=period("fit", date(2018, 1, 2), date(2020, 6, 30)),
            fit_data_role="DISCOVERY",
        )
    with pytest.raises(ValueError, match="must end before evaluation begins"):
        scenario(
            kind=ScenarioKind.WALK_FORWARD,
            period=evaluation,
            fit_period=period("fit", date(2018, 1, 2), date(2020, 1, 2)),
            fit_data_role="DISCOVERY",
        )

    # The control: a fit ending the day before evaluation is accepted.
    accepted = scenario(
        kind=ScenarioKind.WALK_FORWARD,
        period=evaluation,
        fit_period=period("fit", date(2018, 1, 2), date(2020, 1, 1)),
        fit_data_role="DISCOVERY",
    )
    assert accepted.fit_period is not None


def test_a_walk_forward_scenario_must_declare_its_fit() -> None:
    """A walk-forward claim with no stated fit window cannot be checked for leakage at all."""
    with pytest.raises(ValueError, match="requires its exact fit period and data role"):
        scenario(kind=ScenarioKind.WALK_FORWARD)
    with pytest.raises(ValueError, match="only walk-forward scenarios may declare fitting data"):
        scenario(fit_period=period("fit", date(2017, 1, 2), date(2017, 12, 29)))


# --- resampled evidence -----------------------------------------------------------------------


def test_a_resampled_path_cannot_mint_fresh_protected_evidence() -> None:
    """Simulated paths are stress diagnostics. They are not new market observations, however
    many of them are drawn and however favourable they look."""
    resampled = scenario(
        kind=ScenarioKind.RESAMPLED_PATH,
        adverse=True,
        evidence_source=ScenarioEvidenceSource.RESAMPLED_PATH,
        resampling_method=ResamplingMethod.STATIONARY_BLOCK_BOOTSTRAP,
    )
    observed = scenario(
        kind=ScenarioKind.GENERALIZATION,
        adverse=False,
        period=period("general", date(2020, 1, 2), date(2021, 12, 31)),
    )
    subject = campaign(scenarios=(resampled, observed))

    outcome = graded(subject, measured(resampled), measured(observed))

    resampled_outcome = next(
        item for item in outcome.scenarios if item.scenario_id == resampled.scenario_id
    )
    observed_outcome = next(
        item for item in outcome.scenarios if item.scenario_id == observed.scenario_id
    )
    # Both survived on their statistics; only the observed-market one is fresh evidence.
    assert resampled_outcome.verdict is ScenarioVerdict.SURVIVED
    assert resampled_outcome.fresh_evidence is False
    assert observed_outcome.fresh_evidence is True


def test_resampling_must_be_typed_and_must_name_its_method() -> None:
    """An unnamed resampling method hides whether serial dependence was respected."""
    with pytest.raises(ValueError, match="explicitly typed as a resampled path"):
        scenario(evidence_source=ScenarioEvidenceSource.RESAMPLED_PATH)
    with pytest.raises(ValueError, match="dependence-aware resampling method"):
        scenario(
            kind=ScenarioKind.RESAMPLED_PATH,
            evidence_source=ScenarioEvidenceSource.RESAMPLED_PATH,
        )


# --- identity ----------------------------------------------------------------------------------


def test_cosmetic_renaming_does_not_create_a_new_campaign() -> None:
    """Display text and period labels are excluded from identity. A comparison that can be
    renamed into a fresh identity is a comparison whose burden resets for free."""
    plain = scenario()
    renamed = plain.model_copy(
        update={
            "display_name": "The Big Scary Stress Test",
            "period": period("renamed", plain.period.start, plain.period.end),
        }
    )

    assert renamed.scenario_id == plain.scenario_id

    other = scenario(
        kind=ScenarioKind.GENERALIZATION,
        adverse=False,
        period=period("general", date(2020, 1, 2), date(2021, 12, 31)),
    )
    assert campaign(scenarios=(renamed, other)).campaign_id == (
        campaign(scenarios=(plain, other)).campaign_id
    )


def test_a_substantive_change_does_create_a_new_campaign() -> None:
    """The control for the case above, and the prerequisite the later registry slice charges
    against: changing what is actually tested must change the immutable identity."""
    subject = campaign()
    stress, general = subject.scenarios

    tighter = stress.model_copy(update={"minimum_trades": 31})
    assert tighter.scenario_id != stress.scenario_id
    assert campaign(scenarios=(tighter, general)).campaign_id != subject.campaign_id

    widened = subject.model_copy(update={"maximum_underpowered_fraction": Decimal("0.9")})
    assert widened.campaign_id != subject.campaign_id

    assert subject.selection_search_id == f"validation:{subject.campaign_id}"
    assert subject.trial_charge == 1


def test_a_campaign_requires_a_mandatory_adverse_scenario() -> None:
    """A campaign of only friendly scenarios is not hostile validation."""
    friendly = scenario(adverse=False, kind=ScenarioKind.GENERALIZATION)
    other = scenario(
        kind=ScenarioKind.GENERALIZATION,
        adverse=False,
        period=period("general", date(2020, 1, 2), date(2021, 12, 31)),
    )

    with pytest.raises(ValueError, match="requires at least one mandatory adverse scenario"):
        campaign(scenarios=(friendly, other))


def test_a_measurement_cannot_carry_its_own_verdict() -> None:
    """`ScenarioMeasured` reports numbers. A measurement able to name its verdict would put
    SURVIVED in the gift of the code being judged."""
    assert not hasattr(ScenarioMeasured, "verdict")
    with pytest.raises(ValueError):
        ScenarioMeasured(
            scenario_id="a" * 64,
            trades=1,
            effective_observations=1,
            verdict="SURVIVED",  # type: ignore[call-arg]
        )


def test_measurements_for_unknown_scenarios_are_an_integrity_defect() -> None:
    """Grading only what was reported would let a campaign be answered with a different one."""
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(
        subject,
        measured(stress),
        measured(general),
        ScenarioMeasured(
            scenario_id="b" * 64, trades=10, effective_observations=100,
            test_statistic=Decimal("9.0"),
        ),
    )

    assert outcome.verdict is CampaignVerdict.INCONCLUSIVE
    assert any("unknown scenario ids" in defect for defect in outcome.integrity_defects)


def test_duplicate_measurements_are_ambiguous_rather_than_first_wins() -> None:
    """Two answers for one frozen scenario is a defect, not an opportunity to pick one."""
    subject = campaign()
    stress, general = subject.scenarios

    outcome = graded(
        subject,
        measured(stress),
        measured(stress, test_statistic=Decimal("-9.0")),
        measured(general),
    )

    assert verdict_of(outcome, stress) is ScenarioVerdict.UNASSESSED
    assert outcome.verdict is CampaignVerdict.INCONCLUSIVE


def test_the_luck_bar_must_be_positive_and_finite() -> None:
    """A non-positive bar would make every statistic clear it, including negative ones."""
    subject = campaign()
    for bad in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValueError, match="must be positive and finite"):
            grade_campaign(subject, (), luck_threshold_z=bad)


# --- #159: a campaign outcome has to be a shape some measurements could have produced ----------
#
# `promotion.survivor_objections` reads `CampaignOutcome.verdict` and treats it as the campaign's
# truth. So do the runner and `ExperimentOutcome`. All three were described as independently
# agreeing, while nothing established that a `CampaignOutcome` could hold the values it claimed:
# a caller needed no `model_construct` to declare a top-level SURVIVED campaign with no scenarios
# in it at all. `grade_campaign` cannot emit that -- a frozen campaign carries at least two
# scenarios and the grader writes one outcome for every one of them -- so the aggregate was
# effectively caller-declared, wearing a derived verdict's clothes.
#
# The rules below are only those true of *every* output of `grade_campaign`. Which scenarios
# carry the aggregate down depends on `required` and `maximum_underpowered_fraction`, neither of
# which is reachable from a `CampaignOutcome`; reimplementing that here would create a second
# aggregation policy to drift from #215's. The last two tests in this section are the guard
# against exactly that overreach.


def survived_outcome() -> CampaignOutcome:
    """A genuinely graded, genuinely surviving campaign: the base every mutation starts from."""
    subject = campaign()
    stress, general = subject.scenarios
    result = graded(subject, measured(stress), measured(general))
    assert result.verdict is CampaignVerdict.SURVIVED
    return result


def rebuilt(base: CampaignOutcome, **overrides: object) -> dict[str, object]:
    """The fields of a graded outcome with one thing changed, for the normal constructor."""
    return {**dict(base), **overrides}


def test_a_campaign_outcome_with_no_scenarios_describes_no_campaign() -> None:
    """**The core of the finding.** This is the object that made `verdict` caller-declared.

    It went through the ordinary constructor, so no test needed `model_construct` to invent a
    surviving campaign out of nothing.
    """
    with pytest.raises(ValueError, match="no scenario outcomes"):
        CampaignOutcome(**rebuilt(survived_outcome(), scenarios=()))


def test_a_campaign_outcome_cannot_grade_the_same_scenario_twice() -> None:
    base = survived_outcome()
    with pytest.raises(ValueError, match="same scenario twice"):
        CampaignOutcome(**rebuilt(base, scenarios=(base.scenarios[0], base.scenarios[0])))


def test_the_underpowered_count_is_derived_not_declared() -> None:
    with pytest.raises(ValueError, match="underpowered_count is 1"):
        CampaignOutcome(**rebuilt(survived_outcome(), underpowered_count=1))


def test_the_underpowered_fraction_is_derived_not_declared() -> None:
    with pytest.raises(ValueError, match="underpowered_fraction is 0.5"):
        CampaignOutcome(**rebuilt(survived_outcome(), underpowered_fraction=Decimal("0.5")))


def test_a_campaign_cannot_survive_an_unresolved_scenario() -> None:
    """MISLABELED, UNVERIFIED and UNASSESSED are not weak passes. Nothing was established."""
    base = survived_outcome()
    for unresolved in (
        ScenarioVerdict.MISLABELED,
        ScenarioVerdict.UNVERIFIED,
        ScenarioVerdict.UNASSESSED,
    ):
        stalled = base.scenarios[0].model_copy(
            update={"verdict": unresolved, "fresh_evidence": False}
        )
        with pytest.raises(ValueError, match="resolved nothing"):
            CampaignOutcome(**rebuilt(base, scenarios=(stalled, base.scenarios[1])))


def test_integrity_defects_and_a_confident_verdict_cannot_coexist() -> None:
    """`grade_campaign` gives defects precedence as INCONCLUSIVE, so the two imply each other."""
    base = survived_outcome()
    for claimed in (
        CampaignVerdict.SURVIVED,
        CampaignVerdict.REFUTED,
        CampaignVerdict.UNDERPOWERED,
    ):
        with pytest.raises(ValueError, match="unresolved integrity defects"):
            CampaignOutcome(
                **rebuilt(base, verdict=claimed, integrity_defects=("something went wrong",))
            )


def test_an_inconclusive_campaign_has_to_say_what_it_could_not_resolve() -> None:
    with pytest.raises(ValueError, match="could not resolve"):
        CampaignOutcome(**rebuilt(survived_outcome(), verdict=CampaignVerdict.INCONCLUSIVE))


def test_an_underpowered_campaign_needs_an_underpowered_scenario() -> None:
    with pytest.raises(ValueError, match="at least one underpowered scenario"):
        CampaignOutcome(**rebuilt(survived_outcome(), verdict=CampaignVerdict.UNDERPOWERED))


def test_a_refuted_campaign_needs_a_refuted_scenario() -> None:
    with pytest.raises(ValueError, match="at least one refuted scenario"):
        CampaignOutcome(**rebuilt(survived_outcome(), verdict=CampaignVerdict.REFUTED))


def test_a_scenario_that_resolved_nothing_cannot_have_produced_fresh_evidence() -> None:
    """A holdout spent on an unanswered question bought nothing, whatever the window says."""
    base = survived_outcome().scenarios[0]
    with pytest.raises(ValueError, match="cannot have produced"):
        ScenarioOutcome(
            **{
                **dict(base),
                "verdict": ScenarioVerdict.UNASSESSED,
                "fresh_evidence": True,
            }
        )


def test_every_graded_campaign_validates_and_round_trips() -> None:
    """The control. A validator that refused real grader output would be worse than none.

    Each of the four aggregate verdicts is produced by measuring the scenarios differently, and
    each is then re-validated from its own canonical JSON -- the same path #217's `outcome_json`
    takes back out of a manifest.
    """
    subject = campaign()
    stress, general = subject.scenarios
    produced = {
        CampaignVerdict.SURVIVED: graded(subject, measured(stress), measured(general)),
        CampaignVerdict.REFUTED: graded(
            subject, measured(stress, test_statistic=Decimal("0.4")), measured(general)
        ),
        CampaignVerdict.UNDERPOWERED: graded(
            subject, measured(stress, trades=1), measured(general)
        ),
        CampaignVerdict.INCONCLUSIVE: graded(subject, measured(general)),
    }
    for expected, result in produced.items():
        assert result.verdict is expected
        assert CampaignOutcome.model_validate(result.model_dump()) == result


def test_an_optional_scenario_may_fail_without_carrying_the_campaign_down() -> None:
    """**The overreach guard.** "any REFUTED scenario means a REFUTED campaign" is wrong.

    Whether a failing scenario sinks the campaign depends on whether the frozen campaign marked
    it `required`, and a `CampaignOutcome` cannot see that. If the structural validator ever
    starts inferring the aggregate from the scenarios, this is the test that fails.
    """
    subject = campaign(
        scenarios=(
            scenario(),
            scenario(
                kind=ScenarioKind.GENERALIZATION,
                adverse=False,
                required=False,
                period=period("optional", date(2020, 1, 2), date(2021, 12, 31)),
            ),
        )
    )
    stress, optional = subject.scenarios

    result = graded(subject, measured(stress), measured(optional, test_statistic=Decimal("0.4")))

    assert result.verdict is CampaignVerdict.SURVIVED
    assert verdict_of(result, optional) is ScenarioVerdict.REFUTED


def test_an_optional_scenario_may_be_underpowered_within_the_frozen_ceiling() -> None:
    """The same guard for the underpowered rule, which has its own policy input.

    `maximum_underpowered_fraction` is frozen on the campaign and unreachable from the outcome,
    so "any underpowered scenario means an underpowered campaign" would be wrong too.
    """
    subject = campaign(
        scenarios=(
            scenario(),
            scenario(
                kind=ScenarioKind.GENERALIZATION,
                adverse=False,
                required=False,
                period=period("optional", date(2020, 1, 2), date(2021, 12, 31)),
            ),
        )
    )
    stress, optional = subject.scenarios

    result = graded(subject, measured(stress), measured(optional, trades=1))

    assert result.verdict is CampaignVerdict.SURVIVED
    assert result.underpowered_count == 1
    assert result.underpowered_fraction == Decimal("0.5")


# --- #159: SURVIVED on the strength of nothing, and unresolved without a defect ----------------
#
# Two shapes the first structural pass still let through the ordinary constructor. Both follow
# from properties of `grade_campaign` that hold whatever the frozen campaign's required/adverse
# policy is, so neither needs that policy to be reconstructed.


def refuted_outcome() -> CampaignOutcome:
    """A genuinely graded REFUTED campaign: the required stress resolves below the bar."""
    subject = campaign()
    stress, general = subject.scenarios
    result = graded(subject, measured(stress, test_statistic=Decimal("0.4")), measured(general))
    assert result.verdict is CampaignVerdict.REFUTED
    return result


def underpowered_outcome() -> CampaignOutcome:
    """A genuinely graded UNDERPOWERED campaign: the required stress is below the floor."""
    subject = campaign()
    stress, general = subject.scenarios
    result = graded(subject, measured(stress, trades=1), measured(general))
    assert result.verdict is CampaignVerdict.UNDERPOWERED
    return result


def test_a_campaign_cannot_survive_with_no_surviving_scenario() -> None:
    """**Blocker 1.** Every campaign freezes a mandatory adverse scenario.

    `grade_campaign` sends every fate of a required scenario somewhere other than SURVIVED --
    unresolved to INCONCLUSIVE, underpowered to UNDERPOWERED, refuted to REFUTED -- so a campaign
    that survived has a scenario in it that survived. A top-level SURVIVED over nothing but
    failures is a verdict resting on no supporting measurement at all.
    """
    base = refuted_outcome()
    all_refuted = tuple(
        item.model_copy(update={"verdict": ScenarioVerdict.REFUTED}) for item in base.scenarios
    )

    with pytest.raises(ValueError, match="without a single surviving scenario"):
        CampaignOutcome(
            **rebuilt(base, scenarios=all_refuted, verdict=CampaignVerdict.SURVIVED)
        )


def test_a_campaign_cannot_survive_on_underpowered_scenarios_alone() -> None:
    """The same rule where the counts are internally consistent, so nothing else objects.

    Two underpowered scenarios out of two: `underpowered_count` and `underpowered_fraction` are
    both exactly right, and the campaign still cannot have survived them.
    """
    base = underpowered_outcome()
    all_underpowered = tuple(
        item.model_copy(update={"verdict": ScenarioVerdict.UNDERPOWERED, "fresh_evidence": False})
        for item in base.scenarios
    )

    with pytest.raises(ValueError, match="without a single surviving scenario"):
        CampaignOutcome(
            **rebuilt(
                base,
                scenarios=all_underpowered,
                verdict=CampaignVerdict.SURVIVED,
                underpowered_count=2,
                underpowered_fraction=Decimal("1"),
            )
        )


def test_a_refuted_campaign_cannot_hide_an_unresolved_scenario() -> None:
    """**Blocker 2.** An unresolved scenario always becomes an integrity defect.

    The previous rule bound only SURVIVED, which left REFUTED beside an UNASSESSED scenario and
    no defects -- a shape no measurements produce, reading as a campaign that resolved its
    question while a scenario in it answered nothing.
    """
    base = refuted_outcome()
    stalled = base.scenarios[1].model_copy(
        update={"verdict": ScenarioVerdict.UNASSESSED, "fresh_evidence": False}
    )

    with pytest.raises(ValueError, match="reports no integrity defect"):
        CampaignOutcome(**rebuilt(base, scenarios=(base.scenarios[0], stalled)))


def test_an_underpowered_campaign_cannot_hide_an_unresolved_scenario() -> None:
    """The same rule under the other confident aggregate, with counts left consistent."""
    base = underpowered_outcome()
    stalled = base.scenarios[1].model_copy(
        update={"verdict": ScenarioVerdict.UNVERIFIED, "fresh_evidence": False}
    )

    with pytest.raises(ValueError, match="reports no integrity defect"):
        CampaignOutcome(**rebuilt(base, scenarios=(base.scenarios[0], stalled)))


def test_an_unresolved_scenario_round_trips_when_its_defect_is_present() -> None:
    """The control for blocker 2: the honest INCONCLUSIVE shape is untouched.

    `grade_campaign` produced it, so it carries both the unresolved scenario and the defect that
    unresolved scenario raised, and it survives the same canonical round trip #217's
    `outcome_json` takes out of a manifest.
    """
    subject = campaign()
    _stress, general = subject.scenarios
    result = graded(subject, measured(general))

    assert result.verdict is CampaignVerdict.INCONCLUSIVE
    assert result.integrity_defects
    assert any(item.verdict is ScenarioVerdict.UNASSESSED for item in result.scenarios)
    assert CampaignOutcome.model_validate(result.model_dump()) == result
