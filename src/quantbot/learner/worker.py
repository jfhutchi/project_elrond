"""Bounded scheduled outcome processing; no forecast, fit, broker or promotion authority."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from quantbot.learner import service
from quantbot.learner.models import EvaluationClass
from quantbot.learner.repository import LearnerRepository
from quantbot.learner.store import resolve_from_store
from quantbot.storage import Database, SingleWriterLock
from quantbot.storage.schema import learner_outcomes, learner_predictions
from quantbot.strategy import XNYSSessionSequence


def _publish(root: Path, receipt: dict[str, Any], *, final: bool) -> None:
    document = json.dumps(receipt, sort_keys=True)
    temporary = root / 'worker.json.tmp'
    temporary.write_text(document + '\n', encoding='utf-8')
    os.replace(temporary, root / 'worker.json')
    if final:
        (root / 'logs').mkdir(exist_ok=True)
        with (root / 'logs' / 'worker-runs.jsonl').open('a', encoding='utf-8') as output:
            output.write(document + '\n')


def run_cycle(directory: Path, *, code_commit: str) -> dict[str, Any]:
    root = directory.resolve()
    path = root / 'state' / 'learner.db'
    if not path.is_file():
        raise FileNotFoundError('existing learner database required')
    with SingleWriterLock(root / 'state' / 'learner-worker.lock'):
        receipt: dict[str, Any] = dict(
            run_id=str(uuid4()), code_commit=code_commit, status='STARTED',
            started_at=datetime.now(UTC).isoformat(), finished_at=None,
            resolved_this_run=0, evaluated_this_run=0, trading_enabled=False, promotion='NONE',
            blockers=['FORWARD_PRODUCER_MISSING', 'PAPER_PROMOTION_REQUIRED'],
        )
        _publish(root, receipt, final=False)
        database = None
        try:
            database = Database(path)
            with database.transaction() as session:
                pending = tuple(session.scalars(select(learner_predictions.c.model_id).where(
                    learner_predictions.c.evaluation_class == 'HISTORICAL_REPLAY',
                    ~select(learner_outcomes.c.prediction_id).where(
                        learner_outcomes.c.prediction_id == learner_predictions.c.prediction_id
                    ).exists(),
                ).distinct()))
            if pending:
                calendar = XNYSSessionSequence.model_validate_json(
                    (root / 'evidence' / 'calendar.json').read_text(encoding='utf-8-sig'))
                for model_id in pending:
                    resolved = resolve_from_store(database, model_id, calendar)
                    receipt['resolved_this_run'] += resolved
            # Resolve and evaluate have separate transactions. Reconcile exact evidence sets
            # every tick so a crash after resolution cannot permanently skip evaluation.
            with database.transaction() as session:
                evaluations = LearnerRepository(session).list_evaluations()
                rows = session.execute(select(
                    learner_predictions.c.model_id, learner_predictions.c.prediction_id,
                    learner_outcomes.c.prediction_id.label('resolved_id'),
                ).select_from(learner_predictions.outerjoin(learner_outcomes)).where(
                    learner_predictions.c.evaluation_class == 'HISTORICAL_REPLAY')).all()
            expected: dict[str, set[str]] = {}
            scored: dict[str, set[str]] = {}
            for model_id, prediction_id, resolved_id in rows:
                expected.setdefault(model_id, set()).add(prediction_id)
                if resolved_id is not None:
                    scored.setdefault(model_id, set()).add(prediction_id)
            for model_id, ids in scored.items():
                if not any(e.model_id == model_id
                           and e.evaluation_class is EvaluationClass.HISTORICAL_REPLAY
                           and set(e.prediction_ids) == ids
                           and set(e.expected_prediction_ids) == expected[model_id]
                           for e in evaluations):
                    service.evaluate(database, model_id, EvaluationClass.HISTORICAL_REPLAY)
                    receipt['evaluated_this_run'] += 1
            receipt['status'] = 'BLOCKED'
        except Exception:
            # Provider or validation exceptions may include secrets; only publish a fixed code.
            receipt['status'] = 'ERROR'
            receipt['blockers'].append('OUTCOME_PROCESSING_FAILED')
        finally:
            if database is not None:
                database.close()
            receipt['finished_at'] = datetime.now(UTC).isoformat()
            _publish(root, receipt, final=True)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                         text=True, timeout=30).strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=repository,
                                        text=True, timeout=30).strip()
        if commit != args.commit or dirty:
            raise ValueError('runtime checkout does not match clean pin')
        result = run_cycle(args.directory, code_commit=commit)
    except Exception:
        print('LEARNER_WORKER_START_FAILED')
        return 1
    print(json.dumps(result))
    return 1 if result['status'] == 'ERROR' else 0


if __name__ == '__main__':
    raise SystemExit(main())
