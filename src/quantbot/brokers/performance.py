"""Retained bounded daily equity history; raw broker P/L is never the daily metric."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal


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
        if not isinstance(payload, dict) or payload.get('timeframe') != '1D':
            raise ValueError('daily close history required')
        stamps, equities = payload.get('timestamp'), payload.get('equity')
        if (not isinstance(stamps, list) or not isinstance(equities, list)
                or not 1 <= len(stamps) <= 64 or len(stamps) != len(equities)):
            raise ValueError('complete bounded history arrays required')
        result: dict[datetime, Decimal] = {}
        for stamp, value in zip(stamps, equities, strict=True):
            if type(stamp) is not int or isinstance(value, bool) or value is None:
                raise ValueError('invalid close-history value')
            at = datetime.fromtimestamp(stamp, UTC)
            equity = Decimal(str(value))
            if not self.start <= at <= self.end or at in result or not equity.is_finite():
                raise ValueError('invalid close-history timestamp or equity')
            result[at] = equity
        if list(result) != sorted(result):
            raise ValueError('unordered close history')
        return result

    def equity_at(self, close_at: datetime) -> Decimal:
        try:
            return self.points()[close_at]
        except KeyError:
            raise ValueError('exact exchange-close history point unavailable') from None
