from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.windows_shell import permitted_powershell

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows scheduler doubles')

DOUBLES = r'''
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Management
Import-Module Microsoft.PowerShell.Utility
$PSModuleAutoLoadingPreference = 'None'
$global:changes = @()
$global:learner = [pscustomobject]@{
 TaskName='Elrond Learner'; State='Ready'
 Description=('Managed by project_elrond deploy/omen/schedule-learner.ps1. Commit='+('a'*40))
 Principal=[pscustomobject]@{UserId='fixture-user';LogonType='Interactive';RunLevel='Limited'}
 Triggers='every-ten-minutes';Settings='limit-five-minutes'
 Actions=@([pscustomobject]@{Execute=$args[2]
 Arguments=('old action -Commit '+('a'*40)+' -EnvironmentFile "'+$args[2]+'"')
 WorkingDirectory=$args[1]})
}
function Get-ScheduledTask { param($TaskName,$ErrorAction)
 if ($TaskName -eq 'Elrond Learner') { return $global:learner }; return $null }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory)
 [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
function New-ScheduledTaskTrigger {
 param([switch]$Once,$At,$RepetitionInterval,[switch]$AtLogOn,$User)
 [pscustomobject]@{Interval=$RepetitionInterval;AtLogOn=[bool]$AtLogOn;User=$User} }
function New-ScheduledTaskSettingsSet { param($MultipleInstances,$ExecutionTimeLimit,
 [switch]$StartWhenAvailable,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries)
 [pscustomobject]@{MultipleInstances=$MultipleInstances;Limit=$ExecutionTimeLimit} }
function Register-ScheduledTask {
 param($TaskName,$Action,$Trigger,$Settings,$Principal,$Description)
 $global:changes += [pscustomobject]@{Name=$TaskName;Action=$Action;Triggers=$Trigger
 Settings=$Settings;Principal=$Principal} }
function Export-ScheduledTask { param($TaskName)
 '<Task><RegistrationInfo><Description>'+ $global:learner.Description +
 '</Description></RegistrationInfo><Principals><Principal><UserId>'+
 $global:learner.Principal.UserId + '</UserId></Principal></Principals><Triggers><Marker>'+
 $global:learner.Triggers + '</Marker></Triggers><Settings><Limit>'+
 $global:learner.Settings + '</Limit><Enabled>'+ ($global:learner.State -ne 'Disabled') +
 '</Enabled></Settings><Actions><Exec><Arguments>'+ $global:learner.Actions[0].Arguments +
 '</Arguments></Exec></Actions></Task>' }
function Disable-ScheduledTask { param($TaskName)
 $global:changes += 'disable'; $global:learner.State='Disabled' }
function Enable-ScheduledTask { param($TaskName)
 $global:changes += 'enable'; $global:learner.State='Ready' }
function Set-ScheduledTask { param($TaskName,$Action)
 $global:changes += 'set'; $global:learner.Actions=@($Action) }
'''


