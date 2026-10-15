"""Proven actual fees and modeled economics have independent immutable chains."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.domain import Fill
from quantbot.paper_core.contracts import CostItem
from quantbot.paper_core.store import put, table
from quantbot.storage import StateConflictError


def current_costs(
    session: Session, *, cutoff: datetime, fill_ids: set[str] | None = None
) -> dict[tuple[str, str], CostItem]:
    current: dict[tuple[str, str], CostItem] = {}
    query = select(table("core_cost_items").c.payload)
    if fill_ids is not None:
        index = table("core_document_index")
        query = query.join(index, index.c.id == table("core_cost_items").c.id).where(
            index.c.kind == "core_cost_items", index.c.reference.in_(fill_ids)
        )
    for encoded in session.execute(query).scalars():
        item = CostItem.model_validate_json(encoded)
        if item.recorded_at_utc <= cutoff:
            key = (item.fill_id, item.component)
            if key not in current or current[key].revision < item.revision:
                current[key] = item
    return current


def record_cost(session: Session, item: CostItem, fill: Fill) -> None:
    if not item.effective_start <= fill.occurred_at <= item.effective_end:
        raise StateConflictError("cost authority does not apply to fill time")
    # Writes must extend the whole durable chain. A historical receipt cutoff
    # is for reads; using it here could fork an already-recorded later revision.
    old = current_costs(
        session, cutoff=datetime.max.replace(tzinfo=UTC), fill_ids={item.fill_id}
    ).get((item.fill_id, item.component))
    if old is not None and old.id == item.id:
        return
    if old is None:
        if item.revision != 1 or item.supersedes is not None:
            raise StateConflictError("cost predecessor missing")
    elif (
        item.revision != old.revision + 1
        or item.supersedes != old.id
        or item.recorded_at_utc < old.recorded_at_utc
    ):
        raise StateConflictError("cost revision conflicts with current predecessor")
    put(session, "core_cost_items", item.id, item)


def actual_fee(fill: Fill, costs: dict[tuple[str, str], CostItem]) -> Decimal | None:
    item = costs.get((fill.fill_id, "FEE"))
    if item is not None:
        return item.amount if item.source in {"BROKER_REPORTED", "CONTRACTUAL_ZERO"} else None
    return fill.fee if fill.fee_source in {"BROKER_REPORTED", "CONTRACTUAL_ZERO"} else None
