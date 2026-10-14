"""Explicit credential-free process fixture using the durable synthetic broker journal."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantbot.domain import Account, Position
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.paper_core.contracts import LearningCalendar, Observation
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.time_evidence import BrokerTime
from quantbot.storage import StorageRepository
from quantbot.strategy import XNYSSession, XNYSSessionSequence


class ProcessBroker(SyntheticBroker):
    def __init__(self, store, config):
        baseline = store.documents("core_fixture_baselines")[0]
        self.store = store
        self.runtime_config = config
        self.session_open = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        self.session_close = self.session_open + timedelta(hours=23, minutes=59)
        close_file = store.database.path.parent / "session-close.json"
        if close_file.exists():
            self.session_close = datetime.fromisoformat(json.loads(close_file.read_text()))
        super().__init__(
            account_id=store.config.account_id,
            now=store.clocks.sample()[0],
            initial_account=Account.model_validate(baseline["account"]),
            initial_positions=tuple(Position.model_validate(p) for p in baseline["positions"]),
        )
        self.refresh_fixture(store)

    def refresh_fixture(self, store):
        self.now = store.clocks.sample()[0]
        with store.database.transaction() as session:
            repo = StorageRepository(session)
            self.orders = {o.client_order_id: o for o in repo.list_broker_orders()}
            self.fills = list(repo.list_fills())
            quantities, cash, _ = store.ledger(session)
        self.quantities = quantities
        for symbol, quantity in quantities.items():
            if quantity:
                owned = [f for f in self.fills if f.symbol == symbol and f.side.value == "BUY"]
                if owned:
                    self.basis[symbol] = sum(f.price * f.quantity for f in owned) / sum(
                        f.quantity for f in owned
                    )
        self.cash = cash if cash is not None else self.snapshot().account.cash
        self.set_mark(store.config.symbol, Decimal(100), as_of=self.now)

    async def get_clock(self):
        import asyncio

        if (
            self.store.database.path.parent / "clock-failure"
        ).exists() and self.store.runtime_role == "trader":
            raise TimeoutError("EXPLICIT_FIXTURE_TRADER_CLOCK_FAILURE")
        await asyncio.sleep(0.001)
        self.now = self.store.clocks.sample()[0]
        return BrokerTime(
            timestamp=self.now,
            account_id=self.account_id,
            provider=self.provider,
            session_id=self.session_open.date().isoformat(),
            session_open=self.session_open,
            session_close=self.session_close,
            resolution_seconds=0.000001,
        )

    async def observation(self):
        now = self.store.clocks.sample()[0]
        return Observation(
            recorded_at_utc=now,
            release_id=self.store.config.release_id,
            symbol=self.store.config.symbol,
            price=100,
            source_start=now - timedelta(minutes=35),
            source_end=now,
            available_at=now,
            raw_data_digest="explicit-process-bars-" + now.isoformat(),
            feature_version="five-minute-price-alpha-v1",
            features=tuple((n, 0.0) for n in FEATURE_NAMES),
            dependencies=("SPY",),
            dependency_available_at=(now,),
            sequence=int(now.timestamp()),
            session_open=self.session_open,
            session_close=self.session_close,
        )

    async def submit_order(self, intent):
        self.now = self.store.clocks.sample()[0]
        return await super().submit_order(intent)

    async def learning_calendar(self):
        dates = [self.session_open - timedelta(days=days) for days in (3, 2, 1)]
        return LearningCalendar(
            sequence=XNYSSessionSequence(
                calendar="XNYS",
                sessions=tuple(
                    XNYSSession(session_date=d.date(), open_at=d, close_at=d + timedelta(hours=20))
                    for d in dates
                ),
            )
        )


def broker(store, config):
    return ProcessBroker(store, config)
