"""Synthetic broker-close contract tests, never historical account reconstruction."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantbot.operations.daily_performance import CloseLedger
from tests.unit.operations.test_close_capture import Broker, capture
from tests.unit.operations.test_close_capture import setup as setup


class ReferenceBroker(Broker):
    marks = {'2026-09-30': '1000', '2026-10-01': '1000', '2026-10-02': '1010'}
    intervals = ()
    post_close_rows = ()
    omit_prior = False
    intraday = False

    async def get_performance_close_history(self, *, start, end):
        from quantbot.brokers.performance import PortfolioCloseHistory
        self.calls += 1
        points = [end] if self.omit_prior else [start, end]
        if self.intraday:
            from quantbot.brokers.performance import INTRADAY_SCHEMA, close_window_query
            windows = []
            for at in (start, end):
                raw = json.dumps(dict(timeframe='1Min',
                    timestamp=[int(at.timestamp())] if at in points else [],
                    equity=[self.marks[str(at.date())]] if at in points else []))
                windows.append(dict(close_at=at.isoformat(), query=close_window_query(at),
                    response_document=raw, response_hash=hashlib.sha256(raw.encode()).hexdigest()))
            return PortfolioCloseHistory(start, end, json.dumps(dict(
                schema=INTRADAY_SCHEMA, windows=windows)))
        return PortfolioCloseHistory(start, end, json.dumps(dict(
            timeframe='1D', timestamp=[int(at.timestamp()) for at in points],
            equity=[self.marks[str(at.date())] for at in points],
        )))

    async def get_performance_activities(self, *, after, until):
        self.intervals = (*self.intervals, (after, until))
        if after.date() == until.date():
            return self.post_close_rows
        return self.rows


def reference_setup(setup):
    directory, database, _, calendar = setup
    return directory, database, ReferenceBroker(), calendar


@pytest.mark.asyncio
@pytest.mark.parametrize('early', [False, True])
@pytest.mark.parametrize('intraday', [False, True])
async def test_prospective_close_proof_seeds_then_finalizes_exact_close_metric(
        setup, early, intraday):
    context = reference_setup(setup)
    directory, _, broker, calendar = context
    calendar.early = early
    broker.intraday = intraday
    broker.snapshot = broker.snapshot.model_copy(update={
        'account': broker.snapshot.account.model_copy(update={'equity': Decimal('1002')})})
    assert (await capture(context, broker_close_reference=True))['status'] == 'MISSING'
    broker.snapshot = broker.snapshot.model_copy(update={
        'account': broker.snapshot.account.model_copy(update={'equity': Decimal('1015')})})
    result = await capture(context, 2, broker_close_reference=True)
    assert result['status'] == 'FINAL'
    assert CloseLedger(directory / 'closes.db').read_days()[0].net_pnl == Decimal('10')
    assert (datetime(2026, 10, 1, 17 if early else 20, tzinfo=UTC),
            datetime(2026, 10, 2, 17 if early else 20, tzinfo=UTC)) in broker.intervals
    assert len(list((directory / 'close-references').glob('*.json'))) == 2
    before = {path.name: path.read_bytes()
              for path in (directory / 'close-references').glob('*.json')}
    calls = broker.calls
    assert (await capture(context, 2, broker_close_reference=True))['reason'] == 'ALREADY_FINAL'
    assert broker.calls == calls
    assert before == {path.name: path.read_bytes()
                      for path in (directory / 'close-references').glob('*.json')}


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['account_mark', 'history_mark', 'epoch'])
async def test_matching_epoch_cannot_override_mark_disagreement_or_reset(setup, fault):
    context = reference_setup(setup)
    directory, _, broker, _ = context
    await capture(context, broker_close_reference=True)
    if fault == 'account_mark':
        broker.snapshot = broker.snapshot.model_copy(update={
            'previous_close_equity': Decimal('999')})
    elif fault == 'history_mark':
        broker.marks = broker.marks | {'2026-10-01': '999'}
    else:
        broker.snapshot = broker.snapshot.model_copy(update={
            'created_at': datetime(2026, 10, 2, tzinfo=UTC)})
    assert (await capture(context, 2, broker_close_reference=True))['status'] == 'INVALID'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
async def test_legacy_invalid_capture_is_never_repaired_by_history(setup):
    context = reference_setup(setup)
    directory, _, broker, _ = context
    await capture(context)
    broker.snapshot = broker.snapshot.model_copy(update={'previous_close_equity': Decimal('999')})
    await capture(context, 2)
    before = (directory / 'baselines.db').read_bytes()
    assert (await capture(context, 2, broker_close_reference=True))['status'] == 'INVALID'
    assert (directory / 'baselines.db').read_bytes() == before
    assert not (directory / 'close-references').exists()


@pytest.mark.asyncio
async def test_post_close_activity_cannot_claim_reconciled_close(setup):
    context = reference_setup(setup)
    directory, _, broker, _ = context
    broker.post_close_rows = ({'id': 'after-close-fill', 'activity_type': 'FILL'},)
    assert (await capture(context, broker_close_reference=True))['status'] == 'INVALID'
    assert CloseLedger(directory / 'closes.db').read_days() == []
    assert not (directory / 'close-references').exists()


@pytest.mark.asyncio
async def test_response_completion_after_close_window_cannot_finalize(setup):
    context = reference_setup(setup)
    directory, _, _, _ = context
    await capture(context, broker_close_reference=True)
    moments = iter([datetime(2026, 10, 2, 20, 1, tzinfo=UTC),
                    datetime(2026, 10, 2, 20, 1, tzinfo=UTC),
                    datetime(2026, 10, 2, 20, 6, tzinfo=UTC)])
    result = await capture(context, 2, broker_close_reference=True, clock=lambda: next(moments))
    assert result['status'] == 'INVALID'
    assert result['reason'] == 'CLOSE_NOT_CAPTURED'
    assert CloseLedger(directory / 'closes.db').read_days() == []
    assert len(list((directory / 'close-references').glob('*.json'))) == 1


@pytest.mark.asyncio
async def test_tampered_prior_source_fails_without_rewriting_baseline(setup):
    context = reference_setup(setup)
    directory, _, _, _ = context
    await capture(context, broker_close_reference=True)
    receipt = next((directory / 'close-references').glob('*.json'))
    payload = json.loads(receipt.read_text())
    payload['source_hash'] = '0' * 64
    receipt.write_text(json.dumps(payload))
    before = (directory / 'baselines.db').read_bytes()
    with pytest.raises(ValueError, match='unverified broker close mark'):
        await capture(context, 2, broker_close_reference=True)
    assert (directory / 'baselines.db').read_bytes() == before
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
async def test_legacy_baseline_without_reference_cannot_bridge_to_daily_profit(setup):
    context = reference_setup(setup)
    directory, _, _, _ = context
    await capture(context)
    assert (await capture(context, 2, broker_close_reference=True))['status'] == 'MISSING'
    assert CloseLedger(directory / 'closes.db').read_days() == []
    assert len(list((directory / 'close-references').glob('*.json'))) == 1


@pytest.mark.asyncio
async def test_canonical_series_cannot_silently_revert_to_live_marks(setup):
    context = reference_setup(setup)
    directory, _, _, _ = context
    await capture(context, broker_close_reference=True)
    assert (await capture(context, 2))['status'] == 'INVALID'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
async def test_current_point_can_seed_without_inventing_absent_prior_history(setup):
    context = reference_setup(setup)
    directory, _, broker, _ = context
    broker.omit_prior = True
    assert (await capture(context, broker_close_reference=True))['status'] == 'MISSING'
    assert len(list((directory / 'close-references').glob('*.json'))) == 1
    broker.omit_prior = False
    assert (await capture(context, 2, broker_close_reference=True))['status'] == 'FINAL'
    assert CloseLedger(directory / 'closes.db').read_days()[0].net_pnl == Decimal('10')


@pytest.mark.asyncio
async def test_absent_prior_history_cannot_finalize_existing_baseline(setup):
    context = reference_setup(setup)
    directory, _, broker, _ = context
    await capture(context, broker_close_reference=True)
    broker.omit_prior = True
    assert (await capture(context, 2, broker_close_reference=True))['status'] == 'INVALID'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,amount,equity,expected', [
    ('CSD', '100', '1110', '10'), ('CSD', '100', '1100', '0'),
    ('CSW', '-100', '890', '-10'),
])
async def test_exact_close_flows_do_not_count_funding_as_profit(
    setup, kind, amount, equity, expected,
):
    context = reference_setup(setup)
    directory, _, broker, _ = context
    await capture(context, broker_close_reference=True)
    broker.marks = broker.marks | {'2026-10-02': equity}
    broker.rows = ({'id': 'external-cash', 'activity_type': kind, 'net_amount': amount},)
    assert (await capture(context, 2, broker_close_reference=True))['status'] == 'FINAL'
    assert CloseLedger(directory / 'closes.db').read_days()[0].net_pnl == Decimal(expected)


@pytest.mark.asyncio
async def test_cash_flow_source_is_recomputable_and_tampering_is_rejected(setup):
    from quantbot.operations.close_capture import _decode_evidence
    from quantbot.operations.close_reference import evidence_hash, has_reference

    context = reference_setup(setup)
    directory, _, broker, _ = context
    await capture(context, broker_close_reference=True)
    broker.marks = broker.marks | {'2026-10-02': '1110'}
    broker.rows = ({'id': 'deposit', 'activity_type': 'CSD', 'net_amount': '100'},)
    await capture(context, 2, broker_close_reference=True)
    with closing(sqlite3.connect(directory / 'baselines.db')) as connection:
        epoch, document = connection.execute(
            'SELECT epoch,evidence_json FROM paper_close_baselines_v1 '
            'WHERE session_date=?', ('2026-10-02',)).fetchone()
    path = directory / 'close-references' / (evidence_hash(document) + '.json')
    row = json.loads(path.read_text())
    assert json.loads(row['activity_source_document']) == list(broker.rows)
    assert len(row['activity_source_hash']) == 64
    row['activity_source_document'] = row['activity_source_document'].replace('100', '1000')
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match='unverified close activity source'):
        has_reference(directory, _decode_evidence(document), epoch=epoch)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['corrupt', 'missing'])
async def test_interrupted_canonical_finalization_cannot_skip_proof_by_disabling_flag(
    setup, monkeypatch, fault,
):
    context = reference_setup(setup)
    directory, _, _, _ = context
    await capture(context, broker_close_reference=True)
    original = CloseLedger.finalize

    def crash(*args, **kwargs):
        raise RuntimeError('SYNTHETIC_FINALIZATION_INTERRUPTION')

    monkeypatch.setattr(CloseLedger, 'finalize', crash)
    with pytest.raises(RuntimeError):
        await capture(context, 2, broker_close_reference=True)
    monkeypatch.setattr(CloseLedger, 'finalize', original)
    path = next(path for path in (directory / 'close-references').glob('*.json')
                if json.loads(path.read_text())['session_date'] == '2026-10-02')
    if fault == 'corrupt':
        row = json.loads(path.read_text())
        row['source_hash'] = '0' * 64
        path.write_text(json.dumps(row))
    else:
        for receipt in (directory / 'close-references').glob('*.json'):
            receipt.unlink()  # Synthetic missing-file failure, never operational history.
    assert (await capture(context, 2))['status'] == 'INVALID'
    assert CloseLedger(directory / 'closes.db').read_days() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('retry_minute', [2, 8])
async def test_canonical_finalization_recovery_uses_retained_proof_without_recapture(
    setup, monkeypatch, retry_minute,
):
    from quantbot.operations.close_capture import capture_close

    context = reference_setup(setup)
    directory, database, broker, calendar = context
    await capture(context, broker_close_reference=True)
    original = CloseLedger.finalize

    def crash(*args, **kwargs):
        raise RuntimeError('SYNTHETIC_FINALIZATION_INTERRUPTION')

    monkeypatch.setattr(CloseLedger, 'finalize', crash)
    with pytest.raises(RuntimeError):
        await capture(context, 2, broker_close_reference=True)
    monkeypatch.setattr(CloseLedger, 'finalize', original)
    previous_calls = broker.calls
    result = await capture_close(directory, database, broker, calendar,
                                 expected_account_id='paper-test', broker_close_reference=True,
                                 now=datetime(2026, 10, 2, 20, retry_minute, tzinfo=UTC))
    assert result['status'] == 'FINAL'
    assert broker.calls == previous_calls
    assert len(CloseLedger(directory / 'closes.db').read_days()) == 1
