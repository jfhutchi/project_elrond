from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from quantbot.config import Settings, validate_trading_safety
from quantbot.domain.enums import BrokerEnvironment, BrokerProvider, TradingMode

ENVIRONMENT_FIELDS = (
    "BROKER_PROVIDER",
    "TRADING_MODE",
    "BROKER_ENVIRONMENT",
    "EXPECTED_ACCOUNT_ID",
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_API_SECRET",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_API_SECRET",
    "LIVE_TRADING_ACKNOWLEDGED",
    "KILL_SWITCH",
    "MARKET_DATA_PROVIDER",
    "MARKET_DATA_HEALTHY",
    "BROKER_HEALTHY",
    "RISK_ENGINE_HEALTHY",
    "RECONCILIATION_SUCCESSFUL",
)


@pytest.fixture(autouse=True)
def clear_quantbot_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVIRONMENT_FIELDS:
        monkeypatch.delenv(name, raising=False)


def safe_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "BROKER_PROVIDER": BrokerProvider.ALPACA,
        "TRADING_MODE": TradingMode.PAPER,
        "BROKER_ENVIRONMENT": BrokerEnvironment.PAPER,
        "EXPECTED_ACCOUNT_ID": "paper-account",
        "ALPACA_PAPER_API_KEY": "paper-key",
        "ALPACA_PAPER_API_SECRET": "paper-secret",
        "KILL_SWITCH": False,
        "MARKET_DATA_HEALTHY": True,
        "BROKER_HEALTHY": True,
        "RISK_ENGINE_HEALTHY": True,
        "RECONCILIATION_SUCCESSFUL": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_settings_have_fail_closed_defaults() -> None:
    settings = Settings()

    assert settings.BROKER_PROVIDER is BrokerProvider.ALPACA
    assert settings.TRADING_MODE is TradingMode.PAPER
    assert settings.BROKER_ENVIRONMENT is BrokerEnvironment.PAPER
    assert settings.EXPECTED_ACCOUNT_ID is None
    assert settings.LIVE_TRADING_ACKNOWLEDGED is False
    assert settings.KILL_SWITCH is True
    assert settings.MARKET_DATA_HEALTHY is False
    assert settings.BROKER_HEALTHY is False
    assert settings.RISK_ENGINE_HEALTHY is False
    assert settings.RECONCILIATION_SUCCESSFUL is False


