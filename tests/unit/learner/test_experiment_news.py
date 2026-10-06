from datetime import timedelta
from importlib import import_module

import pytest

from tests.fixtures.adaptive_paper import manifest, snapshot


def news():
    return import_module("quantbot.learner.experiment_news")


def document(**updates):
    module, now = news(), snapshot().received_at
    return module.NewsEvidence(
        **(
            {
                "experiment_id": manifest().experiment_id,
                "symbol": "SYN0",
                "asset_id": "synthetic-asset-0",
                "recorded_at": now,
                "published_at": now - timedelta(minutes=5),
                "first_seen_at": now - timedelta(seconds=10),
                "retrieved_at": now,
                "provider": "SYNTHETIC",
                "source_url": "https://synthetic.invalid/story",
                "source_hash": "a" * 64,
                "headline": "Synthetic story",
                "status": "OK",
                "scope": "EXACT_INSTRUMENT_QUERY",
                "synthetic": True,
            }
            | updates
        )
    )


def test_existing_position_sources_available():
    sources = import_module("quantbot.learner.position_sources")
    assert callable(sources.PositionSources.collect)


def test_late_news_excluded():
    module, now = news(), snapshot().received_at
    late = document(retrieved_at=now + timedelta(seconds=1), recorded_at=now + timedelta(seconds=1))
    assert module.build_news_evidence(manifest().roster[0], (late,), decision_at=now) == ()
    assert module.build_news_evidence(manifest().roster[0], (document(),), decision_at=now)


def test_stale_and_unknown_news_not_used_as_zero_information():
    module, now = news(), snapshot().received_at
    stale = document(published_at=now - timedelta(days=3))
    failed = document(
        status="UNAVAILABLE",
        source_hash=None,
        source_url=None,
        published_at=None,
        first_seen_at=None,
    )
    assert module.build_news_evidence(manifest().roster[0], (stale, failed), decision_at=now) == ()
    assert failed.complete_public_coverage is False
    assert failed.status == "UNAVAILABLE"


def test_etf_company_filings_not_inferred():
    module, now = news(), snapshot().received_at
    etf = manifest().roster[0].model_copy(update={"asset_class": "ETF"})
    filing = document(kind="SEC_FILING", scope="COMPANY", company_id="synthetic-company")
    with pytest.raises(ValueError, match="ETF"):
        module.build_news_evidence(etf, (filing,), decision_at=now)


def test_identity_mismatch_and_future_publication_not_accepted():
    module, now = news(), snapshot().received_at
    wrong = document(asset_id="different")
    with pytest.raises(ValueError, match="IDENTITY"):
        module.build_news_evidence(manifest().roster[0], (wrong,), decision_at=now)
    with pytest.raises(ValueError):
        document(published_at=now + timedelta(seconds=1))


