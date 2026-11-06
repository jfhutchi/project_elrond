"""Decision-time context must retain causal provenance and honest missing coverage."""

from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 11, 6, 15, tzinfo=UTC)


def permissions(**changes):
    from quantbot.paper_core.external_context import SourcePermissions

    return SourcePermissions(
        **(
            {
                "provider": "alpaca-news",
                "access": "VERIFIED",
                "metadata_retention": "VERIFIED",
                "retention": "VERIFIED",
                "processing": "VERIFIED",
                "training": "VERIFIED",
                "evidence_refs": ("synthetic-permission-fixture",),
                "checked_at": NOW - timedelta(days=1),
            }
            | changes
        )
    )


def document(title="AAPL beats earnings estimates", *, identity="1", **changes):
    from quantbot.paper_core.external_context import document_from_research
    from quantbot.research.manifest import content_id

    payload = {
        "kind": "NEWS_RESEARCH_V1",
        "source_id": "alpaca-news:" + identity,
        "uri": "https://example.invalid/news/" + identity,
        "publisher": "synthetic",
        "title": title,
        "summary": "",
        "symbols": ["AAPL"],
        "published_at": (NOW - timedelta(minutes=5)).isoformat(),
        "updated_at": (NOW - timedelta(minutes=5)).isoformat(),
        "retrieved_at": (NOW - timedelta(minutes=4)).isoformat(),
        "parser": "synthetic-news-fixture-v1",
        "parsed_item_sha256": "a" * 64,
    } | changes
    return document_from_research(
        payload,
        document_id=content_id(payload),
        permissions=permissions(),
        asset_refs=tuple(payload["symbols"]),
    )


def test_late_observation_is_not_backdated_to_publication():
    from quantbot.paper_core.external_context import build_context_snapshot

    late = document(retrieved_at=(NOW + timedelta(seconds=1)).isoformat())
    snapshot = build_context_snapshot((late,), symbol="AAPL", at=NOW)
    assert snapshot.observed_document_ids == ()
    assert snapshot.readings == ()
    assert "SOURCE_UNAVAILABLE_AT_DECISION" in snapshot.gaps


def test_future_publication_cannot_be_consumed():
    from quantbot.paper_core.external_context import build_context_snapshot

    future = document(
        published_at=(NOW + timedelta(minutes=1)).isoformat(),
        updated_at=(NOW + timedelta(minutes=1)).isoformat(),
        retrieved_at=(NOW + timedelta(minutes=2)).isoformat(),
    )
    snapshot = build_context_snapshot((future,), symbol="AAPL", at=NOW)
    assert not snapshot.readings


def test_negation_and_multiple_entity_ambiguity_are_explicit():
    from quantbot.paper_core.external_context import build_context_snapshot

    denied = document("AAPL did not beat earnings estimates")
    ambiguous = document(
        "AAPL beats estimates while MSFT misses", identity="2", symbols=["AAPL", "MSFT"]
    )
    snapshot = build_context_snapshot((denied, ambiguous), symbol="AAPL", at=NOW)
    readings = {row.document_id: row for row in snapshot.readings}
    assert readings[denied.document_id].negated_terms == ("beat",)
    assert readings[denied.document_id].polarity < 0
    assert readings[ambiguous.document_id].entity_status == "AMBIGUOUS"
    assert readings[ambiguous.document_id].direction == "UNKNOWN"
    assert readings[ambiguous.document_id].polarity is None


def test_deduplication_and_revisions_select_only_the_available_latest_vintage():
    from quantbot.paper_core.external_context import build_context_snapshot

    old = document()
    revised = document(
        "AAPL misses earnings estimates", updated_at=NOW.isoformat(), retrieved_at=NOW.isoformat()
    )
    future_revision = document(
        "AAPL raises guidance",
        updated_at=(NOW + timedelta(minutes=1)).isoformat(),
        retrieved_at=(NOW + timedelta(minutes=2)).isoformat(),
    )
    snapshot = build_context_snapshot(
        (old, old, revised, future_revision),
        symbol="AAPL",
        at=NOW,
        used_document_ids=(revised.document_id,),
    )
    assert len(snapshot.readings) == 1
    assert snapshot.used_document_ids == (revised.document_id,)
    assert dict(snapshot.features)["polarity_mean"] < 0


