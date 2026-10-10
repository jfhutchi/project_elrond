"""Retained bounded daily equity history; raw broker P/L is never the daily metric."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

INTRADAY_SCHEMA = 'ALPACA_PAPER_INTRADAY_CLOSE_PAIR_V1'


def close_window_query(close_at: datetime) -> dict[str, str]:
    if (close_at.tzinfo is None or close_at.utcoffset() is None
            or close_at.second or close_at.microsecond):
        raise ValueError('aware minute-aligned exchange close required')
    close_utc = close_at.astimezone(UTC)
    return dict(start=(close_utc-timedelta(minutes=1)).isoformat().replace('+00:00','Z'),
                end=(close_utc+timedelta(minutes=1)).isoformat().replace('+00:00','Z'),
                timeframe='1Min', intraday_reporting='market_hours', cashflow_types='ALL')


def _series_points(payload: object, *, start: datetime, end: datetime, timeframe: str,
                   limit: int, allow_empty: bool = False) -> dict[datetime, Decimal]:
    if not isinstance(payload, dict) or payload.get('timeframe') != timeframe:
        raise ValueError('required close-history timeframe unavailable')
    stamps, equities = payload.get('timestamp'), payload.get('equity')
    if (not isinstance(stamps, list) or not isinstance(equities, list)
            or not (0 if allow_empty else 1) <= len(stamps) <= limit
            or len(stamps) != len(equities)):
        raise ValueError('complete bounded history arrays required')
    result: dict[datetime, Decimal] = {}
    for stamp, value in zip(stamps, equities, strict=True):
        if type(stamp) is not int or isinstance(value, bool) or value is None:
            raise ValueError('invalid close-history value')
        at = datetime.fromtimestamp(stamp, UTC)
        equity = Decimal(str(value))
        if not start <= at <= end or at in result or not equity.is_finite():
            raise ValueError('invalid close-history timestamp or equity')
        if timeframe == '1Min' and stamp % 60:
            raise ValueError('non-minute intraday timestamp')
        result[at] = equity
    if list(result) != sorted(result):
        raise ValueError('unordered close history')
    return result


def intraday_window_equity(window: object, close_at: datetime, *, required: bool) -> Decimal | None:
    if (not isinstance(window, dict)
            or set(window) != {'close_at', 'query', 'response_document', 'response_hash'}
            or window['close_at'] != close_at.isoformat()
            or window['query'] != close_window_query(close_at)
            or not isinstance(window['response_document'], str)
            or hashlib.sha256(window['response_document'].encode()).hexdigest()
            != window['response_hash']):
        raise ValueError('unverified intraday close query/source')
    payload = json.loads(window['response_document'], parse_float=Decimal,
                         object_pairs_hook=unique_object)
    points = _series_points(payload, start=close_at-timedelta(minutes=1),
                            end=close_at+timedelta(minutes=1), timeframe='1Min', limit=3,
                            allow_empty=not required)
    value = points.get(close_at)
    if required and value is None:
        raise ValueError('exact exchange-close history point unavailable')
    return value


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate history field')
        result[key] = value
    return result


def validate_history_interval(start: datetime, end: datetime) -> None:
    if (start.tzinfo is None or end.tzinfo is None
            or start.utcoffset() is None or end.utcoffset() is None
            or not timedelta(0) < end - start <= timedelta(days=14)):
        raise ValueError('bounded aware close-history interval required')


@dataclass(frozen=True)
class PortfolioCloseHistory:
    start: datetime
    end: datetime
    source_document: str

    def __post_init__(self) -> None:
        validate_history_interval(self.start, self.end)
        if len(self.source_document.encode()) > 262144:
            raise ValueError('bounded close-history document required')
        self.points()

    @property
    def source_hash(self) -> str:
        return hashlib.sha256(self.source_document.encode()).hexdigest()

    def points(self) -> dict[datetime, Decimal]:
        payload = json.loads(self.source_document, parse_float=Decimal,
                             object_pairs_hook=unique_object)
        if isinstance(payload, dict) and payload.get('schema') == INTRADAY_SCHEMA:
            if set(payload) != {'schema', 'windows'} or not isinstance(payload['windows'], list):
                raise ValueError('complete intraday close windows required')
            windows = payload['windows']
            if len(windows) != 2:
                raise ValueError('exact previous/current close windows required')
            selected = {}
            for window, close_at in zip(windows, (self.start, self.end), strict=True):
                value = intraday_window_equity(window, close_at, required=close_at == self.end)
                if value is not None:
                    selected[close_at] = value
            return selected
        # Compatibility for immutable pre-existing exact-close receipts only. New GETs
        # collect intraday windows; unsupported midnight daily labels are not remapped.
        return _series_points(payload, start=self.start, end=self.end, timeframe='1D', limit=64)

    def equity_at(self, close_at: datetime) -> Decimal:
        try:
            return self.points()[close_at]
        except KeyError:
            raise ValueError('exact exchange-close history point unavailable') from None