def test_thesis_journal_is_shadow_and_deduplicates(tmp_path):
    module = news()
    from quantbot.learner.experiment_contract import DecisionRecord
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore

    source = snapshot()
    batch = build_observation_batch(manifest(), source, now=source.received_at)
    decision = DecisionRecord(
        experiment_id=manifest().experiment_id,
        recorded_at=source.received_at,
        model_id="f" * 64,
        features=batch.features[0],
        score=0,
        action="WAIT",
        reason="BELOW_COST_FLOOR",
        synthetic=True,
    )
    store = ExperimentStore(tmp_path / "news.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    store.append(decision, idempotency_key=decision.decision_id)
    first = module.journal_thesis(store, decision, (document(),))
    second = module.journal_thesis(store, decision, (document(),))
    assert first == second
    assert first.order_authority is False
    assert first.decision_id == decision.decision_id
    rows = store.read_as_of(manifest().experiment_id, source.received_at)
    assert len(rows) == 4
    assert (
        next(row for row in rows if row.kind == "NewsEvidence")
        .decode(module.NewsEvidence)
        .source_hash
        == "a" * 64
    )


def provider_snapshot(*, failed=False):
    from datetime import date

    from quantbot.learner.position_sources import PositionResearchSnapshot, ProviderCoverage
    from quantbot.market_data.instruments import Instrument
    from quantbot.research.sources import Source, SourceKind

    now = snapshot().received_at
    identity = manifest().roster[0]
    sources = (
        ()
        if failed
        else (
            Source(
                source_id="synthetic-source",
                kind=SourceKind.NEWS,
                uri="https://synthetic.invalid/story",
                title="Synthetic public story",
                publisher="Synthetic provider",
                published_at=now - timedelta(minutes=5),
                retrieved_at=now,
                content_hash="a" * 64,
                parser_version="synthetic-v1",
            ),
        )
    )
    return PositionResearchSnapshot(
        instrument=Instrument(
            instrument_id=identity.asset_id,
            symbol=identity.symbol,
            venue="SYNTHETIC",
            asset_class="EQUITY",
            exposure_market="US equity",
            listed_on=date(2020, 1, 1),
        ),
        company=None,
        query_end=now,
        collected_at=now,
        news_max_age=timedelta(hours=48),
        filing_max_age=timedelta(days=365),
        sources=sources,
        gaps=(),
        coverage=(
            ProviderCoverage(
                provider="synthetic-news",
                query=identity.symbol,
                scope="EXACT_INSTRUMENT_NEWS_QUERY",
                status="UNAVAILABLE" if failed else "HEALTHY",
                retrieved_at=now,
                returned_count=len(sources),
                usable_count=len(sources),
            ),
        ),
    )


def test_existing_provider_snapshot_connected_with_actual_availability():
    module, source = news(), provider_snapshot()
    evidence = module.evidence_from_position_snapshot(
        manifest(), source, recorded_at=source.collected_at
    )
    row = next(row for row in evidence if row.status == "OK")
    assert row.asset_id == manifest().roster[0].asset_id
    assert row.first_seen_at == row.retrieved_at == source.collected_at
    assert row.source_hash == source.sources[0].content_hash
    assert row.synthetic


def test_provider_failure_stays_unknown_and_mapping_mismatch_refused():
    module, source = news(), provider_snapshot(failed=True)
    evidence = module.evidence_from_position_snapshot(
        manifest(), source, recorded_at=source.collected_at
    )
    assert {row.status for row in evidence} == {"UNAVAILABLE"}
    assert (
        module.build_news_evidence(manifest().roster[0], evidence, decision_at=source.collected_at)
        == ()
    )
    wrong = source.model_copy(
        update={"instrument": source.instrument.model_copy(update={"instrument_id": "different"})}
    )
    with pytest.raises(ValueError, match="IDENTITY"):
        module.evidence_from_position_snapshot(manifest(), wrong, recorded_at=source.collected_at)


@pytest.fixture
def provider_journal(tmp_path):
    from decimal import Decimal

    from quantbot.domain import StrategyIdentity
    from quantbot.learner.position_sources import record_shadow, shadow_decision
    from quantbot.storage import Database, StorageRepository

    source = provider_snapshot()
    database = Database.create(tmp_path / "provider.db")
    identity = StrategyIdentity(
        strategy_id="paper-position-research-shadow",
        version="b" * 40,
        git_commit="b" * 40,
        configuration_hash="c" * 64,
        deployment_timestamp=source.collected_at,
    )
    with database.transaction() as session:
        StorageRepository(session).create_run(
            "synthetic-research", identity, started_at=source.collected_at, status="SHADOW"
        )
    advice = shadow_decision(
        source,
        at=source.collected_at,
        held=None,
        edge_bps=None,
        cost_bps=Decimal(10),
        hard_loss=False,
        thesis="Synthetic only",
        forecast_id=None,
    )
    record_shadow(
        database, run_id="synthetic-research", identity=identity, snapshot=source, advice=advice
    )
    database.close()
    return tmp_path / "provider.db", source


def test_existing_provider_journal_readonly_hash_verified_bridge(provider_journal):
    import hashlib

    path, source = provider_journal
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = news().read_position_journal(
        path, manifest(), now=source.collected_at, expected_commit="b" * 40
    )
    assert any(row.status == "OK" and row.source_hash == "a" * 64 for row in rows)
    assert len({row.symbol for row in rows}) == 6
    assert sum(row.status == "UNAVAILABLE" for row in rows) == 5
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    future = news().read_position_journal(
        path, manifest(), now=source.collected_at - timedelta(seconds=1), expected_commit="b" * 40
    )
    assert all(row.status == "UNAVAILABLE" for row in future)
    stale = news().read_position_journal(
        path, manifest(), now=source.collected_at + timedelta(minutes=11), expected_commit="b" * 40
    )
    assert (
        news().build_news_evidence(
            manifest().roster[0], stale[:2], decision_at=source.collected_at + timedelta(minutes=11)
        )
        == ()
    )


def test_provider_journal_wrong_pin_or_tamper_is_unknown(provider_journal):
    import sqlite3

    path, source = provider_journal
    assert all(
        row.status == "UNAVAILABLE"
        for row in news().read_position_journal(
            path, manifest(), now=source.collected_at, expected_commit="c" * 40
        )
    )
    with sqlite3.connect(path) as db:
        db.execute("UPDATE signals SET signal_id='tampered'")
    rows = news().read_position_journal(
        path, manifest(), now=source.collected_at, expected_commit="b" * 40
    )
    assert all(row.status == "UNAVAILABLE" for row in rows)


def test_thesis_cannot_use_news_after_feature_decision_even_if_journal_is_later(tmp_path):
    from quantbot.learner.experiment_contract import DecisionRecord
    from quantbot.learner.experiment_observation import build_observation_batch
    from quantbot.learner.experiment_store import ExperimentStore

    source = snapshot()
    observation = build_observation_batch(manifest(), source, now=source.received_at)
    decision = DecisionRecord(
        experiment_id=manifest().experiment_id,
        recorded_at=source.received_at + timedelta(seconds=5),
        model_id="f" * 64,
        features=observation.features[0],
        score=0.02,
        action="BUY",
        reason="SHADOW",
        synthetic=True,
    )
    store = ExperimentStore(tmp_path / "thesis.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    late = document(
        first_seen_at=source.received_at + timedelta(seconds=1),
        retrieved_at=source.received_at + timedelta(seconds=1),
        recorded_at=source.received_at + timedelta(seconds=1),
    )
    thesis = news().journal_thesis(store, decision, (late,))
    assert thesis.action == "WAIT"
    assert thesis.evidence_ids == ()


def test_bridge_preserves_verified_prior_availability_for_later_decisions(provider_journal):
    path, source = provider_journal
    now = source.collected_at + timedelta(minutes=1)
    rows = news().read_position_journal(path, manifest(), now=now, expected_commit="b" * 40)
    prior = [row for row in rows if row.symbol == manifest().roster[0].symbol]
    assert any(row.recorded_at == source.collected_at for row in prior)
    usable = news().build_news_evidence(
        manifest().roster[0], prior, decision_at=source.collected_at + timedelta(seconds=30)
    )
    assert len(usable) == 1
    assert usable[0].first_seen_at == source.collected_at
