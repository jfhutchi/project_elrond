"""Validation-campaign selection freedom, charged through the existing search ledger (#159).

#215 proved a campaign's *identity* moves when its selection changes. That is not the same
claim, and on its own it buys nothing: an identity nobody charges is decorative. These tests are
about the **statistical burden**, which is the thing that actually constrains a later result.

The rule being pinned:

    twelve scenarios chosen by one frozen rule  -> one trial
    twelve alternative campaign selections      -> twelve trials

so scenario count is not multiplicity and selection freedom is. The increment comes from the
append-only `search_runs` ledger having another row, never from a caller declaring a redraw
happened -- a caller that could decline to declare one is a caller that could hide it.

`test_a_post_outcome_redraw_raises_cumulative_burden` is the load-bearing case and the one the
forensic audit found missing from both the abandoned branch and its stash.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from quantbot.research.manifest import ExperimentPeriod, content_id
from quantbot.research.registry import (
    VALIDATION_CAMPAIGN_ACTOR,
    HypothesisRegistry,
    RegistrationRefused,
    WindowState,
)
from quantbot.research.validation_campaign import (
    CONTIGUOUS_ROTATION_POLICY_VERSION,
    ScenarioKind,
    ScenarioSelectionMethod,
    ValidationCampaign,
    ValidationScenario,
    select_rotation_periods,
)
from quantbot.storage import Database
from quantbot.storage.schema import hypothesis_data_windows, search_runs
from tests.unit.research.test_runner import NOW, critique, draft

DATASET = "sip-us-equities-daily"
ELIGIBLE = ExperimentPeriod(name="eligible", start=date(2016, 1, 4), end=date(2026, 8, 14))

#: The content id of the standard test draft on the commit *before* `validation_campaign` was
#: added to `HypothesisDraft`, captured by running `content_id(draft())` on that commit. It is a
#: literal on purpose: a value recomputed from the current model would agree with itself however
#: badly the model had drifted.
GOLDEN_CAMPAIGNLESS_DRAFT_ID = (
    "7cd29b705cb9469495533e46e9ccbc604e9d28c33b54a141489eb6184298d724"
)


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database.create(tmp_path / "campaign-registry.db")
    yield db
    db.close()


def scenario(**overrides: object) -> ValidationScenario:
    fields: dict[str, object] = {
        "kind": ScenarioKind.EXECUTION_STRESS,
        "dataset": DATASET,
        "data_role": "PROTECTED_EVALUATION",
        "period": ExperimentPeriod(name="s", start=date(2018, 1, 2), end=date(2019, 12, 31)),
        "required": True,
        "adverse": True,
        "minimum_trades": 30,
        "minimum_effective_observations": 200,
    }
    fields.update(overrides)
    return ValidationScenario(**fields)


def declared_campaign(**overrides: object) -> ValidationCampaign:
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
                period=ExperimentPeriod(
                    name="g", start=date(2020, 1, 2), end=date(2021, 12, 31)
                ),
            ),
        ),
        "maximum_underpowered_fraction": Decimal("0.5"),
    }
    fields.update(overrides)
    return ValidationCampaign(**fields)


def seeded_campaign(*, seed: int, count: int = 3) -> ValidationCampaign:
    """A campaign whose windows are produced by a frozen seed, not chosen by hand."""
    periods = select_rotation_periods(
        ELIGIBLE,
        method=ScenarioSelectionMethod.SEEDED_RANDOM,
        window_days=180,
        count=count,
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
    return declared_campaign(
        selection_method=ScenarioSelectionMethod.SEEDED_RANDOM,
        selection_policy_version=CONTIGUOUS_ROTATION_POLICY_VERSION,
        selection_seed=seed,
        rotation_window_days=180,
        rotation_window_count=count,
        scenarios=scenarios,
    )


def consume_window(session, registry: HypothesisRegistry, hypothesis_id: str) -> None:
    """Put a real reserved window into CONSUMED: an outcome has now been seen.

    Driven through a genuine registration rather than a synthetic row, because
    `hypothesis_data_windows` carries a foreign key to `hypotheses` -- a hand-built row would be
    a window belonging to no hypothesis, which is not a state the system can reach.
    """
    session.execute(
        hypothesis_data_windows.update()
        .where(hypothesis_data_windows.c.hypothesis_id == hypothesis_id)
        .values(state=WindowState.CONSUMED.value, consumed_at="2026-08-20T14:30:00Z")
    )


def campaign_rows(session) -> int:
    return len(
        list(
            session.execute(
                select(search_runs.c.search_id).where(
                    search_runs.c.actor == VALIDATION_CAMPAIGN_ACTOR
                )
            )
        )
    )


# --- the load-bearing case ------------------------------------------------------------------


def test_a_post_outcome_redraw_raises_cumulative_burden(database: Database) -> None:
    """**The property this slice exists for.**

    Campaign A is frozen and charged. A protected window is then consumed -- an outcome has been
    seen. A redraw with a materially different selection is frozen, and the bar every later
    registration is judged against must go up.

    Asserted on the ledger and on `cumulative_trials`, not on identities: #215 already proved
    `a.campaign_id != b.campaign_id`, and an identity nobody charges constrains nothing. What
    must be true here is that the *statistical burden* moved.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)

        first = seeded_campaign(seed=20260903)
        registry.freeze_validation_campaign(first, now=NOW)
        campaigns_after_first = registry.campaign_trials_spent()
        trials_after_first = registry.cumulative_trials()

        # An outcome has now been seen: a real registration's protected window is consumed.
        registry.register(draft(), now=NOW, critique=critique())
        consume_window(session, registry, "H-RUN-1")

        # The redraw. A different seed is a materially different selection, frozen over an
        # untouched dataset -- the original is now contaminated, and refusing *that* is a
        # separate protection with its own test below.
        redrawn = seeded_campaign(seed=20260904)
        assert redrawn.campaign_id != first.campaign_id
        moved = redrawn.model_copy(
            update={
                "scenarios": tuple(
                    item.model_copy(update={"dataset": f"{DATASET}-second"})
                    for item in redrawn.scenarios
                )
            }
        )
        registry.freeze_validation_campaign(moved, now=NOW)

        campaigns_after_second = registry.campaign_trials_spent()
        trials_after_second = registry.cumulative_trials()

    # The campaign ledger charged the redraw, exactly once.
    assert campaigns_after_first == 1, "one frozen campaign is one trial"
    assert campaigns_after_second == 2, "a redraw must cost another trial"
    # And the bar a later registration faces is strictly higher because of it.
    assert trials_after_second > trials_after_first


