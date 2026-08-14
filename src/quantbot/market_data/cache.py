"""Durable cache for validated market-data observations."""

from __future__ import annotations

from quantbot.domain import Bar
from quantbot.market_data.base import AdjustmentMode, MarketDataFeed
from quantbot.market_data.validation import BarValidationReport
from quantbot.storage import StorageRepository


class MarketDataCache:
    """Persist only validated bars with feed and adjustment provenance."""

    def __init__(self, repository: StorageRepository) -> None:
        self._repository = repository

    @staticmethod
    def provider_key(
        provider: str,
        feed: MarketDataFeed,
        adjustment: AdjustmentMode,
        timeframe: str,
    ) -> str:
        normalized = provider.strip().lower()
        if not normalized or normalized != provider:
            raise ValueError("provider must be normalized lowercase text")
        normalized_timeframe = timeframe.strip().lower()
        if not normalized_timeframe or timeframe != timeframe.strip():
            raise ValueError("timeframe must not be blank or padded")
        return f"{normalized}:{feed.value}:{adjustment.value}:{normalized_timeframe}"

    def store(self, report: BarValidationReport) -> int:
        key = self.provider_key(
            report.provider,
            report.feed,
            report.adjustment,
            report.timeframe,
        )
        return self._repository.save_bars(
            report.valid_bars,
            provider=key,
            adjustment_metadata={
                "provider": report.provider,
                "feed": report.feed.value,
                "adjustment": report.adjustment.value,
                "timeframe": report.timeframe,
            },
        )

    def load(
        self,
        *,
        symbol: str,
        provider: str,
        feed: MarketDataFeed,
        adjustment: AdjustmentMode,
        timeframe: str,
    ) -> list[Bar]:
        key = self.provider_key(provider, feed, adjustment, timeframe)
        return self._repository.list_bars(symbol=symbol, provider=key)
