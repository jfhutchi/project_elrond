"""Query-only, nonsecret operational readiness and real economic activity snapshot."""
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

ROOT=Path('D:/Elrond/review/submission-20261015')
c=sqlite3.connect('file:D:/Elrond/state/quantbot-paper-fresh-20260908.db?mode=ro',uri=True)
c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
now=datetime.now(UTC)
roles={h['role']:h for h in (json.loads(r[0]) for r in c.execute(
    'SELECT h.payload FROM core_role_current p JOIN core_role_heartbeats h ON h.id=p.heartbeat_id'))}
binding=json.loads(c.execute('SELECT h.payload FROM core_runtime_current p JOIN core_runtime_config_history h ON h.id=p.document_id').fetchone()[0])
events=[json.loads(r[0]) for r in c.execute('SELECT payload FROM core_runtime_events')]
event=max(events,key=lambda r:r['sequence'])
proof=json.loads(c.execute('SELECT payload FROM core_reconciliation_proofs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
entries=[dict(r) for r in c.execute("SELECT i.intent_id,i.symbol,i.side,i.quantity,i.state,i.created_at FROM order_intents i JOIN core_intent_links l ON l.intent_id=i.intent_id ORDER BY i.created_at DESC LIMIT 12")]
fills=[dict(r) for r in c.execute('SELECT f.fill_id,f.broker_order_id,f.symbol,f.side,f.quantity,f.price,f.occurred_at FROM fills f JOIN broker_orders b ON b.broker_order_id=f.broker_order_id JOIN order_intents i ON i.client_order_id=b.client_order_id JOIN core_intent_links l ON l.intent_id=i.intent_id ORDER BY f.occurred_at')]
tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
counts={name:c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in (
    'core_round_trips','core_learning_runs','core_learning_attempts','core_learning_inputs','core_model_considerations') if name in tables}
stop=dict(c.execute('SELECT * FROM kill_switch_state').fetchone())
active=[dict(r) for r in c.execute('SELECT * FROM core_active_model')]
c.close()
result=dict(at=now.isoformat(),release_id=__import__('quantbot.paper_core.release',fromlist=['PreparedRelease']).PreparedRelease.model_validate(binding['release']).id,
    runtime_action=event['action'],runtime_event_id=__import__('quantbot.research.manifest',fromlist=['content_id']).content_id(event),
    heartbeats=roles,entries=entries,core_fills=fills,counts=counts,stop=stop,active_model=active,
    proof={k:proof.get(k) for k in ('generation','recorded_at_utc','complete','cash_complete','funds_authority')})
tag=now.strftime('%H%M%S')
(ROOT/f'health-{tag}.json').write_text(json.dumps(result,indent=2))
print(json.dumps(dict(at=result['at'],release_id=result['release_id'],runtime_action=result['runtime_action'],
    roles={k:dict(pid=h['pid'],at=h['recorded_at'],progress=h['last_useful_progress'],objections=h['objections']) for k,h in roles.items()},
    latest_entries=entries[:2],core_fills=fills,counts=counts,manual_pause=bool(stop['engaged']))))
