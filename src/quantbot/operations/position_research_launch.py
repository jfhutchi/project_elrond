"""Pinned observational PAPER news task; only its separate shadow store is writable."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from quantbot.anomaly.news import AlpacaNewsSource
from quantbot.config import Settings
from quantbot.domain import StrategyIdentity
from quantbot.learner.forward import _read
from quantbot.learner.intraday import IntradayPrediction
from quantbot.learner.models import LearnerModel
from quantbot.learner.position_sources import PositionSources, record_shadow, shadow_decision
from quantbot.market_data.instrument import InstrumentIdentity
from quantbot.market_data.instruments import Instrument
from quantbot.market_data.transports import HttpxMarketDataTransport
from quantbot.operations.service import _git, isolated_application_environment
from quantbot.research.manifest import content_id
from quantbot.storage import Database, SingleWriterLock, StorageRepository


class ShadowConfiguration(LearnerModel):
    targets: tuple[Instrument, ...] = Field(min_length=1,max_length=2)
    identities: tuple[InstrumentIdentity, ...]
    model_id: str
    trading_commit: str = Field(pattern=r'^[a-f0-9]{40}$')
    cost_bps: int = Field(ge=10)
    configured_at: datetime
    order_authority: bool = False

    def verify(self, at: datetime) -> None:
        if self.order_authority or self.configured_at > at:
            raise ValueError('observational configuration required')
        if {p.symbol for p in self.targets} != {'IWM','QQQ'}:
            raise ValueError('approved instrument scope required')
        identities = {i.instrument_id:i for i in self.identities}
        if len(identities) != len(self.targets):
            raise ValueError('unique identity coverage required')
        for target in self.targets:
            identity = identities[target.instrument_id]
            if (target.asset_class.value != 'ETF' or identity.asset_class.value != 'ETF'
                or identity.current_symbol != target.symbol or not identity.source_refs
                or identity.symbol_at('alpaca',at.date()) != target.symbol
                or identity.sec_cik is not None):
                raise ValueError('provenance-backed ETF identity and alias required')


def current_prediction(learner: Path, config: ShadowConfiguration, symbol: str,
                       at: datetime) -> IntradayPrediction | None:
    """Read only today's hash-verified, frozen-model cohort; never create or repair one."""
    worker = json.loads((learner/'worker.json').read_text(encoding='utf-8-sig'))
    if (worker.get('code_commit') != config.trading_commit or not worker.get('finished_at')
        or worker.get('promotion') != 'PAPER_OBSERVATION'
        or not worker.get('intraday',{}).get('predictions_this_run')):
        return None
    finished = datetime.fromisoformat(worker['finished_at'])
    if (finished.tzinfo is None or not timedelta(0) <= at-finished <= timedelta(minutes=5)
        or worker.get('status') not in ('BLOCKED','HEALTHY')):
        return None
    folder = learner/'intraday'/'predictions'/at.date().isoformat()
    for path in sorted(folder.glob('*-'+config.model_id+'.json'),reverse=True)[:2]:
        try:
            document = _read(path)
            if document['model_id'] != config.model_id:
                continue
            for item in document['predictions']:
                prediction = IntradayPrediction.model_validate(item)
                if (prediction.features.symbol == symbol and prediction.model_id == config.model_id
                    and prediction.recorded_at <= at < prediction.features.target_at
                    and timedelta(0) <= at-prediction.features.receipt_at <= timedelta(minutes=5)):
                    return prediction
        except (ValueError,KeyError):
            continue
    return None


