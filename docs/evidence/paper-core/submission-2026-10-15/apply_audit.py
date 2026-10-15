"""Append the exact reviewed historical refusal after fencing, with GET-only corroboration."""
import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from audit_refusal import INTENT, validate, append
from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.transports import HttpxBrokerTransport
from quantbot.config import Settings, validate_trading_authority
from quantbot.operations.service import isolated_application_environment
from quantbot.paper_core.release import read_status
from quantbot.research.manifest import content_id
from quantbot.storage import Database, StorageRepository

ROOT=Path('D:/Elrond/review/submission-20261015')
DB=Path('D:/Elrond/state/quantbot-paper-fresh-20260908.db')

class GET(HttpxBrokerTransport):
    async def request(self,method,url,**kwargs):
        assert method=='GET' and url.startswith('https://paper-api.alpaca.markets/')
        return await super().request(method,url,**kwargs)

async def main():
    stopped=json.loads((ROOT/'old-role-fence.json').read_text())
    assert stopped['remaining_core_processes']==[]
    assert 0 <= (datetime.now(UTC)-datetime.fromisoformat(stopped['at'])).total_seconds() <= 120
    fence=json.loads((ROOT/'maintenance-fence.json').read_text())
    c=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True)
    detail=validate(c)
    backup_path=ROOT/'pre-audit-backup.db'
    backup_path.touch(exist_ok=False)
    backup=sqlite3.connect(backup_path)
    try:
        c.backup(backup)
        assert backup.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    finally: backup.close();c.close()
    (ROOT/'pre-audit-backup.json').write_text(json.dumps(dict(
        at=datetime.now(UTC).isoformat(), file=backup_path.name,
        sha256=hashlib.sha256(backup_path.read_bytes()).hexdigest(),integrity='ok'
    ),indent=2))
    db=Database(DB)
    try:
        with db.transaction() as session:
            intent=StorageRepository(session).get_order_intent(INTENT)
        status=read_status(DB)
        with isolated_application_environment('C:/Users/leepa/.config/quantbot/quantbot.env'):
            settings=Settings()
            assert settings.TRADING_MODE.value=='PAPER'
            assert validate_trading_authority(settings,settings.EXPECTED_ACCOUNT_ID).allowed
            assert settings.EXPECTED_ACCOUNT_ID==status['commissions'][0]['account_id']
            transport=GET()
            broker=AlpacaPaperBrokerAdapter(api_key=settings.ALPACA_PAPER_API_KEY,
                api_secret=settings.ALPACA_PAPER_API_SECRET,transport=transport,
                max_retries=0,timeout_seconds=Decimal(2))
            try:
                async with asyncio.timeout(12):
                    account=await broker.get_account()
                    assert account.account.account_id==settings.EXPECTED_ACCOUNT_ID
                    first=await broker.get_order_by_client_id(intent.client_order_id,expected=intent)
                    positions=await broker.get_positions()
                    activities=await broker.get_performance_activities(after=intent.created_at,until=datetime.now(UTC))
                    second=await broker.get_order_by_client_id(intent.client_order_id,expected=intent)
                    assert first is None and second is None
                    assert not any(p.symbol==intent.symbol and p.quantity for p in positions)
                    assert not any(a.get('activity_type')=='FILL' and a.get('symbol')==intent.symbol for a in activities)
            finally: await transport.aclose()
        now=datetime.now(UTC)
        assert 0 <= (now-datetime.fromisoformat(stopped['at'])).total_seconds() <= 120
        probe=dict(at=now.isoformat(),intent_id=INTENT,account_verified=True,
            exact_client_absent_twice=True,symbol_position_absent=True,symbol_fill_since_intent_absent=True,
            transport='PAPER_GET_ONLY',activities_digest=content_id(activities),fence_id=fence['fence_id'])
        identity=content_id(probe)
        (ROOT/'historical-broker-corroboration.json').write_text(json.dumps(probe,indent=2))
        with db.transaction() as session:
            session.execute(text('BEGIN IMMEDIATE'))
            event_id=append(session,detail,now=now,fence_id=fence['fence_id'],broker_probe_id=identity)
        result=dict(at=now.isoformat(),intent_id=INTENT,event_id=event_id,
            reconstruction=detail,broker_probe_id=identity,state_preserved='ERROR',original_client_preserved=True)
        (ROOT/'historical-refusal-reconstruction.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({'reconstructed_event_id':event_id,'order_state':'ERROR'}))
    finally: db.close()

if __name__=='__main__': asyncio.run(main())