def test_a_redraw_cannot_hide_inside_the_first_campaigns_record(database: Database) -> None:
    """The other half of the same protection: B cannot reuse A's ledger row.

    Without this, a redraw could be charged nothing by presenting the earlier campaign's
    `search_id` -- the exact move the append-only ledger exists to make impossible.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        first = seeded_campaign(seed=20260903)
        registry.freeze_validation_campaign(first, now=NOW)

        second = seeded_campaign(seed=20260904)
        session.execute(
            search_runs.update()
            .where(search_runs.c.search_id == first.selection_search_id)
            .values(configuration_hash=second.campaign_id)
        )

        with pytest.raises(RegistrationRefused, match="not an authoritative record"):
            registry.freeze_validation_campaign(first, now=NOW)


# --- scenario count is not multiplicity -------------------------------------------------------


def test_a_twelve_scenario_campaign_costs_one_trial_not_twelve(database: Database) -> None:
    """Dispersion across a frozen scenario set is the result, not twelve chances at a winner."""
    periods = select_rotation_periods(
        ELIGIBLE,
        method=ScenarioSelectionMethod.DETERMINISTIC_SWEEP,
        window_days=180,
        step_days=300,
    )
    assert len(periods) >= 12, "the sweep must produce enough windows for this to mean anything"
    wide = declared_campaign(
        selection_method=ScenarioSelectionMethod.DETERMINISTIC_SWEEP,
        selection_policy_version=CONTIGUOUS_ROTATION_POLICY_VERSION,
        rotation_window_days=180,
        rotation_step_days=300,
        scenarios=tuple(
            scenario(
                kind=(
                    ScenarioKind.EXECUTION_STRESS if index == 0 else ScenarioKind.GENERALIZATION
                ),
                adverse=index == 0,
                period=item,
            )
            for index, item in enumerate(periods)
        ),
    )

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        before = registry.cumulative_trials()
        registry.freeze_validation_campaign(wide, now=NOW)
        after = registry.cumulative_trials()

    assert len(wide.scenarios) >= 12
    assert after == before + 1, "one frozen selection, whatever its length"


def test_refreezing_the_same_campaign_charges_nothing_further(database: Database) -> None:
    """Reopening or re-verifying a registration must not inflate its own burden.

    Idempotent by content address: the second freeze finds the row and returns it. A campaign
    that cost more each time it was read would make reading it an act of measurement.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()

        first_id = registry.freeze_validation_campaign(subject, now=NOW)
        after_first = registry.cumulative_trials()
        second_id = registry.freeze_validation_campaign(subject, now=NOW)
        after_second = registry.cumulative_trials()
        third_id = registry.freeze_validation_campaign(subject, now=NOW)

        assert first_id == second_id == third_id
        assert after_second == after_first
        assert registry.cumulative_trials() == after_first
        assert campaign_rows(session) == 1


