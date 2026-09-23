from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.learner.paper import (
    PaperEntryBlocked,
    PaperModelBinding,
    PromotionGatedStrategy,
    PromotionGatedSubmitter,
    assess_paper_entry,
    build_learner_paper_cycle,
)
from quantbot.operations.cycle import CycleStrategyResult
from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
from quantbot.risk import OrderPurpose
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 9, 21, 15, tzinfo=UTC)


@pytest.fixture
def setup(tmp_path):
    database = Database.create(tmp_path / "paper.db")
    binding = PaperModelBinding.from_config(
        model_id="intraday-model-1",
        artifact_hash="a" * 64,
        code_hash="b" * 64,
        execution_config={"bar_minutes": 5, "horizon_minutes": 30},
    )
    identity = binding.strategy_identity(git_commit="c" * 40, deployed_at=NOW)
    yield database, binding, identity
    database.close()


def deploy(setup, stage=Stage.PAPER_OBSERVATION):
    database, binding, identity = setup
    with database.transaction() as session:
        StorageRepository(session).save_strategy_deployment(identity)
        PromotionLedger(session).enter(
            PromotionState(
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                stage=stage,
                reason="Synthetic gateway fixture only",
                actor="test",
                updated_at=NOW,
            )
        )


def test_missing_deployment_is_read_only_and_never_creates_a_store(setup, tmp_path):
    database, binding, identity = setup
    decision = assess_paper_entry(database.path, binding=binding, identity=identity)
    assert not decision.allowed
    assert "PAPER_DEPLOYMENT_MISSING" in decision.reasons
    absent = tmp_path / "absent.db"
    assert not assess_paper_entry(absent, binding=binding, identity=identity).allowed
    assert not absent.exists()
    with database.transaction() as session:
        assert StorageRepository(session).get_promotion(identity.strategy_id) is None


@pytest.mark.parametrize("stage", [Stage.CANDIDATE, Stage.REGISTERED, Stage.SHADOW])
def test_stage_below_observation_refuses_entry(setup, stage):
    deploy(setup, stage)
    database, binding, identity = setup
    assert (
        "PAPER_OBSERVATION_NOT_EARNED"
        in assess_paper_entry(
            database.path,
            binding=binding,
            identity=identity,
        ).reasons
    )


def test_observation_allows_without_inventing_thirty_days(setup):
    deploy(setup)
    database, binding, identity = setup
    decision = assess_paper_entry(database.path, binding=binding, identity=identity)
    assert decision.allowed
    assert decision.stage == "PAPER_OBSERVATION"
    with database.transaction() as session:
        assert StorageRepository(session).list_qualification_days(identity.strategy_id) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_id", "other-model"),
        ("artifact_hash", "d" * 64),
        ("code_hash", "e" * 64),
        ("execution_config_hash", "f" * 64),
    ],
)
def test_model_or_execution_changes_cannot_borrow_deployment(setup, field, value):
    deploy(setup)
    database, binding, identity = setup
    changed = binding.model_copy(update={field: value})
    assert (
        "PAPER_MODEL_IDENTITY_MISMATCH"
        in assess_paper_entry(
            database.path,
            binding=changed,
            identity=identity,
        ).reasons
    )


def test_exact_deployment_commit_and_promotion_config_are_checked(setup):
    deploy(setup)
    database, binding, identity = setup
    changed = identity.model_copy(update={"git_commit": "d" * 40})
    assert (
        "PAPER_DEPLOYMENT_IDENTITY_MISMATCH"
        in assess_paper_entry(
            database.path,
            binding=binding,
            identity=changed,
        ).reasons
    )
    with database.transaction() as session:
        from sqlalchemy import update

        from quantbot.storage.schema import promotion_state

        session.execute(update(promotion_state).values(configuration_hash="e" * 64))
    assert (
        "PAPER_PROMOTION_IDENTITY_MISMATCH"
        in assess_paper_entry(
            database.path,
            binding=binding,
            identity=identity,
        ).reasons
    )


class Runner:
    def __init__(self):
        self.calls = 0
        self.exit_calls = 0

    async def evaluate(self):
        self.calls += 1
        return CycleStrategyResult(submissions=(), signal_count=0)

    async def evaluate_exits(self):
        self.exit_calls += 1
        return CycleStrategyResult(submissions=(), signal_count=0)


class Submitter:
    def __init__(self):
        self.calls = []

    async def submit(self, intent, **kwargs):
        self.calls.append((intent, kwargs))
        return "existing coordinator result"


def intent(identity):
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="client-1",
        strategy_id=identity.strategy_id,
        signal_date=NOW.date(),
        symbol="SPY",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("1"),
        created_at=NOW,
    )


@pytest.mark.asyncio
async def test_gate_runs_before_strategy_and_rechecks_each_entry(setup):
    database, binding, identity = setup
    runner = Runner()
    gated = PromotionGatedStrategy(database.path, runner, binding=binding, identity=identity)
    await gated.evaluate()
    assert runner.calls == 0
    assert runner.exit_calls == 1
    deploy(setup)
    await gated.evaluate()
    delegate = Submitter()
    submitter = PromotionGatedSubmitter(
        database.path,
        delegate,
        binding=binding,
        identity=identity,
    )
    inputs = dict(
        purpose=OrderPurpose.ENTRY,
        market_eligible=False,
        drawdown=None,
        risk_approved=False,
        vetoes=None,
    )
    assert await submitter.submit(intent(identity), **inputs) == "existing coordinator result"
    assert delegate.calls[0][1] == inputs  # Gateway never upgrades missing risk or market gates.
    with database.transaction() as session:
        PromotionLedger(session).demote(
            identity.strategy_id, Stage.SHADOW, reason="synthetic demotion", actor="test", now=NOW
        )
    with pytest.raises(PaperEntryBlocked):
        await submitter.submit(intent(identity), **inputs)
    assert len(delegate.calls) == 1


