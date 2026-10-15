"""Exercise historical reconstruction only on a WAL-aware backup clone."""
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from sqlalchemy import text
from quantbot.paper_core.authority import RuntimeEvent, current_runtime_event
from quantbot.paper_core.store import put
from quantbot.storage import Database, StorageRepository
from audit_refusal import validate, append, INTENT, HEARTBEAT, AUTHORIZED

root=Path('D:/Elrond/review/submission-20261015')
source=sqlite3.connect('file:D:/Elrond/state/quantbot-paper-fresh-20260908.db?mode=ro',uri=True)
clone=root/'audit-offline-clone.db'
assert not clone.exists()
c=sqlite3.connect(clone)
source.backup(c);source.close()
assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
detail=validate(c)
checks=['exact_source_evidence_valid']
for state in ('SUBMITTING','SUBMISSION_UNKNOWN','ORDER_CREATED'):
    c.execute('UPDATE order_intents SET state=? WHERE intent_id=?',(state,INTENT))
    try: validate(c)
    except AssertionError: checks.append(state+'_refused')
    else: raise RuntimeError('state accepted: '+state)
    c.rollback()
c.execute("UPDATE core_role_heartbeats SET payload=json_set(payload,'$.process_epoch','wrong') WHERE id=?",(HEARTBEAT,))
try: validate(c)
except AssertionError: checks.append('changed_process_refused')
else: raise RuntimeError('changed evidence accepted')
c.rollback()
c.execute("UPDATE order_intents SET state='ORDER_CREATED' WHERE intent_id != ? AND intent_id IN (SELECT intent_id FROM core_intent_links WHERE role='ENTRY')",(INTENT,))
try: validate(c)
except AssertionError: checks.append('competing_entry_refused')
else: raise RuntimeError('competing entry accepted')
c.rollback();c.close()
db=Database(clone)
with db.transaction() as session:
    session.execute(text('BEGIN IMMEDIATE'))
    current=current_runtime_event(session)
    event=RuntimeEvent(commission_id=current.commission_id,sequence=current.sequence+1,
        at=datetime.now(UTC),action='FENCE_ENTRY',evidence_ids=('OFFLINE_CLONE',),reason='OFFLINE_CLONE')
    put(session,'core_runtime_events',event.id,event)
with db.transaction() as session:
    session.execute(text('BEGIN IMMEDIATE'))
    identity=append(session,detail,now=datetime.now(UTC),fence_id=event.id,broker_probe_id='OFFLINE_CLONE')
    repo=StorageRepository(session)
    from quantbot.paper_core.send_guard import definitively_unsent
    assert definitively_unsent(repo,INTENT)
    assert repo.get_order_intent(INTENT).state.value=='ERROR'
    assert repo.get_broker_order_by_client_order_id(repo.get_order_intent(INTENT).client_order_id) is None
    checks.append('append_only_reconstruction_preserves_error_and_client')
db.close()
result=dict(at=datetime.now(UTC).isoformat(),offline_clone_only=True,checks=checks,passed=len(checks))
(root/'audit-validation.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
