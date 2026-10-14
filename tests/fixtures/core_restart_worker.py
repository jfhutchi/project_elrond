"""Killed worker: only temporary SQLite and synthetic journal are accessible."""

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from tests.fixtures.core_m2 import NOW, JournalBroker, events, fee_events  # noqa: E402


async def run(path, cut):
    from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation, ProtectionPlan
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes, recover_intents
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import claim_entry, evaluate_entry, record_observation
    from quantbot.storage import Database

    if cut == "outcome_poll":
        from time import monotonic

        with Database(path / "core.db") as db:
            store = CoreStore(db, CoreConfig(account_id="synthetic-account", symbol="XLE"))
            started = monotonic()
            # A healthy fixture worker polls the actual production processor
            # independently; no broker/market/observation object is constructed.
            batch = process_outcomes(store, cutoff=NOW + timedelta(hours=12))
            print(
                "M2_POLL:"
                + json.dumps(
                    {
                        "elapsed_seconds": monotonic() - started,
                        "outcome_ids": [outcome.id for outcome in batch.outcomes],
                        "current": store.documents("core_round_trips"),
                    }
                )
            )
        return
    broker = JournalBroker(path / "broker.json")
    with Database(path / "core.db") as db:
        store = CoreStore(db, CoreConfig(account_id="synthetic-account", symbol="XLE"))
        if cut != "recover":
            observation = Observation(
                symbol="XLE",
                source_start=NOW - timedelta(minutes=5),
                source_end=NOW,
                available_at=NOW,
                recorded_at_utc=NOW,
                price="100",
                raw_data_digest="restart-bar",
                features=(("return", 0.01),),
                dependencies=("SPY",),
                dependency_available_at=(NOW,),
                sequence=1,
                session_close=NOW + timedelta(hours=6),
            )
            record_observation(store, observation)
            decision = evaluate_entry(
                store.config, observation, ModelArtifact(), store.latest_proof()
            )
            if cut == "before_claim":
                os._exit(91)
            wrapper = claim_entry(
                store, decision, ProtectionPlan.for_decision(decision, store.config)
            )
            from decimal import Decimal

            from quantbot.paper_core.guardian import acknowledge_handoffs
            from quantbot.paper_core.runtime import coordinator
            from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision

            acknowledge_handoffs(store, now=NOW)
            if cut == "after_claim":
                os._exit(91)
            original_submit = broker.submit_order
            if cut == "after_order_created":
                from quantbot.domain import IntentState
                from quantbot.storage import StorageRepository

                original_account = broker.get_account

                async def account_at_claim_edge():
                    with db.transaction() as session:
                        stored = StorageRepository(session).get_order_intent(
                            wrapper.intent.intent_id
                        )
                        if stored.state is IntentState.ORDER_CREATED:
                            os._exit(91)
                    return await original_account()

                broker.get_account = account_at_claim_edge

            async def send_then_die(intent):
                result = await original_submit(intent)
                if cut == "after_send":
                    os._exit(91)
                return result

            broker.submit_order = send_then_die
            if cut in {"fill_import", "before_outcome"}:
                from sqlalchemy import event

                def kill_in_transaction(conn, cursor, statement, parameters, context, executemany):
                    table = "fills" if cut == "fill_import" else "core_outcome_revisions"
                    if statement.startswith("INSERT INTO " + table):
                        os._exit(91)

                event.listen(db.engine, "after_cursor_execute", kill_in_transaction)
            await coordinator(store, broker).submit_durable(
                wrapper.intent,
                purpose=OrderPurpose.ENTRY,
                market_eligible=True,
                risk_approved=True,
                drawdown=DrawdownState(
                    current_equity=Decimal(100000),
                    high_water_equity=Decimal(100000),
                    drawdown_fraction=Decimal(0),
                    new_risk_multiplier=Decimal(1),
                    entry_halted=False,
                    liquidation_required=False,
                ),
                vetoes=PortfolioVetoDecision(
                    symbol="XLE",
                    policy_version=1,
                    rule_parameters_hash="EXPLICIT_SYNTHETIC_ASSUMPTIONS",
                    evaluated=(),
                    rejections=(),
                ),
            )
        await recover_intents(store, broker)
        if broker.orders:
            broker.now = NOW + timedelta(minutes=30)
            await guardian_cycle(store, broker, now=broker.now)
            import_broker_events(store, fee_events(store, broker))
        batch = events(store, broker)
        import_broker_events(store, batch)
        import_broker_events(store, batch)
        process_outcomes(store, cutoff=broker.now)
        broker.save()
        if cut != "recover":
            os._exit(91)
        print(
            "M2_RECOVERY:"
            + json.dumps(
                {
                    "posts": len(broker.posts),
                    "current": store.documents("core_round_trips"),
                    "quantities": {s: str(q) for s, q in broker.quantities.items()},
                    "cash": str(broker.cash),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    asyncio.run(run(Path(sys.argv[1]), sys.argv[2]))
