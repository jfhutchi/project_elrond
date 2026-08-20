"""Version 2.0.0 lifts the SPY-only benchmark without loosening it for V1.

V1 pinned benchmark and regime to SPY, which makes a non-equity market inexpressible:
evaluate_symbol always requires regime-symbol bars, so a crypto sleeve forced onto SPY would
have to mix a 24/7 calendar with XNYS sessions. These tests pin that 2.0.0 permits a market
to define its own regime, that V1 is unchanged, and that the escape hatch cannot be used to
smuggle a non-SPY regime into an XNYS strategy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantbot.strategy import StrategyConfig, load_strategy_config, strategy_id_for

ROOT = Path(__file__).parents[3]
CRYPTO = ROOT / "config" / "strategy-crypto-v2.yaml"
EQUITY_V1_2 = ROOT / "config" / "strategy-v1-2.yaml"


def _crypto_payload(**updates: Any) -> dict[str, Any]:
    payload = load_strategy_config(CRYPTO).model_dump(mode="python")
    payload.update(updates)
    return payload


def test_crypto_config_loads_on_a_24_7_calendar_with_its_own_regime() -> None:
    config = load_strategy_config(CRYPTO)

    assert config.version == "2.0.0"
    assert config.calendar == "CRYPTO24"
    assert config.regime_symbol == "BTC/USD"
    assert config.benchmark_symbol == "BTC/USD"
    assert "BTC/USD" in config.universe


def test_periods_are_stated_in_calendar_days_not_sessions() -> None:
    """252 sessions is a year on XNYS but eight months on a calendar with no weekends."""
    crypto = load_strategy_config(CRYPTO)
    equity = load_strategy_config(EQUITY_V1_2)

    assert equity.momentum_long == 252
    assert crypto.momentum_long == 365
    assert crypto.momentum_skip > equity.momentum_skip


def test_crypto_risk_budget_is_tighter_than_equities() -> None:
    """Crypto realises several times equity volatility, so equal bps is not equal risk."""
    crypto = load_strategy_config(CRYPTO)
    equity = load_strategy_config(EQUITY_V1_2)

    assert crypto.risk_per_trade_bps < equity.risk_per_trade_bps
    assert crypto.max_position_value_bps < equity.max_position_value_bps
    assert crypto.max_gross_exposure_bps < equity.max_gross_exposure_bps
    assert crypto.slippage_bps > equity.slippage_bps


def test_v1_still_requires_spy() -> None:
    """The escape hatch must not loosen the versions already deployed."""
    payload = load_strategy_config(EQUITY_V1_2).model_dump(mode="python")
    payload["regime_symbol"] = "QQQ"
    payload["benchmark_symbol"] = "QQQ"

    with pytest.raises(ValueError, match="must both be SPY"):
        StrategyConfig.model_validate(payload)


def test_an_xnys_strategy_cannot_use_a_non_spy_regime_even_at_2_0_0() -> None:
    """2.0.0 exists for non-equity calendars, not to reopen the equity benchmark."""
    payload = _crypto_payload(
        calendar="XNYS", regime_symbol="QQQ", benchmark_symbol="QQQ",
        universe=("QQQ", "SPY", "IWM"), roster_size=1,
    )

    with pytest.raises(ValueError, match="must use SPY as its regime symbol"):
        StrategyConfig.model_validate(payload)


def test_crypto_identity_is_distinct_from_every_equity_version() -> None:
    crypto = strategy_id_for(load_strategy_config(CRYPTO))
    equity_ids = {
        strategy_id_for(load_strategy_config(ROOT / "config" / name))
        for name in ("strategy-v1.yaml", "strategy-v1-1.yaml", "strategy-v1-2.yaml")
    }

    assert crypto not in equity_ids
    assert crypto.startswith("adaptive-momentum-v2-")
