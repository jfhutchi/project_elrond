"""Stable external enumerations used by QuantBot domain objects."""

from enum import StrEnum


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
