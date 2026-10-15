"""Bounded, explicit same-config maintenance for the locally authorized readiness repair."""
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
from quantbot.paper_core.authority import (
    commission_in_session, current_runtime_event, executable_in_session,
    fence_entries, resume_entries, transition_release,
)
from quantbot.paper_core.release import PreparedRelease, prepare_release, read_status, verify_release
from quantbot.paper_core.store import CoreStore, table
from quantbot.storage import Database

SOURCE = Path('C:/Users/leepa/.codex/worktrees/paper-core-m0/project_elrond')
ROOT = Path('D:/Elrond/deploy/submission-20261015')
EVIDENCE = Path('D:/Elrond/review/submission-20261015')
CONFIG = Path('D:/Elrond/config/paper-core-14352cc-20261015/runtime-config.json')
MANIFEST = CONFIG.with_name('runtime-manifest.json')
OLD = '0734974b39088b449abe1279a385c89202756b61f3420aa910d6b2d772a46d4c'
REFERENCE = 'USER_FIX_SUBMISSION_READINESS_20261015'
FILES = ['src/quantbot/execution/coordinator.py'] + [
    'src/quantbot/paper_core/' + name + '.py'
    for name in ('alpaca_adapter','funds','guardian','outcomes','runtime','send_guard')
]


def dump(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding='utf-8')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stage():
    old = PreparedRelease.model_validate_json(MANIFEST.read_text())
    assert old.id == OLD and not verify_release(old)['objections']
    pins = dict(old.executable_pins)
    changes = []
    for name in FILES:
        dest = ROOT / 'patch' / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / name, dest)
        changes.append(dict(path=name, old_sha256=pins[name], new_sha256=digest(dest)))
        pins[name] = digest(dest)
    successor = old.model_copy(update={'executable_pins': tuple(sorted(pins.items()))})
    assert successor.config == old.config and successor.id != old.id
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT/'successor-manifest.json').write_text(successor.model_dump_json(indent=2))
    shutil.copyfile(MANIFEST, ROOT/'predecessor-manifest.json')
    dump(EVIDENCE/'patch-plan.json', dict(old_release_id=old.id, new_release_id=successor.id,
        config_unchanged=True, dependencies_unchanged=True, changes=changes))
    print(json.dumps({'staged_release_id': successor.id, 'changed_files':len(changes)}))


def context(release):
    status = read_status(Path(release.config.store_path))
    db = Database(Path(release.config.store_path))
    store = CoreStore(db, commissioned_core(release,status), runtime_release_id=release.id)
    return db, store


def fence():
    old = PreparedRelease.model_validate_json(MANIFEST.read_text())
    assert old.id == OLD and not verify_release(old)['objections']
    db, store = context(old)
    try:
        stop = read_status(Path(old.config.store_path))['recorded_stop']
        with db.transaction() as session:
            commission = commission_in_session(store, session)
            activation = active_in_session(store, session)[1]
            binding = executable_in_session(session)
        identity = fence_entries(store, reason=REFERENCE,
            evidence_ids=(OLD, (ROOT/'successor-manifest.json').name))
        dump(EVIDENCE/'maintenance-fence.json',dict(at=datetime.now(UTC).isoformat(),
            fence_id=identity,commission_id=commission.id, activation_id=activation.id,
            binding_id=binding.id, prior_stop=stop))
        print(json.dumps({'fence_id':identity}))
    finally:
        db.close()