async def run_once(directory: Path, learner: Path, config: ShadowConfiguration,
                   settings: Settings, commit: str) -> dict[str, object]:
    now = datetime.now(UTC)
    config.verify(now)
    key, secret = settings.ALPACA_PAPER_API_KEY, settings.ALPACA_PAPER_API_SECRET
    if (settings.TRADING_MODE.value != 'PAPER' or settings.BROKER_ENVIRONMENT.value != 'PAPER'
        or settings.LIVE_TRADING_ACKNOWLEDGED or not key or not secret):
        raise ValueError('existing PAPER access required')
    transport = HttpxMarketDataTransport()
    database = Database(directory/'shadow.db')
    run_id = 'position-research-'+uuid4().hex
    identity = StrategyIdentity(strategy_id='paper-position-research-shadow',version=commit,
        git_commit=commit,configuration_hash=content_id(config),deployment_timestamp=config.configured_at)
    receipt: dict[str,object] = dict(started_at=now.isoformat(),code_commit=commit,
        trading_commit=config.trading_commit,mode='PAPER_RESEARCH_SHADOW',order_authority=False,
        order_requests=0,operational_database_writes=0,synthetic=False,results=[])
    try:
        held: set[str] | None = None
        try:
            response = await transport.request('GET','https://paper-api.alpaca.markets/v2/positions',
                headers={'APCA-API-KEY-ID':key.get_secret_value(),
                         'APCA-API-SECRET-KEY':secret.get_secret_value()},params={},
                timeout_seconds=Decimal('8'))
            if response.status_code == 200:
                payload = json.loads(response.content)
                if isinstance(payload,list) and len(payload)<=1000:
                    held = {str(p['symbol']) for p in payload}
        except Exception:  # noqa: BLE001 -- no raw account/transport errors in receipts
            held = None
        with database.transaction() as session:
            repo = StorageRepository(session)
            repo.create_run(run_id,identity,started_at=now,status='SHADOW_RESEARCH')
        provider = PositionSources(AlpacaNewsSource(transport,api_key=key,api_secret=secret,
                                                   timeout_seconds=Decimal('8')))
        results = []
        for target in config.targets:
            snapshot = await provider.collect(target,query_end=datetime.now(UTC))
            decided_at = datetime.now(UTC)
            try:
                prediction = current_prediction(learner,config,target.symbol,decided_at)
            except (OSError,ValueError,KeyError):
                prediction = None
            advice = shadow_decision(snapshot,at=decided_at,
                held=(target.symbol in held) if held is not None else None,
                edge_bps=Decimal(str(prediction.score))*10000 if prediction else None,
                cost_bps=Decimal(config.cost_bps),hard_loss=False,
                thesis='Frozen forecast with public news context; no semantic trade changes. '
                       'Hard-risk evaluation remains owned by existing protection.',
                forecast_id=prediction.prediction_id if prediction else None)
            event = record_shadow(database,run_id=run_id,identity=identity,snapshot=snapshot,
                                  advice=advice)
            results.append(dict(symbol=target.symbol,event_id=event,action=advice.action,
                reason=advice.reason,forecast_id=advice.forecast_id,edge_bps=str(advice.edge_bps)
                if advice.edge_bps is not None else None,coverage=[c.model_dump(mode='json')
                for c in snapshot.coverage],source_count=len(advice.evidence),
                gaps=advice.source_gaps))
        finished = datetime.now(UTC)
        with database.transaction() as session:
            StorageRepository(session).finish_run(run_id,finished_at=finished,status='SHADOW_RECORDED',
                detail={'order_authority':False,'order_requests':0})
        receipt.update(finished_at=finished.isoformat(),status='SHADOW_RECORDED',results=results)
        temporary = directory/'receipt.json.tmp'
        temporary.write_text(json.dumps(receipt,indent=2),encoding='utf-8')
        temporary.replace(directory/'receipt.json')
        return receipt
    finally:
        await transport.aclose()
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--learner-directory',required=True,type=Path)
    parser.add_argument('--env-file',required=True,type=Path)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args(argv)
    try:
        paths = (args.directory,args.learner_directory,args.env_file)
        if not all(p.is_absolute() and p.exists() for p in paths):
            raise ValueError('existing absolute paths required')
        if args.directory.resolve().is_relative_to(args.learner_directory.resolve()):
            raise ValueError('research outputs must be separate from learner evidence')
        repository = Path(__file__).resolve().parents[3]
        if (_git(repository,'rev-parse','HEAD') != args.commit
            or _git(repository,'status','--porcelain','--untracked-files=all')):
            raise ValueError('clean exact research pin required')
        config = ShadowConfiguration.model_validate_json((args.directory/'config.json').read_text())
        config.verify(datetime.now(UTC))
        with isolated_application_environment(args.env_file):
            settings = Settings()
            if (settings.TRADING_MODE.value != 'PAPER'
                or settings.BROKER_ENVIRONMENT.value != 'PAPER'
                or settings.LIVE_TRADING_ACKNOWLEDGED):
                raise ValueError('PAPER only')
            if not (args.directory/'shadow.db').is_file():
                raise ValueError('explicit shadow-store initialization required')
            if args.check:
                print('POSITION_RESEARCH_CHECK_OK')
                return 0
            with SingleWriterLock(args.directory/'shadow.lock'):
                asyncio.run(asyncio.wait_for(run_once(args.directory,args.learner_directory,
                    config,settings,args.commit),timeout=60))
            print('POSITION_RESEARCH_SHADOW_RECORDED; ORDER_AUTHORITY_FALSE')
            return 0
    except Exception:
        print('POSITION_RESEARCH_REFUSED; NO_ORDER_AUTHORITY')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
