"""Did the gate pay for itself, and is the sample big enough to say (#57 phase 2).

The failure this guards against is not a wrong number, it is a number computed over the wrong
set. Pooling across a threshold change judges a rule on another rule's evidence; pooling across
execution policies compares different assumed fills; counting a macro day's refusals as
independent inflates the sample. All three are silent, and all three flatter or damn a gate for
reasons that have nothing to do with it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.anomaly.routing import MINIMUM_EFFECTIVE_OBSERVATIONS
from quantbot.risk.counterfactual import (
    CounterfactualOutcome,
    HypotheticalExecutionPolicy,
    UnevaluableReason,
)
from quantbot.risk.gate_value import (
    GateOutcomeSample,
    GateValueReport,
    GateVerdict,
    group_keys,
    report_all,
    report_gate_value,
)
from quantbot.risk.shadow import CounterfactualState
from quantbot.storage import Database, StorageRepository

START = date(2026, 3, 2)
HASH = "veto-policy-v1-aaaa"
POLICY = "counterfactual-policy-v1-bbbb"


def sample(
    index: int,
    *,
    gate: str = "LIQUIDITY",
    version: int = 1,
    parameters: str = HASH,
    policy: str = POLICY,
    day: int | None = None,
    group: str | None = None,
    excess: str = "-50",
    observation: str | None = None,
) -> GateOutcomeSample:
    return GateOutcomeSample(
        observation_id=observation or f"obs-{index}",
        gate_id=gate,
        gate_version=version,
        rule_parameters_hash=parameters,
        policy_identity=policy,
        session_date=START + timedelta(days=index if day is None else day),
        group_key=group or f"cluster-{index}",
        net_excess_bps=Decimal(excess),
    )


def spread(count: int, *, excess: str = "-50", **overrides: object) -> list[GateOutcomeSample]:
    """One refusal per session, so nothing is discounted for co-movement."""
    return [sample(index, excess=excess, **overrides) for index in range(count)]  # type: ignore[arg-type]


def report(samples: list[GateOutcomeSample], **overrides: object) -> GateValueReport:
    fields: dict[str, object] = {
        "gate_id": "LIQUIDITY",
        "gate_version": 1,
        "rule_parameters_hash": HASH,
        "policy_identity": POLICY,
    }
    fields.update(overrides)
    return report_gate_value(samples, **fields)  # type: ignore[arg-type]


# --- grouping -------------------------------------------------------------------------------------


def test_a_threshold_change_is_a_different_dataset() -> None:
    """Pooling across it judges a rule on evidence gathered under a different rule."""
    old_rule = spread(40, excess="-100")
    new_rule = spread(40, excess="+100", parameters="veto-policy-v1-zzzz")

    combined = report(old_rule + new_rule)
    assert combined.candidates == 40, "only the named parameter hash may contribute"
    assert combined.mean_net_excess_bps == Decimal("-100.00")

    other = report(old_rule + new_rule, rule_parameters_hash="veto-policy-v1-zzzz")
    assert other.mean_net_excess_bps == Decimal("100.00")
    assert other.verdict is not combined.verdict


def test_a_different_execution_policy_is_a_different_dataset() -> None:
    """Two numbers computed under different assumed fills are not comparable.

    The two groups carry **distinct observation ids** on purpose. An earlier version reused the
    same ids, so deduplication collapsed them and the candidate count was 40 whether or not the
    policy filter existed -- the test passed while the protection was absent, which is exactly
    the failure mode this repository keeps finding.
    """
    cheap = spread(40, excess="+20")
    dear = [
        sample(
            index,
            excess="-20",
            policy="counterfactual-policy-v1-zzzz",
            observation=f"other-{index}",
        )
        for index in range(40)
    ]

    first = report(cheap + dear)
    second = report(cheap + dear, policy_identity="counterfactual-policy-v1-zzzz")

    assert first.candidates == 40 and second.candidates == 40
    assert first.mean_net_excess_bps == Decimal("20.00")
    assert second.mean_net_excess_bps == Decimal("-20.00")
    assert len(group_keys(cheap + dear)) == 2


def test_every_distinct_group_gets_its_own_report_and_never_one_pooled_answer() -> None:
    """A single blended verdict is the shape of the mistake this module exists to prevent."""
    mixed = spread(40) + spread(40, gate="DAILY_LOSS") + spread(40, version=2)

    reports = report_all(mixed)

    assert len(reports) == 3
    assert {key[0] for key in reports} == {"LIQUIDITY", "DAILY_LOSS"}
    assert {key[1] for key in reports} == {1, 2}


def test_one_candidate_refused_twice_is_still_one_sample() -> None:
    """Counting reasons as observations inflates the sample, which killed cycle 11's result."""
    duplicated = [
        sample(0, observation="obs-shared"),
        sample(1, observation="obs-shared"),
        *spread(35)[2:],
    ]

    assert report(duplicated).candidates == len({s.observation_id for s in duplicated})


