from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def launcher():
    try:
        return importlib.import_module('quantbot.operations.monitor_launch')
    except ModuleNotFoundError:
        pytest.fail('bounded monitor launcher is missing')


@pytest.fixture
def inputs(tmp_path):
    learner = tmp_path / 'learner'
    learner.mkdir()
    database = tmp_path / 'paper.db'
    database.write_bytes(b'fixture: never opened by check')
    env = tmp_path / 'paper.env'
    env.write_text('TRADING_MODE=PAPER\nBROKER_ENVIRONMENT=PAPER\n'
                   'LIVE_TRADING_ACKNOWLEDGED=false\n')
    return ['--learner-directory', str(learner), '--operations-database', str(database),
            '--env-file', str(env), '--commit', 'a' * 40]


def test_check_never_reads_broker_or_writes_monitor(inputs, monkeypatch, capsys):
    module = launcher()
    monkeypatch.setattr(module, '_git', lambda *a: 'a' * 40 if a[-1] == 'HEAD' else '')
    monkeypatch.setattr(module, 'run', lambda *a: pytest.fail('check must not run monitor'))
    assert module.main(inputs + ['--check']) == 0
    assert not (Path(inputs[1]) / 'monitor').exists()
    assert capsys.readouterr().out.strip() == 'MONITOR_LAUNCH_CHECK_OK'


@pytest.mark.parametrize('fault', ['pin', 'dirty', 'live', 'missing', 'relative'])
def test_refuses_invalid_launch_without_monitor_execution(inputs, monkeypatch, capsys, fault):
    module = launcher()
    monkeypatch.setattr(module, '_git', lambda *a: ('b' * 40 if fault == 'pin' else 'a' * 40)
                        if a[-1] == 'HEAD' else ('M unsafe' if fault == 'dirty' else ''))
    monkeypatch.setattr(module, 'run', lambda *a: pytest.fail('refused launch executed'))
    if fault == 'live':
        Path(inputs[5]).write_text('TRADING_MODE=LIVE\nLIVE_TRADING_ACKNOWLEDGED=true\n')
    if fault == 'missing':
        Path(inputs[3]).unlink()
    if fault == 'relative':
        inputs[1] = 'relative'
    assert module.main(inputs + ['--check']) == 2
    assert capsys.readouterr().out.strip() == 'MONITOR_LAUNCH_REFUSED'


def test_launch_passes_only_fixed_paths_to_monitor(inputs, monkeypatch):
    module = launcher()
    monkeypatch.setattr(module, '_git', lambda *a: 'a' * 40 if a[-1] == 'HEAD' else '')
    observed = []
    monkeypatch.setattr(module, 'run', lambda args: observed.append(args) or 1)
    assert module.main(inputs) == 1
    assert observed == [['--learner-directory', inputs[1], '--operations-database', inputs[3],
                         '--output-directory', str(Path(inputs[1]) / 'monitor'),
                         '--probe-paper-market']]
