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
    DataRole,
    DataWindow,
    HypothesisDraft,
    HypothesisRegistry,
    RefusalReason,
    RegistrationRefused,
    luck_threshold,
    minimum_detectable_sharpe,
    required_sessions,
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
        "horizon_sessions": 1,
        "windows": (window(DataRole.PROTECTED_EVALUATION, "2016-01-04", "2026-08-18"),),
        "primary_estimand": "annualised Sharpe difference against SPY buy-and-hold",
        "expected_sharpe": Decimal("1.0"),
        "minimum_practical_sharpe": Decimal("0.5"),
        "effect_justification": "Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
        "available_sessions": SIP_SESSIONS,
        "comparison_structure": "PAIRED",
        "costs": {"slippage_bps": "1.1", "commission_per_order": "0"},
        "confounders": ("regime dependence", "the 2020 crash dominating the sample"),
        "proposed_by": "claude-opus-5",
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def test_luck_bar_and_power_reproduce_the_figures_already_recorded_in_refuted_md() -> None:
    # Cycles 15-17 quote these bars; the registry must not quietly move them.
    assert str(luck_threshold(62)) == "2.873024"
    assert str(luck_threshold(68)) == "2.904998"

    # Claude's review table on issue #5: years of daily data needed at t=2.9.
    years = {
        sharpe: required_sessions(Decimal(sharpe), Decimal("2.9"), 252) / 252
        for sharpe in ("0.30", "0.50", "0.80", "1.00")
    }
    assert round(years["0.30"]) == 93
    assert round(years["0.50"]) == 34
    assert round(years["0.80"]) == 13
    assert round(years["1.00"], 1) == 8.4

    # The whole SIP history cannot separate a Sharpe below ~0.9 from zero.
    assert minimum_detectable_sharpe(SIP_SESSIONS, Decimal("2.9"), 252) > Decimal("0.89")


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
            make_draft(expected_sharpe=Decimal("1.0")), now=NOW
        )
    assert strong.required_sessions <= SIP_SESSIONS

    # A different dataset, so nothing this project has touched is involved and the
    # contamination gate has nothing to say about it.
    weak = make_draft(
        hypothesis_id="H-2026-002",
        expected_sharpe=Decimal("0.5"),
        minimum_practical_sharpe=Decimal("0.5"),
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
    assert second.luck_threshold_z > first.luck_threshold_z


def test_execution_recomputes_power_against_trials_accumulated_since_registration(
    database: Database,
) -> None:
    """The registered numbers are not trusted at execution time; the bar moves under them."""
    registrable = make_draft(expected_sharpe=Decimal("1.0"), available_sessions=2200)
    with database.transaction() as session:
        registered = HypothesisRegistry(session).register(registrable, now=NOW)
    assert registered.required_sessions <= 2200

    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution("H-2026-001", 1)
    assert clearance.registration_hash == registered.content_hash
    assert clearance.current_required_sessions <= clearance.available_sessions

    # A later, unrelated search raises the luck bar for everything that follows it.
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            make_draft(
                hypothesis_id="H-2026-002",
                expected_sharpe=Decimal("3.0"),
                search_cardinality=10,
                windows=(window(DataRole.FORWARD_PAPER, "2026-08-19", "2027-08-19"),),
            ),
            now=NOW,
        )

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1)
    assert error.value.reason is RefusalReason.UNDERPOWERED
    assert str(registered.required_sessions) in error.value.detail


def test_execution_refuses_a_hypothesis_that_was_never_registered(database: Database) -> None:
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-DOES-NOT-EXIST", 1)
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
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1)
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
