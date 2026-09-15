from __future__ import annotations

import hashlib
import json
import math

import pytest
from pydantic import ValidationError

from quantbot.learner.estimators import FittedEstimator, fit_estimator, predict


def test_ridge_learns_signal_and_changes_predictions() -> None:
    x = ((-2.0, 1.0), (-1.0, -1.0), (0.0, 2.0), (1.0, -2.0), (2.0, 0.0))
    y = tuple(1.0 + 2.0 * row[0] - 3.0 * row[1] for row in x)

    estimator = fit_estimator(x, y, ("momentum", "value"), alpha=0.001, seed=17)
    predictions = predict(estimator, x)
    mean_predictions = predict(
        fit_estimator(x, y, ("momentum", "value"), family="mean"), x
    )

    assert estimator.family == "ridge"
    assert estimator.seed == 17
    assert estimator.coefficients[0] > 0.0
    assert estimator.coefficients[1] < 0.0
    assert predictions[0] != pytest.approx(predictions[-1])
    assert estimator.training_mse < sum(
        (actual - forecast) ** 2 for actual, forecast in zip(y, mean_predictions, strict=True)
    ) / len(y)
    assert predictions == pytest.approx(y, abs=0.01)


def test_mean_family_is_no_skill_training_mean() -> None:
    estimator = fit_estimator(
        ((1.0, 9.0), (2.0, 8.0), (3.0, 7.0)),
        (2.0, 5.0, 8.0),
        ("first", "second"),
        family="mean",
    )

    assert estimator.intercept == 5.0
    assert estimator.coefficients == (0.0, 0.0)
    assert estimator.training_mse == 6.0
    assert predict(estimator, ((1000.0, -1000.0),)) == (5.0,)


def test_ridge_saves_training_scaler_for_future_predictions() -> None:
    estimator = fit_estimator(((0.0,), (2.0,)), (0.0, 2.0), ("signal",), alpha=1.0)

    assert estimator.means == (1.0,)
    assert estimator.scales == (1.0,)
    assert estimator.intercept == 1.0
    assert estimator.coefficients == pytest.approx((2.0 / 3.0,))
    assert predict(estimator, ((1.0,), (4.0,))) == pytest.approx((1.0, 3.0))


def test_regularization_handles_constant_and_collinear_features() -> None:
    estimator = fit_estimator(
        ((1.0, 1.0, 2.0), (1.0, 2.0, 4.0), (1.0, 3.0, 6.0)),
        (1.0, 2.0, 3.0),
        ("constant", "x", "twice_x"),
        alpha=0.5,
    )

    assert estimator.scales[0] == 1.0
    assert estimator.coefficients[0] == 0.0
    assert all(math.isfinite(value) for value in estimator.coefficients)
    assert all(math.isfinite(value) for value in predict(estimator, ((1.0, 4.0, 8.0),)))


def test_quadratic_ridge_generalizes_a_squared_signal_better_than_linear_ridge() -> None:
    x = tuple((float(value),) for value in range(-3, 4))
    y = tuple(1.0 + 0.5 * row[0] + 2.0 * row[0] ** 2 for row in x)
    holdout = ((4.0,),)
    expected = 35.0

    linear = fit_estimator(x, y, ("signal",), family="ridge", alpha=0.001)
    quadratic = fit_estimator(
        x, y, ("signal",), family="quadratic_ridge", alpha=0.001
    )

    linear_error = (predict(linear, holdout)[0] - expected) ** 2
    quadratic_prediction = predict(quadratic, holdout)[0]
    quadratic_error = (quadratic_prediction - expected) ** 2
    assert quadratic.family == "quadratic_ridge"
    assert quadratic.algorithm_version == "quadratic-ridge-squares-cholesky-v1"
    assert len(quadratic.coefficients) == 2
    assert quadratic_error < linear_error / 1000.0
    assert quadratic_prediction == pytest.approx(expected, abs=0.02)


def test_quadratic_ridge_saves_training_scaler_for_raw_and_squared_columns() -> None:
    estimator = fit_estimator(
        ((0.0,), (2.0,)),
        (0.0, 4.0),
        ("signal",),
        family="quadratic_ridge",
        alpha=2.0,
    )

    assert estimator.feature_names == ("signal",)
    assert estimator.means == (1.0, 2.0)
    assert estimator.scales == (1.0, 2.0)
    assert estimator.coefficients == pytest.approx((2.0 / 3.0, 2.0 / 3.0))
    assert predict(estimator, ((4.0,),)) == pytest.approx((26.0 / 3.0,))


def test_quadratic_artifact_requires_two_parameters_per_original_feature() -> None:
    with pytest.raises(ValidationError, match="twice"):
        FittedEstimator(
            family="quadratic_ridge",
            feature_names=("signal",),
            means=(0.0,),
            scales=(1.0,),
            coefficients=(1.0,),
            intercept=0.0,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="quadratic-ridge-squares-cholesky-v1",
        )


