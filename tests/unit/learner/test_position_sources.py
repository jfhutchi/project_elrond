import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.anomaly.news import AlpacaNewsSource
from quantbot.learner.position_sources import (
    CompanyMapping,
    PositionSources,
    company_mapping_from_payload,
    record_shadow,
    shadow_decision,
)
from quantbot.market_data.instruments import AssetClass, Instrument
from quantbot.research.filings import SecFilingProvider, SecFilingScout
from quantbot.research.sources import Source, SourceKind
from quantbot.storage import StorageRepository
from tests.unit.anomaly.test_news import Response, Transport
from tests.unit.learner.test_paper_runner import context as _context
from tests.unit.research.test_filings import ScriptedTransport

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)


def instrument(symbol='QQQ', asset=AssetClass.ETF):
    return Instrument(instrument_id=symbol+'-listing',symbol=symbol,asset_class=asset,
        venue='NASDAQ',exposure_market='US equity',listed_on=date(2000,1,1))


def mapping(symbol='AAPL'):
    return CompanyMapping(instrument_id=symbol+'-listing', symbol=symbol,cik='0000320193',
        provenance=Source(source_id='synthetic-mapping',kind=SourceKind.MARKET_DATA,
            uri='https://www.sec.gov/files/company_tickers.json',title='SYNTHETIC mapping',
            publisher='SEC',published_at=NOW-timedelta(days=1),retrieved_at=NOW-timedelta(hours=1),
            content_hash='synthetic-hash',parser_version='fixture-v1'))


def news_payload(published=None):
    return {'news':[dict(id='synthetic-1',headline='SYNTHETIC instrument news',source='fixture',
        created_at=(published or NOW-timedelta(minutes=5)).isoformat(),symbols=['QQQ'])]}


def collector(*responses, filings=None, clock=lambda:NOW):
    transport = Transport(*(responses or (Response(200,news_payload()),)))
    return PositionSources(AlpacaNewsSource(transport,api_key='synthetic',api_secret='synthetic'),
        filings=filings, clock=clock), transport


@pytest.mark.asyncio
async def test_etf_queries_only_itself_and_never_assumes_company_or_constituents():
    service, transport = collector()
    snapshot = await service.collect(instrument(), query_end=NOW)
    assert transport.requests[0]['symbols'] == 'QQQ'
    assert snapshot.coverage[1].status == 'NOT_APPLICABLE'
    assert snapshot.coverage[1].scope == 'ETF_COMPANY_FILINGS_NOT_INFERRED'
    assert not snapshot.gaps
    advice = shadow_decision(snapshot,at=NOW,held=True,edge_bps=Decimal(20),
        cost_bps=Decimal(10),hard_loss=False,thesis='SYNTHETIC thesis',forecast_id='synthetic')
    assert advice.action == 'HOLD' and not advice.order_authority


@pytest.mark.asyncio
async def test_company_mapping_is_required_and_mismatch_is_not_queried():
    service, _ = collector()
    snapshot = await service.collect(instrument('AAPL',AssetClass.EQUITY),query_end=NOW)
    assert 'COMPANY_MAPPING_UNVERIFIED' in snapshot.gaps
    with pytest.raises(ValueError,match='mapping'):
        await service.collect(instrument('AAPL',AssetClass.EQUITY),query_end=NOW,
                              company=mapping('MSFT'))
    with pytest.raises(ValueError,match='ETF'):
        await service.collect(instrument(),query_end=NOW,company=mapping('QQQ'))


@pytest.mark.asyncio
@pytest.mark.parametrize('response,expected',[(Response(429,{}),'RATE_LIMITED'),
    (RuntimeError('secret-like transport message'),'TIMEOUT'),
    (Response(200,{}),'STALE')])
async def test_provider_failures_preserve_unknown_not_empty_success(response,expected):
    service, _ = collector(response)
    snapshot = await service.collect(instrument(),query_end=NOW)
    assert snapshot.coverage[0].status == expected
    assert snapshot.gaps and not snapshot.sources
    assert 'secret-like' not in snapshot.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize('published',[NOW+timedelta(seconds=1),NOW-timedelta(days=3)])
async def test_future_or_stale_news_cannot_be_used(published):
    service, _ = collector(Response(200,news_payload(published)))
    snapshot = await service.collect(instrument(),query_end=NOW)
    assert not snapshot.sources and snapshot.gaps


@pytest.mark.asyncio
async def test_actual_post_fetch_retrieval_time_prevents_historical_replay():
    service, _ = collector(clock=lambda:NOW+timedelta(seconds=2))
    snapshot = await service.collect(instrument(),query_end=NOW)
    assert snapshot.sources[0].retrieved_at == NOW+timedelta(seconds=2)
    with pytest.raises(ValueError,match='retrieval'):
        shadow_decision(snapshot,at=NOW,held=True,edge_bps=Decimal(20),
            cost_bps=Decimal(10),hard_loss=False,thesis='synthetic',forecast_id='synthetic')


