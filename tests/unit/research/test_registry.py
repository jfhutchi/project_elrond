"""What the hypothesis registry must refuse, and why each refusal is the named one.

Every gate here is asserted against its specific `RefusalReason`, with the unrelated gates set
generously so they cannot cover for it. A test that accepted "some refusal happened" would pass
just as happily with the gate deleted.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.engine import RowMapping

from quantbot.research import (
    EXHAUSTED_TRIALS,
    PRIOR_TRIALS,
    RESERVATION_DAYS,
    Citation,
    ComparisonStructure,
    CriticVerdict,
    Critique,
    DataRole,
    DataWindow,
    DependenceAssumptions,
    EconomicProfile,
    EffectSpecification,
    EpistemicStatus,
    Estimand,
    EvidenceBasis,
    HypothesisDraft,
    HypothesisRegistry,
    Objection,
    ObservedSample,
    PowerOverride,
    PowerVerdict,
    RefusalReason,
    Registration,
    RegistrationRefused,
    Sampling,
    SearchOrigin,
    Severity,
    WindowState,
    gates_promotion,
    summarize,
    unresolved_questions,
)
from quantbot.storage import Database
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import hypotheses, hypothesis_data_windows, search_runs

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)

#: The day a reservation taken at `NOW` still blocks, and the first day it does not (#22).
LAPSES = NOW.date() + timedelta(days=RESERVATION_DAYS)
AFTER_LAPSE = datetime.combine(LAPSES + timedelta(days=1), NOW.timetz())

#: 2016-01-04 to 2026-08 of SIP daily data, the whole history this project has.
SIP_SESSIONS = 2669


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "research.db")
    yield db
    db.close()


def cleared(hypothesis_id: str = "H-2026-001", version: int = 1) -> Critique:
    """A critique raising no objection, so tests of other gates are not about this one."""
    return Critique(
        hypothesis_id=hypothesis_id,
        hypothesis_version=version,
        critic="deterministic",
        verdict=CriticVerdict.PROCEED,
        reasons=("no mechanical check produced an objection",),
        produced_at=NOW,
    )


def register(
    registry: HypothesisRegistry, candidate: HypothesisDraft, **kwargs: object
) -> Registration:
    """Register with a clean critique unless the test is about the critic gate itself."""
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("critique", cleared(candidate.hypothesis_id, candidate.version))
    return registry.register(candidate, **kwargs)  # type: ignore[arg-type]


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
    """A standalone Sharpe against a null of zero.

    This used to declare `comparison=PAIRED` while feeding an *absolute* Sharpe of 1.0, which is
    the mismatch reported in #25: the compiler selected Jobson-Korkie-Memmel for the pair while
    the gate sized a one-sample test. `SINGLE_SAMPLE` is what the arithmetic was always doing.
    Comparative registrations use `sharpe_difference_effect` and must declare a benchmark.
    """
    expected = overrides.pop("expected", Decimal("1.0"))
    assert isinstance(expected, Decimal)
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": expected,
        "minimum_practical": expected / 2,
        "justification": "Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
        "comparison": ComparisonStructure.SINGLE_SAMPLE,
        "economics": EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def sharpe_difference_effect(**overrides: object) -> EffectSpecification:
    """A Sharpe *difference* against a correlated benchmark, sized by Jobson-Korkie-Memmel."""
    expected = overrides.pop("expected", Decimal("0.55"))
    assert isinstance(expected, Decimal)
    fields: dict[str, object] = {
        "estimand": Estimand.SHARPE,
        "expected": expected,
        "minimum_practical": expected / 2,
        "justification": "A trend filter should improve risk-adjusted return over buy-and-hold.",
        "comparison": ComparisonStructure.PAIRED,
        "benchmark_sharpe": Decimal("0.90"),
        "benchmark_correlation": Decimal("0.95"),
        "economics": EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    }
    fields.update(overrides)
    return EffectSpecification(**fields)


def counted(observations: int, *, dataset: str = "sip-us-equities-daily") -> ObservedSample:
    """A verified count, as the experiment builder would supply it at execution."""
    return ObservedSample(
        observations=observations,
        dataset=dataset,
        start_date=date(2016, 1, 4),
        end_date=date(2026, 8, 18),
        method="XNYS sessions with SPY bars present",
        counted_at=NOW,
    )


def make_draft(**overrides: object) -> HypothesisDraft:
    """A registrable draft: powered, uncontaminated, and falsifiable unless overridden."""
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-001",
        "family_id": "trend-following",
        "question": "Does a 200-day trend filter on SPY earn a positive risk-adjusted return?",
        "prediction": "Annualised Sharpe is at least 1.0.",
        "null_hypothesis": "The Sharpe is zero.",
        "falsified_if": "The Sharpe fails to clear the luck bar.",
        "universe": ("SPY",),
        "features": ("close_sma_200",),
        "target": "next-session excess return",
        "windows": (window(DataRole.PROTECTED_EVALUATION, "2016-01-04", "2026-08-18"),),
        "primary_estimand": "annualised Sharpe against a null of zero",
        "effect": sharpe_effect(),
        "available_observations": SIP_SESSIONS,
        "confounders": ("regime dependence", "the 2020 crash dominating the sample"),
        "proposed_by": "claude-opus-5",
        "basis": EvidenceBasis(
            citations=(),
            status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE,
        ),
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def test_registration_freezes_the_prediction_under_a_content_hash(database: Database) -> None:
    with database.transaction() as session:
        registration = register(HypothesisRegistry(session), make_draft(), now=NOW)
    frozen = registration.content_hash

    with database.transaction() as session:
        reloaded = HypothesisRegistry(session).get("H-2026-001", 1)
    assert reloaded is not None
    assert reloaded.content_hash == frozen
    assert reloaded.draft.falsified_if == registration.draft.falsified_if


def test_a_material_change_cannot_overwrite_a_registration(database: Database) -> None:
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    restated = make_draft(prediction="An interior optimum exists.")
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session), restated, now=NOW)
    assert error.value.reason is RefusalReason.ALREADY_REGISTERED

    with database.transaction() as session:
        stored = HypothesisRegistry(session).get("H-2026-001", 1)
    assert stored is not None
    assert stored.draft.prediction != restated.prediction


def test_a_protected_window_a_live_reservation_holds_is_refused(database: Database) -> None:
    """The concurrency half of #22: two live reservations on one holdout cannot coexist.

    The refusal has to name `RESERVED` specifically. A message saying only "overlaps H-2026-001"
    would read identically under the pre-#22 query, which had no state in it at all, so the
    assertion would re-certify the defect.
    """
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(
                windows=(window(DataRole.VALIDATION, "2016-01-04", "2024-12-31"),),
            ),
            now=NOW,
        )

    # Same dataset, overlapping range, now claimed as an untouched holdout.
    later = make_draft(
        hypothesis_id="H-2026-002",
        materially_different="a deliberate second look, on a different dataset or window",
        windows=(window(DataRole.PROTECTED_EVALUATION, "2020-01-02", "2026-08-18"),),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session), later, now=NOW)

    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "H-2026-001" in error.value.detail
    assert "VALIDATION" in error.value.detail
    assert f"RESERVED until {LAPSES.isoformat()}" in error.value.detail
    assert "CONSUMED" not in error.value.detail


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
        register(HypothesisRegistry(session), leaky, now=NOW)

    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "its own DISCOVERY window" in error.value.detail


def test_a_disjoint_protected_window_on_a_consumed_dataset_still_registers(
    database: Database,
) -> None:
    """The block is overlap, not the dataset. Otherwise one registration retires the data."""
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(windows=(window(DataRole.VALIDATION, "2016-01-04", "2021-12-31"),)),
            now=NOW,
        )
    with database.transaction() as session:
        registration = register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-002",
                materially_different="a deliberate second look, on a different dataset or window",
                windows=(window(DataRole.PROTECTED_EVALUATION, "2022-01-03", "2026-08-18"),),
            ),
            now=NOW,
        )
    assert registration.draft.hypothesis_id == "H-2026-002"


# --- #22: reservation, consumption, release -------------------------------------------------
#
# Registration used to write a window and block every later overlap forever, so freezing a
# hypothesis and then abandoning it burned a clean holdout with nobody having looked at the
# data. Every test below names the state that produced its answer, because the pre-#22 query --
# overlap, no state at all -- returns the *correct* verdict in the one case everyone writes a
# test for, a live reservation. Asserting only "some overlap was refused" re-certifies it.

SIP = "sip-us-equities-daily"


def second_claim(**overrides: object) -> HypothesisDraft:
    """A different hypothesis wanting the same protected window H-2026-001 declared."""
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-002",
        "materially_different": "a deliberate second look at the same window",
        "universe": ("QQQ",),
        "features": ("close_sma_100",),
    }
    fields.update(overrides)
    return make_draft(**fields)


def window_state(
    database: Database,
    hypothesis_id: str = "H-2026-001",
    version: int = 1,
    role: DataRole = DataRole.PROTECTED_EVALUATION,
) -> RowMapping:
    with database.transaction() as session:
        return (
            session.execute(
                select(hypothesis_data_windows).where(
                    hypothesis_data_windows.c.hypothesis_id == hypothesis_id,
                    hypothesis_data_windows.c.version == version,
                    hypothesis_data_windows.c.role == role.value,
                )
            )
            .mappings()
            .one()
        )


def test_registering_reserves_a_window_rather_than_spending_it(database: Database) -> None:
    """Freezing a claim is not looking at the data, and only looking spends the holdout."""
    with database.transaction() as session:
        registration = register(HypothesisRegistry(session), make_draft(), now=NOW)

    row = window_state(database)
    assert row["state"] == WindowState.RESERVED.value
    assert row["consumed_at"] is None
    assert row["reserved_until"] == LAPSES.isoformat()
    assert registration.reserved_until == LAPSES

    # And the trial burden on that range is unspent, so `discovery.admissible` does not read a
    # registered-but-never-run hypothesis as having exhausted the window.
    with database.transaction() as session:
        consumption = HypothesisRegistry(session).window_consumption(
            SIP, date(2016, 1, 4), date(2026, 8, 18)
        )
    assert consumption.trials == 0
    assert consumption.status == "UNTOUCHED"
    assert consumption.consumers == ()


def test_a_reservation_lapses_on_its_own_with_nobody_releasing_it(database: Database) -> None:
    """The recovery #22 exists for. Abandonment is the absence of events, so expiry is the
    only release that can be relied on -- and the expired row is deliberately left as it is,
    to show the recovery came from the expiry and not from a cleanup pass."""
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    # Last day of the reservation: still refused, and refused *as a reservation*.
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(
            HypothesisRegistry(session),
            second_claim(),
            now=datetime.combine(LAPSES, NOW.timetz()),
        )
    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert f"RESERVED until {LAPSES.isoformat()}" in error.value.detail

    # One day later the same registration goes through, with nothing having been called.
    with database.transaction() as session:
        accepted = register(HypothesisRegistry(session), second_claim(), now=AFTER_LAPSE)
    assert accepted.draft.hypothesis_id == "H-2026-002"
    assert window_state(database)["state"] == WindowState.RESERVED.value


def test_a_released_reservation_stops_blocking_immediately(database: Database) -> None:
    """Explicit release exists for when abandonment *is* known. Nothing may depend on it."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(registry, make_draft(), now=NOW)
        registry.release("H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION)

    assert window_state(database)["state"] == WindowState.RELEASED.value

    # NOW, not after the lapse: the release is what unblocked this, not the expiry.
    with database.transaction() as session:
        accepted = register(HypothesisRegistry(session), second_claim(), now=NOW)
    assert accepted.draft.hypothesis_id == "H-2026-002"


def test_a_consumed_window_still_blocks_long_after_a_reservation_would_have_lapsed(
    database: Database,
) -> None:
    """No expiry brings back a holdout somebody looked at. Contamination is about what was
    seen, not about how old the record of seeing it is."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(registry, make_draft(), now=NOW)
        registry.consume("H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=NOW)

    much_later = AFTER_LAPSE + timedelta(days=3650)
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session), second_claim(), now=much_later)

    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "CONSUMED" in error.value.detail
    assert "RESERVED until" not in error.value.detail


def test_a_crash_after_the_handoff_still_spends_the_window(database: Database) -> None:
    """Recording consumption on success would make failing a way to read protected data free.

    The handoff is the event. This experiment dies without producing a result of any kind, and
    the holdout is spent anyway -- state, timestamp, and trial burden all as if it had run.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(registry, make_draft(), now=NOW)
        # The handoff: protected data is about to be given to the experiment.
        registry.consume("H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=NOW)

    with pytest.raises(RuntimeError), database.transaction():
        raise RuntimeError("the experiment died holding the holdout")

    row = window_state(database)
    assert row["state"] == WindowState.CONSUMED.value
    assert row["consumed_at"] == encode_utc(NOW)
    with database.transaction() as session:
        consumption = HypothesisRegistry(session).window_consumption(
            SIP, date(2016, 1, 4), date(2026, 8, 18)
        )
    assert consumption.trials == 1
    assert consumption.consumers == ("H-2026-001 v1",)


def test_a_window_cannot_be_consumed_twice(database: Database) -> None:
    """The transition is conditional on the state, so of two callers racing for one supposedly
    untouched window exactly one is told it got it."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(registry, make_draft(), now=NOW)
        registry.consume("H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=NOW)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).consume(
            "H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=NOW
        )
    assert error.value.reason is RefusalReason.WINDOW_NOT_RESERVED
    assert f"CONSUMED since {encode_utc(NOW)}" in error.value.detail


def test_a_consumed_window_cannot_be_released(database: Database) -> None:
    """Unlooking at data is not an operation, so release cannot undo a consumption."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(registry, make_draft(), now=NOW)
        registry.consume("H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=NOW)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).release(
            "H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION
        )
    assert error.value.reason is RefusalReason.WINDOW_NOT_RESERVED
    assert "is CONSUMED, not RESERVED" in error.value.detail
    assert window_state(database)["state"] == WindowState.CONSUMED.value


def test_a_holdout_re_let_after_the_lapse_cannot_still_be_handed_to_the_first_claimant(
    database: Database,
) -> None:
    """The hazard expiry introduces, closed at the handoff rather than left to be noticed.

    Expiry means a window can be claimed twice over time. If the first hypothesis surfaces
    after its lapse and is handed the data anyway, two registrations have both believed they
    held an untouched holdout -- which is the failure #22 must not create while fixing the one
    it does.
    """
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)
    with database.transaction() as session:
        register(HypothesisRegistry(session), second_claim(), now=AFTER_LAPSE)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).consume(
            "H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=AFTER_LAPSE
        )
    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "H-2026-002 v1" in error.value.detail
    assert window_state(database)["state"] == WindowState.RESERVED.value


