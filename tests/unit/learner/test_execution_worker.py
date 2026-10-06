from datetime import timedelta

import pytest

from quantbot.learner.execution_worker import PaperWorkerConfig, load_cohort
from quantbot.learner.forward import _save
from tests.unit.learner.test_paper_runner import context as _paper_context


@pytest.fixture(name="context")
def execution_context(tmp_path, request):
    yield from _paper_context.__wrapped__(tmp_path, request)


def publish(root, runner):
    _save(root / "intraday/models" / (runner.model.model_id + ".json"),
          runner.model.model_dump(mode="json"))
    _save(root / "intraday/fits/fixture.json", {
        "model_ids": [runner.model.model_id], "search_ids": [],
    })
    _save(root / "intraday/predictions/2026-10-01/cohort.json", {
        "model_id": runner.model.model_id,
        "predictions": [p.model_dump(mode="json") for p in runner.predictions],
    })


def test_load_exact_frozen_model_and_current_complete_cohort(context, tmp_path):
    _, broker, runner, _ = context
    publish(tmp_path, runner)
    model, predictions = load_cohort(tmp_path, runner.model.model_id, now=broker.now)
    assert model == runner.model
    assert predictions == runner.predictions


def test_stale_or_different_model_is_not_traded(context, tmp_path):
    _, broker, runner, _ = context
    publish(tmp_path, runner)
    assert load_cohort(tmp_path, "a" * 64, now=broker.now) is None
    assert load_cohort(tmp_path, runner.model.model_id,
                       now=broker.now + timedelta(minutes=3)) is None


def test_no_live_or_extra_configuration_route():
    payload = {"model_id": "a" * 64, "operational_database": "C:/paper.db"}
    assert PaperWorkerConfig.model_validate(payload).mode == "PAPER"
    with pytest.raises(ValueError):
        PaperWorkerConfig.model_validate(payload | {"mode": "LIVE"})
    with pytest.raises(ValueError):
        PaperWorkerConfig.model_validate(payload | {"ignore_kill_switch": True})


@pytest.mark.parametrize('mode,match,expected', [('PAPER', True, 0), ('LIVE', True, 1),
                                               ('PAPER', False, 1)])
def test_deadline_launch_check_is_readonly_and_refuses_live_or_wrong_store(
    tmp_path, monkeypatch, mode, match, expected,
):
    from types import SimpleNamespace

    import quantbot.runtime
    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.learner import execution_worker

    database = tmp_path / 'paper.db'
    database.write_bytes(b'check must never open or mutate this')
    environment = tmp_path / 'fixture.env'
    environment.write_text('TRADING_MODE=PAPER\n')
    monkeypatch.setattr('sys.argv', ['deadline', '--commit', 'd' * 40, '--database',
                                    str(database), '--env-file', str(environment), '--check'])
    monkeypatch.setattr(execution_worker.subprocess, 'check_output',
                        lambda argv, **kwargs: 'd' * 40 if argv[1] == 'rev-parse' else '')
    monkeypatch.setattr(execution_worker, 'Settings', lambda: SimpleNamespace(
        TRADING_MODE=TradingMode(mode), BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
        EXPECTED_ACCOUNT_ID='synthetic-paper-account'))
    monkeypatch.setattr(quantbot.runtime, 'runtime_paths', lambda: SimpleNamespace(
        database=database if match else tmp_path / 'wrong.db'))
    def forbidden(*args, **kwargs):
        raise AssertionError('check cannot construct broker or open store')
    monkeypatch.setattr(quantbot.runtime, 'build_runtime', forbidden)
    before = database.read_bytes()
    assert execution_worker.main() == expected
    assert database.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('supervisor_alive', [False, True])
@pytest.mark.parametrize('scenario', ['fresh', 'waiting', 'altered_config', 'protected',
                                    'missing_account'])
