"""Auditable inherited PAPER positions, without entry or exit-order authority.

A baseline exempts only unchanged, continuously observed inherited holdings from
the learner's one-new-position cap. Every holding still enters aggregate risk
calculations. Closing/changing a holding permanently retires its exemption.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from quantbot.domain import Position, StrategyIdentity
from quantbot.learner.intraday import IntradayPrediction
from quantbot.learner.models import LearnerModel
from quantbot.research.manifest import content_id
from quantbot.storage import Database, StorageRepository

BASELINE = 'LEARNER_INHERITED_BASELINE_V1'
RETIRED = 'LEARNER_INHERITED_RETIRED_V1'
MONITOR = 'LEARNER_INHERITED_MONITOR_V1'


class InheritedPosition(LearnerModel):
    symbol: str = Field(pattern=r'^[A-Z][A-Z0-9.]{0,31}$')
    quantity: Decimal
    average_entry_price: Decimal = Field(gt=0)

    @model_validator(mode='after')
    def nonzero(self) -> Self:
        if not self.quantity:
            raise ValueError('inherited position quantity must be nonzero')
        return self


class InheritedBaseline(LearnerModel):
    account_id_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    captured_at: datetime
    positions: tuple[InheritedPosition, ...] = Field(max_length=1000)

    @model_validator(mode='after')
    def unique(self) -> Self:
        if len({p.symbol for p in self.positions}) != len(self.positions):
            raise ValueError('duplicate inherited symbol')
        return self


def observe_inherited(
    database: Database, *, baseline: InheritedBaseline, positions: Sequence[Position],
    account_id: str, identity: StrategyIdentity, run_id: str, now: datetime,
    predictions: Sequence[IntradayPrediction], cost_bps: int,
) -> set[str]:
    """Record immutable provenance and current thesis; never adopt/order a legacy position."""
    if content_id({'paper_account': account_id}) != baseline.account_id_hash:
        raise ValueError('inherited account baseline mismatch')
    if now.tzinfo is None or baseline.captured_at > now:
        raise ValueError('inherited baseline timestamp invalid')
    current = {p.symbol: p for p in positions}
    if len(current) != len(positions):
        raise ValueError('duplicate observed position')
    forecast = {p.features.symbol: p for p in predictions}
    baseline_id = content_id({'strategy': identity.strategy_id, 'baseline': baseline})
    unchanged: set[str] = set()
    with database.transaction() as session:
        repo = StorageRepository(session)
        records = repo.list_signals()
        changed_since_baseline = {
            fill.symbol for fill in repo.list_fills()
            if fill.occurred_at > baseline.captured_at
        }
        known_ids = {r.signal_id for r in records}
        retired = {r.symbol for r in records if r.payload.get('kind') == RETIRED
                   and r.payload.get('baseline_id') == baseline_id}
        owned = set()
        for row in records:
            document = row.payload.get('document')
            if row.payload.get('kind') != 'LEARNER_PAPER_DEADLINE_V1' or not isinstance(
                document, dict
            ):
                continue
            entry = document.get('entry_intent')
            if isinstance(entry, dict) and isinstance(entry.get('symbol'), str):
                owned.add(entry['symbol'])
        initial_id = 'inherited-baseline-' + baseline_id
        if initial_id not in known_ids:
            repo.record_signal(initial_id, run_id=run_id, strategy_id=identity.strategy_id,
                symbol='INHERITED', occurred_at=now,
                payload={'kind': BASELINE, 'baseline_id': baseline_id,
                         'document': baseline.model_dump(mode='json'),
                         'entry_authority': False, 'exit_authority': False})
        for inherited in baseline.positions:
            position = current.get(inherited.symbol)
            if inherited.symbol in retired:
                continue
            matches = (position is not None and position.quantity == inherited.quantity
                       and position.average_entry_price == inherited.average_entry_price
                       and inherited.symbol not in owned
                       and inherited.symbol not in changed_since_baseline)
            if not matches:
                repo.record_signal('inherited-retired-' + content_id(
                    {'baseline': baseline_id, 'symbol': inherited.symbol}),
                    run_id=run_id, strategy_id=identity.strategy_id, symbol=inherited.symbol,
                    occurred_at=now, payload={'kind': RETIRED, 'baseline_id': baseline_id,
                                             'reason': 'POSITION_CHANGED_CLOSED_OR_LEARNER_OWNED'})
                continue
            unchanged.add(inherited.symbol)
            prediction = forecast.get(inherited.symbol)
            if prediction is None:
                continue
            event_id = 'inherited-monitor-' + content_id(
                {'run': run_id, 'symbol': inherited.symbol, 'prediction': prediction.prediction_id})
            if event_id not in known_ids:
                repo.record_signal(event_id, run_id=run_id, strategy_id=identity.strategy_id,
                    symbol=inherited.symbol, occurred_at=now,
                    payload={'kind': MONITOR, 'baseline_id': baseline_id, 'action': 'MONITOR',
                        'entry_provenance': 'INHERITED_NO_LEARNER_ENTRY',
                        'forecast': prediction.model_dump(mode='json'),
                        'remaining_edge_after_modeled_cost': prediction.score > cost_bps / 10000,
                        'exit_owner': 'EXISTING_STRATEGY_AND_POSITION_PROTECTION',
                        'learner_exit_authority': False})
    return unchanged
