"""Stable external enumerations used by QuantBot domain objects."""

from enum import StrEnum
from typing import Literal


class BrokerProvider(StrEnum):
    ALPACA = "alpaca"
    TRADIER = "tradier"
    IBKR = "ibkr"


class BrokerEnvironment(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"


class IntentState(StrEnum):
    RISK_APPROVED = "RISK_APPROVED"
    ORDER_CREATED = "ORDER_CREATED"
    SUBMITTING = "SUBMITTING"
    BROKER_ACCEPTED = "BROKER_ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"


class ReconciliationStatus(StrEnum):
    RECONCILED = "RECONCILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"


#: Which trading calendar a strategy and its data are aligned to.
#:
#: XNYS sessions open and close, so evaluation happens at a close and acts at the next open.
#: CRYPTO24 has no close: each UTC day is treated as one session so the same
#: evaluate-at-close, act-at-next-open machinery applies unchanged to a 24/7 market.
TradingCalendar = Literal["XNYS", "CRYPTO24"]
