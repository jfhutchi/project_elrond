"""Explicit synthetic in-memory broker. No transport, endpoint, credentials or adapter factory."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from quantbot.brokers import BrokerAccountSnapshot
from quantbot.domain import Account, BrokerOrder, Fill, OrderIntent, OrderSide, Position

if TYPE_CHECKING:
    from quantbot.paper_core.time_evidence import BrokerTime


class SyntheticBroker:
    provider = "EXPLICIT_SYNTHETIC"
    environment = "PAPER"

    def __init__(
        self,
        *,
        account_id: str,
        now: datetime,
        inherited: dict[str, Decimal] | None = None,
        initial_account: Account | None = None,
        initial_positions: tuple[Position, ...] = (),
    ) -> None:
        self.account_id = account_id
        self.now = now
        self.initial_account = initial_account or Account(
            account_id=account_id,
            cash=Decimal(100000),
            buying_power=Decimal(100000),
            equity=Decimal(100000),
            currency="USD",
        )
        if self.initial_account.account_id != account_id:
            raise ValueError("synthetic account mismatch")
        self.cash = self.initial_account.cash
        self.quantities = (
            {p.symbol: p.quantity for p in initial_positions}
            if initial_positions
            else dict(inherited or {})
        )
        self.basis = {s: Decimal(100) for s in self.quantities}
        self.basis.update({p.symbol: p.average_entry_price for p in initial_positions})
        self.initial_value = (
            sum((p.market_value for p in initial_positions), Decimal(0))
            if initial_positions
            else sum((q * Decimal(100) for q in self.quantities.values()), Decimal(0))
        )
        self.marks: dict[str, tuple[Decimal, datetime]] = {}
        self.marks.update({p.symbol: (p.market_price, now) for p in initial_positions})
        self.orders: dict[str, BrokerOrder] = {}
        self.fills: list[Fill] = []
        self.posts: list[OrderIntent] = []

    def set_mark(self, symbol: str, price: Decimal, *, as_of: datetime) -> None:
        self.marks[symbol] = (price, as_of)

    def mark(self, symbol: str) -> tuple[Decimal, datetime] | None:
        return self.marks.get(symbol)

    def snapshot(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            account=Account(
                account_id=self.account_id,
                cash=self.cash,
                buying_power=self.cash,
                equity=self.initial_account.equity
                + self.cash
                - self.initial_account.cash
                + sum(
                    (
                        q * self.marks.get(s, (Decimal(100), self.now))[0]
                        for s, q in self.quantities.items()
                    ),
                    Decimal(0),
                )
                - self.initial_value,
                currency="USD",
            ),
            account_number="SYNTHETIC",
            status="ACTIVE",
            order_entry_allowed=True,
            restrictions=(),
        )

    async def get_account(self) -> BrokerAccountSnapshot:
        return self.snapshot()

    async def get_clock(self) -> BrokerTime:
        from quantbot.brokers.base import BrokerProtocolError

        raise BrokerProtocolError("synthetic fixture requires an explicit advancing provider clock")

    def position_snapshot(self) -> tuple[Position, ...]:
        return tuple(
            Position(
                symbol=s,
                quantity=q,
                market_price=self.marks.get(s, (Decimal(100), self.now))[0],
                average_entry_price=self.basis[s],
                market_value=q * self.marks.get(s, (Decimal(100), self.now))[0],
            )
            for s, q in sorted(self.quantities.items())
            if q != 0
        )

    async def get_positions(self) -> tuple[Position, ...]:
        return self.position_snapshot()

    async def get_position(self, symbol: str) -> Position | None:
        return next((p for p in await self.get_positions() if p.symbol == symbol), None)

    async def get_open_orders(self) -> tuple[BrokerOrder, ...]:
        return ()

    async def get_order_by_client_id(
        self, client_order_id: str, *, expected: OrderIntent | None = None
    ) -> BrokerOrder | None:
        return self.orders.get(client_order_id)

    async def get_fills(self, *, after: datetime | None = None) -> tuple[Fill, ...]:
        return tuple(f for f in self.fills if after is None or f.occurred_at > after)

    async def get_activity_watermark(self) -> datetime | None:
        """The synchronous synthetic journal is complete through its current clock."""
        return self.now

    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        if intent.client_order_id in self.orders:
            raise RuntimeError("duplicate synthetic POST")
        if (
            intent.side is OrderSide.SELL
            and self.quantities.get(intent.symbol, Decimal(0)) < intent.quantity
        ):
            raise RuntimeError("synthetic oversell")
        price = self.marks.get(intent.symbol, (Decimal(100), self.now))[0]
        sign = Decimal(1) if intent.side is OrderSide.BUY else Decimal(-1)
        previous = self.quantities.get(intent.symbol, Decimal(0))
        if intent.side is OrderSide.BUY:
            self.basis[intent.symbol] = (
                previous * self.basis.get(intent.symbol, price) + intent.quantity * price
            ) / (previous + intent.quantity)
        self.quantities[intent.symbol] = previous + sign * intent.quantity
        self.cash -= sign * intent.quantity * price
        order = BrokerOrder(
            broker_order_id="synthetic-" + intent.client_order_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            quantity=intent.quantity,
            filled_quantity=intent.quantity,
            status="filled",
            filled_average_price=price,
            submitted_at=self.now,
        )
        self.orders[intent.client_order_id] = order
        self.fills.append(
            Fill(
                fill_id="synthetic-fill-" + intent.client_order_id,
                broker_order_id=order.broker_order_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                price=price,
                occurred_at=self.now,
            )
        )
        self.posts.append(intent)
        return order
