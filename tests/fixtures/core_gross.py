"""Sixty synthetic executed gross outcomes with unknown fees and adjacent windows.

Legacy selection supplies compatible historical outcomes for V3 learning. Independent
synthetic provider cash activity enables later entries; this does not claim that the
Alpaca adapter acquires such cash evidence or that the operational loop is ready.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.fixtures.core_m2 import prepare


async def gross_episodes(path):
    from quantbot.domain import Bar, OrderSide
    from quantbot.learner.five_minute import FEATURE_NAMES, build_features
    from quantbot.paper_core.contracts import (
        BrokerEvent,
        CashActivity,
        ModelArtifact,
        Observation,
        RoundTrip,
    )
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.strategy import XNYSSession, XNYSSessionSequence

    db, store, broker, proof, _, collect = prepare(path)
    collect = ModelArtifact(feature_version="five-minute-price-alpha-v1")
    sessions = [
        datetime(2026, 10, 14, 13, 30, tzinfo=UTC) + timedelta(days=d)
        for d in (0, 1, 2, 5, 6, 7, 8, 9, 12, 13)
    ]
    calendar = XNYSSessionSequence(
        calendar="XNYS",
        sessions=tuple(
            XNYSSession(
                session_date=s.date(), open_at=s, close_at=s + timedelta(hours=6, minutes=30)
            )
            for s in sessions
        ),
    )
    trips, joins = [], []
    try:
        for i in range(60):
            session_index, offset = divmod(i, 6)
            now = sessions[session_index] + timedelta(minutes=35 + offset * 35)
            sign = 1 if i % 2 == 0 else -1
            broker.now = now
            broker.set_mark("XLE", Decimal(100), as_of=now)
            await guardian_cycle(store, broker, now=now)
            proof = store.latest_proof()
            bars = []
            for k in range(7):
                for symbol in ("XLE", "SPY"):
                    value = Decimal(100)
                    if symbol == "XLE" and k == 5:
                        value = Decimal(100) / (Decimal(1) + Decimal(sign) / 500)
                    bars.append(
                        Bar(
                            symbol=symbol,
                            timestamp=now - timedelta(minutes=35 - k * 5),
                            open=value,
                            high=value,
                            low=value,
                            close=value,
                            volume=1,
                            adjustment="1",
                        )
                    )
            (row,) = build_features(
                bars,
                calendar,
                decision_at=now,
                receipt_at=now,
                symbols=("XLE",),
                source_snapshot_hash=f"m4-causal-bars-{i}",
            )
            observation = Observation(
                recorded_at_utc=now,
                symbol="XLE",
                source_start=now - timedelta(minutes=35),
                source_end=now,
                available_at=now,
                price=100,
                raw_data_digest=row.source_snapshot_hash,
                feature_version="five-minute-price-alpha-v1",
                features=tuple(zip(FEATURE_NAMES, row.values, strict=True)),
                dependencies=("SPY",),
                dependency_available_at=(now,),
                sequence=i + 1,
                session_close=sessions[session_index] + timedelta(hours=6, minutes=30),
            )
            decision = await trader_cycle(store, broker, observation, collect, proof, now=now)
            assert decision.action == "BUY", decision.gates
            if i == 0:
                posted = len(broker.posts)
                assert (
                    await trader_cycle(store, broker, observation, collect, proof, now=now)
                    == decision
                )
                assert len(broker.posts) == posted
            broker.now = now + timedelta(minutes=30)
            # Provider supplied no fee evidence: the closed gross outcome stays pending.
            broker.set_mark("XLE", Decimal(100) + Decimal(sign) / 5, as_of=broker.now)
            await guardian_cycle(store, broker, now=broker.now)
            trip = RoundTrip.model_validate(store.export_loop(decision.id)["round_trip"])
            assert trip.status == "COSTS_PENDING" and trip.cost_reason == "FEE_UNAVAILABLE"
            assert trip.net_pnl is None and trip.actual_fees is None
            assert trip.gross_pnl / trip.entry_notional == Decimal(sign) / 500
            activity = CashActivity(
                recorded_at_utc=broker.now,
                account_id=store.config.account_id,
                document_id=f"gross-cash-{i}",
                activity_ids=tuple(f.fill_id for f in broker.fills),
                movements=tuple(
                    (f.fill_id, f.quantity * f.price * (-1 if f.side is OrderSide.BUY else 1))
                    for f in broker.fills
                ),
                complete=True,
                watermark=broker.now,
            )
            import_broker_events(
                store,
                (
                    BrokerEvent(
                        recorded_at_utc=broker.now,
                        occurred_at=broker.now,
                        account_id=store.config.account_id,
                        endpoint="synthetic/cash-activities",
                        event_id=activity.id,
                        raw_digest=activity.id,
                        client_order_id=broker.posts[-1].client_order_id,
                        cash_activity=activity,
                    ),
                ),
            )
            process_outcomes(store, cutoff=broker.now)
            trip = RoundTrip.model_validate(store.export_loop(decision.id)["round_trip"])
            trips.append(trip)
            joins.append(store.export_loop(decision.id))
        parent = collect
        return db, store, broker, parent, tuple(trips), joins
    except BaseException:
        db.close()
        raise
