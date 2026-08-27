"""Where the economic thesis stands, and every way engineering could be mistaken for it (#52).

The failure mode is not a wrong number. It is a project that keeps getting more impressive and
no more profitable, with nothing on any screen able to say so.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.milestone import LADDER, Milestone, MilestoneCheck, assess_milestone
from quantbot.research.promotion import (
    MINIMUM_FORWARD_DAYS,
    PromotionLedger,
    PromotionState,
    Stage,
)
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.storage import Database, StorageRepository
from tests.unit.research.test_promotion import (  # `ratified_objective` is used as a fixture
    CLAIM_ID,
    qualify,
    ratified_objective,  # noqa: F401
    seed_ledger,
    state,
)
from tests.unit.research.test_registry import make_draft, register, window

NOW = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)
TODAY = NOW.date()


@pytest.fixture
def database(tmp_path: Path):
    db = Database(tmp_path / "milestone.db")
    yield db
    db.close()


@pytest.fixture
def ratified_path(request: pytest.FixtureRequest) -> Path:
    """The shipped mandate with an operator signature, borrowed from the promotion tests.

    Named differently from the fixture it wraps so it is obvious at the call site that this is
    somebody's signature on a copy, not `config/economic-objective-v1.yaml`, which stays
    PROVISIONAL and would refuse to qualify anything.
    """
    path: Path = request.getfixturevalue("ratified_objective")
    return path


def refute(session, record_id: str = "refuted-1", hypothesis_id: str | None = None) -> None:
    """A refutation on the record: the only evidence that this platform can reject anything."""
    ResearchMemory(session).record(
        ResearchRecord(
            record_id=record_id,
            kind=RecordKind.FINDING,
            subject="momentum ranking",
            statement="Top-minus-bottom is t=0.77 against a 2.87 bar.",
            verdict=Verdict.REFUTED,
            hypothesis_id=hypothesis_id,
            hypothesis_version=None if hypothesis_id is None else 1,
            source="REFUTED.md #22",
            recorded_at=NOW,
        )
    )


def survived(session, hypothesis_id: str, record_id: str = "survived-1") -> None:
    ResearchMemory(session).record(
        ResearchRecord(
            record_id=record_id,
            kind=RecordKind.FINDING,
            subject="trend following across a basket",
            statement="The mechanism held under regime split and cross-asset generality.",
            verdict=Verdict.SURVIVED,
            hypothesis_id=hypothesis_id,
            hypothesis_version=1,
            source="cycle 18",
            recorded_at=NOW,
        )
    )


def freeze(session, *, hypothesis_id: str = CLAIM_ID, universe: tuple[str, ...] = ("SPY", "QQQ")):
    return register(
        HypothesisRegistry(session),
        make_draft(
            hypothesis_id=hypothesis_id,
            universe=universe,
            windows=(
                window(DataRole.FORWARD_PAPER, "2026-08-19", "2026-12-31", dataset="alpaca-paper"),
            ),
        ),
        now=NOW,
    )


def survivor_on_the_ladder(session, *, stage: Stage = Stage.RESEARCH_SURVIVOR) -> PromotionState:
    """A strategy standing above the survivor gate with its claim durably bound."""
    return PromotionLedger(session).enter(state(stage))


def test_an_empty_project_is_below_m0_rather_than_defaulting_to_it(database: Database) -> None:
    """There is no level under M0, so `None` is what "not yet" has to look like.

    Defaulting to M0 would report a platform as operational on the strength of having been
    asked. A platform that has never rejected a hypothesis has not shown it can reject one, and
    volume of positive results is the opposite of evidence for that.
    """
    with database.transaction() as session:
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is None
    assert "BELOW_M0" in report.explain()
    assert report.checks[0].blocking, "M0 must say why, not merely fail"
    assert "no refutation" in report.checks[0].blocking[0]


def test_a_platform_that_has_refuted_something_reaches_m0_and_stops_there(
    database: Database,
) -> None:
    """M0 is about the platform. Reaching it says nothing whatever about an edge."""
    with database.transaction() as session:
        freeze(session)
        refute(session)
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
    assert report.candidate_edges == ()
    assert report.next_check is not None
    assert report.next_check.level is Milestone.M1_CANDIDATE_EDGE_DETECTED


def test_a_candidate_edge_needs_a_survivor_not_a_promising_backtest(database: Database) -> None:
    """M1 is the survivor gate, which is a mechanism rather than an opinion.

    Standing above `RESEARCH_SURVIVOR` means the #7 probes ran and passed, the result came from
    the production engine, the test matched the dependence structure of the data, and the frozen
    luck bar was cleared. None of that can be asserted into existence.
    """
    with database.transaction() as session:
        freeze(session)
        refute(session)
        # Below the survivor gate: registered is not survived.
        PromotionLedger(session).enter(state(Stage.REGISTERED))
        stalled = assess_milestone(session, as_of=TODAY)
        assert stalled.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
        assert stalled.candidate_edges == ()

        PromotionLedger(session).promote(
            "adaptive-momentum",
            Stage.RESEARCH_SURVIVOR,
            actor="director",
            reason="cleared every probe",
            now=NOW,
            outcome=_outcome(),
        )
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M1_CANDIDATE_EDGE_DETECTED
    assert report.candidate_edges == (f"adaptive-momentum -> {CLAIM_ID}",)


def test_a_refuted_claim_removes_the_candidate_edge_it_was_behind(database: Database) -> None:
    """The milestone is derived now, not when the stage was granted.

    A stage granted months ago on a claim since refuted is exactly the stale display #53 is
    about, and a milestone reading it as an edge would be that defect on the headline number.
    """
    with database.transaction() as session:
        freeze(session)
        refute(session, record_id="refuted-other")
        survivor_on_the_ladder(session)
        assert assess_milestone(session, as_of=TODAY).current is (
            Milestone.M1_CANDIDATE_EDGE_DETECTED
        )

        refute(session, record_id="refuted-the-claim", hypothesis_id=CLAIM_ID)
        after = assess_milestone(session, as_of=TODAY)

    assert after.candidate_edges == ()
    assert after.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL


def test_generality_cannot_be_shown_by_one_instrument(database: Database) -> None:
    """A single-asset claim never had `cross-asset-generality` in its probe plan.

    `compile_experiment` adds that probe exactly when the universe has more than one instrument,
    and `survivor_objections` refuses a survivor whose planned probes did not all pass. So a
    one-asset survivor cleared a plan that could not contain the check, and reporting M2 for it
    would claim a result nobody measured.
    """
    with database.transaction() as session:
        freeze(session, universe=("SPY",))
        refute(session)
        survivor_on_the_ladder(session)
        survived(session, CLAIM_ID)
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M1_CANDIDATE_EDGE_DETECTED
    stuck = report.next_check
    assert stuck is not None and stuck.level is Milestone.M2_EDGE_GENERALIZES
    assert "single instrument" in stuck.blocking[0]


def test_generality_needs_a_durable_finding_as_well_as_a_wide_universe(
    database: Database,
) -> None:
    """Two independent durable facts, because either alone is a plan rather than a result."""
    with database.transaction() as session:
        freeze(session, universe=("SPY", "QQQ", "IWM"))
        refute(session)
        survivor_on_the_ladder(session)
        without_record = assess_milestone(session, as_of=TODAY)
        assert without_record.current is Milestone.M1_CANDIDATE_EDGE_DETECTED
        assert without_record.next_check is not None
        assert "no durable SURVIVED finding" in without_record.next_check.blocking[0]

        survived(session, CLAIM_ID)
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M2_EDGE_GENERALIZES


def test_forward_observation_needs_authentic_sessions_and_not_a_stage(
    database: Database,
) -> None:
    """M3 counts what the ledger holds. A stage name is not forward evidence."""
    with database.transaction() as session:
        freeze(session, universe=("SPY", "QQQ"))
        refute(session)
        survived(session, CLAIM_ID)
        PromotionLedger(session).enter(state(Stage.PAPER_OBSERVATION))
        thin = assess_milestone(session, as_of=TODAY)
        assert thin.current is Milestone.M2_EDGE_GENERALIZES
        assert thin.next_check is not None
        assert "0/30 authentic forward days" in thin.next_check.blocking[0]

        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M3_FORWARD_EDGE_OBSERVED


def test_m4_cannot_clear_while_intended_capital_capacity_is_unmeasured(
    database: Database, ratified_path: Path
) -> None:
    """The permanent `unassessed` entry, and the reason it is permanent (#51).

    The paper account holds $100 and the mandate's range reaches $10,000. An effect that
    survives at $100 of notional and dies at $10,000 is not an edge worth the machinery, and no
    amount of forward evidence at the smaller size distinguishes the two. Reporting M4 without
    that measurement would be the surrogate-objective failure this module exists to prevent,
    arriving through the module itself.
    """
    with database.transaction() as session:
        # Genuinely qualified through the real gates, so nothing *else* is holding M4 back:
        # #47's forward evidence cleared, under an operator-ratified mandate. The only thing
        # left is the measurement nobody has taken, and that has to be enough on its own.
        qualify(session, objective_path=ratified_path)
        survived(session, CLAIM_ID)
        refute(session, record_id="refuted-elsewhere")
        report = assess_milestone(session, as_of=TODAY)

    m4 = next(
        check for check in report.checks if check.level is Milestone.M4_FORWARD_EDGE_SUPPORTED
    )
    assert m4.evidence == ("adaptive-momentum",), "the strategy really did reach PAPER_QUALIFIED"
    assert not m4.blocking, "nothing except the unmeasured capacity question stands in the way"
    assert m4.unassessed and "#51" in m4.unassessed[0]
    assert not m4.reached, "an unmeasured capacity question must not read as a cleared gate"
    # `qualify` freezes a single-instrument claim, so M2 is separately unreachable here and the
    # contiguous answer stops at M1. That is beside the point being made: M4's own check has
    # nothing blocking it and still does not pass.
    assert report.current is Milestone.M1_CANDIDATE_EDGE_DETECTED
    assert report.forward_supported == ("adaptive-momentum",)


def test_the_ladder_is_contiguous_so_a_gap_cannot_be_stepped_over(database: Database) -> None:
    """A level above an unreached one does not count, however good its own evidence is.

    Built as a genuine gap rather than an empty project: M0 and M1 are earned, M2 is not
    (the claim covers one instrument, so generality was never in its probe plan), and M3 *is*
    -- thirty authentic forward sessions really are accruing. Reporting M3 would announce that
    a mechanism generalises, on the strength of data collected while nobody checked whether it
    did. A broken evidence chain read at its top is how a system claims a conclusion it never
    derived.
    """
    with database.transaction() as session:
        freeze(session, universe=("SPY",))
        refute(session)
        survived(session, CLAIM_ID)
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        PromotionLedger(session).enter(state(Stage.PAPER_OBSERVATION))
        report = assess_milestone(session, as_of=TODAY)

    levels = {check.level: check.reached for check in report.checks}
    assert levels[Milestone.M1_CANDIDATE_EDGE_DETECTED]
    assert not levels[Milestone.M2_EDGE_GENERALIZES], "the gap this test is about"
    assert levels[Milestone.M3_FORWARD_EDGE_OBSERVED], "and a genuinely reached level above it"

    assert report.current is Milestone.M1_CANDIDATE_EDGE_DETECTED
    assert report.next_check is not None
    assert report.next_check.level is Milestone.M2_EDGE_GENERALIZES


def test_forward_evidence_with_no_candidate_behind_it_reaches_nothing(
    database: Database,
) -> None:
    """The other half: thirty real sessions on a deployment with no frozen claim at all."""
    with database.transaction() as session:
        refute(session)
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS)
        PromotionLedger(session).enter(
            state(Stage.PAPER_OBSERVATION, hypothesis_id=None, hypothesis_version=None)
        )
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
    assert report.next_check is not None
    assert report.next_check.level is Milestone.M1_CANDIDATE_EDGE_DETECTED


def test_no_edge_established_is_a_destination_rather_than_a_failure(
    database: Database,
) -> None:
    """The project must be able to conclude this without it reading as a bug.

    It reports that everything reachable has been searched and nothing was found. Having a name
    for it is what stops the alternative: adding another framework because the ladder has no way
    to say stop.
    """
    with database.transaction() as session:
        refute(session)
        with_capacity = assess_milestone(session, as_of=TODAY)
        # Crypto, macro and filings are all untouched, so the search is not over.
        assert with_capacity.clean_capacity
        assert not with_capacity.no_edge_established

    # And with nothing clean left to search, the honest conclusion becomes available. Built by
    # replacing the capacity field rather than by mocking the inventory: the rule under test is
    # "M0 plus no capacity", and stubbing the query would test the stub.
    exhausted = replace(with_capacity, clean_capacity=())

    assert exhausted.no_edge_established
    assert exhausted.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
    # An unsupported capability does not suppress the conclusion -- it qualifies it. A system
    # that can never say "no edge" while any capability is unimplemented always has a reason to
    # keep going, which is the dishonesty in the other direction.
    assert "options-implied-daily" in exhausted.capacity_unavailable


def test_an_inventory_that_answered_nothing_cannot_produce_the_conclusion(
    database: Database,
) -> None:
    """#52.6: the project's most consequential statement must not come from a broken query.

    "We searched everywhere and found nothing" and "we could not see anywhere to search" are
    different statements, and only the first is a finding. An empty inventory is the loudest
    possible version of the second.
    """
    import quantbot.research.milestone as milestone_module

    original = milestone_module.capacity_inventory
    milestone_module.capacity_inventory = lambda session, *, as_of: ()  # type: ignore[assignment]
    try:
        with database.transaction() as session:
            refute(session)
            report = assess_milestone(session, as_of=TODAY)
    finally:
        milestone_module.capacity_inventory = original  # type: ignore[assignment]

    assert report.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
    assert report.clean_capacity == ()
    assert report.capacity_undetermined == ("no sources were inventoried at all",)
    assert not report.no_edge_established, "a broken inventory is not a research result"


def test_a_milestone_is_never_reached_without_a_named_artifact() -> None:
    """`reached` requires evidence, so no level passes on the mere absence of objections.

    "Nothing blocks it" and "something shows it" are different statements, and the first is how
    an empty database climbs a ladder. Asserted on the invariant directly rather than through a
    scenario: no current check produces this shape, which is exactly why the guard has to be
    tested where it can be exercised instead of where it happens not to fire.
    """
    silent = MilestoneCheck(
        level=Milestone.M2_EDGE_GENERALIZES, evidence=(), blocking=(), unassessed=()
    )
    assert not silent.reached, "a level with nothing behind it is an opinion, not a milestone"

    named = MilestoneCheck(
        level=Milestone.M2_EDGE_GENERALIZES,
        evidence=("H-2026-047 over 3 instruments",),
        blocking=(),
        unassessed=(),
    )
    assert named.reached


def test_the_ladder_is_declared_in_order_and_not_named_after_releases() -> None:
    """A version number moves when code ships. These move only on evidence about the market."""
    assert [level.value.split("_")[0] for level in LADDER] == ["M0", "M1", "M2", "M3", "M4", "M5"]
    for level in LADDER:
        assert "v0" not in level.value and "RELEASE" not in level.value


def test_a_multi_instrument_registration_implies_the_generality_probe(database: Database) -> None:
    """The inference M2 rests on, pinned where it can break.

    `_generalises_check` reads the probe *requirement* the universe implies rather than a stored
    probe list, because probe results are not in a queryable table. That inference is only sound
    while `compile_experiment` is the one compiler and adds `cross-asset-generality` exactly when
    the universe is wider than one instrument. If that stops being true, this fails first.
    """
    from quantbot.research.builder import GENERALITY_PROBE, compile_experiment
    from tests.unit.research.test_builder import snapshot

    with database.transaction() as session:
        wide = freeze(session, universe=("SPY", "QQQ"))
        narrow = freeze(session, hypothesis_id="H-2026-601", universe=("SPY",))

    paper = snapshot(dataset="alpaca-paper", role="FORWARD_PAPER")
    assert GENERALITY_PROBE in compile_experiment(wide, datasets=[paper]).probes
    assert GENERALITY_PROBE not in compile_experiment(narrow, datasets=[paper]).probes


def _outcome():
    """A surviving experiment outcome, reusing the builder fixtures rather than hand-building."""
    from tests.unit.research.test_builder import outcome

    return outcome()


def test_forward_days_from_a_different_configuration_do_not_count(database: Database) -> None:
    """M3 inherits the material-change rule rather than restating it.

    Observations of version 1.3.0 are not observations of 1.4.0, and a milestone that counted
    them would resurrect exactly the evidence the ladder throws away on a config change.
    """
    with database.transaction() as session:
        freeze(session, universe=("SPY", "QQQ"))
        refute(session)
        survived(session, CLAIM_ID)
        seed_ledger(session, days=MINIMUM_FORWARD_DAYS, version="1.2.0", config="cfg-old")
        PromotionLedger(session).enter(state(Stage.PAPER_OBSERVATION))
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M2_EDGE_GENERALIZES
    assert report.next_check is not None
    assert "0/30 authentic forward days" in report.next_check.blocking[0]


def test_the_report_names_the_day_it_was_taken(database: Database) -> None:
    """Capacity moves with the calendar, so an undated milestone report is not reproducible."""
    with database.transaction() as session:
        report = assess_milestone(session, as_of=TODAY - timedelta(days=1))

    assert report.as_of == date(2026, 8, 21)


def test_engineering_activity_cannot_advance_the_milestone(database: Database) -> None:
    """The invariant #52 exists for, asserted rather than argued (GPT-5.6 Sol's review).

    Issues closed, tests added, dashboards built, risk gates wired, workers integrated, compute
    spent — every one of those moves a number this project reports about itself, and none of them
    is evidence about the market. The failure mode is not that somebody decides to cheat; it is
    that engineering volume accumulates and the profitability state drifts up with it because
    nothing forbids it.

    So: fill the database with activity of every kind the system records, and assert the
    milestone does not move a millimetre.
    """
    with database.transaction() as session:
        refute(session)
        before = assess_milestone(session, as_of=TODAY)
        assert before.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL

        repository = StorageRepository(session)
        # Operational activity: runs, incidents, reconciliation — the daemon working.
        for index in range(25):
            repository.create_incident(
                f"INC-{index}",
                severity="INFO",
                kind="CYCLE_COMPLETED",
                message="a cycle ran and nothing was wrong",
                occurred_at=NOW,
            )
        # Research machinery being exercised: registrations, tasks, compute, literature.
        for index in range(5):
            freeze(
                session,
                hypothesis_id=f"H-2026-90{index}",
                universe=(f"SYM{index}",),
            )

        after = assess_milestone(session, as_of=TODAY)

    assert after.current == before.current, "engineering activity moved the profitability state"
    assert after.candidate_edges == before.candidate_edges
    assert after.forward_supported == before.forward_supported
    # And the M1 check still names the thing that is actually missing, rather than counting rows.
    stuck = after.next_check
    assert stuck is not None and stuck.level is Milestone.M1_CANDIDATE_EDGE_DETECTED
    assert "RESEARCH_SURVIVOR" in stuck.blocking[0]


def test_registrations_alone_are_not_a_candidate_edge(database: Database) -> None:
    """Freezing questions is research volume, not research results.

    A project can register a hundred hypotheses and have answered none of them. M1 is the
    survivor gate — a claim that passed its own pre-registered test — and the count of
    registrations is exactly the surrogate metric #52 was opened about.
    """
    with database.transaction() as session:
        refute(session)
        for index in range(20):
            freeze(session, hypothesis_id=f"H-2026-80{index}", universe=(f"TCK{index}",))
        report = assess_milestone(session, as_of=TODAY)

    assert report.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
    assert report.candidate_edges == ()


def test_forward_days_must_be_authentic_and_not_backfilled(database: Database) -> None:
    """M3 reads the trading ledger, so a backtest cannot reach it (#52, Sol's invariant 4).

    `ForwardObservation` refuses any role but `FORWARD_PAPER`, and `count_forward_days` refuses a
    sequence a caller assembled rather than one `observe_forward_days` produced. This asserts the
    milestone inherits both rather than reimplementing a looser count of its own.
    """
    from quantbot.research.promotion import DataRole, ForwardObservation, count_forward_days

    with pytest.raises(ValueError, match="cannot increment this counter"):
        ForwardObservation(
            strategy_id="adaptive-momentum",
            trading_date=TODAY,
            trades=1,
            role=DataRole.PROTECTED_EVALUATION,
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )

    hand_built = [
        ForwardObservation(
            strategy_id="adaptive-momentum",
            trading_date=TODAY,
            trades=1,
            role=DataRole.FORWARD_PAPER,
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )
    ]
    with pytest.raises(TypeError, match="not forward evidence"):
        count_forward_days(
            hand_built,  # type: ignore[arg-type]
            strategy_version="1.3.0",
            configuration_hash="cfg-abc",
        )
