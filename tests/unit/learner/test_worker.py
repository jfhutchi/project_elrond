"""Scheduled outcome processing must never manufacture fits, forecasts or orders."""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from quantbot.learner import service
from quantbot.learner.store import StoreReplaySpec, build_from_store
from quantbot.learner.worker import run_cycle
from quantbot.storage import AlreadyLockedError, SingleWriterLock
from quantbot.storage.schema import (
    learner_evaluations,
    learner_outcomes,
    learner_predictions,
    search_runs,
)
from tests.unit.learner.test_store import stored


def worker_store(tmp_path):
    root = tmp_path / 'learner'
    (root / 'state').mkdir(parents=True)
    database, calendar = stored(root / 'state')
    # Existing fixture file is research.db; worker requires a fixed dedicated target.
    database.close()
    (root / 'state' / 'research.db').rename(root / 'state' / 'learner.db')
    from quantbot.storage import Database
    database = Database(root / 'state' / 'learner.db')
    dates = [s.session_date for s in calendar.sessions]
    spec = StoreReplaySpec(
        sessions=calendar, symbols=('AAPL','MSFT'), train_start=dates[60],train_end=dates[70],
        validation_start=dates[71],validation_end=dates[84],
        holdout_start=dates[85],holdout_end=dates[94])
    dataset = build_from_store(database,spec)
    model = service.train(database,dataset.identity)
    service.replay(database,model.model_id)
    (root / 'evidence').mkdir()
    (root / 'evidence' / 'calendar.json').write_text(calendar.model_dump_json())
    return root,database


def test_worker_resolves_once_without_more_fits_forecasts_or_promotion(tmp_path):
    root, database = worker_store(tmp_path)
    def counts():
        with database.transaction() as session:
            return tuple(session.scalar(select(func.count()).select_from(t))
                         for t in (search_runs,learner_predictions,learner_outcomes))
    before=counts()
    first=run_cycle(root,code_commit='a'*40)
    second=run_cycle(root,code_commit='a'*40)
    assert first['resolved_this_run'] == before[1]
    assert second['resolved_this_run'] == 0
    assert counts() == (before[0],before[1],before[1])
    assert second['status'] == 'BLOCKED'
    assert second['trading_enabled'] is False
    assert 'PAPER_PROMOTION_REQUIRED' in second['blockers']
    receipt=json.loads((root/'worker.json').read_text())
    assert receipt['run_id']==second['run_id']
    assert len((root/'logs'/'worker-runs.jsonl').read_text().splitlines())==2
    database.close()


def test_worker_missing_database_does_not_create_it(tmp_path):
    with pytest.raises(FileNotFoundError):
        run_cycle(tmp_path,code_commit='a'*40)
    assert not (tmp_path/'state'/'learner.db').exists()


def test_worker_refuses_duplicate_writer(tmp_path):
    root,database=worker_store(tmp_path)
    with SingleWriterLock(root/'state'/'learner-worker.lock'):
        with pytest.raises(AlreadyLockedError):
            run_cycle(root,code_commit='a'*40)
    database.close()


def test_worker_error_is_recorded_without_raw_exception_text(tmp_path,monkeypatch):
    root,database=worker_store(tmp_path)
    def fail(*args,**kwargs):
        raise ValueError('sk-SYNTHETICabcdefghijklmnop')
    monkeypatch.setattr('quantbot.learner.worker.resolve_from_store',fail)
    result=run_cycle(root,code_commit='a'*40)
    assert result['status']=='ERROR'
    assert 'SYNTHETIC' not in (root/'worker.json').read_text()
    assert datetime.fromisoformat(result['finished_at']) <= datetime.now(UTC)
    database.close()


def test_worker_retries_evaluation_after_resolution_commits(tmp_path, monkeypatch):
    root, database = worker_store(tmp_path)
    original = service.evaluate
    def fail(*args, **kwargs):
        raise ValueError('interrupted evaluation')
    monkeypatch.setattr(service, 'evaluate', fail)
    first = run_cycle(root, code_commit='a'*40)
    assert first['status'] == 'ERROR'
    assert first['resolved_this_run'] > 0
    monkeypatch.setattr(service, 'evaluate', original)
    second = run_cycle(root, code_commit='a'*40)
    assert second['resolved_this_run'] == 0
    assert second['evaluated_this_run'] == 1
    assert run_cycle(root, code_commit='a'*40)['evaluated_this_run'] == 0
    database.close()


def test_worker_checks_evaluation_integrity_without_reauditing_inputs(tmp_path, monkeypatch):
    root, database = worker_store(tmp_path)
    assert run_cycle(root, code_commit='a'*40)['status'] == 'BLOCKED'
    from quantbot.learner.repository import LearnerRepository
    def fail(*args, **kwargs):
        raise AssertionError('no full input audit for already evaluated IDs')
    monkeypatch.setattr(LearnerRepository, '_require_evaluation_inputs', fail)
    assert run_cycle(root, code_commit='a'*40)['status'] == 'BLOCKED'
    with database.transaction() as session:
        session.execute(update(learner_evaluations).values(document_hash='f'*64))
    assert run_cycle(root, code_commit='a'*40)['status'] == 'ERROR'
    database.close()


def test_configured_worker_runs_forward_cycle_and_reports_its_blockers(tmp_path, monkeypatch):
    from quantbot.config import Settings
    root, database = worker_store(tmp_path)
    (root/'forward.json').write_text('{}')
    seen = []
    async def forward_cycle(directory, database, settings, *, code_commit):
        seen.append((directory, code_commit))
        return dict(blockers=['PAPER_PROMOTION_REQUIRED'], captured_bars=99)
    monkeypatch.setattr('quantbot.learner.forward.run_forward_cycle', forward_cycle)
    result = run_cycle(root, code_commit='a'*40, settings=Settings())
    assert seen == [(root.resolve(), 'a'*40)]
    assert result['intraday']['captured_bars'] == 99
    assert 'FORWARD_PRODUCER_MISSING' not in result['blockers']
    assert result['trading_enabled'] is False
    database.close()
