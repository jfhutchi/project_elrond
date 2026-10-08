"""A separate selected-entry expiry must not invalidate research-worker liveness."""
import json
from datetime import timedelta

import pytest

from quantbot.console.market_status import MarketStatus
from quantbot.learner import standby
from quantbot.learner.receipts import valid_worker_authority
from quantbot.operations.monitor import _samples, run_monitor
from tests.unit.learner.test_standby import case as case


def test_fresh_shadow_completion_survives_selected_entry_expiry(case):
    root,active,op,now,record,*_=case
    paper=standby.standby_status(root,now=now)
    completed=now+timedelta(minutes=11)
    doc=dict(run_id='d59d7164-688b-4d39-bd3c-471944f30121',code_commit='a'*40,
             started_at=completed.isoformat(),finished_at=completed.isoformat(),
             status='BLOCKED',trading_enabled=False,promotion='SHADOW',paper=paper)
    (root/'worker.json').write_text(json.dumps(doc))
    (root/'deployment.json').write_text(json.dumps(dict(learner_commit='a'*40)))
    before=op.read_bytes()
    with pytest.raises(standby.StandbyVerificationError,match='STANDBY_ENTRY_HEARTBEAT_STALE'):
        standby.standby_status(root,now=completed)
    sample=_samples(root,op)['LEARNER']
    assert sample is not None and sample['phase']=='COMPLETED'
    assert sample['at']==completed.isoformat()
    assert op.read_bytes()==before
    assert not valid_worker_authority(root,doc)
    market=MarketStatus(checked_at=completed,valid_until=completed+timedelta(seconds=30),
                       reported_open=False,expected_open=False,clock_status='HEALTHY',
                       closed_session_consistent=True)
    receipt=run_monitor(root,op,root/'monitor',now=completed,market=market)
    codes={a['code'] for a in receipt['active_alerts']}
    assert 'LEARNER_HEARTBEAT_STALE' not in codes
    assert receipt['measurements']['PAPER_ENTRY']['status']=='EXPECTED_WAIT'
    assert 'STANDBY_ENTRY_HEARTBEAT_STALE' not in codes
    later=now+timedelta(minutes=16)
    doc.update(started_at=later.isoformat(),finished_at=later.isoformat())
    (root/'worker.json').write_text(json.dumps(doc))
    market=MarketStatus(checked_at=later,valid_until=later+timedelta(seconds=30),
                       reported_open=True,expected_open=True,clock_status='HEALTHY',
                       session_open=now)
    receipt=run_monitor(root,op,root/'monitor',now=later,market=market)
    codes={a['code'] for a in receipt['active_alerts']}
    assert 'LEARNER_HEARTBEAT_STALE' not in codes
    assert 'STANDBY_ENTRY_HEARTBEAT_STALE' in codes
    assert receipt['measurements']['PAPER_ENTRY']['status']=='UNVERIFIED'
    doc['trading_enabled']=True
    (root/'worker.json').write_text(json.dumps(doc))
    assert _samples(root,op)['LEARNER'] is None
    doc['trading_enabled']=False
    (root/'worker.json').write_text(json.dumps(doc))
    assert _samples(root,op)['LEARNER'] is not None
    doc['paper']['orders_submitted']=1
    (root/'worker.json').write_text(json.dumps(doc))
    assert _samples(root,op)['LEARNER'] is None
    doc['paper']['orders_submitted']=0
    (root/'worker.json').write_text(json.dumps(doc))
    assert _samples(root,op)['LEARNER'] is not None
    selected=json.loads((active/'paper-execution.json').read_text())
    selected['risk_per_trade_bps']=26
    (active/'paper-execution.json').write_text(json.dumps(selected))
    assert _samples(root,op)['LEARNER'] is None


def test_closed_observation_completion_is_liveness_not_entry_authority(case):
    root, _, op, now, *_ = case
    completed = now + timedelta(minutes=11)
    market = MarketStatus(checked_at=completed, valid_until=completed+timedelta(seconds=30),
                          reported_open=False, expected_open=False, clock_status='HEALTHY',
                          closed_session_consistent=True)
    paper = standby.observation_standby_status(root, now=completed, market=market)
    doc = dict(run_id='d59d7164-688b-4d39-bd3c-471944f30121', code_commit='a'*40,
               started_at=completed.isoformat(), finished_at=completed.isoformat(),
               status='BLOCKED', trading_enabled=False, promotion='SHADOW', paper=paper)
    (root/'worker.json').write_text(json.dumps(doc))
    (root/'deployment.json').write_text(json.dumps(dict(learner_commit='a'*40)))
    assert _samples(root, op)['LEARNER']['phase'] == 'COMPLETED'
    assert not valid_worker_authority(root, doc)
    paper['entry_expected_wait'] = False
    (root/'worker.json').write_text(json.dumps(doc))
    assert _samples(root, op)['LEARNER'] is None