def test_quadratic_artifact_round_trips_with_versioned_expansion() -> None:
    estimator = fit_estimator(
        ((-1.0,), (0.0,), (1.0,)),
        (2.0, 0.0, 2.0),
        ("signal",),
        family="quadratic_ridge",
        alpha=0.25,
    )
    reloaded = FittedEstimator.model_validate_json(estimator.model_dump_json())

    assert reloaded == estimator
    assert reloaded.content_id == estimator.content_id
    assert predict(reloaded, ((2.0,),)) == predict(estimator, ((2.0,),))


def test_artifact_rejects_an_algorithm_version_for_another_family() -> None:
    with pytest.raises(ValidationError, match="algorithm_version"):
        FittedEstimator(
            family="quadratic_ridge",
            feature_names=("signal",),
            means=(0.0, 0.0),
            scales=(1.0, 1.0),
            coefficients=(1.0, 1.0),
            intercept=0.0,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="ridge-cholesky-v1",
        )


def test_fit_is_deterministic_and_artifact_round_trips() -> None:
    args = (((0.0,), (1.0,), (2.0,)), (1.0, 3.0, 5.0), ("signal",))

    first = fit_estimator(*args, alpha=0.25, seed=42)
    second = fit_estimator(*args, alpha=0.25, seed=42)
    reloaded = FittedEstimator.model_validate_json(first.model_dump_json())

    assert first == second == reloaded
    assert first.content_id == second.content_id == reloaded.content_id
    assert predict(first, ((3.0,),)) == predict(reloaded, ((3.0,),))


def test_content_id_hashes_canonical_artifact_content() -> None:
    estimator = fit_estimator(((0.0,), (1.0,)), (0.0, 1.0), ("signal",), alpha=0.5)
    canonical = json.dumps(
        estimator.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert estimator.content_id == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    changed = estimator.model_copy(update={"intercept": estimator.intercept + 1.0})
    assert changed.content_id != estimator.content_id


def test_artifact_normalizes_sequences_to_deeply_immutable_tuples() -> None:
    estimator = FittedEstimator(
        family="ridge",
        feature_names=["signal"],
        means=[0.0],
        scales=[1.0],
        coefficients=[2.0],
        intercept=1.0,
        alpha=1.0,
        training_mse=0.0,
        seed=0,
        algorithm_version="ridge-cholesky-v1",
    )

    assert isinstance(estimator.feature_names, tuple)
    assert isinstance(estimator.means, tuple)
    assert isinstance(estimator.scales, tuple)
    assert isinstance(estimator.coefficients, tuple)
    with pytest.raises(ValidationError):
        estimator.intercept = 3.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("x", "y", "names", "kwargs"),
    [
        ((), (), ("signal",), {}),
        (((1.0,),), (), ("signal",), {}),
        (((1.0,), (2.0,)), (1.0,), ("signal",), {}),
        (((1.0,), (2.0, 3.0)), (1.0, 2.0), ("signal",), {}),
        (((1.0,),), (1.0,), ("one", "two"), {}),
        (((float("nan"),),), (1.0,), ("signal",), {}),
        (((float("inf"),),), (1.0,), ("signal",), {}),
        (((1.0,),), (float("nan"),), ("signal",), {}),
        (((1.0,),), (float("inf"),), ("signal",), {}),
        (((1.0,),), (1.0,), ("signal",), {"alpha": 0.0}),
        (((1.0,),), (1.0,), ("signal",), {"alpha": -1.0}),
        (((1.0,),), (1.0,), ("signal",), {"family": "lasso"}),
        (((1.0,),), (1.0,), ("",), {}),
        (((1.0, 2.0),), (1.0,), ("same", "same"), {}),
    ],
)
def test_fit_rejects_malformed_or_non_finite_inputs(
    x: tuple[tuple[float, ...], ...],
    y: tuple[float, ...],
    names: tuple[str, ...],
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        fit_estimator(x, y, names, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "x",
    [(), ((1.0, 2.0),), ((float("nan"),),), ((float("inf"),),)],
)
def test_predict_rejects_malformed_or_non_finite_inputs(
    x: tuple[tuple[float, ...], ...],
) -> None:
    estimator = fit_estimator(((0.0,), (1.0,)), (0.0, 1.0), ("signal",))

    if not x:
        assert predict(estimator, x) == ()
    else:
        with pytest.raises(ValueError):
            predict(estimator, x)


@pytest.mark.parametrize(
    "updates",
    [
        {"family": "lasso"},
        {"means": (float("nan"),)},
        {"scales": (0.0,)},
        {"coefficients": (1.0, 2.0)},
        {"intercept": float("inf")},
        {"alpha": 0.0},
        {"training_mse": -1.0},
        {"feature_names": ("signal", "signal")},
    ],
)
def test_artifact_rejects_invalid_persisted_content(updates: dict[str, object]) -> None:
    valid: dict[str, object] = {
        "family": "ridge",
        "feature_names": ("signal",),
        "means": (0.0,),
        "scales": (1.0,),
        "coefficients": (1.0,),
        "intercept": 0.0,
        "alpha": 1.0,
        "training_mse": 0.0,
        "seed": 0,
        "algorithm_version": "ridge-cholesky-v1",
    }
    valid.update(updates)

    with pytest.raises(ValidationError):
        FittedEstimator.model_validate(valid)
