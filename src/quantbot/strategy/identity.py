"""Canonical strategy and market-input identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from quantbot.domain import Bar, StrategyIdentity
from quantbot.strategy.config import StrategyComponents, StrategyConfig


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
    """Serialize a strategy config with semantic Decimal normalization.

    Fields added after a version shipped are omitted while they hold their defaults, because
    those defaults reproduce the behaviour the running versions already had. Omitting them
    keeps deployed identities byte-identical, so adding a capability cannot silently
    re-version a strategy that is already live, while a configuration that actually turns the
    capability on does get a new identity.
    """
    payload = config.model_dump(mode="python")
    if payload.get("components") == StrategyComponents().model_dump(mode="python"):
        payload.pop("components", None)
    # Same reasoning for volatility targeting, added after 1.2.0 was already running. A
    # disabled target reproduces pre-1.3.0 behaviour exactly, so omitting it at the default
    # keeps those identities byte-identical; enabling it does produce a new identity.
    if payload.get("volatility_target_bps") == 0:
        payload.pop("volatility_target_bps", None)
        payload.pop("volatility_lookback_days", None)
    return _canonical_json(payload)


def configuration_hash(config: StrategyConfig) -> str:
    """Return the SHA256 of canonical strategy configuration."""
    return hashlib.sha256(canonical_configuration(config).encode("utf-8")).hexdigest()


def strategy_id_for(config: StrategyConfig) -> str:
    """Return the exact strategy identifier implied by a configuration."""
    digest = configuration_hash(config)
    major_version = config.version.split(".", 1)[0]
    return f"{config.strategy_name}-v{major_version}-{digest[:16]}"


def validate_strategy_identity(config: StrategyConfig, identity: StrategyIdentity) -> None:
    """Reject a deployment identity that is not bound to the supplied config."""
    digest = configuration_hash(config)
    if (
        identity.configuration_hash != digest
        or identity.version != config.version
        or identity.strategy_id != strategy_id_for(config)
    ):
        raise ValueError("strategy identity does not match configuration")


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
    return StrategyIdentity(
        strategy_id=strategy_id_for(config),
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
    normalized_cutoff = cutoff.astimezone(UTC) if cutoff is not None else None
    records: list[dict[str, str]] = []
    for symbol in sorted(histories):
        for bar in sorted(histories[symbol], key=lambda item: item.timestamp):
            if normalized_cutoff is not None and bar.timestamp > normalized_cutoff:
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
    payload = {
        "cutoff": normalized_cutoff.isoformat() if normalized_cutoff is not None else None,
        "bars": records,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
