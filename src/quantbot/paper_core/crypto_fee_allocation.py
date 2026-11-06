"""Positive order-filter evidence can assign asset debits, never fee time or USD cost."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from quantbot.brokers.alpaca import _array_payload
from quantbot.brokers.base import BrokerError, BrokerModel, BrokerProtocolError
from quantbot.domain import OrderSide
from quantbot.paper_core.contracts import PortfolioEvidence, RoundTrip
from quantbot.paper_core.crypto_costs import (
    CryptoFeeActivity,
    parse_fee_activity,
    retain_fee_activity,
)
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError, StorageRepository
from quantbot.storage.database import decode_utc, encode_utc

if TYPE_CHECKING:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker

QUERY_CURSOR = "core_crypto_fee_query"
ALLOCATION_INDEX = "core_crypto_fee_allocation"
ACTIVITY_INDEX = "core_crypto_fee_assignment"
ROTATION_INDEX = "core_crypto_fee_rotation"
GAP_INDEX = "core_crypto_fee_gap"
ENDPOINT: Literal["https://paper-api.alpaca.markets/v2/account/activities"] = (
    "https://paper-api.alpaca.markets/v2/account/activities"
)


class OwnedFeeBasis(BrokerModel):
    lot_id: str
    intent_id: str
    order_id: str
    symbol: str
    baseline_id: str
    baseline_at: datetime
    commission_id: str
    order_digest: str
    fill_ids: tuple[str, ...]
    bought_quantity: Decimal
    terminal_source_id: str


class OrderFeeQueryPage(BrokerModel):
    page_token: str | None
    rows: tuple[dict[str, Any], ...]


class OrderFeeQueryEvidence(PortfolioEvidence):
    contract: Literal["ALPACA_ORDER_FILTER_FEE_QUERY_V1"] = "ALPACA_ORDER_FILTER_FEE_QUERY_V1"
    endpoint: Literal["https://paper-api.alpaca.markets/v2/account/activities"] = ENDPOINT
    basis: OwnedFeeBasis
    request_order_id: str
    direction: Literal["asc"] = "asc"
    page_size: Literal[100] = 100
    pages: tuple[OrderFeeQueryPage, ...]
    protocol_complete: bool
    objections: tuple[str, ...] = ()
    observed_activity_ids: tuple[str, ...]
    symbol_aliases: tuple[tuple[str, str], ...]
    raw_digest: str


class OrderFeeAllocation(PortfolioEvidence):
    contract: Literal["ORDER_FILTER_POST_BASELINE_ASSET_FEE_V1"] = (
        "ORDER_FILTER_POST_BASELINE_ASSET_FEE_V1"
    )
    baseline_binding: Literal["ORDER_FILTER_POST_BASELINE"] = "ORDER_FILTER_POST_BASELINE"
    lot_id: str
    order_id: str
    symbol: str
    asset_currency: str
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    activity_id: str
    fee_document_id: str
    query_document_id: str
    external_event_id: str


class LotAssetFees(BrokerModel):
    quantity: Decimal = Decimal(0)
    allocation_ids: tuple[str, ...] = ()
    fee_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    external_event_ids: tuple[str, ...] = ()


class FeeAllocationGap(PortfolioEvidence):
    contract: Literal["CRYPTO_FEE_ALLOCATION_GAP_V1"] = "CRYPTO_FEE_ALLOCATION_GAP_V1"
    reason: Literal["OUTSIDE_BOUNDED_ORDER_SEARCH_UNALLOCATED"] = (
        "OUTSIDE_BOUNDED_ORDER_SEARCH_UNALLOCATED"
    )
    activity_id: str
    raw_symbol: str
    raw_digest: str
    closed_search_start: datetime
    attribution: Literal["UNASSIGNED_NO_QUANTITY_OR_USD_AUTHORITY"] = (
        "UNASSIGNED_NO_QUANTITY_OR_USD_AUTHORITY"
    )


def _binding(store: CoreStore, at: datetime) -> dict[str, Any]:
    return dict(
        recorded_at_utc=at,
        evidence_lane=store.config.evidence_lane,
        release_id=store.config.release_id,
        account_id=store.config.account_id,
        store_identity=store.database.store_identity,
        config_id=store.config_id,
        policy_id=store.config.policy_id,
    )


def _entry_basis(
    store: CoreStore, session: Session, lot: str, *, at: datetime, exclusive: bool
) -> OwnedFeeBasis:
    from quantbot.paper_core.authority import commission_in_session
    from quantbot.paper_core.outcomes import terminal_evidence
    from quantbot.paper_core.settlement import definitively_unsent

    repo, links = StorageRepository(session), table("core_intent_links")
    entries = (
        session.execute(select(links).where(links.c.lot_id == lot, links.c.role == "ENTRY"))
        .mappings()
        .all()
    )
    if len(entries) != 1:
        raise StateConflictError("CRYPTO_FEE_ENTRY_OWNERSHIP_UNVERIFIED")
    intent = repo.get_order_intent(entries[0]["intent_id"])
    order = repo.get_broker_order_by_client_order_id(intent.client_order_id) if intent else None
    commission = commission_in_session(store, session)
    if (
        intent is None
        or order is None
        or intent.strategy_id != store.config.owner
        or intent.side is not OrderSide.BUY
        or not intent.symbol.endswith("/USD")
        or (order.symbol, order.side, order.quantity, order.time_in_force, order.order_type)
        != (intent.symbol, intent.side, intent.quantity, intent.time_in_force, intent.order_type)
        or not commission.approved_at < intent.created_at <= order.submitted_at <= at
    ):
        raise StateConflictError("CRYPTO_FEE_ENTRY_OWNERSHIP_UNVERIFIED")
    fills = repo.list_fills(broker_order_id=order.broker_order_id)
    terminal = terminal_evidence(session, order, cutoff=at)
    if (
        not fills
        or terminal is None
        or sum((f.quantity for f in fills), Decimal(0)) != order.filled_quantity
        or any(
            f.symbol != intent.symbol
            or f.side is not OrderSide.BUY
            or not commission.approved_at < f.occurred_at <= at
            for f in fills
        )
    ):
        raise StateConflictError("CRYPTO_FEE_ENTRY_FILLS_UNVERIFIED")
    row = session.execute(select(table("core_fixture_baselines"))).mappings().one()
    baseline = json.loads(row["payload"])
    if (
        content_id(baseline) != row["id"]
        or baseline["account"]["account_id"] != store.config.account_id
        or Decimal(dict(baseline["quantities"]).get(intent.symbol, "0")) != 0
    ):
        raise StateConflictError("CRYPTO_FEE_BASELINE_UNVERIFIED")
    if exclusive:
        candidates = session.execute(
            select(table("order_intents").c.intent_id).where(
                table("order_intents").c.symbol == intent.symbol
            )
        ).scalars()
        for candidate in repo.get_order_intents(list(candidates)):
            if (
                candidate
                and candidate.strategy_id != store.config.owner
                and not definitively_unsent(repo, candidate.intent_id)
            ):
                raise StateConflictError("CRYPTO_FEE_FOREIGN_OWNER")
        pending = session.execute(
            select(links.c.lot_id, links.c.intent_id)
            .join(table("order_intents"), table("order_intents").c.intent_id == links.c.intent_id)
            .where(
                links.c.role == "ENTRY",
                table("order_intents").c.symbol == intent.symbol,
                links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
            )
        ).all()
        closed = session.execute(
            select(table("core_round_trips").c.id).where(table("core_round_trips").c.id == lot)
        ).first()
        if not closed and {
            r.lot_id for r in pending if not definitively_unsent(repo, r.intent_id)
        } != {lot}:
            raise StateConflictError("CRYPTO_FEE_AMBIGUOUS_LOTS")
        for raw in session.execute(select(table("core_external_owner_events").c.payload)).scalars():
            event = json.loads(raw)
            if event.get("symbol") == intent.symbol and event.get("owner") != "ACCOUNT_CRYPTO_FEE":
                raise StateConflictError("CRYPTO_FEE_FOREIGN_OWNER")
    return OwnedFeeBasis(
        lot_id=lot,
        intent_id=intent.intent_id,
        order_id=order.broker_order_id,
        symbol=intent.symbol,
        baseline_id=row["id"],
        baseline_at=commission.approved_at,
        commission_id=commission.id,
        order_digest=content_id(order),
        fill_ids=tuple(sorted(f.fill_id for f in fills)),
        bought_quantity=order.filled_quantity,
        terminal_source_id=terminal,
    )


def _cursor_key(store: CoreStore, order_id: str) -> str:
    return content_id((store.config.account_id, store.database.store_identity, order_id))


async def acquire_order_fee_sources(
    broker: AlpacaCoreBroker, store: CoreStore, until: datetime, *, rows: tuple[Any, ...] = ()
) -> tuple[OrderFeeQueryEvidence, ...]:
    """At most one order/four pages per poll; unchanged pending fees retry after five minutes."""
    aliases = broker.adapter.crypto_symbol_aliases
    observed: dict[str, set[str]] = {}
    for raw in rows:
        if raw.get("activity_type") == "CFEE" and raw.get("symbol") in aliases:
            observed.setdefault(aliases[raw["symbol"]], set()).add(str(raw.get("id", "")))
    if not observed:
        return ()
    with store.database.transaction() as session:
        links, intents, index = (
            table("core_intent_links"),
            table("order_intents"),
            table("core_document_index"),
        )
        outcomes = table("core_outcome_index")
        rotation = index.alias("fee_rotation")
        lots = session.execute(
            select(links.c.lot_id)
            .join(intents, intents.c.intent_id == links.c.intent_id)
            .outerjoin(outcomes, outcomes.c.lot_id == links.c.lot_id)
            .outerjoin(
                rotation, and_(rotation.c.kind == ROTATION_INDEX, rotation.c.id == links.c.lot_id)
            )
            .where(
                links.c.role == "ENTRY",
                intents.c.symbol.in_(observed),
                or_(
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                    outcomes.c.source_end >= encode_utc(until - timedelta(days=2)),
                ),
            )
            .order_by(
                func.coalesce(rotation.c.available_at, ""), intents.c.created_at, links.c.lot_id
            )
            .limit(64)
        ).scalars()
        candidates = []
        for lot in lots:
            try:
                basis = _entry_basis(store, session, lot, at=until, exclusive=True)
            except (StateConflictError, ValueError):
                continue
            old = (
                session.execute(
                    select(index).where(
                        index.c.kind == QUERY_CURSOR,
                        index.c.id == _cursor_key(store, basis.order_id),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if old and until - decode_utc(old["available_at"]) < timedelta(minutes=5):
                continue
            candidates.append((old["available_at"] if old else "", basis))
    if not candidates:
        return ()
    basis = min(candidates, key=lambda item: (item[0], item[1].order_id))[1]
    pages, seen, token = [], set(), None
    objections: tuple[str, ...] = ()
    complete = False
    try:
        async with asyncio.timeout(2):
            for _ in range(4):
                params = {"order_id": basis.order_id, "direction": "asc", "page_size": "100"}
                if token is not None:
                    params["page_token"] = token
                page = _array_payload(
                    await broker.adapter._request("GET", "/v2/account/activities", params=params)
                )
                if len(page) > 100 or any(not isinstance(item, dict) for item in page):
                    raise BrokerProtocolError("CRYPTO_FEE_QUERY_PAGE_INVALID")
                typed = tuple(dict(cast(dict[str, Any], item)) for item in page)
                pages.append(OrderFeeQueryPage(page_token=token, rows=typed))
                for raw in typed:
                    identity = raw.get("id")
                    if not isinstance(identity, str) or not identity or identity in seen:
                        raise BrokerProtocolError("CRYPTO_FEE_QUERY_IDENTITY_INVALID")
                    if raw.get("order_id") not in (None, basis.order_id):
                        raise BrokerProtocolError("CRYPTO_FEE_QUERY_ORDER_CONTRADICTION")
                    seen.add(identity)
                if len(page) < 100:
                    complete = True
                    break
                token = str(typed[-1]["id"])
            if not complete:
                objections = ("CRYPTO_FEE_QUERY_PAGE_LIMIT",)
    except (BrokerError, TimeoutError) as error:
        objections = ("CRYPTO_FEE_QUERY_UNAVAILABLE:" + type(error).__name__,)
    return (
        OrderFeeQueryEvidence(
            **_binding(store, broker.now),
            basis=basis,
            request_order_id=basis.order_id,
            pages=tuple(pages),
            protocol_complete=complete,
            objections=objections,
            observed_activity_ids=tuple(sorted(observed[basis.symbol])),
            symbol_aliases=tuple(sorted(aliases.items())),
            raw_digest=content_id(pages),
        ),
    )


def _document(session: Session, identity: str, kind: Any) -> Any:
    raw = session.execute(
        select(table("core_provider_acquisitions").c.payload).where(
            table("core_provider_acquisitions").c.id == identity
        )
    ).scalar_one_or_none()
    if raw is None:
        raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED")
    value = kind.model_validate_json(raw)
    if value.id != identity:
        raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED")
    return value


def _query_valid(store: CoreStore, source: OrderFeeQueryEvidence) -> bool:
    token, seen = None, set()
    if (
        not source.protocol_complete
        or source.objections
        or not source.pages
        or len(source.pages) > 4
        or source.raw_digest != content_id(source.pages)
        or source.request_order_id != source.basis.order_id
        or source.account_id != store.config.account_id
        or source.store_identity != store.database.store_identity
        or source.config_id != store.config_id
        or source.release_id != store.config.release_id
        or source.evidence_lane != store.config.evidence_lane
        or any(
            alias not in {symbol, symbol.replace("/", "")} or not symbol.endswith("/USD")
            for alias, symbol in source.symbol_aliases
        )
    ):
        return False
    for position, page in enumerate(source.pages):
        if (
            page.page_token != token
            or len(page.rows) > 100
            or (position < len(source.pages) - 1 and len(page.rows) != 100)
        ):
            return False
        for raw in page.rows:
            identity = raw.get("id")
            if (
                not isinstance(identity, str)
                or not identity
                or identity in seen
                or raw.get("order_id") not in (None, source.request_order_id)
            ):
                return False
            seen.add(identity)
        token = str(page.rows[-1]["id"]) if page.rows else None
    return len(source.pages[-1].rows) < 100


def _fee_valid(fee: CryptoFeeActivity, basis: OwnedFeeBasis) -> bool:
    return (
        fee.activity_type == "CFEE"
        and fee.symbol == basis.symbol
        and fee.asset_currency == basis.symbol.split("/")[0]
        and fee.quantity_delta is not None
        and fee.quantity_delta < 0
        and fee.net_cash == 0
        and fee.status == "executed"
        and fee.order_id in (None, basis.order_id)
        and fee.fill_id in (None, *basis.fill_ids)
        and (fee.effective_at is None or fee.effective_at > basis.baseline_at)
    )


def _external_id(account_id: str, activity_id: str) -> str:
    return content_id(dict(account=account_id, event=activity_id, owner="ACCOUNT_CRYPTO_FEE"))


def retain_order_fee_allocations(
    store: CoreStore,
    session: Session,
    sources: tuple[OrderFeeQueryEvidence, ...],
    *,
    recorded_at: datetime,
    observed_rows: tuple[Any, ...] = (),
) -> tuple[str, ...]:
    """Caller holds the existing writer transaction; raw fees and debits remain immutable."""
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.work import dirty

    added = []
    index = table("core_document_index")
    for incoming in sources:
        key = _cursor_key(store, incoming.request_order_id)
        cursor = (
            session.execute(select(index).where(index.c.kind == QUERY_CURSOR, index.c.id == key))
            .mappings()
            .one_or_none()
        )
        source = incoming
        if cursor:
            old = _document(session, cursor["reference"], OrderFeeQueryEvidence)
            if old.model_dump(exclude={"recorded_at_utc"}) == incoming.model_dump(
                exclude={"recorded_at_utc"}
            ):
                source = old
        put(session, "core_provider_acquisitions", source.id, source)
        values = dict(reference=source.id, available_at=encode_utc(recorded_at))
        if cursor:
            session.execute(
                index.update()
                .where(index.c.kind == QUERY_CURSOR, index.c.id == key)
                .values(**values)
            )
        else:
            session.execute(index.insert().values(kind=QUERY_CURSOR, id=key, **values))
        rotated = session.execute(
            select(index.c.id).where(
                index.c.kind == ROTATION_INDEX, index.c.id == source.basis.lot_id
            )
        ).first()
        rotation_values = dict(
            reference=source.request_order_id, available_at=encode_utc(recorded_at)
        )
        if rotated:
            session.execute(
                index.update()
                .where(index.c.kind == ROTATION_INDEX, index.c.id == source.basis.lot_id)
                .values(**rotation_values)
            )
        else:
            session.execute(
                index.insert().values(
                    kind=ROTATION_INDEX, id=source.basis.lot_id, **rotation_values
                )
            )
        if not _query_valid(store, source):
            continue
        basis = _entry_basis(
            store, session, source.basis.lot_id, at=source.recorded_at_utc, exclusive=True
        )
        if basis != source.basis or source.recorded_at_utc > recorded_at:
            raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED")
        for raw in (
            raw for page in source.pages for raw in page.rows if raw.get("activity_type") == "CFEE"
        ):
            fee = retain_fee_activity(
                session,
                parse_fee_activity(
                    raw, symbol_aliases=dict(source.symbol_aliases), source_document_id=source.id
                ),
                account_id=store.config.account_id,
                received_at=recorded_at,
            )
            if not _fee_valid(fee, basis) or (
                fee.effective_at is not None and fee.effective_at > source.recorded_at_utc
            ):
                continue
            assert fee.quantity_delta is not None
            activity_key = content_id((store.config.account_id, fee.activity_id))
            old_id = session.execute(
                select(index.c.reference).where(
                    index.c.kind == ACTIVITY_INDEX, index.c.id == activity_key
                )
            ).scalar_one_or_none()
            if old_id:
                old = _document(session, old_id, OrderFeeAllocation)
                if (
                    old.lot_id != basis.lot_id
                    or old.order_id != basis.order_id
                    or old.quantity != -fee.quantity_delta
                ):
                    raise StateConflictError("CRYPTO_FEE_ALLOCATION_CONFLICT")
                continue
            external_id = _external_id(store.config.account_id, fee.activity_id)
            allocation = OrderFeeAllocation(
                **_binding(store, recorded_at),
                lot_id=basis.lot_id,
                order_id=basis.order_id,
                symbol=basis.symbol,
                asset_currency=fee.asset_currency or "",
                quantity=-fee.quantity_delta,
                activity_id=fee.activity_id,
                fee_document_id=fee.id,
                query_document_id=source.id,
                external_event_id=external_id,
            )
            existing = lot_asset_fees(store, session, basis.lot_id, as_of=recorded_at)
            if existing.quantity - fee.quantity_delta > basis.bought_quantity:
                if _quarantine_closed_fee(store, session, allocation, persisted=False):
                    continue
                raise StateConflictError("CRYPTO_FEE_QUANTITY_EXCEEDS_OWNED_BUY")
            previous = session.execute(
                select(table("core_external_owner_events").c.payload).where(
                    table("core_external_owner_events").c.id == external_id
                )
            ).scalar_one_or_none()
            if previous:
                event = json.loads(previous)
                if not _external_matches(event, fee, store.config.account_id):
                    raise StateConflictError("CRYPTO_FEE_ALLOCATION_CONFLICT")
            else:
                event = dict(
                    event_id=fee.activity_id,
                    symbol=fee.symbol,
                    owner="ACCOUNT_CRYPTO_FEE",
                    quantity_delta=str(fee.quantity_delta),
                    net_cash="0",
                    source_document=fee.source_document_id,
                    account_id=store.config.account_id,
                    occurred_at=None,
                    activity_type="CFEE",
                    asset_currency=fee.asset_currency,
                    raw_digest=fee.raw_digest,
                    baseline_binding="ORDER_FILTER_POST_BASELINE",
                    order_id=basis.order_id,
                    query_document_id=source.id,
                    baseline_id=basis.baseline_id,
                    lot_id=basis.lot_id,
                    recognized_at=recorded_at.isoformat(),
                )
                AlpacaCoreBroker.record_external(session, event)
            put(session, "core_provider_acquisitions", allocation.id, allocation)
            session.execute(
                index.insert().values(
                    kind=ACTIVITY_INDEX,
                    id=activity_key,
                    reference=allocation.id,
                    available_at=encode_utc(recorded_at),
                )
            )
            session.execute(
                index.insert().values(
                    kind=ALLOCATION_INDEX,
                    id=allocation.id,
                    reference=basis.lot_id,
                    available_at=encode_utc(recorded_at),
                )
            )
            if not _quarantine_closed_fee(store, session, allocation):
                dirty(session, (basis.lot_id,), available_at=recorded_at)
            added.append(allocation.id)
    _record_unallocated_gaps(store, session, observed_rows, recorded_at)
    return tuple(added)


def _quarantine_closed_fee(
    store: CoreStore, session: Session, allocation: OrderFeeAllocation, *, persisted: bool = True
) -> bool:
    raw = session.execute(
        select(table("core_round_trips").c.payload).where(
            table("core_round_trips").c.id == allocation.lot_id
        )
    ).scalar_one_or_none()
    if raw is None:
        return False
    trip = RoundTrip.model_validate_json(raw)
    if allocation.id in trip.asset_fee_allocation_ids:
        return False
    # A previously proven flat lot cannot absorb a new positive coin debit.
    # Retain both facts and withdraw the current outcome's authority.
    identity = "core-evidence-" + content_id((trip.id, allocation.fee_document_id))
    if session.execute(
        select(table("incidents").c.incident_id).where(table("incidents").c.incident_id == identity)
    ).first():
        return True
    StorageRepository(session).create_incident(
        identity,
        severity="ERROR",
        kind="CORE_EVIDENCE_CONFLICT",
        message="LATE_ASSET_FEE_CONTRADICTS_CLOSED_QUANTITY",
        occurred_at=allocation.recorded_at_utc,
        detail=dict(
            lot_ids=[allocation.lot_id],
            account_scope=False,
            implicated_source_ids=([allocation.id] if persisted else [])
            + [
                allocation.query_document_id,
                allocation.fee_document_id,
                trip.id,
            ],
            disputed_authorities=["OWNERSHIP", "OUTCOME"],
            evidence_lane=store.config.evidence_lane,
            prior_outcome_id=trip.id,
            recognized_asset_fee_quantity=str(allocation.quantity),
        ),
    )
    target = table("core_outcome_index")
    session.execute(
        target.update()
        .where(target.c.lot_id == allocation.lot_id)
        .values(status="INTEGRITY_FAILURE")
    )
    from quantbot.paper_core.work import dirty

    dirty(session, (allocation.lot_id,), available_at=allocation.recorded_at_utc)
    return True


def _record_unallocated_gaps(
    store: CoreStore, session: Session, rows: tuple[Any, ...], at: datetime
) -> None:
    index, links, intents, outcomes = (
        table(name)
        for name in (
            "core_document_index",
            "core_intent_links",
            "order_intents",
            "core_outcome_index",
        )
    )
    for raw in rows:
        if raw.get("activity_type") != "CFEE" or not raw.get("id") or not raw.get("symbol"):
            continue
        activity_id, symbol = str(raw["id"]), str(raw["symbol"])
        key = content_id((store.config.account_id, activity_id))
        if session.execute(
            select(index.c.id).where(
                index.c.kind.in_((ACTIVITY_INDEX, GAP_INDEX)), index.c.id == key
            )
        ).first():
            continue
        matching = (
            select(links.c.lot_id)
            .join(intents, intents.c.intent_id == links.c.intent_id)
            .outerjoin(outcomes, outcomes.c.lot_id == links.c.lot_id)
            .where(
                links.c.role == "ENTRY",
                intents.c.strategy_id == store.config.owner,
                or_(intents.c.symbol == symbol, func.replace(intents.c.symbol, "/", "") == symbol),
            )
        )
        recent = matching.where(
            or_(
                links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                outcomes.c.source_end >= encode_utc(at - timedelta(days=2)),
            )
        )
        older = matching.where(outcomes.c.source_end < encode_utc(at - timedelta(days=2)))
        if session.execute(recent.limit(1)).first() or not session.execute(older.limit(1)).first():
            continue
        gap = FeeAllocationGap(
            **_binding(store, at),
            activity_id=activity_id,
            raw_symbol=symbol,
            raw_digest=content_id(raw),
            closed_search_start=at - timedelta(days=2),
        )
        put(session, "core_provider_acquisitions", gap.id, gap)
        session.execute(
            index.insert().values(
                kind=GAP_INDEX, id=key, reference=gap.id, available_at=encode_utc(at)
            )
        )


def _external_matches(event: dict[str, Any], fee: CryptoFeeActivity, account_id: str) -> bool:
    return (
        event.get("event_id") == fee.activity_id
        and event.get("account_id") == account_id
        and event.get("owner") == "ACCOUNT_CRYPTO_FEE"
        and event.get("symbol") == fee.symbol
        and Decimal(str(event.get("quantity_delta"))) == fee.quantity_delta
        and Decimal(str(event.get("net_cash"))) == 0
        and event.get("raw_digest") == fee.raw_digest
        and event.get("asset_currency") == fee.asset_currency
        and event.get("activity_type") == "CFEE"
    )


def lot_asset_fees(
    store: CoreStore, session: Session, lot_id: str, *, as_of: datetime
) -> LotAssetFees:
    index = table("core_document_index")
    identities = session.execute(
        select(index.c.id).where(
            index.c.kind == ALLOCATION_INDEX,
            index.c.reference == lot_id,
            index.c.available_at <= encode_utc(as_of),
        )
    ).scalars()
    allocations, activities, external_ids, quantity = [], [], [], Decimal(0)
    sources: set[str] = set()
    for identity in identities:
        allocation = _document(session, identity, OrderFeeAllocation)
        source = _document(session, allocation.query_document_id, OrderFeeQueryEvidence)
        fee = _document(session, allocation.fee_document_id, CryptoFeeActivity)
        basis = _entry_basis(store, session, lot_id, at=source.recorded_at_utc, exclusive=False)
        raw = [
            raw for page in source.pages for raw in page.rows if raw.get("id") == fee.activity_id
        ]
        parsed = (
            parse_fee_activity(
                raw[0], symbol_aliases=dict(source.symbol_aliases), source_document_id=source.id
            )
            if len(raw) == 1
            else None
        )
        external = session.execute(
            select(table("core_external_owner_events").c.payload).where(
                table("core_external_owner_events").c.id == allocation.external_event_id
            )
        ).scalar_one_or_none()
        assignment = session.execute(
            select(index.c.reference).where(
                index.c.kind == ACTIVITY_INDEX,
                index.c.id == content_id((store.config.account_id, fee.activity_id)),
            )
        ).scalar_one_or_none()
        event = json.loads(external) if external else {}
        relative_valid = event.get("baseline_binding") != "ORDER_FILTER_POST_BASELINE" or (
            event.get("lot_id") == lot_id
            and event.get("order_id") == basis.order_id
            and event.get("query_document_id") == source.id
            and event.get("baseline_id") == basis.baseline_id
            and event.get("occurred_at") is None
            and event.get("recognized_at") == allocation.recorded_at_utc.isoformat()
        )
        if (
            not _query_valid(store, source)
            or source.basis != basis
            or not _fee_valid(fee, basis)
            or not source.recorded_at_utc <= allocation.recorded_at_utc <= as_of
            or (fee.effective_at is not None and fee.effective_at > source.recorded_at_utc)
            or allocation.lot_id != lot_id
            or allocation.order_id != basis.order_id
            or allocation.quantity != -fee.quantity_delta
            or allocation.symbol != basis.symbol
            or allocation.asset_currency != fee.asset_currency
            or allocation.activity_id != fee.activity_id
            or allocation.account_id != store.config.account_id
            or allocation.store_identity != store.database.store_identity
            or allocation.config_id != store.config_id
            or allocation.release_id != store.config.release_id
            or assignment != allocation.id
            or len(raw) != 1
            or content_id(raw[0]) != fee.raw_digest
            or parsed is None
            or parsed.model_dump(exclude={"source_document_id"})
            != fee.model_dump(exclude={"source_document_id"})
            or external is None
            or allocation.external_event_id
            != _external_id(store.config.account_id, fee.activity_id)
            or not relative_valid
            or not _external_matches(event, fee, store.config.account_id)
        ):
            raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED")
        quantity += allocation.quantity
        if quantity > basis.bought_quantity or fee.activity_id in activities:
            raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED")
        allocations.append(allocation.id)
        activities.append(fee.activity_id)
        sources.update((source.id, fee.id, basis.terminal_source_id, *basis.fill_ids))
        external_ids.append(allocation.external_event_id)
    events = table("core_external_owner_events")
    for event_id, payload in session.execute(
        select(events.c.id, events.c.payload).where(
            func.json_extract(events.c.payload, "$.lot_id") == lot_id
        )
    ):
        event = json.loads(payload)
        if event.get("baseline_binding") == "ORDER_FILTER_POST_BASELINE":
            try:
                recognized = datetime.fromisoformat(event["recognized_at"])
            except (KeyError, ValueError, TypeError) as error:
                raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED") from error
            if recognized.tzinfo is None or recognized <= as_of and event_id not in external_ids:
                raise StateConflictError("CRYPTO_FEE_ALLOCATION_UNVERIFIED")
    return LotAssetFees(
        quantity=quantity,
        allocation_ids=tuple(sorted(allocations)),
        fee_ids=tuple(sorted(activities)),
        source_ids=tuple(sorted(sources)),
        external_event_ids=tuple(sorted(external_ids)),
    )
