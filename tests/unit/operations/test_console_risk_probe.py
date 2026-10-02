"""Console clearance uses parsed sizing policy; no broker calls or kill changes."""
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

from quantbot import cli
from quantbot.config import Settings
from quantbot.storage import Database
from tests.unit.operations.test_readiness import FakeBroker, seed


async def test_console_validates_policy_at_request_time_and_fails_closed(tmp_path, monkeypatch):
    settings = Settings(EXPECTED_ACCOUNT_ID="paper-1", ALPACA_PAPER_API_KEY="key",
                        ALPACA_PAPER_API_SECRET="secret")
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    monkeypatch.setattr("quantbot.brokers.AlpacaPaperBrokerAdapter",
                        lambda **kwargs: FakeBroker())
    config = tmp_path / "strategy.yaml"
    original = (Path(__file__).parents[3] / "config/strategy-v1.yaml").read_bytes()
    config.write_bytes(original)
    monkeypatch.setenv("QUANTBOT_CONFIG", str(config))
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "op.db"))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    with Database.create(tmp_path / "op.db") as database:
        now = datetime.now(UTC)
        seed(database, reconciled_at=now, bar_at=now)
        probe = cli._console_readiness_probe(database, StringIO())
        assert probe is not None
        assert (await probe()).ready
        config.write_text("max_position_value_bps: -1")
        assert "RISK_ENGINE_UNHEALTHY" in (await probe()).blocking
        config.unlink()
        assert "RISK_ENGINE_UNHEALTHY" in (await probe()).blocking
        config.write_bytes(original)
        monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "other.db"))
        assert "RISK_ENGINE_UNHEALTHY" in (await probe()).blocking
        monkeypatch.setenv("QUANTBOT_DATABASE_URL", "sqlite:///contradictory.db")
        assert "RISK_ENGINE_UNHEALTHY" in (await probe()).blocking
