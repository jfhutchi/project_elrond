"""Explicit standby must stay read-only and fail closed on lost pilot evidence."""
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.learner import standby
from quantbot.learner.paper import PaperModelBinding
from quantbot.research.manifest import content_id


@pytest.fixture
def case(tmp_path, monkeypatch):
    now = datetime(2026, 10, 1, 14, tzinfo=UTC)
    original = tmp_path/'original'
    active = tmp_path/'active'
    original.mkdir()
    active.mkdir()
    op = tmp_path/'operational.db'
    model = 'a'*64
    pin = 'b'*40
    binding = PaperModelBinding(model_id=model, artifact_hash='c'*64,
        code_hash='d'*64, execution_config_hash='e'*64)
    identity = binding.strategy_identity(git_commit=pin, deployed_at=now)
    old = 'learner-'+'f'*64
    config = dict(mode='PAPER', experimental_symbol='DIA', model_id=model,
        operational_database=str(op), risk_per_trade_bps=25, max_position_value_bps=500,
        policy=dict(exclusive_learner_entry=True, stop_loss_bps=100, inherited={'proof': True}))
    (active/'paper-execution.json').write_text(json.dumps(config))
    paper = dict(binding=binding.model_dump(), identity=identity.model_dump(mode='json'),
        status='WAITING', blockers=['PAPER_CURRENT_COHORT_UNAVAILABLE'])
    receipt = dict(code_commit=pin, finished_at=now.isoformat(), paper=paper)
    (active/'worker.json').write_text(json.dumps(receipt))
    record = dict(schema='ELROND_ENTRY_STANDBY_V1', original_strategy_id=old,
        active_strategy_id=identity.strategy_id, active_commit=pin,
        paper_configuration_hash=content_id(config), active_root=str(active),
        operational_database=str(op), recorded_at=now.isoformat(),
        authority='EXPLICIT_OPERATOR_PAPER_PILOT_SWITCH')
    (original/'entry-standby.json').write_text(json.dumps(record))
    with sqlite3.connect(op) as c:
        c.execute('CREATE TABLE promotion_state(strategy_id TEXT,stage TEXT)')
        c.executemany('INSERT INTO promotion_state VALUES (?,?)',
            [(old,'SHADOW'),(identity.strategy_id,'PAPER_OBSERVATION')])
        c.execute('CREATE TABLE strategy_deployments(strategy_id TEXT,version TEXT,'
                  'git_commit TEXT,configuration_hash TEXT)')
        c.execute('INSERT INTO strategy_deployments VALUES (?,?,?,?)',
            (identity.strategy_id,identity.version,pin,identity.configuration_hash))
    monkeypatch.setattr(standby,'consent_recorded',lambda *_args,**_kwargs: True)
    return original, active, op, now, record, config, receipt


def test_verified_standby_does_not_grant_authority_or_write_database(case):
    original,_,op,now,*_=case
    before=op.read_bytes()
    result=standby.standby_status(original,now=now)
    assert result['status']=='STANDBY' and result['promotion']=='SHADOW'
    assert not result['trading_enabled'] and result['orders_submitted']==0
    assert result['blockers']==['PAPER_CURRENT_COHORT_UNAVAILABLE']
    assert op.read_bytes()==before


@pytest.mark.parametrize('failure',['original-active','target-demoted','lost-deployment',
    'configuration-changed','stale-heartbeat','wrong-pin','target-missing-binding','missing-consent'])
def test_invalid_standby_cannot_mask_missing_evidence(case,monkeypatch,failure):
    original,active,op,now,record,config,receipt=case
    with sqlite3.connect(op) as c:
        if failure=='original-active':
            c.execute("UPDATE promotion_state SET stage='PAPER_OBSERVATION' WHERE strategy_id=?",
                      (record['original_strategy_id'],))
        elif failure=='target-demoted':
            c.execute("UPDATE promotion_state SET stage='SHADOW' WHERE strategy_id=?",
                      (record['active_strategy_id'],))
        elif failure=='lost-deployment':
            c.execute('DELETE FROM strategy_deployments')
    if failure=='configuration-changed':
        config['max_position_value_bps']=600
        (active/'paper-execution.json').write_text(json.dumps(config))
    elif failure=='stale-heartbeat':
        receipt['finished_at']=(now-timedelta(minutes=11)).isoformat()
    elif failure=='wrong-pin':
        receipt['code_commit']='0'*40
    elif failure=='target-missing-binding':
        receipt['paper'].pop('binding')
    elif failure=='missing-consent':
        monkeypatch.setattr(standby,'consent_recorded',lambda *_args,**_kwargs: False)
    (active/'worker.json').write_text(json.dumps(receipt))
    with pytest.raises((ValueError,KeyError)):
        standby.standby_status(original,now=now)


def test_real_target_setup_failure_remains_visible(case):
    original,active,_,now,_,_,receipt=case
    receipt['paper']['blockers']=['PAPER_EXECUTION_NOT_CONFIGURED']
    (active/'worker.json').write_text(json.dumps(receipt))
    assert standby.standby_status(original,now=now)['blockers']==['PAPER_EXECUTION_NOT_CONFIGURED']