def filing_xml(day='2026-09-30'):
    return (f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
        f'<accession-number>synthetic-accession</accession-number><filing-date>{day}</filing-date>'
        '<filing-href>https://www.sec.gov/Archives/synthetic</filing-href>'
        '<filing-type>10-K</filing-type></content></entry></feed>').encode()


@pytest.mark.asyncio
@pytest.mark.parametrize('day,usable',[('2026-09-30',True),('2026-10-01',False)])
async def test_sec_is_scoped_to_verified_cik_and_same_day_date_is_not_intraday(day,usable):
    transport = ScriptedTransport(filing_xml(day))
    provider = SecFilingProvider(SecFilingScout(transport,now=lambda:NOW))
    service, _ = collector(filings=provider)
    snapshot = await service.collect(instrument('AAPL',AssetClass.EQUITY),
                                     query_end=NOW,company=mapping())
    assert transport.calls[0][1]['CIK'] == '0000320193'
    assert bool([s for s in snapshot.sources if s.kind is SourceKind.SEC_FILING]) == usable
    if not usable:
        assert 'SEC_INTRADAY_PUBLICATION_TIME_UNKNOWN' in snapshot.gaps


@pytest.mark.asyncio
async def test_unconfigured_or_failed_sec_provider_does_not_claim_no_filings():
    service, _ = collector()
    snapshot = await service.collect(instrument('AAPL',AssetClass.EQUITY),
                                     query_end=NOW,company=mapping())
    assert 'SEC_PROVIDER_NOT_CONFIGURED' in snapshot.gaps
    provider = SecFilingProvider(SecFilingScout(ScriptedTransport(b'not XML'),now=lambda:NOW))
    service, _ = collector(filings=provider)
    snapshot = await service.collect(instrument('AAPL',AssetClass.EQUITY),
                                     query_end=NOW,company=mapping())
    assert snapshot.coverage[1].status == 'UNAVAILABLE'


def test_sec_ticker_artifact_hash_and_unique_symbol_bind_real_mapping():
    payload = json.dumps({'0':dict(ticker='AAPL',cik_str=320193,title='SYNTHETIC')}).encode()
    source = mapping().provenance.model_copy(update={
        'content_hash':hashlib.sha256(payload).hexdigest()})
    company = company_mapping_from_payload(instrument('AAPL',AssetClass.EQUITY),payload,source)
    assert company.cik == '0000320193' and company.provenance == source
    with pytest.raises(ValueError,match='hash'):
        company_mapping_from_payload(instrument('AAPL',AssetClass.EQUITY),payload+b' ',source)
    with pytest.raises(ValueError,match='missing'):
        company_mapping_from_payload(instrument('MSFT',AssetClass.EQUITY),payload,source)
    with pytest.raises(ValueError,match='ETF'):
        company_mapping_from_payload(instrument(),payload,source)


@pytest.mark.asyncio
async def test_evidence_can_go_stale_between_collection_and_shadow_decision():
    service, _ = collector()
    snapshot = await service.collect(instrument(),query_end=NOW)
    record = shadow_decision(snapshot,at=NOW+timedelta(days=3),held=True,
        edge_bps=Decimal(20),cost_bps=Decimal(10),hard_loss=False,
        thesis='SYNTHETIC',forecast_id='synthetic')
    assert record.action == 'WAIT' and 'SOURCE_STALE_AT_DECISION' in record.source_gaps


@pytest.fixture
def context(tmp_path, request):
    yield from _context.__wrapped__(tmp_path, request)


@pytest.mark.asyncio
async def test_synthetic_connector_to_durable_journal_is_idempotent_without_orders(context):
    db, broker, runner, _ = context
    service, _ = collector(clock=lambda:broker.now)
    # All inputs are synthetic. Fixture broker time only seeds the isolated test store.
    service.news = AlpacaNewsSource(Transport(Response(200,news_payload(
        broker.now-timedelta(minutes=5)))),api_key='synthetic',api_secret='synthetic')
    snapshot = await service.collect(instrument(),query_end=broker.now)
    advice = shadow_decision(snapshot,at=broker.now,held=True,edge_bps=Decimal(20),
        cost_bps=Decimal(10),hard_loss=False,thesis='SYNTHETIC thesis',forecast_id='synthetic')
    event = record_shadow(db,run_id='entry-run',identity=runner._identity,
                          snapshot=snapshot,advice=advice)
    assert record_shadow(db,run_id='entry-run',identity=runner._identity,
                         snapshot=snapshot,advice=advice) == event
    with db.transaction() as session:
        repo = StorageRepository(session)
        saved = [s for s in repo.list_signals() if s.signal_id == event]
        assert len(saved) == 1
        assert saved[0].payload['research_inputs']['coverage'][1]['status'] == 'NOT_APPLICABLE'
        assert saved[0].payload['decision']['order_authority'] is False
        assert repo.list_unresolved_order_intents() == []
