from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.domain import Bar
from quantbot.execution import ExecutionCoordinator
from quantbot.learner.intraday import fit_intraday_model, predict_intraday
from quantbot.learner.paper_deadlines import PaperDeadlineSupervisor
from quantbot.learner.paper_runner import (
    IntradayPaperRunner,
    PaperExecutionPolicy,
    paper_model_binding,
)
from quantbot.operations.runners import MarketDataSettings
from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
from quantbot.risk.policy_versions import active_policy_version
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig, XNYSSession
from tests.integration.test_execution_coordinator import _long_position, _settings
from tests.integration.test_runtime_wiring import _config_payload
from tests.unit.learner.test_intraday import OPEN, bars, features, historical
from tests.unit.learner.test_paper_deadlines import Broker


@pytest.fixture
def context(tmp_path, request):
    database = Database.create(tmp_path / "paper.db")
    now = OPEN + timedelta(minutes=160)
    labels = tuple(
        label.model_copy(update={"features": label.features.model_copy(update={"symbol": symbol})})
        for label in historical()
        for symbol in ("QQQ", "TLT")
    )
    model = fit_intraday_model(
        labels,
        validation_start=OPEN + timedelta(minutes=71),
        created_at=OPEN + timedelta(minutes=151),
        code_commit="c" * 40,
        family="mean",
    )
    rows = tuple(
        row.model_copy(update={"symbol": symbol})
        for row in features(bars(200), minute=160)
        for symbol in ("QQQ", "TLT")
    )
    predictions = predict_intraday(model, rows, recorded_at=now)
    config = StrategyConfig.model_validate(
        _config_payload() | {"universe": ["SPY", "QQQ", "TLT"], "roster_size": 1}
    )
    data_settings = MarketDataSettings()
    with database.transaction() as session:
        version, _ = active_policy_version(StorageRepository(session), now=now)
    if getattr(request, 'param', None) == 'inherited':
        from quantbot.learner.inherited import InheritedBaseline
        from quantbot.learner.paper_deadlines import PaperDeadline

        policy = PaperExecutionPolicy(inherited=InheritedBaseline(
            account_id_hash=PaperDeadline.account_hash('paper-1'),
            captured_at=now - timedelta(minutes=1),
            positions=tuple(dict(symbol=symbol,quantity=Decimal('1'),
                                 average_entry_price=Decimal('450')) for symbol in ('QQQ','TLT'))))
    else:
        policy = PaperExecutionPolicy()
    binding = paper_model_binding(
        model, config=config, policy=policy, veto_policy=version.policy(), market_data=data_settings
    )
    identity = binding.strategy_identity(git_commit="d" * 40, deployed_at=now)
    sessions = tuple(
        XNYSSession(
            session_date=(now - timedelta(days=i)).date(),
            open_at=OPEN - timedelta(days=i),
            close_at=OPEN - timedelta(days=i) + timedelta(hours=6, minutes=30),
        )
        for i in range(130, -1, -1)
    )

    class Calendar:
        async def sessions(self):
            return sessions

    broker = Broker(database)
    broker.now = now
    with database.transaction() as session:
        repo = StorageRepository(session)
        repo.save_strategy_deployment(identity)
        PromotionLedger(session).enter(
            PromotionState(
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                stage=Stage.PAPER_OBSERVATION,
                reason="synthetic fixture",
                actor="test",
                updated_at=now,
            )
        )
        repo.create_run("entry-run", identity, started_at=now)
        repo.save_account_snapshot("opening", broker.account_snapshot.account, (), captured_at=OPEN)
        from quantbot.market_data import MarketDataCache

        source = MarketDataCache.provider_key(
            data_settings.provider,
            data_settings.feed,
            data_settings.adjustment,
            data_settings.timeframe,
        )
        for index, session_day in enumerate(sessions[:-1]):
            price = Decimal(100) + Decimal(index) / 10
            repo.save_bars(
                [
                    Bar(
                        symbol=symbol,
                        timestamp=session_day.close_at,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=Decimal("10000000"),
                        adjustment=Decimal(1),
                    )
                    for symbol in ("SPY", "QQQ", "TLT")
                ],
                provider=source,
            )
    supervisor = PaperDeadlineSupervisor(
        database=database,
        broker=broker,
        coordinator=ExecutionCoordinator(database, broker, _settings()),
        identity=identity,
        expected_account_id="paper-1",
        lock_path=database.path.with_suffix(".lock"),
        clock=lambda: broker.now,
    )
    runner = IntradayPaperRunner(
        database=database,
        broker=broker,
        sessions=Calendar(),
        model=model,
        predictions=predictions,
        config=config,
        identity=identity,
        binding=binding,
        policy=policy,
        veto_policy=version.policy(),
        market_data=data_settings,
        expected_account_id="paper-1",
        clock=lambda: broker.now,
    )
    runner.bind_run("entry-run")
    yield database, broker, runner, supervisor
    database.close()