def test_progress_labels_verified_standby_and_preserves_real_setup_alerts(tmp_path):
    from quantbot.learner.progress import monitor_progress
    from quantbot.operations.alerts import AlertStore
    now=datetime(2026,10,1,14,tzinfo=UTC)
    receipt=dict(status='BLOCKED',paper={'status':'STANDBY'},operator_consent='RECORDED',
        blockers=['FORECAST_LOOKBACK_WARMUP'],intraday={'progress_expected':False})
    with AlertStore(tmp_path/'alerts.db') as store:
        result=monitor_progress(store,receipt,now=now)
        assert result['paper_status']=='STANDBY_FORECASTS_ACTIVE'
        assert not result['active_alerts']
        receipt['blockers']=['PAPER_EXECUTION_NOT_CONFIGURED']
        result=monitor_progress(store,receipt,now=now)
        assert result['paper_status']=='MISSING_COMPONENT'
        assert any(a['code']=='PAPER_EXECUTION_NOT_CONFIGURED' for a in result['active_alerts'])


@pytest.mark.parametrize('valid',[True,False])
def test_worker_standby_preserves_forecasts_and_never_calls_entry(tmp_path,monkeypatch,valid):
    from quantbot.config import Settings
    from quantbot.learner.worker import run_cycle
    from tests.unit.learner.test_worker import worker_store
    root,database=worker_store(tmp_path)
    for name in ('forward.json','paper-execution.json','entry-standby.json'):
        (root/name).write_text('{}')
    async def forecast(*_args,**_kwargs):
        return dict(blockers=['PAPER_PROMOTION_REQUIRED','PAPER_EXECUTION_NOT_CONFIGURED',
            'FORECAST_LOOKBACK_WARMUP'],progress_expected=False)
    async def unexpected_entry(*_args,**_kwargs):
        pytest.fail('standby must never invoke original entry worker')
    def status(*_args,**_kwargs):
        if not valid:
            raise ValueError('synthetic invalid standby')
        return dict(status='STANDBY',trading_enabled=False,promotion='SHADOW',
            blockers=['PAPER_CURRENT_COHORT_UNAVAILABLE'],cleared_gate_codes=[
                'PAPER_PROMOTION_REQUIRED','PAPER_EXECUTION_NOT_CONFIGURED',
                'PAPER_OBSERVATION_NOT_EARNED'])
    monkeypatch.setattr('quantbot.learner.forward.run_forward_cycle',forecast)
    monkeypatch.setattr('quantbot.learner.execution_worker.execute_paper_tick',unexpected_entry)
    monkeypatch.setattr(standby,'standby_status',status)
    try:
        result=run_cycle(root,code_commit='a'*40,settings=Settings())
        assert result['intraday']['blockers']==['PAPER_PROMOTION_REQUIRED',
            'PAPER_EXECUTION_NOT_CONFIGURED','FORECAST_LOOKBACK_WARMUP']
        assert not result['trading_enabled']
        assert 'PAPER_PROMOTION_REQUIRED' not in result['blockers']
        assert 'PAPER_EXECUTION_NOT_CONFIGURED' not in result['blockers']
        if valid:
            assert result['progress']['paper_status']=='STANDBY_FORECASTS_ACTIVE'
        else:
            assert 'OUTCOME_PROCESSING_FAILED' in result['blockers']
            assert any(a['code']=='OUTCOME_PROCESSING_FAILED' for a in
                result['progress']['active_alerts'])
    finally:
        database.close()


@pytest.mark.parametrize('change',[None,'trading','orders','target','status','promotion'])
def test_receipt_reader_accepts_only_verified_no_order_standby(case,change):
    from quantbot.learner.receipts import valid_worker_authority
    original,_,_,now,*_=case
    # Use a fixed clock through the helper to test receipt shape independently of wall time.
    verified=standby.standby_status(original,now=now)
    from unittest.mock import patch
    receipt=dict(promotion='SHADOW',trading_enabled=False,paper=verified.copy())
    if change=='trading':
        receipt['trading_enabled']=True
    elif change=='orders':
        receipt['paper']['orders_submitted']=1
    elif change=='target':
        receipt['paper']['active_strategy_id']='learner-'+'0'*64
    elif change=='status':
        receipt['paper']['status']='SUCCEEDED'
    elif change=='promotion':
        receipt['paper']['promotion']='PAPER_OBSERVATION'
    with patch.object(standby,'standby_status',return_value=verified):
        assert valid_worker_authority(original,receipt) is (change is None)


def test_receipt_reader_rejects_unverified_standby(case,monkeypatch):
    from quantbot.learner.receipts import valid_worker_authority
    original,_,_,now,*_=case
    receipt=dict(promotion='SHADOW',trading_enabled=False,
        paper=standby.standby_status(original,now=now))
    def invalid(*_args,**_kwargs): raise ValueError('lost standby proof')
    monkeypatch.setattr(standby,'standby_status',invalid)
    assert not valid_worker_authority(original,receipt)
