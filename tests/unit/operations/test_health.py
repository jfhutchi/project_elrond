from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.brokers import BrokerHealth
from quantbot.domain import ReconciliationStatus
from quantbot.operations.health import (
    DataFreshness,
    HealthInputs,
    evaluate_operational_health,
)
from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.qualification import QualificationPolicy

NOW = datetime(2026, 8, 14, 21, 5, tzinfo=UTC)


def _broker(healthy: bool = True) -> BrokerHealth:
    return BrokerHealth(
        healthy=healthy,
        provider="alpaca",
        environment="PAPER",
        account_id="paper-1",
        reasons=() if healthy else ("BROKER_DOWN",),
    )


def test_operational_health_is_ready_only_when_every_gate_passes() -> None:
    report = evaluate_operational_health(
        HealthInputs(
            broker=_broker(),
            data=DataFreshness(
                completed_through=NOW - timedelta(seconds=30),
                checked_at=NOW,
                max_age_seconds=60,
            ),
            stream_connected=True,
            risk_healthy=True,
            reconciliation_status=ReconciliationStatus.RECONCILED,
            account_verified=True,
            kill_switch_engaged=False,
        )
    )

    assert report.ready is True
    assert report.reasons == ()
    assert report.safe_summary()["account_verified"] is True


def test_stale_data_stream_and_reconciliation_fail_closed() -> None:
    report = evaluate_operational_health(
        HealthInputs(
            broker=_broker(),
            data=DataFreshness(
                completed_through=NOW - timedelta(seconds=61),
                checked_at=NOW,
                max_age_seconds=60,
            ),
            stream_connected=False,
            risk_healthy=True,
            reconciliation_status=ReconciliationStatus.RECONCILIATION_REQUIRED,
            account_verified=True,
            kill_switch_engaged=False,
        )
    )

    assert report.ready is False
    assert {"MARKET_DATA_STALE", "TRADE_STREAM_DISCONNECTED", "RECONCILIATION_REQUIRED"} <= set(
        report.reasons
    )


def test_prometheus_metrics_are_deterministic_and_have_no_labels() -> None:
    metrics = OperationalMetrics()
    metrics.increment("quantbot_process_restarts_total")
    metrics.increment("quantbot_cycles_total", Decimal("2"))
    metrics.set_gauge("quantbot_market_data_age_seconds", Decimal("12.5"))

    rendered = metrics.render_prometheus()

    assert rendered == (
        "# TYPE quantbot_cycles_total counter\n"
        "quantbot_cycles_total 2\n"
        "# TYPE quantbot_process_restarts_total counter\n"
        "quantbot_process_restarts_total 1\n"
        "# TYPE quantbot_market_data_age_seconds gauge\n"
        "quantbot_market_data_age_seconds 12.5\n"
    )
    assert "api_key" not in rendered
    assert "{" not in rendered


def test_qualification_policy_requires_the_user_specified_trade_sample() -> None:
    assert QualificationPolicy().minimum_meaningful_trades == 30

    with pytest.raises(ValueError):
        QualificationPolicy(minimum_meaningful_trades=29)