def test_a_new_version_may_take_over_its_own_unspent_reservation(database: Database) -> None:
    """Revising a hypothesis must not cost it the holdout its previous version froze."""
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    revised = make_draft(version=2, prediction="Annualised Sharpe is at least 1.2.")
    with database.transaction() as session:
        accepted = register(HypothesisRegistry(session), revised, now=NOW)
    assert accepted.draft.version == 2


def test_a_new_version_may_not_take_over_its_own_consumption(database: Database) -> None:
    """The other half: what v1 looked at, v2 cannot un-look at."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(registry, make_draft(), now=NOW)
        registry.consume("H-2026-001", 1, dataset=SIP, role=DataRole.PROTECTED_EVALUATION, now=NOW)

    revised = make_draft(version=2, prediction="Annualised Sharpe is at least 1.2.")
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session), revised, now=NOW)
    assert error.value.reason is RefusalReason.CONTAMINATED_WINDOW
    assert "H-2026-001 v1" in error.value.detail
    assert "CONSUMED" in error.value.detail


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
        register(registry, make_draft(windows=forward), now=NOW)
        second = register(
            registry,
            make_draft(
                hypothesis_id="H-2026-002",
                materially_different="a deliberate second look, on a different dataset or window",
                windows=forward,
            ),
            now=NOW,
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
        strong = register(
            HypothesisRegistry(session),
            make_draft(effect=sharpe_effect(expected=Decimal("1.0"))),
            now=NOW,
        )
    assert strong.power.observations_required <= SIP_SESSIONS

    # A different dataset, so nothing this project has touched is involved and the
    # contamination gate has nothing to say about it.
    weak = make_draft(
        hypothesis_id="H-2026-002",
        materially_different="a deliberate second look, on a different dataset or window",
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
        register(HypothesisRegistry(session), weak, now=NOW)

    assert error.value.reason is RefusalReason.UNDERPOWERED
    assert "2669 are available" in error.value.detail


def test_the_trial_count_is_the_project_history_not_this_cycle(database: Database) -> None:
    with database.transaction() as session:
        first = register(
            HypothesisRegistry(session),
            make_draft(
                search_cardinality=12,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
            ),
            now=NOW,
        )
    assert first.cumulative_trials == PRIOR_TRIALS + 12

    with database.transaction() as session:
        second = register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-002",
                materially_different="a deliberate second look, on a different dataset or window",
                search_cardinality=5,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
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
        registered = register(HypothesisRegistry(session), registrable, now=NOW)
    assert registered.power.observations_required <= 2200

    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(2200)
        )
    assert clearance.registration_hash == registered.content_hash
    assert clearance.power.observations_required <= clearance.power.observations_available

    # A later, unrelated search raises the luck bar for everything that follows it.
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-002",
                materially_different="a deliberate second look, on a different dataset or window",
                effect=sharpe_effect(expected=Decimal("3.0")),
                search_cardinality=10,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
                windows=(window(DataRole.FORWARD_PAPER, "2026-08-19", "2027-08-19"),),
            ),
            now=NOW,
        )

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(2200)
        )
    assert error.value.reason is RefusalReason.UNDERPOWERED
    assert str(registered.power.observations_required) in error.value.detail


def test_execution_refuses_a_hypothesis_that_was_never_registered(database: Database) -> None:
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-DOES-NOT-EXIST", 1, now=NOW)
    assert error.value.reason is RefusalReason.NOT_REGISTERED


def test_editing_the_stored_registration_is_detected_at_execution(database: Database) -> None:
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    with database.transaction() as session:
        stored = session.execute(select(hypotheses.c.document_json)).scalar_one()
        document = json.loads(str(stored))
        document["draft"]["falsified_if"] = "Nothing could falsify this."
        session.execute(update(hypotheses).values(document_json=json.dumps(document)))

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(SIP_SESSIONS)
        )
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
        register(HypothesisRegistry(session), weak, now=NOW)
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
        registration = register(HypothesisRegistry(session), weak, now=NOW, override=override)
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
        register(
            HypothesisRegistry(session),
            make_draft(effect=sharpe_effect(expected=Decimal("0.3"))),
            now=NOW,
            override=PowerOverride(authorized_by="hutch", reason="accepted"),
        )
    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(SIP_SESSIONS)
        )
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
        register(HypothesisRegistry(session), churn, now=NOW)

    assert error.value.reason is RefusalReason.UNECONOMIC
    assert error.value.assessment is not None
    assert error.value.assessment.verdict is PowerVerdict.UNECONOMIC
    assert "1260" in error.value.detail


def test_every_execution_check_is_recorded_not_only_the_registration(
    database: Database,
) -> None:
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)
    with database.transaction() as session:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(SIP_SESSIONS)
        )

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
                sampling=Sampling.OVERLAPPING,
                horizon_observations=21,
                feature_lookback_observations=200,
            ),
        ),
    )
    with database.transaction() as session:
        registration = register(HypothesisRegistry(session), overlapping, now=NOW)

    assert registration.power.variance_inflation == Decimal("21")
    assert registration.power.dependence.horizon_observations == 21
    assert summarize(registration)["variance_inflation"] == "21.000000"


def test_a_repeat_of_an_existing_idea_is_refused_unless_the_difference_is_written_down(
    database: Database,
) -> None:
    """Momentum returned null on three universes because each new one had a plausible excuse.

    Writing the excuse down is the whole gate: cheap when the difference is real, and
    impossible to fill in honestly when it is not.
    """
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    repeat = make_draft(
        hypothesis_id="H-2026-002",
        windows=(window(DataRole.FORWARD_PAPER, "2026-08-19", "2027-08-19", dataset="paper"),),
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session), repeat, now=NOW)
    assert error.value.reason is RefusalReason.DUPLICATE
    assert "H-2026-001 v1" in error.value.detail

    with database.transaction() as session:
        accepted = register(
            HypothesisRegistry(session),
            repeat.model_copy(
                update={"materially_different": "same signal, but on forward paper evidence"}
            ),
            now=NOW,
        )
    assert accepted.draft.hypothesis_id == "H-2026-002"


def test_a_genuinely_different_universe_is_not_treated_as_a_repeat(database: Database) -> None:
    """The gate is overlap, not similarity of wording. A new universe is a new question."""
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    elsewhere = make_draft(
        hypothesis_id="H-2026-002",
        universe=("EWJ", "EWG", "EWU", "EWZ", "INDA"),
        features=("country_index_momentum_252",),
        windows=(
            window(
                DataRole.PROTECTED_EVALUATION,
                "2016-01-04",
                "2026-08-18",
                dataset="global-country-indices-daily",
            ),
        ),
    )
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        report = registry.novelty_report(elsewhere)
        register(registry, elsewhere, now=NOW)
    assert report.novel
    assert report.highest_overlap == 0.0


def test_window_consumption_answers_untouched_partial_and_exhausted(database: Database) -> None:
    """The structural limit REFUTED.md states in prose, made queryable."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        virgin = registry.window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )
    assert virgin.status == "UNTOUCHED"
    assert virgin.trials == 0
    assert virgin.usable

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(
            registry,
            make_draft(
                search_cardinality=5,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
            ),
            now=NOW,
        )
        registry.consume(
            "H-2026-001",
            1,
            dataset="sip-us-equities-daily",
            role=DataRole.PROTECTED_EVALUATION,
            now=NOW,
        )
    with database.transaction() as session:
        partial = HypothesisRegistry(session).window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )
    assert partial.status == "PARTIALLY_CONSUMED"
    assert partial.trials == 5
    assert partial.consumers == ("H-2026-001 v1",)

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(
            registry,
            make_draft(
                hypothesis_id="H-2026-002",
                materially_different="a second family against the same window",
                search_cardinality=40,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
                # Validation rather than protected evaluation, so it consumes the window
                # without tripping the contamination gate this test is not about.
                windows=(window(DataRole.VALIDATION, "2016-01-04", "2026-08-18"),),
            ),
            now=NOW,
        )
        registry.consume(
            "H-2026-002",
            1,
            dataset="sip-us-equities-daily",
            role=DataRole.VALIDATION,
            now=NOW,
        )
    with database.transaction() as session:
        spent = HypothesisRegistry(session).window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )
    assert spent.status == "EXHAUSTED"
    assert not spent.usable


