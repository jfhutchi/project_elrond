"""Immutable prospective broker-close lineage; never backfill a missing comparison."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quantbot.brokers.performance import PortfolioCloseHistory, unique_object
from quantbot.operations.daily_performance import CloseEvidence
from quantbot.operations.performance_flows import external_flow

SCHEMA = 'ALPACA_PAPER_CLOSE_REFERENCE_V1'
MODE = {'schema': SCHEMA, 'mark_basis': 'EXACT_EXCHANGE_CLOSE'}


def reference_mode_enabled(directory: Path) -> bool:
    marker = directory / 'close-reference-mode.json'
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8'), object_pairs_hook=unique_object) != MODE:
            raise ValueError('conflicting immutable close reference mode')
        return True
    return any((directory / 'close-references').glob('*.json'))


def _retain_mode(directory: Path) -> None:
    marker = directory / 'close-reference-mode.json'
    payload = json.dumps(MODE, sort_keys=True)
    try:
        with marker.open('x', encoding='utf-8') as handle:
            handle.write(payload)
    except FileExistsError:
        reference_mode_enabled(directory)


def evidence_document(evidence: CloseEvidence) -> str:
    return json.dumps(asdict(evidence), default=str, sort_keys=True)


def evidence_hash(document: str) -> str:
    return hashlib.sha256(document.encode()).hexdigest()


def retain_reference(directory: Path, evidence: CloseEvidence, *, epoch: str,
                     history: PortfolioCloseHistory, reconciliation: Mapping[str, Any],
                     activities: Sequence[Mapping[str, object]] | None) -> None:
    document = evidence_document(evidence)
    activity_source = (json.dumps(activities, default=str, sort_keys=True)
                       if activities is not None else None)
    row = dict(schema=SCHEMA, session_date=str(evidence.session_date),
               close_at=evidence.close_at.isoformat(), captured_at=evidence.captured_at.isoformat(),
               evidence_hash=evidence_hash(document), account_epoch=epoch,
               history_start=history.start.isoformat(), history_end=history.end.isoformat(),
               source_document=history.source_document, source_hash=history.source_hash,
               reconciliation=dict(reconciliation), post_close_activity_count=0,
               activity_source_document=activity_source,
               activity_source_hash=evidence_hash(activity_source) if activity_source else None,
               pinned_account_verified=True)
    # Validate before publication; the same validator is used for prior baseline lineage.
    validate_reference(row, evidence, epoch=epoch)
    _retain_mode(directory)
    receipts = directory / 'close-references'
    receipts.mkdir(exist_ok=True)
    path = receipts / (evidence_hash(document) + '.json')
    payload = json.dumps(row, sort_keys=True)
    try:
        with path.open('x', encoding='utf-8') as handle:
            handle.write(payload)
    except FileExistsError:
        if path.read_text(encoding='utf-8') != payload:
            raise ValueError('conflicting immutable close reference') from None


def validate_reference(row: Mapping[str, Any], evidence: CloseEvidence, *, epoch: str) -> None:
    if (set(row) != {'schema', 'session_date', 'close_at', 'captured_at', 'evidence_hash',
                    'account_epoch', 'history_start', 'history_end', 'source_document',
                    'source_hash', 'reconciliation', 'post_close_activity_count',
                    'pinned_account_verified', 'activity_source_document', 'activity_source_hash'}
            or row['schema'] != SCHEMA or row['session_date'] != str(evidence.session_date)
            or row['close_at'] != evidence.close_at.isoformat()
            or row['captured_at'] != evidence.captured_at.isoformat()
            or row['evidence_hash'] != evidence_hash(evidence_document(evidence))
            or row['account_epoch'] != epoch or row['pinned_account_verified'] is not True
            or type(row['post_close_activity_count']) is not int
            or row['post_close_activity_count'] != 0):
        raise ValueError('unverified close reference lineage')
    history = PortfolioCloseHistory(datetime.fromisoformat(row['history_start']),
                                    datetime.fromisoformat(row['history_end']),
                                    row['source_document'])
    if (history.source_hash != row['source_hash'] or history.end != evidence.close_at
            or history.equity_at(evidence.close_at) != evidence.close_equity):
        raise ValueError('unverified broker close mark')
    activity_source = row['activity_source_document']
    observed_flow = None
    if activity_source is not None:
        if (not isinstance(activity_source, str)
                or evidence_hash(activity_source) != row['activity_source_hash']):
            raise ValueError('unverified close activity source')
        activities = json.loads(activity_source, object_pairs_hook=unique_object)
        if (not isinstance(activities, list)
                or any(not isinstance(item, dict) for item in activities)):
            raise ValueError('unverified close activity source')
        try:
            observed_flow = external_flow(activities)
        except (ValueError, KeyError, ArithmeticError):
            pass
    elif row['activity_source_hash'] is not None:
        raise ValueError('unverified close activity source')
    if (evidence.flows_verified != (observed_flow is not None)
            or observed_flow is not None and observed_flow != evidence.net_external_flow
            or observed_flow is None and evidence.net_external_flow != 0):
        raise ValueError('unverified close cash flows')
    reconciliation = row['reconciliation']
    if (not isinstance(reconciliation, dict)
            or set(reconciliation) != {'id', 'completed_at', 'snapshot_hash'}
            or not isinstance(reconciliation['id'], str) or not reconciliation['id']
            or not isinstance(reconciliation['snapshot_hash'], str)
            or len(reconciliation['snapshot_hash']) != 64):
        raise ValueError('unverified close reconciliation reference')
    completed = datetime.fromisoformat(reconciliation['completed_at'])
    if (completed.tzinfo is None or not evidence.close_at <= completed <= evidence.captured_at
            or evidence.captured_at - completed > timedelta(minutes=2)
            or not evidence.reconciled):
        raise ValueError('unverified close reconciliation timing')


def has_reference(directory: Path, evidence: CloseEvidence, *, epoch: str) -> bool:
    path = directory / 'close-references' / (evidence_hash(evidence_document(evidence)) + '.json')
    if not path.exists():
        return False
    row = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    if not isinstance(row, dict):
        raise ValueError('invalid immutable close reference')
    validate_reference(row, evidence, epoch=epoch)
    return True
