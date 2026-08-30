"""The path that feeds the capacity ladder, and the ways it could feed it a guess (#51).

The engine was already right. What was missing was anything that produced its inputs, so the
milestone gate received `None` forever. Every test here is about the seam rather than the
arithmetic: where the numbers come from, what happens when they are absent, and whether the gate
actually reads the artifact instead of a tracker.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.domain import Bar
from quantbot.research.capacity_inputs import (
    MINIMUM_VOLUME_SESSIONS,
    CapacityInputs,
    InputProvenance,
    InstrumentAllocation,
    TurnoverBasis,
    median_dollar_volume,
    turnover_from_holding_period,
)
from quantbot.research.capacity_production import (
    assess_from_inputs,
    assessment_row,
    build_capacity_inputs,
)
from quantbot.research.capital import CapacityVerdict, CostModel
from quantbot.research.mandate import load_economic_objective
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

PROVIDER = "alpaca:sip:all:1day"
AS_OF = date(2026, 3, 27)


def objective():
    return load_economic_objective()


def config():
    return load_strategy_config("config/strategy-v1.yaml")


def bars_for(symbols, *, sessions: int = 25, volume: str = "5000000", close: str = "100"):
    return [
        Bar(
            symbol=symbol,
            timestamp=datetime(2026, 3, 2, 21, 0, tzinfo=UTC) + timedelta(days=index),
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal(volume),
            adjustment=Decimal("1"),
        )
        for symbol in symbols
        for index in range(sessions)
    ]


def store(tmp_path: Path, bars) -> Database:
    database = Database.create(tmp_path / "capacity.db")
    if bars:
        with database.transaction() as session:
            StorageRepository(session).save_bars(bars, provider=PROVIDER)
    return database


def inputs_from(database: Database, *, gross_excess_bps: Decimal | None = None) -> CapacityInputs:
    with database.transaction() as session:
        return build_capacity_inputs(
            session,
            strategy_id="adaptive-momentum",
            configuration_hash="cfg-abc",
            config=config(),
            objective=objective(),
            cost_model=CostModel(),
            as_of=AS_OF,
            market_data_source=PROVIDER,
            provider=PROVIDER,
            gross_excess_bps=gross_excess_bps,
        )


# --- inputs come from state --------------------------------------------------------------------


def test_the_inputs_are_read_from_stored_bars_rather_than_supplied(tmp_path: Path) -> None:
    """The whole point of #51's remaining gap: nothing produced these, so nothing ran."""
    universe = config().universe
    database = store(tmp_path, bars_for(universe))

    built = inputs_from(database)
    database.close()

    assert len(built.allocations) == len(universe)
    assert all(item.volume_provenance is InputProvenance.OBSERVED for item in built.allocations)
    assert all(item.volume_sessions == MINIMUM_VOLUME_SESSIONS for item in built.allocations)
    assert built.identity.startswith("capacity-inputs-v1-")


def test_a_symbol_without_enough_stored_volume_is_excluded_and_named(tmp_path: Path) -> None:
    """Substituting a guess for a book nobody measured is the failure this issue is about."""
    universe = config().universe
    plenty = bars_for(universe[1:], sessions=25)
    thin = bars_for(universe[:1], sessions=3)
    database = store(tmp_path, plenty + thin)

    built = inputs_from(database)
    database.close()

    assert len(built.allocations) == len(universe) - 1
    assert universe[0] not in {item.symbol for item in built.allocations}
    assert any(universe[0] in reason for reason in built.unavailable)
    assert any(
        "required before its traded volume counts as measured" in reason
        for reason in built.unavailable
    )


def test_dropping_a_symbol_renormalises_the_remaining_weights(tmp_path: Path) -> None:
    """Otherwise the portfolio sums to less than one and every participation is understated.

    That error is invisible and optimistic: less notional per book means less participation,
    which means less drag, which means a capacity verdict that is too generous.
    """
    universe = config().universe
    database = store(tmp_path, bars_for(universe[1:]) + bars_for(universe[:1], sessions=3))

    built = inputs_from(database)
    database.close()

    total = sum((item.weight for item in built.allocations), Decimal("0"))
    assert Decimal("0.999") < total < Decimal("1.001"), f"weights total {total}"


def test_weights_that_do_not_form_a_portfolio_are_refused() -> None:
    """A capacity answer computed from weights summing to a half measures a different strategy."""
    with pytest.raises(ValueError, match="not a portfolio"):
        CapacityInputs(
            strategy_id="adaptive-momentum",
            configuration_hash="cfg",
            objective_identity="economic-objective-v1-x",
            cost_model_identity="cost-model-x",
            as_of=AS_OF,
            market_data_source=PROVIDER,
            allocations=(
                InstrumentAllocation(
                    symbol="SPY",
                    weight=Decimal("0.5"),
                    weight_provenance=InputProvenance.MODELLED,
                    session_volume_usd=Decimal("1000000"),
                    volume_provenance=InputProvenance.OBSERVED,
                    volume_sessions=20,
                ),
            ),
        )


# --- provenance ---------------------------------------------------------------------------------


