"""Environment-only configuration and fail-closed trading safety gates."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from quantbot.domain.enums import BrokerEnvironment, BrokerProvider, TradingMode


class Settings(BaseSettings):
    """QuantBot settings loaded from process environment variables only."""

    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        case_sensitive=True,
    )

    BROKER_PROVIDER: BrokerProvider = BrokerProvider.ALPACA
    TRADING_MODE: TradingMode = TradingMode.PAPER
    BROKER_ENVIRONMENT: BrokerEnvironment = BrokerEnvironment.PAPER
    EXPECTED_ACCOUNT_ID: str | None = None

    ALPACA_PAPER_API_KEY: SecretStr | None = Field(default=None, exclude=True, repr=False)
    ALPACA_PAPER_API_SECRET: SecretStr | None = Field(default=None, exclude=True, repr=False)
    ALPACA_LIVE_API_KEY: SecretStr | None = Field(default=None, exclude=True, repr=False)
    ALPACA_LIVE_API_SECRET: SecretStr | None = Field(default=None, exclude=True, repr=False)

    LIVE_TRADING_ACKNOWLEDGED: bool = False
    KILL_SWITCH: bool = True

    MARKET_DATA_PROVIDER: BrokerProvider = BrokerProvider.ALPACA
    MARKET_DATA_HEALTHY: bool = False
    BROKER_HEALTHY: bool = False
    RISK_ENGINE_HEALTHY: bool = False
    RECONCILIATION_SUCCESSFUL: bool = False

    @field_validator("EXPECTED_ACCOUNT_ID", mode="before")
    @classmethod
    def normalize_expected_account_id(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    def safe_summary(self) -> dict[str, str | bool]:
        """Return operational settings with credential values omitted."""
        return {
            "broker_provider": self.BROKER_PROVIDER.value,
            "trading_mode": self.TRADING_MODE.value,
            "broker_environment": self.BROKER_ENVIRONMENT.value,
            "expected_account_configured": self.EXPECTED_ACCOUNT_ID is not None,
            "paper_credentials_configured": _complete_pair(
                self.ALPACA_PAPER_API_KEY,
                self.ALPACA_PAPER_API_SECRET,
            ),
            "live_credentials_configured": _complete_pair(
                self.ALPACA_LIVE_API_KEY,
                self.ALPACA_LIVE_API_SECRET,
            ),
            "live_trading_acknowledged": self.LIVE_TRADING_ACKNOWLEDGED,
            "kill_switch": self.KILL_SWITCH,
            "market_data_provider": self.MARKET_DATA_PROVIDER.value,
            "market_data_healthy": self.MARKET_DATA_HEALTHY,
            "broker_healthy": self.BROKER_HEALTHY,
            "risk_engine_healthy": self.RISK_ENGINE_HEALTHY,
            "reconciliation_successful": self.RECONCILIATION_SUCCESSFUL,
        }


class SafetyValidation(BaseModel):
    """Frozen result of evaluating every new-order safety gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reasons: tuple[str, ...]
    broker_label: str
    environment_label: str


def validate_trading_safety(
    settings: Settings,
    reported_account_id: str | None = None,
) -> SafetyValidation:
    """Evaluate all readiness gates; any failed gate prohibits new orders."""
    reasons: list[str] = []

    if settings.KILL_SWITCH:
        reasons.append("KILL_SWITCH_ENGAGED")
    if not settings.BROKER_HEALTHY:
        reasons.append("BROKER_UNHEALTHY")
    if not settings.MARKET_DATA_HEALTHY:
        reasons.append("MARKET_DATA_UNHEALTHY")
    if not settings.RISK_ENGINE_HEALTHY:
        reasons.append("RISK_ENGINE_UNHEALTHY")
    if not settings.RECONCILIATION_SUCCESSFUL:
        reasons.append("RECONCILIATION_REQUIRED")

    required_environment = BrokerEnvironment(settings.TRADING_MODE.value)
    if settings.BROKER_ENVIRONMENT is not required_environment:
        reasons.append("BROKER_ENVIRONMENT_MISMATCH")

    if settings.TRADING_MODE is TradingMode.LIVE and not settings.LIVE_TRADING_ACKNOWLEDGED:
        reasons.append("LIVE_TRADING_NOT_ACKNOWLEDGED")

    if settings.EXPECTED_ACCOUNT_ID is None:
        reasons.append("EXPECTED_ACCOUNT_ID_MISSING")
    elif reported_account_id is None or not reported_account_id.strip():
        reasons.append("ACCOUNT_VERIFICATION_UNAVAILABLE")
    elif reported_account_id.strip() != settings.EXPECTED_ACCOUNT_ID:
        reasons.append("ACCOUNT_MISMATCH")

    if not _selected_credentials_complete(settings):
        reasons.append("CREDENTIALS_INCOMPLETE")

    return SafetyValidation(
        allowed=not reasons,
        reasons=tuple(reasons),
        broker_label=settings.BROKER_PROVIDER.value,
        environment_label=settings.BROKER_ENVIRONMENT.value,
    )


def _selected_credentials_complete(settings: Settings) -> bool:
    if settings.BROKER_PROVIDER is not BrokerProvider.ALPACA:
        return False
    if settings.TRADING_MODE is TradingMode.LIVE:
        return _complete_pair(settings.ALPACA_LIVE_API_KEY, settings.ALPACA_LIVE_API_SECRET)
    return _complete_pair(settings.ALPACA_PAPER_API_KEY, settings.ALPACA_PAPER_API_SECRET)


def _complete_pair(key: SecretStr | None, secret: SecretStr | None) -> bool:
    return _secret_present(key) and _secret_present(secret)


def _secret_present(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value().strip())
