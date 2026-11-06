"""Causal, permission-bound news context; deterministic readings grant no order authority."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from quantbot.learner.models import LearnerModel
from quantbot.research.manifest import content_id

Status = Literal["VERIFIED", "UNKNOWN", "DENIED"]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Polarity = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
INTERPRETER_VERSION: Literal["news-lexical-negation-v1"] = "news-lexical-negation-v1"
READING_FEATURE_NAMES = (
    "positive_terms",
    "negative_terms",
    "negated_terms",
    "earnings_event",
    "legal_event",
    "operations_event",
)
CONTEXT_FEATURE_NAMES = (
    "document_count",
    "positive_count",
    "negative_count",
    "polarity_mean",
    "disagreement",
    "missing_coverage_count",
)


def aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("aware context time required")
    return at.astimezone(UTC)


class SourcePermissions(LearnerModel):
    provider: str = Field(min_length=1)
    access: Status = "UNKNOWN"
    metadata_retention: Status = "UNKNOWN"
    retention: Status = "UNKNOWN"
    processing: Status = "UNKNOWN"
    training: Status = "UNKNOWN"
    evidence_refs: tuple[str, ...] = ()
    checked_at: datetime

    @model_validator(mode="after")
    def verified_has_provenance(self) -> Self:
        if "VERIFIED" in (
            self.access,
            self.metadata_retention,
            self.retention,
            self.processing,
            self.training,
        ) and (not self.evidence_refs or any(not ref.strip() for ref in self.evidence_refs)):
            raise ValueError("verified permissions require independently recorded evidence")
        return self


class ContextDocument(LearnerModel):
    document_id: Digest
    source_id: str = Field(min_length=1)
    source_uri: str
    publisher: str
    content_hash: Digest
    asset_refs: tuple[str, ...]
    published_at: datetime
    updated_at: datetime
    first_seen_at: datetime
    parser_version: str
    permissions: SourcePermissions
    text: str | None = Field(default=None, max_length=6001)

    @model_validator(mode="after")
    def causal_and_permitted(self) -> Self:
        if not self.published_at <= self.updated_at <= self.first_seen_at:
            raise ValueError("publication, revision and first observation must be causal")
        if not self.asset_refs or len(set(self.asset_refs)) != len(self.asset_refs):
            raise ValueError("explicit unique asset references required")
        if (
            self.permissions.access != "VERIFIED"
            or self.permissions.metadata_retention != "VERIFIED"
        ):
            raise ValueError("access and metadata retention must be verified")
        if self.text is not None and self.permissions.retention != "VERIFIED":
            raise ValueError("full text retention permission is not verified")
        return self


def document_from_research(
    payload: Mapping[str, Any],
    *,
    document_id: str,
    permissions: SourcePermissions,
    asset_refs: tuple[str, ...],
) -> ContextDocument:
    """Adapt a verified existing journal vintage; never infer earlier first observation."""
    if payload.get("kind") != "NEWS_RESEARCH_V1" or content_id(payload) != document_id:
        raise ValueError("existing news document kind and digest must match")
    if permissions.provider != "alpaca-news" or not str(payload["source_id"]).startswith(
        "alpaca-news:"
    ):
        raise ValueError("provider permission must match the source")
    text = None
    if permissions.retention == "VERIFIED":
        text = str(payload.get("title", ""))[:2000] + "\n" + str(payload.get("summary", ""))[:4000]
    return ContextDocument(
        document_id=document_id,
        source_id=payload["source_id"],
        source_uri=payload["uri"],
        publisher=payload.get("publisher", ""),
        content_hash=payload["parsed_item_sha256"],
        asset_refs=asset_refs,
        published_at=payload["published_at"],
        updated_at=payload.get("updated_at", payload["published_at"]),
        first_seen_at=payload["retrieved_at"],
        parser_version=payload["parser"],
        permissions=permissions,
        text=text,
    )


class ProviderCoverage(LearnerModel):
    provider: str
    status: Literal["AVAILABLE", "PARTIAL", "UNAVAILABLE", "UNKNOWN"]
    observed_at: datetime
    reason: str


class ContextReading(LearnerModel):
    document_id: Digest
    source_id: str
    symbol: str
    interpreted_at: datetime
    interpreter_version: str = INTERPRETER_VERSION
    entity_status: Literal["EXACT_PROVIDER_TAG", "AMBIGUOUS", "UNAVAILABLE"]
    event_tags: tuple[str, ...]
    positive_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    negated_terms: tuple[str, ...]
    polarity: Polarity | None
    direction: Literal["UP", "DOWN", "NEUTRAL", "UNKNOWN"]
    horizon_seconds: Literal[1800] = 1800
    expected_return_bps: None = None
    confidence_kind: Literal["UNCALIBRATED_HEURISTIC"] = "UNCALIBRATED_HEURISTIC"
    reading_confidence: None = None
    predicted_direction_confidence: None = None
    feature_values: tuple[float, ...]
    order_authority: Literal[False] = False

    @property
    def reading_id(self) -> str:
        return content_id(self)


_POSITIVE = frozenset({"beat", "beats", "upgrade", "upgraded", "growth", "raises", "approval"})
_NEGATIVE = frozenset({"miss", "misses", "downgrade", "downgraded", "cuts", "loss", "fraud"})
_NEGATIONS = frozenset({"not", "no", "never", "without", "denies", "denied"})
_EVENTS = {
    "EARNINGS": frozenset({"earnings", "revenue", "guidance", "estimates"}),
    "LEGAL_REGULATORY": frozenset({"lawsuit", "fraud", "regulator", "approval", "fda"}),
    "OPERATIONS": frozenset({"production", "recall", "layoff", "contract", "bankruptcy"}),
}


def interpret_document(document: ContextDocument, *, symbol: str, at: datetime) -> ContextReading:
    """A bounded lexical baseline, not a calibrated forecast or an LLM instruction channel."""
    at = aware(at)
    if document.first_seen_at > at or document.permissions.checked_at > at:
        raise ValueError("source or permission unavailable at interpretation time")
    usable = document.text is not None and document.permissions.processing == "VERIFIED"
    entity: Literal["EXACT_PROVIDER_TAG", "AMBIGUOUS", "UNAVAILABLE"] = (
        ("EXACT_PROVIDER_TAG" if document.asset_refs == (symbol,) else "AMBIGUOUS")
        if usable
        else "UNAVAILABLE"
    )
    positive: list[str] = []
    negative: list[str] = []
    negated: list[str] = []
    terms: set[str] = set()
    score = 0
    # Negation is confined to the preceding three tokens in the same sentence/clause.
    permitted_text = (document.text or "") if usable else ""
    for clause in re.split(r"[.!?;\n]|\bbut\b|\bhowever\b", permitted_text, flags=re.I):
        tokens = re.findall(r"[a-z]+", clause.lower())
        terms.update(tokens)
        for index, token in enumerate(tokens):
            if token not in _POSITIVE | _NEGATIVE:
                continue
            sign = 1 if token in _POSITIVE else -1
            (positive if sign == 1 else negative).append(token)
            if set(tokens[max(0, index - 3) : index]) & _NEGATIONS:
                sign *= -1
                negated.append(token)
            score += sign
    events = tuple(tag for tag, vocabulary in _EVENTS.items() if terms & vocabulary)
    polarity = float(max(-1, min(1, score))) if entity == "EXACT_PROVIDER_TAG" else None
    direction: Literal["UP", "DOWN", "NEUTRAL", "UNKNOWN"] = (
        "UNKNOWN"
        if polarity is None
        else "UP"
        if polarity > 0
        else "DOWN"
        if polarity < 0
        else "NEUTRAL"
    )
    return ContextReading(
        document_id=document.document_id,
        source_id=document.source_id,
        symbol=symbol,
        interpreted_at=at,
        entity_status=entity,
        event_tags=events,
        positive_terms=tuple(positive),
        negative_terms=tuple(negative),
        negated_terms=tuple(negated),
        polarity=polarity,
        direction=direction,
        feature_values=(
            float(len(positive)),
            float(len(negative)),
            float(len(negated)),
            *(float(tag in events) for tag in _EVENTS),
        ),
    )


class ContextSnapshot(LearnerModel):
    contract: Literal["PRETRADE_NEWS_CONTEXT_V1"] = "PRETRADE_NEWS_CONTEXT_V1"
    symbol: str
    at: datetime
    observed_document_ids: tuple[str, ...]
    eligible_document_ids: tuple[str, ...]
    used_document_ids: tuple[str, ...]
    readings: tuple[ContextReading, ...]
    coverage: tuple[ProviderCoverage, ...]
    gaps: tuple[str, ...]
    features: tuple[tuple[str, float], ...]
    order_authority: Literal[False] = False

    @model_validator(mode="after")
    def causal_feature_lineage(self) -> Self:
        observed, eligible, used = (
            set(self.observed_document_ids),
            set(self.eligible_document_ids),
            set(self.used_document_ids),
        )
        if (
            not used <= eligible <= observed
            or len(used) != len(self.used_document_ids)
            or len(eligible) != len(self.eligible_document_ids)
            or len(observed) != len(self.observed_document_ids)
            or len({row.document_id for row in self.readings}) != len(self.readings)
            or any(
                row.interpreted_at != self.at
                or row.symbol != self.symbol
                or row.document_id not in observed
                for row in self.readings
            )
            or eligible != {row.document_id for row in self.readings if row.polarity is not None}
            or any(row.observed_at > self.at for row in self.coverage)
            or tuple(name for name, _ in self.features) != CONTEXT_FEATURE_NAMES
        ):
            raise ValueError("context feature lineage must use available observed sources")
        selected = [
            row.polarity
            for row in self.readings
            if row.document_id in used and row.polarity is not None
        ]
        positive, negative = sum(v > 0 for v in selected), sum(v < 0 for v in selected)
        expected = (
            float(len(selected)),
            float(positive),
            float(negative),
            sum(selected) / len(selected) if selected else 0.0,
            float(bool(positive and negative)),
            float(sum(row.status != "AVAILABLE" for row in self.coverage) + 2),
        )
        if tuple(value for _, value in self.features) != expected:
            raise ValueError("context features must exactly describe actually used readings")
        return self

    @property
    def snapshot_id(self) -> str:
        return content_id(self)


def build_context_snapshot(
    documents: Sequence[ContextDocument],
    *,
    symbol: str,
    at: datetime,
    coverage: Sequence[ProviderCoverage] = (),
    used_document_ids: tuple[str, ...] = (),
    invalidated_document_ids: tuple[str, ...] = (),
    max_age: timedelta = timedelta(hours=48),
) -> ContextSnapshot:
    at = aware(at)
    if len(documents) > 1000 or max_age <= timedelta(0):
        raise ValueError("bounded context document window required")
    gaps = {"X_UNAVAILABLE_IN_THIS_PIPELINE", "REDDIT_UNAVAILABLE_IN_THIS_PIPELINE"}
    versions: dict[str, ContextDocument] = {}
    observed: set[str] = set()
    for original in documents:
        doc = ContextDocument.model_validate(original.model_dump())
        if symbol not in doc.asset_refs:
            continue
        if max(doc.published_at, doc.first_seen_at, doc.permissions.checked_at) > at:
            gaps.add("SOURCE_UNAVAILABLE_AT_DECISION")
            continue
        observed.add(doc.document_id)
        if doc.document_id in invalidated_document_ids:
            gaps.add("SOURCE_INVALIDATED")
            continue
        if at - doc.published_at > max_age:
            gaps.add("SOURCE_STALE")
            continue
        old = versions.get(doc.source_id)
        if old is None or (doc.updated_at, doc.first_seen_at) > (old.updated_at, old.first_seen_at):
            versions[doc.source_id] = doc
    readings = tuple(
        interpret_document(doc, symbol=symbol, at=at)
        for doc in sorted(versions.values(), key=lambda row: row.document_id)
    )
    eligible = tuple(row.document_id for row in readings if row.polarity is not None)
    if len(set(used_document_ids)) != len(used_document_ids) or not set(used_document_ids) <= set(
        eligible
    ):
        raise ValueError("actually used sources must be unique, available and permitted")
    if any(row.entity_status == "UNAVAILABLE" for row in readings):
        gaps.add("SOURCE_TEXT_OR_PROCESSING_UNAVAILABLE")
    if any(row.entity_status == "AMBIGUOUS" for row in readings):
        gaps.add("ENTITY_MAPPING_AMBIGUOUS")
    current_coverage = tuple(row for row in coverage if row.observed_at <= at)
    missing = sum(row.status != "AVAILABLE" for row in current_coverage) + 2
    gaps.update(
        row.provider + ":" + row.reason for row in current_coverage if row.status != "AVAILABLE"
    )
    selected = [
        row.polarity
        for row in readings
        if row.document_id in used_document_ids and row.polarity is not None
    ]
    positive, negative = sum(v > 0 for v in selected), sum(v < 0 for v in selected)
    values = (
        float(len(selected)),
        float(positive),
        float(negative),
        sum(selected) / len(selected) if selected else 0.0,
        float(bool(positive and negative)),
        float(missing),
    )
    return ContextSnapshot(
        symbol=symbol,
        at=at,
        observed_document_ids=tuple(sorted(observed)),
        eligible_document_ids=eligible,
        used_document_ids=tuple(sorted(used_document_ids)),
        readings=readings,
        coverage=current_coverage,
        gaps=tuple(sorted(gaps)),
        features=tuple(zip(CONTEXT_FEATURE_NAMES, values, strict=True)),
    )
