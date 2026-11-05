"""Explicit offline asset/session contracts; these never authorize broker PAPER."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from quantbot.brokers.base import BrokerModel
from quantbot.domain import TimeInForce
from quantbot.market_data.calendar import crypto_sessions
from quantbot.research.manifest import content_id

if TYPE_CHECKING:
    from quantbot.paper_core.contracts import CoreConfig, Observation


def account_risk_day(at: datetime) -> date:
    """One account day, independent of the instrument's market session."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("account risk day requires aware time")
    return at.astimezone(ZoneInfo("America/New_York")).date()


class AssetCapability(BrokerModel):
    version: Literal["OFFLINE_ASSET_CAPABILITY_V1"] = "OFFLINE_ASSET_CAPABILITY_V1"
    symbol: str
    asset_class: Literal["us_equity", "crypto"]
    feed: Literal["iex", "crypto"]
    calendar: Literal["XNYS", "CRYPTO24"]
    benchmark: str
    quantity_step: Decimal = Field(gt=0, allow_inf_nan=False)
    minimum_quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    time_in_force: TimeInForce

    @model_validator(mode="after")
    def coherent_market(self) -> Self:
        expected = (
            ("iex", "XNYS", "SPY", TimeInForce.DAY)
            if self.asset_class == "us_equity"
            else ("crypto", "CRYPTO24", "BTC/USD", TimeInForce.GTC)
        )
        pattern = (
            r"[A-Z]{1,5}(?:\.[A-Z])?" if self.asset_class == "us_equity" else r"[A-Z]{2,12}/USD"
        )
        if (
            re.fullmatch(pattern, self.symbol) is None
            or (self.feed, self.calendar, self.benchmark, self.time_in_force) != expected
        ):
            raise ValueError("asset capability market/feed/calendar/benchmark/TIF mismatch")
        if self.minimum_quantity % self.quantity_step != 0:
            raise ValueError("minimum quantity must be a whole number of quantity steps")
        return self

    @property
    def id(self) -> str:
        return content_id(self)

    def bound_quantity(self, quantity: Decimal) -> Decimal:
        bounded = (quantity / self.quantity_step).to_integral_value(rounding=ROUND_DOWN)
        bounded *= self.quantity_step
        return bounded if bounded >= self.minimum_quantity else Decimal(0)


class AssetSession(BrokerModel):
    capability_id: str = Field(min_length=64, max_length=64)
    symbol: str
    calendar: Literal["XNYS", "CRYPTO24"]
    feed: Literal["iex", "crypto"]
    observed_at: datetime
    session_open: datetime
    session_close: datetime
    account_risk_day: date

    @model_validator(mode="after")
    def coherent_time(self) -> Self:
        if self.account_risk_day != account_risk_day(self.observed_at):
            raise ValueError("asset session account risk day must use the shared ET day")
        if not self.session_open <= self.observed_at < self.session_close:
            raise ValueError("asset observation must be inside its market session")
        if self.calendar == "CRYPTO24":
            session = crypto_sessions(self.observed_at.date(), self.observed_at.date())[0]
            if (self.session_open, self.session_close) != (session.open_at, session.close_at):
                raise ValueError("crypto session requires exact UTC day boundaries")
        return self


def capability_for(config: CoreConfig, symbol: str) -> AssetCapability | None:
    return next((c for c in config.asset_capabilities if c.symbol == symbol), None)


def observation_capability_objections(
    config: CoreConfig, observation: Observation, *, quantity: Decimal | None = None
) -> tuple[str, ...]:
    if not config.asset_capabilities:
        return ("ASSET_CAPABILITY_NOT_CONFIGURED",) if observation.asset_session else ()
    capability = capability_for(config, observation.symbol)
    session = observation.asset_session
    if capability is None or session is None:
        return ("ASSET_CAPABILITY_OR_SESSION_MISSING",)
    if (
        session.capability_id != capability.id
        or session.symbol != observation.symbol
        or session.calendar != capability.calendar
        or session.feed != capability.feed
        or session.observed_at != observation.source_end
        or session.session_open != observation.session_open
        or session.session_close != observation.session_close
        or session.account_risk_day != account_risk_day(observation.recorded_at_utc)
        or observation.dependencies != (capability.benchmark,)
        or observation.evidence_lane != "SYNTHETIC_TEST"
        or observation.feed != "EXPLICIT_SYNTHETIC"
        or observation.feature_version != "synthetic-causal-v1"
    ):
        return ("ASSET_CAPABILITY_OR_SESSION_MISMATCH",)
    if quantity is not None and (quantity <= 0 or capability.bound_quantity(quantity) != quantity):
        return ("ASSET_QUANTITY_CAPABILITY_MISMATCH",)
    return ()