def test_settings_load_environment_and_ignore_unrelated_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BROKER_PROVIDER", "tradier")
    monkeypatch.setenv("TRADING_MODE", "LIVE")
    monkeypatch.setenv("BROKER_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "acct-live")
    monkeypatch.setenv("UNRELATED_APPLICATION_SETTING", "ignored")

    settings = Settings()

    assert settings.BROKER_PROVIDER is BrokerProvider.TRADIER
    assert settings.TRADING_MODE is TradingMode.LIVE
    assert settings.BROKER_ENVIRONMENT is BrokerEnvironment.LIVE
    assert settings.EXPECTED_ACCOUNT_ID == "acct-live"


def test_settings_do_not_load_dotenv_implicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("TRADING_MODE=LIVE\nKILL_SWITCH=false\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.TRADING_MODE is TradingMode.PAPER
    assert settings.KILL_SWITCH is True


def test_settings_secrets_are_excluded_from_repr_dump_and_safe_summary() -> None:
    settings = safe_settings(
        ALPACA_LIVE_API_KEY="live-key",
        ALPACA_LIVE_API_SECRET="live-secret",
    )

    rendered = " ".join(
        (
            repr(settings),
            str(settings),
            repr(settings.model_dump()),
            repr(settings.safe_summary()),
        )
    )

    for raw_secret in ("paper-key", "paper-secret", "live-key", "live-secret"):
        assert raw_secret not in rendered
    assert "ALPACA_PAPER_API_KEY" not in settings.model_dump()
    assert settings.safe_summary()["paper_credentials_configured"] is True


def test_paper_safety_allows_orders_only_when_all_gates_pass() -> None:
    result = validate_trading_safety(safe_settings(), reported_account_id="  paper-account  ")

    assert result.allowed is True
    assert result.reasons == ()
    assert result.broker_label == "alpaca"
    assert result.environment_label == "PAPER"

    with pytest.raises(ValidationError):
        result.allowed = False  # type: ignore[misc]


@pytest.mark.parametrize("reported_account_id", [None, "", "   "])
@pytest.mark.parametrize(
    "mode_overrides",
    [
        {},
        {
            "TRADING_MODE": TradingMode.LIVE,
            "BROKER_ENVIRONMENT": BrokerEnvironment.LIVE,
            "ALPACA_LIVE_API_KEY": "live-key",
            "ALPACA_LIVE_API_SECRET": "live-secret",
            "LIVE_TRADING_ACKNOWLEDGED": True,
        },
    ],
    ids=["paper", "live"],
)
def test_safety_rejects_unavailable_broker_account_verification(
    mode_overrides: dict[str, Any],
    reported_account_id: str | None,
) -> None:
    result = validate_trading_safety(
        safe_settings(**mode_overrides),
        reported_account_id=reported_account_id,
    )

    assert result.allowed is False
    assert "ACCOUNT_VERIFICATION_UNAVAILABLE" in result.reasons


@pytest.mark.parametrize(
    ("overrides", "reported_account_id", "expected_reason"),
    [
        ({"BROKER_HEALTHY": False}, "paper-account", "BROKER_UNHEALTHY"),
        ({"MARKET_DATA_HEALTHY": False}, "paper-account", "MARKET_DATA_UNHEALTHY"),
        ({"RISK_ENGINE_HEALTHY": False}, "paper-account", "RISK_ENGINE_UNHEALTHY"),
        (
            {"RECONCILIATION_SUCCESSFUL": False},
            "paper-account",
            "RECONCILIATION_REQUIRED",
        ),
        ({"KILL_SWITCH": True}, "paper-account", "KILL_SWITCH_ENGAGED"),
        ({"EXPECTED_ACCOUNT_ID": None}, None, "EXPECTED_ACCOUNT_ID_MISSING"),
        ({}, "wrong-account", "ACCOUNT_MISMATCH"),
        (
            {"BROKER_ENVIRONMENT": BrokerEnvironment.LIVE},
            "paper-account",
            "BROKER_ENVIRONMENT_MISMATCH",
        ),
        ({"ALPACA_PAPER_API_SECRET": None}, "paper-account", "CREDENTIALS_INCOMPLETE"),
    ],
)
def test_paper_safety_fails_closed_for_each_gate(
    overrides: dict[str, Any],
    reported_account_id: str | None,
    expected_reason: str,
) -> None:
    result = validate_trading_safety(safe_settings(**overrides), reported_account_id)

    assert result.allowed is False
    assert expected_reason in result.reasons


def test_live_safety_requires_live_acknowledgement_and_live_credentials() -> None:
    settings = safe_settings(
        TRADING_MODE=TradingMode.LIVE,
        BROKER_ENVIRONMENT=BrokerEnvironment.LIVE,
        ALPACA_LIVE_API_KEY="live-key",
        ALPACA_LIVE_API_SECRET="live-secret",
        LIVE_TRADING_ACKNOWLEDGED=True,
    )

    result = validate_trading_safety(settings, reported_account_id="paper-account")

    assert result.allowed is True
    assert result.reasons == ()
    assert result.environment_label == "LIVE"


def test_live_safety_does_not_accept_paper_credentials_or_missing_acknowledgement() -> None:
    settings = safe_settings(
        TRADING_MODE=TradingMode.LIVE,
        BROKER_ENVIRONMENT=BrokerEnvironment.LIVE,
        LIVE_TRADING_ACKNOWLEDGED=False,
    )

    result = validate_trading_safety(settings, reported_account_id="paper-account")

    assert result.allowed is False
    assert "LIVE_TRADING_NOT_ACKNOWLEDGED" in result.reasons
    assert "CREDENTIALS_INCOMPLETE" in result.reasons
    assert "paper-key" not in repr(result.model_dump())
    assert "paper-secret" not in repr(result.model_dump())


def test_non_alpaca_broker_fails_closed_without_provider_credentials() -> None:
    result = validate_trading_safety(
        safe_settings(BROKER_PROVIDER=BrokerProvider.IBKR),
        reported_account_id="paper-account",
    )

    assert result.allowed is False
    assert "CREDENTIALS_INCOMPLETE" in result.reasons
