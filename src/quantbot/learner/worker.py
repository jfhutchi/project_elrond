"""Ten-minute data, forecast, outcome and bounded challenger worker; no promotion authority."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from quantbot.config import Settings
from quantbot.learner import service
from quantbot.learner.models import Evaluation, EvaluationClass
from quantbot.learner.progress import monitor_progress
from quantbot.learner.store import resolve_from_store
from quantbot.operations.alerts import AlertPolicy, AlertStore
from quantbot.storage import Database, SingleWriterLock
from quantbot.storage.schema import learner_evaluations, learner_outcomes, learner_predictions
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


def run_cycle(
    directory: Path, *, code_commit: str, settings: Settings | None = None,
) -> dict[str, Any]:
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
                evaluations = []
                # This is queue reconciliation, not an audit or a performance claim. Verify
                # persisted receipt integrity without replaying all its raw inputs each tick.
                # New evaluations still use service.evaluate and its full input validation.
                for row in session.execute(select(learner_evaluations)).mappings():
                    document = row['document_json']
                    if hashlib.sha256(document.encode('utf-8')).hexdigest() != row['document_hash']:
                        raise ValueError('evaluation receipt integrity failure')
                    evaluation = Evaluation.model_validate_json(document)
                    if (evaluation.evaluation_id != row['evaluation_id']
                            or evaluation.model_id != row['model_id']
                            or evaluation.evaluation_class.value != row['evaluation_class']
                            or evaluation.created_at != datetime.fromisoformat(row['created_at'])):
                        raise ValueError('evaluation receipt identity failure')
                    evaluations.append(evaluation)
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
            if (root/'forward.json').is_file():
                from quantbot.learner.forward import run_forward_cycle
                receipt['blockers'].remove('FORWARD_PRODUCER_MISSING')
                if settings is None:
                    raise ValueError('forward data capture requires explicit PAPER settings')
                receipt['intraday'] = asyncio.run(run_forward_cycle(
                    root, database, settings, code_commit=code_commit))
                receipt['blockers'] = receipt['intraday']['blockers']
                if (root/'paper-execution.json').is_file():
                    from quantbot.learner.execution_worker import execute_paper_tick
                    receipt['paper'] = asyncio.run(execute_paper_tick(
                        root, database, settings, code_commit=code_commit))
                    receipt['trading_enabled'] = receipt['paper']['trading_enabled']
                    receipt['promotion'] = receipt['paper'].get('promotion') or 'NONE'
                    receipt['blockers'] = [code for code in receipt['blockers'] if code not in
                        ('PAPER_PROMOTION_REQUIRED', 'PAPER_EXECUTION_NOT_CONFIGURED')]
                    receipt['blockers'].extend(receipt['paper']['blockers'])
            receipt['status'] = 'BLOCKED'
        except Exception:
            # Provider or validation exceptions may include secrets; only publish a fixed code.
            receipt['status'] = 'ERROR'
            receipt['blockers'].append('OUTCOME_PROCESSING_FAILED')
        finally:
            if database is not None:
                database.close()
            receipt['finished_at'] = datetime.now(UTC).isoformat()
            try:
                policy_path = root / 'alert-policy.json'
                policy = AlertPolicy(**json.loads(policy_path.read_text(encoding='utf-8'))) \
                    if policy_path.is_file() else AlertPolicy()
                with AlertStore(root / 'state' / 'alerts.db', policy=policy) as alerts:
                    receipt['progress'] = monitor_progress(
                        alerts, receipt, now=datetime.fromisoformat(receipt['finished_at']))
            except Exception:
                receipt['status'] = 'ERROR'
                receipt['blockers'].append('ALERT_STATE_FAILED')
            _publish(root, receipt, final=True)
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--bootstrap-start', type=lambda s: datetime.fromisoformat(s).date())
    parser.add_argument('--bootstrap-end', type=lambda s: datetime.fromisoformat(s).date())
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                         text=True, timeout=30).strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=repository,
                                        text=True, timeout=30).strip()
        if commit != args.commit or dirty:
            raise ValueError('runtime checkout does not match clean pin')
        if args.env_file is None:
            result = run_cycle(args.directory, code_commit=commit)
        else:
            from quantbot.operations.service import isolated_application_environment
            with isolated_application_environment(args.env_file):
                settings = Settings()
                if args.bootstrap_start or args.bootstrap_end:
                    from quantbot.learner.forward import bootstrap_history
                    if not args.bootstrap_start or not args.bootstrap_end:
                        raise ValueError('both bootstrap bounds required')
                    with SingleWriterLock(args.directory/'state'/'learner-worker.lock'):
                        with Database(args.directory/'state'/'learner.db') as database:
                            result = asyncio.run(bootstrap_history(
                                args.directory, database, settings, code_commit=commit,
                                start=args.bootstrap_start, end=args.bootstrap_end))
                else:
                    result = run_cycle(args.directory, code_commit=commit, settings=settings)
    except Exception:
        print('LEARNER_WORKER_START_FAILED')
        return 1
    print(json.dumps(result))
    return 1 if result['status'] == 'ERROR' else 0


if __name__ == '__main__':
    raise SystemExit(main())
