"""Isolated PAPER wiring must not loosen holdouts, risk or account position limits."""
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.learner import forward
from quantbot.learner.execution_worker import PaperWorkerConfig, worker_risk_config
from quantbot.learner.inherited import InheritedBaseline
from quantbot.learner.paper_runner import PaperExecutionPolicy, paper_model_binding
from quantbot.research.intraday_accounting import record_intraday_attempt
from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
from quantbot.storage import Database, StorageRepository
from tests.integration.test_execution_coordinator import _long_position
from tests.unit.learner import test_paper_runner as fixture_module

context = fixture_module.context


def test_existing_declarations_are_checked_even_with_empty_isolated_store(tmp_path, monkeypatch):
    declared_path=(tmp_path/'declared.db').resolve()
    with Database.create(declared_path):
        pass
    called=[]
    def sealed(session, *_):
        rows=session.connection().exec_driver_sql('PRAGMA database_list').fetchall()
        called.append(Path(rows[0][2]))
        if called[-1].resolve()==declared_path:
            raise ValueError('sealed holdout')
    monkeypatch.setattr(forward.service,'guard_range',sealed)
    before=declared_path.read_bytes()
    token=forward._DECLARATIONS.set(declared_path)
    try:
        with Database.create(tmp_path/'isolated.db') as isolated:
            assert not forward._guard(isolated,datetime(2026,4,1,tzinfo=UTC),
                                      datetime(2026,4,2,tzinfo=UTC))
        assert called==[declared_path]
        assert declared_path.read_bytes()==before
    finally:
        forward._DECLARATIONS.reset(token)


def test_missing_declarations_fail_closed(tmp_path):
    token=forward._DECLARATIONS.set((tmp_path/'missing.db').resolve())
    try:
        with Database.create(tmp_path/'isolated.db') as isolated:
            assert not forward._guard(isolated,datetime.now(UTC),datetime.now(UTC))
    finally:
        forward._DECLARATIONS.reset(token)


def test_unrelated_trial_does_not_become_a_failed_dia_fit(tmp_path, monkeypatch):
    with Database.create(tmp_path/'local.db') as db:
        with db.transaction() as session:
            record_intraday_attempt(session,search_id='intraday:learner-other',
                configuration_hash='a'*64,start=datetime(2026,2,2).date(),
                end=datetime(2026,3,20).date(),now=datetime.now(UTC))
        def no_fit(*_,**__):
            pytest.fail('no training data permits a fit')
        monkeypatch.setattr(forward,'fit_intraday_model',no_fit)
        assert forward._train_challengers(tmp_path/'intraday',db,
            forward.ForwardConfig(symbols=('DIA',),experiment_id='dia-fixed-v2'),'a'*40
        )==(0,'TRAINING_DATA_WARMUP')


def test_dia_risk_roster_changes_only_explicit_fields_and_tightens_limits(context):
    _,broker,runner,_=context
    model=runner.model.model_copy(update={'training_symbols':('DIA',)})
    baseline=InheritedBaseline(account_id_hash='a'*64,captured_at=broker.now,positions=())
    config=PaperWorkerConfig(model_id=model.model_id,operational_database=Path('C:/paper.db'),
        experimental_symbol='DIA',policy=PaperExecutionPolicy(
            inherited=baseline,exclusive_learner_entry=True))
    planned=worker_risk_config(runner._config,config,model)
    assert planned.universe==('DIA','SPY') and planned.roster_size==1
    assert planned.risk_per_trade_bps<=25 and planned.max_position_value_bps<=500
    changed={'universe','roster_size','risk_per_trade_bps','max_position_value_bps'}
    assert {k:v for k,v in planned.model_dump().items() if k not in changed}=={
        k:v for k,v in runner._config.model_dump().items() if k not in changed}
    with pytest.raises(ValueError):
        worker_risk_config(runner._config,config,runner.model)
    with pytest.raises(ValueError):
        worker_risk_config(runner._config,config.model_copy(update={
            'policy':PaperExecutionPolicy(inherited=baseline)}),model)


