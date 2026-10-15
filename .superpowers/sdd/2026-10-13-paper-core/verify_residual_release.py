"""Owner read-only release and static task verification; no operating store or provider."""
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path('src').resolve()))
from quantbot.paper_core.release import PreparedRelease, RuntimeConfig, verify_release

bundle = Path(sys.argv[2])
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
release = PreparedRelease.model_validate_json((bundle/'prepared-release.json').read_text())
config = RuntimeConfig.model_validate_json((bundle/'prepared-config.json').read_text())
assert PreparedRelease.model_validate_json(release.model_dump_json()).id == release.id
assert RuntimeConfig.model_validate_json(config.model_dump_json()).id == config.id
verification = verify_release(release)
assert not verification['objections'], verification
pins = json.loads((bundle/'source-pins.json').read_text())['pins']
assert all(sha(Path(path)) == digest for path, digest in pins.items())
ci = json.loads((bundle/'ci-results.json').read_text())
assert len(ci) == 6
assert all(row['exit_code'] == 0 and sha(Path(row['log'])) == row['sha256'] for row in ci)
ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
proposals = json.loads((bundle/'task-proposals.json').read_text())
tasks = []
for record in proposals['tasks']:
    path = bundle/record['path']
    assert sha(path) == record['sha256']
    task = ET.parse(path).getroot()
    assert task.find('t:Settings/t:Enabled', ns).text == 'false'
    assert task.find('t:Settings/t:ExecutionTimeLimit', ns).text == 'PT0S'
    assert task.find('t:Principals/t:Principal/t:RunLevel', ns).text == 'LeastPrivilege'
    assert len(task.find('t:Triggers', ns)) == 0
    arguments = task.find('t:Actions/t:Exec/t:Arguments', ns).text
    assert str(bundle.resolve()) in arguments and 'Bypass' not in arguments
    if record['precommission_verifier']:
        assert '-Verify' in arguments and '-Role' not in arguments
    else:
        assert '-SubmissionDisabled -EntryDisabled' in arguments and '-Verify' not in arguments
    tasks.append({'name': record['name'], 'sha256': record['sha256'], 'mode': 'VERIFY' if record['precommission_verifier'] else 'SUBMISSION_AND_ENTRY_DISABLED'})
result = {'pins': len(pins), 'pin_manifest_sha256': sha(bundle/'source-pins.json'), 'release_id': release.id, 'config_id': config.id, 'independent_verify_release': verification, 'ci_verified': len(ci), 'static_tasks': tasks, 'actual_token_execution': 'NOT_PERFORMED'}
Path(sys.argv[1]).write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
print(json.dumps(result))
