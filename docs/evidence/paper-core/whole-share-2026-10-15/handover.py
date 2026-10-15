"""Explicit approved PAPER policy handover. Private operational helper, never provider POST."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from quantbot.paper_core.__main__ import commissioned_core
from quantbot.paper_core.activation import active_in_session
from quantbot.paper_core.authority import (commission_in_session, current_runtime_event,
    executable_in_session, fence_entries, resume_entries, transition_release, transition_entry_policy)
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.release import PreparedRelease, prepare_release, read_status, verify_release
from quantbot.paper_core.store import CoreStore, table
from quantbot.storage import Database

SOURCE=Path('C:/Users/leepa/.codex/worktrees/paper-core-m0/project_elrond')
ROOT=Path('D:/Elrond/deploy/whole-share-20261015')
EVIDENCE=Path('D:/Elrond/review/whole-share-20261015')
CONFIG=Path('D:/Elrond/config/paper-core-14352cc-20261015/runtime-config.json')
MANIFEST=CONFIG.with_name('runtime-manifest.json')
OLD='aaa40bdbc8f49b8f63ad736740cbffb0fc00f9d14f96e344e0aba4d68c6d78d0'
REFERENCE='USER_APPROVED_AFFORDABLE_STOCKS_ONE_SHARE_EXISTING_LOSS_BUDGET_20261015'
FILES=['src/quantbot/execution/coordinator.py','src/quantbot/operations/protection.py',
    'src/quantbot/storage/repositories.py']+[
    'src/quantbot/paper_core/'+name+'.py' for name in (
    '__main__','alpaca_adapter','authority','contracts','entry_policy','health','outcomes',
    'release','runtime','selection','send_guard','store','trader','protection_bridge')]

def dump(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2),encoding='utf-8')

def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def load(name): return PreparedRelease.model_validate_json((ROOT/name).read_text())
def context(release):
    status=read_status(Path(release.config.store_path))
    db=Database(Path(release.config.store_path))
    return db,CoreStore(db,commissioned_core(release,status),runtime_release_id=release.id)

def preservation(release):
    c=sqlite3.connect(Path(release.config.store_path).as_uri()+'?mode=ro',uri=True)
    c.execute('BEGIN')
    rows={name:[list(r) for r in c.execute('SELECT * FROM '+name+' ORDER BY 1')] for name in (
        'schema_version','core_runtime_commissions','core_active_model','kill_switch_state',
        'core_models','core_activations','core_learning_runs','core_learning_inputs','core_learning_attempts')}
    c.close()
    return rows

def stage():
    old=PreparedRelease.model_validate_json(MANIFEST.read_text())
    assert old.id==OLD and not verify_release(old)['objections']
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    ROOT.mkdir(parents=True,exist_ok=True)
    pins=dict(old.executable_pins);changes=[]
    # Refuse unreviewed source differences outside the explicit patch set.
    retained_support={'src/quantbot/console/state.py'}
    for name,expected in pins.items():
        if name not in FILES and name not in retained_support:
            native=(Path(old.config.repository)/name).read_bytes().replace(b'\r\n',b'\n')
            assert (SOURCE/name).read_bytes().replace(b'\r\n',b'\n')==native, 'unmapped source difference: '+name
    for name in FILES:
        patch=ROOT/'patch'/name;patch.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(SOURCE/name,patch)
        changes.append(dict(path=name,old_sha256=pins.get(name),new_sha256=digest(patch)))
        pins[name]=digest(patch)
    bridge=old.model_copy(update={'executable_pins':tuple(sorted(pins.items()))})
    policy=EntryPolicy(symbols=old.config.core.entry_symbols+('F','T','PFE','VZ','HPQ','SOFI','UBER'))
    config=old.config.model_copy(update={'entry_policy':policy})
    config=type(config).model_validate_json(config.model_dump_json())
    amended=bridge.model_copy(update={'config':config})
    assert bridge.config==old.config and amended.config.core==old.config.core
    for name,release in (('predecessor',old),('bridge',bridge),('amended',amended)):
        (ROOT/(name+'-manifest.json')).write_text(release.model_dump_json(indent=2))
        (ROOT/(name+'-config.json')).write_text(release.config.model_dump_json(indent=2))
    dump(EVIDENCE/'patch-plan.json',dict(old_release_id=old.id,bridge_release_id=bridge.id,
        amended_release_id=amended.id,entry_policy_id=policy.id,entry_policy=policy.model_dump(mode='json'),
        base_config_unchanged=True,dependencies_unchanged=True,changes=changes,
        retained_separate_support_files=sorted(retained_support)))
    print(json.dumps(dict(staged_bridge=bridge.id,staged_amendment=amended.id,files=len(changes))))

def fence():
    old=load('predecessor-manifest.json')
    assert PreparedRelease.model_validate_json(MANIFEST.read_text()).id==old.id
    assert not verify_release(old)['objections']
    db,store=context(old)
    try:
        identity=fence_entries(store,reason=REFERENCE,evidence_ids=(old.id,'patch-plan.json'))
        dump(EVIDENCE/'maintenance-fence.json',dict(at=datetime.now(UTC).isoformat(),fence_id=identity))
        dump(EVIDENCE/'private-preservation-before.json',preservation(old))
        print(json.dumps(dict(fence_id=identity)))
    finally: db.close()

def stopped(kind='bridge'):
    name='amended-role-fence.json' if kind=='amended' else 'old-role-fence.json'
    value=json.loads((EVIDENCE/name).read_text(encoding='utf-8-sig'))
    assert value['remaining_core_processes']==[]
    assert 0<=(datetime.now(UTC)-datetime.fromisoformat(value['at'])).total_seconds()<=120

def install():
    old=load('predecessor-manifest.json');new=load('bridge-manifest.json')
    assert PreparedRelease.model_validate_json(MANIFEST.read_text()).id==old.id
    assert not verify_release(old)['objections'];stopped()
    db,store=context(old)
    try:
        with db.transaction() as session:
            assert current_runtime_event(session).action=='FENCE_ENTRY'
    finally: db.close()
    source=sqlite3.connect(old.config.store_path);backup=sqlite3.connect(EVIDENCE/'prepatch-backup.db')
    try:
        source.backup(backup)
        assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally: backup.close();source.close()
    for name in FILES:
        dest=Path(old.config.repository)/name;saved=ROOT/'previous'/name
        if dest.exists():
            saved.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(dest,saved)
        assert digest(ROOT/'patch'/name)==dict(new.executable_pins)[name]
        shutil.copyfile(ROOT/'patch'/name,dest)
    assert prepare_release(new.config)==new
    result=verify_release(new);assert not result['objections']
    shutil.copyfile(ROOT/'bridge-manifest.json',MANIFEST)
    dump(EVIDENCE/'bridge-installed.json',result)
    print(json.dumps(dict(installed_bridge=new.id,backup_integrity='ok')))

def transition(kind):
    stopped(kind)
    prior=load('predecessor-manifest.json' if kind=='bridge' else 'bridge-manifest.json')
    new=load(kind+'-manifest.json')
    assert not verify_release(new)['objections']
    db,store=context(prior)
    try:
        with db.transaction() as session:
            commission=commission_in_session(store,session);binding=executable_in_session(session)
            current=current_runtime_event(session);active=active_in_session(store,session)[1]
        assert binding.release.id==prior.id and current.action=='FENCE_ENTRY'
        approval=dict(commission=commission.model_dump(mode='json'),commission_id=commission.id,
            expected_binding_id=binding.id,old_release_id=prior.id,new_release_id=new.id,
            expected_fence_id=current.id,expected_activation_id=active.id,
            approved_at=datetime.now(UTC).isoformat(),approval_reference=REFERENCE,
            old_writer_fence_evidence_ids=('old-role-fence.json',))
        if kind=='amended': approval['entry_policy_id']=new.config.entry_policy.id
        if kind=='amended':
            approval['old_writer_fence_evidence_ids']=('amended-role-fence.json',digest(EVIDENCE/'amended-role-fence.json'))
        dump(EVIDENCE/('private-'+kind+'-approval.json'),approval)
        identity=(transition_release if kind=='bridge' else transition_entry_policy)(store,new,approval)
        # Roles remain stopped. Exact bytes verified before binding CAS; promote matching config after.
        shutil.copyfile(ROOT/(kind+'-manifest.json'),MANIFEST)
        shutil.copyfile(ROOT/(kind+'-config.json'),CONFIG)
        dump(EVIDENCE/(kind+'-binding.json'),dict(at=datetime.now(UTC).isoformat(),binding_id=identity,
            old_release_id=prior.id,new_release_id=new.id,fence_id=current.id))
        print(json.dumps(dict(transition=kind,binding_id=identity,release_id=new.id)))
    finally: db.close()

def resume():
    new=PreparedRelease.model_validate_json(MANIFEST.read_text())
    final_name='corrected-manifest.json' if (ROOT/'corrected-manifest.json').exists() else 'amended-manifest.json'
    assert new.id==load(final_name).id and not verify_release(new)['objections']
    from quantbot.paper_core.work import current_receipts
    from quantbot.paper_core.authority import RoleHeartbeat
    db,store=context(new)
    try:
        with db.transaction() as session:
            commission=commission_in_session(store,session);current=current_runtime_event(session)
            active=active_in_session(store,session)[1];receipt=current_receipts(session)[0]
            payload=session.execute(select(table('core_role_heartbeats').c.payload).join(
                table('core_role_current'),table('core_role_current').c.heartbeat_id==table('core_role_heartbeats').c.id
            ).where(table('core_role_current').c.role=='guardian')).scalar_one()
            h=RoleHeartbeat.model_validate_json(payload)
            candidates=list(session.execute(select(table('core_time_evidence').c.id).where(
                table('core_time_evidence').c.id.in_(h.evidence_ids))).scalars())
        assert candidates
        approval=dict(commission=commission.model_dump(mode='json'),commission_id=commission.id,
            runtime_release_id=new.id,expected_fence_id=current.id,expected_activation_id=active.id,
            proof_id=store.latest_proof().id,guardian_receipt_id=receipt.id,guardian_heartbeat_id=h.id,
            time_evidence_id=candidates[-1],approved_at=datetime.now(UTC).isoformat(),approval_reference=REFERENCE)
        dump(EVIDENCE/'private-resume-approval.json',approval)
        identity=resume_entries(store,approval)
        dump(EVIDENCE/'entry-resume.json',dict(at=datetime.now(UTC).isoformat(),event_id=identity,
            release_id=new.id,fence_id=current.id,guardian_heartbeat_id=h.id))
        before=json.loads((EVIDENCE/'private-preservation-before.json').read_text())
        after=preservation(new)
        dump(EVIDENCE/'preservation-check.json',dict(at=datetime.now(UTC).isoformat(),
            tables={name:dict(unchanged=before[name]==rows,count=len(rows)) for name,rows in after.items()}))
        assert before==after
        print(json.dumps(dict(resume_event_id=identity,preserved=True)))
    finally: db.close()

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('stage','fence','install','bridge','amended','resume'))
    action=p.parse_args().action
    if action in ('bridge','amended'): transition(action)
    else: globals()[action]()
