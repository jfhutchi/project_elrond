import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.brokers.base import PaperPerformanceAccount
from quantbot.console.performance_reader import read_performance
from quantbot.domain import Account
from quantbot.operations.close_capture import capture_close, external_flow, initialize
from quantbot.operations.daily_performance import CloseLedger
from quantbot.strategy import XNYSSession


class Broker:
    environment = 'PAPER'
    base_url = 'https://paper-api.alpaca.markets'
    calls = 0
    rows = ()
    snapshot = PaperPerformanceAccount(
        account=Account(account_id='paper-test', equity=Decimal('1000'), cash=Decimal('1000'),
                        buying_power=Decimal('1000'), currency='USD'),
        created_at=datetime(2026, 9, 1, tzinfo=UTC), previous_close_equity=Decimal('1000'))

    async def get_performance_account(self):
        self.calls += 1
        return self.snapshot

    async def get_performance_activities(self, **kwargs):
        self.calls += 1
        return self.rows


class Calendar:
    early = False

    async def get_sessions(self, start, end):
        return tuple(XNYSSession(session_date=d,
                                 open_at=datetime.combine(d, datetime.min.time(), UTC)
                                 + timedelta(hours=13, minutes=30),
                                 close_at=datetime.combine(d, datetime.min.time(), UTC)
                                 + timedelta(hours=17 if self.early else 20))
                     for d in (date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)))


