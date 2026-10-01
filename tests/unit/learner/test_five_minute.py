"""Synthetic data: exact five-minute point-in-time contract, no broker or IO."""

from datetime import UTC, datetime, timedelta

import pytest

from quantbot.domain import Bar
from quantbot.learner import five_minute as engine
from quantbot.strategy import XNYSSession, XNYSSessionSequence

OPEN = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)


def calendar(minutes=390):
    return XNYSSessionSequence(
        calendar="XNYS",
        sessions=tuple(
            XNYSSession(
                session_date=(OPEN + timedelta(days=day)).date(),
                open_at=OPEN + timedelta(days=day),
                close_at=OPEN + timedelta(days=day, minutes=minutes),
            )
            for day in range(2)
        ),
    )


def bars(count=20, volume=1):
    return [
        Bar(
            symbol=s,
            timestamp=OPEN + timedelta(minutes=5 * i),
            open=str(base + i),
            high=str(base + i),
            low=str(base + i),
            close=str(base + i),
            volume=str(volume),
            adjustment="1",
        )
        for i in range(count)
        for s, base in (("DIA", 100), ("SPY", 200))
    ]


def features(source=None, minute=35, close=390):
    decision = OPEN + timedelta(minutes=minute)
    return engine.build_features(
        bars() if source is None else source,
        calendar(close),
        decision_at=decision,
        receipt_at=decision,
        symbols=("DIA",),
        source_snapshot_hash="synthetic",
    )


def test_new_contract_completed_cutoff_and_price_only_returns():
    (row,) = features()
    assert row.contract == "five-minute-price-alpha-v1"
    assert row.feature_at == OPEN + timedelta(minutes=35)
    assert row.target_at == OPEN + timedelta(minutes=65)
    assert row.reference_close == 106
    assert row.values[:3] == pytest.approx((106 / 105 - 1, 106 / 103 - 1, 106 / 100 - 1))
    assert len(row.values) == 10
    assert features(bars(volume=900)) == features(bars(volume=1))
    assert features(bars(7)) == features(bars(20))


@pytest.mark.parametrize("minute,count", [(34, 7), (38, 7), (35, 6)])
def test_warmup_staleness_and_missing_completed_bar_reject(minute, count):
    assert features(bars(count), minute=minute) == ()


def test_exact_two_minute_freshness_limit_retained():
    assert features(bars(7), minute=37)


@pytest.mark.parametrize("symbol", ["DIA", "SPY"])
def test_missing_panel_bar_is_not_filled_or_slid_back(symbol):
    assert (
        features(
            [
                b
                for b in bars(7)
                if not (b.symbol == symbol and b.timestamp == OPEN + timedelta(minutes=30))
            ]
        )
        == ()
    )


def test_early_close_horizon_and_market_close_reject():
    assert features(minute=35, close=65)
    assert features(minute=40, close=65) == ()
    assert features(minute=65, close=65) == ()


def test_misaligned_duplicate_or_unavailable_receipt_reject():
    with pytest.raises(ValueError, match="duplicate"):
        features(bars() + [bars()[0]])
    with pytest.raises(ValueError, match="aligned"):
        features([bars()[0].model_copy(update={"timestamp": OPEN + timedelta(minutes=1)})])
    with pytest.raises(ValueError, match="receipt"):
        engine.build_features(
            bars(),
            calendar(),
            decision_at=OPEN + timedelta(minutes=35),
            receipt_at=OPEN + timedelta(minutes=36),
            symbols=("DIA",),
            source_snapshot_hash="synthetic",
        )


def test_exact_elapsed_target_label_requires_real_asset_and_benchmark():
    (row,) = features()
    target = row.target_at
    assert (
        engine.resolve_label(
            row,
            bars(),
            resolved_at=target - timedelta(seconds=1),
            receipt_at=target - timedelta(seconds=1),
            source_snapshot_hash="synthetic-target",
        )
        is None
    )
    label = engine.resolve_label(
        row, bars(), resolved_at=target, receipt_at=target, source_snapshot_hash="synthetic-target"
    )
    assert label.raw_return == pytest.approx(112 / 106 - 1)
    assert label.benchmark_return == pytest.approx(212 / 206 - 1)
    assert label.evaluation_class == "PROSPECTIVE_LOCAL"
    assert (
        engine.resolve_label(
            row,
            [b for b in bars() if b.timestamp != target - timedelta(minutes=5)],
            resolved_at=target,
            receipt_at=target,
            source_snapshot_hash="missing",
        )
        is None
    )


def test_minute_feature_object_cannot_be_used_as_five_minute_label():
    from quantbot.learner.intraday import IntradayFeatureRow

    row = IntradayFeatureRow(
        symbol="DIA",
        decision_at=OPEN + timedelta(minutes=35),
        feature_at=OPEN + timedelta(minutes=35),
        target_at=OPEN + timedelta(minutes=65),
        values=(0.0,) * 12,
        reference_close=100,
        benchmark_close=200,
        source_snapshot_hash="minute",
        receipt_at=OPEN + timedelta(minutes=35),
    )
    with pytest.raises(ValueError, match="contract"):
        engine.resolve_label(
            row,
            bars(),
            resolved_at=row.target_at,
            receipt_at=row.target_at,
            source_snapshot_hash="wrong-contract",
        )
