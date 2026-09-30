import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.operations.alerts import AlertStore
from quantbot.operations.monitor import MonitorPolicy, run_monitor

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)


def worker_receipt(at):
    return dict(finished_at=at.isoformat(), started_at=at.isoformat(), status='BLOCKED',
                run_id='d59d7164-688b-4d39-bd3c-471944f30121', code_commit='a' * 40,
                trading_enabled=False, promotion='NONE')


def sources(tmp_path, at=NOW):
    learner = tmp_path / 'learner'
    learner.mkdir()
    (learner / 'worker.json').write_text(json.dumps(worker_receipt(at)))
    (learner / 'deployment.json').write_text(json.dumps({'learner_commit': 'a' * 40}))
    database = tmp_path / 'operations.db'
    with closing(sqlite3.connect(database)) as c, c:
        c.executescript('''
            CREATE TABLE position_protection_cycles(completed_at TEXT, outcome TEXT);
            CREATE TABLE reconciliation_runs(completed_at TEXT, status TEXT);
            CREATE TABLE ingestion_runs(finished_at TEXT, status TEXT, mode TEXT);
        ''')
        c.execute('INSERT INTO position_protection_cycles VALUES (?, ?)',
                  (at.isoformat(), 'HEALTHY'))
        c.execute('INSERT INTO reconciliation_runs VALUES (?, ?)', (at.isoformat(), 'RECONCILED'))
        c.execute('INSERT INTO ingestion_runs VALUES (?, ?, ?)',
                  (at.isoformat(), 'SUCCEEDED', 'LIVE'))
    return learner, database


def test_independent_monitor_detects_dead_worker_and_stale_protection_without_db_writes(tmp_path):
    learner, database = sources(tmp_path, NOW - timedelta(hours=3))
    before = database.read_bytes()
    receipt = run_monitor(learner, database, tmp_path / 'monitor', now=NOW)
    assert {a['code'] for a in receipt['active_alerts']} == {
        'LEARNER_HEARTBEAT_STALE', 'PROTECTION_HEARTBEAT_STALE',
        'RECONCILIATION_HEARTBEAT_STALE', 'INGESTION_HEARTBEAT_STALE'}
    assert database.read_bytes() == before
    assert receipt['external_delivery'] == 'NOT_CONFIGURED'
    assert (tmp_path / 'monitor' / 'receipt.json').exists()


def test_future_unknown_or_missing_measurement_never_claims_recovery(tmp_path):
    learner, database = sources(tmp_path, NOW - timedelta(hours=3))
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=NOW)
    (learner / 'worker.json').write_text(json.dumps({
        'finished_at': (NOW + timedelta(days=1)).isoformat()}))
    database.unlink()
    receipt = run_monitor(learner, database, output, now=NOW + timedelta(minutes=1))
    assert all(a['severity'] == 'CRITICAL' for a in receipt['active_alerts'])
    assert all(e['event'] != 'RECOVERED' for e in receipt['notifications'])
    assert not database.exists()


def test_fresh_waiting_worker_is_alive_and_real_receipt_recovers(tmp_path):
    learner, database = sources(tmp_path)
    output = tmp_path / 'monitor'
    assert run_monitor(learner, database, output, now=NOW)['active_alerts'] == []
    run_monitor(learner, database, output, now=NOW + timedelta(minutes=16))
    at = NOW + timedelta(minutes=17)
    (learner / 'worker.json').write_text(json.dumps(worker_receipt(at)))
    receipt = run_monitor(learner, database, output, now=at)
    recovered = [e for e in receipt['notifications'] if e['event'] == 'RECOVERED']
    assert {e['code'] for e in recovered} == {'LEARNER_HEARTBEAT_STALE'}


def test_digest_is_periodic_and_ack_does_not_hide_active_warning(tmp_path):
    learner, database = sources(tmp_path, NOW - timedelta(minutes=46))
    output = tmp_path / 'monitor'
    policy = MonitorPolicy(digest_seconds=60)
    first = run_monitor(learner, database, output, now=NOW, policy=policy)
    assert len(first['digest']['alerts']) == 1
    with AlertStore(output / 'alerts.db') as store:
        store.acknowledge('INGESTION_HEARTBEAT_STALE', now=NOW)
    middle = run_monitor(learner, database, output, now=NOW + timedelta(seconds=30), policy=policy)
    assert middle['digest'] is None
    last = run_monitor(learner, database, output, now=NOW + timedelta(seconds=60), policy=policy)
    assert last['digest']['alerts'][0]['acknowledged_at'] is not None


