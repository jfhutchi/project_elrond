from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.anomaly.catalyst import CatalystCandidate, SourceHealth, SourceResult
from quantbot.learner.position_research import decision, news_evidence, record_decision
from quantbot.research.sources import Source, SourceKind
from quantbot.research.sources import SourceHealth as ResearchHealth
from quantbot.storage import StorageRepository
from tests.unit.learner.test_paper_runner import context as _context

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)


def article(**changes):
    values = dict(source='news', external_id='1', published_at=NOW-timedelta(minutes=1),
                  retrieved_at=NOW, headline='Public announcement', content_hash='abc',
                  parser='alpaca-news')
    return CatalystCandidate(**(values | changes))


def evaluate(**changes):
    values = dict(symbol='QQQ', at=NOW, thesis='Frozen forecast, ETF-specific coverage only',
                  forecast_id='forecast-1', held=True, edge_bps=Decimal('20'),
                  cost_bps=Decimal('10'), hard_loss=False, sources=(),
                  source_gaps=('SEC company mapping unavailable for ETF',),
                  max_age=timedelta(days=2))
    return decision(**(values | changes))


def test_news_reuses_original_provenance_and_missing_time_is_a_gap():
    result = SourceResult(source='alpaca-news', health=SourceHealth.HEALTHY,
                          candidates=(article(), article(external_id='2', published_at=None)))
    sources, gaps = news_evidence(result)
    assert len(sources) == 1 and sources[0].content_hash == 'abc'
    assert sources[0].published_at < sources[0].retrieved_at
    assert gaps == ('PUBLICATION_TIME_UNKNOWN',)


def test_outage_is_not_no_news():
    sources, gaps = news_evidence(SourceResult(source='news', health=SourceHealth.UNAVAILABLE,
                                              detail='unavailable'))
    assert not sources and gaps == ('SOURCE_UNAVAILABLE',)


@pytest.mark.parametrize('held,edge,expected', [(True,'20','HOLD'), (True,'5','SELL'),
                                               (False,'20','BUY'), (False,'5','WAIT')])
def test_remaining_edge_after_cost_not_mark_profit_drives_advice(held, edge, expected):
    sources, _ = news_evidence(SourceResult(source='news', health=SourceHealth.HEALTHY,
                                            candidates=(article(),)))
    record = evaluate(held=held, edge_bps=Decimal(edge), sources=sources, source_gaps=())
    assert record.action == expected and not record.order_authority


def test_source_gaps_cannot_become_confident_advice_and_loss_limit_wins():
    assert evaluate().action == 'WAIT'
    record = evaluate(hard_loss=True, edge_bps=Decimal('999'))
    assert record.action == 'SELL' and record.reason == 'HARD_RISK_LIMIT'
    assert not record.order_authority


@pytest.mark.parametrize('field,value', [('published_at',NOW+timedelta(seconds=1)),
    ('retrieved_at',NOW+timedelta(seconds=1)), ('published_at',NOW-timedelta(days=3))])
def test_future_retrieval_or_old_publication_never_back_advice(field,value):
    source = Source(source_id='s',kind=SourceKind.NEWS,uri='https://example.org/news',
        title='announcement',publisher='publisher',published_at=NOW-timedelta(minutes=1),
        retrieved_at=NOW,content_hash='abc',parser_version='1')
    record = evaluate(sources=(source.model_copy(update={field:value}),), source_gaps=())
    assert record.action == 'WAIT' and not record.evidence
    assert 'SOURCE_NOT_USABLE_AS_OF_DECISION' in record.source_gaps


def test_nonfinite_edge_and_naive_decision_are_rejected():
    with pytest.raises(ValueError):
        evaluate(edge_bps=Decimal('NaN'))
    with pytest.raises(ValueError):
        evaluate(at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize('changes', [dict(health=ResearchHealth.STALE),
    dict(derived=True,derived_from='original'),
    dict(published_at=NOW.replace(tzinfo=None)), dict(kind=SourceKind.PAPER)])
def test_unhealthy_summary_undated_or_unrelated_source_is_not_decision_evidence(changes):
    source = Source(source_id='filing',kind=SourceKind.SEC_FILING,
        uri='https://www.sec.gov/Archives/example', title='Filing',publisher='SEC',
        published_at=NOW-timedelta(minutes=1),retrieved_at=NOW,
        content_hash='filing-hash',parser_version='1')
    record = evaluate(sources=(source.model_copy(update=changes),), source_gaps=())
    assert record.action == 'WAIT' and not record.evidence


def test_existing_filing_provenance_is_preserved_without_fiscal_period_inference():
    source = Source(source_id='filing',kind=SourceKind.SEC_FILING,
        uri='https://www.sec.gov/Archives/example',title='Filing',publisher='SEC',
        published_at=NOW-timedelta(minutes=1),retrieved_at=NOW,
        content_hash='filing-hash',parser_version='1')
    record = evaluate(symbol='AAPL', sources=(source,), source_gaps=())
    assert record.evidence == (source,) and record.action == 'HOLD'
    assert not record.order_authority


def test_loss_limit_without_position_cannot_recommend_phantom_sale():
    assert evaluate(held=False,hard_loss=True).action == 'WAIT'


@pytest.fixture
def context(tmp_path, request):
    yield from _context.__wrapped__(tmp_path, request)


def test_journal_is_idempotent_durable_and_creates_no_order(context):
    db, broker, runner, _ = context
    record = evaluate(at=broker.now)
    first = record_decision(db, run_id='entry-run', identity=runner._identity, record=record)
    assert record_decision(db, run_id='entry-run', identity=runner._identity,
                           record=record) == first
    with db.transaction() as session:
        repo = StorageRepository(session)
        events = [s for s in repo.list_signals()
                  if s.payload.get('kind') == 'PAPER_RESEARCH_DECISION_V1']
        assert len(events) == 1 and events[0].payload['decision']['thesis'] == record.thesis
        assert not repo.list_unresolved_order_intents()
