import hashlib,json,subprocess
from pathlib import Path
from datetime import datetime,UTC
root=Path.cwd()
paths=list((root/'src').rglob('*.py'))+list((root/'tests/integration/paper_core').rglob('*.py'))+list((root/'tests/fault/paper_core').rglob('*.py'))
paths += [root/p for p in ['uv.lock','pyproject.toml','deploy/omen/start-paper-core.ps1','deploy/omen/windows-job.ps1','deploy/omen/service-host.cs','deploy/omen/process-job.cs','tests/fixtures/core_m6.py','tests/fixtures/capture_core_v34.py','tests/fixtures/core_v34_m5.json.gz','tests/integration/test_storage_parity.py','tests/unit/test_migration_authority.py','tests/unit/test_storage.py','tests/unit/research/test_registry.py','.superpowers/sdd/2026-10-13-paper-core/check.py','.superpowers/sdd/2026-10-13-paper-core/ci_m6.py']]
paths += [root/'.superpowers/sdd/2026-10-13-paper-core/ci_m6_fix1.py']
paths += [root/'.superpowers/sdd/2026-10-13-paper-core/ci_m6_fix2.py']
doc={'frozen_at':datetime.now(UTC).isoformat(),'accepted_head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'pins':{p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}}
(root/'docs/evidence/paper-core/m6/fix2/source-pins.json').write_text(json.dumps(doc,indent=2),encoding='utf-8')
print('FROZEN',len(doc['pins']),doc['frozen_at'])
