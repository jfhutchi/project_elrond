"""Intraday point-in-time windows, exact labels, and purged training."""

from datetime import UTC, datetime, timedelta

import pytest

from quantbot.domain import Bar
from quantbot.learner import intraday
from quantbot.strategy import XNYSSession, XNYSSessionSequence

OPEN = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)


def calendar(close_minutes=390):
    return XNYSSessionSequence(
        calendar="XNYS",
        sessions=tuple(
            XNYSSession(
                session_date=(OPEN + timedelta(days=day)).date(),
                open_at=OPEN + timedelta(days=day),
                close_at=OPEN + timedelta(days=day, minutes=close_minutes),
            )
            for day in range(2)
        ),
    )


def bars(count=150):
    return [
        Bar(
            symbol=symbol,
            timestamp=OPEN + timedelta(minutes=i),
            open=str(base + i),
            high=str(base + i),
            low=str(base + i),
            close=str(base + i),
            volume=str(1000 + i),
            adjustment="1",
        )
        for i in range(count)
        for symbol, base in (("AAPL", 100), ("SPY", 200))
    ]


def features(source=None, minute=31, **kwargs):
    decision = OPEN + timedelta(minutes=minute)
    return intraday.build_features(
        bars() if source is None else source,
        calendar(),
        decision_at=decision,
        symbols=("AAPL",),
        source_snapshot_hash="snapshot",
        receipt_at=decision,
        **kwargs,
    )


def test_completed_bar_cutoff_and_hand_calculated_returns():
    (row,) = features()
    assert row.feature_at == OPEN + timedelta(minutes=31)
    assert row.target_at == OPEN + timedelta(minutes=61)
    assert row.reference_close == 130
    assert row.values[:4] == pytest.approx((130 / 129 - 1, 130 / 125 - 1, 130 / 115 - 1, 0.3))
    assert row.values[6] == pytest.approx(130 / 115 - 230 / 215)
    assert len(row.values) == 12
    assert features(bars(31)) == features(bars(150))


def test_missing_minute_or_benchmark_rejects_whole_cohort():
    assert features([b for b in bars() if b.timestamp != OPEN + timedelta(minutes=15)]) == ()
    assert features([b for b in bars() if b.symbol != "SPY"]) == ()


def test_stale_incomplete_and_cross_session_history_cannot_issue():
    assert features(bars(30), minute=30) == ()
    assert features(bars(31), minute=34) == ()
    assert features(bars(31), minute=33)  # Exactly two minutes old remains acceptable.
    assert features(bars(390), minute=361) == ()
    assert (
        intraday.build_features(
            bars(390),
            calendar(180),
            decision_at=OPEN + timedelta(minutes=151),
            receipt_at=OPEN + timedelta(minutes=151),
            symbols=("AAPL",),
            source_snapshot_hash="snapshot",
        )
        == ()
    )


def test_duplicate_minute_and_unavailable_snapshot_fail_closed():
    with pytest.raises(ValueError, match="duplicate"):
        features(bars() + [bars()[0]])
    with pytest.raises(ValueError, match="receipt"):
        intraday.build_features(
            bars(),
            calendar(),
            decision_at=OPEN + timedelta(minutes=31),
            receipt_at=OPEN + timedelta(minutes=32),
            symbols=("AAPL",),
            source_snapshot_hash="snapshot",
        )


def test_exact_target_close_is_required_and_cannot_resolve_early():
    (row,) = features()
    target = OPEN + timedelta(minutes=61)
    kwargs = dict(receipt_at=target, source_snapshot_hash="labels")
    assert (
        intraday.resolve_label(row, bars(), resolved_at=target - timedelta(seconds=1), **kwargs)
        is None
    )
    assert intraday.resolve_label(row, bars(60), resolved_at=target, **kwargs) is None
    label = intraday.resolve_label(row, bars(), resolved_at=target, **kwargs)
    assert label is not None
    assert label.raw_return == pytest.approx(160 / 130 - 1)
    assert label.benchmark_return == pytest.approx(260 / 230 - 1)
    assert label.excess_return == pytest.approx(160 / 130 - 260 / 230)
    assert (
        intraday.resolve_label(
            row,
            bars(),
            resolved_at=target + timedelta(seconds=1),
            receipt_at=target - timedelta(seconds=1),
            source_snapshot_hash="early",
        )
        is None
    )


def historical():
    return intraday.build_historical_labels(
        bars(),
        calendar(),
        decision_times=tuple(OPEN + timedelta(minutes=i) for i in (31, 41, 71, 81, 101)),
        symbols=("AAPL",),
        source_snapshot_hash="historical",
        observed_at=OPEN + timedelta(minutes=150),
    )


