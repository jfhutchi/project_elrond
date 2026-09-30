"""Offline SYNTHETIC provider-to-journal inputs; no credentials, broker or live I/O.

Run from the checkout with PYTHONPATH=src. Real durable writes are tested against
an isolated fixture database; this demonstration only prints synthetic documents.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from quantbot.anomaly.news import AlpacaNewsSource
from quantbot.learner.position_sources import (
    PositionSources,
    company_mapping_from_payload,
    shadow_decision,
)
from quantbot.market_data.instruments import AssetClass, Instrument
from quantbot.research.filings import SecFilingProvider, SecFilingScout
from quantbot.research.sources import Source, SourceKind

NOW = datetime(2026,10,1,14,tzinfo=UTC)


class SyntheticTransport:
    async def request(self, method: str, url: str, *, headers: Any,
                      params: Any, timeout_seconds: Any) -> Any:
        symbol = params['symbols']
        return SimpleNamespace(status_code=200,content=json.dumps({'news':[dict(
            id='SYNTHETIC-'+symbol, headline='SYNTHETIC '+symbol+' announcement',
            source='synthetic-provider',symbols=[symbol],
            created_at=(NOW-timedelta(minutes=5)).isoformat())]}).encode())

    def fetch(self, url: str, params: dict[str,str], *, timeout_seconds: float) -> bytes:
        assert params['CIK'] == '0000320193'
        return (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><content>'
            b'<accession-number>SYNTHETIC</accession-number><filing-date>2026-09-30</filing-date>'
            b'<filing-href>https://www.sec.gov/Archives/SYNTHETIC</filing-href>'
            b'<filing-type>10-K</filing-type></content></entry></feed>')


async def main() -> None:
    transport = SyntheticTransport()
    service = PositionSources(AlpacaNewsSource(transport,api_key='SYNTHETIC',
        api_secret='SYNTHETIC'),filings=SecFilingProvider(SecFilingScout(transport,
        now=lambda:NOW)),clock=lambda:NOW)
    snapshots = []
    for symbol, asset in (('QQQ',AssetClass.ETF),('AAPL',AssetClass.EQUITY)):
        instrument = Instrument(instrument_id='SYNTHETIC-'+symbol,symbol=symbol,
            venue='NASDAQ',asset_class=asset,exposure_market='US equity',listed_on=date(2000,1,1))
        company = None
        if asset is AssetClass.EQUITY:
            payload = json.dumps({'0':dict(ticker='AAPL',cik_str=320193)}).encode()
            provenance = Source(source_id='SYNTHETIC-mapping',kind=SourceKind.MARKET_DATA,
                uri='https://www.sec.gov/files/company_tickers.json',title='SYNTHETIC mapping',
                publisher='SYNTHETIC',published_at=NOW-timedelta(days=1),
                retrieved_at=NOW-timedelta(hours=1),content_hash=hashlib.sha256(payload).hexdigest(),
                parser_version='synthetic-fixture-1')
            company = company_mapping_from_payload(instrument,payload,provenance)
        snapshot = await service.collect(instrument,query_end=NOW,company=company)
        advice = shadow_decision(snapshot,at=NOW,held=True,edge_bps=Decimal('20'),
            cost_bps=Decimal('10'),hard_loss=False,thesis='SYNTHETIC frozen forecast thesis',
            forecast_id='SYNTHETIC-forecast-'+symbol)
        snapshots.append(dict(inputs=snapshot.model_dump(mode='json'),
                              advice=advice.model_dump(mode='json')))
    print(json.dumps(dict(synthetic=True,mode='SOURCE_ONLY_SHADOW',
        actual_provider_requests=0,broker_calls=0,orders_submitted=0,
        pilot_universe_changed=False,examples=snapshots),indent=2))


if __name__ == '__main__':
    asyncio.run(main())