def test_cosmetic_renaming_adds_no_burden(database: Database) -> None:
    """Display text is not a scientific choice, so it cannot cost a trial.

    The control for the redraw case: if renaming charged, the ledger would be measuring
    keystrokes rather than selection freedom.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        plain = declared_campaign()
        registry.freeze_validation_campaign(plain, now=NOW)
        after_plain = registry.cumulative_trials()

        renamed = plain.model_copy(
            update={
                "scenarios": tuple(
                    item.model_copy(update={"display_name": f"Scary Test {index}"})
                    for index, item in enumerate(plain.scenarios)
                )
            }
        )
        assert renamed.campaign_id == plain.campaign_id
        registry.freeze_validation_campaign(renamed, now=NOW)

        assert registry.cumulative_trials() == after_plain
        assert campaign_rows(session) == 1


# --- fail-closed registration -----------------------------------------------------------------


def test_a_registration_naming_an_unfrozen_campaign_is_refused(database: Database) -> None:
    """Fails closed. A campaign with no ledger row was preregistered only in the telling, and
    its selection freedom would go uncharged."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        with pytest.raises(RegistrationRefused, match="no frozen search record"):
            registry.register(
                draft(validation_campaign=declared_campaign()),
                now=NOW,
                critique=critique(),
            )


def test_a_registration_citing_a_record_for_another_campaign_is_refused(
    database: Database,
) -> None:
    """The row is the authority, and it must describe *this* campaign."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()
        registry.freeze_validation_campaign(subject, now=NOW)
        session.execute(
            search_runs.update()
            .where(search_runs.c.search_id == subject.selection_search_id)
            .values(configuration_hash="f" * 64)
        )

        with pytest.raises(RegistrationRefused, match="not an authoritative record"):
            registry.register(
                draft(validation_campaign=subject), now=NOW, critique=critique()
            )


def test_a_frozen_campaign_registers_and_is_charged(database: Database) -> None:
    """The control for both refusals: the ordinary path works and the burden is counted."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()
        registry.freeze_validation_campaign(subject, now=NOW)

        registration = registry.register(
            draft(validation_campaign=subject), now=NOW, critique=critique()
        )

        assert registration.draft.validation_campaign is not None
        assert registration.draft.validation_campaign.campaign_id == subject.campaign_id
        # The campaign row is inside the bar this very registration was judged against.
        assert registration.cumulative_trials >= registry.campaign_trials_spent()


# --- freeze before measurement -----------------------------------------------------------------


def test_a_campaign_cannot_be_frozen_over_an_already_consumed_window(
    database: Database,
) -> None:
    """The lifecycle this whole slice would otherwise be arithmetic about a fiction.

    Read the protected outcome, then write a campaign that fits it, then call it preregistered.
    Refused at the freeze, before any burden is recorded.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.register(draft(), now=NOW, critique=critique())
        consume_window(session, registry, "H-RUN-1")

        with pytest.raises(RegistrationRefused, match="already-consumed window"):
            registry.freeze_validation_campaign(declared_campaign(), now=NOW)

        assert campaign_rows(session) == 0, "a refused freeze must charge nothing"


def test_a_live_reservation_does_not_block_freezing(database: Database) -> None:
    """The control, and the reason only CONSUMED blocks: a reservation is a claim on data
    nobody has looked at, so a campaign frozen beside one is still frozen before measurement.

    Refusing here would make a second version of a hypothesis unable to declare scenarios at all.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.register(draft(), now=NOW, critique=critique())

        registry.freeze_validation_campaign(declared_campaign(), now=NOW)

        assert campaign_rows(session) == 1


# --- burden is monotone and derived --------------------------------------------------------------


