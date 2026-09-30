from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.learner.inherited import InheritedBaseline, observe_inherited
from quantbot.learner.paper_deadlines import PaperDeadline
from quantbot.storage import StorageRepository
from tests.integration.test_execution_coordinator import _long_position
from tests.unit.learner.test_paper_runner import context as _context


@pytest.fixture(name='context')
def inherited_context(tmp_path, request):
    yield from _context.__wrapped__(tmp_path, request)


def baseline(runner, positions):
    return InheritedBaseline(
        account_id_hash=PaperDeadline.account_hash('paper-1'),
        captured_at=runner._clock() - timedelta(minutes=1),
        positions=tuple(dict(symbol=p.symbol, quantity=p.quantity,
                             average_entry_price=p.average_entry_price) for p in positions),
    )


def observe(context, captured):
    db, broker, runner, _ = context
    return observe_inherited(db, baseline=captured, positions=broker.positions,
        account_id='paper-1', identity=runner._identity, run_id='entry-run', now=broker.now,
        predictions=runner.predictions, cost_bps=10)


def test_inherited_positions_are_audited_without_orders_or_exit_handoff(context):
    db, broker, runner, _ = context
    broker.positions = tuple(_long_position(Decimal('1')).model_copy(update={'symbol':symbol})
                             for symbol in ('QQQ','TLT'))
    captured = baseline(runner,broker.positions)
    assert observe(context,captured) == {'QQQ','TLT'}
    with db.transaction() as session:
        repo = StorageRepository(session)
        assert repo.list_unresolved_order_intents() == []
        events = repo.list_signals()
        assert not any(e.payload.get('kind') == 'LEARNER_PAPER_DEADLINE_V1' for e in events)
        monitored = [e for e in events if e.payload.get('kind') == 'LEARNER_INHERITED_MONITOR_V1']
        assert {e.symbol for e in monitored} == {'QQQ','TLT'}
        assert all(e.payload['action'] == 'MONITOR' and
                   e.payload['entry_provenance'] == 'INHERITED_NO_LEARNER_ENTRY' for e in monitored)


@pytest.mark.parametrize('change',['quantity','basis','absent'])
def test_changed_or_closed_baseline_is_retired_and_cannot_be_reused(context,change):
    _, broker, runner, _ = context
    original = _long_position(Decimal('1')).model_copy(update={'symbol':'QQQ'})
    broker.positions = (original,)
    captured = baseline(runner,broker.positions)
    assert observe(context,captured) == {'QQQ'}
    broker.now += timedelta(seconds=1)
    broker.positions = () if change == 'absent' else (original.model_copy(update={
        'quantity' if change == 'quantity' else 'average_entry_price':Decimal('2')}),)
    assert observe(context,captured) == set()
    broker.now += timedelta(seconds=1)
    broker.positions = (original,)
    assert observe(context,captured) == set()


def test_baseline_for_another_account_cannot_exempt_positions(context):
    _, broker, runner, _ = context
    captured = baseline(runner,())
    captured = captured.model_copy(update={'account_id_hash': '0'*64})
    with pytest.raises(ValueError,match='account'):
        observe(context,captured)


def test_close_and_same_price_reopen_between_ticks_retires_baseline(context):
    from quantbot.domain import Fill, OrderSide
    from tests.integration.test_execution_coordinator import _accepted, _intent

    db, broker, runner, _ = context
    position = _long_position(Decimal('1')).model_copy(update={'symbol':'QQQ'})
    broker.positions = (position,)
    captured = baseline(runner,broker.positions)
    assert observe(context,captured) == {'QQQ'}
    with db.transaction() as session:
        repo = StorageRepository(session)
        for name,side in [('close',OrderSide.SELL),('reopen',OrderSide.BUY)]:
            intent = _intent().model_copy(update={'intent_id':name,'client_order_id':name,
                'strategy_id':runner._identity.strategy_id,'symbol':'QQQ','side':side,
                'quantity':Decimal('1')})
            repo.create_order_intent(intent)
            repo.save_broker_order(_accepted(intent).model_copy(update={
                'broker_order_id':name,'filled_quantity':Decimal('1'),'status':'filled',
                'filled_average_price':position.average_entry_price}))
            repo.record_fill(Fill(fill_id=name,broker_order_id=name,symbol='QQQ',side=side,
                quantity=Decimal('1'),price=position.average_entry_price,
                occurred_at=broker.now+timedelta(seconds=1)))
    broker.now += timedelta(seconds=2)
    assert observe(context,captured) == set()
