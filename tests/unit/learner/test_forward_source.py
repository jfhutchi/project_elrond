from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from quantbot.config import Settings
from quantbot.learner import forward_source
from quantbot.learner.forward_source import collect_snapshot, read_snapshot
from quantbot.market_data.base import MarketDataProtocolError, TransportResponse

NOW = datetime(2026, 9, 21, 15, tzinfo=UTC)


class SyntheticTransport:
    def __init__(self, *, extra_day: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.extra_day = extra_day

    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, str], timeout_seconds: Decimal,
    ) -> TransportResponse:
        self.calls.append((method, url, dict(params)))
        if url.endswith('/calendar'):
            payload: object = [
                {'date': day, 'open': '09:30', 'close': '16:00'}
                for day in ('2026-09-18', '2026-09-21', '2026-09-22')
            ]
        else:
            days = ['2026-09-18'] + (['2026-09-21'] if self.extra_day else [])
            payload = {'bars': {symbol: [
                {'t': day + 'T04:00:00Z', 'o': 100, 'h': 102,
                 'l': 99, 'c': 101, 'v': 1000, 'n': 100, 'vw': 101}
                for day in days
            ] for symbol in ('AAPL', 'SPY')}, 'next_page_token': None}
        return TransportResponse(status_code=200, headers={}, content=json.dumps(payload).encode())


def credentials() -> Settings:
    # Bypass BaseSettings sources: tests must never inspect the operator environment.
    return Settings.model_construct(
        ALPACA_PAPER_API_KEY=SecretStr('synthetic-key'),
        ALPACA_PAPER_API_SECRET=SecretStr('synthetic-secret'),
    )


