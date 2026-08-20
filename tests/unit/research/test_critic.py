"""The guardrail layer, tested against the mistakes this project actually made.

Every fixture here is a real recorded failure with its real numbers. A critic that cannot catch
the errors this repository already paid for is not calibrated for it, and a synthetic fixture
would prove only that the code runs.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from quantbot.research.critic import (
    MECHANICAL_CHECKS,
    CriticVerdict,
    Critique,
    DeterministicCritic,
    DeterministicReview,
    Objection,
    Severity,
    consensus,
    cost_ladder,
    cross_asset_generality,
    deterministic_objections,
    disagreements,
    hidden_beta,
    regime_split,
    shifted_feature_probe,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def objection(severity: Severity = Severity.BLOCKING, check: str = "hidden-beta") -> Objection:
    return Objection(
        check=check,
        severity=severity,
        finding="alpha is indistinguishable from zero",
        evidence={"alpha_t": "0.05"},
    )


def critique(**overrides: object) -> Critique:
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-001",
        "hypothesis_version": 1,
        "critic": "deterministic",
        "verdict": CriticVerdict.PROCEED,
        "reasons": ("no mechanical check produced an objection",),
        "produced_at": NOW,
    }
    fields.update(overrides)
    return Critique(**fields)


def test_a_levered_benchmark_shows_no_alpha_however_good_the_raw_return_looks() -> None:
    """Cycle 15: the rotation is beta 0.71 with alpha t=0.05, and 0.71x SPY reproduces it.

    Built as exactly that -- a scaled benchmark plus noise -- so a raw return comparison would
    call it a strategy and the regression cannot.
    """
    generator = random.Random(11)
    benchmark = [generator.gauss(0.0004, 0.011) for _ in range(2669)]
    strategy = [0.71 * value + generator.gauss(0.0, 0.0005) for value in benchmark]

    report = hidden_beta(strategy, benchmark)
    assert 0.70 < report.beta < 0.72
    assert abs(report.alpha_t) < 2.9
    assert report.r_squared > 0.95

    # A genuinely additive edge is separated from the same benchmark.
    with_alpha = [value + 0.0009 for value in strategy]
    assert hidden_beta(with_alpha, benchmark).alpha_t > 2.9


def test_the_strongest_raw_edge_measured_here_dies_on_the_cost_ladder() -> None:
    """Refuted #8: 1-day reversal, +427.9% gross over the sample, -28.8% net at 5bps.

    Both recorded numbers are asserted, and the leg count is the one they imply rather than a
    round figure chosen to make the arithmetic land: 9,134 legs, from
    (4.279 + 0.288) x 10000 / 5.
    """
    legs = 9134.0
    ladder = cost_ladder(gross_return=4.279, trade_legs=legs, ladder=(0.5, 1.1, 5.0, 25.0))
    net_by_cost = {point.cost_bps: point.net_return for point in ladder.points}

    assert net_by_cost[5.0] == pytest.approx(-0.288, abs=0.002)
    assert net_by_cost[0.5] > 0
    assert ladder.break_even_bps == pytest.approx(4.68, abs=0.01)

    # It clears the ~1.1bps actually measured, and dies against the 5bps model the project
    # deployed. That is the whole point of a ladder rather than one number.
    assert ladder.survives(1.1)
    assert not ladder.survives(5.0)

    # A slow signal turning over twice a year survives every rung, including 25bps.
    slow = cost_ladder(gross_return=4.279, trade_legs=21.2)
    assert slow.survives(25.0)
    assert min(point.net_return for point in slow.points) > 0


def test_an_effect_that_holds_in_three_eras_of_four_is_flagged() -> None:
    """Cycle 11: vol targeting as alpha won 2016-18, 2019-21, 2022-24 and lost 2025-26."""
    split = regime_split(
        {"2016-2018": 0.31, "2019-2021": 0.24, "2022-2024": 0.11, "2025-2026": -0.18}
    )
    assert split.wins == 3
    assert not split.consistent
    assert not split.coin_flip  # 3 of 4 is inconsistent, not chance

    even = regime_split({"a": 0.2, "b": -0.1, "c": 0.3, "d": -0.2})
    assert even.coin_flip


def test_a_mechanism_that_works_on_six_of_twelve_assets_is_a_coin_flip() -> None:
    """The same cycle-11 result, on the axis that actually killed it."""
    report = cross_asset_generality(
        {f"asset-{index}": (1.0 if index < 6 else -1.0) for index in range(12)}, threshold=0.0
    )
    assert report.assets == 12
    assert report.clearing == 6
    assert report.indistinguishable_from_chance

    strong = cross_asset_generality(
        {f"asset-{index}": (1.0 if index < 11 else -1.0) for index in range(12)}, threshold=0.0
    )
    assert not strong.indistinguishable_from_chance


def test_a_result_unchanged_by_shifting_every_feature_a_bar_is_reading_the_future() -> None:
    baseline = [0.01, 0.02, -0.01, 0.03]
    assert shifted_feature_probe(baseline, [0.011, 0.019, -0.012, 0.028]) is True
    # Byte-identical output from shifted inputs means the shift never reached the maths.
    assert shifted_feature_probe(baseline, list(baseline)) is False


def test_a_blocking_objection_cannot_be_returned_as_proceed() -> None:
    """The gate is the objection, not the verdict a critic feels like writing."""
    with pytest.raises(ValueError, match="cannot return PROCEED"):
        critique(objections=(objection(),))
    with pytest.raises(ValueError, match="must name what is wrong"):
        critique(verdict=CriticVerdict.REJECT)

    advisory = critique(objections=(objection(severity=Severity.ADVISORY),))
    assert advisory.verdict is CriticVerdict.PROCEED
    assert not advisory.blocking


def test_a_verdict_without_reasons_cannot_be_constructed() -> None:
    """Confidence is advisory; reasons are mandatory. A number without them is a vote."""
    with pytest.raises(ValueError):
        critique(reasons=())
    assert critique(confidence=0.9).confidence == 0.9
    assert critique().confidence is None


def test_agreement_can_never_outvote_an_objection() -> None:
    """Model consensus is not evidence, and the code cannot be configured to treat it as such."""
    approving = [
        critique(critic="model-a"),
        critique(critic="model-b"),
        critique(critic="model-c"),
    ]
    assert consensus(approving) is CriticVerdict.PROCEED

    one_objects = [
        *approving,
        critique(critic="deterministic", verdict=CriticVerdict.REJECT, objections=(objection(),)),
    ]
    assert consensus(one_objects) is CriticVerdict.REJECT
    assert len(disagreements(one_objects)) == 4
    assert disagreements(approving) == ()

    with pytest.raises(ValueError, match="no critiques"):
        consensus([])


def test_the_deterministic_critic_names_what_it_did_not_assess() -> None:
    """Absence of an objection must not read as approval — on either surface.

    The critic leaves two surfaces open: judgment dimensions it cannot assess at all, and
    mechanical checks it was handed no input for. Recording only the first made a critic that
    ran nothing mechanically look fully covered on the mechanical side.
    """
    review = DeterministicReview(objections=(objection(),), assessed=("hidden-beta",))
    blocked = DeterministicCritic(review).review("H-2026-001", 1, now=NOW)
    assert blocked.verdict is CriticVerdict.REVISE
    assert blocked.blocking
    assert blocked.reasons[0].startswith("hidden-beta:")
    assert "economic mechanism plausibility" in blocked.unassessed
    # It ran hidden-beta, so that one is not listed; the other five are.
    assert "hidden-beta" not in blocked.unassessed
    assert "cost-sensitivity" in blocked.unassessed


def test_a_critic_that_ran_no_check_cannot_clear_a_hypothesis() -> None:
    """The defect this replaces: `DeterministicCritic([])` returned PROCEED.

    Its reason was "no mechanical check produced an objection" — true, and true precisely
    because none had been run. `deterministic_objections`'s own docstring says a check that was
    never run must not read as a check that passed; that rule now governs the pass as a whole.
    """
    empty = deterministic_objections(alpha_threshold=2.9)
    assert empty.objections == ()
    assert empty.assessed == ()

    critique = DeterministicCritic(empty).review("H-2026-001", 1, now=NOW)

    assert critique.verdict is not CriticVerdict.PROCEED
    assert critique.verdict is CriticVerdict.REVISE
    assert [item.check for item in critique.objections] == ["no-coverage"]
    assert critique.blocking
    assert "no mechanical check ran" in critique.reasons[0]
    # Every mechanical check is named as unassessed, not silently absent.
    for name in MECHANICAL_CHECKS:
        assert name in critique.unassessed


def test_the_mechanical_checks_compose_into_objections_with_their_numbers() -> None:
    generator = random.Random(3)
    benchmark = [generator.gauss(0.0004, 0.011) for _ in range(2669)]
    levered = [0.71 * value for value in benchmark]

    review = deterministic_objections(
        beta=hidden_beta(levered, benchmark),
        ladder=cost_ladder(gross_return=0.05, trade_legs=9134.0),
        split=regime_split({"a": 0.2, "b": -0.1, "c": 0.3, "d": -0.2}),
        generality=cross_asset_generality(
            {f"a{i}": (1.0 if i < 6 else -1.0) for i in range(12)}, threshold=0.0
        ),
        shifted_probe_passed=False,
        survivorship_bias=0.0,
        alpha_threshold=2.9,
    )
    objections = review.objections
    # Every check that was fed an input is recorded as assessed, whether or not it objected.
    assert set(review.assessed) == set(MECHANICAL_CHECKS)
    assert review.unassessed == ()
    checks = {item.check for item in objections}
    assert checks == {
        "hidden-beta",
        "cost-sensitivity",
        "regime-split",
        "cross-asset-generality",
        "shifted-feature-probe",
        "survivorship",
    }
    # Every objection carries the measurement behind it, not an adjective.
    assert all(item.evidence for item in objections)
    assert any(item.severity is Severity.ADVISORY for item in objections)


def test_a_check_that_was_never_run_produces_no_objection_and_no_reassurance() -> None:
    """Supplying one input assesses one check and leaves the rest explicitly open.

    This test previously asserted only that no objection was produced, and passed while the
    critic returned PROCEED — which is the strongest reassurance available. Asserting the
    absence of an objection while ignoring the verdict is the failure class `CLAUDE.md` names:
    it could not fail for the reason it was named after.
    """
    review = deterministic_objections(alpha_threshold=2.9, survivorship_bias=0.4)
    assert review.objections == ()
    assert review.assessed == ("survivorship",)

    critique = DeterministicCritic(review).review("H", 1, now=NOW)

    assert critique.verdict is CriticVerdict.PROCEED, "one clean check may clear"
    assert "survivorship" not in critique.unassessed
    assert "hidden-beta" in critique.unassessed, "the five unrun checks stay named"
    assert "1 of 6 mechanical checks" in critique.reasons[0]