def test_no_campaign_can_reduce_accumulated_burden(database: Database) -> None:
    """Burden is a sum over append-only rows, so it can only rise.

    Every campaign shape available is tried against an existing charge, including re-freezing
    the first one, and none of them lowers the total.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        first = seeded_campaign(seed=1)
        registry.freeze_validation_campaign(first, now=NOW)
        high_water = registry.cumulative_trials()

        for index, candidate in enumerate(
            (first, declared_campaign(), seeded_campaign(seed=2, count=2)), start=1
        ):
            moved = candidate.model_copy(
                update={
                    "scenarios": tuple(
                        item.model_copy(update={"dataset": f"{DATASET}-{index}"})
                        for item in candidate.scenarios
                    )
                }
            )
            registry.freeze_validation_campaign(moved, now=NOW)
            assert registry.cumulative_trials() >= high_water
            high_water = registry.cumulative_trials()


def test_the_charge_is_read_from_the_ledger_not_from_the_payload(database: Database) -> None:
    """`trial_charge` on the model is a claim; the ledger is the authority.

    Proven by contradiction: the stored row is edited to a different count, and the burden
    follows the row rather than the campaign object's own `trial_charge`.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()
        registry.freeze_validation_campaign(subject, now=NOW)
        assert subject.trial_charge == 1
        assert registry.campaign_trials_spent() == 1

        session.execute(
            search_runs.update()
            .where(search_runs.c.search_id == subject.selection_search_id)
            .values(candidates_evaluated=7)
        )

        assert subject.trial_charge == 1
        assert registry.campaign_trials_spent() == 7


# --- backward compatibility ------------------------------------------------------------------


def test_a_campaignless_draft_hashes_exactly_as_it_did_before_this_field_existed() -> None:
    """**Registrations made before #159 must mean exactly what they meant when made.**

    `content_hash` is `sha256(canonical_result_json(self))` and `canonical_result_json`
    serialises through `model_dump`, which includes fields left at `None`. An ordinary optional
    field would therefore have inserted `"validation_campaign":null` into every registration's
    canonical form, changed every stored content hash, and made every stored registration fail
    the verification `_load` performs -- so `HypothesisDraft` omits the key entirely when absent.

    The literal below was captured by running `content_id(draft())` on the commit before the
    field existed. Recomputing it from the current model would prove nothing.
    """
    assert content_id(draft()) == GOLDEN_CAMPAIGNLESS_DRAFT_ID
    assert "validation_campaign" not in draft().model_dump()
    # And a campaign, when present, *is* part of identity -- otherwise the selection could
    # change without the registration's identity moving, which is the opposite of the point.
    assert "validation_campaign" in draft(validation_campaign=declared_campaign()).model_dump()
    assert content_id(draft(validation_campaign=declared_campaign())) != (
        GOLDEN_CAMPAIGNLESS_DRAFT_ID
    )


def test_ordinary_registration_is_unchanged_by_campaign_accounting(
    database: Database,
) -> None:
    """A hypothesis with no campaign registers exactly as before, and is charged as before."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        before = registry.cumulative_trials()

        registration = registry.register(draft(), now=NOW, critique=critique())

        assert registration.draft.validation_campaign is None
        assert registry.campaign_trials_spent() == 0
        # The draft declares a search cardinality of 1, and nothing else moved.
        assert registration.cumulative_trials == before + 1


def test_reading_a_registration_mutates_nothing(database: Database) -> None:
    """Reopening must not charge, reserve, or otherwise leave a trace.

    A read that mutated would make inspection itself a form of search, which is the failure this
    repository has already had once in `window_consumption`.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()
        registry.freeze_validation_campaign(subject, now=NOW)
        registry.register(draft(validation_campaign=subject), now=NOW, critique=critique())

        trials = registry.cumulative_trials()
        rows = campaign_rows(session)
        windows = session.execute(text("SELECT count(*) FROM hypothesis_data_windows")).scalar()

        for _ in range(3):
            reopened = registry.get("H-RUN-1", 1)
            assert reopened is not None
            assert reopened.draft.validation_campaign == subject

        assert registry.cumulative_trials() == trials
        assert campaign_rows(session) == rows
        assert (
            session.execute(text("SELECT count(*) FROM hypothesis_data_windows")).scalar()
            == windows
        )


