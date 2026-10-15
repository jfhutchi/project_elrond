"""Proposed cross-boundary safety acceptance. Failures are audit evidence, not fixes.

Run through run_probes.py with an explicit source archive or isolated checkout.
All state and provider values are synthetic; production code is imported read-only.
"""
from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.storage import Database, StorageRepository
from tests.unit.learner.test_experiment_lots import OPEN, lots, prepare
from tests.unit.operations.test_position_protection_supervisor import (
    NOW, IDENTITY, FakeBroker, account, position, supervisor,
)


def test_provider_fill_without_fee_evidence_cannot_finalize_net_profit(tmp_path):
    fill = AlpacaPaperBrokerAdapter._fill_from_payload({
        'activity_type': 'FILL', 'id': 'synthetic-fill', 'order_id': 'stop',
        'symbol': 'SYN0', 'side': 'sell', 'qty': '1', 'price': '102',
        'transaction_time': (OPEN + timedelta(seconds=5)).isoformat(),
    })
    journal = prepare(tmp_path, exit_fee=fill.fee)
    outcome = lots().resolve_lot(journal, 'a' * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.net_pnl is None, f'No provider fee evidence, yet finalized {outcome.net_pnl}'
    assert 'FEE_UNAVAILABLE' in outcome.reasons


@pytest.mark.asyncio
async def test_repeated_unreconciled_observation_cannot_become_reconciliation(tmp_path):
    with Database.create(tmp_path / 'synthetic.db') as db:
        local = account().account.model_copy(update={'cash': Decimal('99925')})
        actual = account().model_copy(update={
            'account': account().account.model_copy(update={'cash': Decimal('99850')})})
        with db.transaction() as session:
            repo = StorageRepository(session)
            repo.save_strategy_deployment(IDENTITY)
            repo.save_account_snapshot('synthetic-reconciled-baseline', local,
                (position('XLE', quantity='1', price='75'),),
                captured_at=NOW - timedelta(minutes=5))
        subject = supervisor(db, FakeBroker(actual, (position('XLE', quantity='2', price='75'),)))
        first = await subject.evaluate(now=NOW)
        second = await subject.evaluate(now=NOW + timedelta(seconds=15))
        assert first.unprotected_count == 1
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.list_fills() == []
            assert repo.get_latest_reconciliation() is None
        assert second.unprotected_count == 1, 'Repeated broker read silently became reconciled'
        assert second.exit_required_count == 0


@pytest.mark.asyncio
async def test_unstable_quote_acquisition_pauses_entries_without_latching_global_stop(tmp_path):
    from tests.fault.test_stable_snapshot_recovery import (
        SequencedBroker, account as snapshot_account, positions, _prepare_database,
    )
    from tests.fault.test_paper_transient_acquisition import cycle
    from quantbot.operations.kill_switch import KillSwitchController
    with _prepare_database(tmp_path, snapshot_account(), positions()) as db:
        broker = SequencedBroker(['2000.00', '2000.01', '2000.00', '2000.02'])
        result = await cycle(db, tmp_path, broker, []).run(
            'synthetic-unstable', started_at=NOW, trading_date=NOW.date())
        assert result.orders_submitted == 0
        assert result.status == 'BLOCKED'
        assert not KillSwitchController(db).get().engaged
