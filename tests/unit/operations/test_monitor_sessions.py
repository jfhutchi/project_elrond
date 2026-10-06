import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.console.market_status import assess_market
from quantbot.domain.models import MarketClock
from quantbot.operations.alerts import AlertStore
from quantbot.operations.monitor import run_monitor
from quantbot.strategy import XNYSSession
from tests.unit.operations.test_monitor import sources, worker_receipt

OPEN = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 10, 1, 20, tzinfo=UTC)


def market(now, *, reported=None, holiday=False):
    opened = not holiday and OPEN <= now < CLOSE
    clock = MarketClock(timestamp=now, is_open=opened if reported is None else reported,
                        next_open=OPEN if now < OPEN else OPEN + timedelta(days=1),
                        next_close=CLOSE if now < CLOSE else CLOSE + timedelta(days=1))
    sessions = () if holiday else (XNYSSession(session_date=OPEN.date(),
                                              open_at=OPEN, close_at=CLOSE),)
    return assess_market(clock, sessions, now=now)


def test_verified_closure_suppresses_only_expected_market_loop_silence(tmp_path):
    now = CLOSE + timedelta(hours=1)
    learner, database = sources(tmp_path, CLOSE - timedelta(minutes=1))
    receipt = run_monitor(learner, database, tmp_path / 'monitor', now=now, market=market(now))
    assert receipt['measurements']['PROTECTION']['status'] == 'EXPECTED_WAIT'
    assert receipt['measurements']['RECONCILIATION']['status'] == 'EXPECTED_WAIT'
    assert {a['code'] for a in receipt['active_alerts']} == {
        'LEARNER_HEARTBEAT_STALE', 'INGESTION_HEARTBEAT_STALE'}
    assert receipt['forecast_wait'] == 'EXPECTED_CLOSED_SESSION'


def test_opening_grace_expires_and_forecast_warmup_has_exact_boundary(tmp_path):
    now = OPEN + timedelta(minutes=19)
    learner, database = sources(tmp_path, now)
    with closing(sqlite3.connect(database)) as c, c:
        for table in ('position_protection_cycles', 'reconciliation_runs'):
            c.execute(f'UPDATE {table} SET completed_at=?',
                      ((OPEN - timedelta(days=1)).isoformat(),))
    output = tmp_path / 'monitor'
    receipt = run_monitor(learner, database, output, now=now, market=market(now))
    assert receipt['measurements']['PROTECTION']['status'] == 'SESSION_STARTUP_GRACE'
    assert receipt['active_alerts'] == []
    assert receipt['forecast_wait'] == 'EXPECTED_FORECAST_WARMUP'
    later = OPEN + timedelta(minutes=21)
    assert 'PROTECTION_HEARTBEAT_STALE' in {a['code'] for a in run_monitor(
        learner, database, output, now=later, market=market(later))['active_alerts']}
    boundary = OPEN + timedelta(minutes=31)
    assert run_monitor(learner, database, output, now=boundary,
                       market=market(boundary))['forecast_wait'] == 'ELIGIBLE_SESSION'


def test_closed_session_pauses_existing_alert_without_false_recovery(tmp_path):
    now = CLOSE - timedelta(minutes=1)
    learner, database = sources(tmp_path, now - timedelta(hours=3))
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=now, market=market(now))
    after = CLOSE + timedelta(minutes=1)
    receipt = run_monitor(learner, database, output, now=after, market=market(after))
    assert 'PROTECTION_HEARTBEAT_STALE' in {a['code'] for a in receipt['active_alerts']}
    assert all(e['event'] != 'RECOVERED' for e in receipt['notifications'])


def test_failure_or_unverified_clock_cannot_be_hidden_by_calendar_wait(tmp_path):
    now = CLOSE + timedelta(hours=1)
    learner, database = sources(tmp_path, now)
    with closing(sqlite3.connect(database)) as c, c:
        c.execute("UPDATE position_protection_cycles SET outcome='INCIDENT'")
    receipt = run_monitor(learner, database, tmp_path / 'monitor',
                          now=now, market=market(now))
    assert 'PROTECTION_HEARTBEAT_STALE' in {a['code'] for a in receipt['active_alerts']}
    stale = now + timedelta(hours=2)
    receipt = run_monitor(learner, database, tmp_path / 'monitor', now=stale, market=market(now))
    assert receipt['measurements']['RECONCILIATION']['status'] == 'STALE'


