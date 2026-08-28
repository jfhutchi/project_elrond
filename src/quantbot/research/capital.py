"""Whether an effect survives at the size it would actually be traded (#51).

The system can increasingly prove that evidence is authentic. It cannot yet prove that a
small-notional effect survives at intended capital, and those are different claims. A strategy can
have real alpha on a $100 paper account and be economically useless at $10,000, because spread is
instrument- and time-dependent, impact grows with participation, and a fractional order exerts no
capacity pressure at all.

**The ladder comes from the mandate.** `EconomicObjective.intended_capital_range_usd` is the
operator's statement of what capital this project is for; a hard-coded ladder would be a second
place that declares it, and the two would diverge. Levels outside the mandate are reported as
out-of-mandate context and never as pass or fail -- a strategy that fails at $100,000 is not unfit
if $100,000 was never intended, and treating it as such manufactures a negative result as surely
as the other direction manufactures a positive one.

**A provisional mandate cannot certify capacity.** Same rule as `PAPER_QUALIFIED` under #54: the
numbers are computed and reported while observing, and the verdict cannot read as "the edge
survives at intended capital" until the operator owns the range it was measured against.

**The deliverable is one number.** The capital at which benchmark-relative edge crosses zero or
violates a liquidity constraint. A reader given seven rows picks the one they like; a reader given
"the edge dies at $4,200" has to engage with it. The per-level table is supporting detail.

**No fitted impact model, deliberately.** The obvious Level-2 implementation is a square-root
impact law with a calibrated coefficient. This project has zero forward fills and no point-in-time
NBBO history, so any coefficient would be invented and then carried around with the authority of a
measurement -- the false precision #51's own cost hierarchy warns against. What is here instead is
a stress multiplier with a declared shape and **no fitted parameters**: its constants are recorded
as assumptions, frozen and hashed, and the assessment reports how much the answer moves across a
range of them rather than a point estimate.

**Cost may only move conservatively without pre-registration.** Raising the assumed cost needs no
permission. Lowering it is a research claim -- `scripts/slippage_report.py` already refuses to do
it from a handful of favourable fills, and `CostModel` refuses to be constructed below the frozen
baseline without naming the registration that authorised it. That is also the answer to the
opening-auction question: the realised fills are favourable and the mechanism is plausible, and
"plausible mechanism plus three observations" is the shape this project has learned to distrust.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.mandate import EconomicObjective

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
BASIS_POINTS = Decimal("10000")

#: The cost the deployed config charges per side, and the floor this module will not go below
#: without a registered claim. `config/strategy-v1-2.yaml` sets it; it is repeated here as the
#: baseline a recalibration has to argue against rather than silently replace.
BASELINE_SLIPPAGE_BPS = Decimal("5")


class CostLevel(StrEnum):
    """#51's cost hierarchy, with only two of the four reachable and the reason recorded."""

    #: Fixed conservative bps. Simple, auditable, and the default.
    FIXED_BPS = "FIXED_BPS"
    #: Fixed bps plus a participation stress multiplier with declared, unfitted constants.
    PARTICIPATION_STRESS = "PARTICIPATION_STRESS"
    #: Instrument/time spread calibration. Needs point-in-time NBBO history this project does
    #: not store, so constructing one would mean inventing the input.
    SPREAD_CALIBRATED = "SPREAD_CALIBRATED"
    #: Calibrated against realised fills. Needs the >=30 observations `slippage_report.py` is
    #: waiting for; there are three.
    FORWARD_CALIBRATED = "FORWARD_CALIBRATED"


#: Levels with no implementation behind them. Constructing a model at one is refused rather than
#: silently treated as equivalent to the level below, because a cost model that claims to be
#: calibrated and is not is worse than one that admits it is an assumption.
UNAVAILABLE_LEVELS: frozenset[CostLevel] = frozenset(
    {CostLevel.SPREAD_CALIBRATED, CostLevel.FORWARD_CALIBRATED}
)


class CapacityVerdict(StrEnum):
    """What the ladder found, with the two honest non-numeric answers kept distinct."""

    #: Positive edge at every level inside the mandate.
    SURVIVES_MANDATE = "SURVIVES_MANDATE"
    #: Edge is positive at small size and crosses zero inside the mandate. `breakeven_usd` says
    #: where.
    EXHAUSTS_INSIDE_MANDATE = "EXHAUSTS_INSIDE_MANDATE"
    #: There is no positive edge at any level, so there is no capacity question to answer. This
    #: is the project's actual situation, and reporting it as a breakeven of $0 would read as a
    #: capacity finding when it is an absence-of-edge finding.
    NO_EDGE_TO_EXHAUST = "NO_EDGE_TO_EXHAUST"
    #: Something needed to answer could not be measured. Blocking, as everywhere else here.
    UNASSESSED = "UNASSESSED"


