"""Explicit new-lot lineage and conservative, replayable economic outcomes.

This journal grants no order authority. Missing ownership, terminal broker evidence,
fees or flat reconciliation prevent finalization rather than manufacturing profit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id

Quantity = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class LotEvent(LearnerModel):
    experiment_id: Text
    lot_id: Digest
    decision_id: Digest
    symbol: Text
    account_id_hash: Digest
    recorded_at: datetime
    occurred_at: datetime
    kind: Literal[
        "ENTRY_DECLARED",
        "ORDER_LINK",
        "FILL",
        "PROTECTION_BOUND",
        "ORDER_TERMINAL",
        "RECONCILED_FLAT",
    ]
    order_id: Text
    quantity: Quantity
    source_hash: Digest
    synthetic: bool = False
    position_before_entry: Quantity | None = None
    side: Literal["BUY", "SELL"] | None = None
    owner: Literal["ENTRY", "DEADLINE", "PROTECTION", "UNKNOWN"] | None = None
    fill_id: Text | None = None
    price: Price | None = None
    fee: Quantity | None = None
    terminal: bool = False
    reconciled: bool = False

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.occurred_at > self.recorded_at:
            raise ValueError("FUTURE_LOT_EVIDENCE")
        if self.kind == "ENTRY_DECLARED" and self.position_before_entry != 0:
            raise ValueError("EXISTING_INVENTORY_CANNOT_BE_ADOPTED")
        if self.kind == "FILL" and (
            self.quantity <= 0
            or self.fill_id is None
            or self.price is None
            or self.side is None
            or self.owner is None
        ):
            raise ValueError("INCOMPLETE_FILL_IDENTITY")
        if self.kind == "ORDER_LINK" and (self.side is None or self.owner is None):
            raise ValueError("INCOMPLETE_ORDER_OWNERSHIP")
        return self


class EconomicOutcome(LearnerModel):
    lot_id: Digest
    status: Literal["COMPLETE", "INCOMPLETE"]
    reasons: tuple[str, ...]
    net_pnl: Decimal | None
    fees: Decimal | None
    residual_quantity: Decimal
    protected_quantity: Decimal
    protection_verified: bool
    entry_fill_ids: tuple[str, ...]
    exit_fill_ids: tuple[str, ...]
    synthetic: bool
    resolved_at: datetime


class LotOutcomeRevision(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    source_state_hash: Digest
    outcome: EconomicOutcome

    @model_validator(mode="after")
    def check(self) -> Self:
        if self.recorded_at != self.outcome.resolved_at:
            raise ValueError("OUTCOME_AVAILABILITY_MISMATCH")
        return self


def record_lot_event(store: ExperimentStore, event: LotEvent) -> bool:
    event = LotEvent.model_validate(event.model_dump())
    manifests = [
        row.decode(ExperimentManifest)
        for row in store.read_as_of(event.experiment_id, event.recorded_at)
        if row.kind == "ExperimentManifest"
    ]
    if (
        len(manifests) != 1
        or event.synthetic != manifests[0].synthetic
        or event.symbol not in {identity.symbol for identity in manifests[0].roster}
    ):
        raise ValueError("MANIFEST_LOT_IDENTITY_MISMATCH")
    identity = (
        f"fill:{event.fill_id}"
        if event.kind == "FILL"
        else f"{event.kind}:{event.order_id}:{event.occurred_at.isoformat()}"
    )
    return store.append(event, idempotency_key=f"lot:{event.lot_id}:{identity}")


def _lot_events(store: ExperimentStore, lot_id: str, now: datetime) -> list[LotEvent]:
    # Discover the namespace, then use the store's hash-checked as-of reader.
    with store._transaction(write=False) as db:
        experiments = tuple(
            row[0]
            for row in db.execute("SELECT DISTINCT experiment_id FROM events WHERE kind='LotEvent'")
        )
    events = [
        record.decode(LotEvent)
        for experiment in experiments
        for record in store.read_as_of(experiment, now)
        if record.kind == "LotEvent"
    ]
    return [event for event in events if event.lot_id == lot_id]


def journal_lot_outcome(store: ExperimentStore, lot_id: str, *, now: datetime) -> bool:
    """Single-lease coordinator appends each evidence revision once, never rewrites it."""
    rows = _lot_events(store, lot_id, now)
    experiments = {row.experiment_id for row in rows}
    if len(experiments) != 1:
        raise ValueError("LOT_EXPERIMENT_UNVERIFIED")
    experiment = next(iter(experiments))
    source_hash = content_id(sorted(content_id(row) for row in rows))
    for event in store.read_as_of(experiment, now):
        if event.kind == "LotOutcomeRevision":
            revision = event.decode(LotOutcomeRevision)
            if revision.source_state_hash == source_hash and revision.outcome.lot_id == lot_id:
                return False
    revision = LotOutcomeRevision(
        experiment_id=experiment,
        recorded_at=now,
        source_state_hash=source_hash,
        outcome=resolve_lot(store, lot_id, now=now),
    )
    return store.append(revision, idempotency_key=f"lot-outcome:{lot_id}:{source_hash}")


def resolve_lot(store: ExperimentStore, lot_id: str, *, now: datetime) -> EconomicOutcome:
    rows = _lot_events(store, lot_id, now)
    reasons: set[str] = set()
    declarations = [row for row in rows if row.kind == "ENTRY_DECLARED"]
    identity = {
        (row.experiment_id, row.decision_id, row.symbol, row.account_id_hash, row.synthetic)
        for row in rows
    }
    if len(declarations) != 1 or len(identity) != 1:
        reasons.add("LOT_IDENTITY_UNVERIFIED")
    links = {row.order_id: row for row in rows if row.kind == "ORDER_LINK"}
    if len(links) != sum(row.kind == "ORDER_LINK" for row in rows):
        reasons.add("ORDER_OWNERSHIP_CONFLICT")
    fills = sorted(
        (row for row in rows if row.kind == "FILL"),
        key=lambda row: (row.occurred_at, row.fill_id or ""),
    )
    buys = [row for row in fills if row.side == "BUY"]
    sells = [row for row in fills if row.side == "SELL"]
    if not buys:
        reasons.add("ENTRY_FILL_UNAVAILABLE")
    declaration = declarations[0] if len(declarations) == 1 else None
    for row in fills:
        link = links.get(row.order_id)
        allowed = {"ENTRY"} if row.side == "BUY" else {"DEADLINE", "PROTECTION"}
        if (
            link is None
            or link.side != row.side
            or link.owner != row.owner
            or row.owner not in allowed
            or link.occurred_at > row.occurred_at
        ):
            reasons.add(
                "ENTRY_OWNERSHIP_UNVERIFIED" if row.side == "BUY" else "EXIT_OWNERSHIP_UNVERIFIED"
            )
        if declaration is None or declaration.occurred_at > row.occurred_at:
            reasons.add("LOT_DECLARATION_UNVERIFIED")
    inventory = Decimal(0)
    times = sorted({row.occurred_at for row in fills})
    for timestamp in times:
        at_time = [row for row in fills if row.occurred_at == timestamp]
        sold = sum((row.quantity for row in at_time if row.side == "SELL"), Decimal(0))
        if sold > inventory:
            reasons.add("FILL_ORDERING_UNVERIFIED")
        inventory -= sold
        inventory += sum((row.quantity for row in at_time if row.side == "BUY"), Decimal(0))
    if inventory != 0:
        reasons.add("RESIDUAL_INVENTORY")
    if any(row.fee is None for row in fills):
        reasons.add("FEE_UNAVAILABLE")
    terminal = {row.order_id: row for row in rows if row.kind == "ORDER_TERMINAL" and row.terminal}
    for order in {row.order_id for row in fills}:
        actual = [row for row in fills if row.order_id == order]
        receipt = terminal.get(order)
        if (
            receipt is None
            or receipt.quantity != sum((row.quantity for row in actual), Decimal(0))
            or receipt.recorded_at < max(row.recorded_at for row in actual)
            or receipt.occurred_at < max(row.occurred_at for row in actual)
            or any(
                row.order_id == order
                and row.kind == "ORDER_TERMINAL"
                and row.terminal
                and (
                    row.quantity != receipt.quantity
                    or row.occurred_at < max(fill.occurred_at for fill in actual)
                )
                for row in rows
            )
        ):
            reasons.add("TERMINAL_FILL_RECONCILIATION_UNAVAILABLE")
    last_fill = max((row.occurred_at for row in fills), default=now)
    if not any(
        row.kind == "RECONCILED_FLAT"
        and row.reconciled
        and row.quantity == 0
        and row.occurred_at >= last_fill
        for row in rows
    ):
        reasons.add("FLAT_RECONCILIATION_UNAVAILABLE")
    protection = [row for row in rows if row.kind == "PROTECTION_BOUND"]
    protected = max((row.quantity for row in protection), default=Decimal(0))
    entry_quantity = sum((row.quantity for row in buys), Decimal(0))
    first_exit = min((row.occurred_at for row in sells), default=now)
    protection_verified = (
        bool(protection)
        and protected == entry_quantity
        and all(row.quantity <= entry_quantity for row in protection)
        and any(
            row.occurred_at < first_exit and row.quantity == entry_quantity for row in protection
        )
    )
    if not protection_verified:
        reasons.add("PROTECTION_UNVERIFIED")
    fees = (
        sum((row.fee or Decimal(0) for row in fills), Decimal(0))
        if "FEE_UNAVAILABLE" not in reasons
        else None
    )
    proceeds = sum((row.quantity * row.price for row in sells if row.price is not None), Decimal(0))
    cost = sum((row.quantity * row.price for row in buys if row.price is not None), Decimal(0))
    return EconomicOutcome(
        lot_id=lot_id,
        status="INCOMPLETE" if reasons else "COMPLETE",
        reasons=tuple(sorted(reasons)),
        net_pnl=None if reasons else proceeds - cost - (fees or 0),
        fees=fees,
        residual_quantity=inventory,
        protected_quantity=protected,
        protection_verified=protection_verified,
        entry_fill_ids=tuple(row.fill_id for row in buys if row.fill_id is not None),
        exit_fill_ids=tuple(row.fill_id for row in sells if row.fill_id is not None),
        synthetic=any(row.synthetic for row in rows),
        resolved_at=now,
    )