def test_a_multi_dataset_campaign_is_refused_rather_than_guessed_at(
    database: Database,
) -> None:
    """One ledger row carries one dataset, and that is what attributes a count to the window it
    spent. Splitting would charge selection freedom once per dataset; a composite label would
    corrupt the attribution. Refused until there is a design decision."""
    mixed = declared_campaign(
        scenarios=(
            scenario(),
            scenario(
                kind=ScenarioKind.GENERALIZATION,
                adverse=False,
                dataset="sip-us-equities-daily-other",
                period=ExperimentPeriod(
                    name="g", start=date(2020, 1, 2), end=date(2021, 12, 31)
                ),
            ),
        )
    )

    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        with pytest.raises(RegistrationRefused, match="one search over one dataset"):
            registry.freeze_validation_campaign(mixed, now=NOW)
        assert campaign_rows(session) == 0


def test_the_ledger_row_records_enough_to_reproduce_the_selection(
    database: Database,
) -> None:
    """The frozen design lives in the row, so a selection is reproducible from the ledger alone
    rather than only from a registration that may not exist yet."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = seeded_campaign(seed=20260903)
        search_id = registry.freeze_validation_campaign(subject, now=NOW)

        row = (
            session.execute(
                select(search_runs).where(search_runs.c.search_id == search_id)
            )
            .mappings()
            .one()
        )

    assert row["actor"] == VALIDATION_CAMPAIGN_ACTOR
    assert row["configuration_hash"] == subject.campaign_id
    assert row["candidates_evaluated"] == 1
    assert row["dataset"] == DATASET
    assert row["data_role"] == "PROTECTED_EVALUATION"
    rebuilt = ValidationCampaign.model_validate_json(str(row["detail_json"]))
    assert rebuilt.campaign_id == subject.campaign_id
    assert rebuilt == subject


# --- the ledger row is an authoritative record, or it is nothing ----------------------------
#
# Matching `configuration_hash` alone was a fail-open gap, and the shape of it is worth stating
# because it is not obvious: `campaign_trials_spent()` counts rows by `actor`, so a row carrying
# the right `search_id` and `configuration_hash` under some *other* actor satisfied the freeze
# and the registration check while contributing no burden. A campaign accepted as preregistered
# and charged nothing is the exact failure this slice exists to prevent.
#
# Each case below corrupts exactly one column of an otherwise valid row, so a refusal can only
# be attributable to that column.


def frozen_row_with(session, registry: HypothesisRegistry, **corruption: object):
    """Freeze a real campaign, then corrupt one column of its authoritative row."""
    subject = declared_campaign()
    registry.freeze_validation_campaign(subject, now=NOW)
    session.execute(
        search_runs.update()
        .where(search_runs.c.search_id == subject.selection_search_id)
        .values(**corruption)
    )
    return subject


@pytest.mark.parametrize(
    ("corruption", "column"),
    [
        # The load-bearing one: burden is counted by actor, so a foreign actor is a row that
        # proves preregistration while paying nothing.
        ({"actor": "someone-else"}, "actor"),
        ({"actor_version": "99"}, "actor_version"),
        # A campaign is one frozen selection comparison. A ledger disagreeing about the count
        # is a disagreement between campaign semantics and the evidence for them.
        ({"candidates_evaluated": 5}, "candidates_evaluated"),
        ({"dataset": "some-other-dataset"}, "dataset"),
        # Most-protected-role attribution decides which window the burden is charged against.
        ({"data_role": "VALIDATION"}, "data_role"),
        ({"start_date": "2017-01-01"}, "start_date"),
        ({"end_date": "2025-01-01"}, "end_date"),
    ],
)
def test_a_corrupted_column_cannot_prove_preregistration(
    database: Database, corruption: dict[str, object], column: str
) -> None:
    """Every material column is verified, and a mismatch fails closed on both paths.

    Asserted on the freeze *and* on registration, because the two used to disagree about what a
    valid record is, and one of them being lenient is enough.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = frozen_row_with(session, registry, **corruption)

        with pytest.raises(RegistrationRefused, match="not an authoritative record") as freeze:
            registry.freeze_validation_campaign(subject, now=NOW)
        assert column in str(freeze.value)

        with pytest.raises(RegistrationRefused, match="not an authoritative record"):
            registry.register(draft(validation_campaign=subject), now=NOW, critique=critique())


