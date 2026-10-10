"""Synthetic raw provider windows; actual day labels are never relabeled as closes."""
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.brokers.performance import PortfolioCloseHistory

START = datetime(2026, 10, 8, 20, tzinfo=UTC)
END = datetime(2026, 10, 9, 20, tzinfo=UTC)


def document(*, missing_prior=False, early=False):
    windows = []
    start, end = (at.replace(hour=17) if early else at for at in (START, END))
    for close, equity in ((start, 1000), (end, 1010)):
        raw = json.dumps(dict(timeframe='1Min',
            timestamp=[int((close-timedelta(minutes=1)).timestamp()), int(close.timestamp())],
            equity=[equity-1, equity]))
        if missing_prior and close == start:
            raw = json.dumps(dict(timeframe='1Min', timestamp=[], equity=[]))
        windows.append(dict(close_at=close.isoformat(),
            query=dict(start=(close-timedelta(minutes=1)).isoformat().replace('+00:00','Z'),
                       end=(close+timedelta(minutes=1)).isoformat().replace('+00:00','Z'),
                       timeframe='1Min', intraday_reporting='market_hours', cashflow_types='ALL'),
            response_document=raw, response_hash=hashlib.sha256(raw.encode()).hexdigest()))
    return start, end, dict(schema='ALPACA_PAPER_INTRADAY_CLOSE_PAIR_V1', windows=windows)


@pytest.mark.parametrize('early', [False, True])
def test_raw_intraday_close_windows_select_only_exact_requested_closes(early):
    start, end, payload = document(early=early)
    history = PortfolioCloseHistory(start, end, json.dumps(payload))
    assert history.points() == {start: Decimal(1000), end: Decimal(1010)}


def test_missing_prior_point_can_seed_current_without_inventing_comparison():
    start, end, payload = document(missing_prior=True)
    history = PortfolioCloseHistory(start, end, json.dumps(payload))
    assert history.points() == {end: Decimal(1010)}


@pytest.mark.parametrize('fault', ['hash', 'query', 'close', 'duplicate', 'daily_label', 'nearest'])
def test_unverified_intraday_source_cannot_become_close_evidence(fault):
    start, end, payload = document()
    window = payload['windows'][-1]
    if fault == 'hash':
        window['response_hash'] = '0'*64
    elif fault == 'query':
        window['query']['intraday_reporting'] = 'extended_hours'
    elif fault == 'close':
        window['close_at'] = start.isoformat()
    elif fault == 'duplicate':
        payload['windows'] = [window, window]
    else:
        raw = json.loads(window['response_document'])
        if fault == 'daily_label':
            raw.update(timeframe='1D', timestamp=[int(end.replace(hour=0).timestamp())],
                       equity=[1010])
        else:
            raw.update(timestamp=[int((end-timedelta(minutes=1)).timestamp())], equity=[1010])
        window['response_document'] = json.dumps(raw)
        window['response_hash'] = hashlib.sha256(window['response_document'].encode()).hexdigest()
    with pytest.raises(ValueError):
        PortfolioCloseHistory(start, end, json.dumps(payload))