def operational(database, account, at, *, status='RECONCILED'):
    with closing(sqlite3.connect(database)) as c, c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS schema_version(id INT,store_identity TEXT);
        CREATE TABLE IF NOT EXISTS reconciliation_runs(reconciliation_id TEXT,status TEXT,
                                                       completed_at TEXT);
        CREATE TABLE IF NOT EXISTS reconciliation_diffs(reconciliation_id TEXT);
        CREATE TABLE IF NOT EXISTS account_snapshots(account_id TEXT,captured_at TEXT,equity TEXT,
                                                   cash TEXT,currency TEXT);
        DELETE FROM schema_version; DELETE FROM reconciliation_runs; DELETE FROM account_snapshots;
        ''')
        c.execute('INSERT INTO schema_version VALUES(1,?)', ('test-store-lineage',))
        c.execute('INSERT INTO reconciliation_runs VALUES(?,?,?)',
                  ('test-recon', status, at.isoformat()))
        c.execute('INSERT INTO account_snapshots VALUES(?,?,?,?,?)',
                  (account.account.account_id, at.isoformat(), str(account.account.equity),
                   str(account.account.cash), 'USD'))


@pytest.fixture
def setup(tmp_path):
    directory = tmp_path / 'performance'
    database = tmp_path / 'operations.db'
    initialize(directory, database)
    return directory, database, Broker(), Calendar()


async def capture(setup, day=1, **kwargs):
    directory, database, broker, calendar = setup
    at = datetime(2026, 10, day, 17 if calendar.early else 20, 1, tzinfo=UTC)
    operational(database, broker.snapshot, at)
    before = database.read_bytes()
    result = await capture_close(directory, database, broker, calendar,
                                 expected_account_id='paper-test', now=at, **kwargs)
    assert database.read_bytes() == before
    return result


@pytest.mark.asyncio
async def test_real_two_close_path_cash_adjustment_and_production_reader(setup):
    directory, _, broker, _ = setup
    assert (await capture(setup))['status'] == 'MISSING'
    assert CloseLedger(directory / 'closes.db').read_days() == []
    broker.snapshot = broker.snapshot.model_copy(update={
        'account': broker.snapshot.account.model_copy(update={'equity': Decimal('1110'),
                                                             'cash': Decimal('1110')})})
    broker.rows = ({'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '100'},)
    assert (await capture(setup, 2))['status'] == 'FINAL'
    assert CloseLedger(directory / 'closes.db').read_days()[0].net_pnl == 10
    data = read_performance(directory, now=datetime(2026, 10, 2, 20, 2, tzinfo=UTC))
    assert data.available
    assert data.rows[0].net_pnl == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,amount,equity,expected', [
    ('CSW', '-100', '890', '-10'), ('CSD', '100', '1100', '0'),
])
async def test_withdrawal_and_deposit_do_not_become_profit(setup, kind, amount, equity, expected):
    directory, _, broker, _ = setup
    await capture(setup)
    broker.snapshot = broker.snapshot.model_copy(update={
        'account': broker.snapshot.account.model_copy(update={'equity': Decimal(equity),
                                                             'cash': Decimal(equity)})})
    broker.rows = ({'id': 'flow', 'activity_type': kind, 'net_amount': amount},)
    assert (await capture(setup, 2))['status'] == 'FINAL'
    assert CloseLedger(directory / 'closes.db').read_days()[0].net_pnl == Decimal(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['epoch', 'broker_baseline', 'unknown_flow'])
async def test_resets_unverifiable_baseline_or_journal_never_count_as_returns(setup, change):
    directory, _, broker, _ = setup
    await capture(setup)
    if change == 'epoch':
        broker.snapshot = broker.snapshot.model_copy(update={
            'created_at': datetime(2026, 10, 2, tzinfo=UTC)})
    elif change == 'broker_baseline':
        broker.snapshot = broker.snapshot.model_copy(update={
            'previous_close_equity': Decimal('2000')})
    else:
        broker.rows = ({'id': 'ambiguous', 'activity_type': 'JNLC', 'net_amount': '500'},)
    assert (await capture(setup, 2))['status'] == 'INVALID'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
async def test_early_close_not_assumed_to_be_1600(setup):
    directory, _, _, calendar = setup
    calendar.early = True
    await capture(setup)
    assert (await capture(setup, 2))['status'] == 'FINAL'
    data = json.loads((directory / 'calendar.json').read_text())
    assert data['sessions'][1]['close_at'].endswith('17:00:00+00:00')


@pytest.mark.asyncio
async def test_missed_previous_session_does_not_invent_two_day_profit(setup):
    directory, _, _, _ = setup
    assert (await capture(setup, 2))['status'] == 'MISSING'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('minutes,status', [(-1, 'PENDING'), (6, 'INVALID')])
async def test_outside_close_window_never_reads_account_or_activities(setup, minutes, status):
    directory, database, broker, calendar = setup
    now = datetime(2026, 10, 1, 20, tzinfo=UTC) + timedelta(minutes=minutes)
    result = await capture_close(directory, database, broker, calendar,
                                 expected_account_id='paper-test', now=now)
    assert result['status'] == status
    assert broker.calls == 0


@pytest.mark.asyncio
async def test_unreconciled_close_not_recorded_or_finalized(setup):
    directory, database, broker, calendar = setup
    now = datetime(2026, 10, 1, 20, 1, tzinfo=UTC)
    operational(database, broker.snapshot, now, status='RECONCILIATION_REQUIRED')
    result = await capture_close(directory, database, broker, calendar,
                                 expected_account_id='paper-test', now=now)
    assert result['reason'] == 'RECONCILIATION_UNVERIFIED'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
async def test_close_pass_prepares_real_reconciliation_before_equity_capture(setup):
    directory, database, broker, calendar = setup
    at = datetime(2026, 10, 1, 20, 1, tzinfo=UTC)
    operational(database, broker.snapshot, at - timedelta(minutes=6))

    async def prepare():
        operational(database, broker.snapshot, at)

    result = await capture_close(directory, database, broker, calendar,
        expected_account_id='paper-test', now=at, prepare_reconciliation=prepare)
    assert result['status'] == 'MISSING'
    assert result['reason'] == 'BASELINE_MISSING'
    assert broker.calls == 1


@pytest.mark.asyncio
async def test_outside_close_window_does_not_prepare_reconciliation(setup):
    directory, database, broker, calendar = setup

    async def forbidden():
        pytest.fail('no operational writer outside the close window')

    result = await capture_close(directory, database, broker, calendar,
        expected_account_id='paper-test', now=datetime(2026, 10, 1, 19, tzinfo=UTC),
        prepare_reconciliation=forbidden)
    assert result['status'] == 'PENDING'
    assert broker.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('retry_minute', [2, 8])
async def test_capture_survives_crash_before_final_ledger_without_recapture(
    setup, monkeypatch, retry_minute,
):
    directory, database, broker, calendar = setup
    await capture(setup)
    original = CloseLedger.finalize
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic crash')
    monkeypatch.setattr(CloseLedger, 'finalize', fail)
    with pytest.raises(RuntimeError):
        await capture(setup, 2)
    monkeypatch.setattr(CloseLedger, 'finalize', original)
    previous_calls = broker.calls
    result = await capture_close(directory, database, broker, calendar,
                                 expected_account_id='paper-test',
                                 now=datetime(2026, 10, 2, 20, retry_minute, tzinfo=UTC))
    assert result['status'] == 'FINAL'
    assert broker.calls == previous_calls
    assert len(CloseLedger(directory / 'closes.db').read_days()) == 1


@pytest.mark.asyncio
async def test_disagreeing_previous_mark_does_not_claim_account_reset(setup):
    _, _, broker, _ = setup
    await capture(setup)
    broker.snapshot = broker.snapshot.model_copy(update={'previous_close_equity': Decimal('999')})
    result = await capture(setup, 2)
    assert result == {'session_date': '2026-10-02', 'status': 'INVALID',
                      'reason': 'CAPTURE_UNAVAILABLE'}


@pytest.mark.parametrize('rows', [
    [{'id': 'x', 'activity_type': 'CSD', 'net_amount': '-1'}],
    [{'id': 'x', 'activity_type': 'CSW', 'net_amount': '1'}],
    [{'id': 'x', 'activity_type': 'CSD', 'net_amount': 'NaN'}],
    [{'id': 'x', 'activity_type': 'ACATS', 'net_amount': '100'}],
    [{'id': 'x', 'activity_type': 'FILL'}, {'id': 'x', 'activity_type': 'FILL'}],
])
def test_bad_or_ambiguous_external_flows_fail_closed(rows):
    with pytest.raises(ValueError):
        external_flow(rows)


def test_economic_fees_and_income_are_not_external_funding():
    assert external_flow([{'id': '1', 'activity_type': 'FEE', 'net_amount': '-1'},
                          {'id': '2', 'activity_type': 'DIV', 'net_amount': '2'}]) == 0


def test_init_refuses_existing_or_operational_targets(setup):
    directory, database, _, _ = setup
    with pytest.raises(ValueError):
        initialize(directory, database)
    with pytest.raises(ValueError):
        initialize(directory.parent / 'other', directory.parent / 'other/closes.db')