async def test_worker_consumes_forecast_submits_paper_and_preserves_independent_exits(
    context, tmp_path, monkeypatch, supervisor_alive, scenario,
):
    import json
    from datetime import datetime
    from decimal import Decimal
    from types import SimpleNamespace

    from quantbot.domain import Fill, OrderSide
    from quantbot.execution import ExecutionCoordinator
    from quantbot.learner import execution_worker
    from quantbot.learner.paper_runner import PaperExecutionPolicy, paper_model_binding
    from quantbot.operations.cycle import DataSyncResult
    from quantbot.operations.metrics import OperationalMetrics
    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
    from quantbot.storage import StorageRepository
    from tests.integration.test_execution_coordinator import _long_position, _mark_ready, _settings

    database, broker, runner, supervisor = context
    _mark_ready(database)
    publish(tmp_path, runner)
    (tmp_path / "paper-execution.json").write_text(json.dumps({
        "model_id": runner.model.model_id, "operational_database": str(database.path),
    }))
    (tmp_path / "forward.json").write_text(json.dumps({"registry_database": str(database.path)}))
    config = runner._config.model_copy(update={
        "risk_per_trade_bps": min(25, runner._config.risk_per_trade_bps),
        "max_position_value_bps": min(500, runner._config.max_position_value_bps),
    })
    binding = paper_model_binding(runner.model, config=config, policy=PaperExecutionPolicy(),
                                 veto_policy=runner._veto_policy, market_data=runner._market_data)
    identity = binding.strategy_identity(git_commit="d" * 40, deployed_at=broker.now)
    with database.transaction() as session:
        repo = StorageRepository(session)
        repo.save_strategy_deployment(identity)
        PromotionLedger(session).enter(PromotionState(
            strategy_id=identity.strategy_id, strategy_version=identity.version,
            configuration_hash=identity.configuration_hash, stage=Stage.PAPER_OBSERVATION,
            reason="explicit synthetic unqualified PAPER pilot", actor="test",
            updated_at=broker.now,
        ))

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=True)

    class Sync:
        async def sync(self):
            return DataSyncResult(healthy=True, completed_through=broker.now, bars_stored=0,
                                  stale_symbols=(), reasons=())

    async def close():
        pass

    runtime = SimpleNamespace(
        database=database, broker=broker, config=runner._config, settings=_settings(),
        identity=runner._identity, running_git_commit="d" * 40,
        market_data_config=runner._market_data, sessions=runner._sessions,
        coordinator=ExecutionCoordinator(database, broker, _settings()), data_sync=Sync(),
        recovery=Recovery(), metrics=OperationalMetrics(), aclose=close,
        paths=SimpleNamespace(lock=tmp_path / "runtime.lock"), runtime_context=lambda: None,
    )
    monkeypatch.setattr(database, "close", lambda: None)
    import quantbot.runtime
    monkeypatch.setattr(quantbot.runtime, "build_runtime", lambda settings: runtime)
    monkeypatch.setattr(quantbot.runtime, "runtime_paths",
                        lambda: SimpleNamespace(database=database.path))
    guards = []
    monkeypatch.setattr(execution_worker, "_guard",
                        lambda *args: guards.append(args[1:]) or scenario != 'protected')
    if scenario != 'fresh':
        monkeypatch.setattr(execution_worker, 'load_cohort', lambda *args, **kwargs: None)
    if scenario == 'altered_config':
        path = tmp_path / 'paper-execution.json'
        path.write_text(json.dumps(json.loads(path.read_text()) | {
            'max_position_value_bps': 499}))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return broker.now

    monkeypatch.setattr(execution_worker, "datetime", Clock)
    # Only the independently invoked supervisor can refresh entry permission.
    if supervisor_alive:
        await supervisor.run_once()
    settings = _settings()
    if scenario == 'missing_account':
        settings = settings.model_copy(update={'EXPECTED_ACCOUNT_ID': None})
    receipt = await execution_worker.execute_paper_tick(
        tmp_path, database, settings, code_commit="d" * 40)
    if scenario in ('altered_config', 'protected', 'missing_account'):
        assert receipt['status'] == 'BLOCKED'
        assert receipt['trading_enabled'] is False
        assert not receipt.get('cleared_gate_codes')
        assert broker.submitted == []
        assert receipt['blockers'] == (['PROTECTED_FEATURE_WINDOW'] if scenario == 'protected'
                                      else ['PAPER_DEPLOYMENT_OR_CONFIG_REQUIRED'])
        return
    from quantbot.console import learner as console_learner
    from quantbot.learner.receipts import valid_worker_authority
    from quantbot.operations.monitor import _samples

    worker = dict(paper=receipt, promotion=receipt['promotion'],
                  trading_enabled=receipt['trading_enabled'], code_commit='d' * 40,
                  run_id='d59d7164-688b-4d39-bd3c-471944f30121',
                  status='BLOCKED', started_at=broker.now.isoformat(),
                  finished_at=broker.now.isoformat(), resolved_this_run=0,
                  blockers=receipt['blockers'])
    assert valid_worker_authority(tmp_path, worker)
    (tmp_path / 'worker.json').write_text(json.dumps(worker))
    deployment = dict(learner_commit='d' * 40, scheduled=True, interval_minutes=10)
    (tmp_path / 'deployment.json').write_text(json.dumps(deployment))
    monkeypatch.setattr(console_learner, 'datetime', Clock)
    assert _samples(tmp_path, database.path)['LEARNER']['phase'] == 'COMPLETED'
    assert console_learner._worker(tmp_path, deployment)['last_worker_at'] == broker.now.isoformat()
    assert console_learner._worker(tmp_path, deployment)['promotion'] == receipt['promotion']
    tampered = worker | {'paper': receipt | {'mode': 'LIVE'}}
    assert not valid_worker_authority(tmp_path, tampered)
    assert not valid_worker_authority(tmp_path, worker | {'code_commit': 'e' * 40})
    if scenario == 'waiting':
        assert receipt['status'] == 'WAITING'
        assert receipt['orders_submitted'] == 0
        assert receipt['trading_enabled'] is False
        assert receipt['blockers'] == ['PAPER_CURRENT_COHORT_UNAVAILABLE']
        assert set(receipt['cleared_gate_codes']) == {
            'PAPER_PROMOTION_REQUIRED', 'PAPER_EXECUTION_NOT_CONFIGURED'}
        assert broker.submitted == []
        assert len(guards) == 1
        return
    if not supervisor_alive:
        assert receipt['orders_submitted'] == 0
        assert receipt['trading_enabled'] is False
        assert broker.submitted == []
        return
    assert receipt["orders_submitted"] == 1, receipt
    assert len(guards) == 2  # Both model lineage and genuine cohort range checked.
    entry = broker.submitted[0]
    order = broker.orders[entry.client_order_id].model_copy(update={
        "filled_quantity": entry.quantity, "filled_average_price": Decimal("259"),
        "status": "filled",
    })
    broker.orders[entry.client_order_id] = order
    broker.fills = [Fill(fill_id="worker-fill", broker_order_id=order.broker_order_id,
                        symbol=entry.symbol, side=OrderSide.BUY, quantity=entry.quantity,
                        price=Decimal("259"), occurred_at=broker.now)]
    broker.positions = (_long_position(entry.quantity).model_copy(update={
        "symbol": entry.symbol, "market_price": Decimal("259"),
        "market_value": entry.quantity * 259,
    }),)
    # Halt the entry system; deadlines must still submit the risk-reducing exit.
    with database.transaction() as session:
        StorageRepository(session).set_kill_switch(True, reason="test halt", updated_at=broker.now)
    broker.now = runner.predictions[0].features.target_at
    assert (await supervisor.run_once()).exits_submitted == 1
    assert broker.submitted[-1].side is OrderSide.SELL
    # Resolve genuine elapsed forecast labels, then exercise the unchanged bounded
    # challenger procedure on an explicitly synthetic 25-session fixture. This is
    # source integration evidence, never a forward-performance claim.
    from quantbot.learner import forward
    from quantbot.learner.intraday import resolve_label
    from tests.unit.learner.test_intraday import bars

    source_bars = tuple(
        bar.model_copy(update={"symbol": symbol})
        for bar in bars(200) if bar.symbol == "AAPL"
        for symbol in runner.model.training_symbols
    ) + tuple(bar for bar in bars(200) if bar.symbol == "SPY")
    labels = tuple(resolve_label(
        p.features, source_bars, resolved_at=broker.now, receipt_at=broker.now,
        source_snapshot_hash="e" * 64,
    ) for p in runner.predictions)
    assert all(label is not None for label in labels)
    for index in range(25):
        delta = timedelta(days=index)
        shifted = []
        for label in labels:
            features = label.features.model_copy(update={
                field: getattr(label.features, field) + delta
                for field in ("decision_at", "feature_at", "target_at", "receipt_at")
            })
            shifted.append(label.model_copy(update={
                "features": features, "available_at": label.available_at + delta,
            }).model_dump(mode="json"))
        forward._save(tmp_path / "intraday/outcomes" /
                      features.decision_at.date().isoformat() / "fixture.json", {"labels": shifted})
    monkeypatch.setattr(forward, "_now", lambda: broker.now + timedelta(days=26))
    count, status = forward._train_challengers(
        tmp_path / "intraday", database,
        forward.ForwardConfig(symbols=runner.model.training_symbols), "d" * 40,
    )
    assert count == 2 and status == "CHALLENGER_TRAINED_NOT_PROMOTED"
    assert any(model.parent_model_id == runner.model.model_id
               for model in forward._models(tmp_path / "intraday"))
    # Adaptation cannot silently grant a new fitted model order authority.
    with database.transaction() as session:
        state = PromotionLedger(session).current(identity.strategy_id)
        assert state.stage is Stage.PAPER_OBSERVATION
