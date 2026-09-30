"""The dashboard must not turn saved replay results into a running/profitable learner."""
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.learner import read_learner_panel
from quantbot.research.manifest import content_id
from quantbot.storage import Database


def test_worker_receipt_fresh_stale_and_wrong_pin(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    deployment = json.loads((root / 'deployment.json').read_text())
    deployment.update(scheduled=True, interval_minutes=10)
    (root / 'deployment.json').write_text(json.dumps(deployment))
    receipt = dict(code_commit='a'*40, status='BLOCKED',
                   finished_at=datetime.now(UTC).isoformat(), resolved_this_run=0,
                   trading_enabled=False, promotion='NONE',
                   blockers=['FORWARD_PRODUCER_MISSING', 'PAPER_PROMOTION_REQUIRED'])
    def save():
        (root / 'worker.json').write_text(json.dumps(receipt))
        return read_learner_panel(root)
    panel = save()
    assert panel.status == 'SCHEDULED_BLOCKED'
    assert any('PAPER entry' in blocker for blocker in panel.worker_blockers)
    assert all('observation deployment' not in blocker for blocker in panel.worker_blockers)
    receipt['finished_at'] = (datetime.now(UTC)-timedelta(minutes=16)).isoformat()
    assert save().status == 'STALE_WORKER'
    receipt['code_commit'] = 'b'*40
    assert save().status == 'WORKER_UNVERIFIED'
    receipt['code_commit'] = 'a'*40
    receipt['finished_at'] = datetime.now(UTC).isoformat()
    receipt['status'] = 'ERROR'
    assert save().status == 'WORKER_ERROR'
    receipt['blockers'] = ['sk-SYNTHETICabcdefghijklmnop']
    assert save().status == 'WORKER_UNVERIFIED'
    assert 'SYNTHETIC' not in save().model_dump_json()


def fixture_store(root: Path) -> Path:
    (root / 'state').mkdir(parents=True)
    (root / 'evidence').mkdir()
    model = dict(model_id='model-1', dataset_id='dataset-1', family='ridge', version=1,
                 parent_model_id=None, created_at='2026-09-21T14:00:00Z',
                 code_commit='a' * 40, code_dirty=False, artifact_hash='b' * 64)
    doc = json.dumps(model)
    with sqlite3.connect(root / 'state' / 'learner.db') as c:
        c.executescript('''
        CREATE TABLE learner_model_versions (model_id TEXT, document_hash TEXT, document_json TEXT);
        CREATE TABLE learner_predictions (prediction_id TEXT, model_id TEXT,
            evaluation_class TEXT, recorded_at TEXT);
        CREATE TABLE learner_outcomes (prediction_id TEXT, resolved_at TEXT);
        CREATE TABLE learner_evaluations (created_at TEXT);
        CREATE TABLE search_runs (actor TEXT, candidates_evaluated INTEGER);
        ''')
        c.execute('INSERT INTO learner_model_versions VALUES (?,?,?)',
                  ('model-1', hashlib.sha256(doc.encode()).hexdigest(), doc))
        c.execute("INSERT INTO learner_predictions VALUES ('p1','model-1','HISTORICAL_REPLAY',"
                  "'2026-09-21T14:01:00Z')")
        c.execute("INSERT INTO learner_outcomes VALUES ('p1','2026-09-21T14:02:00Z')")
        c.execute("INSERT INTO learner_evaluations VALUES ('2026-09-21T14:03:00Z')")
        c.execute("INSERT INTO search_runs VALUES ('elrond-learner',1)")
    portfolio = dict(net_return=-.035, max_drawdown=.09, excess_return_vs_spy=.025,
                     cost_over_initial=.012, traded_notional_over_initial=24,
                     mean_daily_gross_exposure=1,
                     daily_equity=[dict(at='2026-03-31T20:00:00Z', equity=.965)])
    report = dict(contract='learner-next-open-fifth-close-liquidated-v1',
                  lineage=dict(model_id='model-1', model_hash=content_id(model),
                              dataset_id='dataset-1', code_commit='a' * 40),
                  evidence_class='EXPLORATORY_HISTORICAL_REPLAY', promotion='NONE',
                  positive_expectancy_justified=False, period_count=1,
                  predictive=dict(mean_ic=-.16), prediction_ids=['p1'],
                  costs=dict(slippage_bps_per_side='5', commission_per_order='0'),
                  periods=[dict(decision_at='2026-03-24T20:00:00Z',
                                entry_at='2026-03-25T13:30:00Z',
                                exit_at='2026-03-31T20:00:00Z')],
                  portfolios={k: portfolio for k in ('top_quintile','all_universe','spy')},
                  secret='do-not-render-this')
    (root / 'evidence' / 'ridge-economics.json').write_text(json.dumps(
        dict(report_hash=content_id(report), report=report)))
    (root / 'deployment.json').write_text(json.dumps(dict(
        learner_commit='a' * 40, scheduled=False, forecasting=False,
        holdout=dict(start='2026-04-01', end='2026-09-30'), secret='do-not-render-this')))
    return root


def test_missing_store_does_not_create_database_or_claim_zero(tmp_path):
    root = tmp_path / 'missing'
    panel = read_learner_panel(root)
    assert panel.status == 'UNAVAILABLE'
    assert panel.historical_predictions is None
    assert not root.exists()


def test_saved_evidence_is_not_a_live_worker_and_reads_do_not_write(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    db = root / 'state' / 'learner.db'
    before = db.read_bytes()
    panel = read_learner_panel(root)
    assert panel.status == 'NOT_SCHEDULED'
    assert panel.historical_predictions == panel.resolved_predictions == 1
    assert panel.prospective_predictions == panel.shadow_predictions == 0
    assert panel.fit_attempts == 1
    assert panel.performance[0].net_return == -.035
    assert panel.performance[0].rank_ic == -.16
    assert panel.last_evaluation_at == '2026-09-21T14:03:00Z'
    assert 'do-not-render-this' not in panel.model_dump_json()
    assert db.read_bytes() == before


def test_tampered_report_is_not_rendered_as_performance(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    path = root / 'evidence' / 'ridge-economics.json'
    payload = json.loads(path.read_text())
    payload['report']['portfolios']['top_quintile']['net_return'] = 12
    path.write_text(json.dumps(payload))
    panel = read_learner_panel(root)
    assert not panel.performance
    assert panel.report_errors


def test_report_bound_to_another_model_is_rejected(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    path = root / 'evidence' / 'ridge-economics.json'
    payload = json.loads(path.read_text())
    payload['report']['lineage']['model_hash'] = 'c' * 64
    payload['report_hash'] = content_id(payload['report'])
    path.write_text(json.dumps(payload))
    assert not read_learner_panel(root).performance


def test_scheduled_declaration_never_proves_running(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    (root / 'deployment.json').write_text('{"scheduled":true,"forecasting":true}')
    panel = read_learner_panel(root)
    assert panel.status == 'WORKER_UNVERIFIED'


def test_different_stock_selections_still_have_matched_comparison_dates(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    with sqlite3.connect(root / 'state' / 'learner.db') as c:
        document = c.execute('SELECT document_json FROM learner_model_versions').fetchone()[0]
        model = json.loads(document)
        model.update(model_id='mean-1', family='mean')
        document = json.dumps(model)
        c.execute('INSERT INTO learner_model_versions VALUES (?,?,?)',
                  ('mean-1', hashlib.sha256(document.encode()).hexdigest(), document))
    path = root / 'evidence' / 'ridge-economics.json'
    payload = json.loads(path.read_text())
    report = payload['report']
    report['periods'][0]['top'] = ['ABC']
    path.write_text(json.dumps(dict(report_hash=content_id(report), report=report)))
    report['periods'][0]['top'] = ['ABC', 'XYZ']
    report['lineage'].update(model_id='mean-1', model_hash=content_id(model))
    (root / 'evidence' / 'mean-economics.json').write_text(json.dumps(
        dict(report_hash=content_id(report), report=report)))
    assert len(read_learner_panel(root).performance) == 3


def test_page_api_and_navigation_expose_same_whitelisted_state(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    with Database.create(tmp_path / 'operator.db') as database:
        with TestClient(create_app(database, learner_directory=root)) as client:
            page = client.get('/learner')
            assert page.status_code == 200
            assert 'Historical replay' in page.text
            assert '-3.50%' in page.text
            assert 'Not scheduled' in page.text
            assert 'do-not-render-this' not in page.text
            data = client.get('/api/learner').json()
            assert data == client.get('/api/state').json()['learner']
            assert data['historical_predictions'] == 1
            assert 'href="/learner"' in client.get('/').text
            assert 'href="/learner"' in client.get('/research').text
            assert client.post('/learner').status_code == 405


def test_bad_schema_does_not_break_operations(tmp_path):
    root = tmp_path / 'learner'
    (root / 'state').mkdir(parents=True)
    with sqlite3.connect(root / 'state' / 'learner.db'):
        pass
    with Database.create(tmp_path / 'operator.db') as database:
        with TestClient(create_app(database, learner_directory=root)) as client:
            assert client.get('/').status_code == 200
            assert client.get('/api/learner').json()['status'] == 'UNAVAILABLE'


def test_bad_holdout_metadata_cannot_crash_pages_or_leak_values(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    for holdout in (None, {'start': 'sk-SYNTHETICabcdefghijklmnop', 'end': '2026-09-30'}):
        (root / 'deployment.json').write_text(json.dumps(dict(holdout=holdout)))
        panel = read_learner_panel(root)
        assert panel.status == 'UNAVAILABLE'
        assert 'SYNTHETIC' not in panel.model_dump_json()


def test_renaming_ridge_report_does_not_turn_it_into_mean_baseline(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    evidence = root / 'evidence'
    (evidence / 'mean-economics.json').write_bytes((evidence / 'ridge-economics.json').read_bytes())
    names = [r.name for r in read_learner_panel(root).performance]
    assert 'Mean / equal-weight baseline' not in names
    assert 'Equal-weight baseline' in names


def test_other_execution_contract_is_not_presented_as_next_open(tmp_path):
    root = fixture_store(tmp_path / 'learner')
    path = root / 'evidence' / 'ridge-economics.json'
    payload = json.loads(path.read_text())
    payload['report']['contract'] = 'unrelated-close-to-close-contract'
    payload['report_hash'] = content_id(payload['report'])
    path.write_text(json.dumps(payload))
    assert not read_learner_panel(root).performance


def test_intraday_worker_dashboard_and_invalid_counters(tmp_path):
    root = fixture_store(tmp_path/'learner')
    deployment = json.loads((root/'deployment.json').read_text())
    deployment.update(scheduled=True, interval_minutes=10)
    (root/'deployment.json').write_text(json.dumps(deployment))
    intraday = dict(mode='INTRADAY_30_MINUTE', captured_at=datetime.now(UTC).isoformat(),
                    captured_bars=999, observations_this_run=2, predictions_this_run=4,
                    outcomes_this_run=2, trained_this_run=0, observation_cohorts=12,
                    resolved_cohorts=9, prediction_cohorts=24, model_count=2,
                    training_status='WAITING_FOR_NEW_TRAINING_OUTCOMES', performance=[
                        dict(model_id='b'*64, matched_periods=1, simulated_net_return=-.001,
                             simulated_max_drawdown=.001, rank_ic=None, mse=.0001)])
    receipt = dict(code_commit='a'*40, status='BLOCKED', finished_at=datetime.now(UTC).isoformat(),
                   resolved_this_run=0, trading_enabled=False, promotion='NONE',
                   blockers=['PAPER_EXECUTION_NOT_CONFIGURED'], intraday=intraday)
    def save():
        (root/'worker.json').write_text(json.dumps(receipt))
        return read_learner_panel(root)
    assert save().intraday.predictions_this_run == 4
    with Database.create(tmp_path/'operator.db') as database:
        with TestClient(create_app(database, learner_directory=root)) as client:
            page = client.get('/learner')
            assert page.status_code == 200
            assert '999' in page.text
            assert 'not paper fills' in page.text.lower()
    for value in (-1, True, 'sk-SYNTHETICabcdefghijklmnop'):
        intraday['captured_bars'] = value
        assert save().status == 'WORKER_UNVERIFIED'
        assert 'SYNTHETIC' not in save().model_dump_json()


def test_progress_alerts_display_owner_action_and_ack_without_resolving(tmp_path):
    from quantbot.learner.progress import monitor_progress
    from quantbot.operations.alerts import AlertStore

    root = fixture_store(tmp_path / 'learner')
    now = datetime.now(UTC)
    (root / 'deployment.json').write_text(json.dumps(dict(
        scheduled=True, interval_minutes=10, learner_commit='a' * 40)))
    receipt = dict(code_commit='a' * 40, status='BLOCKED', finished_at=now.isoformat(),
                   resolved_this_run=0, trading_enabled=False, promotion='NONE',
                   blockers=['PAPER_PROMOTION_REQUIRED'])
    with AlertStore(root / 'state' / 'alerts.db') as alerts:
        monitor_progress(alerts, receipt, now=now)
        alerts.acknowledge('PAPER_PROMOTION_REQUIRED', now=now)
        receipt['progress'] = monitor_progress(alerts, receipt, now=now)
    (root / 'worker.json').write_text(json.dumps(receipt))
    assert read_learner_panel(root).progress.paper_status == 'OPERATOR_ACTION_REQUIRED'
    with Database.create(tmp_path / 'operator.db') as database:
        with TestClient(create_app(database, learner_directory=root)) as client:
            page = client.get('/learner').text
            assert 'Operator action required' in page
            assert 'Operator' in page
            assert 'exact PAPER model deployment' in page
            assert 'Acknowledged; condition remains active' in page
            assert 'External delivery is not configured' in page
