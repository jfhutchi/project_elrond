"""Retained protection assessment with a GET-only broker and no submitter."""
import asyncio,json,os
from dataclasses import replace
from datetime import UTC,datetime
from decimal import Decimal
from pathlib import Path
from quantbot.config import Settings
from quantbot.operations.service import isolated_application_environment
from quantbot.runtime import build_runtime
from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.transports import HttpxBrokerTransport
from quantbot.storage import SingleWriterLock
from quantbot.learner.paper_deadlines import supervisor_objections
class GET(HttpxBrokerTransport):
 async def request(self,method,url,**kw):
  assert method=='GET' and url.startswith('https://paper-api.alpaca.markets/')
  return await super().request(method,url,**kw)
async def main():
 with isolated_application_environment('C:/Users/leepa/.config/quantbot/quantbot.env'):
  os.environ['QUANTBOT_GIT_COMMIT']='14352cc428dcd999750138d52fbd5f30347991a1'
  s=Settings();runtime=build_runtime(s);t=GET()
  try:
   b=AlpacaPaperBrokerAdapter(api_key=s.ALPACA_PAPER_API_KEY,api_secret=s.ALPACA_PAPER_API_SECRET,transport=t,max_retries=0,timeout_seconds=Decimal(2))
   async with asyncio.timeout(6):
    with SingleWriterLock(runtime.paths.lock):
     r=await replace(runtime.protector(),submitter=None,broker=b).evaluate(now=datetime.now(UTC))
   result=dict(at=datetime.now(UTC).isoformat(),outcome=r.outcome,unprotected_count=r.unprotected_count,
       assessment=r.model_dump(mode='json'),deadline_objections=supervisor_objections(runtime.database,account_id=s.EXPECTED_ACCOUNT_ID,now=datetime.now(UTC)))
   # Keep the full assessment local; print only the safety outcome.
   Path('D:/Elrond/review/submission-20261015/private-protection-assessment.json').write_text(json.dumps(result,indent=2))
   print(json.dumps({k:v for k,v in result.items() if k!='assessment'}))
  finally:
   await t.aclose();await runtime.aclose();runtime.database.close()
asyncio.run(main())
