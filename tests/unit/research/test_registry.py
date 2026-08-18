"""What the hypothesis registry must refuse, and why each refusal is the named one.

Every gate here is asserted against its specific `RefusalReason`, with the unrelated gates set
generously so they cannot cover for it. A test that accepted "some refusal happened" would pass
just as happily with the gate deleted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, update

from quantbot.research import (
    PRIOR_TRIALS,
    ComparisonStructure,
    DataRole,
    DataWindow,
    DependenceAssumptions,
    EconomicProfile,
    EffectSpecification,
    Estimand,
    HypothesisDraft,
    HypothesisRegistry,
    PowerOverride,
    PowerVerdict,
    RefusalReason,
    RegistrationRefused,
    Sampling,
    summarize,
    unresolved_questions,
)
from quantbot.storage import Database
from quantbot.storage.schema import hypotheses

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)

#: 2016-01-04 to 2026-08 of SIP daily data, the whole history this project has.
SIP_SESSIONS = 2669


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "research.db")
    yield db
    db.close()


def window(
    role: DataRole,
    start: str,
    end: str,
    *,
    dataset: str = "sip-us-equities-daily",
) -> DataWindow:
    return DataWindow(
        dataset=dataset,
        snapshot="2026-08-18",
        role=role,
        start=date.fromisoformat(start),
        end=date.fromisoformat(end),
    )


def sharpe_effect(**overrides: object) -> EffectSpecification:
    expected = overrides.pop("expected", Decimal("1.0"))
    assert isinstance(expected, Decimal)
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": expected,
        "minimum_practical": expected / 2,
        "justification": "Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
        "comparison": ComparisonStructure.PAIRED,
        "economics": EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def make_draft(**overrides: object) -> HypothesisDraft:
    """A registrable draft: powered, uncontaminated, and falsifiable unless overridden."""
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-001",
        "family_id": "trend-following",
        "question": "Does a 200-day trend filter on SPY beat buy-and-hold on Sharpe?",
        "prediction": "Sharpe is higher by at least 0.10 with lower maximum drawdown.",
        "null_hypothesis": "The Sharpe difference is zero.",
        "falsified_if": "The paired Sharpe difference fails to clear the luck bar.",
        "universe": ("SPY",),
        "features": ("close_sma_200",),
        "target": "next-session excess return",
        "windows": (window(DataRole.PROTECTED_EVALUATION, "2016-01-04", "2026-08-18"),),
        "primary_estimand": "annualised Sharpe difference against SPY buy-and-hold",
        "effect": sharpe_effect(),
        "available_observations": SIP_SESSIONS,
        "confounders": ("regime dependence", "the 2020 crash dominating the sample"),
        "proposed_by": "claude-opus-5",
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def test_registration_freezes_the_prediction_under_a_content_hash(database: Database) -> None:
    with database.transaction() as session:
        registration = HypothesisRegistry(session).register(make_draft(), now=NOW)
    frozen = registration.content_hash

    with database.transaction() as session:
        reloaded = HypothesisRegistry(session).get("H-2026-001", 1)
    assert reloaded is not None
    assert reloaded.content_hash == frozen
    assert reloaded.draft.falsified_if == registration.draft.falsified_if


def test_a_material_change_cannot_overwrite_a_registration(database: Database) -> None:
    with database.transaction() as session:
        HypothesisRegistry(session).register(make_draft(), now=NOW)

    restated = make_draft(prediction="An interior optimum exists.")
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(restated, now=NOW)
    assert error.value.reason is RefusalReason.ALREADY_REGISTERED

    with database.transaction() as session:
        stored = HypothesisRegistry(session).get("H-2026-001", 1)
    assert stored is not None
    assert stored.draft.prediction != restated.prediction


def test_a_protected_window_another_registration_consumed_is_refused(
    database: Database,
) -> None:
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            make_draft(
                windows=(window(DataRole.VALIDATION, "2016-01-04", "2024-12-31"),),
            ),
            now=NOW,
        )

    # Same dataset, overlapping range, now claimed as an untouched holdout.
    later = make_draft(
        hypothesis_id="H-2026-002",
        windows=(window(DataRole.PROTECTED_EVALUATION, "2020-01-02", "2026-08-18"),),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(later, now=NOW)

    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "H-2026-001" in error.value.detail
    assert "VALIDATION" in error.value.detail


def test_a_protected_window_overlapping_its_own_discovery_range_is_refused(
    database: Database,
) -> None:
    leaky = make_draft(
        windows=(
            window(DataRole.DISCOVERY, "2016-01-04", "2022-12-30"),
            window(DataRole.PROTECTED_EVALUATION, "2022-01-03", "2026-08-18"),
        )
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(leaky, now=NOW)

    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "its own DISCOVERY window" in error.value.detail


def test_a_disjoint_protected_window_on_a_consumed_dataset_still_registers(
    database: Database,
) -> None:
    """The block is overlap, not the dataset. Otherwise one registration retires the data."""
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            make_draft(windows=(window(DataRole.VALIDATION, "2016-01-04", "2021-12-31"),)),
            now=NOW,
        )
    with database.transaction() as session:
        registration = HypothesisRegistry(session).register(
            make_draft(
                hypothesis_id="H-2026-002",
                windows=(window(DataRole.PROTECTED_EVALUATION, "2022-01-03", "2026-08-18"),),
            ),
            now=NOW,
        )
    assert registration.draft.hypothesis_id == "H-2026-002"


def test_forward_paper_windows_may_overlap_because_the_account_runs_once(
    database: Database,
) -> None:
    """Two hypotheses watching the same forward period is multiple testing, not contamination.

    It is carried by the trial count instead. Blocking it would make the paper account -- the
    only uncontaminated data this project will ever get -- single-use.
    """
    forward = (window(DataRole.FORWARD_PAPER, "2026-08-19", "2027-08-19", dataset="paper"),)
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.register(make_draft(windows=forward), now=NOW)
        second = registry.register(
            make_draft(hypothesis_id="H-2026-002", windows=forward), now=NOW
        )
    assert second.cumulative_trials > PRIOR_TRIALS + 1


def test_an_effect_too_small_for_the_available_data_is_refused_as_underpowered(
    database: Database,
) -> None:
    """Only the power gate can fire here: the window is virgin and nothing else is registered.

    The same draft with a larger claimed effect registers against the identical sample, so a
    pass proves the arithmetic ran rather than that some other check stayed quiet.
    """
    with database.transaction() as session:
        strong = HypothesisRegistry(session).register(
            make_draft(effect=sharpe_effect(expected=Decimal("1.0"))), now=NOW
        )
    assert strong.power.observations_required <= SIP_SESSIONS

    # A different dataset, so nothing this project has touched is involved and the
    # contamination gate has nothing to say about it.
    weak = make_draft(
        hypothesis_id="H-2026-002",
        effect=sharpe_effect(expected=Decimal("0.5")),
        windows=(
            window(
                DataRole.PROTECTED_EVALUATION,
                "2016-01-04",
                "2026-08-18",
                dataset="fred-macro-daily",
            ),
        ),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(weak, now=NOW)

    assert error.value.reason is RefusalReason.UNDERPOWERED
    assert "2669 are available" in error.value.detail


def test_the_trial_count_is_the_project_history_not_this_cycle(database: Database) -> None:
    with database.transaction() as session:
        first = HypothesisRegistry(session).register(make_draft(search_cardinality=12), now=NOW)
    assert first.cumulative_trials == PRIOR_TRIALS + 12

    with database.transaction() as session:
        second = HypothesisRegistry(session).register(
            make_draft(
                hypothesis_id="H-2026-002",
                search_cardinality=5,
                windows=(window(DataRole.FORWARD_PAPER, "2026-08-19", "2027-08-19"),),
            ),
            now=NOW,
        )
    # 68 already spent by cycles 1-17, plus 12 mined for the first, plus 5 for this one.
    assert second.cumulative_trials == PRIOR_TRIALS + 12 + 5
    assert second.power.luck_threshold_z > first.power.luck_threshold_z


def test_execution_recomputes_power_against_trials_accumulated_since_registration(
    database: Database,
) -> None:
    """The registered numbers are not trusted at execution time; the bar moves under them."""
    registrable = make_draft(
        effect=sharpe_effect(expected=Decimal("1.0")), available_observations=2200
    )
    with database.transaction() as session:
        registered = HypothesisRegistry(session).register(registrable, now=NOW)
    assert registered.power.observations_required <= 2200

    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)
    assert clearance.registration_hash == registered.content_hash
    assert clearance.power.observations_required <= clearance.power.observations_available

    # A later, unrelated search raises the luck bar for everything that follows it.
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            make_draft(
                hypothesis_id="H-2026-002",
                effect=sharpe_effect(expected=Decimal("3.0")),
                search_cardinality=10,
                windows=(window(DataRole.FORWARD_PAPER, "2026-08-19", "2027-08-19"),),
            ),
            now=NOW,
        )

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)
    assert error.value.reason is RefusalReason.UNDERPOWERED
    assert str(registered.power.observations_required) in error.value.detail


def test_execution_refuses_a_hypothesis_that_was_never_registered(database: Database) -> None:
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-DOES-NOT-EXIST", 1, now=NOW)
    assert error.value.reason is RefusalReason.NOT_REGISTERED


def test_editing_the_stored_registration_is_detected_at_execution(database: Database) -> None:
    with database.transaction() as session:
        HypothesisRegistry(session).register(make_draft(), now=NOW)

    with database.transaction() as session:
        stored = session.execute(select(hypotheses.c.document_json)).scalar_one()
        document = json.loads(str(stored))
        document["draft"]["falsified_if"] = "Nothing could falsify this."
        session.execute(update(hypotheses).values(document_json=json.dumps(document)))

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)
    assert error.value.reason is RefusalReason.TAMPERED


def test_a_registration_without_a_falsification_criterion_is_rejected() -> None:
    with pytest.raises(ValueError, match="falsified_if"):
        make_draft(falsified_if="   ")


def test_a_draft_cannot_split_one_dataset_role_across_ranges() -> None:
    with pytest.raises(ValueError, match="one contiguous window"):
        make_draft(
            windows=(
                window(DataRole.DISCOVERY, "2016-01-04", "2018-12-31"),
                window(DataRole.DISCOVERY, "2020-01-02", "2021-12-31"),
            )
        )


def test_an_underpowered_refusal_survives_as_research_memory(database: Database) -> None:
    """The distinction #19 exists to protect: declined to test is not tested and failed.

    The transaction that refused has already rolled back, so the refusal reaches durable state
    only because it travels out on the exception for the caller to record.
    """
    weak = make_draft(effect=sharpe_effect(expected=Decimal("0.3")))
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(weak, now=NOW)
    refusal = error.value.assessment
    assert refusal is not None
    assert refusal.verdict is PowerVerdict.UNDERPOWERED

    with database.transaction() as session:
        HypothesisRegistry(session).record_assessment(refusal)

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        assert registry.get("H-2026-001", 1) is None
        recorded = registry.list_assessments("H-2026-001")

    assert [entry.verdict for entry in recorded] == [PowerVerdict.UNDERPOWERED]
    assert unresolved_questions(recorded) == recorded
    assert recorded[0].shortfall_observations > 0


def test_an_operator_override_registers_and_stays_labelled_underpowered(
    database: Database,
) -> None:
    weak = make_draft(effect=sharpe_effect(expected=Decimal("0.3")))
    override = PowerOverride(authorized_by="hutch", reason="precursor to a forward-paper test")

    with database.transaction() as session:
        registration = HypothesisRegistry(session).register(weak, now=NOW, override=override)
    assert registration.power.verdict is PowerVerdict.OVERRIDDEN

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        stored = registry.get("H-2026-001", 1)
        assessments = registry.list_assessments("H-2026-001")
        verdict_column = session.execute(select(hypotheses.c.power_verdict)).scalar_one()

    assert stored is not None
    assert stored.power.verdict is PowerVerdict.OVERRIDDEN
    assert stored.power.override == override
    assert verdict_column == "OVERRIDDEN"
    assert summarize(stored)["overridden_by"] == "hutch"
    assert [entry.override for entry in assessments] == [override]


def test_an_override_carries_into_execution_rather_than_expiring(database: Database) -> None:
    """The operator accepted the shortfall, not one particular sample count."""
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            make_draft(effect=sharpe_effect(expected=Decimal("0.3"))),
            now=NOW,
            override=PowerOverride(authorized_by="hutch", reason="accepted"),
        )
    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)
    assert clearance.power.verdict is PowerVerdict.OVERRIDDEN
    assert clearance.power.stage == "EXECUTION"


def test_an_edge_that_cannot_pay_its_costs_is_refused_as_uneconomic(database: Database) -> None:
    """Distinct from underpowered: the sample is enormous, so only the cost floor can fire."""
    churn = make_draft(
        available_observations=1_000_000,
        effect=sharpe_effect(
            expected=Decimal("2.0"),
            minimum_practical=Decimal("0.2"),
            economics=EconomicProfile(
                annual_rebalances=252,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("5"),
            ),
        ),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(churn, now=NOW)

    assert error.value.reason is RefusalReason.UNECONOMIC
    assert error.value.assessment is not None
    assert error.value.assessment.verdict is PowerVerdict.UNECONOMIC
    assert "1260" in error.value.detail


def test_every_execution_check_is_recorded_not_only_the_registration(
    database: Database,
) -> None:
    with database.transaction() as session:
        HypothesisRegistry(session).register(make_draft(), now=NOW)
    with database.transaction() as session:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)

    with database.transaction() as session:
        stages = [
            entry.stage for entry in HypothesisRegistry(session).list_assessments("H-2026-001")
        ]
    assert stages == ["REGISTRATION", "EXECUTION"]


def test_a_registration_records_the_dependence_assumptions_behind_its_power_number(
    database: Database,
) -> None:
    """A minimum detectable effect quoted without its assumptions is false precision."""
    overlapping = make_draft(
        # Deliberately generous, so the power gate cannot fire and the assumptions themselves
        # are what is under test.
        available_observations=20_000,
        effect=sharpe_effect(
            expected=Decimal("2.0"),
            dependence=DependenceAssumptions(
                sampling=Sampling.OVERLAPPING, horizon_observations=21
            ),
        )
    )
    with database.transaction() as session:
        registration = HypothesisRegistry(session).register(overlapping, now=NOW)

    assert registration.power.variance_inflation == Decimal("21")
    assert registration.power.dependence.horizon_observations == 21
    assert summarize(registration)["variance_inflation"] == "21.000000"