def test_a_disjoint_range_of_a_spent_dataset_is_still_untouched(database: Database) -> None:
    """Consumption is per range, not per dataset. Otherwise one run retires a data source."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(
            registry,
            make_draft(
                windows=(window(DataRole.VALIDATION, "2016-01-04", "2020-12-31"),),
                search_cardinality=40,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
            ),
            now=NOW,
        )
        registry.consume(
            "H-2026-001",
            1,
            dataset="sip-us-equities-daily",
            role=DataRole.VALIDATION,
            now=NOW,
        )
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        old = registry.window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2020, 12, 31)
        )
        later = registry.window_consumption(
            "sip-us-equities-daily", date(2021, 1, 4), date(2026, 8, 18)
        )
    assert old.status == "EXHAUSTED"
    assert later.status == "UNTOUCHED"


def test_a_hypothesis_cannot_be_frozen_without_a_critic_verdict(database: Database) -> None:
    """A hypothesis reaching protected data unreviewed is what #7 exists to prevent."""
    blocking = Critique(
        hypothesis_id="H-2026-001",
        hypothesis_version=1,
        critic="deterministic",
        verdict=CriticVerdict.REVISE,
        reasons=("hidden-beta: alpha is t=0.05 against a 2.90 bar",),
        objections=(
            Objection(
                check="hidden-beta",
                severity=Severity.BLOCKING,
                finding="alpha is indistinguishable from zero at beta 0.71",
                evidence={"alpha_t": "0.05", "beta": "0.71"},
            ),
        ),
        produced_at=NOW,
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(make_draft(), now=NOW, critique=blocking)
    assert error.value.reason is RefusalReason.CRITIQUE_BLOCKS
    assert "alpha is indistinguishable from zero" in error.value.detail

    with database.transaction() as session:
        assert HypothesisRegistry(session).get("H-2026-001", 1) is None


def test_a_critique_of_a_different_hypothesis_cannot_be_borrowed(database: Database) -> None:
    """A clean review of something else is not a review of this."""
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(
            make_draft(), now=NOW, critique=cleared("H-SOMETHING-ELSE", 1)
        )
    assert error.value.reason is RefusalReason.CRITIQUE_MISMATCHED

    # And a review of the right hypothesis at the wrong version is equally not a review.
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).register(
            make_draft(), now=NOW, critique=cleared("H-2026-001", 2)
        )
    assert error.value.reason is RefusalReason.CRITIQUE_MISMATCHED