@pytest.mark.asyncio
async def test_real_sizing_and_all_vetoes_produce_one_handed_off_entry(context):
    database, broker, runner, supervisor = context
    await supervisor.run_once()
    result = await runner.evaluate()
    assert len(result.submissions) == 1, runner.last_reasons
    request = result.submissions[0]
    assert request.risk_approved is True
    assert request.vetoes.allowed
    assert request.intent.quantity > 0
    assert (
        request.intent.quantity * Decimal(str(runner.predictions[0].features.reference_close))
        <= 2000
    )
    with database.transaction() as session:
        records = StorageRepository(session).list_signals()
        assert any(record.payload.get("kind") == "LEARNER_PAPER_DEADLINE_V1" for record in records)
    assert (await runner.evaluate()).submissions == ()


@pytest.mark.asyncio
async def test_entry_clock_is_measured_after_broker_response(context):
    _, broker, runner, supervisor = context
    await supervisor.run_once()
    original = broker.get_market_clock

    async def delayed_clock():
        broker.now += timedelta(seconds=2)
        return await original()

    broker.get_market_clock = delayed_clock
    result = await runner.evaluate()
    assert len(result.submissions) == 1, runner.last_reasons


@pytest.mark.asyncio
async def test_missing_supervisor_and_stale_minute_price_refuse_entries(context):
    _, broker, runner, supervisor = context
    assert (await runner.evaluate()).submissions == ()
    assert "DEADLINE_SUPERVISOR_UNAVAILABLE" in runner.last_reasons
    broker.now += timedelta(minutes=3)
    await supervisor.run_once()
    assert (await runner.evaluate()).submissions == ()
    assert "PAPER_PREDICTION_STALE" in runner.last_reasons


@pytest.mark.asyncio
async def test_existing_position_and_pending_buy_cannot_be_double_reserved(context):
    _, broker, runner, supervisor = context
    await supervisor.run_once()
    broker.positions = (_long_position(Decimal("1")).model_copy(update={"symbol": "QQQ"}),)
    assert (await runner.evaluate()).submissions == ()
    assert "PAPER_SYMBOL_ALREADY_HELD" in runner.last_reasons
    broker.positions = ()
    from tests.integration.test_execution_coordinator import _accepted, _intent

    order = _accepted(_intent()).model_copy(update={"symbol": "QQQ"})
    broker.orders[order.client_order_id] = order
    assert (await runner.evaluate()).submissions == ()
    assert "PAPER_PENDING_BUY_RISK" in runner.last_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize('context',['inherited'],indirect=True)
async def test_grandfathered_roster_is_monitored_without_duplicate_entry_or_adoption(context):
    db, broker, runner, supervisor = context
    await supervisor.run_once()
    broker.positions = tuple(_long_position(Decimal('1')).model_copy(update={'symbol':symbol})
                             for symbol in ('QQQ','TLT'))
    result = await runner.evaluate()
    assert result.submissions == ()
    assert 'PAPER_MODEL_POSITION_LIMIT' not in runner.last_reasons
    assert 'PAPER_SYMBOL_ALREADY_HELD' in runner.last_reasons
    with db.transaction() as session:
        rows = StorageRepository(session).list_signals()
        assert len([r for r in rows if r.payload.get('kind') ==
                    'LEARNER_INHERITED_MONITOR_V1']) == 2
        assert not any(r.payload.get('kind') == 'LEARNER_PAPER_DEADLINE_V1' for r in rows)
        assert not StorageRepository(session).list_unresolved_order_intents()


@pytest.mark.asyncio
async def test_real_portfolio_veto_refusal_cannot_be_overridden_by_model_score(context):
    database, _, runner, supervisor = context
    await supervisor.run_once()
    with database.transaction() as session:
        from sqlalchemy import update

        from quantbot.storage.schema import bars as bar_table

        session.execute(update(bar_table).values(volume="1"))
    assert (await runner.evaluate()).submissions == ()
    assert runner.last_reasons


@pytest.mark.asyncio
async def test_forecast_cohort_cannot_be_changed_after_binding(context):
    _, _, runner, _ = context
    with pytest.raises(ValueError, match="model"):
        IntradayPaperRunner(
            database=runner._database,
            broker=runner._broker,
            sessions=runner._sessions,
            model=runner.model,
            predictions=tuple(
                item.model_copy(update={"score": item.score + 1}) for item in runner.predictions
            ),
            config=runner._config,
            identity=runner._identity,
            binding=runner.binding,
            policy=runner.policy,
            veto_policy=runner._veto_policy,
            market_data=runner._market_data,
            expected_account_id="paper-1",
        )


