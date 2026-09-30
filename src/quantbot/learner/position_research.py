"""PAPER research advice journal, deliberately disconnected from order authority.

Callers must query the actual instrument: ETF news is not evidence about every
constituent. Publication and retrieval must both precede the decision. This
records a supplied frozen forecast/thesis; it does not infer alpha from a headline.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from quantbot.anomaly.catalyst import SourceHealth as NewsHealth
from quantbot.anomaly.catalyst import SourceResult
from quantbot.domain import StrategyIdentity
from quantbot.learner.models import LearnerModel
from quantbot.research.manifest import content_id
from quantbot.research.sources import Source, SourceHealth, SourceKind
from quantbot.storage import Database, StorageRepository


class ResearchDecision(LearnerModel):
    symbol: str
    at: datetime
    thesis: str
    forecast_id: str
    held: bool
    edge_bps: Decimal
    cost_bps: Decimal
    action: Literal['BUY', 'HOLD', 'SELL', 'WAIT']
    reason: str
    evidence: tuple[Source, ...]
    source_gaps: tuple[str, ...]
    order_authority: Literal[False] = False


def news_evidence(result: SourceResult) -> tuple[tuple[Source, ...], tuple[str, ...]]:
    """Adapt the existing news parser without re-dating or inventing coverage."""
    if result.health is not NewsHealth.HEALTHY:
        return (), ('SOURCE_' + result.health.value,)
    sources = []
    gaps = []
    for item in result.candidates:
        if item.published_at is None:
            gaps.append('PUBLICATION_TIME_UNKNOWN')
            continue
        if item.published_at > item.retrieved_at:
            gaps.append('SOURCE_TIMESTAMP_INVALID')
            continue
        sources.append(Source(source_id=content_id({'provider':result.source,
            'id':item.external_id, 'hash':item.content_hash}), kind=SourceKind.NEWS,
            uri=item.url or ('urn:news:' + content_id(
                {'source':item.source,'id':item.external_id})),
            title=item.headline, publisher=item.source, published_at=item.published_at,
            retrieved_at=item.retrieved_at, content_hash=item.content_hash,
            parser_version=str(item.parser_version)))
    if not result.candidates:
        gaps.append('NO_ITEMS_IN_BOUNDED_QUERY_NOT_COMPLETE_PUBLIC_COVERAGE')
    return tuple(sources), tuple(sorted(set(gaps)))


def decision(*, symbol: str, at: datetime, thesis: str, forecast_id: str, held: bool,
             edge_bps: Decimal, cost_bps: Decimal, hard_loss: bool,
             sources: tuple[Source, ...], source_gaps: tuple[str, ...],
             max_age: timedelta) -> ResearchDecision:
    """Log edge after costs and evidence availability; hard loss always takes priority.

No mark-profit input exists: a positive mark alone is not a sell thesis. Advice
is never a veto override, a position handoff, or a model-promotion decision.
"""
    if (at.tzinfo is None or at.utcoffset() is None or max_age <= timedelta(0)
            or not edge_bps.is_finite() or not cost_bps.is_finite() or cost_bps < 0
            or not symbol.strip() or not thesis.strip() or not forecast_id.strip()):
        raise ValueError('invalid research decision inputs')
    accepted = []
    gaps = list(source_gaps)
    for source in sources:
        moments = (source.published_at, source.retrieved_at)
        usable = (all(t.tzinfo is not None and t.utcoffset() is not None for t in moments)
            and source.health is SourceHealth.OK and not source.derived
            and source.published_at <= source.retrieved_at <= at
            and at-source.published_at <= max_age
            and source.kind in (SourceKind.NEWS, SourceKind.SEC_FILING))
        if usable:
            accepted.append(source)
        else:
            gaps.append('SOURCE_NOT_USABLE_AS_OF_DECISION')
    action: Literal['BUY', 'HOLD', 'SELL', 'WAIT']
    if held and hard_loss:
        action, reason = 'SELL', 'HARD_RISK_LIMIT'
    elif gaps or not accepted:
        action, reason = 'WAIT', 'RESEARCH_COVERAGE_INCOMPLETE'
    elif edge_bps > cost_bps:
        action, reason = ('HOLD' if held else 'BUY'), 'FROZEN_FORECAST_EDGE_AFTER_COSTS'
    else:
        action, reason = ('SELL' if held else 'WAIT'), 'NO_REMAINING_EDGE_AFTER_COSTS'
    return ResearchDecision(symbol=symbol, at=at, thesis=thesis, forecast_id=forecast_id,
        held=held, edge_bps=edge_bps, cost_bps=cost_bps, action=action, reason=reason,
        evidence=tuple(accepted), source_gaps=tuple(sorted(set(gaps))))


def record_decision(database: Database, *, run_id: str, identity: StrategyIdentity,
                    record: ResearchDecision,
                    research_inputs: dict[str, object] | None = None) -> str:
    """Append idempotently to the existing durable signal ledger; no schema change."""
    event_id = 'paper-research-' + content_id({'run':run_id,'strategy':identity.strategy_id,
        'version':identity.version,'decision':record,'research_inputs':research_inputs})
    with database.transaction() as session:
        repo = StorageRepository(session)
        if event_id not in {s.signal_id for s in repo.list_signals()}:
            repo.record_signal(event_id, run_id=run_id, strategy_id=identity.strategy_id,
                symbol=record.symbol, occurred_at=record.at,
                payload={'kind':'PAPER_RESEARCH_DECISION_V1',
                         'decision':record.model_dump(mode='json'),
                         'research_inputs':research_inputs})
    return event_id
