from datetime import UTC, datetime, timedelta

import pytest

from quantbot.learner.models import EvaluationClass, Prediction


def test_a_shadow_prediction_cannot_be_backdated_past_its_target():
    from quantbot.learner.data import FEATURE_NAMES, FeatureRow

    cutoff = datetime(2020, 1, 1, tzinfo=UTC)
    features = FeatureRow(
        as_of=cutoff,
        input_start_at=cutoff - timedelta(days=90),
        target_at=cutoff + timedelta(days=7),
        symbol="AAA",
        universe_id="fixture",
        source_id="fixture",
        source_hash="a" * 64,
        base_close=100,
        benchmark_close=100,
        calendar_hash="c" * 64,
        asof_session_index=60,
        target_session_index=65,
        values=tuple(0.0 for _ in FEATURE_NAMES),
    )
    with pytest.raises(ValueError, match="before.*target"):
        Prediction(
            prediction_id="p",
            model_id="m",
            features=features,
            evaluation_class=EvaluationClass.SHADOW,
            created_at=cutoff + timedelta(days=8),
            score=0.0,
            rank=1,
            percentile=1.0,
            cohort_id="c",
        )
