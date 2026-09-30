"""Bounded public-provider inputs for a position's PAPER shadow journal.

No broker, universe mutation, model promotion, or order submission lives here.
The news query is one instrument, not a inferred list of ETF constituents.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from quantbot.anomaly.news import AlpacaNewsSource
from quantbot.domain import StrategyIdentity
from quantbot.learner.models import LearnerModel
from quantbot.learner.position_research import (
    ResearchDecision,
    decision,
    news_evidence,
    record_decision,
)
from quantbot.market_data.instruments import AssetClass, Instrument
from quantbot.market_data.pointintime import Capability
from quantbot.research.filings import SecFilingProvider
from quantbot.research.sources import Source, SourceHealth, SourceKind
from quantbot.storage import Database


class CompanyMapping(LearnerModel):
    instrument_id: str
    symbol: str
    cik: str = Field(pattern=r'^\d{10}$')
    provenance: Source


def company_mapping_from_payload(instrument: Instrument, payload: bytes,
                                 provenance: Source) -> CompanyMapping:
    """Parse an already retrieved SEC ticker artifact, without fetching or guessing CIKs."""
    if instrument.asset_class is not AssetClass.EQUITY:
        raise ValueError('ETF/company identity cannot be inferred from a ticker table')
    if (len(payload) > 8_000_000 or not _sec_uri(provenance.uri)
        or urlsplit(provenance.uri).path != '/files/company_tickers.json'
        or provenance.content_hash != hashlib.sha256(payload).hexdigest()):
        raise ValueError('company mapping provenance or payload hash invalid')
    document = json.loads(payload)
    if not isinstance(document,dict):
        raise ValueError('company mapping payload invalid')
    matches = [v for v in document.values() if isinstance(v,dict)
               and v.get('ticker') == instrument.symbol]
    if len(matches) != 1:
        raise ValueError('company mapping missing or ambiguous')
    cik = matches[0].get('cik_str')
    if isinstance(cik,bool) or not isinstance(cik,int) or not 0 < cik < 10**10:
        raise ValueError('company mapping CIK invalid')
    return CompanyMapping(instrument_id=instrument.instrument_id,symbol=instrument.symbol,
                          cik=str(cik).zfill(10),provenance=provenance)


class ProviderCoverage(LearnerModel):
    provider: str
    query: str
    scope: str
    status: str
    retrieved_at: datetime
    returned_count: int
    usable_count: int
    complete_public_coverage: Literal[False] = False


class PositionResearchSnapshot(LearnerModel):
    instrument: Instrument
    company: CompanyMapping | None
    query_end: datetime
    collected_at: datetime
    news_max_age: timedelta
    filing_max_age: timedelta
    sources: tuple[Source, ...]
    gaps: tuple[str, ...]
    coverage: tuple[ProviderCoverage, ...]


def _aware(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('research clock must be timezone-aware')


def _sec_uri(uri: str) -> bool:
    parsed = urlsplit(uri)
    return parsed.scheme == 'https' and parsed.hostname in ('www.sec.gov','sec.gov')


class PositionSources:
    def __init__(self, news: AlpacaNewsSource, *, filings: SecFilingProvider | None = None,
                 clock: Callable[[], datetime] = lambda:datetime.now(UTC),
                 news_max_age: timedelta = timedelta(hours=48),
                 filing_max_age: timedelta = timedelta(days=365),
                 filing_form: Literal['10-K','10-Q','8-K'] = '10-K') -> None:
        if min(news_max_age,filing_max_age) <= timedelta(0):
            raise ValueError('research freshness windows must be positive')
        self.news, self.filings, self.clock = news, filings, clock
        self.news_age, self.filing_age, self.form = news_max_age, filing_max_age, filing_form

    async def collect(self, instrument: Instrument, *, query_end: datetime,
                      company: CompanyMapping | None = None) -> PositionResearchSnapshot:
        _aware(query_end)
        if instrument.asset_class not in (AssetClass.ETF,AssetClass.EQUITY):
            raise ValueError('position research slice supports equities and ETFs only')
        if not instrument.tradable_on(query_end.date()):
            raise ValueError('instrument is not listed at query time')
        if company is not None:
            if instrument.asset_class is AssetClass.ETF:
                raise ValueError('ETF company or constituent mapping must not be inferred')
            provenance = company.provenance
            for at in (provenance.published_at,provenance.retrieved_at):
                _aware(at)
            if (company.instrument_id != instrument.instrument_id
                or company.symbol != instrument.symbol
                or provenance.health is not SourceHealth.OK or provenance.derived
                or provenance.kind is not SourceKind.MARKET_DATA or not _sec_uri(provenance.uri)
                or not provenance.published_at <= provenance.retrieved_at <= query_end):
                raise ValueError('company mapping is mismatched or not verified as of query')
        sources: list[Source] = []
        gaps: list[str] = []
        coverage: list[ProviderCoverage] = []
        try:
            result = await asyncio.wait_for(self.news.fetch((instrument.symbol,),
                window_start=query_end-self.news_age, observed_at=query_end,
                retrieved_at=query_end),timeout=15)
            received = self.clock()
            _aware(received)
            # The existing adapter's caller-supplied retrieval timestamp predates I/O.
            # Keep publication untouched; measure actual completion, never backdate availability.
            result = result.model_copy(update={'candidates':tuple(item.model_copy(
                update={'retrieved_at':received}) for item in result.candidates)})
            candidates, news_gaps = news_evidence(result)
            gaps.extend(news_gaps)
            accepted = [s for s in candidates
                        if query_end-self.news_age <= s.published_at <= query_end]
            if len(accepted) != len(candidates):
                gaps.append('NEWS_STALE_OR_AFTER_QUERY')
            sources.extend(accepted)
            coverage.append(ProviderCoverage(provider=self.news.name,query=instrument.symbol,
                scope='EXACT_INSTRUMENT_NEWS_QUERY',status=result.health.value,
                retrieved_at=received,returned_count=len(result.candidates),usable_count=len(accepted)))
        except Exception:  # noqa: BLE001 -- failure is unknown; no raw exception/credential logging
            received = self.clock()
            _aware(received)
            gaps.append('NEWS_PROVIDER_UNAVAILABLE')
            coverage.append(ProviderCoverage(provider=self.news.name,query=instrument.symbol,
                scope='EXACT_INSTRUMENT_NEWS_QUERY',status='UNAVAILABLE',retrieved_at=received,
                returned_count=0,usable_count=0))
        if instrument.asset_class is AssetClass.ETF:
            status, scope = 'NOT_APPLICABLE','ETF_COMPANY_FILINGS_NOT_INFERRED'
        elif company is None:
            status, scope = 'UNKNOWN','COMPANY_MAPPING_UNVERIFIED'
            gaps.append(scope)
        elif self.filings is None:
            status, scope = 'UNAVAILABLE','SEC_PROVIDER_NOT_CONFIGURED'
            gaps.append(scope)
        else:
            dataset = company.cik+':'+self.form
            try:
                observations = await asyncio.wait_for(asyncio.to_thread(self.filings.observations,
                    dataset,capability=Capability.FUNDAMENTALS,retrieved_at=query_end),timeout=35)
                received = self.clock()
                _aware(received)
                accepted = []
                for observation in observations:
                    source = observation.value
                    if not isinstance(source,Source):
                        gaps.append('SEC_SOURCE_INVALID')
                        continue
                    # The existing EDGAR Atom connector only knows a filing DATE.
                    # A same-day midnight timestamp cannot establish intraday publication.
                    if source.published_at.date() >= query_end.astimezone(UTC).date():
                        gaps.append('SEC_INTRADAY_PUBLICATION_TIME_UNKNOWN')
                        continue
                    if (source.kind is not SourceKind.SEC_FILING or source.derived
                        or source.health is not SourceHealth.OK or not _sec_uri(source.uri)
                        or source.publisher != 'SEC EDGAR (CIK '+company.cik+')'
                        or not query_end-self.filing_age <= source.published_at <= query_end
                        or observation.availability.available_at > query_end):
                        gaps.append('SEC_SOURCE_NOT_USABLE_AS_OF_QUERY')
                        continue
                    accepted.append(source.model_copy(update={'retrieved_at':received}))
                if not observations:
                    gaps.append('NO_FILINGS_IN_BOUNDED_QUERY_NOT_COMPLETE_COVERAGE')
                sources.extend(accepted)
                coverage.append(ProviderCoverage(provider='sec-edgar',query=dataset,
                    scope='VERIFIED_COMPANY_CIK_ONE_FORM',status='HEALTHY',retrieved_at=received,
                    returned_count=len(observations),usable_count=len(accepted)))
            except Exception:  # noqa: BLE001 -- do not leak transport or contact details
                received = self.clock()
                _aware(received)
                gaps.append('SEC_PROVIDER_UNAVAILABLE')
                coverage.append(ProviderCoverage(provider='sec-edgar',query=dataset,
                    scope='VERIFIED_COMPANY_CIK_ONE_FORM',status='UNAVAILABLE',retrieved_at=received,
                    returned_count=0,usable_count=0))
            status = scope = ''
        if scope:
            coverage.append(ProviderCoverage(provider='sec-edgar',query=instrument.symbol,
                scope=scope,status=status,retrieved_at=received,returned_count=0,usable_count=0))
        finished = self.clock()
        _aware(finished)
        if finished < query_end or any(c.retrieved_at > finished for c in coverage):
            raise ValueError('research collection clock regressed')
        return PositionResearchSnapshot(instrument=instrument,company=company,query_end=query_end,
            collected_at=finished,news_max_age=self.news_age,filing_max_age=self.filing_age,
            sources=tuple(sources),gaps=tuple(sorted(set(gaps))),coverage=tuple(coverage))


def shadow_decision(snapshot: PositionResearchSnapshot, *, at: datetime, held: bool,
                    edge_bps: Decimal, cost_bps: Decimal, hard_loss: bool,
                    thesis: str, forecast_id: str) -> ResearchDecision:
    _aware(at)
    if at < snapshot.collected_at:
        raise ValueError('decision cannot precede actual provider retrieval')
    sources = []
    gaps = list(snapshot.gaps)
    for source in snapshot.sources:
        age = snapshot.news_max_age if source.kind is SourceKind.NEWS else snapshot.filing_max_age
        if at-source.published_at <= age:
            sources.append(source)
        else:
            gaps.append('SOURCE_STALE_AT_DECISION')
    return decision(symbol=snapshot.instrument.symbol,at=at,held=held,edge_bps=edge_bps,
        cost_bps=cost_bps,hard_loss=hard_loss,thesis=thesis,forecast_id=forecast_id,
        sources=tuple(sources),source_gaps=tuple(gaps),
        max_age=max(snapshot.news_max_age,snapshot.filing_max_age))


def record_shadow(database: Database, *, run_id: str, identity: StrategyIdentity,
                  snapshot: PositionResearchSnapshot, advice: ResearchDecision) -> str:
    """Durably retain query scope/coverage alongside advice, with no order-writing path."""
    if advice.symbol != snapshot.instrument.symbol or advice.at < snapshot.collected_at:
        raise ValueError('shadow advice does not match collected instrument/retrieval')
    return record_decision(database,run_id=run_id,identity=identity,record=advice,
                           research_inputs=snapshot.model_dump(mode='json'))
