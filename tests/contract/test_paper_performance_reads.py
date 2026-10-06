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