def test_clock_calendar_disagreement_never_suppresses_stall(tmp_path):
    now = OPEN + timedelta(hours=2)
    learner, database = sources(tmp_path, OPEN - timedelta(days=1))
    receipt = run_monitor(learner, database, tmp_path / 'monitor', now=now,
                          market=market(now, reported=False))
    assert receipt['measurements']['PROTECTION']['status'] == 'STALE'
    assert receipt['forecast_wait'] == 'UNVERIFIED_SESSION'


def test_protected_wait_does_not_change_registry_or_heartbeat_expectation(tmp_path):
    now = OPEN + timedelta(hours=1)
    learner, database = sources(tmp_path, now)
    doc = dict(worker_receipt(now), blockers=['PROTECTED_FEATURE_WINDOW',
                                            'PAPER_PROMOTION_REQUIRED',
                                            'PAPER_EXECUTION_NOT_CONFIGURED'])
    (learner / 'worker.json').write_text(json.dumps(doc))
    before = database.read_bytes()
    receipt = run_monitor(learner, database, tmp_path / 'monitor', now=now, market=market(now))
    assert receipt['forecast_wait'] == 'PROTECTED_FEATURE_WINDOW'
    assert database.read_bytes() == before


def test_cli_probe_failure_keeps_stall_visible(tmp_path, monkeypatch, capsys):
    from quantbot.operations import monitor

    now = datetime.now(UTC)
    learner, database = sources(tmp_path, now - timedelta(hours=5))

    def refused():
        raise ValueError('configuration unavailable')

    monkeypatch.setattr(monitor, 'paper_market_probe', refused)
    assert monitor.main(['--learner-directory', str(learner), '--operations-database',
                         str(database), '--output-directory', str(tmp_path / 'monitor'),
                         '--probe-paper-market']) == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['forecast_wait'] == 'UNVERIFIED_SESSION'
    assert receipt['measurements']['PROTECTION']['status'] == 'STALE'


def test_cli_supplies_verified_market_probe(tmp_path, monkeypatch, capsys):
    from quantbot.operations import monitor

    now = datetime.now(UTC)
    learner, database = sources(tmp_path, now)
    measured = market(CLOSE + timedelta(hours=1))

    async def probe():
        return measured

    observed = []

    def run(*args, **kwargs):
        observed.append(kwargs['market'])
        return {'active_alerts': []}

    monkeypatch.setattr(monitor, 'paper_market_probe', lambda: probe)
    monkeypatch.setattr(monitor, 'run_monitor', run)
    assert monitor.main(['--learner-directory', str(learner), '--operations-database',
                         str(database), '--output-directory', str(tmp_path / 'monitor'),
                         '--probe-paper-market']) == 0
    assert observed == [measured]
    capsys.readouterr()


def skewed_market(now):
    clock = MarketClock(timestamp=now + timedelta(seconds=3.5), is_open=False,
                        next_open=OPEN + timedelta(days=1), next_close=CLOSE + timedelta(days=1))
    return assess_market(clock, (XNYSSession(session_date=OPEN.date(),
                         open_at=OPEN, close_at=CLOSE),), now=now)


def test_closed_clock_fault_deduplicates_without_generic_silence_cascade(tmp_path):
    now = CLOSE + timedelta(hours=1)
    learner, database = sources(tmp_path, CLOSE - timedelta(minutes=1))
    (learner / 'worker.json').write_text(json.dumps(worker_receipt(now)))
    with closing(sqlite3.connect(database)) as c, c:
        c.execute('UPDATE ingestion_runs SET finished_at=?', (now.isoformat(),))
    output = tmp_path / 'monitor'
    first = run_monitor(learner, database, output, now=now, market=skewed_market(now))
    assert {a['code'] for a in first['active_alerts']} == {'CLOCK_SYNC_REQUIRED'}
    assert first['measurements']['PROTECTION']['status'] == 'EXPECTED_WAIT'
    assert first['forecast_wait'] == 'EXPECTED_CLOSED_SESSION'
    later = now + timedelta(seconds=30)
    second = run_monitor(learner, database, output, now=later, market=skewed_market(later))
    assert not second['notifications']
    assert second['active_alerts'][0]['first_seen'] == now.isoformat()
    unknown = run_monitor(learner, database, output, now=later + timedelta(seconds=1))
    assert any(a['code'] == 'CLOCK_SYNC_REQUIRED' for a in unknown['active_alerts'])
    assert not any(e['code'] == 'CLOCK_SYNC_REQUIRED' and e['event'] == 'RECOVERED'
                   for e in unknown['notifications'])
    fixed = later + timedelta(seconds=2)
    recovered = run_monitor(learner, database, output, now=fixed, market=market(fixed))
    assert any(e['code'] == 'CLOCK_SYNC_REQUIRED' and e['event'] == 'RECOVERED'
               for e in recovered['notifications'])