# --- dependence-aware sample size -----------------------------------------------------------------


def test_a_macro_day_of_refusals_is_not_many_independent_observations() -> None:
    """The whole roster refused on one day is one underlying reason, not forty.

    #56's routing gate measured what ignoring this costs: a 1.67x overstatement on real data.
    The estimator is reused rather than reimplemented, because two notions of effective sample
    size in one repository eventually disagree -- and the flattering one survives review.
    """
    # Each day moves its whole roster together by a different amount. Identical values across
    # every session would give the estimator no between-cluster variance to detect -- correct
    # behaviour, and not what a macro day looks like.
    macro = [
        sample(
            index,
            day=index // 20,
            group=f"cluster-{index}",
            excess=("-150", "-20", "-90")[index // 20],
        )
        for index in range(60)
    ]

    result = report(macro)

    assert result.candidates == 60
    assert result.effective_observations < 60
    assert result.design_effect > Decimal("1")
    assert result.intraclass_correlation > Decimal("0")


def test_refusals_spread_across_sessions_are_barely_discounted() -> None:
    """A discount that always fires would understate every gate."""
    result = report(spread(40))

    assert result.design_effect == Decimal("1")
    assert result.effective_observations == 40


# --- what it may say ------------------------------------------------------------------------


def test_below_the_floor_no_point_estimate_is_reported_at_all() -> None:
    """Suppressed rather than caveated.

    A number with a warning beside it is still a number somebody will tune a threshold against,
    and this dataset makes threshold tuning from noise unusually easy.
    """
    result = report(spread(5))

    assert result.verdict is GateVerdict.UNDERPOWERED
    assert result.mean_net_excess_bps is None
    assert str(MINIMUM_EFFECTIVE_OBSERVATIONS) in result.detail


def test_an_underpowered_report_cannot_be_constructed_with_a_number() -> None:
    """Structural, because the suppression is the protection."""
    with pytest.raises(ValueError, match="tune a threshold against"):
        GateValueReport(
            gate_id="LIQUIDITY",
            gate_version=1,
            rule_parameters_hash=HASH,
            policy_identity=POLICY,
            candidates=5,
            effective_observations=5,
            intraclass_correlation=Decimal("0"),
            design_effect=Decimal("1"),
            verdict=GateVerdict.UNDERPOWERED,
            mean_net_excess_bps=Decimal("-50"),
            detail="underpowered, with a number anyway",
        )


def test_a_gate_with_nothing_evaluated_says_so_rather_than_reporting_zero() -> None:
    """An unevaluated gate and a gate that broke even are different facts."""
    result = report([])

    assert result.verdict is GateVerdict.NO_EVALUATED_OUTCOMES
    assert result.mean_net_excess_bps is None
    assert result.candidates == 0


def test_the_verdict_distinguishes_a_gate_that_paid_from_one_that_cost() -> None:
    """A control that never costs anything is not binding, so both directions are findings."""
    paid = report(spread(40, excess="-120"))
    cost = report(spread(40, excess="+120"))

    assert paid.verdict is GateVerdict.REFUSALS_AVOIDED_LOSSES
    assert paid.mean_net_excess_bps == Decimal("-120.00")
    assert cost.verdict is GateVerdict.REFUSALS_FORWENT_GAINS
    assert cost.mean_net_excess_bps == Decimal("120.00")


# --- persistence ----------------------------------------------------------------------------


def outcome(**overrides: object) -> CounterfactualOutcome:
    fields: dict[str, object] = {
        "observation_id": "obs-1",
        "policy_identity": POLICY,
        "state": CounterfactualState.UNAVAILABLE,
        "detail": "the horizon has not elapsed",
    }
    fields.update(overrides)
    return CounterfactualOutcome(**fields)  # type: ignore[arg-type]


def seeded(tmp_path: Path) -> Database:
    """A store holding one refusal, so an outcome has something to reference."""
    from quantbot.risk.shadow import CandidateDirection, RejectedCandidate, RejectionReason, as_rows

    database = Database.create(tmp_path / "cf.db")
    refusal = RejectedCandidate(
        observation_id="obs-1",
        candidate_id="cand-1",
        strategy_id="adaptive-momentum",
        configuration_hash="cfg",
        symbol="SPY",
        direction=CandidateDirection.LONG,
        observed_at=datetime(2026, 3, 2, 20, 5, tzinfo=UTC),
        session_date=START,
        reference_price=Decimal("500"),
        reasons=(
            RejectionReason(
                gate_id="LIQUIDITY",
                gate_version=1,
                rule_parameters_hash=HASH,
                detail="refused",
            ),
        ),
    )
    parent, reasons = as_rows(refusal)
    with database.transaction() as session:
        StorageRepository(session).record_rejected_candidate(parent, reasons)
    return database


def test_an_outcome_persists_and_never_amends_the_refusal(tmp_path: Path) -> None:
    """Append-only: the refusal keeps saying what was known when it was made."""
    database = seeded(tmp_path)
    row = outcome().as_row()

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_counterfactual_outcome(row) is True
        assert repository.record_counterfactual_outcome(row) is False
        stored = repository.list_counterfactual_outcomes(observation_id="obs-1")
        refusals = repository.list_rejected_candidates()
    database.close()

    assert len(stored) == 1
    assert stored[0]["state"] == "COUNTERFACTUAL_UNAVAILABLE"
    assert stored[0]["net_excess_bps"] is None
    assert len(refusals) == 1, "the refusal is untouched by its outcome"


def test_one_policy_has_one_answer_per_refusal(tmp_path: Path) -> None:
    """Enforced by the store, so a re-run cannot overwrite a measurement with a different one."""
    database = seeded(tmp_path)
    first = outcome()
    contradiction = outcome(detail="a different answer under the same policy")
    assert first.identity != contradiction.identity

    with database.transaction() as session:
        StorageRepository(session).record_counterfactual_outcome(first.as_row())

    with pytest.raises(Exception, match="one_outcome_per_refusal_and_policy|UNIQUE"):
        with database.transaction() as session:
            StorageRepository(session).record_counterfactual_outcome(contradiction.as_row())
    database.close()


def test_a_second_policy_adds_an_answer_rather_than_replacing_one(tmp_path: Path) -> None:
    """Two execution assumptions are two datasets, and both survive."""
    database = seeded(tmp_path)
    other = HypotheticalExecutionPolicy(horizon_sessions=5)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_counterfactual_outcome(outcome().as_row())
        assert (
            repository.record_counterfactual_outcome(
                outcome(policy_identity=other.identity).as_row()
            )
            is True
        )
        stored = repository.list_counterfactual_outcomes(observation_id="obs-1")
    database.close()

    assert len(stored) == 2
    assert {row["policy_identity"] for row in stored} == {POLICY, other.identity}


def test_a_terminal_outcome_keeps_its_reason_through_storage(tmp_path: Path) -> None:
    """A reason lost in storage makes never indistinguishable from not yet."""
    database = seeded(tmp_path)
    terminal = outcome(
        state=CounterfactualState.UNEVALUABLE,
        horizon_session=date(2026, 4, 1),
        evaluated_at=datetime(2026, 4, 2, tzinfo=UTC),
        unevaluable_reason=UnevaluableReason.HORIZON_PRICE_MISSING,
        detail="no price at the horizon session",
    )

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_counterfactual_outcome(terminal.as_row())
        stored = repository.list_counterfactual_outcomes(observation_id="obs-1")
    database.close()

    assert stored[0]["unevaluable_reason"] == "HORIZON_PRICE_MISSING"
    assert stored[0]["net_excess_bps"] is None