def test_the_critique_is_frozen_inside_the_registration(database: Database) -> None:
    """The critic cannot rewrite a frozen registration, because the review is part of it."""
    review = cleared()
    with database.transaction() as session:
        frozen = register(HypothesisRegistry(session), make_draft(), critique=review)

    with database.transaction() as session:
        stored = HypothesisRegistry(session).get("H-2026-001", 1)
    assert stored is not None
    assert stored.critique == review
    assert stored.content_hash == frozen.content_hash

    # Changing the critique changes the hash, so a later rewrite is a different registration.
    revised = frozen.model_copy(
        update={"critique": review.model_copy(update={"critic": "someone-else"})}
    )
    assert revised.content_hash != frozen.content_hash
    assert summarize(stored)["critic_verdict"] == "PROCEED"
    assert summarize(stored)["unassessed_by_critic"] == []


def test_a_hypothesis_must_say_what_backs_it_before_it_can_be_frozen(
    database: Database,
) -> None:
    """Either citations, or an explicit statement that there are none (#4).

    There is no default: "we did not look" would otherwise be indistinguishable from "we
    looked and there is nothing", and the second is a finding while the first is a gap.
    """
    fields = make_draft().model_dump()
    fields.pop("basis")
    with pytest.raises(ValueError, match="basis"):
        HypothesisDraft(**fields)

    cited = make_draft(
        basis=EvidenceBasis(
            citations=(
                Citation(
                    claim="trend following persists out of sample",
                    source_id="paper-tsmom-2012",
                    locator="table 2",
                    status=EpistemicStatus.LITERATURE_SUPPORTED,
                ),
            ),
            status=EpistemicStatus.LITERATURE_SUPPORTED,
        )
    )
    with database.transaction() as session:
        frozen = register(HypothesisRegistry(session), cited)

    # The basis travels into the frozen registration, and literature never gates promotion.
    assert frozen.draft.basis.status is EpistemicStatus.LITERATURE_SUPPORTED
    assert not frozen.draft.basis.gates_promotion
    assert not gates_promotion(frozen.draft.basis.status)