@pytest.mark.parametrize('subsystem,table,column,failure', [
    ('PROTECTION', 'position_protection_cycles', 'outcome', 'INCIDENT'),
    ('RECONCILIATION', 'reconciliation_runs', 'status', 'FAILED'),
])
@pytest.mark.parametrize('missing', [False, True])
def test_closed_clock_fault_keeps_failed_or_never_completed_components_visible(
    tmp_path, subsystem, table, column, failure, missing,
):
    now = CLOSE + timedelta(hours=1)
    learner, database = sources(tmp_path, now)
    with closing(sqlite3.connect(database)) as c, c:
        if missing:
            c.execute(f'DELETE FROM {table}')
        else:
            c.execute(f'UPDATE {table} SET {column}=?', (failure,))
    result = run_monitor(learner, database, tmp_path / 'monitor',
                         now=now, market=skewed_market(now))
    assert {a['code'] for a in result['active_alerts']} == {
        'CLOCK_SYNC_REQUIRED', subsystem + '_HEARTBEAT_STALE'}


def test_closed_clock_fault_retains_existing_stalls_without_false_recovery(tmp_path):
    before = CLOSE - timedelta(minutes=1)
    learner, database = sources(tmp_path, before - timedelta(hours=3))
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=before, market=market(before))
    now = CLOSE + timedelta(minutes=1)
    result = run_monitor(learner, database, output, now=now, market=skewed_market(now))
    assert {'PROTECTION_HEARTBEAT_STALE', 'RECONCILIATION_HEARTBEAT_STALE',
            'CLOCK_SYNC_REQUIRED'} <= {a['code'] for a in result['active_alerts']}
    assert not any(e['event'] == 'RECOVERED' for e in result['notifications'])


def test_clock_acknowledgement_and_reminder_do_not_change_authority_or_recovery(tmp_path):
    now = CLOSE + timedelta(minutes=1)
    learner, database = sources(tmp_path, now)
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=now, market=skewed_market(now))
    with AlertStore(output / 'alerts.db') as store:
        store.acknowledge('CLOCK_SYNC_REQUIRED', now=now)
    later = now + timedelta(hours=1)
    result = run_monitor(learner, database, output, now=later, market=skewed_market(later))
    clock = next(a for a in result['active_alerts'] if a['code'] == 'CLOCK_SYNC_REQUIRED')
    assert clock['acknowledged_at'] == now.isoformat()
    assert clock['resolved_at'] is None
    assert any(e['code'] == 'CLOCK_SYNC_REQUIRED' and e['event'] == 'REMINDER'
               for e in result['notifications'])
    expired = later + timedelta(minutes=2)
    unknown = run_monitor(learner, database, output, now=expired, market=skewed_market(later))
    assert unknown['clock_status'] == 'UNVERIFIED'
    assert unknown['measurements']['PROTECTION']['status'] == 'STALE'
    assert any(a['code'] == 'CLOCK_SYNC_REQUIRED' for a in unknown['active_alerts'])


def test_clock_catalog_is_fixed_and_matches_local_operator_action():
    from pathlib import Path

    from quantbot.operations.monitor import CLOCK_ACTION

    catalog = json.loads((Path(__file__).resolve().parents[3] /
                          'docs/alert-delivery-catalog.json').read_text())
    assert catalog['CLOCK_SYNC_REQUIRED'] == dict(
        subsystem='clock', category='OPERATOR_ACTION_REQUIRED', action=CLOCK_ACTION,
    )
