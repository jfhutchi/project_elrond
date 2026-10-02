"""Descriptive views of already-frozen models; no fits, forecasts or store writes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from quantbot.learner.data import DailyInput, LabelRow
from quantbot.learner.economics import simulate
from quantbot.learner.estimators import predict
from quantbot.learner.metrics import ScoredRow
from quantbot.learner.models import ModelVersion
from quantbot.research.manifest import content_id
from quantbot.research.registry import DataRole
from quantbot.strategy import XNYSSessionSequence

Role = Literal["TRAINING_IN_SAMPLE", "VALIDATION_REUSED"]


def build_history_report(
    model: ModelVersion,
    labels: Sequence[LabelRow],
    inputs: Sequence[DailyInput],
    calendar: XNYSSessionSequence,
    *,
    role: Role,
    month: str | None = None,
    protected_start: str | None = None,
    protected_end: str | None = None,
) -> dict[str, Any]:
    expected = DataRole.DISCOVERY if role == "TRAINING_IN_SAMPLE" else DataRole.VALIDATION
    if role not in ("TRAINING_IN_SAMPLE", "VALIDATION_REUSED") or any(
        label.role is not expected for label in labels
    ):
        raise ValueError("history role must match existing dataset rows")
    if (protected_start is None) != (protected_end is None):
        raise ValueError("complete protected window required")
    if protected_start is not None and protected_end is not None:
        start, end = date.fromisoformat(protected_start), date.fromisoformat(protected_end)
        if any(
            label.features.as_of.date() <= end and label.features.target_at.date() >= start
            for label in labels
        ):
            raise ValueError("protected rows cannot be replayed")
    if month is not None:
        date.fromisoformat(month + "-01")
        labels = tuple(label for label in labels if label.features.as_of.strftime("%Y-%m") == month)
    if not labels:
        raise ValueError("no complete existing rows for requested history range")
    scores = predict(model.artifact, tuple(label.features.values for label in labels))
    rows = tuple(
        ScoredRow(
            content_id([model.model_id, label.identity, role]),
            content_id([role, label.features.as_of]),
            label.features.symbol,
            label.features.as_of,
            label.features.target_at,
            score,
            label.raw_return,
            label.benchmark_return,
            label.excess_return,
        )
        for label, score in zip(labels, scores, strict=True)
    )
    report = simulate(rows, inputs, calendar, expected_count=len(rows))
    report.update(
        view_role=role,
        source_evidence="LOCAL_DECLARED",
        raw_cohorts=len({label.features.as_of for label in labels}),
        instrument_count=len({label.features.symbol for label in labels}),
        independent_samples_established=False,
        range_month=month,
        decision_start=min(label.features.as_of for label in labels).isoformat(),
        decision_end=max(label.features.as_of for label in labels).isoformat(),
    )
    return report
