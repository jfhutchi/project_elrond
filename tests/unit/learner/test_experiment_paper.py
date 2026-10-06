"""Pooled strategy binding preserves the retained PAPER risk algorithm."""

from datetime import timedelta
from importlib import import_module

import pytest

from quantbot.learner.experiment_contract import CandidateArtifact, DecisionRecord
from quantbot.learner.experiment_observation import build_observation_batch
from quantbot.research.manifest import content_id
from tests.fixtures.adaptive_paper import manifest, snapshot
from tests.unit.learner.test_experiment_research import fake_artifact, request


def module():
    return import_module("quantbot.learner.experiment_paper")


def cohort():
    frozen = manifest()
    source = snapshot()
    observation = build_observation_batch(frozen, source, now=source.received_at)
    candidate = CandidateArtifact(
        experiment_id=frozen.experiment_id,
        recorded_at=frozen.registered_at,
        manifest_hash=content_id(frozen),
        trial_id=request().trial_id,
        training_source_hash="a" * 64,
        validation_source_hash="b" * 64,
        artifact=fake_artifact(request()).model_copy(update={"intercept": 0.02}),
        synthetic=True,
    )
    decisions = tuple(
        DecisionRecord(
            experiment_id=frozen.experiment_id,
            recorded_at=source.received_at,
            model_id=candidate.model_id,
            features=row,
            score=0.02,
            action="BUY",
            reason="SYNTHETIC_ONLY",
            synthetic=True,
        )
        for row in observation.features
    )
    return frozen, candidate, decisions, {identity.symbol: source for identity in frozen.roster}


def test_pooled_prediction_preserves_exact_roster_and_provider_receipts():
    frozen, candidate, decisions, sources = cohort()
    predictions = module().pooled_predictions(frozen, candidate, decisions, sources=sources)
    assert {row.features.symbol for row in predictions} == {row.symbol for row in frozen.roster}
    assert all(
        row.model_id == candidate.model_id and row.order_authority is False for row in predictions
    )


def test_substituted_score_and_missing_source_refused():
    frozen, candidate, decisions, sources = cohort()
    changed = decisions[0].model_copy(update={"score": 0.5})
    with pytest.raises(ValueError, match="EXACT_POOLED_FORECAST"):
        module().pooled_predictions(frozen, candidate, (changed, *decisions[1:]), sources=sources)
    with pytest.raises(ValueError, match="SOURCE_MISSING"):
        module().pooled_predictions(frozen, candidate, decisions, sources={})


def test_untrained_symbol_duplicate_or_late_model_refused():
    frozen, candidate, decisions, sources = cohort()
    unknown = decisions[0].model_copy(
        update={
            "features": decisions[0].features.model_copy(update={"symbol": "UNDECLARED"}),
        }
    )
    with pytest.raises(ValueError, match="ROSTER"):
        module().pooled_predictions(frozen, candidate, (unknown,), sources=sources)
    with pytest.raises(ValueError, match="DUPLICATE"):
        module().pooled_predictions(
            frozen, candidate, (decisions[0], decisions[0]), sources=sources
        )
    late = candidate.model_copy(
        update={"recorded_at": decisions[0].recorded_at + timedelta(seconds=1)}
    )
    with pytest.raises(ValueError, match="MODEL"):
        module().pooled_predictions(frozen, late, decisions, sources=sources)


def test_live_factory_cannot_open_database_or_construct_service():
    from types import SimpleNamespace

    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.learner.paper_runner import PaperExecutionNotConfigured

    frozen, candidate, decisions, sources = cohort()
    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            TRADING_MODE=TradingMode.LIVE,
            BROKER_ENVIRONMENT=BrokerEnvironment.LIVE,
            LIVE_TRADING_ACKNOWLEDGED=False,
            EXPECTED_ACCOUNT_ID=None,
        )
    )
    with pytest.raises(PaperExecutionNotConfigured, match="PAPER"):
        module().build_pooled_paper_services(
            runtime,
            manifest=frozen,
            candidate=candidate,
            decisions=decisions,
            sources=sources,
            policy=None,
            classification=None,
            clock=lambda: decisions[0].recorded_at,
            clock_health=lambda now: True,
            holdout_guard=lambda start, end: True,
        )


def test_protected_range_refused_before_feature_reconstruction():
    frozen, candidate, decisions, sources = cohort()
    with pytest.raises(ValueError, match="PROTECTED"):
        module().pooled_predictions(
            frozen,
            candidate,
            decisions,
            sources=sources,
            holdout_guard=lambda start, end: False,
        )