def install():
    old = PreparedRelease.model_validate_json(MANIFEST.read_text())
    new = PreparedRelease.model_validate_json((ROOT/'successor-manifest.json').read_text())
    assert old.id == OLD and new.config == old.config and not verify_release(old)['objections']
    fence = json.loads((EVIDENCE/'maintenance-fence.json').read_text())
    stopped = json.loads((EVIDENCE/'old-role-fence.json').read_text())
    assert stopped['remaining_core_processes'] == []
    assert 0 <= (datetime.now(UTC)-datetime.fromisoformat(stopped['at'])).total_seconds() <= 120
    db, store = context(old)
    try:
        with db.transaction() as session:
            assert current_runtime_event(session).id == fence['fence_id']
    finally: db.close()
    # SQLite backup API includes committed WAL state. Never copy an active .db file.
    source = sqlite3.connect(old.config.store_path)
    backup = sqlite3.connect(EVIDENCE/'prepatch-backup.db')
    try:
        source.backup(backup)
        assert backup.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    finally: backup.close(); source.close()
    for name in FILES:
        dest = Path(old.config.repository)/name
        saved = ROOT/'previous'/name
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(dest,saved)
        assert digest(ROOT/'patch'/name) == dict(new.executable_pins)[name]
        shutil.copyfile(ROOT/'patch'/name,dest)
    actual = prepare_release(new.config)
    assert actual == new and not verify_release(actual)['objections']
    # Manifest promotion precedes the explicit binding CAS; roles remain stopped.
    shutil.copyfile(ROOT/'successor-manifest.json',MANIFEST)
    dump(EVIDENCE/'installed-verification.json',verify_release(new))
    print(json.dumps({'installed_release_id':new.id,'backup_integrity':'ok'}))


def transition():
    new = PreparedRelease.model_validate_json(MANIFEST.read_text())
    old = PreparedRelease.model_validate_json((ROOT/'predecessor-manifest.json').read_text())
    assert not verify_release(new)['objections']
    status=read_status(Path(old.config.store_path))
    db=Database(Path(old.config.store_path))
    store=CoreStore(db,commissioned_core(old,status),runtime_release_id=old.id)
    try:
        with db.transaction() as session:
            commission=commission_in_session(store,session)
            binding=executable_in_session(session)
            current=current_runtime_event(session)
            active=active_in_session(store,session)[1]
        document=dict(commission=commission.model_dump(mode='json'),commission_id=commission.id,
            expected_binding_id=binding.id,old_release_id=old.id,new_release_id=new.id,
            expected_fence_id=current.id,expected_activation_id=active.id,
            approved_at=datetime.now(UTC).isoformat(),approval_reference=REFERENCE,
            old_writer_fence_evidence_ids=('old-role-fence.json',))
        dump(EVIDENCE/'private-transition-approval.json',document)
        identity=transition_release(store,new,document)
        dump(EVIDENCE/'binding-transition.json',dict(at=datetime.now(UTC).isoformat(),
            binding_id=identity,old_release_id=old.id,new_release_id=new.id,fence_id=current.id))
        print(json.dumps({'binding_id':identity}))
    finally: db.close()


def resume():
    new=PreparedRelease.model_validate_json(MANIFEST.read_text())
    assert not verify_release(new)['objections']
    db,store=context(new)
    try:
        from quantbot.paper_core.work import current_receipts
        with db.transaction() as session:
            commission=commission_in_session(store,session)
            current=current_runtime_event(session)
            active=active_in_session(store,session)[1]
            receipt=current_receipts(session)[0]
            payload=session.execute(select(table('core_role_heartbeats').c.payload).join(
                table('core_role_current'), table('core_role_current').c.heartbeat_id == table('core_role_heartbeats').c.id
            ).where(table('core_role_current').c.role=='guardian')).scalar_one()
        from quantbot.paper_core.authority import RoleHeartbeat
        h=RoleHeartbeat.model_validate_json(payload)
        with db.transaction() as session:
            candidates=list(session.execute(select(table('core_time_evidence').c.id).where(
                table('core_time_evidence').c.id.in_(h.evidence_ids)
            )).scalars())
        assert candidates
        document=dict(commission=commission.model_dump(mode='json'),commission_id=commission.id,
            runtime_release_id=new.id,expected_fence_id=current.id,expected_activation_id=active.id,
            proof_id=store.latest_proof().id,guardian_receipt_id=receipt.id,
            guardian_heartbeat_id=h.id,time_evidence_id=candidates[-1],
            approved_at=datetime.now(UTC).isoformat(),approval_reference=REFERENCE)
        dump(EVIDENCE/'private-resume-approval.json',document)
        identity=resume_entries(store,document)
        dump(EVIDENCE/'entry-resume.json',dict(at=datetime.now(UTC).isoformat(),event_id=identity,
            release_id=new.id, fence_id=current.id,guardian_heartbeat_id=h.id))
        print(json.dumps({'resume_event_id':identity}))
    finally: db.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('stage','fence','install','transition','resume'))
    globals()[p.parse_args().action]()
