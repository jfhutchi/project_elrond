"""Whether an effect survives at the size it would be traded, and how that could be flattered (#51).

The project can prove evidence is authentic and cannot yet prove a $100 effect survives at
$10,000. Every test here is about the second question not being answered dishonestly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.capital import (
    BASELINE_SLIPPAGE_BPS,
    STRESS_MULTIPLIERS,
    UNAVAILABLE_LEVELS,
    CapacityVerdict,
    CostLevel,
    CostModel,
    InstrumentCapacity,
    assess_capital_capacity,
    capital_ladder,
)
from quantbot.research.mandate import (
    DEFAULT_MANDATE_PATH,
    MandateStatus,
    load_economic_objective,
)

REPOSITORY = Path(__file__).resolve().parents[3]


def objective(**overrides: object):
    """The shipped mandate, ratified, unless a test is about ratification."""
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH).model_copy(
        update={
            "status": MandateStatus.RATIFIED,
            "ratified_by": "hutch",
            "ratified_at": date(2026, 8, 27),
        }
    )
    return frozen.model_copy(update=overrides) if overrides else frozen


def book(*volumes: str, weight: str | None = None):
    """An equal-weighted book over the given per-session dollar volumes."""
    share = Decimal(weight) if weight else Decimal(1) / Decimal(len(volumes))
    return {
        f"SYM{index}": InstrumentCapacity(weight=share, session_volume_usd=Decimal(volume))
        for index, volume in enumerate(volumes)
    }


def assess(**overrides: object):
    fields: dict[str, object] = {
        "strategy_id": "adaptive-momentum",
        "objective": objective(),
        "cost_model": CostModel(),
        "gross_excess_bps": Decimal("5000"),
        "instruments": book("50000000", "50000000"),
        "turnover": Decimal("12"),
    }
    fields.update(overrides)
    return assess_capital_capacity(**fields)  # type: ignore[arg-type]


def test_the_ladder_comes_from_the_mandate_not_from_a_constant() -> None:
    """A hard-coded ladder is a second declaration of what capital this project is for.

    The two would drift, and the mandate is the one the operator signs. Endpoints are exact so
    rounding cannot move the boundaries the mandate itself set.
    """
    rungs = capital_ladder(objective())

    assert rungs[0] == Decimal("100"), "the mandate's floor, exactly"
    assert rungs[-1] == Decimal("10000"), "the mandate's ceiling, exactly"
    assert list(rungs) == sorted(rungs), "a ladder ascends"

    # Changing the mandate moves the ladder, which is the whole point of deriving it.
    wider = capital_ladder(objective(intended_capital_range_usd=(500, 50000)))
    assert wider[0] == Decimal("500")
    assert wider[-1] == Decimal("50000")


def test_levels_outside_the_mandate_are_context_and_never_scored() -> None:
    """A strategy that fails at $100,000 is not unfit if $100,000 was never intended.

    Letting an out-of-mandate rung decide the verdict manufactures a negative result exactly as
    scoring only the smallest rung would manufacture a positive one.
    """
    # Deep enough that the mandate's whole range clears, shallow enough that ten times the
    # ceiling does not. If the out-of-mandate rung decided the verdict, this would fail.
    beyond = assess(
        ladder=[Decimal("100"), Decimal("10000"), Decimal("100000")],
        gross_excess_bps=Decimal("200"),
        instruments=book("20000000"),
    )

    inside = [rung for rung in beyond.rungs if rung.inside_mandate]
    outside = [rung for rung in beyond.rungs if not rung.inside_mandate]

    assert [rung.capital_usd for rung in outside] == [Decimal("100000")]
    assert not outside[0].positive, "the out-of-mandate rung is genuinely negative"
    assert all(rung.positive for rung in inside)
    assert beyond.verdict is CapacityVerdict.SURVIVES_MANDATE, (
        "a level the operator never intended must not decide the verdict"
    )


def test_no_edge_anywhere_is_not_a_capacity_finding() -> None:
    """The project's actual situation, and the answer that must not read as a breakeven.

    The deployed rotation has alpha t=0.05. Reporting "capacity exhausted at $0" would describe
    a capacity limit; what is true is that there was never an edge for capacity to erode.
    """
    nothing = assess(gross_excess_bps=Decimal("5"))

    assert nothing.verdict is CapacityVerdict.NO_EDGE_TO_EXHAUST
    assert nothing.breakeven_usd is None, "a breakeven would claim a capacity limit"
    assert not nothing.supports_intended_capital


def test_the_headline_is_the_capital_where_the_edge_dies() -> None:
    """One number, because a reader given seven rows picks the one they like."""
    thin = assess(gross_excess_bps=Decimal("200"), instruments=book("200000"))

    assert thin.verdict is CapacityVerdict.EXHAUSTS_INSIDE_MANDATE
    assert thin.breakeven_usd == Decimal("1000")
    assert "edge crosses zero at $1000" in thin.explain()
    # And it is the *first* rung that goes negative, not the last positive one.
    crossing = next(rung for rung in thin.rungs if rung.capital_usd == thin.breakeven_usd)
    assert not crossing.positive


def test_a_draft_mandate_cannot_certify_capacity_but_a_project_default_can() -> None:
    """Capacity needs a capital range that was frozen, not one that was signed (#72).

    The earlier rule required ratification, which imported #54's deadlock into a second module:
    a capacity verdict could never be produced because the range it measured against was waiting
    on a signature. What actually matters is that the range cannot be edited after the answer is
    known -- true of a `PROJECT_DEFAULT`, not true of a draft.
    """
    default = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)
    assert not default.ratified(), "still not an approval"

    measured = assess(objective=default)
    assert measured.verdict is not CapacityVerdict.UNASSESSED
    assert not any("PROJECT_DEFAULT" in item for item in measured.blocking)

    # And the assessment can say what authority its capital range carried, which is what a
    # reader needs instead of the gate.
    assert "not approved by the operator" in default.authority

    draft = default.model_copy(update={"status": MandateStatus.PROVISIONAL})
    unusable = assess(objective=draft)

    assert unusable.verdict is CapacityVerdict.UNASSESSED
    assert not unusable.supports_intended_capital
    assert any("PROVISIONAL" in item for item in unusable.blocking)


def test_every_missing_input_blocks_rather_than_being_substituted() -> None:
    """A capacity number computed from a guessed turnover has the authority of a measurement.

    That is the failure this issue exists to prevent, one level above the cost model.
    """
    for field, fragment in (
        ("gross_excess_bps", "nothing for capacity to erode"),
        ("instruments", "participation at any capital"),
        ("turnover", "round trips a year"),
    ):
        result = assess(**{field: None})
        assert result.verdict is CapacityVerdict.UNASSESSED, field
        assert any(fragment in item for item in result.blocking), field

    # An empty book is not a book, and the guard must not be a `None` check only.
    assert assess(instruments={}).verdict is CapacityVerdict.UNASSESSED


def test_cost_cannot_be_lowered_below_the_baseline_without_a_registration() -> None:
    """Raising an assumed cost needs no permission; lowering it is a research claim.

    `scripts/slippage_report.py` already refuses to recalibrate from a handful of favourable
    fills — *"lowering an assumed cost is the direction that flatters every backtest"*. This
    makes the same rule mechanical rather than a comment in a script nobody has to run.
    """
    with pytest.raises(ValueError, match="needs a registered hypothesis"):
        CostModel(slippage_bps_per_side=Decimal("1"))

    # With a registration naming the claim, it is allowed — the rule is pre-registration, not
    # prohibition.
    authorised = CostModel(
        slippage_bps_per_side=Decimal("1"), recalibration_hypothesis_id="H-2026-055"
    )
    assert authorised.slippage_bps_per_side < BASELINE_SLIPPAGE_BPS

    # Raising it is always allowed.
    assert CostModel(slippage_bps_per_side=Decimal("25")).slippage_bps_per_side == Decimal("25")


def test_an_uncalibrated_level_cannot_claim_to_be_calibrated() -> None:
    """Levels 1 and 3 need inputs this project does not have.

    Constructing one would carry an invented coefficient with the authority of a measurement —
    the false precision #51's own cost hierarchy warns against.
    """
    assert UNAVAILABLE_LEVELS == {CostLevel.SPREAD_CALIBRATED, CostLevel.FORWARD_CALIBRATED}
    for level in UNAVAILABLE_LEVELS:
        with pytest.raises(ValueError, match="has no implementation"):
            CostModel(level=level)


def test_drag_grows_with_participation_and_with_turnover() -> None:
    """The stress shape is declared, not fitted, and it must actually bite.

    A stress multiplier that did not increase cost with size would let a large order look as
    cheap as a small one, which is the entire thing this issue says a fixed-bps model cannot see.
    """
    model = CostModel()
    small = model.drag_bps(participation=Decimal("0.001"), turnover=Decimal("12"))
    large = model.drag_bps(participation=Decimal("0.5"), turnover=Decimal("12"))
    traded_more = model.drag_bps(participation=Decimal("0.001"), turnover=Decimal("24"))

    assert large > small, "participation must cost something"
    assert traded_more > small, "turnover compounds capacity cost"

    # Level 0 is participation-blind by construction, which is exactly why it is not enough.
    fixed = CostModel(level=CostLevel.FIXED_BPS)
    assert fixed.drag_bps(participation=Decimal("0.5"), turnover=Decimal("12")) == fixed.drag_bps(
        participation=Decimal("0.001"), turnover=Decimal("12")
    )


def test_the_verdict_reports_how_much_it_leans_on_the_unfitted_constant() -> None:
    """The stress shape has no fitted parameters, so the answer is partly the constant's.

    A single verdict computed under one chosen multiplier reads as a measurement. Reported across
    a range of that multiplier, a reader can see whether the conclusion is the strategy's or the
    assumption's -- which is the whole mitigation for having declared the shape rather than
    measured it.
    """
    # Deep enough to survive at the assumed stress, thin enough that a harsher one kills it.
    marginal = assess(gross_excess_bps=Decimal("700"), instruments=book("900000"))

    assert [point.multiplier for point in marginal.stress_sensitivity] == list(STRESS_MULTIPLIERS)

    # The reported headline is the 1x point, so the sensitivity is anchored to the answer given
    # rather than being an unrelated second computation.
    unit = next(point for point in marginal.stress_sensitivity if point.multiplier == Decimal("1"))
    assert (unit.verdict, unit.breakeven_usd) == (marginal.verdict, marginal.breakeven_usd)

    # And it actually moves: this verdict is the constant's, and the reader is told so.
    assert len({point.verdict for point in marginal.stress_sensitivity}) > 1

    # Harsher assumptions may never produce a better verdict.
    survived = [
        point.verdict is CapacityVerdict.SURVIVES_MANDATE
        for point in marginal.stress_sensitivity
    ]
    assert survived == sorted(survived, reverse=True), "more cost cannot rescue an edge"

    # A blocked assessment has no answer whose sensitivity could be reported.
    assert assess(turnover=None).stress_sensitivity == ()


def test_an_assessment_names_the_mandate_and_the_cost_model_it_used() -> None:
    """A capacity claim that cannot say what it was costed under is unauditable.

    Same reason `ForwardEvidence` names its objective: the assumptions are the result.
    """
    result = assess()

    assert result.objective_identity.startswith("economic-objective-v1-")
    assert result.cost_model_identity.startswith("cost-model-participation_stress-")

    # And changing an assumption changes the identity, so two results cannot be confused.
    stressed = CostModel(participation_stress_bps=Decimal("2000"))
    assert stressed.identity != CostModel().identity


def test_a_configured_commission_actually_changes_the_answer() -> None:
    """Sol's #51 blocker 1: `commission_per_order` was declared and never charged.

    A cost input that can be configured and has no effect is a false control -- worse than an
    absent one, because a reader who sets it believes the result accounts for it. And a per-order
    fee is not a rate: it is a large fraction of a $100 order and a rounding error on a $10,000
    one, so omitting it made exactly the smallest rungs look cheaper than they are.
    """
    free = assess(gross_excess_bps=Decimal("400"), instruments=book("900000"))
    charged = assess(
        gross_excess_bps=Decimal("400"),
        instruments=book("900000"),
        cost_model=CostModel(commission_per_order=Decimal("1")),
    )

    smallest_free = free.rungs[0]
    smallest_charged = charged.rungs[0]
    assert smallest_charged.execution_drag_bps > smallest_free.execution_drag_bps, (
        "a configured commission that changes nothing is a control in name only"
    )

    # And it bites hardest at the bottom of the ladder, which is the capacity claim that matters.
    def uplift(rungs, index):
        return rungs[index].execution_drag_bps

    bottom = uplift(charged.rungs, 0) - uplift(free.rungs, 0)
    top = uplift(charged.rungs, -1) - uplift(free.rungs, -1)
    assert bottom > top, "a fixed fee is proportionally heavier on a smaller account"


def test_participation_is_measured_per_instrument_not_on_the_whole_account() -> None:
    """Sol's #51 blocker 2: dividing total capital by one volume figure is not portfolio capacity.

    It asks what would happen if the entire account were a single order into a single book, which
    is neither what the strategy does nor a conservative stand-in for it. Two books with the same
    *total* liquidity are not equivalent when one holds a sleeve that cannot be filled.
    """
    # Same total session volume in both, distributed differently.
    even = {
        "A": InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("1000000")),
        "B": InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("1000000")),
    }
    lopsided = {
        "A": InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("1990000")),
        "B": InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("10000")),
    }

    balanced = assess(gross_excess_bps=Decimal("400"), instruments=even)
    concentrated = assess(gross_excess_bps=Decimal("400"), instruments=lopsided)

    assert concentrated.rungs[-1].execution_drag_bps > balanced.rungs[-1].execution_drag_bps, (
        "a thin sleeve must cost more than the same dollars spread evenly"
    )

    # The reported participation is the worst instrument's, not an average that hides it.
    worst = concentrated.rungs[-1].participation
    thin_leg = Decimal("10000") * Decimal("0.5") / Decimal("10000")
    assert worst == thin_leg

    # Drag is a capital-weighted average, not a sum. One instrument taking the whole book, and
    # two instruments each taking half of a half-sized book, place identically-sized orders into
    # identically-deep liquidity -- so the portfolio cost must be the same. Summing instead of
    # weighting would make cost scale with how many tickers the same money is spread over.
    whole = {"A": InstrumentCapacity(weight=Decimal("1"), session_volume_usd=Decimal("1000000"))}
    halves = {
        "A": InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("500000")),
        "B": InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("500000")),
    }
    one = assess(gross_excess_bps=Decimal("400"), instruments=whole).rungs[-1]
    two = assess(gross_excess_bps=Decimal("400"), instruments=halves).rungs[-1]
    assert one.participation == two.participation
    assert one.execution_drag_bps == two.execution_drag_bps, (
        "spreading the same money over more tickers is not more expensive by itself"
    )

    # And weight matters, not just liquidity: the same books allocated differently differ.
    tilted = {
        "A": InstrumentCapacity(weight=Decimal("0.9"), session_volume_usd=Decimal("1000000")),
        "B": InstrumentCapacity(weight=Decimal("0.1"), session_volume_usd=Decimal("1000000")),
    }
    assert (
        assess(gross_excess_bps=Decimal("400"), instruments=tilted).rungs[-1].participation
        > balanced.rungs[-1].participation
    )


def test_an_instrument_cannot_carry_impossible_allocation_or_liquidity() -> None:
    """The inputs are measurements, so the shapes that cannot be measurements are refused."""
    with pytest.raises(ValueError):
        InstrumentCapacity(weight=Decimal("0"), session_volume_usd=Decimal("1000"))
    with pytest.raises(ValueError):
        InstrumentCapacity(weight=Decimal("1.5"), session_volume_usd=Decimal("1000"))
    with pytest.raises(ValueError):
        InstrumentCapacity(weight=Decimal("0.5"), session_volume_usd=Decimal("0"))


def test_m4_reads_the_assessment_rather_than_the_issue_number(tmp_path: Path) -> None:
    """Closing #51 must not open M4; an assessment that says the edge survives opens it.

    The three states are distinguishable, and the middle one is the one worth having: an absent
    assessment is `unassessed` ("nobody looked"), a negative assessment is `blocking` ("somebody
    looked and the answer was no"), and only a supporting one clears.
    """
    from datetime import UTC, datetime

    from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
    from quantbot.research.milestone import Milestone, assess_milestone
    from quantbot.storage import Database

    moment = datetime(2026, 8, 27, 14, 30, tzinfo=UTC)
    database = Database(tmp_path / "m4.db")
    with database.transaction() as session:
        ResearchMemory(session).record(
            ResearchRecord(
                record_id="refuted-1",
                kind=RecordKind.FINDING,
                subject="momentum ranking",
                statement="t=0.77 against a 2.87 bar.",
                verdict=Verdict.REFUTED,
                source="REFUTED.md #22",
                recorded_at=moment,
            )
        )

        def m4_of(capacity):
            report = assess_milestone(session, as_of=date(2026, 8, 27), capacity=capacity)
            return next(
                check
                for check in report.checks
                if check.level is Milestone.M4_FORWARD_EDGE_SUPPORTED
            )

        absent = m4_of(None)
        assert absent.unassessed and "#51" in absent.unassessed[0]
        assert not absent.reached

        negative = m4_of(assess(gross_excess_bps=Decimal("5")))
        assert not negative.unassessed, "somebody looked, so this is not an unrun check"
        assert any("NO_EDGE_TO_EXHAUST" in item for item in negative.blocking)
        assert not negative.reached

        supporting = m4_of(assess())
        assert supporting.unassessed == ()
        assert not any("#51" in item for item in supporting.blocking), (
            "a supporting capacity result must stop being a reason to refuse"
        )
    database.close()