@pytest.fixture
async def runtime_context(tmp_path):
    from tests.unit.learner.test_five_minute_paper import context as retained

    async for database, broker, original, _supervisor in retained.__wrapped__(tmp_path):
        frozen = manifest().model_copy(
            update={
                "registered_at": broker.now - timedelta(days=1),
                "prospective_start_at": broker.now - timedelta(minutes=35),
                "roster": tuple(
                    row.model_copy(update={"verified_at": broker.now - timedelta(days=2)})
                    for row in manifest().roster
                ),
            }
        )
        source = snapshot(opened=broker.now - timedelta(minutes=35, seconds=1))
        observation = build_observation_batch(frozen, source, now=source.received_at)
        candidate = CandidateArtifact(
            experiment_id=frozen.experiment_id,
            recorded_at=frozen.registered_at,
            manifest_hash=content_id(frozen),
            trial_id=request().trial_id,
            training_source_hash="a" * 64,
            validation_source_hash="b" * 64,
            artifact=fake_artifact(request()).model_copy(update={"intercept": 0.02}),
            synthetic=True,
        )
        decisions = tuple(
            DecisionRecord(
                experiment_id=frozen.experiment_id,
                recorded_at=source.received_at,
                model_id=candidate.model_id,
                features=row,
                score=0.02,
                action="BUY",
                reason="SYNTHETIC_ONLY",
                synthetic=True,
            )
            for row in observation.features
        )
        config = original._config.model_copy(
            update={
                "universe": tuple(row.symbol for row in frozen.roster) + ("SPY",),
            }
        )
        binding = module().pooled_paper_binding(
            frozen,
            candidate,
            config=config,
            policy=original.policy,
            veto_policy=original._veto_policy,
            market_data=original._market_data,
            classification=original._exposure,
        )
        identity = binding.strategy_identity(git_commit="e" * 40, deployed_at=candidate.recorded_at)
        kwargs = dict(
            database=database,
            broker=broker,
            sessions=original._sessions,
            manifest=frozen,
            candidate=candidate,
            decisions=decisions,
            sources={row.symbol: source for row in frozen.roster},
            config=config,
            identity=identity,
            binding=binding,
            policy=original.policy,
            veto_policy=original._veto_policy,
            market_data=original._market_data,
            classification=original._exposure,
            expected_account_id="paper-1",
            clock=lambda broker=broker: broker.now,
            clock_health=lambda now: True,
            holdout_guard=lambda start, end: True,
        )
        yield kwargs, broker


async def test_uncommissioned_pooled_runner_cannot_write_intents_or_send(runtime_context):
    from quantbot.storage.schema import order_intents

    kwargs, broker = runtime_context
    runner = module().PooledPaperRunner(**kwargs)
    result = await runner.evaluate()
    assert result.submissions == ()
    assert any("DEPLOYMENT" in reason or "PROMOTION" in reason for reason in runner.last_reasons)
    with kwargs["database"].transaction() as session:
        assert session.execute(order_intents.select()).all() == []
    assert broker.submitted == []


def test_pooled_constructor_rejects_risk_expansion(runtime_context):
    kwargs, broker = runtime_context
    expanded = kwargs["config"].model_copy(update={"max_position_value_bps": 501})
    with pytest.raises(ValueError, match="RISK"):
        module().PooledPaperRunner(**(kwargs | {"config": expanded}))
    assert not broker.submitted