def test_execution_refuses_without_a_verified_count_rather_than_trusting_the_declared_one(
    database: Database,
) -> None:
    """The gate must fail closed on sample size, which is the input most worth overstating.

    `verify_for_execution` used to default to `draft.available_observations` whenever the caller
    omitted an override. So it re-checked the multiple-testing burden against durable state — the
    hard part, done correctly — and then read sample size straight off the proposal. A registrant
    who overstated it got a clean pass from a gate whose docstring said the registered numbers are
    not trusted.
    """
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)

    assert error.value.reason is RefusalReason.UNVERIFIED_SAMPLE
    assert "planning number" in error.value.detail


def test_an_overstated_declaration_cannot_clear_execution_on_a_smaller_verified_count(
    database: Database,
) -> None:
    """The specific attack: declare a sample large enough to pass, then run on what exists.

    Registration is allowed to be optimistic — it is a planning document. Execution is not, and
    this is the assertion that the verified number, not the declared one, decides.
    """
    with database.transaction() as session:
        registered = register(
            HypothesisRegistry(session),
            make_draft(
                effect=sharpe_effect(expected=Decimal("1.0")),
                available_observations=SIP_SESSIONS,
            ),
            now=NOW,
        )

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(120)
        )

    assert error.value.reason is RefusalReason.UNDERPOWERED
    assert registered.power.observations_required > 120


