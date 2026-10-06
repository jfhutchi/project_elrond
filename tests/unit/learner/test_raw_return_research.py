"""Synthetic target separation; no active trading model or broker access."""

from quantbot.learner import adaptive_research as study
from quantbot.learner.forward import _read
from tests.unit.learner.test_adaptive_research import CUTOFF, NOW, START, label


def test_raw_return_target_is_separately_versioned_and_fits_only_two(tmp_path):
    training = tuple(
        label(START).model_copy(
            update={
                "raw_return": value,
                "benchmark_return": value,
                "excess_return": 0,
                "available_at": label(START).available_at.replace(minute=minute + 30),
                "features": label(START).features.model_copy(
                    update={
                        "symbol": "DIA",
                        "decision_at": START.replace(minute=minute),
                        "feature_at": START.replace(minute=minute),
                        "target_at": label(START).features.target_at.replace(minute=minute + 30),
                        "receipt_at": START.replace(minute=minute),
                    }
                ),
            }
        )
        for minute, value in ((0, 0.002), (10, 0.003))
    )
    validation = (label(NOW.replace(hour=16, minute=0)),)
    charges = []
    result = study.fit_shadow_challengers(
        root=tmp_path / "raw",
        training=training,
        validation=validation,
        cutoff=CUTOFF,
        now=NOW,
        guard=lambda *_: True,
        charge=charges.append,
        target="raw_return",
    )
    assert len(charges) == 2 and result["engine_calls"] == 2
    for candidate in result["candidates"]:
        assert candidate["model_target"] == "raw_return"
        assert candidate["target_contract"] == "five-minute-raw-dia-return-shadow-v1"
        assert candidate["artifact"]["intercept"] == 0.0025
        assert candidate["order_authority"] is False
    assert _read(tmp_path / "raw/spec.json")["model_target"] == "raw_return"
