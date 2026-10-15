"""Query-only nonsecret current-bound role, fill, outcome and protection evidence."""
import json,sqlite3,sys
from datetime import UTC,datetime
from pathlib import Path
from quantbot.paper_core.authority import ExecutableBinding,RoleHeartbeat
from quantbot.paper_core.release import PreparedRelease

root=Path('D:/Elrond/review/whole-share-20261015')
c=sqlite3.connect('file:D:/Elrond/state/quantbot-paper-fresh-20260908.db?mode=ro',uri=True)
c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
now=datetime.now(UTC)
binding=ExecutableBinding.model_validate_json(c.execute('SELECT h.payload FROM core_runtime_current p JOIN core_runtime_config_history h ON h.id=p.document_id').fetchone()[0])
release=binding.release
manifests=[]
for p in Path('D:/Elrond/config').glob('*/runtime-manifest.json'):
    try:
        if PreparedRelease.model_validate_json(p.read_text()).id==release.id: manifests.append(str(p))
    except (ValueError,OSError): pass
assert len(manifests)==1
roles=[RoleHeartbeat.model_validate_json(r[0]) for r in c.execute('SELECT h.payload FROM core_role_current p JOIN core_role_heartbeats h ON h.id=p.heartbeat_id')]
entries=[dict(r) for r in c.execute('SELECT i.intent_id,i.symbol,i.side,i.quantity,i.state,i.created_at FROM order_intents i JOIN core_intent_links l ON l.intent_id=i.intent_id ORDER BY i.created_at DESC LIMIT 10')]
fills=[dict(r) for r in c.execute('SELECT f.fill_id,f.broker_order_id,f.symbol,f.side,f.quantity,f.price,f.occurred_at FROM fills f JOIN broker_orders b ON b.broker_order_id=f.broker_order_id JOIN order_intents i ON i.client_order_id=b.client_order_id JOIN core_intent_links l ON l.intent_id=i.intent_id ORDER BY f.occurred_at')]
outcomes=[json.loads(r[0]) for r in c.execute('SELECT payload FROM core_round_trips')]
counts={n:c.execute('SELECT count(*) FROM '+n).fetchone()[0] for n in ('core_learning_runs','core_learning_attempts','core_learning_inputs')}
protection=dict(c.execute('SELECT observed_at,outcome,position_count,evaluated_count,unprotected_count,exit_required_count,reason FROM position_protection_cycles ORDER BY observed_at DESC LIMIT 1').fetchone())
active=[dict(r) for r in c.execute('SELECT * FROM core_active_model')]
stop=dict(c.execute('SELECT * FROM kill_switch_state').fetchone())
event=max([json.loads(r[0]) for r in c.execute('SELECT payload FROM core_runtime_events')],key=lambda d:d['sequence'])
proof=json.loads(c.execute('SELECT payload FROM core_reconciliation_proofs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
c.close()
result=dict(at=now.isoformat(),binding_id=binding.id,release_id=release.id,
    manifest=manifests[0],repository=release.config.repository,interpreter=release.interpreter,
    entry_policy_id=release.config.entry_policy.id if release.config.entry_policy else None,
    symbols=release.config.entry_symbols,runtime_action=event['action'],
    roles={h.role:dict(h.model_dump(mode='json'),current_release=h.release_id==release.id,
        fresh=h.recorded_at<=now<h.valid_until) for h in roles},
    entries=entries,core_fills=fills,core_round_trips=outcomes,learning_counts=counts,
    active_model=active,stop=stop,retained_protection=protection,
    quantity_proof={k:proof.get(k) for k in ('generation','recorded_at_utc','complete','cash_complete','funds_authority')})
tag=sys.argv[1] if len(sys.argv)>1 else now.strftime('%H%M%S')
(root/('health-'+tag+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
print(json.dumps(dict(at=result['at'],release_id=release.id,runtime_action=result['runtime_action'],
    roles={k:dict(pid=h['pid'],current=h['current_release'],fresh=h['fresh'],objections=h['objections']) for k,h in result['roles'].items()},
    fills=fills,outcomes=[{k:d.get(k) for k in ('symbol','open_quantity','status','gross_pnl','actual_fees','net_pnl','eligibility_reasons')} for d in outcomes],
    learning_counts=counts,retained_protection=protection)))
