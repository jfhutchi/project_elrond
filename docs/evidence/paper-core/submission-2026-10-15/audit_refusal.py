"""One-off reconstruction of one proven pre-POST refusal; never resets or sends an order."""
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from quantbot.domain import IntentState
from quantbot.paper_core.authority import RoleHeartbeat
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository

INTENT = 'core-entry-605c087ca600b455d6c15311a2d247e3d4d03889a26630ed81f162d29d6fe3a7'
HEARTBEAT = '24e84824a14d9b8f9e65660278da3e27d4e32ad224e64e514a7687336381de99'
RELEASE = '0734974b39088b449abe1279a385c89202756b61f3420aa910d6b2d772a46d4c'
AUTHORIZED = 'core-send-authorized-641deb680fdd4c0357f607087dd8e331cf92a240f7cdcd4d1296d7f01fe6ef20'
PROCESS = 'ef7f21ee-310f-4a62-94c2-7c3412863800'
SOURCE = '14352cc428dcd999750138d52fbd5f30347991a1'


def validate(connection):
    connection.row_factory = sqlite3.Row
    intent = dict(connection.execute('SELECT * FROM order_intents WHERE intent_id=?', (INTENT,)).fetchone())
    assert intent['state'] == 'ERROR'
    assert intent['client_order_id'] == 'qb-605c087ca600b455d6c15311a2d247e3d4d03889'
    assert not connection.execute('SELECT 1 FROM broker_orders WHERE client_order_id=?', (intent['client_order_id'],)).fetchone()
    events = [dict(r) for r in connection.execute('SELECT * FROM order_events WHERE intent_id=?', (INTENT,))]
    assert len(events) == 1 and events[0]['event_id'] == AUTHORIZED
    authorization = events[0]
    detail = json.loads(authorization['detail_json'])
    assert authorization['event_type'] == 'CORE_LOCKED_SEND_AUTHORIZED'
    assert detail['contract'] == 'core-final-send-v1' and detail['process_epoch'] == PROCESS
    payload = connection.execute('SELECT payload FROM core_role_heartbeats WHERE id=?', (HEARTBEAT,)).fetchone()[0]
    heartbeat = RoleHeartbeat.model_validate_json(payload)
    assert heartbeat.id == HEARTBEAT and heartbeat.role == 'trader'
    assert heartbeat.release_id == RELEASE and heartbeat.process_epoch == PROCESS
    assert heartbeat.config_id == detail['config_id']
    assert set(heartbeat.objections) == {f'CORE_ENTRY_SLOT_RESERVED:{INTENT}', 'CORE_CURRENT_CASH_OR_ACCOUNT_UNCERTIFIED'}
    authorized_at = datetime.fromisoformat(authorization['occurred_at'].replace('Z', '+00:00'))
    assert 0 <= (heartbeat.recorded_at - authorized_at).total_seconds() < 1
    # The original coordinator can only set ERROR with this gate exception before
    # POST. Later dispatch excludes ERROR. Require this to be the only intent that
    # could supply the gate failure in that heartbeat: all earlier links have a
    # durable definitive refusal timestamped before this intent was even created.
    for row in connection.execute('SELECT i.* FROM order_intents i JOIN core_intent_links l ON l.intent_id=i.intent_id WHERE l.role=\'ENTRY\' AND i.created_at<=?', (heartbeat.recorded_at.isoformat(),)):
        if row['intent_id'] == INTENT:
            continue
        assert row['created_at'] < intent['created_at'] and row['state'] == 'ERROR'
        prior = connection.execute('SELECT occurred_at,detail_json FROM order_events WHERE intent_id=? AND event_type=\'CORE_ENTRY_SEND_REFUSED\'', (row['intent_id'],)).fetchone()
        assert prior and prior['occurred_at'] < intent['created_at']
        prior_detail = json.loads(prior['detail_json'])
        assert prior_detail['definitively_unsent'] is True and prior_detail['state_at_refusal'] == 'ORDER_CREATED'
    return {
        'guard_contract': 'core-final-send-v1',
        'definitively_unsent': True,
        'state_at_refusal': 'ORDER_CREATED',
        'reasons': ['CORE_CURRENT_CASH_OR_ACCOUNT_UNCERTIFIED'],
        'source': 'AUDITED_HISTORICAL_PREPOST_RECONSTRUCTION',
        'intent_id': INTENT,
        'source_commit': SOURCE,
        'source_release_id': RELEASE,
        'authorization_event_id': AUTHORIZED,
        'authorization_sha256': content_id(authorization),
        'heartbeat_id': HEARTBEAT,
        'process_epoch': PROCESS,
        'approval_reference': 'USER_FIX_SUBMISSION_READINESS_20261015',
    }


def append(session, detail, *, now, fence_id, broker_probe_id):
    from sqlalchemy import select
    from quantbot.paper_core.authority import current_runtime_event
    from quantbot.paper_core.store import table
    repo = StorageRepository(session)
    assert validate(session.connection().connection.driver_connection) == detail
    current = current_runtime_event(session)
    assert current.action == 'FENCE_ENTRY' and current.id == fence_id
    stored = repo.get_order_intent(INTENT)
    assert stored and stored.state is IntentState.ERROR
    assert repo.get_broker_order_by_client_order_id(stored.client_order_id) is None
    assert not session.execute(select(table('order_intents').c.intent_id).where(
        table('order_intents').c.state.in_(['ORDER_CREATED','RISK_APPROVED','SUBMITTING','SUBMISSION_UNKNOWN'])
    )).first()
    assert {e.event_id for e in repo.list_order_events(intent_id=INTENT)} == {AUTHORIZED}
    event_id = 'core-send-refused-' + content_id({'intent': INTENT})
    repo.record_order_event(event_id, event_type='CORE_ENTRY_SEND_REFUSED', occurred_at=now,
        intent_id=INTENT, detail={**detail, 'audited_at': now.isoformat(), 'fence_id': fence_id,
        'broker_probe_id': broker_probe_id})
    return event_id
