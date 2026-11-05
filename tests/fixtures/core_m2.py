"""Credential-free provider journal and fixture for M2 restart tests."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantbot.domain import BrokerOrder, Fill, OrderIntent, OrderSide
from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation
from quantbot.paper_core.runtime import initialize_fixture
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore
from quantbot.storage import Database

NOW = datetime(2026, 10, 14, 14, tzinfo=UTC)


class JournalBroker(SyntheticBroker):
    """Provider side effects survive the application process, including lost ack."""

    def __init__(self, path: Path) -> None:
        super().__init__(account_id="synthetic-account", now=NOW, inherited={"SPY": Decimal(2)})
        self.path = path
        self.partial: Decimal | None = None
        if path.exists():
            state = json.loads(path.read_text())
            self.now = datetime.fromisoformat(state["now"])
            self.cash = Decimal(state["cash"])
            self.quantities = {s: Decimal(q) for s, q in state["quantities"].items()}
            self.basis = {s: Decimal(q) for s, q in state["basis"].items()}
            self.orders = {k: BrokerOrder.model_validate(o) for k, o in state["orders"].items()}
            self.fills = [Fill.model_validate(f) for f in state["fills"]]
            self.posts = [OrderIntent.model_validate(i) for i in state["posts"]]

    def save(self):
        self.path.write_text(
            json.dumps(
                {
                    "now": self.now.isoformat(),
                    "cash": str(self.cash),
                    "quantities": {s: str(q) for s, q in self.quantities.items()},
                    "basis": {s: str(q) for s, q in self.basis.items()},
                    "orders": {k: o.model_dump(mode="json") for k, o in self.orders.items()},
                    "fills": [f.model_dump(mode="json") for f in self.fills],
                    "posts": [i.model_dump(mode="json") for i in self.posts],
                }
            )
        )

    async def submit_order(self, intent):
        order = await super().submit_order(intent)
        if self.partial is not None:
            full = self.fills.pop()
            self.quantities[intent.symbol] -= (intent.quantity - self.partial) * (
                1 if intent.side is OrderSide.BUY else -1
            )
            self.cash += (
                (intent.quantity - self.partial)
                * full.price
                * (1 if intent.side is OrderSide.BUY else -1)
            )
            self.fills.append(full.model_copy(update={"quantity": self.partial}))
            order = order.model_copy(
                update={"filled_quantity": self.partial, "status": "partially_filled"}
            )
            self.orders[intent.client_order_id] = order
        self.save()
        return order

    async def get_open_orders(self):
        return tuple(o for o in self.orders.values() if o.status == "partially_filled")


def prepare(path: Path):
    config = CoreConfig(account_id="synthetic-account", symbol="XLE")
    broker = JournalBroker(path / "broker.json")
    db = Database.create(path / "core.db")
    store = CoreStore(db, config)
    proof = initialize_fixture(store, broker, now=NOW, entry_enabled=True)
    observation = Observation(
        symbol="XLE",
        source_start=NOW - timedelta(minutes=5),
        source_end=NOW,
        available_at=NOW,
        recorded_at_utc=NOW,
        price=Decimal(100),
        raw_data_digest="m2-bar",
        features=(("return", 0.01),),
        dependencies=("SPY",),
        dependency_available_at=(NOW,),
        sequence=1,
        session_close=NOW + timedelta(hours=6),
    )
    broker.save()
    return db, store, broker, proof, observation, ModelArtifact()


def events(store, broker):
    from quantbot.paper_core.contracts import BrokerEvent
    from quantbot.research.manifest import content_id

    result = []
    for order in broker.orders.values():
        fills = tuple(f for f in broker.fills if f.broker_order_id == order.broker_order_id)
        result.append(
            BrokerEvent(
                recorded_at_utc=broker.now,
                occurred_at=broker.now,
                account_id=store.config.account_id,
                provider=broker.provider,
                endpoint="synthetic/order-and-complete-activities",
                event_id=content_id(order),
                raw_digest=content_id(order),
                client_order_id=order.client_order_id,
                order=order,
                activity_complete=True,
                activity_watermark=broker.now,
                activity_fill_ids=tuple(f.fill_id for f in fills),
            )
        )
        for fill in fills:
            result.append(
                BrokerEvent(
                    recorded_at_utc=broker.now,
                    occurred_at=fill.occurred_at,
                    account_id=store.config.account_id,
                    provider=broker.provider,
                    endpoint="synthetic/activities",
                    event_id=fill.fill_id,
                    raw_digest=content_id(fill),
                    client_order_id=order.client_order_id,
                    fill=fill,
                )
            )
    return tuple(result)


def fee_events(store, broker, *, amount="0", revision=1, supersedes=None, effective_start=NOW):
    from quantbot.paper_core.contracts import BrokerEvent, CostItem
    from quantbot.research.manifest import content_id

    result = []
    for fill in broker.fills:
        cost = CostItem(
            recorded_at_utc=broker.now,
            fill_id=fill.fill_id,
            amount=Decimal(amount),
            source="BROKER_REPORTED",
            document_id="synthetic-fee-activity",
            effective_start=effective_start,
            effective_end=effective_start + timedelta(days=1),
            revision=revision,
            supersedes=(supersedes or {}).get(fill.fill_id),
        )
        order = next(o for o in broker.orders.values() if o.broker_order_id == fill.broker_order_id)
        result.append(
            BrokerEvent(
                recorded_at_utc=broker.now,
                occurred_at=broker.now,
                account_id=store.config.account_id,
                provider=broker.provider,
                endpoint="synthetic/fees",
                event_id=cost.id,
                raw_digest=content_id(cost),
                client_order_id=order.client_order_id,
                cost=cost,
            )
        )
    return tuple(result)