def test_malformed_or_started_worker_does_not_recover(tmp_path):
    learner, database = sources(tmp_path, NOW - timedelta(hours=3))
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=NOW)
    for doc in ({'finished_at': NOW.isoformat()},
                dict(worker_receipt(NOW), status='STARTED'),
                dict(worker_receipt(NOW), code_commit='b' * 40)):
        (learner / 'worker.json').write_text(json.dumps(doc))
        receipt = run_monitor(learner, database, output, now=NOW)
        assert 'LEARNER_HEARTBEAT_STALE' in {a['code'] for a in receipt['active_alerts']}


def test_newer_failures_and_backfill_cannot_establish_fresh_health(tmp_path):
    learner, database = sources(tmp_path, NOW - timedelta(seconds=30))
    with closing(sqlite3.connect(database)) as c, c:
        c.execute('INSERT INTO position_protection_cycles VALUES (?, ?)',
                  (NOW.isoformat(), 'INCIDENT'))
        c.execute('INSERT INTO reconciliation_runs VALUES (?, ?)',
                  (NOW.isoformat(), 'FAILED'))
        c.execute('UPDATE ingestion_runs SET finished_at=?',
                  ((NOW - timedelta(hours=3)).isoformat(),))
        c.execute('INSERT INTO ingestion_runs VALUES (?, ?, ?)',
                  (NOW.isoformat(), 'SUCCEEDED', 'BACKFILL'))
    receipt = run_monitor(learner, database, tmp_path / 'monitor', now=NOW)
    assert {a['code'] for a in receipt['active_alerts']} == {
        'PROTECTION_HEARTBEAT_STALE', 'RECONCILIATION_HEARTBEAT_STALE',
        'INGESTION_HEARTBEAT_STALE'}


def test_running_tick_ages_last_verified_completion_without_flapping_or_recovering(tmp_path):
    learner, database = sources(tmp_path)
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=NOW)
    started = dict(worker_receipt(NOW), status='STARTED', finished_at=None)
    (learner / 'worker.json').write_text(json.dumps(started))
    inside = run_monitor(learner, database, output, now=NOW + timedelta(seconds=30))
    assert 'LEARNER_HEARTBEAT_STALE' not in {a['code'] for a in inside['active_alerts']}
    expired = run_monitor(learner, database, output, now=NOW + timedelta(minutes=16))
    assert 'LEARNER_HEARTBEAT_STALE' in {a['code'] for a in expired['active_alerts']}
    pending = run_monitor(learner, database, output, now=NOW + timedelta(minutes=17))
    assert all(e['event'] != 'RECOVERED' for e in pending['notifications'])


def test_defaults_allow_normal_protection_and_ingestion_cadences(tmp_path):
    learner, database = sources(tmp_path)
    output = tmp_path / 'monitor'
    receipt = run_monitor(learner, database, output, now=NOW + timedelta(minutes=19))
    assert receipt['measurements']['PROTECTION']['status'] == 'FRESH'
    assert receipt['measurements']['INGESTION']['status'] == 'FRESH'
    later = run_monitor(learner, database, output, now=NOW + timedelta(minutes=21))
    assert later['measurements']['PROTECTION']['status'] == 'STALE'


def test_backward_clock_cannot_rewrite_verified_completion(tmp_path):
    learner, database = sources(tmp_path)
    output = tmp_path / 'monitor'
    run_monitor(learner, database, output, now=NOW)
    earlier = NOW - timedelta(minutes=1)
    (learner / 'worker.json').write_text(json.dumps(worker_receipt(earlier)))
    before = (output / 'alerts.db').read_bytes()
    with pytest.raises(ValueError, match='backwards'):
        run_monitor(learner, database, output, now=earlier)
    assert (output / 'alerts.db').read_bytes() == before
