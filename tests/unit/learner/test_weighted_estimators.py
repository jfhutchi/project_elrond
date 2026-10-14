import math

import pytest

from quantbot.learner.estimators import FittedEstimator, fit_estimator, predict


def test_weighted_ridge_uses_normalized_weights_in_every_stage():
    x, y = ((0.0,), (2.0,)), (0.0, 4.0)
    fitted = fit_estimator(x, y, ("x",), sample_weights=(1.0, 2.0))
    assert fitted.sample_weights == pytest.approx((2 / 3, 4 / 3))
    assert fitted.means == pytest.approx((4 / 3,))
    assert fitted.scales == pytest.approx((math.sqrt(8 / 9),))
    assert fitted.intercept == pytest.approx(8 / 3)
    # Weighted standardized sum of squares is n=2; ridge shrinks slope by 2/3.
    assert predict(fitted, x) == pytest.approx((8 / 9, 32 / 9))
    assert fitted.training_mse == pytest.approx(32 / 81)
    assert fitted.algorithm_version == "weighted-ridge-cholesky-v1"
    assert fit_estimator(x, y, ("x",), sample_weights=(10, 20)).content_id == fitted.content_id
    assert (
        FittedEstimator.model_validate_json(fitted.model_dump_json()).content_id
        == fitted.content_id
    )


def test_default_unweighted_document_and_identity_remain_exact():
    fitted = fit_estimator(((0.0,), (2.0,)), (0.0, 2.0), ("x",))
    assert "sample_weights" not in fitted.model_dump(mode="json")
    assert fitted.algorithm_version == "ridge-cholesky-v1"
    assert fitted.coefficients == pytest.approx((2 / 3,))
    assert (
        FittedEstimator.model_validate_json(fitted.model_dump_json()).content_id
        == fitted.content_id
    )


@pytest.mark.parametrize("weights", [(0, 1), (-1, 1), (float("nan"), 1), (1,), (True, 1)])
def test_weighted_fit_refuses_invalid_weights(weights):
    with pytest.raises(ValueError):
        fit_estimator(((0.0,), (2.0,)), (0, 2), ("x",), sample_weights=weights)
