"""Reviewed one-file same-config repair while the approved entry amendment stays fenced."""
import argparse,hashlib,json,runpy,shutil,sqlite3,subprocess
from datetime import UTC,datetime
from pathlib import Path
from quantbot.paper_core.authority import commission_in_session,executable_in_session,current_runtime_event,transition_release
from quantbot.paper_core.activation import active_in_session
from quantbot.paper_core.release import PreparedRelease,prepare_release,verify_release

h=runpy.run_path('D:/Elrond/deploy/whole-share-20261015/handover.py')
ROOT,EVIDENCE,MANIFEST=h['ROOT'],h['EVIDENCE'],h['MANIFEST']
OLD='172810ad8e4170e19e66235be3980b78cf9d56956d5ec4677127b5d2586256de'
FILE='src/quantbot/paper_core/trader.py'
def stage():
    old=PreparedRelease.model_validate_json(MANIFEST.read_text())
    assert old.id==OLD and not verify_release(old)['objections']
    blob=(h['SOURCE']/FILE).read_bytes();digest=hashlib.sha256(blob).hexdigest()
    pins=dict(old.executable_pins);prior=pins[FILE];pins[FILE]=digest
    new=old.model_copy(update={'executable_pins':tuple(sorted(pins.items()))})
    assert new.config==old.config and new.id!=old.id
    (ROOT/'corrected-trader.py').write_bytes(blob)
    (ROOT/'corrected-manifest.json').write_text(new.model_dump_json(indent=2))
    h['dump'](EVIDENCE/'corrected-patch-plan.json',dict(old_release_id=old.id,new_release_id=new.id,
        source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=h['SOURCE'],text=True).strip(),
        config_unchanged=True,entry_policy_unchanged=True,path=FILE,old_sha256=prior,new_sha256=digest))
    print(json.dumps(dict(staged_corrected=new.id,config_unchanged=True)))
def install():
    old=PreparedRelease.model_validate_json(MANIFEST.read_text())
    new=PreparedRelease.model_validate_json((ROOT/'corrected-manifest.json').read_text())
    assert old.id==OLD and not verify_release(old)['objections'] and new.config==old.config
    stopped=json.loads((EVIDENCE/'corrected-role-fence.json').read_text(encoding='utf-8-sig'))
    assert stopped['remaining_core_processes']==[] and 0<=(datetime.now(UTC)-datetime.fromisoformat(stopped['at'])).total_seconds()<=120
    source=sqlite3.connect(old.config.store_path);backup=sqlite3.connect(EVIDENCE/'pre-correction-backup.db')
    try:
        source.backup(backup)
        assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally:backup.close();source.close()
    dest=Path(old.config.repository)/FILE
    shutil.copyfile(dest,ROOT/'pre-correction-trader.py')
    shutil.copyfile(ROOT/'corrected-trader.py',dest)
    assert prepare_release(new.config)==new and not verify_release(new)['objections']
    db,store=h['context'](old)
    try:
        with db.transaction() as session:
            commission=commission_in_session(store,session);binding=executable_in_session(session)
            event=current_runtime_event(session);active=active_in_session(store,session)[1]
        approval=dict(commission_id=commission.id,expected_binding_id=binding.id,
            old_release_id=old.id,new_release_id=new.id,expected_fence_id=event.id,
            expected_activation_id=active.id,approved_at=datetime.now(UTC).isoformat(),
            approval_reference=h['REFERENCE'],old_writer_fence_evidence_ids=(
                'corrected-role-fence.json',h['digest'](EVIDENCE/'corrected-role-fence.json')))
        h['dump'](EVIDENCE/'private-corrected-approval.json',approval)
        identity=transition_release(store,new,approval)
        shutil.copyfile(ROOT/'corrected-manifest.json',MANIFEST)
        h['dump'](EVIDENCE/'corrected-binding.json',dict(at=datetime.now(UTC).isoformat(),
            old_release_id=old.id,new_release_id=new.id,binding_id=identity,
            config_unchanged=True,backup_integrity='ok',verification=verify_release(new)))
        print(json.dumps(dict(corrected_binding=identity,release_id=new.id,backup_integrity='ok')))
    finally:db.close()
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('stage','install'))
    globals()[p.parse_args().action]()