async def test_capture_completed_bars_and_archive_actual_receipt_time(tmp_path: Path) -> None:
    transport = SyntheticTransport()
    moments = iter([NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    snapshot = await collect_snapshot(
        tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
        transport=transport, clock=lambda: next(moments),
    )
    assert len(snapshot.sessions.sessions) == 3
    assert [item.bar.symbol for item in snapshot.inputs] == ['AAPL', 'SPY']
    assert all(item.bar.timestamp == datetime(2026, 9, 18, 20, tzinfo=UTC)
               for item in snapshot.inputs)
    assert all(item.available_at == NOW + timedelta(seconds=2) for item in snapshot.inputs)
    assert snapshot.received_at == NOW + timedelta(seconds=2)
    assert transport.calls == [
        ('GET', 'https://paper-api.alpaca.markets/v2/calendar',
         {'start': '2026-09-18', 'end': '2026-10-05'}),
        ('GET', 'https://data.alpaca.markets/v2/stocks/bars',
         {'symbols': 'AAPL,SPY', 'timeframe': '1Day', 'start': '2026-09-18T00:00:00Z',
          'end': '2026-09-18T20:00:00Z', 'limit': '10000', 'adjustment': 'all',
          'feed': 'iex', 'sort': 'asc'}),
    ]
    archive = tmp_path / 'market' / 'snapshots' / f'{snapshot.identity}.json'
    content = archive.read_bytes()
    assert hashlib.sha256(content).hexdigest() == snapshot.identity
    assert 'synthetic-key' not in content.decode()
    assert 'synthetic-secret' not in content.decode()
    assert len(snapshot.receipts) == 2
    assert read_snapshot(archive) == snapshot
    archive.write_bytes(content + b' ')
    with pytest.raises(ValueError, match='hash'):
        read_snapshot(archive)


async def test_provider_identity_stays_stable_across_receipt_times(tmp_path: Path) -> None:
    async def capture(offset: int):
        return await collect_snapshot(
            tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
            transport=SyntheticTransport(), clock=lambda: NOW + timedelta(seconds=offset),
        )
    first = await capture(0)
    archived = (tmp_path / 'market' / 'snapshots' / f'{first.identity}.json').read_bytes()
    second = await capture(1)
    assert first.source_id == second.source_id
    assert first.query_hash == second.query_hash
    assert first.identity != second.identity
    assert (tmp_path / 'market' / 'snapshots' / f'{first.identity}.json').read_bytes() == archived


async def test_incomplete_provider_bar_fails_before_archive(tmp_path: Path) -> None:
    with pytest.raises(MarketDataProtocolError, match='outside completed'):
        await collect_snapshot(
            tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
            transport=SyntheticTransport(extra_day=True), clock=lambda: NOW,
        )
    assert not list(tmp_path.rglob('*.json'))


async def test_invalid_request_and_missing_credentials_do_not_fetch(tmp_path: Path) -> None:
    transport = SyntheticTransport()
    for symbols, settings in [(('aapl',), credentials()), (('AAPL',), Settings.model_construct())]:
        with pytest.raises(ValueError):
            await collect_snapshot(
                tmp_path, symbols, date(2026, 9, 18), date(2026, 9, 21), settings,
                transport=transport, clock=lambda: NOW,
            )
    assert transport.calls == []


async def test_archive_never_overwrites_existing_corrupt_capture(tmp_path: Path) -> None:
    async def capture():
        return await collect_snapshot(
            tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
            transport=SyntheticTransport(), clock=lambda: NOW,
        )
    snapshot = await capture()
    archive = tmp_path / 'market' / 'snapshots' / f'{snapshot.identity}.json'
    assert await capture() == snapshot
    archive.write_bytes(b'corrupt existing receipt')
    with pytest.raises(ValueError, match='corruption'):
        await capture()
    assert archive.read_bytes() == b'corrupt existing receipt'
    assert not list(archive.parent.glob('.capture-*'))


async def test_oversized_response_fails_before_parsing_or_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forward_source, 'MAX_RESPONSE_BYTES', 10)
    with pytest.raises(MarketDataProtocolError, match='size limit'):
        await collect_snapshot(
            tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
            transport=SyntheticTransport(), clock=lambda: NOW,
        )
    assert not list(tmp_path.rglob('*.json'))


async def test_infinite_provider_pagination_stops_at_bounded_request_count(tmp_path: Path) -> None:
    class PagedTransport(SyntheticTransport):
        async def request(
            self, method: str, url: str, *, headers: Mapping[str, str],
            params: Mapping[str, str], timeout_seconds: Decimal,
        ) -> TransportResponse:
            response = await super().request(
                method, url, headers=headers, params=params, timeout_seconds=timeout_seconds,
            )
            if url.endswith('/calendar'):
                return response
            payload = json.loads(response.content)
            payload['next_page_token'] = str(len(self.calls))
            return response.model_copy(update={'content': json.dumps(payload).encode()})

    transport = PagedTransport()
    with pytest.raises(MarketDataProtocolError, match='request limit'):
        await collect_snapshot(
            tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
            transport=transport, clock=lambda: NOW,
        )
    assert len(transport.calls) == 64
    assert not list(tmp_path.rglob('*.json'))


class MinuteTransport(SyntheticTransport):
    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, str], timeout_seconds: Decimal,
    ) -> TransportResponse:
        response = await super().request(
            method, url, headers=headers, params=params, timeout_seconds=timeout_seconds,
        )
        if url.endswith('/calendar'):
            return response
        timestamps = [
            '2026-09-18T20:00:00Z',  # At close: next minute is outside regular session.
            '2026-09-21T13:29:00Z',  # Before open.
            '2026-09-21T13:30:00Z',
            '2026-09-21T14:59:00Z',
            '2026-09-21T15:00:00Z',  # Still forming at the capture cutoff.
        ]
        payload = {'bars': {symbol: [
            {'t': timestamp, 'o': 100, 'h': 102, 'l': 99, 'c': 101,
             'v': 1000, 'n': 100, 'vw': 101}
            for timestamp in timestamps
        ] for symbol in ('AAPL', 'SPY')}, 'next_page_token': None}
        return response.model_copy(update={'content': json.dumps(payload).encode()})