async def _commissioned_pool(runtime_context, *, thin_first=False):
    from decimal import Decimal

    from quantbot.domain import Bar
    from quantbot.execution import ExecutionCoordinator
    from quantbot.learner.paper_deadlines import PaperDeadlineSupervisor
    from quantbot.market_data import MarketDataCache
    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
    from quantbot.risk.exposure import ExposureClassification, ExposureGroup
    from quantbot.storage import StorageRepository
    from quantbot.storage.schema import promotion_state
    from tests.integration.test_execution_coordinator import _settings

    kwargs, broker = runtime_context
    classification = ExposureClassification(
        version=kwargs["classification"].version + 1,
        source="SYNTHETIC TEST MANDATES ONLY",
        effective_from=broker.now.date(),
        mandate={
            **kwargs["classification"].mandate,
            **{
                row.symbol: frozenset({ExposureGroup.US_EQUITY})
                for row in kwargs["manifest"].roster
            },
        },
    )
    binding = module().pooled_paper_binding(
        kwargs["manifest"],
        kwargs["candidate"],
        config=kwargs["config"],
        policy=kwargs["policy"],
        veto_policy=kwargs["veto_policy"],
        market_data=kwargs["market_data"],
        classification=classification,
    )
    identity = binding.strategy_identity(
        git_commit="e" * 40, deployed_at=kwargs["candidate"].recorded_at
    )
    kwargs = kwargs | {"classification": classification, "binding": binding, "identity": identity}
    sessions = await kwargs["sessions"].sessions()
    market = kwargs["market_data"]
    provider = MarketDataCache.provider_key(
        market.provider, market.feed, market.adjustment, market.timeframe
    )
    database = kwargs["database"]
    with database.transaction() as session:
        repository = StorageRepository(session)
        for row in session.execute(promotion_state.select()).mappings():
            PromotionLedger(session).demote(
                row["strategy_id"],
                Stage.SHADOW,
                reason="isolated synthetic fixture",
                actor="test",
                now=broker.now,
            )
        repository.save_strategy_deployment(identity)
        PromotionLedger(session).enter(
            PromotionState(
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                stage=Stage.PAPER_OBSERVATION,
                reason="isolated synthetic fixture",
                actor="test",
                updated_at=broker.now,
            )
        )
        repository.create_run("pooled-run", identity, started_at=broker.now)
        for index, day in enumerate(sessions[:-1]):
            price = Decimal("100") + Decimal(index) / 10
            repository.save_bars(
                tuple(
                    Bar(
                        symbol=row.symbol,
                        timestamp=day.close_at,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume="1"
                        if thin_first and row.symbol == kwargs["manifest"].roster[0].symbol
                        else "10000000",
                        adjustment="1",
                    )
                    for row in kwargs["manifest"].roster
                ),
                provider=provider,
            )
    supervisor = PaperDeadlineSupervisor(
        database=database,
        broker=broker,
        coordinator=ExecutionCoordinator(database, broker, _settings()),
        identity=identity,
        expected_account_id="paper-1",
        lock_path=database.path.with_suffix(".lock"),
        clock=lambda broker=broker: broker.now,
    )
    await supervisor.run_once()
    runner = module().PooledPaperRunner(**kwargs)
    runner.bind_run("pooled-run")
    return kwargs, broker, runner, supervisor


async def test_pooled_forecast_uses_real_risk_vetoes_and_deadline_handoff(runtime_context):
    from quantbot.learner.paper_deadlines import deadline_entry_objections

    kwargs, broker, runner, supervisor = await _commissioned_pool(runtime_context)
    database = kwargs["database"]
    result = await runner.evaluate()
    assert len(result.submissions) == 1, runner.last_reasons
    submission = result.submissions[0]
    assert submission.intent.symbol == kwargs["manifest"].roster[0].symbol
    assert submission.risk_approved is True and submission.vetoes.allowed
    assert len(submission.vetoes.evaluated) == 6
    assert (
        deadline_entry_objections(database, submission.intent, account_id="paper-1", now=broker.now)
        == ()
    )
    assert not broker.submitted


async def test_measured_veto_of_top_symbol_attributes_selected_second_lot(
    runtime_context, tmp_path
):
    from types import SimpleNamespace

    from quantbot.learner.experiment_execution import ExperimentStrategyRunner
    from quantbot.learner.experiment_lots import LotEvent
    from quantbot.learner.experiment_store import ExperimentStore

    kwargs, broker, runner, supervisor = await _commissioned_pool(runtime_context, thin_first=True)
    store = ExperimentStore(tmp_path / "selected-lot.db", create=True)
    store.append(kwargs["manifest"], idempotency_key="manifest")
    strategy = ExperimentStrategyRunner(
        kwargs["manifest"],
        kwargs["decisions"],
        store=store,
        services=SimpleNamespace(runner=runner, deadlines=supervisor),
        simulation=True,
    )
    result = await strategy.evaluate()
    assert len(result.submissions) == 1, runner.last_reasons
    assert result.submissions[0].intent.symbol == kwargs["manifest"].roster[1].symbol
    events = store.read_as_of(kwargs["manifest"].experiment_id, broker.now)
    declared = next(
        e.decode(LotEvent)
        for e in events
        if e.kind == "LotEvent" and e.decode(LotEvent).kind == "ENTRY_DECLARED"
    )
    assert declared.decision_id == kwargs["decisions"][1].decision_id
    assert not broker.submitted
