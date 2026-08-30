"""The path that actually feeds the capacity ladder, and persists what it found (#51).

The engine has been correct and unreachable since #62/#67: nothing produced a turnover, an
allocation or a per-instrument volume, so `assess_milestone` received `None` and reported the
capacity question as permanently unassessed. Reading that as "issue #51 is open" was the mistake
the milestone gate was explicitly written to avoid -- and it was true anyway, because no artifact
existed for it to read.

This closes the seam in one direction only. It assembles inputs from durable state, runs the
existing ladder, and persists a content-addressed assessment. It does not change the arithmetic,
does not lower a bar, and cannot manufacture the edge the ladder exists to test: with no measured
benchmark-relative excess the verdict is `UNASSESSED`, which is the project's actual position.

**Weights are a project default and say so.** The strategy declares a `roster_size` and a
universe but not a per-instrument allocation, because it holds an equal-weighted roster selected
each period. Equal weight across the universe is therefore the honest model of what it does, and
it is `MODELLED` rather than `OBSERVED`. It is also the conservative direction for capacity: a
concentrated roster would put more notional through fewer books, so equal weight understates
participation. That is worth stating plainly rather than leaving as a footnote -- the number is
optimistic in exactly one direction, and a later measured allocation should replace it.

**Volume is observed or the instrument is absent.** A symbol with fewer than
`MINIMUM_VOLUME_SESSIONS` stored sessions contributes no allocation and is named in
`unavailable`. Substituting a guess for a book nobody measured is the failure this issue is about.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from quantbot.research.capacity_inputs import (
    MINIMUM_VOLUME_SESSIONS,
    CapacityInputs,
    InputProvenance,
    InstrumentAllocation,
    TurnoverBasis,
    median_dollar_volume,
    turnover_from_holding_period,
)
from quantbot.research.capital import (
    CapacityAssessment,
    CostModel,
    assess_capital_capacity,
)
from quantbot.research.mandate import EconomicObjective
from quantbot.storage import StorageRepository
from quantbot.strategy import StrategyConfig


def build_capacity_inputs(
    session: Session,
    *,
    strategy_id: str,
    configuration_hash: str,
    config: StrategyConfig,
    objective: EconomicObjective,
    cost_model: CostModel,
    as_of: date,
    market_data_source: str,
    gross_excess_bps: Decimal | None = None,
    provider: str | None = None,
) -> CapacityInputs:
    """Assemble the ladder's inputs from durable state, naming what could not be measured.

    `gross_excess_bps` stays a parameter rather than being read from anywhere, because nothing in
    this repository currently measures a benchmark-relative excess for a deployed strategy.
    Passing `None` -- the honest default -- produces a blocked assessment, which is the correct
    answer and not a defect in this function.
    """
    repository = StorageRepository(session)
    unavailable: list[str] = []
    allocations: list[InstrumentAllocation] = []

    # Equal weight across whatever the universe actually holds, so the model tracks the config
    # rather than restating it. A universe change moves the weights automatically.
    symbols = tuple(config.universe)
    weight = (Decimal(1) / Decimal(len(symbols))).quantize(Decimal("0.00000001"))
    for symbol in symbols:
        bars = repository.list_bars(symbol=symbol, provider=provider)
        if len(bars) < MINIMUM_VOLUME_SESSIONS:
            unavailable.append(
                f"{symbol} has {len(bars)} stored sessions and {MINIMUM_VOLUME_SESSIONS} are "
                "required before its traded volume counts as measured"
            )
            continue
        recent = bars[-MINIMUM_VOLUME_SESSIONS:]
        volume = median_dollar_volume(
            [bar.close for bar in recent], [bar.volume for bar in recent]
        )
        if volume is None or volume <= 0:
            unavailable.append(f"{symbol} has stored sessions but no usable dollar volume")
            continue
        allocations.append(
            InstrumentAllocation(
                symbol=symbol,
                weight=weight,
                # The strategy holds an equal-weighted roster; it does not declare per-symbol
                # weights, so this is derived from its universe rather than measured from fills.
                weight_provenance=InputProvenance.MODELLED,
                session_volume_usd=volume,
                volume_provenance=InputProvenance.OBSERVED,
                volume_sessions=len(recent),
            )
        )

    # Weights are normalised over the instruments that survived, so a dropped symbol does not
    # leave the portfolio summing to less than one and quietly understating every participation.
    if allocations:
        total = sum((item.weight for item in allocations), Decimal("0"))
        allocations = [
            item.model_copy(update={"weight": item.weight / total}) for item in allocations
        ]

    turnover = turnover_from_holding_period(config.exit_period)
    if gross_excess_bps is None:
        unavailable.append(
            "no benchmark-relative excess has been measured for this strategy, so there is no "
            "edge for capacity to erode and the ladder has nothing to evaluate"
        )

    return CapacityInputs(
        strategy_id=strategy_id,
        configuration_hash=configuration_hash,
        objective_identity=objective.identity,
        cost_model_identity=cost_model.identity,
        as_of=as_of,
        market_data_source=market_data_source,
        turnover=turnover,
        turnover_basis=TurnoverBasis.CONFIGURED_HOLDING_PERIOD,
        turnover_provenance=InputProvenance.MODELLED,
        gross_excess_bps=gross_excess_bps,
        gross_excess_provenance=(
            None if gross_excess_bps is None else InputProvenance.OBSERVED
        ),
        allocations=tuple(allocations),
        unavailable=tuple(unavailable),
    )


def assess_from_inputs(
    inputs: CapacityInputs, *, objective: EconomicObjective, cost_model: CostModel
) -> CapacityAssessment:
    """Run the existing ladder over an input artifact, carrying its provenance into the answer.

    The arithmetic is untouched. What changes is that the verdict now names the artifact it came
    from and which of its inputs were modelled, so a reader cannot mistake a number resting on a
    default for one resting on a measurement.
    """
    assessment = assess_capital_capacity(
        strategy_id=inputs.strategy_id,
        objective=objective,
        cost_model=cost_model,
        gross_excess_bps=inputs.gross_excess_bps,
        instruments=inputs.instruments() or None,
        turnover=inputs.turnover,
    )
    blocking = tuple([*assessment.blocking, *inputs.unavailable])
    return CapacityAssessment(
        strategy_id=assessment.strategy_id,
        objective_identity=assessment.objective_identity,
        cost_model_identity=assessment.cost_model_identity,
        rungs=assessment.rungs,
        verdict=assessment.verdict,
        breakeven_usd=assessment.breakeven_usd,
        blocking=blocking,
        stress_sensitivity=assessment.stress_sensitivity,
        inputs_identity=inputs.identity,
        modelled_inputs=inputs.modelled_inputs,
    )


def assessment_row(assessment: CapacityAssessment, *, as_of: date) -> dict[str, object]:
    """Flatten an assessment for storage, keeping `storage` free of any research import."""
    rung = assessment.first_failing_rung
    return {
        "assessment_id": assessment_identity(assessment, as_of=as_of),
        "strategy_id": assessment.strategy_id,
        "as_of": as_of.isoformat(),
        "inputs_identity": assessment.inputs_identity,
        "objective_identity": assessment.objective_identity,
        "cost_model_identity": assessment.cost_model_identity,
        "verdict": assessment.verdict.value,
        "breakeven_usd": (
            None if assessment.breakeven_usd is None else str(assessment.breakeven_usd)
        ),
        "first_failing_rung_usd": None if rung is None else str(rung.capital_usd),
        "binding_symbol": assessment.binding_symbol,
        "binding_constraint": assessment.binding_constraint,
        "modelled_inputs": ",".join(assessment.modelled_inputs),
        "blocking": " | ".join(assessment.blocking),
        "rung_count": len(assessment.rungs),
    }


def assessment_identity(assessment: CapacityAssessment, *, as_of: date) -> str:
    """Content-addressed over the verdict and everything that produced it.

    `as_of` participates because the same strategy assessed on two dates rests on different
    stored liquidity, and pooling those would be the same error as pooling across a threshold
    change.
    """
    import hashlib
    import json

    payload = {
        "strategy_id": assessment.strategy_id,
        "as_of": as_of.isoformat(),
        "inputs_identity": assessment.inputs_identity,
        "objective_identity": assessment.objective_identity,
        "cost_model_identity": assessment.cost_model_identity,
        "verdict": assessment.verdict.value,
        "breakeven_usd": (
            None if assessment.breakeven_usd is None else str(assessment.breakeven_usd)
        ),
        "rungs": [
            {
                "capital_usd": str(rung.capital_usd),
                "inside_mandate": rung.inside_mandate,
                "participation": str(rung.participation),
                "binding_symbol": rung.binding_symbol,
                "execution_drag_bps": str(rung.execution_drag_bps),
                "net_excess_bps": str(rung.net_excess_bps),
            }
            for rung in assessment.rungs
        ],
        "blocking": list(assessment.blocking),
        "modelled_inputs": list(assessment.modelled_inputs),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"capacity-assessment-v1-{digest[:16]}"


__all__ = [
    "assess_from_inputs",
    "assessment_identity",
    "assessment_row",
    "build_capacity_inputs",
]
