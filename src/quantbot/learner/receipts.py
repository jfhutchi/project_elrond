"""Read-only validation of explicitly commissioned PAPER worker receipts."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from quantbot.domain import StrategyIdentity
from quantbot.learner.forward import _models
from quantbot.learner.paper import PaperModelBinding, assess_paper_entry
from quantbot.research.manifest import content_id


def valid_worker_authority(root: Path, receipt: dict[str, Any]) -> bool:
    """Do not confuse genuine PAPER submissions with live authority or auto-promotion.

    A non-NONE claim must match the configured frozen model, published artifact,
    running code pin and both durable operational records. No broker calls/writes.
    Ordinary observation-only receipts retain their existing contract.
    """
    if receipt.get('trading_enabled') is False and receipt.get('promotion') == 'NONE':
        return True
    try:
        from quantbot.learner.execution_worker import PaperWorkerConfig

        path = root / 'paper-execution.json'
        if path.stat().st_size > 100_000:
            return False
        config = PaperWorkerConfig.model_validate_json(path.read_text(encoding='utf-8-sig'))
        paper = receipt['paper']
        binding = PaperModelBinding.model_validate(paper['binding'])
        identity = StrategyIdentity.model_validate(paper['identity'])
        model = next(m for m in _models(root / 'intraday') if m.model_id == config.model_id)
        submitted = paper['orders_submitted']
        code_hash = content_id({
            name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
            for name in ('intraday.py', 'paper.py', 'paper_runner.py', 'paper_deadlines.py',
                         'inherited.py')
        })
        expected_enabled = (paper['status'] == 'SUCCEEDED'
                            and type(submitted) is int and submitted > 0)
        if (paper['mode'] != 'PAPER' or not config.operational_database.is_absolute()
                or binding.model_id != config.model_id
                or binding.code_hash != code_hash
                or binding.artifact_hash != content_id(model.artifact)
                or identity.git_commit != receipt['code_commit']
                or type(submitted) is not int or submitted < 0
                or paper['trading_enabled'] is not expected_enabled
                or receipt['trading_enabled'] is not expected_enabled
                or receipt['promotion'] != paper['promotion']):
            return False
        decision = assess_paper_entry(
            config.operational_database, binding=binding, identity=identity)
        return decision.allowed and decision.stage == receipt['promotion']
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return False