def test_a_foreign_actor_row_cannot_become_an_uncharged_accepted_campaign(
    database: Database,
) -> None:
    """The gap stated as the property it protects, not as a column comparison.

    A row under a foreign actor contributes nothing to `campaign_trials_spent()`. If it could
    still satisfy the freeze, the campaign would be accepted as preregistered while its
    selection freedom went uncounted -- and every later registration would be judged against a
    bar that never saw it.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = frozen_row_with(session, registry, actor="not-the-campaign-actor")

        # The row exists and its configuration hash is right, which is all the old check asked.
        assert registry.campaign_trials_spent() == 0, "a foreign-actor row charges nothing"

        with pytest.raises(RegistrationRefused, match="not an authoritative record"):
            registry.freeze_validation_campaign(subject, now=NOW)
        with pytest.raises(RegistrationRefused, match="not an authoritative record"):
            registry.register(draft(validation_campaign=subject), now=NOW, critique=critique())


def test_a_tampered_detail_json_cannot_prove_preregistration(database: Database) -> None:
    """The stored design must rebuild the campaign it claims to be.

    Without this, every verified column could match while the row held the design of a different
    selection -- so the ledger would reproduce the wrong campaign, which is the one thing
    `detail_json` is there for.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        other = seeded_campaign(seed=7)

        subject = frozen_row_with(session, registry, detail_json=other.model_dump_json())
        with pytest.raises(RegistrationRefused, match="rebuilds as"):
            registry.freeze_validation_campaign(subject, now=NOW)

        session.execute(
            search_runs.update()
            .where(search_runs.c.search_id == subject.selection_search_id)
            .values(detail_json="{not json at all")
        )
        with pytest.raises(RegistrationRefused, match="readable frozen"):
            registry.freeze_validation_campaign(subject, now=NOW)


def test_an_intact_row_still_verifies(database: Database) -> None:
    """The control for every corruption above. A check refusing everything would satisfy them
    all, so the untouched row must still be accepted and still charge exactly once."""
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()
        first = registry.freeze_validation_campaign(subject, now=NOW)

        assert registry.freeze_validation_campaign(subject, now=NOW) == first
        assert registry.campaign_trials_spent() == 1
        registry.register(draft(validation_campaign=subject), now=NOW, critique=critique())


# --- ordering: proven history is not post-hoc -------------------------------------------------


def test_an_identical_campaign_stays_reusable_after_its_window_is_consumed(
    database: Database,
) -> None:
    """A campaign frozen *before* measurement remains verifiable after it.

    The consumed-window barrier used to run first, so re-freezing a legitimately preregistered
    campaign was refused as post-hoc once its results existed -- the ledger row proving the
    campaign predates the outcome being read as evidence that it does not. That made a correct
    campaign impossible to re-verify at exactly the moment it had something to say.

    Nothing here weakens contamination: the row is reusable only because it already exists and
    verifies completely as this exact campaign.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        subject = declared_campaign()
        first = registry.freeze_validation_campaign(subject, now=NOW)
        assert registry.campaign_trials_spent() == 1

        # The outcome is now visible on a window the campaign covers.
        registry.register(draft(), now=NOW, critique=critique())
        consume_window(session, registry, "H-RUN-1")

        again = registry.freeze_validation_campaign(subject, now=NOW)

        assert again == first, "the same campaign keeps its original record"
        assert campaign_rows(session) == 1, "no second row"
        assert registry.campaign_trials_spent() == 1, "and no second charge"


def test_a_new_campaign_after_consumption_is_still_refused(database: Database) -> None:
    """The control for the case above, and the protection it must not have weakened.

    A materially different selection has a different `campaign_id`, so it finds no row, falls
    through to the barrier, and is refused. Idempotency is available only to a campaign that can
    show it was already there.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        registry.freeze_validation_campaign(declared_campaign(), now=NOW)
        registry.register(draft(), now=NOW, critique=critique())
        consume_window(session, registry, "H-RUN-1")

        redrawn = declared_campaign(
            scenarios=(
                scenario(minimum_trades=31),
                scenario(
                    kind=ScenarioKind.GENERALIZATION,
                    adverse=False,
                    period=ExperimentPeriod(
                        name="g", start=date(2020, 1, 2), end=date(2021, 12, 31)
                    ),
                ),
            )
        )
        assert redrawn.campaign_id != declared_campaign().campaign_id

        with pytest.raises(RegistrationRefused, match="already-consumed window"):
            registry.freeze_validation_campaign(redrawn, now=NOW)

        assert campaign_rows(session) == 1, "the refused redraw charged nothing"