def invoke(tmp_path, script, body):
    path = ROOT / 'deploy' / 'omen' / script
    assert path.is_file(), f'{script} is missing'
    directory = tmp_path / 'learner'
    directory.mkdir()
    host = tmp_path / 'service-host.exe'
    host.write_bytes(b'fixture: never executed')
    repo = tmp_path / 'repo'
    scripts = repo / 'deploy' / 'omen'
    scripts.mkdir(parents=True)
    (scripts / "start-monitor.ps1").write_text("exit 0")
    (scripts / "start-alert-dispatch.ps1").write_text("exit 0")
    (scripts / "start-learner.ps1").write_text('throw "must not launch learner"')
    (directory / "state").mkdir()
    (directory / "state" / "learner.db").write_bytes(b"untouched fixture")
    (directory / "deployment.json").write_text(json.dumps({"learner_commit": "a" * 40}))
    wrapper = tmp_path / "double.ps1"
    wrapper.write_text(DOUBLES + body, encoding="utf-8")
    result = subprocess.run(
        [
            permitted_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(wrapper),
            str(path),
            str(directory),
            str(host),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "ELROND_TEST_ROOT": str(repo)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_monitor_plan_never_registers(tmp_path):
    result = invoke(tmp_path, 'schedule-monitor.ps1', r'''
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] -ServiceHost $args[2] `
 -Commit ('b'*40) -EnvironmentFile $args[2] -OperationsDatabase $args[2]
@{Changes=$global:changes} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['Changes'] == []


def test_monitor_registration_is_independent_and_bounded(tmp_path):
    result = invoke(tmp_path, 'schedule-monitor.ps1', r'''
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] -ServiceHost $args[2] `
 -Commit ('b'*40) -EnvironmentFile $args[2] -OperationsDatabase $args[2] -Apply
@{Changes=$global:changes} | ConvertTo-Json -Depth 8 -Compress
''')
    task, = result['Changes']
    assert task['Name'] == 'Elrond Observer'
    assert task['Settings']['Limit']['TotalSeconds'] == 60
    assert task['Settings']['MultipleInstances'] == 'IgnoreNew'
    assert task['Triggers'][0]['Interval']['TotalSeconds'] == 120
    assert task['Principal']['LogonType'] == 'Interactive'
    assert task['Principal']['RunLevel'] == 'Limited'


def test_dispatch_registration_reuses_existing_principal_and_is_bounded(tmp_path):
    result = invoke(tmp_path, 'schedule-alert-dispatch.ps1', r'''
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] -ServiceHost $args[2] `
 -Commit ('b'*40) -QueueDatabase $args[2] -OperationsDatabase $args[2] -Apply
@{Changes=$global:changes} | ConvertTo-Json -Depth 8 -Compress
''')
    task, = result['Changes']
    assert task['Name'] == 'Elrond Alert Dispatcher'
    assert task['Settings']['Limit']['TotalSeconds'] == 60
    assert task['Settings']['MultipleInstances'] == 'IgnoreNew'
    assert task['Principal']['LogonType'] == 'Interactive'
    assert task['Principal']['RunLevel'] == 'Limited'


@pytest.mark.parametrize('state', ['Running', 'unmanaged', 'privileged'])
def test_learner_update_refuses_unsafe_task_without_changes(tmp_path, state):
    setup = {'Running': "$global:learner.State='Running'",
             'unmanaged': "$global:learner.Description='not ours'",
             'privileged': "$global:learner.Principal.RunLevel='Highest'"}[state]
    result = invoke(tmp_path, 'update-learner-task.ps1', setup + r'''
$refused = $false
try { & $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply } catch { $refused=$true }
@{Refused=$refused;Changes=$global:changes} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['Refused'] and result['Changes'] == []


def test_learner_plan_preserves_task_and_store(tmp_path):
    result = invoke(tmp_path, 'update-learner-task.ps1', r'''
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup')
@{Changes=$global:changes;BackupExists=(Test-Path ($args[1]+'\backup'))} | ConvertTo-Json -Compress
''')
    assert result == {'Changes': [], 'BackupExists': False}


@pytest.mark.parametrize('enabled', [True, False])
def test_apply_then_rollback_restores_only_action_and_metadata(tmp_path, enabled):
    setup = '' if enabled else "$global:learner.State='Disabled'\n"
    result = invoke(tmp_path, 'update-learner-task.ps1', setup + r'''
$old = $global:learner.Actions[0].Arguments
$before = Get-Content ($args[1]+'\state\learner.db') -Raw
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply
$pin = (Get-Content ($args[1]+'\deployment.json') -Raw | ConvertFrom-Json).learner_commit
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply -Rollback
$saved = Get-Content ($args[1]+'\backup\rollback.json') -Raw | ConvertFrom-Json
$finalPin=(Get-Content ($args[1]+'\deployment.json') -Raw | ConvertFrom-Json).learner_commit
@{Changes=$global:changes;AppliedPin=$pin;FinalPin=$finalPin
 RestoredAction=($global:learner.Actions[0].Arguments -eq $old);State=$global:learner.State
 DatabaseUnchanged=($before -eq (Get-Content ($args[1]+'\state\learner.db') -Raw))
 Audit=$saved.Status;Principal=$global:learner.Principal} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['AppliedPin'] == 'b' * 40 and result['FinalPin'] == 'a' * 40
    assert result['RestoredAction'] and result['DatabaseUnchanged']
    assert result['State'] == ('Ready' if enabled else 'Disabled')
    assert result['Audit'] == 'ROLLED_BACK'
    assert result['Principal']['UserId'] == 'fixture-user'
    assert result['Changes'] == (['disable', 'set', 'enable'] * 2 if enabled
                                  else ['disable', 'set'] * 2)


def test_failed_update_restores_original_without_starting_worker(tmp_path):
    result = invoke(tmp_path, 'update-learner-task.ps1', r'''
function Set-ScheduledTask { param($TaskName,$Action)
 if ($Action.Arguments -match ('b'*40)) { throw 'simulated registration failure' }
 $global:changes += 'restore'; $global:learner.Actions=@($Action) }
$refused=$false
try { & $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply } catch { $refused=$true }
@{Refused=$refused;Changes=$global:changes;State=$global:learner.State
 Pin=(Get-Content ($args[1]+'\deployment.json') -Raw | ConvertFrom-Json).learner_commit
 Audit=(Get-Content ($args[1]+'\backup\rollback.json') -Raw | ConvertFrom-Json).Status
} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['Refused'] and result['State'] == 'Ready' and result['Pin'] == 'a' * 40
    assert result['Audit'] == 'FAILED_RESTORED'
    assert result['Changes'] == ['disable', 'restore', 'enable']


def test_tick_race_is_refused_without_rewriting_running_task(tmp_path):
    result = invoke(tmp_path, 'update-learner-task.ps1', r'''
function Disable-ScheduledTask { param($TaskName)
 $global:changes += 'disable'; $global:learner.State='Running' }
$refused=$false
try { & $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply } catch { $refused=$true }
@{Refused=$refused;Changes=$global:changes} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['Refused']
    assert result['Changes'] == ['disable', 'enable']


@pytest.mark.parametrize('running', [False, True])
def test_concurrent_action_drift_is_not_overwritten(tmp_path, running):
    result = invoke(tmp_path, 'update-learner-task.ps1', r'''
function Disable-ScheduledTask { param($TaskName)
 $global:changes += 'disable'; $global:learner.State=''' +
                    ("'Running'" if running else "'Disabled'") + r'''
 $global:learner.Actions=@([pscustomobject]@{Execute=$args[2]
 Arguments='operator-changed';WorkingDirectory='different'}) }
$refused=$false
try { & $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply } catch { $refused=$true }
@{Refused=$refused;Changes=$global:changes;Action=$global:learner.Actions[0].Arguments
} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['Refused'] and result['Action'] == 'operator-changed'
    assert result['Changes'] == ['disable']


@pytest.mark.parametrize('field', ['Principal.UserId', 'Triggers', 'Settings'])
@pytest.mark.parametrize('phase', ['apply', 'rollback'])
def test_definition_drift_is_refused_and_left_disabled(tmp_path, field, phase):
    mutation = f"$global:learner.{field}='operator-changed'"
    if phase == 'apply':
        prefix = ("function Disable-ScheduledTask { param($TaskName)\n"
                  "$global:changes += 'disable'; $global:learner.State='Disabled'\n"
                  + mutation + "\n}\n")
    else:
        prefix = r'''
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply
$global:changes=@()
''' + mutation + '\n'
    result = invoke(tmp_path, 'update-learner-task.ps1', prefix + r'''
$refused=$false
try { & $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] `
 -Commit ('b'*40) -ExpectedOldCommit ('a'*40) -EnvironmentFile $args[2] `
 -BackupDirectory ($args[1]+'\backup') -Apply ''' +
                    ('-Rollback' if phase == 'rollback' else '') + r''' } catch {$refused=$true}
@{Refused=$refused;Changes=$global:changes;State=$global:learner.State
} | ConvertTo-Json -Compress
''')
    assert result['Refused'] and result['State'] == 'Disabled'
    assert result['Changes'] == ['disable']


def test_monitor_wrapper_uses_owned_interpreter_and_check_arguments(tmp_path):
    repo = tmp_path / 'wrapper-repo'
    scripts = repo / 'deploy' / 'omen'
    scripts.mkdir(parents=True)
    for filename in ('start-monitor.ps1', 'windows-job.ps1', 'process-job.cs'):
        shutil.copy2(ROOT / 'deploy' / 'omen' / filename, scripts / filename)
    module = repo / 'src' / 'quantbot' / 'operations'
    module.mkdir(parents=True)
    (module.parent / '__init__.py').write_text('')
    (module / '__init__.py').write_text('')
    (module / 'monitor_launch.py').write_text(
        'import json,sys\nprint(json.dumps({"argv":sys.argv[1:],"python":sys.executable}))\n')
    target = Path(sys.prefix).resolve()
    subprocess.run(
        [
            permitted_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"New-Item -ItemType Junction -Path '{repo / '.venv'}' -Target '{target}' | Out-Null",
        ],
        check=True,
        timeout=15,
    )
    environment = tmp_path / "fixture.env"
    environment.write_text("TRADING_MODE=PAPER\n")
    database = tmp_path / "paper.db"
    database.write_bytes(b"not opened by fixture")
    result = subprocess.run(
        [
            permitted_powershell(),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(scripts / "start-monitor.ps1"),
            "-Repository",
            str(repo),
            "-Directory",
            str(tmp_path),
            "-Database",
            str(database),
            "-EnvironmentFile",
            str(environment),
            "-Commit",
            "b" * 40,
            "-Check",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip())
    assert Path(observed['python']).resolve() == (repo / '.venv/Scripts/python.exe').resolve()
    assert observed['argv'] == ['--learner-directory', str(tmp_path),
                                '--operations-database', str(database), '--commit', 'b' * 40,
                                '--env-file', str(environment), '--check']


@pytest.mark.parametrize('mode', ['deadline', 'close', 'initialize'])
def test_deadline_wrapper_launches_exit_only_module_with_exact_store_and_owned_python(
    tmp_path, mode,
):
    repo = tmp_path / 'wrapper-repo'
    scripts = repo / 'deploy/omen'
    scripts.mkdir(parents=True)
    for filename in ('start-learner.ps1', 'windows-job.ps1', 'process-job.cs'):
        shutil.copy2(ROOT / 'deploy/omen' / filename, scripts / filename)
    module = repo / 'src/quantbot' / ('learner' if mode == 'deadline' else 'operations')
    module.mkdir(parents=True)
    (module.parent / '__init__.py').write_text('')
    (module / '__init__.py').write_text('')
    (module / ('execution_worker.py' if mode == 'deadline' else 'close_capture.py')).write_text(
        'import json,sys\nprint(json.dumps({"argv":sys.argv[1:],"python":sys.executable}))\n')
    subprocess.run([permitted_powershell(), '-NoProfile', '-NonInteractive', '-Command',
                    f"New-Item -ItemType Junction -Path '{repo / '.venv'}' "
                    f"-Target '{Path(sys.prefix).resolve()}' | Out-Null"], check=True, timeout=15)
    directory = tmp_path / 'learner'
    (directory / 'state').mkdir(parents=True)
    learner_database = directory / 'state/learner.db'
    learner_database.write_bytes(b'not opened')
    operations = tmp_path / 'paper.db'
    operations.write_bytes(b'not opened')
    environment = tmp_path / 'fixture.env'
    environment.write_text('TRADING_MODE=PAPER\n')
    result = subprocess.run([
        permitted_powershell(), '-NoProfile', '-NonInteractive', '-File',
        str(scripts / 'start-learner.ps1'), '-Repository', str(repo), '-Directory', str(directory),
        '-Database', str(learner_database), '-EnvironmentFile', str(environment),
        '-Commit', 'b' * 40,
        '-DeadlineSupervisor' if mode == 'deadline' else '-CloseCapture',
        '-OperationsDatabase', str(operations),
        *(['-Check'] if mode == 'deadline' else ['-InitializeCapture'] if mode == 'initialize'
          else []),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip())
    assert Path(observed['python']).resolve() == (repo / '.venv/Scripts/python.exe').resolve()
    expected = ['--commit', 'b' * 40, '--database', str(operations)]
    expected += ['--check'] if mode == 'deadline' else [
        '--directory', str(directory / 'performance')]
    if mode == 'initialize':
        expected += ['--initialize']
    assert observed['argv'] == expected + ['--env-file', str(environment)]


def test_dispatch_wrapper_checks_both_readonly_sources_with_owned_python(tmp_path):
    repo = tmp_path / 'wrapper-repo'
    scripts = repo / 'deploy/omen'
    scripts.mkdir(parents=True)
    for filename in ('start-alert-dispatch.ps1', 'windows-job.ps1', 'process-job.cs'):
        shutil.copy2(ROOT / 'deploy/omen' / filename, scripts / filename)
    module = repo / 'src/quantbot/operations'
    module.mkdir(parents=True)
    (module.parent / '__init__.py').write_text('')
    (module / '__init__.py').write_text('')
    (module / 'alert_dispatch.py').write_text(
        'import json,sys\nprint(json.dumps({"argv":sys.argv[1:],"python":sys.executable}))\n')
    subprocess.run([permitted_powershell(), '-NoProfile', '-NonInteractive', '-Command',
                    f"New-Item -ItemType Junction -Path '{repo / '.venv'}' "
                    f"-Target '{Path(sys.prefix).resolve()}' | Out-Null"], check=True, timeout=15)
    database = tmp_path / 'alerts.db'
    database.write_bytes(b'fixture: never opened')
    observer = tmp_path / 'observer.db'
    observer.write_bytes(b'fixture: never opened')
    queues = (tmp_path / 'queue.db', tmp_path / 'observer-queue.db')
    result = subprocess.run([
        permitted_powershell(), '-NoProfile', '-NonInteractive', '-File',
        str(scripts / 'start-alert-dispatch.ps1'), '-Repository', str(repo),
        '-Directory', str(tmp_path), '-Database', str(database), '-QueueDatabase', str(queues[0]),
        '-ObserverDatabase', str(observer), '-ObserverQueueDatabase', str(queues[1]),
        '-Commit', 'b' * 40, '-Check',
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    observed = [json.loads(line) for line in result.stdout.strip().splitlines()]
    assert len(observed) == 2
    for item, queue in zip(observed, queues, strict=True):
        assert Path(item['python']).resolve() == (repo / '.venv/Scripts/python.exe').resolve()
        assert item['argv'] == ['--queue', str(queue), '--commit', 'b' * 40, '--check']
    assert all(not queue.exists() for queue in queues)


def test_dispatch_legacy_principal_needs_exact_description(tmp_path):
    result = invoke(tmp_path, 'schedule-alert-dispatch.ps1', r'''
$global:learner.Description='approved legacy description'
$refused=$false
try { & $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] -ServiceHost $args[2] `
 -Commit ('b'*40) -QueueDatabase $args[2] -OperationsDatabase $args[2] `
 -ExpectedLearnerDescription 'wrong description' -Apply } catch {$refused=$true}
& $args[0] -Repository $env:ELROND_TEST_ROOT -Directory $args[1] -ServiceHost $args[2] `
 -Commit ('b'*40) -QueueDatabase $args[2] -OperationsDatabase $args[2] `
 -ExpectedLearnerDescription 'approved legacy description' -Apply
@{Refused=$refused;Changes=$global:changes} | ConvertTo-Json -Depth 8 -Compress
''')
    assert result['Refused']
    assert len(result['Changes']) == 1
    assert result['Changes'][0]['Name'] == 'Elrond Alert Dispatcher'
