"""Point-in-time company/fund shadow context. Never news execution authority."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from quantbot.learner.experiment_contract import (
    DecisionRecord,
    Digest,
    ExperimentManifest,
    InstrumentIdentity,
)
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel, Text
from quantbot.learner.position_research import ResearchDecision
from quantbot.learner.position_sources import PositionResearchSnapshot
from quantbot.research.manifest import content_id
from quantbot.research.sources import SourceHealth, SourceKind
from quantbot.storage.database import decode_utc


def unavailable_evidence(
    manifest: ExperimentManifest,
    identity: InstrumentIdentity,
    *,
    now: datetime,
    reason: str,
) -> NewsEvidence:
    return NewsEvidence(
        experiment_id=manifest.experiment_id,
        symbol=identity.symbol,
        asset_id=identity.asset_id,
        recorded_at=now,
        retrieved_at=now,
        provider="POSITION_RESEARCH_JOURNAL",
        source_url=None,
        source_hash=None,
        headline="PUBLIC_PROVIDER_COVERAGE",
        published_at=None,
        first_seen_at=None,
        status="UNAVAILABLE",
        scope="FUND_LEVEL" if identity.asset_class == "ETF" else "EXACT_INSTRUMENT_QUERY",
        coverage_reason=reason,
        synthetic=manifest.synthetic,
    )


def read_position_journal(
    path: Path | None,
    manifest: ExperimentManifest,
    *,
    now: datetime,
    expected_commit: str | None = None,
) -> tuple[NewsEvidence, ...]:
    """Bounded query-only bridge from the existing observational provider journal.

    Verify the original content-addressed signal before importing exact-instrument
    provenance. Missing/failing/old coverage remains unknown, with no order authority.
    """
    result: list[NewsEvidence] = []
    for identity in manifest.roster:
        reason = "POSITION_RESEARCH_NOT_CONFIGURED"
        evidence: tuple[NewsEvidence, ...] = ()
        if path is not None:
            reason = "POSITION_RESEARCH_UNAVAILABLE"
            try:
                if expected_commit is None or len(expected_commit) != 40:
                    raise ValueError("PINNED_RESEARCH_SOURCE_REQUIRED")
                if not path.is_absolute() or path.is_symlink() or not path.is_file():
                    raise ValueError("EXISTING_NON_SYMLINK_JOURNAL_REQUIRED")
                with sqlite3.connect(
                    path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
                ) as db:
                    db.execute("PRAGMA query_only=ON")
                    rows = db.execute(
                        "SELECT s.signal_id,s.run_id,s.strategy_id,r.strategy_version,"
                        "s.occurred_at,s.payload_json FROM signals s JOIN runs r USING(run_id) "
                        "WHERE s.symbol=? AND s.strategy_id=r.strategy_id "
                        "AND s.strategy_id='paper-position-research-shadow' "
                        "AND r.strategy_version=? "
                        "AND length(s.payload_json)<=100000 "
                        "AND julianday(s.occurred_at)<=julianday(?) "
                        "ORDER BY julianday(s.occurred_at) DESC LIMIT 1",
                        (identity.symbol, expected_commit, now.isoformat()),
                    ).fetchall()
                if rows:
                    event_id, run_id, strategy, version, occurred, payload = rows[0]
                    if len(payload) > 100_000 or decode_utc(occurred) > now:
                        raise ValueError("BOUNDED_AS_OF_JOURNAL_REQUIRED")
                    document = json.loads(payload)
                    if document.get("kind") != "PAPER_RESEARCH_DECISION_V1":
                        raise ValueError("POSITION_RESEARCH_KIND_REQUIRED")
                    advice = ResearchDecision.model_validate(document["decision"])
                    snapshot = PositionResearchSnapshot.model_validate(document["research_inputs"])
                    expected = "paper-research-" + content_id(
                        {
                            "run": run_id,
                            "strategy": strategy,
                            "version": version,
                            "decision": advice,
                            "research_inputs": snapshot.model_dump(mode="json"),
                        }
                    )
                    if event_id != expected or advice.at != decode_utc(occurred):
                        raise ValueError("POSITION_RESEARCH_HASH_UNVERIFIED")
                    # Retain the verified source journal's original availability,
                    # rather than pretending the import preceded the decision.
                    evidence = evidence_from_position_snapshot(
                        manifest, snapshot, recorded_at=decode_utc(occurred)
                    )
                    if now - snapshot.collected_at > timedelta(minutes=10):
                        reason = "POSITION_RESEARCH_RECEIPT_STALE"
                        evidence = tuple(
                            row.model_copy(update={"status": "STALE"}) for row in evidence
                        )
                else:
                    reason = "POSITION_RESEARCH_INSTRUMENT_NOT_COVERED"
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                evidence = ()
        result.extend(
            evidence or (unavailable_evidence(manifest, identity, now=now, reason=reason),)
        )
    return tuple(result)


class NewsEvidence(LearnerModel):
    experiment_id: Text
    symbol: Text
    asset_id: Text
    recorded_at: datetime
    provider: Text
    kind: Literal["NEWS", "SEC_FILING"] = "NEWS"
    source_url: str | None
    source_hash: Digest | None
    headline: Text
    published_at: datetime | None
    first_seen_at: datetime | None
    retrieved_at: datetime
    status: Literal["OK", "STALE", "UNAVAILABLE", "EMPTY", "NOT_APPLICABLE", "COVERAGE_REPORTED"]
    scope: Literal["EXACT_INSTRUMENT_QUERY", "FUND_LEVEL", "COMPANY"]
    company_id: str | None = None
    company_mapping_hash: Digest | None = None
    returned_count: int | None = Field(default=None, ge=0)
    usable_count: int | None = Field(default=None, ge=0)
    coverage_reason: str | None = None
    complete_public_coverage: Literal[False] = False
    synthetic: bool = False

    @property
    def evidence_id(self) -> str:
        return content_id(self)

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if self.retrieved_at > self.recorded_at:
            raise ValueError("recording precedes retrieval")
        if self.status == "OK" and (
            self.source_hash is None
            or self.source_url is None
            or self.published_at is None
            or self.first_seen_at is None
        ):
            raise ValueError("usable research requires complete provenance")
        if (
            self.published_at is not None
            and self.first_seen_at is not None
            and not (self.published_at <= self.first_seen_at <= self.retrieved_at)
        ):
            raise ValueError("publication/first-seen/retrieval ordering invalid")
        return self


class ThesisRecord(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    decision_id: Digest
    symbol: Text
    action: Literal["BUY", "HOLD", "EXIT", "WAIT"]
    reason: Text
    evidence_ids: tuple[Digest, ...]
    limitations: tuple[str, ...]
    order_authority: Literal[False] = False
    synthetic: bool = False


def build_news_evidence(
    identity: InstrumentIdentity, documents: Sequence[NewsEvidence], *, decision_at: datetime
) -> tuple[NewsEvidence, ...]:
    result = []
    for original in documents:
        row = NewsEvidence.model_validate(original.model_dump())
        if (row.symbol, row.asset_id) != (identity.symbol, identity.asset_id):
            raise ValueError("RESEARCH_INSTRUMENT_IDENTITY_MISMATCH")
        if row.kind == "SEC_FILING" and row.status == "OK":
            if identity.asset_class == "ETF":
                raise ValueError("ETF_COMPANY_FILINGS_NOT_INFERRED")
            host = urlsplit(row.source_url or "").hostname
            if (
                row.company_id is None
                or row.company_mapping_hash is None
                or host
                not in {
                    "www.sec.gov",
                    "sec.gov",
                    "data.sec.gov",
                }
            ):
                raise ValueError("verified company filing identity and provenance required")
        if (
            row.status == "OK"
            and row.published_at is not None
            and row.first_seen_at is not None
            and row.first_seen_at <= row.retrieved_at <= row.recorded_at <= decision_at
            and decision_at - row.published_at <= timedelta(hours=48)
        ):
            result.append(row)
    return tuple(result)


def journal_thesis(
    store: ExperimentStore, decision: DecisionRecord, evidence: Sequence[NewsEvidence]
) -> ThesisRecord:
    decision = DecisionRecord.model_validate(decision.model_dump())
    manifests = tuple(
        row.decode(ExperimentManifest)
        for row in store.read_as_of(decision.experiment_id, decision.recorded_at)
        if row.kind == "ExperimentManifest"
    )
    if len(manifests) != 1:
        raise ValueError("exact registered experiment required")
    identity = next(
        (row for row in manifests[0].roster if row.symbol == decision.features.symbol), None
    )
    if identity is None or any(row.experiment_id != decision.experiment_id for row in evidence):
        raise ValueError("RESEARCH_EXPERIMENT_IDENTITY_MISMATCH")
    usable = build_news_evidence(identity, evidence, decision_at=decision.features.decision_at)
    for row in evidence:
        if row.recorded_at <= decision.recorded_at:
            store.append(row, idempotency_key="news:" + row.evidence_id)
    record = ThesisRecord(
        experiment_id=decision.experiment_id,
        recorded_at=decision.recorded_at,
        decision_id=decision.decision_id,
        symbol=decision.features.symbol,
        action=decision.action if usable else "WAIT",
        reason=decision.reason if usable else "RESEARCH_COVERAGE_INCOMPLETE_SHADOW",
        evidence_ids=tuple(row.evidence_id for row in usable),
        limitations=("SHADOW_CONTEXT_ONLY", "PUBLIC_INFORMATION_COVERAGE_INCOMPLETE"),
        synthetic=decision.synthetic,
    )
    store.append(record, idempotency_key="thesis:" + content_id(record))
    return record


def evidence_from_position_snapshot(
    manifest: ExperimentManifest, snapshot: PositionResearchSnapshot, *, recorded_at: datetime
) -> tuple[NewsEvidence, ...]:
    """Connect existing authorized provider results without changing strategy authority."""
    snapshot = PositionResearchSnapshot.model_validate(snapshot.model_dump())
    identity = next(
        (row for row in manifest.roster if row.symbol == snapshot.instrument.symbol), None
    )
    if (
        identity is None
        or identity.asset_id != snapshot.instrument.instrument_id
        or (identity.asset_class != snapshot.instrument.asset_class.value)
    ):
        raise ValueError("PROVIDER_INSTRUMENT_IDENTITY_MISMATCH")
    if snapshot.collected_at > recorded_at:
        raise ValueError("provider snapshot was not available at recording time")
    scope: Literal["EXACT_INSTRUMENT_QUERY", "FUND_LEVEL", "COMPANY"] = (
        "FUND_LEVEL" if identity.asset_class == "ETF" else "EXACT_INSTRUMENT_QUERY"
    )
    result: list[NewsEvidence] = []
    for source in snapshot.sources:
        if source.kind not in {SourceKind.NEWS, SourceKind.SEC_FILING}:
            continue
        status: Literal["OK", "STALE", "UNAVAILABLE"] = "OK"
        if source.derived or source.health is not SourceHealth.OK:
            status = "UNAVAILABLE"
        result.append(
            NewsEvidence(
                experiment_id=manifest.experiment_id,
                symbol=identity.symbol,
                asset_id=identity.asset_id,
                recorded_at=recorded_at,
                provider=source.publisher,
                kind="SEC_FILING" if source.kind is SourceKind.SEC_FILING else "NEWS",
                source_url=source.uri,
                source_hash=source.content_hash,
                headline=source.title,
                published_at=source.published_at,
                first_seen_at=source.retrieved_at,
                retrieved_at=source.retrieved_at,
                status=status,
                scope="COMPANY" if source.kind is SourceKind.SEC_FILING else scope,
                company_id=snapshot.company.cik if snapshot.company else None,
                company_mapping_hash=content_id(snapshot.company) if snapshot.company else None,
                synthetic=manifest.synthetic,
            )
        )
    for coverage in snapshot.coverage:
        coverage_status: Literal["UNAVAILABLE", "EMPTY", "NOT_APPLICABLE", "COVERAGE_REPORTED"]
        if coverage.status == "HEALTHY":
            coverage_status = "EMPTY" if coverage.returned_count == 0 else "COVERAGE_REPORTED"
        elif coverage.status == "NOT_APPLICABLE":
            coverage_status = "NOT_APPLICABLE"
        else:
            coverage_status = "UNAVAILABLE"
        result.append(
            NewsEvidence(
                experiment_id=manifest.experiment_id,
                symbol=identity.symbol,
                asset_id=identity.asset_id,
                recorded_at=recorded_at,
                provider=coverage.provider,
                kind="SEC_FILING" if coverage.provider == "sec-edgar" else "NEWS",
                source_url=None,
                source_hash=None,
                headline="PUBLIC_PROVIDER_COVERAGE",
                published_at=None,
                first_seen_at=None,
                retrieved_at=coverage.retrieved_at,
                status=coverage_status,
                scope=scope,
                coverage_reason=coverage.scope,
                returned_count=coverage.returned_count if coverage.status == "HEALTHY" else None,
                usable_count=coverage.usable_count if coverage.status == "HEALTHY" else None,
                synthetic=manifest.synthetic,
            )
        )
    return tuple(result)
