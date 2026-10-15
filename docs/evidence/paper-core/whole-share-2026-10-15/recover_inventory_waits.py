"""Normal evidence-based recovery of exactly two obsolete guardian inventory waits."""
import json,runpy
from datetime import UTC,datetime
from sqlalchemy import select
from quantbot.paper_core.authority import (RoleHeartbeat,commission_in_session,current_runtime_event,guardian_objections)
from quantbot.paper_core.health import active_health,recover_health
from quantbot.paper_core.funds import entry_inventory_objections
from quantbot.paper_core.release import PreparedRelease
from quantbot.paper_core.store import table
from quantbot.paper_core.time_evidence import TimeEvidence
from quantbot.storage import StorageRepository

h=runpy.run_path('D:/Elrond/deploy/whole-share-20261015/handover.py')
release=PreparedRelease.model_validate_json(h['MANIFEST'].read_text())
db,store=h['context'](release)
try:
    now=datetime.now(UTC);proof=store.latest_proof()
    with db.transaction() as session:
        repo=StorageRepository(session);commission=commission_in_session(store,session)
        fence=current_runtime_event(session);before=active_health(session)
        stop=repo.get_kill_switch_state()
        assert not stop.engaged and fence.action=='FENCE_ENTRY'
        assert len(before)==2 and all(v.role=='GUARDIAN' and v.state=='RETRYABLE_FAILURE' for v in before)
        assert {v.objections for v in before}=={('INVENTORY_ACQUISITION_RETRYABLE',),('OWNED_FILL_ACTIVITY_PENDING',)}
        assert all(proof.generation>v.failed_generation for v in before)
        assert not entry_inventory_objections(store,session,proof,now=now)
        assert not repo.list_unresolved_order_intents()
        assert not guardian_objections(store,session,now=now)
        assert not any(i.kind=='CORE_EVIDENCE_CONFLICT' for i in repo.list_incidents(unresolved_only=True))
        heartbeat=RoleHeartbeat.model_validate_json(session.execute(select(table('core_role_heartbeats').c.payload).join(
            table('core_role_current'),table('core_role_current').c.heartbeat_id==table('core_role_heartbeats').c.id
        ).where(table('core_role_current').c.role=='guardian')).scalar_one())
        times=[TimeEvidence.model_validate_json(p) for p in session.execute(select(table('core_time_evidence').c.payload).where(table('core_time_evidence').c.id.in_(heartbeat.evidence_ids))).scalars()]
        assert times and all(not t.objections and 0<=(now-t.wall_anchor).total_seconds()<=30 and t.samples[-1].provider.account_id==commission.account_id for t in times)
        from quantbot.paper_core.contracts import RoundTrip
        from quantbot.paper_core.send_guard import definitively_unsent
        for link in store.links():
            if link['role']!='ENTRY' or definitively_unsent(repo,link['intent_id']):continue
            payload=session.execute(select(table('core_round_trips').c.payload).where(table('core_round_trips').c.id==link['lot_id'])).scalar_one()
            assert RoundTrip.model_validate_json(payload).open_quantity==0
    recover_health(store,proof,now=now,symbol=store.config.symbol)
    with db.transaction() as session:
        assert active_health(session)==()
        assert StorageRepository(session).get_kill_switch_state()==stop
        ready=[json.loads(p) for p in session.execute(select(table('core_health_events').c.payload)).scalars() if json.loads(p).get('state')=='READY']
        matching=[v for v in ready if set(v.get('recovered_from',[]))=={e.id for e in before}]
        assert len(matching)==1 and matching[0]['new_proof_id']==proof.id
    h['dump'](h['EVIDENCE']/'scoped-recovery.json',dict(at=now.isoformat(),release_id=release.id,
        contract='EXISTING_RECOVER_HEALTH_WITH_NEW_CERTIFIED_INVENTORY',fence_id=fence.id,proof_id=proof.id,
        guardian_heartbeat_id=heartbeat.id,time_evidence_ids=[t.id for t in times],
        recovered_events=[dict(id=v.id,role=v.role,scope=v.scope,state=v.state,objections=v.objections) for v in before],
        remaining_active_health=0,manual_stop_unchanged=True,flat_owned_core=True,fresh_bars_not_asserted=True))
    print(json.dumps(dict(recovered_obsolete_inventory_waits=2,manual_stop_unchanged=True,fresh_bars_not_asserted=True)))
finally:db.close()
