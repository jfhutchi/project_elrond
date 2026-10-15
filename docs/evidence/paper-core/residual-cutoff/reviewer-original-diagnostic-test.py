import asyncio,json,sys
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
sys.path.insert(0,str(Path.cwd()))
from sqlalchemy import select
from tests.fixtures.core_m2 import NOW,prepare,fee_events
from quantbot.paper_core import outcomes
from quantbot.paper_core.guardian import guardian_cycle,synchronize
from quantbot.paper_core.trader import trader_cycle
from quantbot.paper_core.contracts import RoundTrip,LearningPolicy
from quantbot.paper_core.store import table
from quantbot.paper_core.learning import _select

def test_cost_after_poll_cutoff_before_version_capture_is_not_lost(tmp_path):
    async def run():
        db,store,broker,proof,observation,model=prepare(tmp_path)
        try:
            decision=await trader_cycle(store,broker,observation,model,proof,now=NOW)
            broker.now=NOW+timedelta(minutes=30)
            await guardian_cycle(store,broker,now=broker.now)
            zero=fee_events(store,broker)
            outcomes.import_broker_events(store,zero)
            await synchronize(store,broker,now=broker.now)
            outcomes.process_outcomes(store,cutoff=broker.now)
            original=RoundTrip.model_validate(store.documents('core_round_trips')[0])
            assert original.status=='COMPLETE' and original.actual_fees==0
            cutoff=broker.now
            broker.now+=timedelta(seconds=1)
            outcomes.import_broker_events(store,fee_events(store,broker,amount='0.10',revision=2,supersedes={e.cost.fill_id:e.cost.id for e in zero}))
            def state():
                with db.transaction() as session:
                    return dict(session.execute(select(table('core_outcome_work')).where(table('core_outcome_work').c.lot_id==decision.id)).mappings().one())
            dirty=state()
            stale=outcomes.process_outcomes(store,cutoff=cutoff)
            after_stale=state()
            fresh=outcomes.process_outcomes(store,cutoff=broker.now)
            current=RoundTrip.model_validate(store.documents('core_round_trips')[0])
            direct=outcomes.reconcile_round_trip(store,decision.id,as_of=broker.now)
            with db.transaction() as session:
                eligible,excluded,_=_select(session,LearningPolicy(parent_model=model, dataset=original.dataset, policy_id=original.policy_id, evidence_lane=original.evidence_lane),NOW.replace(hour=21),execution_now=NOW.replace(hour=21))
            print('REVIEW_CUTOFF_LOST_NOTIFICATION:'+json.dumps({'lot_id':decision.id,'original_outcome':original.id,'dirty_before':dirty,'after_stale_cutoff':after_stale,'after_current_poll':state(),'stale_batch_count':len(stale.outcomes),'fresh_batch_count':len(fresh.outcomes),'current_outcome':current.id,'current_status':current.status,'current_actual_fees':str(current.actual_fees),'current_net':str(current.net_pnl),'direct_current_fees':str(direct.actual_fees),'direct_current_outcome':direct.id,'eligible_ids':[r['outcome_id'] for r in eligible],'excluded':excluded},default=str))
            assert dirty['version']>dirty['processed_version']
            assert after_stale['version']==after_stale['processed_version']
            assert not fresh.outcomes and current.id==original.id
            assert direct.actual_fees==Decimal('0.20') and current.actual_fees==0
        finally: db.close()
    asyncio.run(run())