def test_a_count_from_an_unregistered_dataset_is_refused(database: Database) -> None:
    """Counting from somewhere else is not verification, it is a different experiment.

    Without this, the mandatory-count requirement is satisfiable by measuring whatever dataset
    happens to be largest, which restores the original defect through a longer path.
    """
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(SIP_SESSIONS, dataset="crypto-1min")
        )

    assert error.value.reason is RefusalReason.UNVERIFIED_SAMPLE
    assert "crypto-1min" in error.value.detail


def test_an_operator_override_still_does_not_excuse_measuring(database: Database) -> None:
    """An override accepts a known shortfall; it is not permission to skip counting.

    Worth pinning separately because the override path is the obvious place for the fallback to
    creep back in — it already carries one exception forward, so a second looks natural there.
    """
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(available_observations=200, effect=sharpe_effect(expected=Decimal("0.2"))),
            now=NOW,
            override=PowerOverride(
                authorized_by="hutch",
                reason="accepting a known shortfall to exercise the pipeline end to end",
            ),
        )

    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)

    assert error.value.reason is RefusalReason.UNVERIFIED_SAMPLE


def test_a_count_from_outside_the_registered_window_is_refused(database: Database) -> None:
    """Matching the dataset is not enough; the range has to be the one that was frozen.

    Residual hole in the #24 fix: `verify_for_execution` compared `observed.dataset` against the
    registered datasets and never looked at the dates. A hypothesis registered on 2016-2026 could
    therefore be cleared by a count taken over 1990-2026 of the same dataset — the declared-sample
    defect the check exists to close, wearing a verified label and a much larger N.
    """
    with database.transaction() as session:
        register(HypothesisRegistry(session), make_draft(), now=NOW)

    inflated = ObservedSample(
        observations=SIP_SESSIONS * 3,
        dataset="sip-us-equities-daily",
        start_date=date(1990, 1, 2),
        end_date=date(2026, 8, 18),
        method="every session the vendor has, not the window that was registered",
        counted_at=NOW,
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=inflated
        )

    assert error.value.reason is RefusalReason.UNVERIFIED_SAMPLE
    assert "no registered window covers" in error.value.detail


def test_counting_fewer_observations_than_the_window_spans_is_allowed(
    database: Database,
) -> None:
    """The honest shortfall must still clear, or verification becomes unusable.

    Containment rather than equality is the rule: data missing at the edges, a feature needing
    warm-up, or a symbol listing late all produce a genuine count narrower than the registered
    window. Surfacing that is the entire point of verifying. Refusing it would push every caller
    back to declaring the window's nominal width, which is where this started.
    """
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(effect=sharpe_effect(expected=Decimal("1.0"))),
            now=NOW,
        )

    short = ObservedSample(
        observations=2400,
        dataset="sip-us-equities-daily",
        start_date=date(2016, 6, 1),
        end_date=date(2026, 8, 18),
        method="sessions with a bar present after the 200-day warm-up",
        counted_at=NOW,
    )
    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=short
        )

    assert clearance.observed.observations == 2400
    assert clearance.power.observations_available == 2400


def test_measured_requires_a_durable_record_and_a_matching_count(database: Database) -> None:
    """#23: MEASURED is now reachable, and only against evidence.

    It was refused outright while nothing emitted search telemetry, because accepting it on
    trust would have reopened the laundering path one enum value across from where #23 closed
    it. `SubprocessWorker` now discloses a cardinality and `record_search_run` persists it, so
    the count can be derived from a row.

    Two things are checked, and the second is the one that matters: the record must exist, AND
    the declared count must match it. Verifying only the id would leave the number itself
    declared -- a draft could cite a real search of 5,000 and register a cardinality of 1, which
    is the original defect wearing a reference.
    """
    with database.transaction() as session:
        with pytest.raises(ValidationError, match="must name one"):
            make_draft(search_cardinality=500, search_origin=SearchOrigin.MEASURED)

        # Naming a record that does not exist.
        with pytest.raises(RegistrationRefused, match=r"not\s+recorded"):
            register(
                HypothesisRegistry(session),
                make_draft(
                    search_cardinality=500,
                    search_origin=SearchOrigin.MEASURED,
                    search_run_id="search-nobody-logged",
                ),
            )