def exclusive_runner(database,broker,runner):
    prior=runner._identity
    runner.policy=runner.policy.model_copy(update={'exclusive_learner_entry':True})
    runner.binding=paper_model_binding(runner.model,config=runner._config,policy=runner.policy,
        veto_policy=runner._veto_policy,market_data=runner._market_data)
    runner._identity=runner.binding.strategy_identity(git_commit='d'*40,deployed_at=broker.now)
    with database.transaction() as session:
        repo=StorageRepository(session)
        repo.save_strategy_deployment(runner._identity)
        repo.create_run('exclusive',runner._identity,started_at=broker.now)
        PromotionLedger(session).enter(PromotionState(
            strategy_id=runner._identity.strategy_id,strategy_version=runner._identity.version,
            configuration_hash=runner._identity.configuration_hash,stage=Stage.PAPER_OBSERVATION,
            reason='explicit synthetic fixture',actor='test',updated_at=broker.now))
    runner.bind_run('exclusive')
    return prior


@pytest.mark.asyncio
async def test_two_active_learner_entry_models_cannot_run(context):
    database,broker,runner,_=context
    exclusive_runner(database,broker,runner)
    assert not (await runner.evaluate()).submissions
    assert runner.last_reasons==('PAPER_OTHER_LEARNER_ENTRY_ACTIVE',)
    assert broker.submitted==[]


@pytest.mark.asyncio
async def test_new_position_outside_model_roster_still_consumes_exclusive_slot(context):
    database,broker,runner,supervisor=context
    prior=exclusive_runner(database,broker,runner)
    with database.transaction() as session:
        PromotionLedger(session).demote(prior.strategy_id,Stage.SHADOW,
            reason='synthetic explicit entry suspension',actor='test',now=broker.now)
    await supervisor.run_once()
    broker.positions=(_long_position().model_copy(update={'symbol':'SPY'}),)
    before=broker.positions
    assert not (await runner.evaluate()).submissions
    assert 'PAPER_MODEL_POSITION_LIMIT' in runner.last_reasons
    assert broker.positions==before and broker.submitted==[]


def test_risk_collection_covers_legacy_holdings_without_entry_authority(context):
    from quantbot.learner.inherited import InheritedPosition
    from quantbot.learner.paper_runner import pilot_risk_symbols
    _, broker, runner, _ = context
    baseline = InheritedBaseline(account_id_hash='a'*64, captured_at=broker.now,
        positions=(InheritedPosition(symbol='TLT', quantity=1, average_entry_price=100),))
    policy = PaperExecutionPolicy(inherited=baseline, exclusive_learner_entry=True)
    config = runner._config.model_copy(update={'universe': ('DIA', 'SPY')})
    assert pilot_risk_symbols(config, policy) == ('DIA', 'SPY', 'TLT')
    assert config.universe == ('DIA', 'SPY')
    assert policy.inherited == baseline


def test_pilot_daily_cursor_cannot_borrow_existing_universe_checkpoint(context):
    from quantbot.learner.paper_runner import PilotRiskDataSync
    from quantbot.operations.runners import MarketDataSync
    database, _, runner, _ = context
    # No retrieval is needed: this verifies the identities used by durable cursors.
    kwargs = dict(database=database, client=None, sessions=None,
                  config=runner._config, market_data=runner._market_data)
    original = MarketDataSync(**kwargs)
    pilot = PilotRiskDataSync(**kwargs)
    dia = PilotRiskDataSync(**{**kwargs, 'config': runner._config.model_copy(
        update={'universe': ('DIA', 'SPY')})})
    assert original.source_id != pilot.source_id != dia.source_id
    assert PilotRiskDataSync(**kwargs).source_id == pilot.source_id
    assert MarketDataSync(**{**kwargs, 'config': dia._config}).source_id == original.source_id