@pytest.mark.asyncio
async def test_exits_still_reach_existing_controls_when_unpromoted(setup):
    database, binding, identity = setup
    delegate = Submitter()
    submitter = PromotionGatedSubmitter(
        database.path,
        delegate,
        binding=binding,
        identity=identity,
    )
    for purpose in (OrderPurpose.STRATEGY_EXIT, OrderPurpose.DRAWDOWN_LIQUIDATION):
        await submitter.submit(
            intent(identity),
            purpose=purpose,
            market_eligible=False,
            drawdown=None,
            risk_approved=None,
            vetoes=None,
        )
    assert len(delegate.calls) == 2


@pytest.mark.asyncio
async def test_revoked_promotion_keeps_horizon_exits_but_rejects_exit_only_entries(setup):
    from quantbot.operations.cycle import SubmissionRequest

    database, binding, identity = setup
    request = SubmissionRequest(
        intent=intent(identity),
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )

    class HorizonRunner(Runner):
        async def evaluate_exits(self):
            return CycleStrategyResult(submissions=(request,), signal_count=1)

    gated = PromotionGatedStrategy(
        database.path, HorizonRunner(), binding=binding, identity=identity
    )
    result = await gated.evaluate()
    assert result.submissions == (request,)
    request = request.model_copy(update={"purpose": OrderPurpose.ENTRY})
    with pytest.raises(PaperEntryBlocked, match="EXIT_ONLY_RUNNER_RETURNED_ENTRY"):
        await gated.evaluate()


def test_factory_refuses_nonpaper_or_a_replacement_coordinator(setup):
    from quantbot.config import Settings
    from quantbot.domain import TradingMode

    database, binding, identity = setup
    runtime = SimpleNamespace(
        database=database, coordinator=Submitter(), settings=Settings(TRADING_MODE=TradingMode.LIVE)
    )
    with pytest.raises(ValueError, match="PAPER"):
        build_learner_paper_cycle(runtime, Runner(), binding=binding, identity=identity)
    runtime.settings = Settings()
    with pytest.raises(TypeError, match="ExecutionCoordinator"):
        build_learner_paper_cycle(runtime, Runner(), binding=binding, identity=identity)


def test_schema28_read_only_assessment_does_not_migrate_or_mutate(setup):
    import sqlite3

    deploy(setup)
    database, binding, identity = setup
    with sqlite3.connect(database.path) as connection:
        connection.execute("UPDATE schema_version SET version = 28")
    before = database.path.read_bytes()
    assert assess_paper_entry(database.path, binding=binding, identity=identity).allowed
    assert database.path.read_bytes() == before
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 28


@pytest.mark.asyncio
async def test_real_coordinator_keeps_risk_and_veto_gates(setup):
    from quantbot.execution import ExecutionCoordinator, OrderSubmissionBlocked
    from tests.integration.test_execution_coordinator import (
        AcceptingBroker,
        _drawdown,
        _mark_ready,
        _settings,
    )

    deploy(setup)
    database, binding, identity = setup
    _mark_ready(database)
    broker = AcceptingBroker(database)
    coordinator = ExecutionCoordinator(database, broker, _settings())
    gateway = PromotionGatedSubmitter(
        database.path, coordinator, binding=binding, identity=identity
    )
    with pytest.raises(OrderSubmissionBlocked) as failure:
        await gateway.submit(
            intent(identity),
            purpose=OrderPurpose.ENTRY,
            market_eligible=True,
            drawdown=_drawdown(),
            risk_approved=False,
            vetoes=None,
        )
    assert "RISK_NOT_APPROVED" in failure.value.decision.reasons
    assert "PORTFOLIO_VETOES_NOT_EVALUATED" in failure.value.decision.reasons
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_unpromoted_exit_only_cycle_cannot_create_a_qualified_day(setup, tmp_path):
    from quantbot.execution import ExecutionCoordinator
    from quantbot.operations.cycle import DataSyncResult
    from quantbot.operations.metrics import OperationalMetrics
    from tests.integration.test_execution_coordinator import AcceptingBroker, _settings

    deploy(setup, Stage.SHADOW)
    database, binding, identity = setup
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(False, reason="test", updated_at=NOW)

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=True)

    class Sync:
        async def sync(self):
            return DataSyncResult(
                healthy=True, completed_through=NOW, bars_stored=0, stale_symbols=(), reasons=()
            )

    runtime = SimpleNamespace(
        database=database,
        settings=_settings(),
        running_git_commit=identity.git_commit,
        coordinator=ExecutionCoordinator(database, AcceptingBroker(database), _settings()),
        paths=SimpleNamespace(lock=tmp_path / "runtime.lock"),
        recovery=Recovery(),
        data_sync=Sync(),
        metrics=OperationalMetrics(),
        runtime_context=lambda: None,
    )
    runner = Runner()
    cycle = build_learner_paper_cycle(runtime, runner, binding=binding, identity=identity)
    result = await cycle.run("synthetic-cycle", started_at=NOW, trading_date=NOW.date())
    assert result.status == "SUCCEEDED"
    assert runner.exit_calls == 1
    with database.transaction() as session:
        days = StorageRepository(session).list_qualification_days(
            identity.strategy_id, qualified=True
        )
        assert days == []
