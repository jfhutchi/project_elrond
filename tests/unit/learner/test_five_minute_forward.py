"""Synthetic forward-only forecasts: no fitting, trading, or fabricated outcomes."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from quantbot.learner.estimators import fit_estimator
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.learner.five_minute_forward import forecast
from quantbot.research.manifest import content_id
from tests.unit.learner.test_five_minute import OPEN, bars, calendar, features


def model():
    return dict(
        contract="five-minute-price-alpha-v1",
        feature_names=list(FEATURE_NAMES),
        artifact=fit_estimator(
            ((0.0,) * 10, (1.0,) * 10), (0.0, 0.0), FEATURE_NAMES, family="mean", alpha=1.0, seed=0
        ).model_dump(mode="json"),
        training_symbols=["DIA"],
        created_at=(OPEN - timedelta(days=1)).isoformat(),
    )


def test_real_point_in_time_forecast_preserves_model_source_and_cost_veto():
    m = model()
    rows = features()
    (p,) = forecast(m, content_id(m), rows, recorded_at=rows[0].decision_at)
    assert p.score == 0
    assert p.cost_status == "BELOW_FIXED_COST_FLOOR"
    assert p.fixed_cost_floor == 0.001
    assert p.evaluation_class == "PROSPECTIVE_LOCAL"
    assert p.model_id == content_id(m)
    assert p.features.source_snapshot_hash == "synthetic"
    assert p.order_authority is False


@pytest.mark.parametrize(
    "change", ["late-model", "wrong-contract", "wrong-roster", "wrong-features", "wrong-hash"]
)
def test_forecast_rejects_unbound_model_or_hindsight(change):
    m = model()
    rows = features()
    if change == "late-model":
        m["created_at"] = (rows[0].decision_at + timedelta(seconds=1)).isoformat()
    if change == "wrong-contract":
        m["contract"] = "minute-alpha-v1"
    if change == "wrong-roster":
        m["training_symbols"] = ["QQQ"]
    if change == "wrong-features":
        m["feature_names"] = ["x"] * 10
    with pytest.raises(ValueError):
        forecast(
            m,
            "a" * 64 if change == "wrong-hash" else content_id(m),
            rows,
            recorded_at=rows[0].decision_at,
        )


def test_historical_rows_and_post_target_recording_are_not_forward_evidence():
    m = model()
    rows = features()
    historic = rows[0].model_copy(update={"evaluation_class": "EXPLORATORY_HISTORICAL"})
    with pytest.raises(ValueError):
        forecast(m, content_id(m), (historic,), recorded_at=rows[0].decision_at)
    with pytest.raises(ValueError):
        forecast(m, content_id(m), rows, recorded_at=rows[0].target_at)


@pytest.mark.parametrize("blocked", ["holdout", "clock"])
async def test_tick_checks_holdout_and_clock_before_provider(tmp_path, monkeypatch, blocked):
    from quantbot.config import Settings
    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.learner import five_minute_forward as worker

    settings = Settings.model_construct(
        TRADING_MODE=TradingMode.PAPER,
        BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
        LIVE_TRADING_ACKNOWLEDGED=False,
    )

    async def refused(*args, **kwargs):
        pytest.fail("provider must not run before actual eligibility checks")

    monkeypatch.setattr(worker, "collect_snapshot", refused)
    receipt = await worker.observe_tick(
        tmp_path,
        settings,
        (model(),),
        guard=lambda *_: blocked != "holdout",
        clock_health=lambda _: blocked != "clock",
        clock=lambda: OPEN + timedelta(minutes=35),
    )
    assert receipt["orders"] == receipt["predictions"] == receipt["outcomes"] == 0
    assert not (tmp_path / "predictions").exists()


async def test_tick_forecast_then_real_elapsed_outcome_and_idempotent_repeat(tmp_path, monkeypatch):
    from quantbot.config import Settings
    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.learner import five_minute_forward as worker
    from quantbot.learner.forward import _read

    settings = Settings.model_construct(
        TRADING_MODE=TradingMode.PAPER,
        BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
        LIVE_TRADING_ACKNOWLEDGED=False,
    )
    now = [OPEN + timedelta(minutes=35, seconds=1)]

    async def capture(*args, **kwargs):
        data = [b for b in bars() if b.timestamp + timedelta(minutes=5) < now[0]]
        return SimpleNamespace(
            inputs=[SimpleNamespace(bar=b) for b in data],
            sessions=calendar(),
            received_at=now[0],
            identity="synthetic-" + now[0].isoformat(),
        )

    monkeypatch.setattr(worker, "collect_snapshot", capture)
    frozen = model()
    arguments = dict(guard=lambda *_: True, clock_health=lambda _: True, clock=lambda: now[0])
    first = await worker.observe_tick(tmp_path, settings, (frozen,), **arguments)
    assert first["predictions"] == 1 and first["outcomes"] == 0
    path = next((tmp_path / "predictions").glob("*/*.json"))
    original = path.read_bytes()
    assert first["target_at"] == (OPEN + timedelta(minutes=65)).isoformat()
    now[0] = OPEN + timedelta(minutes=65, seconds=1)
    second = await worker.observe_tick(tmp_path, settings, (frozen,), **arguments)
    assert second["outcomes"] == 1 and second["predictions"] == 1
    outcome = next((tmp_path / "outcomes").glob("*/*.json"))
    label = _read(outcome)["label"]
    assert label["raw_return"] == pytest.approx(112 / 106 - 1)
    assert _read(outcome)["account_pnl"] is False
    assert path.read_bytes() == original
    third = await worker.observe_tick(tmp_path, settings, (frozen,), **arguments)
    assert third["predictions"] == third["outcomes"] == 0
    assert third["orders"] == 0
    assert frozen == model()


def test_expected_off_boundary_wait_is_distinct_from_missing_panel():
    from quantbot.learner.five_minute_forward import window_wait

    data = bars(7)
    assert (
        window_wait(data, calendar(), OPEN + timedelta(minutes=38))
        == "WAITING_FOR_NEXT_FIVE_MINUTE_BOUNDARY"
    )
    assert (
        window_wait(data[:-1], calendar(), OPEN + timedelta(minutes=38))
        == "INCOMPLETE_FIVE_MINUTE_PANEL"
    )
    assert (
        window_wait(bars(6), calendar(), OPEN + timedelta(minutes=30))
        == "FIVE_MINUTE_LOOKBACK_WARMUP"
    )
