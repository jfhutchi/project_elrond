"""The capacity ladder now has a production caller, and writes what it finds (#51).

`capacity_production` was correct and callerless. Every construction site for
`CapacityAssessment` and every call to `record_capacity_assessment` in this repository was a
test, so `assess_milestone` read an empty table and reported the capacity question `UNASSESSED`
-- permanently, and not because the answer was unknown but because nothing ever asked.

`milestone.py` already documented that `UNASSESSED` means *no production artifact exists*. That
was true, and would have stayed true after the evidence arrived, because nothing would have
noticed. This is the thirteenth instance of that shape on this branch and the one with the
longest fuse.

The assertions here are deliberately about the **row**, not the return value. A method that
assembles a perfectly correct assessment, hands back its id and persists nothing is exactly the
defect this is closing, and asserting the id would not see it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantbot.domain import Bar, StrategyIdentity
from quantbot.market_data.cache import MarketDataCache
from quantbot.operations.runners import AdaptiveMomentumRunner, MarketDataSettings
from quantbot.runtime import runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config
from tests.unit.operations.test_research_context import with_real_containment

NOW = datetime(2026, 8, 28, 20, 5, tzinfo=UTC)
AS_OF = date(2026, 8, 28)


def identity() -> StrategyIdentity:
    return StrategyIdentity(
        strategy_id="adaptive-momentum-v1-abc123",
        version="1.2.0",
        git_commit="a" * 40,
        configuration_hash="c" * 16,
        deployment_timestamp=NOW,
    )


def settings() -> MarketDataSettings:
    return MarketDataSettings()


def provider_key(market_data: MarketDataSettings) -> str:
    return MarketDataCache.provider_key(
        market_data.provider,
        market_data.feed,
        market_data.adjustment,
        market_data.timeframe,
    )


def runner(database: Database) -> SimpleNamespace:
    """Only the collaborators `_record_capacity_assessment` actually reads."""
    market_data = settings()
    return with_real_containment(
        SimpleNamespace(
            _database=database,
            _identity=identity(),
            _config=load_strategy_config(runtime_paths().config),
            _market_data=market_data,
        )
    )


def bars_for(symbols: list[str], market_data: MarketDataSettings) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=datetime(2026, 8, day, 20, 0, tzinfo=UTC),
            open="100",
            high="101",
            low="99",
            close="100",
            volume="500000",
            adjustment="1",
        )
        for symbol in symbols
        for day in range(3, 28)
    ]


@pytest.fixture
def database(tmp_path: Path) -> Database:
    store = Database.create(tmp_path / "capacity.db")
    market_data = settings()
    config = load_strategy_config(runtime_paths().config)
    with store.transaction() as session:
        StorageRepository(session).save_bars(
            bars_for(list(config.universe), market_data),
            provider=provider_key(market_data),
        )
    yield store
    store.close()


def stored(database: Database) -> list[dict[str, object]]:
    """Every recorded assessment. Queried directly: the repository exposes only the latest one,
    and these tests are about how many rows exist, which is what the caller-less bug hid."""
    from sqlalchemy import select

    from quantbot.storage.schema import capacity_assessments

    with database.transaction() as session:
        return [
            dict(row)
            for row in session.execute(
                select(capacity_assessments)
                .where(capacity_assessments.c.strategy_id == identity().strategy_id)
                .order_by(capacity_assessments.c.as_of)
            ).mappings()
        ]


def test_the_cycle_writes_a_capacity_assessment(database: Database) -> None:
    """The row is the point. Nothing in production had ever written one.

    Asserting the returned id would pass against a method that assembles a correct assessment and
    persists nothing, which is precisely the defect being closed.
    """
    with database.transaction() as session:
        AdaptiveMomentumRunner._record_capacity_assessment(  # noqa: SLF001
            runner(database), session, trading_date=AS_OF, account_id="acct-1"
        )

    rows = stored(database)

    assert len(rows) == 1
    assert rows[0]["strategy_id"] == identity().strategy_id
    assert rows[0]["as_of"] == AS_OF.isoformat()


def test_a_refusal_is_recorded_rather_than_skipped(database: Database) -> None:
    """The case that is true today, and the one most likely to be written as a skip.

    With no measured benchmark-relative excess the ladder returns `UNASSESSED` and names why. A
    persisted refusal says the question was asked and could not be answered -- a different fact
    from silence, and the one that lets an operator watch the measurement start working the day
    enough sessions exist.

    Skipping the write when the verdict is unassessed would look tidy and would restore exactly
    the bug this closes: an empty table reported as "not yet assessed".
    """
    with database.transaction() as session:
        AdaptiveMomentumRunner._record_capacity_assessment(  # noqa: SLF001
            runner(database), session, trading_date=AS_OF, account_id="acct-1"
        )

    rows = stored(database)

    assert len(rows) == 1, "an unassessable capacity question is still a recorded answer"
    assert rows[0]["verdict"] == "UNASSESSED"
    assert rows[0]["blocking"], "a refusal that names no reason is not a refusal"


def test_two_runs_on_one_date_record_once(database: Database) -> None:
    """The identity is content-addressed, so a re-run does not accumulate duplicates.

    A cycle that ran twice in a day -- a restart, a manual run -- must not make the capacity
    history look like two independent assessments.
    """
    for _ in range(2):
        with database.transaction() as session:
            AdaptiveMomentumRunner._record_capacity_assessment(  # noqa: SLF001
                runner(database), session, trading_date=AS_OF, account_id="acct-1"
            )

    assert len(stored(database)) == 1


def test_a_different_date_is_a_different_assessment(database: Database) -> None:
    """The same strategy assessed on two dates rests on different stored liquidity.

    Collapsing those would pool measurements taken over different books, which is the same error
    as pooling across a changed threshold.
    """
    for as_of in (AS_OF, date(2026, 8, 27)):
        with database.transaction() as session:
            AdaptiveMomentumRunner._record_capacity_assessment(  # noqa: SLF001
                runner(database), session, trading_date=as_of, account_id="acct-1"
            )

    assert len(stored(database)) == 2


def test_the_milestone_gate_stops_reading_an_empty_table(database: Database) -> None:
    """The consequence the wiring exists for, asserted end to end.

    `milestone.py` documents that `UNASSESSED` means *no production artifact exists*. That was
    true and would have stayed true forever, because nothing produced one. After a cycle records
    an assessment the gate reads a real artifact -- still `UNASSESSED` today, because the excess
    is unmeasured, but now for the honest reason rather than because nobody asked.

    Without this assertion the wiring could be correct and the gate could still be reading
    something else, which is the shape of the bug being closed.
    """
    with database.transaction() as session:
        assert (
            StorageRepository(session).latest_capacity_assessment(identity().strategy_id)
            is None
        ), "no artifact before the cycle runs"

        AdaptiveMomentumRunner._record_capacity_assessment(  # noqa: SLF001
            runner(database), session, trading_date=AS_OF, account_id="acct-1"
        )

    with database.transaction() as session:
        found = StorageRepository(session).latest_capacity_assessment(identity().strategy_id)

    assert found is not None, "the gate now reads an artifact rather than an empty table"
    assert found["as_of"] == AS_OF.isoformat()
