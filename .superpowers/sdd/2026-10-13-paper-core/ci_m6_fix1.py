"""Exact CI tools in a credential-stripped environment; no pytest duplication."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[3]
scratch = Path(tempfile.mkdtemp(prefix='paper-core-m6-ci-'))
env = {k: os.environ[k] for k in ('SystemRoot','WINDIR','COMSPEC','PROCESSOR_ARCHITECTURE',
    'PROCESSOR_ARCHITEW6432','NUMBER_OF_PROCESSORS','PATHEXT') if k in os.environ}
env['PATH'] = str(Path(sys.executable).parent)+';C:\\Users\\leepa\\.local\\bin;C:\\Program Files\\Git\\cmd;C:\\Windows\\System32'
env.update(TEMP=str(scratch), TMP=str(scratch), USERPROFILE=str(scratch),
    LOCALAPPDATA=str(scratch/'Local'), APPDATA=str(scratch/'Roaming'), UV_OFFLINE='1',
    UV_PYTHON_DOWNLOADS='never', UV_NO_CONFIG='1', TRADING_MODE='PAPER', BROKER_ENVIRONMENT='PAPER',
    LIVE_TRADING_ACKNOWLEDGED='false', KILL_SWITCH='true', QUANTBOT_DB_PATH=str(scratch/'unused.db'))
helpers=['scripts/ci_checks.py','scripts/local_ci.py','scripts/ci_process.py']
commands=[('ruff',['uv','run','--frozen','ruff','check','src','tests']),
    ('mypy-linux',['uv','run','--frozen','mypy','--platform','linux','src']),
    ('mypy-win32',['uv','run','--frozen','mypy','--platform','win32','src']),
    ('ruff-helpers',['uv','run','--frozen','ruff','check',*helpers]),
    ('mypy-linux-helpers',['uv','run','--frozen','mypy','--platform','linux',*helpers]),
    ('mypy-win32-helpers',['uv','run','--frozen','mypy','--platform','win32',*helpers])]
out=root/'docs/evidence/paper-core/m6/fix1'
prefix = sys.argv[1] if len(sys.argv) > 1 else ''
out.mkdir(parents=True, exist_ok=True)
results=[]
for name, cmd in commands:
    log=out/(prefix+name+'.txt')
    with log.open('w',encoding='utf-8') as stream:
        result=subprocess.run(cmd,cwd=root,env=env,stdout=stream,stderr=subprocess.STDOUT)
    results.append({'command':cmd,'exit_code':result.returncode,'log':str(log.relative_to(root)),
        'sha256':hashlib.sha256(log.read_bytes()).hexdigest()})
    print(name,result.returncode,log.read_text(encoding='utf-8',errors='replace')[-1000:],flush=True)
(out/(prefix+'ci-results.json')).write_text(json.dumps(results,indent=2),encoding='utf-8')
raise SystemExit(any(r['exit_code'] for r in results))


