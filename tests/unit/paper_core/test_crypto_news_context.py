"""Exact Alpaca crypto pair tags preserve provider payload and first-seen provenance."""

from datetime import timedelta

import pytest
from tests.unit.learner.test_context_integration import stores
from tests.unit.paper_core.test_external_context import NOW, permissions

from quantbot.paper_core.context_integration import (
    capture_research_context,
    load_context_capture,
    retained_context_documents,
)
from quantbot.research.manifest import canonical_result_json, content_id


def retained_pair(state, tags, *, at=NOW, index_tags=None):
    payload = dict(
        kind="NEWS_RESEARCH_V1",
        source_id="alpaca-news:pair-fixture",
        uri="https://synthetic.invalid/pair",
        publisher="synthetic",
        title="Crypto growth beats estimates",
        summary="",
        symbols=list(tags),
        published_at=(at - timedelta(minutes=5)).isoformat(),
        updated_at=(at - timedelta(minutes=5)).isoformat(),
        retrieved_at=(at - timedelta(minutes=4)).isoformat(),
        parser="synthetic-v1",
        parsed_item_sha256="a" * 64,
    )
    identity = content_id(payload)
    with state.connection() as db:
        db.execute(
            "insert into research_documents values(?,?)", (identity, canonical_result_json(payload))
        )
        for tag in index_tags or tags:
            db.execute(
                "insert into research_symbols values(?,?,?,?,?,?)",
                (
                    tag,
                    identity,
                    payload["source_id"],
                    payload["retrieved_at"],
                    payload["published_at"],
                    payload["kind"],
                ),
            )
    return identity, payload


@pytest.mark.asyncio
async def test_actual_crypto_observation_links_exact_pair_tag_and_rehydrates(tmp_path):
    from tests.unit.paper_core.test_crypto_broker_data import broker_fixture

    from quantbot.paper_core.roles import attach_observed_context

    state, _ = stores(tmp_path)
    broker, _ = broker_fixture()
    cohort = await broker.acquire_crypto_cohort(("BTC/USD",))
    (observation,) = cohort.observations
    at = observation.recorded_at_utc
    identity, payload = retained_pair(state, ("BTCUSD",), at=at)
    with state.connection() as db:
        state._set(db, "context:source-permissions", [permissions().model_dump(mode="json")])
    linked = attach_observed_context(state, observation, at=at)
    assert linked.context_snapshot_id is not None
    capture = load_context_capture(state, linked.context_snapshot_id)
    assert capture.snapshot.observed_document_ids == (identity,)
    assert capture.snapshot.used_document_ids == () and linked.features == observation.features
    binding = capture.bindings[0]
    assert binding.asset_refs == ("BTC/USD",)
    assert binding.provider_asset_refs == ("BTCUSD",)
    assert binding.asset_mapping == "ALPACA_NEWS_USD_PAIR_TAG_V1"
    assert binding.first_seen_at == at - timedelta(minutes=4)
    (document,) = retained_context_documents(state, capture)
    assert document.asset_refs == ("BTC/USD",) and document.first_seen_at == binding.first_seen_at
    with state.connection() as db:
        stored = db.execute(
            "select payload from research_documents where id=?", (identity,)
        ).fetchone()
    assert stored[0] == canonical_result_json(payload)
    assert load_context_capture(state, linked.context_snapshot_id) == capture
    forged = capture.model_copy(
        update={"bindings": (binding.model_copy(update={"asset_refs": ("ETH/USD",)}),)}
    )
    with pytest.raises(ValueError, match="MAPPING_PROVENANCE"):
        retained_context_documents(state, forged)


@pytest.mark.parametrize(
    "pair,tag",
    [("BTC/USD", "BTC"), ("ETH/USD", "ETH"), ("SOL/USD", "SOL"), ("DOGE/USD", "DOGEUSD")],
)
def test_bare_or_unregistered_pair_tags_are_never_guessed(tmp_path, pair, tag):
    state, _ = stores(tmp_path)
    retained_pair(state, (tag,))
    assert (
        capture_research_context(
            state, symbol=pair, at=NOW, permissions={"alpaca-news": permissions()}
        )
        is None
    )


def test_dual_pair_alias_indexes_deduplicate_before_bound_and_keep_original_tags(tmp_path):
    state, _ = stores(tmp_path)
    identity, _ = retained_pair(state, ("ETHUSD", "ETH/USD"))
    snapshot = capture_research_context(
        state,
        symbol="ETH/USD",
        at=NOW,
        permissions={"alpaca-news": permissions()},
        maximum_documents=1,
    )
    assert snapshot is not None
    assert snapshot.observed_document_ids == (identity,)
    assert "CONTEXT_DOCUMENT_BOUND_REACHED" not in snapshot.gaps
    capture = load_context_capture(state, snapshot.snapshot_id)
    assert capture.bindings[0].asset_refs == ("ETH/USD",)
    assert len(retained_context_documents(state, capture)) == 1


def test_existing_unmapped_binding_identity_remains_unchanged(tmp_path):
    from quantbot.paper_core.context_integration import ContextBinding

    state, _ = stores(tmp_path)
    retained_pair(state, ("AAPL",))
    snapshot = capture_research_context(
        state, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    assert snapshot is not None
    binding = load_context_capture(state, snapshot.snapshot_id).bindings[0]
    original = binding.model_dump()
    assert "asset_mapping" not in original and "provider_asset_refs" not in original
    assert content_id(ContextBinding.model_validate(original)) == content_id(original)