def test_training_purges_touching_targets_and_content_binds_lineage():
    labels = historical()
    model = intraday.fit_intraday_model(
        labels,
        validation_start=OPEN + timedelta(minutes=71),
        created_at=OPEN + timedelta(minutes=151),
        code_commit="abc",
        family="mean",
    )
    assert model.training_count == 1  # 41-minute label ends exactly at validation boundary.
    assert model.validation_count == 3
    assert model.artifact.intercept == pytest.approx(labels[0].excess_return)
    assert model.training_end < model.validation_start
    assert model.validation_mse >= 0
    assert all(label.evaluation_class == "EXPLORATORY_HISTORICAL" for label in labels)
    assert model.model_id != model.model_copy(update={"code_commit": "def"}).model_id
    with pytest.raises(ValueError, match="family"):
        intraday.fit_intraday_model(
            labels,
            validation_start=OPEN + timedelta(minutes=71),
            created_at=OPEN + timedelta(minutes=151),
            code_commit="abc",
            family="quadratic_ridge",
        )


def test_forward_prediction_is_bound_to_model_and_rejects_future_training():
    model = intraday.fit_intraday_model(
        historical(),
        validation_start=OPEN + timedelta(minutes=71),
        created_at=OPEN + timedelta(minutes=151),
        code_commit="abc",
    )
    with pytest.raises(ValueError, match="model"):
        intraday.predict_intraday(model, features(), recorded_at=OPEN + timedelta(minutes=31))
    rows = features(bars(200), minute=160)
    (prediction,) = intraday.predict_intraday(
        model, rows, recorded_at=OPEN + timedelta(minutes=160)
    )
    assert prediction.model_id == model.model_id
    assert prediction.features == rows[0]
    assert prediction.prediction_id
    with pytest.raises(ValueError, match="record"):
        intraday.predict_intraday(model, rows, recorded_at=OPEN + timedelta(minutes=190))


def test_training_rejects_duplicate_labels_and_unobserved_labels():
    labels = historical()
    kwargs = dict(
        validation_start=OPEN + timedelta(minutes=71),
        created_at=OPEN + timedelta(minutes=151),
        code_commit="abc",
    )
    with pytest.raises(ValueError, match="duplicate"):
        intraday.fit_intraday_model((*labels, labels[0]), **kwargs)
    kwargs["created_at"] = OPEN + timedelta(minutes=130)
    with pytest.raises(ValueError, match="observed"):
        intraday.fit_intraday_model(labels, **kwargs)


def test_historical_receipt_is_actual_observation_and_cannot_be_forward_evidence():
    labels = historical()
    assert labels[0].features.receipt_at == OPEN + timedelta(minutes=150)
    with pytest.raises(ValueError, match="historical"):
        intraday.IntradayPrediction(
            model_id="model",
            features=labels[0].features,
            recorded_at=OPEN + timedelta(minutes=31),
            score=0,
        )


def test_predict_rejects_mixed_decision_cohort():
    model = intraday.fit_intraday_model(
        historical(),
        validation_start=OPEN + timedelta(minutes=71),
        created_at=OPEN + timedelta(minutes=151),
        code_commit="abc",
    )
    with pytest.raises(ValueError, match="cohort"):
        intraday.predict_intraday(
            model,
            (*features(bars(200), minute=160), *features(bars(200), minute=161)),
            recorded_at=OPEN + timedelta(minutes=161),
        )


def test_model_binds_training_roster_and_rejects_changed_forecast_symbols():
    model = intraday.fit_intraday_model(
        historical(),
        validation_start=OPEN + timedelta(minutes=71),
        created_at=OPEN + timedelta(minutes=151),
        code_commit="abc",
    )
    assert model.training_symbols == ("AAPL",)
    (row,) = features(bars(200), minute=160)
    changed = row.model_copy(update={"symbol": "MSFT"})
    with pytest.raises(ValueError, match="roster"):
        intraday.predict_intraday(model, (changed,), recorded_at=row.decision_at)


def test_training_rejects_incomplete_or_changing_roster():
    labels = historical()
    extra = labels[0].model_copy(
        update={"features": labels[0].features.model_copy(update={"symbol": "MSFT"})}
    )
    with pytest.raises(ValueError, match="roster"):
        intraday.fit_intraday_model(
            (*labels, extra),
            validation_start=OPEN + timedelta(minutes=71),
            created_at=OPEN + timedelta(minutes=151),
            code_commit="abc",
        )


def test_no_partial_cohort_and_no_naive_decision():
    with pytest.raises(ValueError, match="timezone"):
        intraday.build_features(
            bars(),
            calendar(),
            decision_at=datetime(2026, 9, 21, 14, 1),
            receipt_at=OPEN + timedelta(minutes=31),
            symbols=("AAPL",),
            source_snapshot_hash="snapshot",
        )
    assert (
        intraday.build_features(
            bars(),
            calendar(),
            decision_at=OPEN + timedelta(minutes=31),
            receipt_at=OPEN + timedelta(minutes=31),
            symbols=("AAPL", "MSFT"),
            source_snapshot_hash="snapshot",
        )
        == ()
    )
