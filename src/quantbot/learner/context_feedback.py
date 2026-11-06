"""Independent reading-label learning, separate from cost-aware market outcome evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from quantbot.learner.estimators import FittedEstimator, fit_estimator, predict
from quantbot.learner.models import LearnerModel
from quantbot.paper_core.external_context import (
    CONTEXT_FEATURE_NAMES,
    INTERPRETER_VERSION,
    READING_FEATURE_NAMES,
    ContextDocument,
    ContextReading,
    ContextSnapshot,
    Digest,
    Polarity,
    SourcePermissions,
    aware,
    interpret_document,
)
from quantbot.research.manifest import content_id


class ReviewEvidence(LearnerModel):
    reference_id: str = Field(min_length=1)
    content_hash: Digest
    available_at: datetime
    kind: Literal["HUMAN_ANNOTATION", "SOURCE_CORRECTION"]


class ReadingReview(LearnerModel):
    experiment_id: str = Field(min_length=1)
    reading_id: Digest
    reviewed_at: datetime
    recorded_at: datetime
    verdict: Literal["CONFIRMED", "CORRECTED", "INCONCLUSIVE"]
    label_authority: Literal["HUMAN_REVIEW", "SOURCE_CORRECTION", "OUTCOME_ONLY"]
    evidence: tuple[ReviewEvidence, ...]
    corrected_polarity: Polarity | None = None
    outcome_net_bps: float | None = None
    category: Literal["SENTIMENT_READING", "ENTITY", "FACT", "PREDICTIVE", "EXECUTION"] = (
        "SENTIMENT_READING"
    )

    @model_validator(mode="after")
    def independent_available_label(self) -> Self:
        if self.recorded_at < self.reviewed_at or any(
            row.available_at > self.reviewed_at for row in self.evidence
        ):
            raise ValueError("review evidence must be available before review and recording")
        if self.verdict != "INCONCLUSIVE":
            kind = {"HUMAN_REVIEW": "HUMAN_ANNOTATION", "SOURCE_CORRECTION": "SOURCE_CORRECTION"}
            if (
                not self.evidence
                or self.label_authority not in kind
                or any(row.kind != kind[self.label_authority] for row in self.evidence)
            ):
                raise ValueError("reading labels require explicit independent label authority")
        if (self.verdict == "CORRECTED") != (self.corrected_polarity is not None):
            raise ValueError("only corrected reviews supply a replacement polarity")
        return self


class SourceInvalidation(LearnerModel):
    reference_id: str
    invalidated_at: datetime
    reason: Literal["DELETED", "REDACTED", "RIGHTS_REVOKED"]


class ReadingExample(LearnerModel):
    experiment_id: str
    reading_id: Digest
    source_id: str
    document_id: Digest
    decision_at: datetime
    label_available_at: datetime
    features: tuple[float, ...]
    baseline_polarity: Polarity
    target_polarity: Polarity
    lineage_refs: tuple[str, ...]

    @model_validator(mode="after")
    def causal_label(self) -> Self:
        if self.label_available_at < self.decision_at or len(self.features) != len(
            READING_FEATURE_NAMES
        ):
            raise ValueError("reading examples require causal labels and versioned features")
        return self

    @property
    def example_id(self) -> str:
        return content_id(self)


def _invalidated(invalidations: Sequence[SourceInvalidation], at: datetime) -> set[str]:
    return {row.reference_id for row in invalidations if row.invalidated_at <= at}


def build_examples(
    snapshots: Sequence[ContextSnapshot],
    documents: Sequence[ContextDocument],
    reviews: Sequence[ReadingReview],
    *,
    as_of: datetime,
    invalidations: Sequence[SourceInvalidation] = (),
    training_permissions: Mapping[str, SourcePermissions] | None = None,
) -> tuple[tuple[ReadingExample, ...], tuple[tuple[str, str], ...]]:
    """Join original readings with independently supplied labels; P&L is never a label."""
    as_of = aware(as_of)
    if max(len(snapshots), len(documents), len(reviews)) > 2000:
        raise ValueError("bounded context training inputs required")
    docs = {row.document_id: ContextDocument.model_validate(row.model_dump()) for row in documents}
    labels: dict[str, ReadingReview] = {}
    conflicting: set[str] = set()
    review: ReadingReview | None
    for original in reviews:
        review = ReadingReview.model_validate(original.model_dump())
        if review.recorded_at <= as_of:
            old = labels.get(review.reading_id)
            if (
                old is not None
                and (review.reviewed_at, review.recorded_at) == (old.reviewed_at, old.recorded_at)
                and (review.verdict, review.corrected_polarity)
                != (old.verdict, old.corrected_polarity)
            ):
                conflicting.add(review.reading_id)
            if old is None or (review.reviewed_at, review.recorded_at, content_id(review)) > (
                old.reviewed_at,
                old.recorded_at,
                content_id(old),
            ):
                labels[review.reading_id] = review
    revoked = _invalidated(invalidations, as_of)
    examples: list[ReadingExample] = []
    exclusions: list[tuple[str, str]] = []
    seen_sources: set[str] = set()
    for original_snapshot in sorted(snapshots, key=lambda row: (row.at, row.snapshot_id)):
        snapshot = ContextSnapshot.model_validate(original_snapshot.model_dump())
        for reading in snapshot.readings:
            doc, review = docs.get(reading.document_id), labels.get(reading.reading_id)
            permission = (training_permissions or {}).get(
                reading.document_id,
                doc.permissions if doc is not None else None,
            )
            reason = None
            lineage: tuple[str, ...] = (reading.document_id, reading.source_id)
            if permission is not None:
                lineage += (content_id(permission), *permission.evidence_refs)
            if review is not None:
                lineage += (content_id(review), *(row.reference_id for row in review.evidence))
            if set(lineage) & revoked:
                reason = "SOURCE_INVALIDATED"
            elif reading.reading_id in conflicting:
                reason = "CONFLICTING_INDEPENDENT_REVIEWS"
            elif (
                doc is None
                or permission is None
                or permission.training != "VERIFIED"
                or permission.provider != doc.permissions.provider
            ):
                reason = "TRAINING_PERMISSION_UNVERIFIED"
            elif permission.checked_at > as_of:
                reason = "TRAINING_PERMISSION_UNAVAILABLE"
            elif snapshot.at > as_of or reading.interpreted_at != snapshot.at:
                reason = "READING_UNAVAILABLE"
            elif reading.polarity is None or reading != interpret_document(
                doc, symbol=snapshot.symbol, at=snapshot.at
            ):
                reason = "READING_UNVERIFIED_OR_AMBIGUOUS"
            elif review is None or review.reviewed_at < snapshot.at:
                reason = "INDEPENDENT_REVIEW_UNAVAILABLE"
            elif review.verdict == "INCONCLUSIVE" or review.category != "SENTIMENT_READING":
                reason = "NO_INDEPENDENT_SENTIMENT_LABEL"
            elif reading.source_id in seen_sources:
                reason = "DUPLICATE_SOURCE_CLUSTER"
            if reason:
                exclusions.append((reading.reading_id, reason))
                continue
            assert review is not None and reading.polarity is not None
            target = (
                review.corrected_polarity if review.verdict == "CORRECTED" else reading.polarity
            )
            assert target is not None
            examples.append(
                ReadingExample(
                    experiment_id=review.experiment_id,
                    reading_id=reading.reading_id,
                    source_id=reading.source_id,
                    document_id=reading.document_id,
                    decision_at=snapshot.at,
                    label_available_at=review.recorded_at,
                    features=reading.feature_values,
                    baseline_polarity=reading.polarity,
                    target_polarity=target,
                    lineage_refs=lineage,
                )
            )
            seen_sources.add(reading.source_id)
    return tuple(examples), tuple(exclusions)


class InterpreterCandidate(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    split_at: datetime
    baseline_version: Literal["news-lexical-negation-v1"] = INTERPRETER_VERSION
    estimator: FittedEstimator
    training_reading_ids: tuple[str, ...]
    heldout_reading_ids: tuple[str, ...]
    lineage_refs: tuple[str, ...]
    training_example_ids: tuple[str, ...]
    heldout_example_ids: tuple[str, ...]
    heldout_mse: float = Field(ge=0)
    baseline_heldout_mse: float = Field(ge=0)
    grants_activation: Literal[False] = False
    evaluation: Literal["CHRONOLOGICAL_READING_HOLDOUT_NOT_PROFITABILITY"] = (
        "CHRONOLOGICAL_READING_HOLDOUT_NOT_PROFITABILITY"
    )

    @property
    def candidate_id(self) -> str:
        return content_id(self)


def _mse(actual: Sequence[float], forecast: Sequence[float]) -> float:
    return sum((a - b) ** 2 for a, b in zip(actual, forecast, strict=True)) / len(actual)


def fit_interpreter_candidate(
    examples: Sequence[ReadingExample],
    *,
    split_at: datetime,
    as_of: datetime,
) -> InterpreterCandidate:
    """One fixed ridge fit; chronological holdout never supplies coefficients or scaling."""
    split_at, as_of = aware(split_at), aware(as_of)
    examples = tuple(ReadingExample.model_validate(row.model_dump()) for row in examples)
    if not split_at < as_of or len(examples) > 2000:
        raise ValueError("bounded chronological fitting window required")
    if len({row.experiment_id for row in examples}) != 1:
        raise ValueError("one registered experiment required")
    if len({row.source_id for row in examples}) != len(examples):
        raise ValueError("duplicate/revised source clusters cannot cross training or holdout")
    train = [
        row for row in examples if row.decision_at < split_at and row.label_available_at < split_at
    ]
    test = [
        row for row in examples if split_at <= row.decision_at and row.label_available_at <= as_of
    ]
    # Floors only bound mechanics; they do not establish statistical power or promotion readiness.
    if (
        len(train) < 8
        or len(test) < 4
        or min(
            len({row.decision_at.date() for row in train}),
            len({row.decision_at.date() for row in test}),
        )
        < 2
    ):
        raise ValueError("insufficient independent training/heldout source and time clusters")
    estimator = fit_estimator(
        [row.features for row in train],
        [row.target_polarity for row in train],
        READING_FEATURE_NAMES,
        family="ridge",
        alpha=1.0,
        seed=0,
    )
    predictions = tuple(
        max(-1.0, min(1.0, value)) for value in predict(estimator, [row.features for row in test])
    )
    return InterpreterCandidate(
        experiment_id=train[0].experiment_id,
        recorded_at=as_of,
        split_at=split_at,
        estimator=estimator,
        training_reading_ids=tuple(row.reading_id for row in train),
        heldout_reading_ids=tuple(row.reading_id for row in test),
        training_example_ids=tuple(row.example_id for row in train),
        heldout_example_ids=tuple(row.example_id for row in test),
        lineage_refs=tuple(sorted({ref for row in (*train, *test) for ref in row.lineage_refs})),
        heldout_mse=_mse([row.target_polarity for row in test], predictions),
        baseline_heldout_mse=_mse(
            [row.target_polarity for row in test], [row.baseline_polarity for row in test]
        ),
    )


def candidate_usable(
    candidate: InterpreterCandidate,
    invalidations: Sequence[SourceInvalidation],
    *,
    at: datetime,
) -> bool:
    at = aware(at)
    return candidate.recorded_at <= at and not set(candidate.lineage_refs) & _invalidated(
        invalidations, at
    )


def candidate_polarity(
    candidate: InterpreterCandidate,
    reading: ContextReading,
    *,
    at: datetime,
    invalidations: Sequence[SourceInvalidation] = (),
) -> float:
    """Prospective candidate reading only; no mutation or activation of the baseline."""
    if (
        not candidate_usable(candidate, invalidations, at=at)
        or not candidate.recorded_at <= reading.interpreted_at <= aware(at)
        or reading.interpreter_version != candidate.baseline_version
        or reading.polarity is None
        or {reading.document_id, reading.source_id} & _invalidated(invalidations, aware(at))
    ):
        raise ValueError("candidate or prospective reading is unavailable/invalidated")
    return max(-1.0, min(1.0, predict(candidate.estimator, (reading.feature_values,))[0]))


class DownstreamExample(LearnerModel):
    decision_id: str
    decision_at: datetime
    context: ContextSnapshot
    price_features: tuple[float, ...]
    price_available_at: datetime
    label_available_at: datetime
    net_return_bps: float
    cost_basis: Literal["MODELED_NET", "ACTUAL_NET"]
    cost_evidence_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def causal_prices_and_labels(self) -> Self:
        if (
            self.context.at > self.decision_at
            or not self.price_available_at <= self.decision_at < self.label_available_at
        ):
            raise ValueError("price inputs precede decision; outcome labels must follow it")
        return self


class DownstreamComparison(LearnerModel):
    split_at: datetime
    recorded_at: datetime
    training_decision_ids: tuple[str, ...]
    heldout_decision_ids: tuple[str, ...]
    price_only_mse: float
    price_plus_context_mse: float
    price_estimator: FittedEstimator
    context_estimator: FittedEstimator
    grants_activation: Literal[False] = False


def compare_context_to_price_only(
    rows: Sequence[DownstreamExample],
    documents: Sequence[ContextDocument],
    *,
    price_feature_names: tuple[str, ...],
    split_at: datetime,
    as_of: datetime,
    invalidations: Sequence[SourceInvalidation] = (),
    training_permissions: Mapping[str, SourcePermissions] | None = None,
) -> DownstreamComparison:
    """Same causal net labels and heldout dates for price-only and augmented ridge arms."""
    split_at, as_of = aware(split_at), aware(as_of)
    rows = tuple(DownstreamExample.model_validate(row.model_dump()) for row in rows)
    if not split_at < as_of or not 12 <= len(rows) <= 2000:
        raise ValueError("bounded chronological downstream comparison required")
    if len({row.decision_id for row in rows}) != len(rows):
        raise ValueError("downstream decisions must be unique")
    docs = {row.document_id: row for row in documents}
    revoked = _invalidated(invalidations, as_of)
    for row in rows:
        if not row.context.used_document_ids:
            raise ValueError("downstream comparison needs explicitly used context")
        for ref in row.context.used_document_ids:
            doc = docs.get(ref)
            permission = (training_permissions or {}).get(
                ref,
                doc.permissions if doc is not None else None,
            )
            if (
                doc is None
                or permission is None
                or permission.training != "VERIFIED"
                or permission.provider != doc.permissions.provider
                or permission.checked_at > as_of
                or {doc.document_id, doc.source_id} & revoked
            ):
                raise ValueError("downstream source training permission or lineage unavailable")
            if doc.first_seen_at > row.context.at or doc.permissions.checked_at > row.context.at:
                raise ValueError("downstream source was unavailable at the original decision")
            baseline = interpret_document(doc, symbol=row.context.symbol, at=row.context.at)
            if baseline not in row.context.readings:
                raise ValueError("downstream original reading provenance is unavailable")
    train = sorted(
        (row for row in rows if row.decision_at < split_at and row.label_available_at < split_at),
        key=lambda row: row.decision_at,
    )
    test = sorted(
        (row for row in rows if split_at <= row.decision_at and row.label_available_at <= as_of),
        key=lambda row: row.decision_at,
    )
    if len(train) < 8 or len(test) < 4:
        raise ValueError("insufficient downstream training/heldout examples")

    def clusters(group: Sequence[DownstreamExample]) -> set[str]:
        return {docs[ref].source_id for row in group for ref in row.context.used_document_ids}

    if clusters(train) & clusters(test):
        raise ValueError("source clusters overlap downstream training and holdout")
    target = [row.net_return_bps for row in train]
    price = fit_estimator([row.price_features for row in train], target, price_feature_names)

    def augmented(row: DownstreamExample) -> tuple[float, ...]:
        return (*row.price_features, *(value for _, value in row.context.features))

    context = fit_estimator(
        [augmented(row) for row in train], target, (*price_feature_names, *CONTEXT_FEATURE_NAMES)
    )
    heldout = [row.net_return_bps for row in test]
    return DownstreamComparison(
        split_at=split_at,
        recorded_at=as_of,
        training_decision_ids=tuple(row.decision_id for row in train),
        heldout_decision_ids=tuple(row.decision_id for row in test),
        price_only_mse=_mse(heldout, predict(price, [row.price_features for row in test])),
        price_plus_context_mse=_mse(heldout, predict(context, [augmented(row) for row in test])),
        price_estimator=price,
        context_estimator=context,
    )
