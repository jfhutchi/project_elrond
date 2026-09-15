from datetime import UTC, datetime, timedelta

import pytest

from quantbot.learner.metrics import ScoredRow, evaluate_predictions, spearman


def test_spearman_uses_average_tie_ranks_and_handles_no_skill():
    assert spearman((1, 2, 3), (3, 2, 1)) == pytest.approx(-1)
    assert spearman((1, 1, 2), (1, 1, 3)) == pytest.approx(1)
    assert spearman((1, 1, 1), (1, 2, 3)) is None


def test_known_cross_section_has_correct_ic_spread_and_no_small_sample_claim():
    start = datetime(2020, 1, 1, tzinfo=UTC)
    rows = tuple(
        ScoredRow(
            prediction_id=str(i),
            cohort_id="c",
            symbol=str(i),
            as_of=start,
            target_at=start + timedelta(days=7),
            score=float(i),
            raw_return=i / 100,
            benchmark_return=0.01,
            excess_return=i / 100 - 0.01,
        )
        for i in range(5)
    )
    result = evaluate_predictions(rows, expected_count=5)
    assert result["mean_ic"] == pytest.approx(1)
    assert result["top_quantile_raw_return"] == pytest.approx(0.04)
    assert result["bottom_quantile_raw_return"] == 0
    assert result["top_minus_bottom_excess_spread"] == pytest.approx(0.04)
    assert result["coverage"] == 1.0
    assert result["ic_t_stat"] is None
    assert result["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    assert result["ranking_outcomes"][-1] == {
        "prediction_id": "4",
        "realized_percentile": 0.9,
    }
    partial = evaluate_predictions(rows, expected_count=10)
    assert partial["coverage"] == 0.5
    assert partial["mean_ic"] is None
    assert partial["top_minus_bottom_excess_spread"] is None


def test_metrics_refuse_empty_and_duplicate_or_nonfinite_observations():
    with pytest.raises(ValueError, match="no resolved"):
        evaluate_predictions((), expected_count=0)
    with pytest.raises(ValueError, match="finite"):
        spearman((1, float("nan")), (1, 2))
    start = datetime(2020, 1, 1, tzinfo=UTC)
    row = ScoredRow("id", "cohort", "A", start, start + timedelta(days=5), 1, 0.1, 0, 0.1)
    with pytest.raises(ValueError, match="duplicate prediction identity"):
        evaluate_predictions((row, row), expected_count=2)


@pytest.mark.parametrize("period_count, nonoverlapping", [(30, 6), (95, 19), (100, 20)])
def test_ic_t_stat_requires_twenty_nonoverlapping_periods(period_count, nonoverlapping):
    start = datetime(2020, 1, 1, tzinfo=UTC)
    rows = tuple(
        ScoredRow(
            prediction_id=f"{period}-{symbol}",
            cohort_id=str(period),
            symbol=str(symbol),
            as_of=start + timedelta(days=period),
            target_at=start + timedelta(days=period + 5),
            score=float(symbol),
            raw_return=(-1) ** period * symbol / 100 + 0.001,
            benchmark_return=0.001,
            excess_return=(-1) ** period * symbol / 100,
        )
        for period in range(period_count)
        for symbol in range(5)
    )
    result = evaluate_predictions(rows, expected_count=len(rows))
    assert result["valid_ic_periods"] == period_count
    assert result["nonoverlapping_periods"] == nonoverlapping
    if nonoverlapping < 20:
        assert result["ic_t_stat"] is None
        assert result["evidence_status"] == "INSUFFICIENT_EVIDENCE"
    else:
        assert result["ic_t_stat"] == pytest.approx(0)
        assert result["evidence_status"] == "DESCRIPTIVE_ONLY"
    assert result["positive_expectancy_justified"] is False
