"""Explicit PAPER entry amendment; base ownership, exits and learning remain immutable."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    model_serializer,
    model_validator,
)

from quantbot.brokers.base import BrokerModel
from quantbot.domain import Account
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from quantbot.paper_core.asset_capability import AssetCapability
    from quantbot.paper_core.contracts import CoreConfig, Decision, Observation
    from quantbot.paper_core.store import CoreStore


class EntryPolicy(BrokerModel):
    contract: Literal["WHOLE_SHARE_FALLBACK_V1", "RESOURCE_BOUNDED_PAPER_V1"] = (
        "WHOLE_SHARE_FALLBACK_V1"
    )
    symbols: tuple[str, ...]
    universe: Literal["CONFIGURED_V1", "ALPACA_ASSETS_V1"] = "CONFIGURED_V1"
    minimum_whole_shares: Literal[1] = 1
    sizing_headroom_bps: Literal[100] = 100

    @model_validator(mode="after")
    def stock_universe(self) -> Self:
        if self.dynamic:
            if self.symbols or self.contract != "RESOURCE_BOUNDED_PAPER_V1":
                raise ValueError("discovery requires resource-bounded PAPER without a whitelist")
            return self
        if not 1 <= len(self.symbols) <= 32:
            raise ValueError("legacy configured universe requires 1 to 32 stocks")
        if len(set(self.symbols)) != len(self.symbols) or any(
            re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", s) is None or s == "SPY" for s in self.symbols
        ):
            raise ValueError("unique supported stocks excluding SPY required")
        return self

    @model_serializer(mode="wrap")
    def preserve_legacy(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result = dict(handler(self))
        if "universe" not in self.model_fields_set:
            result.pop("universe", None)
        return result

    @property
    def dynamic(self) -> bool:
        return self.universe == "ALPACA_ASSETS_V1"

    def permits(self, symbol: str) -> bool:
        return supported_symbol(symbol) if self.dynamic else symbol in self.symbols

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


def supported_symbol(symbol: str) -> bool:
    """Broker symbol syntax only; actual asset and market-data capability are checked separately."""
    return re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,31}", symbol) is not None


def entry_quantity(
    account: Account,
    price: Decimal,
    *,
    policy: EntryPolicy | None,
    fractionable: bool | None,
    config: CoreConfig | None = None,
    available_notional: Decimal | None = None,
    capability: AssetCapability | None = None,
) -> Decimal:
    from quantbot.paper_core.economics import limits, parallel_policy

    if config is not None and parallel_policy(config, policy):
        bound = limits(config, entry_policy=policy)
        available = max(
            Decimal(0),
            min(
                account.cash - account.equity * bound.reserve_bps / 10000,
                available_notional if available_notional is not None else account.cash,
            ),
        )
        normal = entry_quantity(
            account, price, policy=policy, fractionable=fractionable, capability=capability
        )
        if fractionable is False:
            return normal if normal * price <= available else Decimal(0)
        available_quantity = (
            capability.bound_quantity(available / price)
            if capability is not None
            else (available / price).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
        )
        return min(normal, available_quantity)
    target = account.equity * Decimal("0.005")
    if policy is not None:
        target *= Decimal(10000 - policy.sizing_headroom_bps) / 10000
    normal = (
        capability.bound_quantity(target / price)
        if capability is not None
        else (target / price).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
    )
    if policy is not None and fractionable is False:
        return max(Decimal(1), normal.to_integral_value(rounding="ROUND_DOWN"))
    return normal


def entry_risk_allowed(
    account: Account,
    quantity: Decimal,
    price: Decimal,
    *,
    minimum_whole_share: bool = False,
    config: CoreConfig | None = None,
    available_notional: Decimal | None = None,
    policy: EntryPolicy | None = None,
) -> bool:
    from quantbot.paper_core.economics import limits, parallel_policy

    notional = quantity * price
    if config is not None and parallel_policy(config, policy):
        bound = limits(config, entry_policy=policy)
        return (
            quantity > 0
            and (
                notional <= account.equity * bound.position_bps / 10000
                or minimum_whole_share
                and quantity == 1
            )
            and notional * bound.stop_bps / 10000 <= account.equity * bound.entry_risk_bps / 10000
            and notional
            <= max(Decimal(0), account.cash - account.equity * bound.reserve_bps / 10000)
            and (available_notional is None or notional <= available_notional)
        )
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
        if store.config.asset_capabilities:
            from quantbot.paper_core.asset_capability import observation_capability_objections
            from quantbot.paper_core.contracts import Observation

            payload = session.execute(
                select(table("core_observations").c.payload).where(
                    table("core_observations").c.id == decision.observation_id
                )
            ).scalar_one_or_none()
            if payload is None:
                return ("ASSET_CAPABILITY_OR_SESSION_MISSING",), False
            observed = Observation.model_validate_json(payload)
            capability_reasons = observation_capability_objections(
                store.config, observed, quantity=decision.quantity
            )
            if decision.entry_policy_id is not None or decision.symbol != observed.symbol:
                capability_reasons = (*capability_reasons, "ENTRY_POLICY_MISMATCH")
            return capability_reasons, False
        return (("ENTRY_POLICY_MISMATCH",) if decision.entry_policy_id else ()), False
    if decision.entry_policy_id != policy.id or not policy.permits(decision.symbol):
        return ("ENTRY_POLICY_MISMATCH",), False
    if policy.dynamic:
        from quantbot.paper_core.selection import validate_cohort

        try:
            if decision.selection is None:
                raise ValueError("discovery selection required")
            validate_cohort(store.config, decision.selection.cohort, entry_policy=policy)
            discovery = decision.selection.cohort.discovery
            if discovery is None or decision.symbol not in discovery.symbols:
                raise ValueError("discovery member required")
            from quantbot.paper_core.discovery import require_fresh_discovery

            require_fresh_discovery(discovery, now)
        except (ValueError, TypeError):
            return ("DISCOVERY_AUTHORITY_MISSING_OR_INVALID",), False
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