def test_model_training_commit_may_predate_paper_deployment(context):
    _, _, runner, _ = context
    deployment = runner.binding.strategy_identity(
        git_commit="d" * 40, deployed_at=runner._identity.deployment_timestamp
    )
    rebuilt = IntradayPaperRunner(
        database=runner._database,
        broker=runner._broker,
        sessions=runner._sessions,
        model=runner.model,
        predictions=runner.predictions,
        config=runner._config,
        identity=deployment,
        binding=runner.binding,
        policy=runner.policy,
        veto_policy=runner._veto_policy,
        market_data=runner._market_data,
        expected_account_id="paper-1",
    )
    assert rebuilt._identity.git_commit == "d" * 40
    assert rebuilt.model.code_commit == "c" * 40


@pytest.mark.asyncio
async def test_production_composition_submits_then_independent_deadline_exits(context, tmp_path):
    from types import SimpleNamespace

    from quantbot.domain import Fill, OrderSide
    from quantbot.learner.paper_runner import build_intraday_paper_services
    from quantbot.operations.cycle import DataSyncResult
    from quantbot.operations.metrics import OperationalMetrics
    from tests.integration.test_execution_coordinator import _mark_ready

    database, broker, original, _ = context
    _mark_ready(database)

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=True)

    class RiskSync:
        async def sync(self):
            return DataSyncResult(
                healthy=True,
                completed_through=broker.now - timedelta(days=1),
                bars_stored=0,
                stale_symbols=(),
                reasons=(),
            )

    runtime = SimpleNamespace(
        database=database,
        broker=broker,
        config=original._config,
        settings=_settings(),
        identity=original._identity,
        running_git_commit=original._identity.git_commit,
        market_data_config=original._market_data,
        sessions=original._sessions,
        coordinator=ExecutionCoordinator(database, broker, _settings()),
        data_sync=RiskSync(),
        recovery=Recovery(),
        metrics=OperationalMetrics(),
        paths=SimpleNamespace(lock=tmp_path / "runtime.lock"),
        runtime_context=lambda: None,
    )
    services = build_intraday_paper_services(
        runtime, model=original.model, predictions=original.predictions, clock=lambda: broker.now
    )
    await services.deadlines.run_once()
    services.runner.bind_run("composed-run")
    result = await services.cycle.run(
        "composed-run", started_at=broker.now, trading_date=broker.now.date()
    )
    assert result.orders_submitted == 1
    assert len(broker.submitted) == 1
    entry = broker.submitted[0]
    order = broker.orders[entry.client_order_id].model_copy(
        update={
            "filled_quantity": entry.quantity,
            "filled_average_price": Decimal("259"),
            "status": "filled",
        }
    )
    broker.orders[entry.client_order_id] = order
    broker.fills = [
        Fill(
            fill_id="composed-fill",
            broker_order_id=order.broker_order_id,
            symbol=entry.symbol,
            side=OrderSide.BUY,
            quantity=entry.quantity,
            price=Decimal("259"),
            occurred_at=broker.now,
        )
    ]
    broker.positions = (
        _long_position(entry.quantity).model_copy(
            update={
                "symbol": entry.symbol,
                "market_price": Decimal("259"),
                "market_value": entry.quantity * 259,
            }
        ),
    )
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(
            True, reason="synthetic halt", updated_at=broker.now
        )
    broker.now = original.predictions[0].features.target_at
    assert (await services.deadlines.run_once()).exits_submitted == 1
    assert broker.submitted[-1].side is OrderSide.SELL
    assert broker.submitted[-1].quantity == entry.quantity


@pytest.mark.asyncio
async def test_cohort_sync_does_not_upgrade_failed_operational_health(context):
    from quantbot.learner.paper_runner import IntradayCohortSync
    from quantbot.operations.cycle import DataSyncResult

    _, broker, runner, _ = context

    class BadRiskSync:
        async def sync(self):
            return DataSyncResult(
                healthy=False,
                completed_through=broker.now,
                bars_stored=0,
                stale_symbols=(),
                reasons=("RISK_DATA_FAILED",),
                risk_healthy=False,
                stream_connected=False,
            )

    result = await IntradayCohortSync(BadRiskSync(), runner).sync()
    assert not result.healthy and not result.risk_healthy and not result.stream_connected
    assert "RISK_DATA_FAILED" in result.reasons
