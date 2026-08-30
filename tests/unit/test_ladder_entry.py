"""A deployed strategy has to actually be on the ladder (#46).

`PromotionLedger` -- `enter`, `promote`, `demote` -- was called only by its own tests, so no
strategy was ever on the ladder in a real store. That is invisible rather than loud: the console
reads `list_promotions()` directly, so an empty ladder renders as an empty panel, and every gate
that judges a strategy simply has nothing to judge.

Only entry is wired here. Automatic promotion is a separate decision recorded on #46: a promoter
written before any forward evidence exists could only be tested against fixtures, which is the
condition that produced this defect class nine times over.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.research.promotion import (
    ECONOMIC_STAGES,
    PromotionLedger,
    Stage,
)
from quantbot.runtime import load_or_create_identity
from quantbot.storage import Database, StorageRepository
from quantbot.strategy.config import StrategyConfig, load_strategy_config

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "ladder.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def config() -> StrategyConfig:
    return load_strategy_config("config/strategy-v1-3.yaml")


def ladder_position(database: Database, strategy_id: str):
    with database.transaction() as session:
        return PromotionLedger(session).current(strategy_id)


def test_deploying_a_strategy_puts_it_on_the_ladder(
    database: Database, config: StrategyConfig
) -> None:
    """The gap this closes. A strategy off the ladder cannot be judged by anything that judges."""
    identity = load_or_create_identity(database, config, now=NOW)

    position = ladder_position(database, identity.strategy_id)

    assert position is not None
    assert position.stage is Stage.CANDIDATE


def test_entry_claims_nothing_that_has_to_be_earned(
    database: Database, config: StrategyConfig
) -> None:
    """`CANDIDATE` is the stage with no gate behind it, and that is why it is the entry point.

    Entering at an economic stage would assert a qualification no gate ever ran. The ledger
    refuses it outright, and this asserts the runtime does not try -- a deployment must never be
    a route around `promote()`.
    """
    identity = load_or_create_identity(database, config, now=NOW)

    position = ladder_position(database, identity.strategy_id)

    assert position is not None
    assert position.stage not in ECONOMIC_STAGES
    assert position.actor == "runtime"
    assert "claims nothing" in position.reason


def marker_position(database: Database, strategy_id: str, reason: str) -> None:
    """Move the recorded position, without going through a gate.

    Promoting properly needs a frozen registration -- `REGISTERED` refuses a deployment with no
    claim behind it, which is correct and is not what these tests are about. What matters here is
    that a redeploy leaves an existing position alone, so the position is simply made
    distinguishable and then checked for survival.
    """
    from quantbot.storage.repositories import PromotionRecord

    with database.transaction() as session:
        StorageRepository(session).save_promotion(
            PromotionRecord(
                strategy_id=strategy_id,
                stage=Stage.CANDIDATE.value,
                strategy_version="v1",
                configuration_hash="cfg",
                reason=reason,
                actor="an-earlier-session",
                updated_at=NOW,
            ),
            direction="PROMOTION",
        )


def test_a_restart_does_not_reset_what_a_strategy_earned(
    database: Database, config: StrategyConfig
) -> None:
    """The failure that would make this worse than not wiring it at all.

    Re-entering discards everything a strategy has earned, through the same call that
    legitimately starts one. The ledger refuses rather than upserting; this asserts the runtime
    treats that refusal as success rather than propagating it -- a restart must be uneventful.
    """
    identity = load_or_create_identity(database, config, now=NOW)
    marker_position(database, identity.strategy_id, "earned in an earlier session")

    again = load_or_create_identity(database, config, now=NOW)

    assert again.strategy_id == identity.strategy_id
    position = ladder_position(database, identity.strategy_id)
    assert position is not None
    assert position.reason == "earned in an earlier session", "a redeploy reset the ladder"
    assert position.actor == "an-earlier-session"


def test_a_strategy_already_on_the_ladder_is_left_alone(
    database: Database, config: StrategyConfig
) -> None:
    """Entry establishes a position; it never moves one.

    Covers the case a restart does not: a store where the position exists but no deployment row
    does, which is what a restored backup or a repaired store looks like -- and the one case
    where the runtime genuinely does reach `enter()` a second time.
    """
    strategy_id = load_or_create_identity(database, config, now=NOW).strategy_id
    marker_position(database, strategy_id, "survived a repaired store")
    with database.transaction() as session:
        session.connection().exec_driver_sql(
            "DELETE FROM strategy_deployments WHERE strategy_id = ?", (strategy_id,)
        )

    load_or_create_identity(database, config, now=NOW)

    position = ladder_position(database, strategy_id)
    assert position is not None
    assert position.reason == "survived a repaired store"


def test_the_runtime_does_not_promote(database: Database, config: StrategyConfig) -> None:
    """Asserted structurally: the deployment path may put a strategy on the ladder and no more.

    An agent wiring automatic promotion would be building the mechanism that advances strategies
    toward the live-capital boundary. That is a decision left open on #46, and this test is what
    makes it a decision rather than a drift.
    """
    import ast

    tree = ast.parse(Path("src/quantbot/runtime.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "enter" in called, "the ladder is not entered, so nothing is on it"
    assert "promote" not in called, "the runtime promotes; that is not its decision to make"


def test_the_entry_is_recorded_as_a_promotion_event(
    database: Database, config: StrategyConfig
) -> None:
    """History starts where the strategy did, for the same reason policy version 1 is recorded.

    Without the event, the first thing the ladder's history says about a strategy is whatever
    happened to it later, and nothing explains how it arrived.
    """
    identity = load_or_create_identity(database, config, now=NOW)

    with database.transaction() as session:
        history = PromotionLedger(session).history(identity.strategy_id)

    assert len(history) == 1
    assert Stage.CANDIDATE.value in str(history[0])
