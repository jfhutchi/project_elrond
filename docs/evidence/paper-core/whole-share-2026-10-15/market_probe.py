"""GET-only market cohort diagnostics; no operating store or order mutations."""
import asyncio,json
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from pathlib import Path
from quantbot.config import Settings
from quantbot.operations.service import isolated_application_environment
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.transports import HttpxMarketDataTransport
from quantbot.market_data.base import BarQuery,MarketDataFeed,AdjustmentMode

class GET(HttpxMarketDataTransport):
 async def request(self,method,url,**kw):
  assert method=='GET' and url=='https://data.alpaca.markets/v2/stocks/bars'
  return await super().request(method,url,**kw)
async def main():
 with isolated_application_environment('C:/Users/leepa/.config/quantbot/quantbot.env'):
  s=Settings();t=GET();now=datetime.now(UTC);end=now.replace(second=0,microsecond=0)-timedelta(minutes=now.minute%5)
  m=AlpacaMarketDataClient(api_key=s.ALPACA_PAPER_API_KEY,api_secret=s.ALPACA_PAPER_API_SECRET,transport=t,max_retries=0,timeout_seconds=Decimal(2))
  try:
   batch=await m.get_historical_bars(BarQuery(symbols=('AAPL','SPY'),start=end-timedelta(minutes=40),end=end-timedelta(microseconds=1),timeframe='5Min',feed=MarketDataFeed.IEX,adjustment=AdjustmentMode.ALL,limit=100))
   result=dict(at=now.isoformat(),cutoff=end.isoformat(),seconds_since_cutoff=(now-end).total_seconds(),bars={s:[b.timestamp.isoformat() for b in batch.bars if b.symbol==s] for s in ('AAPL','SPY')})
   Path('D:/Elrond/review/whole-share-20261015/market-probe.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
  finally:await t.aclose()
asyncio.run(main())
