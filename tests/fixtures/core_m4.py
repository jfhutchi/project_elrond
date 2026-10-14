"""Forty actual synthetic executed/protected/costed episodes; no invented target table."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.fixtures.core_m2 import prepare


class ExecutionClock:
    """Explicit synthetic execution domain, independently advanced from input cutoff."""

    process_id = "m4-execution-fixture"
    execution_domain = "SYNTHETIC_TEST:m4-execution-v1"

    def __init__(self, at):
        self.at = at
        self.elapsed = 0.0

    def sample(self):
        return self.at, self.elapsed

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds


def learning_calendar():
    from quantbot.paper_core.contracts import LearningCalendar
    from quantbot.strategy import XNYSSession, XNYSSessionSequence

    opens = tuple(
        datetime(2026, 10, day, 13, 30, tzinfo=UTC)
        for day in (14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30)
    )
    return LearningCalendar(
        sequence=XNYSSessionSequence(
            calendar="XNYS",
            sessions=tuple(
                XNYSSession(
                    session_date=s.date(), open_at=s, close_at=s + timedelta(hours=6, minutes=30)
                )
                for s in opens
            ),
        )
    )


async def episodes(path, count=40):
    from quantbot.domain import Bar
    from quantbot.learner.five_minute import FEATURE_NAMES, build_features
    from quantbot.paper_core.contracts import BrokerEvent, CostItem, ModelArtifact, Observation
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.research.manifest import content_id
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
        for i in range(count):
            session_index = i // 6 if i < 30 else 5 + (i - 30) // 2
            offset = i % 6 if i < 30 else (i - 30) % 2
            now = sessions[session_index] + timedelta(minutes=35 + offset * 65)
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
            # Known fees are zero for this explicit synthetic provider activity.
            broker.set_mark("XLE", Decimal(100) + Decimal(sign) / 5, as_of=broker.now)
            await guardian_cycle(store, broker, now=broker.now)
            new_fills = broker.fills[-2:]
            costs = []
            for fill in new_fills:
                cost = CostItem(
                    recorded_at_utc=broker.now,
                    fill_id=fill.fill_id,
                    amount=0,
                    source="BROKER_REPORTED",
                    document_id=f"synthetic-fee-{fill.fill_id}",
                    effective_start=now,
                    effective_end=broker.now,
                )
                order = next(
                    o for o in broker.orders.values() if o.broker_order_id == fill.broker_order_id
                )
                costs.append(
                    BrokerEvent(
                        recorded_at_utc=broker.now,
                        occurred_at=broker.now,
                        account_id=store.config.account_id,
                        endpoint="synthetic/fees",
                        client_order_id=order.client_order_id,
                        event_id=cost.id,
                        raw_digest=content_id(cost),
                        cost=cost,
                    )
                )
            import_broker_events(store, tuple(costs))
            (trip,) = process_outcomes(store, cutoff=broker.now).outcomes
            assert trip.net_pnl / trip.entry_notional == Decimal(sign) / 500
            trips.append(trip)
            joins.append(store.export_loop(decision.id))
        parent = ModelArtifact.fixture_constant(-20.0)
        return db, store, broker, parent, tuple(trips), joins
    except BaseException:
        db.close()
        raise
