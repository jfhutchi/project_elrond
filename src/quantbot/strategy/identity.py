"""Canonical strategy and market-input identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from quantbot.domain import Bar, StrategyIdentity
from quantbot.strategy.config import StrategyConfig


def _decimal_string(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized.quantize(Decimal(1)), "f")
    return format(normalized, "f")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_string(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_configuration(config: StrategyConfig) -> str:
    """Serialize a strategy config with semantic Decimal normalization."""
    return _canonical_json(config.model_dump(mode="python"))


def configuration_hash(config: StrategyConfig) -> str:
    """Return the SHA256 of canonical strategy configuration."""
    return hashlib.sha256(canonical_configuration(config).encode("utf-8")).hexdigest()


def build_strategy_identity(
    config: StrategyConfig,
    *,
    git_commit: str,
    deployment_timestamp: datetime,
) -> StrategyIdentity:
    """Build the persistent identity for one immutable strategy deployment."""
    if deployment_timestamp.tzinfo is None or deployment_timestamp.utcoffset() is None:
        raise ValueError("deployment_timestamp must be timezone-aware")
    digest = configuration_hash(config)
    major_version = config.version.split(".", 1)[0]
    return StrategyIdentity(
        strategy_id=f"{config.strategy_name}-v{major_version}-{digest[:16]}",
        version=config.version,
        git_commit=git_commit,
        configuration_hash=digest,
        deployment_timestamp=deployment_timestamp,
    )


def bar_set_hash(
    histories: Mapping[str, Sequence[Bar]],
    cutoff: datetime | None = None,
) -> str:
    """Hash symbol-sorted, time-sorted bars through an optional inclusive cutoff."""
    if cutoff is not None and (cutoff.tzinfo is None or cutoff.utcoffset() is None):
        raise ValueError("cutoff must be timezone-aware")
    records: list[dict[str, str]] = []
    for symbol in sorted(histories):
        for bar in sorted(histories[symbol], key=lambda item: item.timestamp):
            if cutoff is not None and bar.timestamp > cutoff:
                continue
            records.append(
                {
                    "symbol": bar.symbol,
                    "timestamp": bar.timestamp.isoformat(),
                    "open": _decimal_string(bar.open),
                    "high": _decimal_string(bar.high),
                    "low": _decimal_string(bar.low),
                    "close": _decimal_string(bar.close),
                    "volume": _decimal_string(bar.volume),
                    "adjustment": _decimal_string(bar.adjustment),
                }
            )
    return hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()
