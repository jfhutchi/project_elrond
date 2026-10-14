import json,hashlib,shutil
from pathlib import Path
root=Path.cwd(); src=root/'.superpowers/sdd/2026-10-13-paper-core'; dest=root/'docs/evidence/paper-core/m6/preparation'; dest.mkdir(parents=True,exist_ok=True)
names=['m6-backup-preparation.json','m6-host-metadata-preparation.json','m6-inventory-preparation.json','m6-task-principals-preparation.json','m6-restored-ledger-metadata.json','m6-restored-protection-metadata.json','m6-restored-ledger-metadata.json','m6-remote-heads-preparation.json','m6-task-action-index.json','m6-registry-metadata.json','m6-v34-root-verification.json']
records=[]
for name in dict.fromkeys(names):
 p=src/name
 if p.exists():
  shutil.copyfile(p,dest/name); records.append({'source':str(p),'archive':str((dest/name).relative_to(root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
for folder in ['m6-task-archive','m6-config-archive']:
 for p in (src/folder).rglob('*'):
  if p.is_file():
   q=dest/folder/p.relative_to(src/folder); q.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(p,q); records.append({'source':str(p),'archive':str(q.relative_to(root)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
(dest/'provenance.json').write_text(json.dumps({'scope':'DATED_READ_ONLY_PREPARATION_NOT_FRESH_BROKER_AUTHORITY','records':records},indent=2),encoding='utf-8')
inv=json.loads((src/'m6-inventory-preparation.json').read_text(encoding='utf-8-sig'))
for s in inv['stores']:print(s.get('role'),s.get('store_identity'),s.get('schema_version'),s.keys())
print('packaged',len(records),'nonsecret preparation archives')