def test_a_measured_count_that_contradicts_its_record_is_refused(database: Database) -> None:
    """The record is the authority, and a mismatch is refused rather than silently corrected.

    Quietly adopting the recorded value would hide a registration whose stated burden differs
    from its evidence, which is a mistake someone should see rather than one the system fixes
    on their behalf.
    """
    with database.transaction() as session:
        session.execute(
            search_runs.insert().values(
                search_id="search-1",
                actor="rd-agent",
                actor_version="0.4.1",
                candidates_evaluated=5000,
                dataset="sip-us-equities-daily",
                start_date="2016-01-04",
                end_date="2026-08-18",
                data_role="DISCOVERY",
                started_at="2026-08-21T12:00:00Z",
                finished_at="2026-08-21T13:00:00Z",
                configuration_hash="cfg-1",
                detail_json="{}",
            )
        )

        with pytest.raises(RegistrationRefused, match="recorded 5000"):
            register(
                HypothesisRegistry(session),
                make_draft(
                    search_cardinality=1,
                    search_origin=SearchOrigin.MEASURED,
                    search_run_id="search-1",
                ),
            )


def test_the_only_way_to_claim_a_search_is_to_sign_for_it(database: Database) -> None:
    """What remains registrable above a count of 1, so the refusals above are not a dead end."""
    with database.transaction() as session:
        registered = register(
            HypothesisRegistry(session),
            make_draft(
                search_cardinality=12,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
            ),
            now=NOW,
        )
    assert registered.draft.search_origin is SearchOrigin.OPERATOR_ATTESTED
    assert registered.draft.search_attested_by == "hutch"
    # And the burden actually moved, rather than the origin being decorative.
    assert registered.cumulative_trials >= PRIOR_TRIALS + 12


def test_window_consumption_says_which_family_spent_the_budget(database: Database) -> None:
    """#14: statistical budget keyed to family, not one undifferentiated number.

    An operator deciding where to look next needs to know who spent the range. A total alone
    says the data is used up without saying by what, which is the "misleading global number"
    the issue names.

    The three hypotheses take non-overlapping spans of the same dataset, because that is the
    only honest way several of them coexist there: the contamination gate refuses two claims on
    one protected window, and rightly so.
    """
    spans = (
        ("2016-01-04", "2018-12-31", "trend-following"),
        ("2019-01-02", "2021-12-31", "trend-following"),
        ("2022-01-03", "2024-12-31", "mean-reversion"),
    )
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        for index, (start, end, family) in enumerate(spans):
            register(
                registry,
                make_draft(
                    hypothesis_id=f"H-FAM-{index}",
                    family_id=family,
                    materially_different="a different span of the same dataset",
                    search_cardinality=3,
                    search_origin=SearchOrigin.OPERATOR_ATTESTED,
                    search_attested_by="hutch",
                    features=(f"feature_{index}",),
                    windows=(window(DataRole.PROTECTED_EVALUATION, start, end),),
                ),
            )
            registry.consume(
                f"H-FAM-{index}",
                1,
                dataset="sip-us-equities-daily",
                role=DataRole.PROTECTED_EVALUATION,
                now=NOW,
            )

        consumption = registry.window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )

    assert consumption.by_family == {"mean-reversion": 3, "trend-following": 6}
    assert consumption.trials == 9, "the total is every family's spend, not one family's"


