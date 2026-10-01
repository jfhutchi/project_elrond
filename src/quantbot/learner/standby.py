"""Read-only, explicit original-entry standby; never grants order authority."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from quantbot.learner.consent import consent_recorded
from quantbot.learner.models import LearnerModel
from quantbot.learner.paper import PaperModelBinding
from quantbot.research.manifest import content_id


class EntryStandby(LearnerModel):
    schema_version: Literal['ELROND_ENTRY_STANDBY_V1'] = Field(alias='schema')
    original_strategy_id: str = Field(pattern=r'^learner-[a-f0-9]{64}$')
    active_strategy_id: str = Field(pattern=r'^learner-[a-f0-9]{64}$')
    active_commit: str = Field(pattern=r'^[a-f0-9]{40}$')
    paper_configuration_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    active_root: Path
    operational_database: Path
    recorded_at: datetime
    authority: Literal['EXPLICIT_OPERATOR_PAPER_PILOT_SWITCH']


def _read(path: Path, maximum: int = 65536) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError('standby input absent or oversized')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('standby input must be a document')
    return value


def standby_status(root: Path, *, now: datetime) -> dict[str, Any]:
    """Verify the selected pilot and actual shadow state; no broker or DB writes."""
    record = EntryStandby.model_validate(_read(root/'entry-standby.json', 4096))
    if (not record.active_root.is_absolute() or not record.operational_database.is_absolute()
            or record.active_root.resolve() == root.resolve() or record.recorded_at > now):
        raise ValueError('invalid standby scope')
    config = _read(record.active_root/'paper-execution.json')
    if content_id(config) != record.paper_configuration_hash:
        raise ValueError('active pilot configuration changed')
    policy = config.get('policy', {})
    if (config.get('mode') != 'PAPER' or config.get('experimental_symbol') != 'DIA'
            or config.get('operational_database') != str(record.operational_database)
            or not 0 < config.get('risk_per_trade_bps', 10000) <= 25
            or not 0 < config.get('max_position_value_bps', 10000) <= 500
            or policy.get('exclusive_learner_entry') is not True
            or policy.get('stop_loss_bps') != 100 or not policy.get('inherited')):
        raise ValueError('active pilot limits/configuration disagree')
    if not consent_recorded(record.active_root, now=now):
        raise ValueError('active pilot consent not verified')
    target = _read(record.active_root/'worker.json')
    finished = datetime.fromisoformat(target['finished_at'])
    if not timedelta(0) <= now-finished <= timedelta(minutes=10):
        raise ValueError('active pilot heartbeat stale')
    paper = target.get('paper', {})
    binding = PaperModelBinding.model_validate(paper['binding'])
    identity = binding.strategy_identity(git_commit=record.active_commit,
        deployed_at=datetime.fromisoformat(paper['identity']['deployment_timestamp'].replace('Z','+00:00')))
    if (identity.strategy_id != record.active_strategy_id
            or identity.model_dump(mode='json') != paper['identity']
            or binding.model_id != config.get('model_id')
            or target.get('code_commit') != record.active_commit):
        raise ValueError('active pilot receipt/binding disagree')
    uri = record.operational_database.resolve().as_uri()+'?mode=ro'
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute('PRAGMA query_only=ON')
        rows = dict(connection.execute('SELECT strategy_id,stage FROM promotion_state '
            'WHERE strategy_id IN (?,?)',
            (record.original_strategy_id, record.active_strategy_id)))
        deployed = connection.execute('SELECT git_commit,configuration_hash FROM '
            'strategy_deployments WHERE strategy_id=? AND version=?',
            (identity.strategy_id, identity.version)).fetchone()
    if (rows.get(record.original_strategy_id) != 'SHADOW'
            or rows.get(record.active_strategy_id) != 'PAPER_OBSERVATION'
            or deployed != (record.active_commit, identity.configuration_hash)):
        raise ValueError('actual entry stages/deployment disagree')
    return dict(status='STANDBY', trading_enabled=False, promotion='SHADOW',
        reason='Explicit pilot switch; original forecasts and outcomes remain active.',
        active_strategy_id=record.active_strategy_id, active_root=str(record.active_root),
        active_pilot_status=paper.get('status'), blockers=list(paper.get('blockers', [])),
        cleared_gate_codes=['PAPER_PROMOTION_REQUIRED','PAPER_EXECUTION_NOT_CONFIGURED',
                            'PAPER_OBSERVATION_NOT_EARNED'], orders_submitted=0)