def test_observed_context_does_not_claim_to_have_influenced_a_price_only_decision():
    from quantbot.paper_core.external_context import build_context_snapshot

    item = document()
    snapshot = build_context_snapshot((item,), symbol="AAPL", at=NOW)
    assert snapshot.observed_document_ids == (item.document_id,)
    assert snapshot.eligible_document_ids == (item.document_id,)
    assert snapshot.used_document_ids == ()
    assert dict(snapshot.features)["document_count"] == 0
    assert {"X_UNAVAILABLE_IN_THIS_PIPELINE", "REDDIT_UNAVAILABLE_IN_THIS_PIPELINE"} <= set(
        snapshot.gaps
    )


def test_unknown_text_retention_preserves_only_permitted_metadata():
    from quantbot.paper_core.external_context import document_from_research
    from quantbot.research.manifest import content_id

    payload = {
        "kind": "NEWS_RESEARCH_V1",
        "source_id": "alpaca-news:metadata",
        "title": "must not be retained",
        "summary": "private article text",
        "uri": "https://example.invalid/story",
        "publisher": "synthetic",
        "published_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "retrieved_at": NOW.isoformat(),
        "parser": "fixture",
        "parsed_item_sha256": "b" * 64,
    }
    item = document_from_research(
        payload,
        document_id=content_id(payload),
        asset_refs=("AAPL",),
        permissions=permissions(retention="UNKNOWN"),
    )
    assert item.text is None
    assert "must not be retained" not in item.model_dump_json()
    assert item.document_id == content_id(payload)


def test_forged_digest_and_unverified_metadata_retention_are_refused():
    from quantbot.paper_core.external_context import document_from_research

    with pytest.raises(ValueError):
        document_from_research(
            {},
            document_id="f" * 64,
            asset_refs=("AAPL",),
            permissions=permissions(metadata_retention="UNKNOWN"),
        )


def test_redacted_document_cannot_be_marked_used_again():
    from quantbot.paper_core.external_context import build_context_snapshot

    item = document()
    with pytest.raises(ValueError, match="used"):
        build_context_snapshot(
            (item,),
            symbol="AAPL",
            at=NOW,
            used_document_ids=(item.document_id,),
            invalidated_document_ids=(item.document_id,),
        )


def test_processing_permission_is_required_even_for_lexical_term_extraction():
    from quantbot.paper_core.external_context import build_context_snapshot

    item = document().model_copy(update={"permissions": permissions(processing="DENIED")})
    snapshot = build_context_snapshot((item,), symbol="AAPL", at=NOW)
    assert snapshot.readings[0].positive_terms == ()
    assert snapshot.readings[0].event_tags == ()
    assert not any(snapshot.readings[0].feature_values)


def test_snapshot_cannot_claim_future_or_unobserved_sources_were_used():
    from quantbot.paper_core.external_context import ContextSnapshot, build_context_snapshot

    snapshot = build_context_snapshot((document(),), symbol="AAPL", at=NOW)
    with pytest.raises(ValueError):
        ContextSnapshot.model_validate(snapshot.model_dump() | {"used_document_ids": ("f" * 64,)})


def test_persisted_features_cannot_disagree_with_the_used_readings():
    from quantbot.paper_core.external_context import ContextSnapshot, build_context_snapshot

    item = document()
    snapshot = build_context_snapshot(
        (item,), symbol="AAPL", at=NOW, used_document_ids=(item.document_id,)
    )
    changed = tuple(
        (name, -1.0 if name == "polarity_mean" else value) for name, value in snapshot.features
    )
    with pytest.raises(ValueError, match="features"):
        ContextSnapshot.model_validate(snapshot.model_dump() | {"features": changed})
