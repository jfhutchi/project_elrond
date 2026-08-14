"""Public market-data API."""

from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.base import (
    AdjustmentMode,
    AsyncMarketDataTransport,
    BarQuery,
    HistoricalMarketDataClient,
    MarketDataBatch,
    MarketDataError,
    MarketDataFeed,
    MarketDataProtocolError,
    MarketDataRequestError,
    MarketDataTransportError,
    MarketSessionClose,
    Sleeper,
    TransportResponse,
)
from quantbot.market_data.cache import MarketDataCache
from quantbot.market_data.stream import (
    AlpacaStockDataStream,
    StreamDisconnected,
    StreamError,
    StreamProtocolError,
    StreamStaleError,
    WebSocketConnection,
    WebSocketConnector,
)
from quantbot.market_data.transports import (
    HttpxMarketDataTransport,
    WebsocketsConnection,
    WebsocketsConnector,
)
from quantbot.market_data.validation import (
    BarValidationReport,
    DataGap,
    DataGapReason,
    MarketDataValidationError,
    align_daily_bars_to_session_closes,
    validate_bar_batch,
)

__all__ = [
    "AdjustmentMode",
    "AlpacaMarketDataClient",
    "AlpacaStockDataStream",
    "AsyncMarketDataTransport",
    "BarQuery",
    "BarValidationReport",
    "DataGap",
    "DataGapReason",
    "HistoricalMarketDataClient",
    "HttpxMarketDataTransport",
    "MarketDataBatch",
    "MarketDataCache",
    "MarketDataError",
    "MarketDataFeed",
    "MarketDataProtocolError",
    "MarketDataRequestError",
    "MarketDataTransportError",
    "MarketDataValidationError",
    "MarketSessionClose",
    "Sleeper",
    "StreamDisconnected",
    "StreamError",
    "StreamProtocolError",
    "StreamStaleError",
    "TransportResponse",
    "WebSocketConnection",
    "WebSocketConnector",
    "WebsocketsConnection",
    "WebsocketsConnector",
    "align_daily_bars_to_session_closes",
    "validate_bar_batch",
]
