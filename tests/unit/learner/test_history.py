"""Synthetic replay views: no fitting new models or writing learner evidence."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from quantbot.learner.estimators import fit_estimator
from quantbot.learner.history import build_history_report
from quantbot.research.registry import DataRole
from tests.unit.learner.test_economics import panel


def fixture():
    cal, rows, inputs = panel()
    labels = [
        SimpleNamespace(
            features=SimpleNamespace(
                values=(1.0,), symbol=r.symbol, as_of=r.as_of, target_at=r.target_at
            ),
            identity=r.prediction_id,
            raw_return=r.raw_return,
            benchmark_return=r.benchmark_return,
            excess_return=r.excess_return,
            role=DataRole.DISCOVERY,
        )
        for r in rows
    ]
    model = SimpleNamespace(
        model_id="f" * 64,
        artifact=fit_estimator(
            ((1.0,), (2.0,)), (0.0, 0.0), ("x",), family="mean", alpha=1.0, seed=0
        ),
    )
    return model, labels, inputs, cal


def test_training_is_explicitly_in_sample_with_real_costs_and_matching_benchmark():
    model, labels, inputs, cal = fixture()
    report = build_history_report(model, labels, inputs, cal, role="TRAINING_IN_SAMPLE")
    assert report["view_role"] == "TRAINING_IN_SAMPLE"
    assert report["promotion"] == "NONE"
    assert report["period_count"] == 2
    assert report["raw_cohorts"] == 3
    assert report["portfolios"]["top_quintile"] == report["portfolios"]["all_universe"]
    assert report["costs"]["slippage_bps_per_side"] == "5"
    assert report["source_evidence"] == "LOCAL_DECLARED"


def test_no_validation_relabeling_or_protected_dates():
    model, labels, inputs, cal = fixture()
    with pytest.raises(ValueError, match="role"):
        build_history_report(model, labels, inputs, cal, role="VALIDATION_REUSED")
    labels[0].features.target_at = datetime(2026, 4, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="protected"):
        build_history_report(
            model,
            labels,
            inputs,
            cal,
            role="TRAINING_IN_SAMPLE",
            protected_start="2026-04-01",
            protected_end="2026-09-30",
        )
