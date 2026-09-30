"""Operator consent is communication evidence, never an execution authority."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from quantbot.learner.models import LearnerModel


class PaperConsent(LearnerModel):
    schema_version: Literal['ELROND_PAPER_CONSENT_V1'] = Field(alias='schema')
    mode: Literal['PAPER']
    model_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    recorded_at: datetime
    authority: Literal['EXPLICIT_OPERATOR_CHAT_APPROVAL']
    maximum_new_positions: Literal[1]
    maximum_position_bps: Literal[500]
    maximum_entry_risk_bps: Literal[25]
    stop_bps: Literal[100]
    horizon_minutes: Literal[30]
    inherited_positions: Literal['MONITOR_ONLY_EXISTING_EXIT_OWNER']


def consent_recorded(root: Path, *, now: datetime) -> bool:
    """Require the exact published strategy; no promotion, writes or broker calls."""
    path = root / 'paper-consent.json'
    try:
        if path.stat().st_size > 4096:
            return False
        consent = PaperConsent.model_validate_json(path.read_text(encoding='utf-8'))
        if consent.recorded_at > now:
            return False
        configured = root / 'paper-execution.json'
        if configured.exists():
            import json
            if configured.stat().st_size > 65536:
                return False
            config = json.loads(configured.read_text(encoding='utf-8'))
            if not isinstance(config, dict) or config.get('model_id') != consent.model_id:
                return False
        from quantbot.learner.forward import _models
        return any(model.model_id == consent.model_id for model in _models(root / 'intraday'))
    except (OSError, ValueError, KeyError, TypeError):
        return False
