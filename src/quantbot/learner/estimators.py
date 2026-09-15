"""Small deterministic estimators for the bounded learner service (#242).

The fitted artifact contains everything prediction needs.  Ridge coefficients operate on the
saved standardized feature space; callers therefore cannot accidentally recompute scaling from
evaluation data.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

_FAMILIES = frozenset({"mean", "quadratic_ridge", "ridge"})
_RIDGE_VERSION = "ridge-cholesky-v1"
_MEAN_VERSION = "mean-v1"
_QUADRATIC_RIDGE_VERSION = "quadratic-ridge-squares-cholesky-v1"
_ALGORITHM_VERSIONS = {
    "mean": _MEAN_VERSION,
    "quadratic_ridge": _QUADRATIC_RIDGE_VERSION,
    "ridge": _RIDGE_VERSION,
}


class FittedEstimator(BaseModel):
    """Immutable, content-addressed parameters for a fitted numeric estimator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str
    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    coefficients: tuple[float, ...]
    intercept: float
    alpha: float
    training_mse: float
    seed: int
    algorithm_version: str

    @field_validator("family")
    @classmethod
    def validate_family(cls, value: str) -> str:
        if value not in _FAMILIES:
            supported = ", ".join(sorted(_FAMILIES))
            raise ValueError(f"unsupported estimator family {value!r}; expected one of {supported}")
        return value

    @field_validator("feature_names")
    @classmethod
    def validate_feature_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("feature_names must contain at least one feature")
        if any(not name.strip() for name in value):
            raise ValueError("feature names must be non-empty")
        if len(set(value)) != len(value):
            raise ValueError("feature names must be unique")
        return value

    @field_validator("means", "scales", "coefficients")
    @classmethod
    def validate_finite_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("estimator vectors must contain only finite values")
        return value

    @field_validator("intercept", "alpha", "training_mse")
    @classmethod
    def validate_finite_scalar(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("estimator scalars must be finite")
        return value

    @field_validator("algorithm_version")
    @classmethod
    def validate_algorithm_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("algorithm_version must be non-empty")
        return value

    @model_validator(mode="after")
    def validate_dimensions_and_bounds(self) -> Self:
        original_width = len(self.feature_names)
        width = original_width * 2 if self.family == "quadratic_ridge" else original_width
        dimension_message = (
            " must be twice feature_names for quadratic_ridge"
            if self.family == "quadratic_ridge"
            else " length must match feature_names"
        )
        if len(self.means) != width:
            raise ValueError("means" + dimension_message)
        if len(self.scales) != width:
            raise ValueError("scales" + dimension_message)
        if len(self.coefficients) != width:
            raise ValueError("coefficients" + dimension_message)
        if any(scale <= 0.0 for scale in self.scales):
            raise ValueError("scales must be positive")
        if self.alpha <= 0.0:
            raise ValueError("alpha must be positive")
        if self.training_mse < 0.0:
            raise ValueError("training_mse must be non-negative")
        expected_version = _ALGORITHM_VERSIONS[self.family]
        if self.algorithm_version != expected_version:
            raise ValueError(
                f"algorithm_version for {self.family} must be {expected_version!r}"
            )
        return self

    @property
    def content_id(self) -> str:
        """SHA-256 address of the canonical JSON artifact content."""
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finite_float(value: object, *, location: str) -> float:
    if isinstance(value, bool | str | bytes):
        raise ValueError(f"{location} must be numeric")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{location} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{location} must be finite")
    return result


def _validated_matrix(
    x: Sequence[Sequence[float]],
    *,
    expected_width: int,
    allow_empty: bool,
) -> tuple[tuple[float, ...], ...]:
    if not x:
        if allow_empty:
            return ()
        raise ValueError("x must contain at least one row")

    rows: list[tuple[float, ...]] = []
    for row_index, source_row in enumerate(x):
        if isinstance(source_row, str | bytes):
            raise ValueError(f"x row {row_index} must be a numeric sequence")
        try:
            raw_row = tuple(source_row)
        except TypeError as exc:
            raise ValueError(f"x row {row_index} must be a numeric sequence") from exc
        if len(raw_row) != expected_width:
            raise ValueError(
                f"x row {row_index} has width {len(raw_row)}; expected {expected_width}"
            )
        rows.append(
            tuple(
                _finite_float(value, location=f"x[{row_index}][{column_index}]")
                for column_index, value in enumerate(raw_row)
            )
        )
    return tuple(rows)


def _validated_targets(y: Sequence[float], *, expected_length: int) -> tuple[float, ...]:
    if isinstance(y, str | bytes):
        raise ValueError("y must be a numeric sequence")
    if len(y) != expected_length:
        raise ValueError(f"y has length {len(y)}; expected {expected_length}")
    return tuple(
        _finite_float(value, location=f"y[{index}]") for index, value in enumerate(y)
    )


def _validated_feature_names(
    feature_names: tuple[str, ...], *, expected_width: int
) -> tuple[str, ...]:
    try:
        names = tuple(feature_names)
    except TypeError as exc:
        raise ValueError("feature_names must be a sequence") from exc
    if len(names) != expected_width:
        raise ValueError(
            f"feature_names has length {len(names)}; expected {expected_width}"
        )
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("feature names must be non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError("feature names must be unique")
    return names


def _training_scaler(
    rows: tuple[tuple[float, ...], ...],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    count = len(rows)
    width = len(rows[0])
    means = tuple(math.fsum(row[column] for row in rows) / count for column in range(width))
    raw_scales = tuple(
        math.sqrt(math.fsum((row[column] - means[column]) ** 2 for row in rows) / count)
        for column in range(width)
    )
    scales = tuple(scale if scale > 0.0 else 1.0 for scale in raw_scales)
    return means, scales


def _expanded_rows(
    rows: tuple[tuple[float, ...], ...], *, family: str
) -> tuple[tuple[float, ...], ...]:
    if family != "quadratic_ridge":
        return rows
    expanded: list[tuple[float, ...]] = []
    for row_index, row in enumerate(rows):
        squares = tuple(value * value for value in row)
        if any(not math.isfinite(value) for value in squares):
            raise ValueError(
                f"quadratic expansion produced a non-finite value in x row {row_index}"
            )
        expanded.append((*row, *squares))
    return tuple(expanded)


def _cholesky_solve(matrix: list[list[float]], rhs: list[float]) -> tuple[float, ...]:
    """Solve a symmetric positive-definite system through a Cholesky factorization."""
    width = len(rhs)
    lower = [[0.0] * width for _ in range(width)]
    for row in range(width):
        for column in range(row + 1):
            residual = matrix[row][column] - math.fsum(
                lower[row][prior] * lower[column][prior] for prior in range(column)
            )
            if row == column:
                if not math.isfinite(residual) or residual <= 0.0:
                    raise ValueError("ridge normal equations are not positive definite")
                lower[row][column] = math.sqrt(residual)
            else:
                lower[row][column] = residual / lower[column][column]

    forward = [0.0] * width
    for row in range(width):
        residual = rhs[row] - math.fsum(
            lower[row][column] * forward[column] for column in range(row)
        )
        forward[row] = residual / lower[row][row]

    solution = [0.0] * width
    for row in range(width - 1, -1, -1):
        residual = forward[row] - math.fsum(
            lower[column][row] * solution[column] for column in range(row + 1, width)
        )
        solution[row] = residual / lower[row][row]
    if any(not math.isfinite(value) for value in solution):
        raise ValueError("ridge fit produced non-finite coefficients")
    return tuple(solution)


def fit_estimator(
    x: Sequence[Sequence[float]],
    y: Sequence[float],
    feature_names: tuple[str, ...],
    *,
    family: str = "ridge",
    alpha: float = 1.0,
    seed: int = 0,
) -> FittedEstimator:
    """Fit a deterministic mean baseline or fixed regularized numeric estimator."""
    if family not in _FAMILIES:
        supported = ", ".join(sorted(_FAMILIES))
        raise ValueError(f"unsupported estimator family {family!r}; expected one of {supported}")
    fitted_alpha = _finite_float(alpha, location="alpha")
    if fitted_alpha <= 0.0:
        raise ValueError("alpha must be positive")

    if not x:
        raise ValueError("x must contain at least one row")
    try:
        first_row = tuple(x[0])
    except (TypeError, IndexError) as exc:
        raise ValueError("x row 0 must be a numeric sequence") from exc
    if not first_row:
        raise ValueError("x rows must contain at least one feature")
    names = _validated_feature_names(feature_names, expected_width=len(first_row))
    rows = _validated_matrix(x, expected_width=len(names), allow_empty=False)
    targets = _validated_targets(y, expected_length=len(rows))

    model_rows = _expanded_rows(rows, family=family)
    means, scales = _training_scaler(model_rows)
    intercept = math.fsum(targets) / len(targets)

    if family == "mean":
        coefficients = (0.0,) * len(names)
        algorithm_version = _MEAN_VERSION
    else:
        standardized = tuple(
            tuple(
                (row[column] - means[column]) / scales[column]
                for column in range(len(means))
            )
            for row in model_rows
        )
        centered_targets = tuple(target - intercept for target in targets)
        normal_matrix = [
            [
                math.fsum(row[left] * row[right] for row in standardized)
                + (fitted_alpha if left == right else 0.0)
                for right in range(len(means))
            ]
            for left in range(len(means))
        ]
        normal_rhs = [
            math.fsum(
                row[column] * target
                for row, target in zip(standardized, centered_targets, strict=True)
            )
            for column in range(len(means))
        ]
        coefficients = _cholesky_solve(normal_matrix, normal_rhs)
        algorithm_version = _ALGORITHM_VERSIONS[family]

    fitted_values = tuple(
        intercept
        + math.fsum(
            coefficient * ((row[column] - means[column]) / scales[column])
            for column, coefficient in enumerate(coefficients)
        )
        for row in model_rows
    )
    training_mse = math.fsum(
        (actual - fitted) ** 2
        for actual, fitted in zip(targets, fitted_values, strict=True)
    ) / len(targets)

    return FittedEstimator(
        family=family,
        feature_names=names,
        means=means,
        scales=scales,
        coefficients=coefficients,
        intercept=intercept,
        alpha=fitted_alpha,
        training_mse=training_mse,
        seed=seed,
        algorithm_version=algorithm_version,
    )


def predict(
    estimator: FittedEstimator, x: Sequence[Sequence[float]]
) -> tuple[float, ...]:
    """Predict with the scaler and parameters frozen into ``estimator``."""
    rows = _validated_matrix(
        x,
        expected_width=len(estimator.feature_names),
        allow_empty=True,
    )
    model_rows = _expanded_rows(rows, family=estimator.family)
    predictions = tuple(
        estimator.intercept
        + math.fsum(
            coefficient
            * ((row[column] - estimator.means[column]) / estimator.scales[column])
            for column, coefficient in enumerate(estimator.coefficients)
        )
        for row in model_rows
    )
    if any(not math.isfinite(value) for value in predictions):
        raise ValueError("prediction produced a non-finite value")
    return predictions


__all__ = ["FittedEstimator", "fit_estimator", "predict"]