#: Multipliers applied to the participation-stress assumption when reporting sensitivity. The
#: shape is declared rather than fitted, so the honest way to present a result computed under it
#: is beside the results computed under harsher and gentler versions of the same assumption.
STRESS_MULTIPLIERS: tuple[Decimal, ...] = (
    Decimal("0.5"),
    Decimal("1"),
    Decimal("2"),
    Decimal("4"),
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CostModel(FrozenModel):
    """Frozen execution-cost assumptions, hashed so a result can name what it was costed under.

    Every constant here is an **assumption**, not an estimate, and the distinction is the point.
    `participation_stress_bps` is what the model charges at 100% participation; the shape is
    linear in participation and stated rather than fitted, because fitting it would need
    observations this project does not have.
    """

    level: CostLevel = CostLevel.PARTICIPATION_STRESS
    slippage_bps_per_side: Decimal = Field(default=BASELINE_SLIPPAGE_BPS, ge=0, allow_inf_nan=False)
    commission_per_order: Decimal = Field(default=Decimal("0"), ge=0, allow_inf_nan=False)
    #: Additional cost charged at full participation in a session's volume. Deliberately large:
    #: an order that is the entire day's volume does not execute at the quote.
    participation_stress_bps: Decimal = Field(
        default=Decimal("1000"), ge=0, allow_inf_nan=False
    )
    #: The registration authorising a cost below the baseline. Required only for that direction.
    recalibration_hypothesis_id: str | None = None

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        if self.level in UNAVAILABLE_LEVELS:
            raise ValueError(
                f"{self.level.value} has no implementation: it needs point-in-time NBBO history "
                "or a realised-fill sample this project does not have, and a cost model that "
                "claims to be calibrated and is not is worse than one that admits it is an "
                "assumption"
            )
        if (
            self.slippage_bps_per_side < BASELINE_SLIPPAGE_BPS
            and self.recalibration_hypothesis_id is None
        ):
            raise ValueError(
                f"{self.slippage_bps_per_side}bps is below the {BASELINE_SLIPPAGE_BPS}bps "
                "baseline; lowering an assumed cost flatters every result computed after it, so "
                "it needs a registered hypothesis rather than an edit"
            )
        return self

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"cost-model-{self.level.value.lower()}-{digest[:16]}"

    def commission_drag_bps(self, *, capital: Decimal, orders_per_year: Decimal) -> Decimal:
        """Commission as annualised basis points of capital.

        A per-order fee is not a rate, and it bites hardest exactly where this issue is pointed:
        a fixed charge is a large fraction of a $100 order and a rounding error on a $10,000 one,
        so leaving it out of the arithmetic while offering it as a configurable field made the
        smallest rungs look cheaper than they are.
        """
        if capital <= 0:
            raise ValueError("commission cannot be expressed as a rate on zero capital")
        return self.commission_per_order * orders_per_year / capital * BASIS_POINTS

    def drag_bps(self, *, participation: Decimal, turnover: Decimal) -> Decimal:
        """Annualised execution drag in basis points, for one round trip repeated `turnover`x.

        Two sides per round trip. The stress term is linear in participation and charged on each
        side, which is pessimistic and meant to be: the alternative is a curve nobody measured.
        """
        per_side = self.slippage_bps_per_side
        if self.level is CostLevel.PARTICIPATION_STRESS:
            per_side += self.participation_stress_bps * participation
        return per_side * 2 * turnover


class InstrumentCapacity(FrozenModel):
    """One instrument's share of the strategy, and the liquidity behind that share.

    Capacity is a property of **orders**, not of an account. A ladder that divided total capital
    by a single volume figure would be asking what happens if the whole account were one order
    into one book -- which is neither what the strategy does nor a conservative stand-in for it,
    because it understates participation for a concentrated sleeve and overstates it for a
    diversified one. Sol's #51 review is explicit that the scalar ratio must not be called
    measured portfolio capacity.
    """

    #: Fraction of capital this instrument carries. The order notional at a given rung is
    #: `capital * weight`, which is the number participation has to be measured against.
    weight: Decimal = Field(gt=0, le=1, allow_inf_nan=False)
    #: Dollar volume in a session for this instrument. Per symbol, because a book that absorbs
    #: $10m and one that absorbs $200k are not interchangeable at the same order size.
    session_volume_usd: Decimal = Field(gt=0, allow_inf_nan=False)


@dataclass(frozen=True, slots=True)
class LadderRung:
    """One capital level, and what the strategy's economics look like there."""

    capital_usd: Decimal
    #: False for levels outside the mandate's intended range. Reported for context, never scored.
    inside_mandate: bool
    #: Share of a session's volume this capital would represent, worst instrument.
    participation: Decimal
    execution_drag_bps: Decimal
    #: Benchmark-relative return in bps a year, after the drag above.
    net_excess_bps: Decimal

    @property
    def positive(self) -> bool:
        return self.net_excess_bps > 0


@dataclass(frozen=True, slots=True)
class StressPoint:
    """The verdict under a scaled participation-stress assumption.

    The stress shape has no fitted parameters, which means the answer depends on a constant
    somebody chose. Reporting the verdict across a range of that constant is what keeps the
    result from reading as a measurement: if the verdict holds from half to four times the
    assumed stress it is robust to the assumption, and if it flips at 2x the reader needs to know
    the conclusion is the constant's rather than the strategy's.
    """

    multiplier: Decimal
    verdict: CapacityVerdict
    breakeven_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    """What the ladder says, and everything it could not say.

    `blocking` and `unassessed` are the same shape as `ForwardEvidence` because the question has
    the same structure: the reasons an answer is unavailable matter as much as the answer, and a
    check that did not run is not a check that passed.
    """

    strategy_id: str
    objective_identity: str
    cost_model_identity: str
    rungs: tuple[LadderRung, ...]
    verdict: CapacityVerdict
    #: The capital at which edge crosses zero, when it does so inside the mandate.
    breakeven_usd: Decimal | None
    blocking: tuple[str, ...]
    #: The same verdict recomputed across `STRESS_MULTIPLIERS`. Empty when the assessment is
    #: blocked, because there is no answer whose sensitivity could be reported.
    stress_sensitivity: tuple[StressPoint, ...]

    @property
    def supports_intended_capital(self) -> bool:
        """Whether the edge survives across the mandate's whole intended range."""
        return self.verdict is CapacityVerdict.SURVIVES_MANDATE

    def explain(self) -> str:
        parts = [f"{self.strategy_id}: {self.verdict.value}"]
        if self.breakeven_usd is not None:
            parts.append(f"edge crosses zero at ${self.breakeven_usd}")
        parts.extend(self.blocking)
        return "; ".join(parts)


def capital_ladder(objective: EconomicObjective, *, steps: int = 5) -> tuple[Decimal, ...]:
    """Capital levels to evaluate, derived from the mandate rather than from a constant.

    Endpoints of the intended range plus interior points, geometrically spaced because capacity
    effects are multiplicative in size rather than additive. A hard-coded ladder would be a
    second declaration of what capital this project is for, and it would drift from the mandate.
    """
    if steps < 2:
        raise ValueError("a ladder needs at least its two endpoints")
    low, high = objective.intended_capital_range_usd
    if high == low:
        return (Decimal(low),)
    ratio = (Decimal(high) / Decimal(low)) ** (Decimal(1) / Decimal(steps - 1))
    rungs = [Decimal(low) * ratio**index for index in range(steps)]
    # Belt and braces on the mandate's own boundaries. Measured across realistic integer ranges
    # the geometric terms already land exactly, so this assignment does not currently change any
    # output -- it is a guard against `Decimal` drift rather than a load-bearing mechanism, and
    # it is not mutation-checked because nothing can currently make it fire.
    rungs[0], rungs[-1] = Decimal(low), Decimal(high)
    return tuple(value.quantize(Decimal("1")) for value in rungs)


def assess_capital_capacity(
    *,
    strategy_id: str,
    objective: EconomicObjective,
    cost_model: CostModel,
    gross_excess_bps: Decimal | None,
    instruments: Mapping[str, InstrumentCapacity] | None,
    turnover: Decimal | None,
    ladder: Sequence[Decimal] | None = None,
) -> CapacityAssessment:
    """Evaluate the frozen strategy across the capital ladder.

    Every input that can be unavailable is optional, and each one blocks. A capacity answer
    computed from a substituted turnover or a guessed liquidity is a number with the authority of
    a measurement and the content of an assumption -- which is the specific failure this issue
    exists to prevent, one level up from the cost model.

    `instruments` carries the strategy's allocation and each instrument's traded volume, because
    capacity is a property of the orders placed rather than of the account total.
    """
    rungs_usd = tuple(ladder) if ladder is not None else capital_ladder(objective)
    low, high = objective.intended_capital_range_usd
    blocking: list[str] = []

    if gross_excess_bps is None:
        blocking.append(
            "no benchmark-relative excess has been measured for this strategy, so there is "
            "nothing for capacity to erode"
        )
    if not instruments:
        blocking.append(
            "no per-instrument allocation and traded volume is available for this strategy, so "
            "the notional of the orders it would place -- and therefore participation at any "
            "capital -- is unknown"
        )
    if turnover is None:
        blocking.append(
            "no turnover is available, so the number of round trips a year that cost would be "
            "charged on is unknown"
        )

    # The mandate has to be the operator's before this may say the edge survives. Reported and
    # computed regardless, so the window keeps accruing (#54).
    unratified = objective.ratification_objections()
    blocking.extend(unratified)

    rungs: list[LadderRung] = []
    sensitivity: list[StressPoint] = []
    if not blocking:
        assert gross_excess_bps is not None
        assert instruments is not None
        assert turnover is not None
        # The same measured inputs for the headline and every stressed variant of it, so the
        # sensitivity cannot silently be computed against different data than the answer.
        evaluate = partial(
            _rungs_for,
            rungs_usd=rungs_usd,
            bounds=(Decimal(low), Decimal(high)),
            gross_excess_bps=gross_excess_bps,
            instruments=instruments,
            turnover=turnover,
        )
        rungs = evaluate(cost_model)
        for multiplier in STRESS_MULTIPLIERS:
            stressed = cost_model.model_copy(
                update={
                    "participation_stress_bps": (
                        cost_model.participation_stress_bps * multiplier
                    )
                }
            )
            under, where = _verdict(evaluate(stressed), blocking=())
            sensitivity.append(
                StressPoint(multiplier=multiplier, verdict=under, breakeven_usd=where)
            )

    verdict, breakeven = _verdict(rungs, blocking=blocking)
    return CapacityAssessment(
        strategy_id=strategy_id,
        objective_identity=objective.identity,
        cost_model_identity=cost_model.identity,
        rungs=tuple(rungs),
        verdict=verdict,
        breakeven_usd=breakeven,
        blocking=tuple(blocking),
        stress_sensitivity=tuple(sensitivity),
    )


def _rungs_for(
    cost_model: CostModel,
    rungs_usd: Sequence[Decimal],
    *,
    bounds: tuple[Decimal, Decimal],
    gross_excess_bps: Decimal,
    instruments: Mapping[str, InstrumentCapacity],
    turnover: Decimal,
) -> list[LadderRung]:
    """The ladder under one set of cost assumptions, shared by the headline and its sensitivity.

    Participation is computed **per instrument** from the notional that instrument would actually
    be traded at, and the resulting drag is weighted by the share of capital it carries. The
    reported `participation` is the worst instrument's, because that is the one that binds and an
    average would hide the sleeve that cannot be filled.
    """
    low, high = bounds
    rungs = []
    # Each instrument is entered and exited once per round trip, and there are `turnover` round
    # trips a year. Commission is charged on orders, so it needs the count rather than a rate.
    orders_per_year = Decimal(len(instruments)) * 2 * turnover
    for capital in rungs_usd:
        weighted_drag = Decimal("0")
        worst_participation = Decimal("0")
        for holding in instruments.values():
            participation = capital * holding.weight / holding.session_volume_usd
            worst_participation = max(worst_participation, participation)
            weighted_drag += holding.weight * cost_model.drag_bps(
                participation=participation, turnover=turnover
            )
        drag = weighted_drag + cost_model.commission_drag_bps(
            capital=capital, orders_per_year=orders_per_year
        )
        rungs.append(
            LadderRung(
                capital_usd=capital,
                inside_mandate=low <= capital <= high,
                participation=worst_participation,
                execution_drag_bps=drag,
                net_excess_bps=gross_excess_bps - drag,
            )
        )
    return rungs


def _verdict(
    rungs: Sequence[LadderRung], *, blocking: Sequence[str]
) -> tuple[CapacityVerdict, Decimal | None]:
    """The headline, and the capital behind it.

    Scored only on rungs inside the mandate. A level the operator never intended to trade is
    context, and letting it decide the verdict manufactures a negative result the same way
    scoring only the smallest level would manufacture a positive one.
    """
    if blocking or not rungs:
        return CapacityVerdict.UNASSESSED, None
    inside = [rung for rung in rungs if rung.inside_mandate]
    if not inside:
        return CapacityVerdict.UNASSESSED, None
    if not any(rung.positive for rung in inside):
        # No edge anywhere, which is not a capacity finding. Reporting a breakeven of zero here
        # would read as "capacity is exhausted at $0" rather than "there was never an edge".
        return CapacityVerdict.NO_EDGE_TO_EXHAUST, None
    if all(rung.positive for rung in inside):
        return CapacityVerdict.SURVIVES_MANDATE, None
    # Positive at the bottom and negative somewhere above: the first rung that goes negative is
    # where the edge dies, and it is the number worth reporting.
    first_negative = next(rung for rung in inside if not rung.positive)
    return CapacityVerdict.EXHAUSTS_INSIDE_MANDATE, first_negative.capital_usd


__all__ = [
    "BASELINE_SLIPPAGE_BPS",
    "UNAVAILABLE_LEVELS",
    "CapacityAssessment",
    "CapacityVerdict",
    "CostLevel",
    "CostModel",
    "InstrumentCapacity",
    "StressPoint",
    "LadderRung",
    "assess_capital_capacity",
    "capital_ladder",
]