def test_a_new_family_does_not_start_from_zero_on_a_spent_range(database: Database) -> None:
    """Exhaustion is deliberately NOT family-scoped, and this is why.

    The multiple-testing burden is a property of the data, not of the question's label. Forty
    hypotheses tested on 2016-2026 raise the luck bar for the forty-first whatever family it
    belongs to. Scoping exhaustion by family would hand out fresh significance for the price of
    renaming the question -- which is the laundering this whole subsystem exists to prevent.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        register(
            registry,
            make_draft(
                hypothesis_id="H-HEAVY",
                family_id="trend-following",
                search_cardinality=EXHAUSTED_TRIALS,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
            ),
        )
        registry.consume(
            "H-HEAVY",
            1,
            dataset="sip-us-equities-daily",
            role=DataRole.PROTECTED_EVALUATION,
            now=NOW,
        )

        consumption = registry.window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )

    assert consumption.status == "EXHAUSTED"
    # The spend is attributable to one family, and the range is spent for every family.
    assert consumption.by_family == {"trend-following": EXHAUSTED_TRIALS}
    assert consumption.usable is False


def test_two_families_jointly_exhaust_a_range_neither_could_alone(database: Database) -> None:
    """The case that distinguishes a global burden from a family-scoped one.

    Two families each spend a little over half the budget on different spans of one dataset.
    Globally the range is spent; per family, neither is close. Scoping exhaustion by family
    would report this range as usable and hand out fresh significance for the price of renaming
    the question.

    A single family spending the whole budget cannot test this: there, the total and the
    per-family maximum are the same number, and a family-scoped implementation passes. That
    mutation survived until this test existed.
    """
    half = EXHAUSTED_TRIALS // 2 + 1
    spans = (
        ("2016-01-04", "2020-12-31", "trend-following"),
        ("2021-01-04", "2024-12-31", "mean-reversion"),
    )
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        for index, (start, end, family) in enumerate(spans):
            register(
                registry,
                make_draft(
                    hypothesis_id=f"H-JOINT-{index}",
                    family_id=family,
                    materially_different="a different span of the same dataset",
                    search_cardinality=half,
                    search_origin=SearchOrigin.OPERATOR_ATTESTED,
                    search_attested_by="hutch",
                    features=(f"joint_feature_{index}",),
                    windows=(window(DataRole.PROTECTED_EVALUATION, start, end),),
                    effect=sharpe_effect(expected=Decimal("3.0")),
                ),
            )
            registry.consume(
                f"H-JOINT-{index}",
                1,
                dataset="sip-us-equities-daily",
                role=DataRole.PROTECTED_EVALUATION,
                now=NOW,
            )

        consumption = registry.window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )

    assert consumption.trials == half * 2
    assert consumption.trials >= EXHAUSTED_TRIALS
    # Neither family alone reaches the threshold.
    assert all(spent < EXHAUSTED_TRIALS for spent in consumption.by_family.values())
    assert len(consumption.by_family) == 2
    # And the range is still spent, because the burden belongs to the data.
    assert consumption.status == "EXHAUSTED"
    assert consumption.usable is False


def test_no_route_through_the_gate_is_opened_by_overstating_an_input(
    database: Database,
) -> None:
    """#5's closure test: three lies a registrant benefits from, none of which lowers the bar.

    Each of these is an input the registry cannot compute for itself and therefore has to be
    told. That is exactly the set worth attacking together rather than one test at a time --
    the gate is only as strong as the weakest number it accepts on trust, and a suite that
    checks them separately can still leave a combination that works.
    """
    registrable = make_draft(
        effect=sharpe_effect(expected=Decimal("1.0")), available_observations=2200
    )
    with database.transaction() as session:
        registered = register(HypothesisRegistry(session), registrable, now=NOW)

    # 1. An inflated sample count. `available_observations` was the planning number; the gate
    #    requires a verified one and does not fall back to the claim.
    with database.transaction() as session, pytest.raises(RegistrationRefused) as unverified:
        HypothesisRegistry(session).verify_for_execution("H-2026-001", 1, now=NOW)
    assert unverified.value.reason is RefusalReason.UNVERIFIED_SAMPLE

    # 2. A verified count taken over a wider window than the registration froze. The dataset
    #    matches and the number is real; it is simply a count of different data. This is the
    #    declared-sample defect wearing a verified label.
    wider = ObservedSample(
        observations=9000,
        dataset="sip-us-equities-daily",
        start_date=date(1990, 1, 2),
        end_date=date(2026, 8, 18),
        method="every session since 1990",
        counted_at=NOW,
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as widened:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=wider
        )
    assert widened.value.reason is RefusalReason.UNVERIFIED_SAMPLE
    assert "no registered window covers" in widened.value.detail

    # 3. A count from a dataset the registration never named.
    elsewhere = counted(2200, dataset="crypto-daily")
    with database.transaction() as session, pytest.raises(RegistrationRefused) as foreign:
        HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=elsewhere
        )
    assert foreign.value.reason is RefusalReason.UNVERIFIED_SAMPLE

    # 4. A search burden that does not match its stated origin. There is no self-report origin
    #    to declare -- that path was removed rather than validated -- so the remaining move is
    #    to run a wide search and file it under an origin that means "searched nothing".
    with pytest.raises(ValidationError, match="searched nothing"):
        make_draft(
            hypothesis_id="H-2026-009",
            materially_different="a second look after a wide search",
            search_cardinality=500,
            search_origin=SearchOrigin.NO_DATA_DEPENDENT_SEARCH,
        )

    # 5. And MEASURED cannot be claimed without a durable search record behind it.
    with pytest.raises(ValidationError):
        make_draft(
            hypothesis_id="H-2026-010",
            materially_different="a second look after a measured search",
            search_cardinality=500,
            search_origin=SearchOrigin.MEASURED,
        )

    # And an honest count over the registered window still clears, so none of the above is a
    # blanket refusal dressed up as four protections.
    with database.transaction() as session:
        clearance = HypothesisRegistry(session).verify_for_execution(
            "H-2026-001", 1, now=NOW, observed=counted(2200)
        )
    assert clearance.registration_hash == registered.content_hash


def test_an_overstated_registration_reserves_nothing_permanent(database: Database) -> None:
    """#35: the bounded-harm argument, as a mechanism rather than a claim.

    GPT-5.6 Sol argues registration-time power should require a verified observation count,
    because a registrant can otherwise reserve a protected holdout after overstating the sample.
    I argue the declared figure is correct at that gate and that the harm is bounded since #22 --
    a reservation expires, and only consumed windows count toward the burden.

    This test is worth having whichever way that decision goes, because my half of it is
    currently an assertion about behaviour rather than a demonstration of it. If the argument is
    wrong, this fails and the disagreement is settled by evidence.

    The overstatement here is deliberate and large: 10,000 claimed against a window that will
    only ever supply a few thousand. The registration succeeds -- that is the behaviour under
    discussion -- and the question is what it costs.
    """
    inflated = make_draft(
        effect=sharpe_effect(expected=Decimal("1.0")), available_observations=10_000
    )
    with database.transaction() as session:
        register(HypothesisRegistry(session), inflated, now=NOW)

    with database.transaction() as session:
        registry = HypothesisRegistry(session)

        # 1. Execution clearance refuses it, because the verified count is what that gate uses.
        with pytest.raises(RegistrationRefused) as refused:
            registry.verify_for_execution("H-2026-001", 1, now=NOW, observed=counted(800))
        assert refused.value.reason is RefusalReason.UNDERPOWERED

        # 2. Nothing was consumed. The window is spoken for, not spent.
        consumption = registry.window_consumption(
            "sip-us-equities-daily", date(2016, 1, 4), date(2026, 8, 18)
        )

    assert consumption.status != "EXHAUSTED", (
        "an overstated registration must not push a range to exhausted; it reserved, it did not "
        "consume, and only consumption is permanent"
    )
    assert consumption.trials == 0, (
        "the multiple-testing burden counts consumed windows, so an unexecuted registration "
        "costs the next hypothesis nothing"
    )
