"""Explicit PAPER entry amendment; base ownership, exits and learning remain immutable."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, StrictBool, model_validator

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Account
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from quantbot.paper_core.contracts import CoreConfig, Decision, Observation
    from quantbot.paper_core.store import CoreStore


class EntryPolicy(BrokerModel):
    contract: Literal["WHOLE_SHARE_FALLBACK_V1"] = "WHOLE_SHARE_FALLBACK_V1"
    symbols: tuple[str, ...] = Field(min_length=1, max_length=32)
    minimum_whole_shares: Literal[1] = 1
    sizing_headroom_bps: Literal[100] = 100

    @model_validator(mode="after")
    def stock_universe(self) -> Self:
        if len(set(self.symbols)) != len(self.symbols) or any(
            re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", s) is None or s == "SPY" for s in self.symbols
        ):
            raise ValueError("unique supported stocks excluding SPY required")
        return self

    @property
    def id(self) -> str:
        return content_id(self)


class AssetEligibility(BrokerModel):
    source: Literal["ALPACA_PAPER_ASSET_V1"] = "ALPACA_PAPER_ASSET_V1"
    symbol: str
    endpoint: Literal["https://paper-api.alpaca.markets"] = "https://paper-api.alpaca.markets"
    asset_class: Literal["us_equity"] = "us_equity"
    status: Literal["active"] = "active"
    entry_policy_id: str = Field(min_length=64, max_length=64)
    tradable: Literal[True] = True
    fractionable: StrictBool
    observed_at: datetime
    raw_digest: str = Field(min_length=64, max_length=64)

    @property
    def id(self) -> str:
        return content_id(self)


def symbols(config: CoreConfig, policy: EntryPolicy | None) -> tuple[str, ...]:
    return policy.symbols if policy else config.entry_symbols


def entry_quantity(
    account: Account, price: Decimal, *, policy: EntryPolicy | None, fractionable: bool | None
) -> Decimal:
    target = account.equity * Decimal("0.005")
    if policy is not None:
        target *= Decimal(10000 - policy.sizing_headroom_bps) / 10000
    normal = (target / price).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
    if policy is not None and fractionable is False:
        return max(Decimal(1), normal.to_integral_value(rounding="ROUND_DOWN"))
    return normal


def entry_risk_allowed(
    account: Account, quantity: Decimal, price: Decimal, *, minimum_whole_share: bool = False
) -> bool:
    notional = quantity * price
    return (
        quantity > 0
        and (notional <= account.equity * Decimal("0.005") or minimum_whole_share and quantity == 1)
        and notional * Decimal("0.01") <= account.equity * Decimal("0.0025")
        and notional <= account.cash
        and notional <= account.buying_power * Decimal("0.95")
    )


def minimum_whole_share(
    policy: EntryPolicy | None, decision: Decision, observation: Observation
) -> bool:
    return (
        policy is not None
        and decision.entry_policy_id == policy.id
        and observation.asset_eligibility is not None
        and observation.asset_eligibility.fractionable is False
        and decision.quantity == 1
    )


def eligibility_objections(
    eligibility: AssetEligibility | None,
    *,
    symbol: str,
    now: datetime,
    quantity: Decimal | None = None,
    max_age: float = 120,
    policy_id: str | None = None,
) -> tuple[str, ...]:
    if eligibility is None:
        return ("ASSET_ELIGIBILITY_UNVERIFIED",)
    if (
        eligibility.symbol != symbol
        or eligibility.observed_at.tzinfo is None
        or policy_id is not None
        and eligibility.entry_policy_id != policy_id
        or not 0 <= (now - eligibility.observed_at).total_seconds() <= max_age
    ):
        return ("ASSET_ELIGIBILITY_STALE_OR_MISMATCHED",)
    if (
        quantity is not None
        and not eligibility.fractionable
        and quantity != quantity.to_integral_value()
    ):
        return ("ASSET_WHOLE_QUANTITY_REQUIRED",)
    return ()


def decision_entry_authority(
    store: CoreStore,
    session: Session,
    decision: Decision,
    *,
    now: datetime,
    fresh: AssetEligibility | None = None,
    require_fresh: bool = False,
) -> tuple[tuple[str, ...], bool]:
    from sqlalchemy import select

    from quantbot.paper_core.contracts import Observation
    from quantbot.paper_core.store import table

    try:
        policy = entry_policy_in_session(store, session)
    except StateConflictError:
        return ("CORE_RUNTIME_BINDING_MISMATCH",), False
    if policy is None:
        return (("ENTRY_POLICY_MISMATCH",) if decision.entry_policy_id else ()), False
    if decision.entry_policy_id != policy.id or decision.symbol not in policy.symbols:
        return ("ENTRY_POLICY_MISMATCH",), False
    payload = session.execute(
        select(table("core_observations").c.payload).where(
            table("core_observations").c.id == decision.observation_id
        )
    ).scalar_one_or_none()
    if payload is None:
        return ("ASSET_ELIGIBILITY_UNVERIFIED",), False
    observation = Observation.model_validate_json(payload)
    original = observation.asset_eligibility
    reasons = list(
        eligibility_objections(
            original,
            symbol=decision.symbol,
            now=now,
            quantity=decision.quantity,
            policy_id=policy.id,
        )
    )
    if require_fresh:
        reasons.extend(
            eligibility_objections(
                fresh,
                symbol=decision.symbol,
                now=now,
                quantity=decision.quantity,
                max_age=5,
                policy_id=policy.id,
            )
        )
        if (
            original is not None
            and fresh is not None
            and original.fractionable != fresh.fractionable
        ):
            reasons.append("ASSET_CAPABILITY_CHANGED")
    return tuple(dict.fromkeys(reasons)), minimum_whole_share(policy, decision, observation)


def entry_policy_in_session(store: CoreStore, session: Session) -> EntryPolicy | None:
    from quantbot.paper_core.authority import executable_in_session

    binding = executable_in_session(session)
    if binding is None:
        return None
    if binding.release.id != store.runtime_release_id:
        raise StateConflictError("CORE_RUNTIME_BINDING_MISMATCH")
    return binding.release.config.entry_policy
