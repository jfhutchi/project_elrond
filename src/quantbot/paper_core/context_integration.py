"""Bounded observed-only context receipts in the existing discovery journal."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from quantbot.learner.models import LearnerModel
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import (
    INTERPRETER_VERSION,
    ContextDocument,
    ContextSnapshot,
    Digest,
    ProviderCoverage,
    SourcePermissions,
    aware,
    build_context_snapshot,
    document_from_research,
)
from quantbot.research.manifest import content_id

# Only these explicit provider pair spellings; bare stock-like tickers are never aliases.
ALPACA_NEWS_PAIR_TAGS = {
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
}


def _pair_asset_refs(provider_refs: tuple[str, ...]) -> tuple[str, ...]:
    pairs = {tag: pair for pair, tag in ALPACA_NEWS_PAIR_TAGS.items()}
    return tuple(dict.fromkeys(pairs.get(ref, ref) for ref in provider_refs))


class ContextBinding(LearnerModel):
    """No duplicated source text; source disappearance makes training unavailable."""

    document_id: Digest
    source_id: str
    asset_refs: tuple[str, ...]
    published_at: datetime | None
    first_seen_at: datetime
    permissions: SourcePermissions
    kind: Literal["NEWS_RESEARCH_V1", "SEC_COMPANY_RESEARCH_V1"]
    provider_asset_refs: tuple[str, ...] | None = None
    asset_mapping: Literal["ALPACA_NEWS_USD_PAIR_TAG_V1"] | None = None

    @model_serializer(mode="wrap")
    def preserve_original_bindings(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        for name in {"provider_asset_refs", "asset_mapping"} - self.model_fields_set:
            payload.pop(name, None)
        return payload


class ContextCapture(LearnerModel):
    kind: Literal["OBSERVED_CONTEXT_CAPTURE_V1"] = "OBSERVED_CONTEXT_CAPTURE_V1"
    snapshot: ContextSnapshot
    bindings: tuple[ContextBinding, ...] = Field(max_length=200)
    gaps: tuple[str, ...] = ()
    order_authority: Literal[False] = False


def load_source_permissions(state: DiscoveryStore) -> dict[str, SourcePermissions]:
    """Operator-recorded rights evidence; absence never implies a provider entitlement."""
    with state.connection() as db:
        values = state._get(db, "context:source-permissions", [])
    if not isinstance(values, list) or len(values) > 8:
        raise ValueError("BOUNDED_SOURCE_PERMISSION_RECORDS_REQUIRED")
    records = tuple(SourcePermissions.model_validate(value) for value in values)
    if len({row.provider for row in records}) != len(records):
        raise ValueError("DUPLICATE_SOURCE_PERMISSION_RECORD")
    return {row.provider: row for row in records}


def permission_coverage(
    permissions: Mapping[str, SourcePermissions],
    *,
    at: datetime,
) -> tuple[ProviderCoverage, ...]:
    at = aware(at)
    result = []
    for provider in ("alpaca-news", "sec-edgar", "x", "reddit"):
        permission = permissions.get(provider)
        social = provider in {"x", "reddit"}
        verified = (
            permission is not None
            and permission.provider == provider
            and (
                permission.checked_at <= at
                and permission.access == "VERIFIED"
                and permission.metadata_retention == "VERIFIED"
            )
        )
        result.append(
            ProviderCoverage(
                provider=provider,
                status="UNAVAILABLE" if social else "UNKNOWN",
                observed_at=at,
                reason="No configured authorized collection in this pipeline"
                if social
                else "Permission verified; retained symbol coverage not yet inspected"
                if verified
                else "Access/metadata permission unverified",
            )
        )
    return tuple(result)


def capture_research_context(
    state: DiscoveryStore,
    *,
    symbol: str,
    at: datetime,
    permissions: Mapping[str, SourcePermissions],
    maximum_documents: int = 100,
) -> ContextSnapshot | None:
    """Run before selection. Never assert that observed context influenced a decision."""
    at = aware(at)
    if not 1 <= maximum_documents <= 200:
        raise ValueError("context document bound must be 1..200")
    documents: list[ContextDocument] = []
    bindings: list[ContextBinding] = []
    gaps: list[str] = []
    # The date range and LIMIT use the existing indexed symbol/kind journal window.
    with state.connection() as db:
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        rows = []
        if {"research_documents", "research_symbols"} <= tables:
            for kind, age in (
                ("NEWS_RESEARCH_V1", timedelta(hours=48)),
                ("SEC_COMPANY_RESEARCH_V1", timedelta(days=365)),
            ):
                tags: tuple[str, ...] = (symbol,)
                if kind == "NEWS_RESEARCH_V1" and symbol in ALPACA_NEWS_PAIR_TAGS:
                    tags += (ALPACA_NEWS_PAIR_TAGS[symbol],)
                for tag in tags:
                    rows.extend(
                        db.execute(
                            "select d.id,d.payload from research_symbols s "
                            "join research_documents d on d.id=s.document_id "
                            "where s.symbol=? and s.kind=? and s.published_at>=? "
                            "and s.published_at<=? and s.received_at<=? "
                            "order by s.published_at desc,s.received_at desc,d.id limit ?",
                            (
                                tag,
                                kind,
                                (at - age).isoformat(),
                                at.isoformat(),
                                at.isoformat(),
                                maximum_documents + 1,
                            ),
                        ).fetchall()
                    )
    # A story indexed under both provider spellings is one document, including at the bound.
    rows = list({row[0]: row for row in rows}.values())
    if len(rows) > maximum_documents:
        gaps.append("CONTEXT_DOCUMENT_BOUND_REACHED")
    rows = sorted(rows, key=lambda row: (json.loads(row[1])["retrieved_at"], row[0]), reverse=True)[
        :maximum_documents
    ]
    counts = {"alpaca-news": 0, "sec-edgar": 0}
    for identity, encoded in rows:
        payload = json.loads(encoded)
        if content_id(payload) != identity:
            raise ValueError("RESEARCH_DOCUMENT_HASH_MISMATCH")
        provider = "alpaca-news" if payload["kind"] == "NEWS_RESEARCH_V1" else "sec-edgar"
        permission = permissions.get(provider)
        if (
            permission is None
            or permission.provider != provider
            or permission.checked_at > at
            or permission.access != "VERIFIED"
            or permission.metadata_retention != "VERIFIED"
        ):
            gaps.append(provider + ":PERMISSION_UNVERIFIED")
            continue
        asset_refs = tuple(payload["symbols"])
        mapping: dict[str, Any] = {}
        if provider == "alpaca-news" and symbol in ALPACA_NEWS_PAIR_TAGS:
            normalized = _pair_asset_refs(asset_refs)
            if normalized != asset_refs:
                mapping = dict(
                    provider_asset_refs=asset_refs, asset_mapping="ALPACA_NEWS_USD_PAIR_TAG_V1"
                )
                asset_refs = normalized
        first_seen = aware(datetime.fromisoformat(payload["retrieved_at"]))
        if first_seen > at or symbol not in asset_refs:
            continue
        if provider == "alpaca-news":
            document = document_from_research(
                payload, document_id=identity, permissions=permission, asset_refs=asset_refs
            )
            documents.append(document)
        else:
            # SEC aggregates have no single publication timestamp or narrative reading.
            gaps.append("SEC_AGGREGATE_PUBLICATION_UNKNOWN_NO_NARRATIVE_INTERPRETER")
        bindings.append(
            ContextBinding(
                document_id=identity,
                source_id=payload["source_id"],
                asset_refs=asset_refs,
                published_at=payload.get("published_at"),
                first_seen_at=first_seen,
                permissions=permission,
                kind=payload["kind"],
                **mapping,
            )
        )
        counts[provider] += 1
    if not bindings:
        return None
    coverage = []
    for provider, count in counts.items():
        permission = permissions.get(provider)
        verified = (
            permission is not None
            and permission.provider == provider
            and (
                permission.checked_at <= at
                and permission.access == "VERIFIED"
                and permission.metadata_retention == "VERIFIED"
            )
        )
        coverage.append(
            ProviderCoverage(
                provider=provider,
                status="PARTIAL" if count else "UNKNOWN",
                observed_at=at,
                reason=(
                    f"{count} retained vintages; incomplete provider coverage"
                    if count
                    else "No eligible retained symbol documents"
                    if verified
                    else "Access/metadata permission unverified"
                ),
            )
        )
    coverage.extend(
        ProviderCoverage(
            provider=p,
            status="UNAVAILABLE",
            observed_at=at,
            reason="No configured authorized collection in this pipeline",
        )
        for p in ("x", "reddit")
    )
    snapshot = build_context_snapshot(documents, symbol=symbol, at=at, coverage=coverage)
    snapshot = ContextSnapshot.model_validate(
        snapshot.model_dump()
        | {
            "observed_document_ids": tuple(sorted({b.document_id for b in bindings})),
            "gaps": tuple(sorted(set(snapshot.gaps) | set(gaps))),
        }
    )
    capture = ContextCapture(
        snapshot=snapshot, bindings=tuple(bindings), gaps=tuple(sorted(set(gaps)))
    )
    fingerprint = content_id(
        dict(
            symbol=symbol,
            bindings=bindings,
            gaps=capture.gaps,
            coverage=tuple(row.model_dump(exclude={"observed_at"}) for row in coverage),
            interpreter_version=INTERPRETER_VERSION,
        )
    )
    with state.connection() as db:
        previous = state._get(db, "context-current:" + symbol)
        if previous and previous["fingerprint"] == fingerprint:
            old_id = state._get(db, "context-capture:" + previous["snapshot_id"])
            old_row = db.execute("select payload from receipts where id=?", (old_id,)).fetchone()
            if old_row:
                old = ContextCapture.model_validate_json(old_row[0])
                if content_id(old) != old_id:
                    raise ValueError("CONTEXT_CAPTURE_HASH_MISMATCH")
                if old.snapshot.at <= at:
                    return old.snapshot
        existing = state._get(db, "context-capture:" + snapshot.snapshot_id)
        if existing is not None and existing != content_id(capture):
            raise ValueError("CONTEXT_CAPTURE_IDENTITY_CONFLICT")
        capture_id = state._receipt(db, capture)
        state._set(db, "context-capture:" + snapshot.snapshot_id, capture_id)
        state._set(
            db,
            "context-current:" + symbol,
            dict(fingerprint=fingerprint, snapshot_id=snapshot.snapshot_id),
        )
    return snapshot


def load_context_capture(state: DiscoveryStore, snapshot_id: str) -> ContextCapture:
    with state.connection() as db:
        identity = state._get(db, "context-capture:" + snapshot_id)
        row = db.execute("select payload from receipts where id=?", (identity,)).fetchone()
    if row is None:
        raise ValueError("CONTEXT_CAPTURE_UNAVAILABLE")
    capture = ContextCapture.model_validate_json(row[0])
    if content_id(capture) != identity or capture.snapshot.snapshot_id != snapshot_id:
        raise ValueError("CONTEXT_CAPTURE_HASH_MISMATCH")
    return capture


def retained_context_documents(
    state: DiscoveryStore, capture: ContextCapture
) -> tuple[ContextDocument, ...]:
    """Rehydrate only original permitted news vintages; deleted/redacted content is absent."""
    documents = []
    with state.connection() as db:
        for binding in capture.bindings:
            if binding.kind != "NEWS_RESEARCH_V1":
                continue
            row = db.execute(
                "select payload from research_documents where id=?", (binding.document_id,)
            ).fetchone()
            if row is None:
                continue
            payload = json.loads(row[0])
            if binding.asset_mapping is not None or binding.provider_asset_refs is not None:
                if (
                    binding.asset_mapping != "ALPACA_NEWS_USD_PAIR_TAG_V1"
                    or binding.permissions.provider != "alpaca-news"
                    or binding.provider_asset_refs != tuple(payload["symbols"])
                    or binding.asset_refs != _pair_asset_refs(tuple(payload["symbols"]))
                ):
                    raise ValueError("CONTEXT_ASSET_MAPPING_PROVENANCE_MISMATCH")
            documents.append(
                document_from_research(
                    payload,
                    document_id=binding.document_id,
                    permissions=binding.permissions,
                    asset_refs=binding.asset_refs,
                )
            )
    return tuple(documents)