async def test_intraday_capture_preserves_completed_regular_minute_timestamps(
    tmp_path: Path,
) -> None:
    transport = MinuteTransport()
    moments = iter([NOW, NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)])
    snapshot = await collect_snapshot(
        tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
        transport=transport, clock=lambda: next(moments), timeframe='1Min',
    )
    assert snapshot.source_id == 'alpaca:iex:raw:1min'
    assert snapshot.query.timeframe == '1Min'
    assert snapshot.query.adjustment.value == 'raw'
    assert [(item.bar.symbol, item.bar.timestamp.isoformat()) for item in snapshot.inputs] == [
        ('AAPL', '2026-09-21T13:30:00+00:00'), ('SPY', '2026-09-21T13:30:00+00:00'),
        ('AAPL', '2026-09-21T14:59:00+00:00'), ('SPY', '2026-09-21T14:59:00+00:00'),
    ]
    assert all(item.available_at > item.bar.timestamp + timedelta(minutes=1)
               for item in snapshot.inputs)
    assert transport.calls[-1][2]['end'] == '2026-09-21T14:59:00Z'
    archive = tmp_path / 'market' / 'snapshots' / f'{snapshot.identity}.json'
    assert read_snapshot(archive) == snapshot


async def test_unsupported_capture_timeframe_does_not_fetch(tmp_path: Path) -> None:
    transport = SyntheticTransport()
    with pytest.raises(ValueError, match='timeframe'):
        await collect_snapshot(
            tmp_path, ('AAPL',), date(2026, 9, 18), date(2026, 9, 21), credentials(),
            transport=transport, clock=lambda: NOW, timeframe='5Min',
        )
    assert transport.calls == []


class EmptyMinuteTransport(SyntheticTransport):
    async def request(
        self, method: str, url: str, *, headers: Mapping[str, str],
        params: Mapping[str, str], timeout_seconds: Decimal,
    ) -> TransportResponse:
        response = await super().request(
            method, url, headers=headers, params=params, timeout_seconds=timeout_seconds,
        )
        if url.endswith('/calendar'):
            sessions = json.loads(response.content)
            sessions = [item for item in sessions if item['date'] >= params['start']]
            return response.model_copy(update={'content': json.dumps(sessions).encode()})
        return response.model_copy(update={'content': b'{"bars":{},"next_page_token":null}'})


@pytest.mark.parametrize('hour', [0, 15])
async def test_weekend_capture_archives_calendar_only_empty_result(
    tmp_path: Path, hour: int,
) -> None:
    transport = EmptyMinuteTransport()
    weekend = datetime(2026, 9, 19, hour, tzinfo=UTC)
    snapshot = await collect_snapshot(
        tmp_path, ('AAPL',), date(2026, 9, 19), date(2026, 9, 19), credentials(),
        transport=transport, clock=lambda: weekend, timeframe='1Min',
    )
    assert snapshot.inputs == ()
    assert snapshot.empty_reason == 'OUTSIDE_FORECAST_SESSION'
    assert len(snapshot.receipts) == 1
    assert len(transport.calls) == 1
    assert snapshot.query.start <= snapshot.query.end
    archive = tmp_path / 'market' / 'snapshots' / f'{snapshot.identity}.json'
    assert read_snapshot(archive) == snapshot


async def test_open_session_empty_provider_response_is_incomplete_data(tmp_path: Path) -> None:
    transport = EmptyMinuteTransport()
    snapshot = await collect_snapshot(
        tmp_path, ('AAPL',), date(2026, 9, 21), date(2026, 9, 21), credentials(),
        transport=transport, clock=lambda: NOW, timeframe='1Min',
    )
    assert snapshot.inputs == ()
    assert snapshot.empty_reason == 'INCOMPLETE_DATA'
    assert len(snapshot.receipts) == 2
    archive = tmp_path / 'market' / 'snapshots' / f'{snapshot.identity}.json'
    assert read_snapshot(archive) == snapshot
