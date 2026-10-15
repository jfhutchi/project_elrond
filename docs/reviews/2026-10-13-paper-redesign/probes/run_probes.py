"""Run three synthetic acceptance-gap probes against an explicit source checkout.

No deployment commands, credentials, application settings, or broker network calls.
Exit 1 is expected on the audited PR288: it records named defects, not acceptance.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--main-only', action='store_true',
                        help='Main lacks PR288 transient fixture; run fee/protection probes only.')
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    required = (source / 'pyproject.toml', source / 'src/quantbot/operations/protection.py')
    if not all(p.is_file() for p in required):
        parser.error('source must be the explicit Elrond checkout/archive root')
    scratch = Path(tempfile.mkdtemp(prefix='elrond-audit-probes-')).resolve()
    env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'COMSPEC')
           if key in os.environ}
    env.update(TEMP=str(scratch), TMP=str(scratch), PYTHONDONTWRITEBYTECODE='1',
               TRADING_MODE='PAPER', BROKER_ENVIRONMENT='PAPER',
               LIVE_TRADING_ACKNOWLEDGED='false', KILL_SWITCH='true',
               QUANTBOT_DB_PATH=str(scratch / 'unused.db'),
               QUANTBOT_LOCK_PATH=str(scratch / 'unused.lock'),
               QUANTBOT_REPORTS_DIR=str(scratch / 'reports'))
    bootstrap = '''
import sys,socket
original_pair, original_connect = socket.socketpair, socket.socket.connect
def refuse(*a,**kw): raise RuntimeError('NETWORK DISABLED BY OFFLINE AUDIT')
def self_pipe(*a,**kw):
    socket.socket.connect=original_connect
    try: return original_pair(*a,**kw)
    finally: socket.socket.connect=refuse
socket.socketpair=self_pipe
socket.socket.connect=refuse
socket.socket.connect_ex=refuse
socket.create_connection=refuse
sys.path.insert(0,sys.argv.pop(1))
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
'''
    command = [sys.executable, '-I', '-B', '-c', bootstrap, str(source), '-q',
               '-p', 'no:cacheprovider', '-c', str(source / 'pyproject.toml'),
               '--basetemp', str(scratch / 'pytest'),
               str(Path(__file__).with_name('test_acceptance_gaps.py'))]
    if args.main_only:
        command += ['-k', 'not unstable_quote']
    result = subprocess.run(command, cwd=source, env=env, text=True, capture_output=True)
    log = scratch / 'result.txt'
    log.write_text(result.stdout + result.stderr, encoding='utf-8')
    print(result.stdout)
    print(result.stderr)
    print(f'Synthetic probe log: {log}')
    print('These probes do not establish broker-PAPER behavior or complete the redesign.')
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
