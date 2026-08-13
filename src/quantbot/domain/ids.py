"""Deterministic identifiers for broker submissions."""

import hashlib
import json
from datetime import date

from quantbot.domain.enums import OrderSide


def build_client_order_id(
    strategy_version: str,
    symbol: str,
    signal_date: date,
    side: OrderSide,
    sequence: int,
) -> str:
    """Build a stable broker-safe identifier from canonical intent fields."""
    if sequence < 0:
        raise ValueError("sequence must be nonnegative")
    if not strategy_version.strip():
        raise ValueError("strategy_version must not be empty")

    normalized_symbol = symbol.strip()
    if not normalized_symbol:
        raise ValueError("symbol must not be empty")
    if normalized_symbol != normalized_symbol.upper():
        raise ValueError("symbol must be uppercase")

    payload = {
        "strategy_version": strategy_version,
        "symbol": normalized_symbol,
        "signal_date": signal_date.isoformat(),
        "side": side.value,
        "sequence": sequence,
    }
    canonical_json = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return f"qb-{digest[:40]}"
