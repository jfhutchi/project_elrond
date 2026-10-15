import asyncio,json
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from pathlib import Path
from quantbot.config import Settings
from quantbot.operations.service import isolated_application_environment
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.transports import HttpxMarketDataTransport

async def main():
 with isolated_application_environment('C:/Users/leepa/.config/quantbot/quantbot.env'):
  s=Settings();t=HttpxMarketDataTransport();now=datetime.now(UTC)
  m=AlpacaMarketDataClient(api_key=s.ALPACA_PAPER_API_KEY,api_secret=s.ALPACA_PAPER_API_SECRET,transport=t,max_retries=0,timeout_seconds=Decimal(2))
  results=[]
  try:
   for feed,timeframe,adjustment,latest in [('iex','5Min','all',False),('iex','5Min','raw',False),('iex','1Min','raw',False),('sip','1Min','raw',False),('iex','1Min','raw',True)]:
    url='https://data.alpaca.markets/v2/stocks/bars'+('/latest' if latest else '')
    params=dict(symbols='AAPL,SPY',feed=feed)
    if not latest:params.update(timeframe=timeframe,start=(now-timedelta(minutes=45)).isoformat(),end=now.isoformat(),limit='1000',sort='asc',adjustment=adjustment)
    r=await t.request('GET',url,params=params,headers=m._headers,timeout_seconds=Decimal(2))
    p=json.loads(r.content)
    bars=p.get('bars',{})
    result=dict(feed=feed,timeframe=timeframe,adjustment=adjustment,latest=latest,status=r.status_code,
        headers={k:v for k,v in r.headers.items() if k.lower() in ('date','age','cache-control','x-cache','x-request-id')},
        times={s:([b['t'] for b in v][-5:] if isinstance(v,list) else v.get('t')) for s,v in bars.items()})
    results.append(result)
   document=dict(at=now.isoformat(),results=results)
   Path('D:/Elrond/review/submission-20261015/feed-probe.json').write_text(json.dumps(document,indent=2));print(json.dumps(document))
  finally:await t.aclose()
asyncio.run(main())
