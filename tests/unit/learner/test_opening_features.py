"""Opening inputs use actual premarket bars without changing the regular-session contract."""

from datetime import timedelta

import pytest

from quantbot.learner import five_minute as engine
from tests.unit.learner.test_five_minute import OPEN, bars, calendar


def opening_source():
    return [
        bar.model_copy(update={"timestamp": bar.timestamp - timedelta(minutes=35)})
        for bar in bars(20)
    ]


def opening(source=None, minute=0):
    decision = OPEN + timedelta(minutes=minute)
    return engine.build_opening_features(
        opening_source() if source is None else source,
        calendar(),
        decision_at=decision,
        receipt_at=decision,
        symbols=("DIA",),
        source_snapshot_hash="real-input-fixture",
    )


def test_first_opening_boundary_uses_seven_completed_premarket_bars():
    (row,) = opening()
    assert row.feature_at == OPEN
    assert row.reference_close == 106
    assert row.values[:3] == pytest.approx((106 / 105 - 1, 106 / 103 - 1, 106 / 100 - 1))
    assert (
        engine.build_features(
            opening_source(),
            calendar(),
            decision_at=OPEN,
            receipt_at=OPEN,
            symbols=("DIA",),
            source_snapshot_hash="unchanged-regular-contract",
        )
        == ()
    )


@pytest.mark.parametrize("minute", [-5, 3, 35, 390])
def test_opening_path_rejects_outside_window_or_stale_cutoff(minute):
    assert opening(minute=minute) == ()


@pytest.mark.parametrize("symbol", ["DIA", "SPY"])
def test_missing_premarket_bar_never_pads_or_substitutes_yesterday(symbol):
    source = opening_source()
    source = [
        bar
        for bar in source
        if not (bar.symbol == symbol and bar.timestamp == OPEN - timedelta(minutes=20))
    ]
    source += [
        bar.model_copy(update={"timestamp": bar.timestamp - timedelta(days=1)})
        for bar in opening_source()[:14]
    ]
    assert opening(source) == ()


def test_future_bars_cannot_change_opening_features_and_next_boundaries_progress():
    source = opening_source()
    available = [bar for bar in source if bar.timestamp + timedelta(minutes=5) <= OPEN]
    assert opening(source) == opening(available)
    assert opening(minute=30)[0].feature_at == OPEN + timedelta(minutes=30)