def test_turnover_is_derived_from_the_strategys_own_holding_period_and_says_so(
    tmp_path: Path,
) -> None:
    """A project default under #72, derived from the config so it moves when the strategy does.

    It is `MODELLED`, and the assessment repeats that, because a verdict resting on an assumed
    turnover must never read as one resting on counted round trips.
    """
    strategy = config()
    database = store(tmp_path, bars_for(strategy.universe))

    built = inputs_from(database)
    database.close()

    assert built.turnover == turnover_from_holding_period(strategy.exit_period)
    assert built.turnover_basis is TurnoverBasis.CONFIGURED_HOLDING_PERIOD
    assert built.turnover_provenance is InputProvenance.MODELLED
    assert any("turnover" in item for item in built.modelled_inputs)


def test_the_assessment_repeats_which_inputs_were_modelled(tmp_path: Path) -> None:
    """An empty `modelled_inputs` is a strong claim: every input was measured."""
    database = store(tmp_path, bars_for(config().universe))
    built = inputs_from(database, gross_excess_bps=Decimal("300"))
    database.close()

    assessed = assess_from_inputs(built, objective=objective(), cost_model=CostModel())

    assert assessed.inputs_identity == built.identity
    assert "instrument weights" in assessed.modelled_inputs
    assert any("turnover" in item for item in assessed.modelled_inputs)


def test_volume_is_the_median_so_one_huge_session_does_not_raise_assumed_liquidity() -> None:
    """The number is what a book absorbs on an ordinary day, not on its best one."""
    closes = [Decimal("100")] * 5
    ordinary = [Decimal("1000")] * 4 + [Decimal("1000000")]

    assert median_dollar_volume(closes, ordinary) == Decimal("100000")
    assert median_dollar_volume([], []) is None


# --- the verdict --------------------------------------------------------------------------------


def test_with_no_measured_excess_the_verdict_is_unassessed(tmp_path: Path) -> None:
    """The project's actual position, and it must survive contact with a working input path.

    Wiring the seam must not manufacture the edge the ladder exists to test. Everything else can
    be present -- allocations, liquidity, turnover -- and the answer is still that there is no
    measured benchmark-relative excess for capacity to erode.
    """
    database = store(tmp_path, bars_for(config().universe))
    built = inputs_from(database, gross_excess_bps=None)
    database.close()

    assert built.complete is False
    assessed = assess_from_inputs(built, objective=objective(), cost_model=CostModel())

    assert assessed.verdict is CapacityVerdict.UNASSESSED
    assert any("no benchmark-relative excess" in reason for reason in assessed.blocking)


def test_a_measured_excess_produces_a_ladder_that_names_what_binds(tmp_path: Path) -> None:
    """"XLU binds at $2m" tells an operator what to change; "the strategy exhausts" does not."""
    database = store(tmp_path, bars_for(config().universe))
    built = inputs_from(database, gross_excess_bps=Decimal("300"))
    database.close()

    assessed = assess_from_inputs(built, objective=objective(), cost_model=CostModel())

    assert assessed.verdict is not CapacityVerdict.UNASSESSED
    assert assessed.rungs
    assert all(rung.binding_symbol is not None for rung in assessed.rungs)
    assert assessed.binding_symbol in set(config().universe)


def test_a_thin_book_binds_before_a_deep_one(tmp_path: Path) -> None:
    """The binding instrument is the one that actually cannot be filled, not an average."""
    universe = config().universe
    thin, deep = universe[0], universe[1]
    database = store(
        tmp_path,
        bars_for([thin], volume="1000") + bars_for([sym for sym in universe if sym != thin]),
    )
    built = inputs_from(database, gross_excess_bps=Decimal("300"))
    database.close()

    assessed = assess_from_inputs(built, objective=objective(), cost_model=CostModel())

    assert assessed.binding_symbol == thin, (
        f"{deep} has a book 5000x deeper; the thin one must bind"
    )


# --- persistence and the gate --------------------------------------------------------------------


def test_the_assessment_persists_and_is_idempotent(tmp_path: Path) -> None:
    """Re-running over unchanged state re-derives the same id and stores once."""
    database = store(tmp_path, bars_for(config().universe))
    built = inputs_from(database, gross_excess_bps=Decimal("300"))
    assessed = assess_from_inputs(built, objective=objective(), cost_model=CostModel())
    row = assessment_row(assessed, as_of=AS_OF)

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_capacity_assessment(row) is True
        assert repository.record_capacity_assessment(row) is False

    with database.transaction() as session:
        stored = StorageRepository(session).latest_capacity_assessment("adaptive-momentum")
    database.close()

    assert stored is not None
    assert stored["verdict"] == assessed.verdict.value
    assert stored["inputs_identity"] == built.identity
    assert stored["binding_symbol"] == assessed.binding_symbol
    assert stored["assessment_id"].startswith("capacity-assessment-v1-")


def test_a_different_input_artifact_is_a_different_assessment(tmp_path: Path) -> None:
    """Two verdicts computed from different liquidity must never share an identity."""
    universe = config().universe
    first = store(tmp_path / "a", bars_for(universe, volume="5000000"))
    second = store(tmp_path / "b", bars_for(universe, volume="9000000"))

    left = assess_from_inputs(
        inputs_from(first, gross_excess_bps=Decimal("300")),
        objective=objective(),
        cost_model=CostModel(),
    )
    right = assess_from_inputs(
        inputs_from(second, gross_excess_bps=Decimal("300")),
        objective=objective(),
        cost_model=CostModel(),
    )
    first.close()
    second.close()

    assert left.inputs_identity != right.inputs_identity
    assert (
        assessment_row(left, as_of=AS_OF)["assessment_id"]
        != assessment_row(right, as_of=AS_OF)["assessment_id"]
    )
