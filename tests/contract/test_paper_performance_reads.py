from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.brokers import BrokerProtocolError
from tests.contract.test_alpaca_paper_broker import (
    ScriptedBrokerTransport,
    account_payload,
    adapter,
    response,
)

NOW = datetime(2026, 10, 1, 20, 1, tzinfo=UTC)


async def test_close_metadata_uses_existing_paper_get_account():
    transport = ScriptedBrokerTransport([response(200, account_payload(
        created_at='2026-09-01T12:00:00Z', last_equity='100000.00'))])
    account = await adapter(transport).get_performance_account()
    assert account.previous_close_equity == Decimal('100000')
    assert account.created_at == datetime(2026, 9, 1, 12, tzinfo=UTC)
    assert account.account.equity == Decimal('101234.56')
    assert transport.calls[0]['method'] == 'GET'
    assert transport.calls[0]['url'] == 'https://paper-api.alpaca.markets/v2/account'


@pytest.mark.parametrize('updates',[{'last_equity':'NaN'}, {'created_at':'bad'}])
async def test_unverifiable_close_metadata_is_rejected(updates):
    payload = account_payload(created_at='2026-09-01T12:00:00Z',last_equity='100000') | updates
    with pytest.raises((ValueError, BrokerProtocolError)):
        await adapter(ScriptedBrokerTransport([response(200,payload)])).get_performance_account()


async def test_cashflow_read_paginates_all_activity_types_without_order_mutation():
    first = [dict(id=str(i),activity_type='FILL') for i in range(100)]
    last = [dict(id='deposit',activity_type='CSD',net_amount='100')]
    transport = ScriptedBrokerTransport([response(200,first),response(200,last)])
    rows = await adapter(transport).get_performance_activities(
        after=NOW-timedelta(days=1),until=NOW)
    assert len(rows) == 101 and rows[-1]['id'] == 'deposit'
    assert all(c['method'] == 'GET' and
               c['url'] == 'https://paper-api.alpaca.markets/v2/account/activities'
               for c in transport.calls)
    assert transport.calls[1]['params']['page_token'] == '99'
    assert 'activity_types' not in transport.calls[0]['params']


async def test_repeated_full_activity_page_cannot_be_treated_as_complete():
    page = [dict(id=str(i),activity_type='FILL') for i in range(100)]
    transport = ScriptedBrokerTransport([response(200,page),response(200,page)])
    with pytest.raises(BrokerProtocolError,match='repeated'):
        await adapter(transport).get_performance_activities(
            after=NOW-timedelta(days=1),until=NOW)


async def test_close_history_uses_bounded_paper_get_and_retains_source():
    start, end = NOW - timedelta(days=1, minutes=1), NOW - timedelta(minutes=1)
    payloads = [dict(timeframe='1Min', timestamp=[int(at.timestamp())], equity=[value])
                for at, value in ((start, 1000), (end, 1010))]
    transport = ScriptedBrokerTransport([response(200, payload) for payload in payloads])
    method = getattr(adapter(transport), 'get_performance_close_history', None)
    assert callable(method), 'Supported broker-close GET is missing'
    history = await method(start=start, end=end)
    assert history.equity_at(start) == Decimal('1000')
    assert history.equity_at(end) == Decimal('1010')
    assert len(history.source_hash) == 64 and history.source_document
    assert len(transport.calls) == 2
    for call, day in zip(transport.calls, ('2026-09-30', '2026-10-01'), strict=True):
        assert call['method'] == 'GET'
        assert call['url'] == 'https://paper-api.alpaca.markets/v2/account/portfolio/history'
        assert call['params'] == dict(start=f'{day}T19:59:00Z', end=f'{day}T20:01:00Z',
            timeframe='1Min', intraday_reporting='market_hours', cashflow_types='ALL')


@pytest.mark.parametrize('patch', [
    {'timeframe': '1D'}, {'equity': [1000]}, {'equity': [1000, 'NaN']},
    {'timestamp': [1790798400, 1790798400]}, {'timestamp': [True, 1790798400]},
    {'timestamp': [1790798400, 1790971200]},
])
async def test_close_history_rejects_ambiguous_or_incomplete_series(patch):
    payload = dict(timeframe='1Min', timestamp=[1790798340, 1790798400], equity=[1000, 1010])
    method = getattr(adapter(ScriptedBrokerTransport([response(200, payload | patch)])),
                     'get_performance_close_history', None)
    assert callable(method), 'Supported broker-close GET is missing'
    with pytest.raises((ValueError, BrokerProtocolError)):
        await method(start=NOW-timedelta(days=1, minutes=1), end=NOW-timedelta(minutes=1))
