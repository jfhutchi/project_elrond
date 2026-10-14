"""Narrow retail PAPER reuse. Quantity pagination is never all-in cost authority."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerProtocolError
from quantbot.domain import OrderSide
from quantbot.learner.five_minute import FEATURE_NAMES, build_features
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.base import AdjustmentMode, BarQuery, MarketDataFeed
from quantbot.market_data.calendar import AlpacaMarketCalendar, session_sequence
from quantbot.paper_core.contracts import BrokerEvent, CostItem, LearningCalendar, Observation
from quantbot.paper_core.costs import current_costs
from quantbot.paper_core.operational_costs import pending_cost_revision_source
from quantbot.paper_core.release import RuntimeConfig
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.paper_core.time_evidence import BrokerTime, Clocks
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage import StateConflictError, StorageRepository


class AlpacaCoreBroker:
    provider = "alpaca"
    environment = "PAPER"
    endpoint = "https://paper-api.alpaca.markets"
    cost_revision_batch_size = 128

    def __init__(
        self,
        adapter: AlpacaPaperBrokerAdapter,
        market: AlpacaMarketDataClient,
        calendar: AlpacaMarketCalendar,
        config: RuntimeConfig,
        clocks: Clocks,
        *,
        account_id: str,
    ) -> None:
        if type(adapter) is not AlpacaPaperBrokerAdapter or adapter.base_url != self.endpoint:
            raise ValueError("exact existing retail PAPER adapter required")
        self.adapter, self.market, self.calendar = adapter, market, calendar
        self.config, self.clocks, self.account_id = config, clocks, account_id
        self._marks: dict[str, tuple[Decimal, datetime]] = {}
        self.store: CoreStore | None = None
        self.pending_progress: tuple[str, str, str | None, str | None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.adapter, name)

    @property
    def now(self) -> datetime:
        return self.clocks.sample()[0]

    def mark(self, symbol: str) -> tuple[Decimal, datetime] | None:
        return self._marks.get(symbol)

    async def get_clock(self) -> BrokerTime:
        clock = await self.adapter.get_market_clock()
        sessions = await self.calendar.get_sessions(
            clock.timestamp.date() - timedelta(days=7), clock.timestamp.date() + timedelta(days=7)
        )
        matching = [s for s in sessions if s.open_at <= clock.timestamp < s.close_at]
        if not matching:
            matching = [s for s in sessions if s.open_at > clock.timestamp]
        if not matching:
            raise BrokerProtocolError("CLOCK_CALENDAR_SESSION_UNAVAILABLE")
        session = matching[0]
        return BrokerTime(
            timestamp=clock.timestamp,
            account_id=self.account_id,
            provider=self.provider,
            session_id=session.session_date.isoformat(),
            session_open=session.open_at,
            session_close=session.close_at,
            resolution_seconds=0.000001,
        )

    async def learning_calendar(self) -> LearningCalendar:
        sessions = await self.calendar.get_sessions(
            self.config.learning_origin.date(), self.now.date() + timedelta(days=7)
        )
        return LearningCalendar(
            sequence=session_sequence(sessions), authority="ALPACA_PAPER_XNYS_V1"
        )

    async def observation(self) -> Observation:
        now = self.now
        clock = await self.adapter.get_market_clock()
        if not clock.is_open:
            raise ValueError("MARKET_CLOSED")
        sessions = await self.calendar.get_sessions(
            now.date() - timedelta(days=7), now.date() + timedelta(days=7)
        )
        sequence = session_sequence(sessions)
        end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        query = BarQuery(
            symbols=tuple(sorted({self.config.core.symbol, "SPY"})),
            start=end - timedelta(minutes=40),
            end=end - timedelta(microseconds=1),
            timeframe="5Min",
            feed=MarketDataFeed.IEX,
            adjustment=AdjustmentMode.ALL,
            limit=100,
        )
        batch = await self.market.get_historical_bars(query)
        received = self.now
        rows = build_features(
            batch.bars,
            sequence,
            decision_at=received,
            receipt_at=received,
            symbols=(self.config.core.symbol,),
            source_snapshot_hash=content_id(batch),
        )
        matching = [r for r in rows if r.feature_at == end]
        if not matching:
            raise ValueError("FRESH_COMPLETE_BAR_UNAVAILABLE")
        row = matching[-1]
        latest = max(
            (b for b in batch.bars if b.symbol == self.config.core.symbol),
            key=lambda b: b.timestamp,
        )
        self._marks[self.config.core.symbol] = (latest.close, end)
        session = next(s for s in sessions if s.session_date == now.date())
        return Observation(
            recorded_at_utc=received,
            evidence_lane="BROKER_PAPER",
            release_id=self.store.config.release_id if self.store else self.config.core.release_id,
            symbol=self.config.core.symbol,
            feed="alpaca",
            source_start=end - timedelta(minutes=35),
            source_end=end,
            available_at=received,
            capture_received_at=received,
            price=latest.close,
            raw_data_digest=content_id(batch),
            feature_version="five-minute-price-alpha-v1",
            features=tuple(zip(FEATURE_NAMES, row.values, strict=True)),
            dependencies=("SPY",),
            dependency_available_at=(received,),
            sequence=int(end.timestamp() // 300),
            session_open=session.open_at,
            session_close=session.close_at,
        )

    async def refresh_mark(self, symbol: str) -> None:
        # Stop observations have their own narrow acquisition; entry waits do not prevent it.
        now = self.now
        end = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
        batch = await self.market.get_historical_bars(
            BarQuery(
                symbols=(symbol,),
                start=end - timedelta(minutes=10),
                end=end - timedelta(microseconds=1),
                timeframe="5Min",
                feed=MarketDataFeed.IEX,
                adjustment=AdjustmentMode.ALL,
                limit=4,
            )
        )
        if batch.bars:
            last = max(batch.bars, key=lambda b: b.timestamp)
            self._marks[symbol] = (last.close, last.timestamp + timedelta(minutes=5))

    async def owned_events(self, store: CoreStore) -> tuple[BrokerEvent, ...]:
        from quantbot.paper_core.authority import commission_in_session
        from quantbot.storage import StorageRepository

        with store.database.transaction() as session:
            commission = commission_in_session(store, session)
            repo = StorageRepository(session)
            progress = (
                session.execute(
                    select(table("core_provider_progress")).where(
                        table("core_provider_progress").c.account_id == self.account_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            cursor = (
                datetime.fromisoformat(progress["watermark"])
                if progress
                else commission.approved_at
            )
            relevant = session.execute(
                select(table("core_intent_links").c.intent_id)
                .join(
                    table("order_intents"),
                    table("order_intents").c.intent_id == table("core_intent_links").c.intent_id,
                )
                .where(
                    (
                        table("order_intents").c.created_at
                        >= (self.now - timedelta(days=2)).isoformat()
                    )
                    | table("order_intents").c.state.not_in(
                        ("FILLED", "CANCELLED", "EXPIRED", "REJECTED", "ERROR")
                    )
                )
            ).scalars()
            intents = [repo.get_order_intent(identity) for identity in relevant]
        # Replay overlaps creation dates to retain late fees. No empty-day zero inference.
        until = min(self.now, cursor + timedelta(hours=6))
        after = max(commission.approved_at - timedelta(days=2), cursor - timedelta(days=2))
        async with asyncio.timeout(5):
            rows = await self.adapter.get_performance_activities(after=after, until=until)
            fills = tuple(
                self.adapter._fill_from_payload(r) for r in rows if r.get("activity_type") == "FILL"
            )
            events = []
            for intent in intents:
                assert intent is not None
                first = await self.adapter.get_order_by_client_id(
                    intent.client_order_id, expected=intent
                )
                if first is None:
                    continue  # Recovery keeps its original reservation and client ID.
                second = await self.adapter.get_order_by_client_id(
                    intent.client_order_id, expected=intent
                )
                owned = tuple(f for f in fills if f.broker_order_id == first.broker_order_id)
                complete = (
                    first == second
                    and sum((f.quantity for f in owned), Decimal(0)) == (first.filled_quantity)
                    and all(f.occurred_at <= until for f in owned)
                )
                at = self.now
                events.append(
                    BrokerEvent(
                        recorded_at_utc=at,
                        occurred_at=at,
                        evidence_lane="BROKER_PAPER",
                        release_id=store.config.release_id,
                        account_id=self.account_id,
                        provider="alpaca",
                        endpoint=self.endpoint + "/v2/orders",
                        client_order_id=intent.client_order_id,
                        event_id=content_id({"order": first, "complete": complete}),
                        raw_digest=content_id(first),
                        order=first,
                        activity_complete=complete,
                        activity_watermark=until if complete else None,
                        activity_fill_ids=tuple(f.fill_id for f in owned),
                    )
                )
                for fill in owned:
                    events.append(
                        BrokerEvent(
                            recorded_at_utc=at,
                            occurred_at=fill.occurred_at,
                            evidence_lane="BROKER_PAPER",
                            release_id=store.config.release_id,
                            account_id=self.account_id,
                            provider="alpaca",
                            endpoint=self.endpoint + "/v2/account/activities",
                            client_order_id=intent.client_order_id,
                            event_id=fill.fill_id,
                            raw_digest=content_id(fill),
                            fill=fill,
                        )
                    )
        document = {
            "account_id": self.account_id,
            "endpoint": self.endpoint,
            "after": after,
            "until": until,
            "rows": rows,
            "cost_authority": "UNKNOWN_ALL_IN_COMPONENTS",
            "cash_authority": "NOT_CERTIFIED",
            "release_id": store.config.release_id,
        }
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            repo = StorageRepository(session)
            costs = current_costs(session, cutoff=self.now)
            contract_conflicted = self._revise_conflicted_contract(
                store, session, document, progress, costs, events
            )
            document_id = content_id(document)
            put(session, "core_provider_acquisitions", document_id, document)
            self.pending_progress = (
                until.isoformat(),
                document_id,
                progress["watermark"] if progress else None,
                progress["document_id"] if progress else None,
            )
            for raw in rows:
                if raw.get("activity_type") == "FILL":
                    continue
                if "net_amount" not in raw or not raw.get("id"):
                    raise BrokerProtocolError("NONTRADE_CASH_ACTIVITY_SCHEMA_UNVERIFIED")
                amount = Decimal(str(raw["net_amount"]))
                if not amount.is_finite():
                    raise BrokerProtocolError("NONTRADE_CASH_ACTIVITY_NONFINITE")
                # NonTradeActivity supplies cash independently of the account balance.
                creation = raw.get("created_at") or raw.get("date")
                if creation is None:
                    raise BrokerProtocolError("NONTRADE_CASH_CREATION_TIME_UNVERIFIED")
                occurred = datetime.fromisoformat(str(creation).replace("Z", "+00:00"))
                if occurred.tzinfo is None:
                    # A date-only activity cannot establish the intraday baseline boundary.
                    if raw["activity_type"] in {"FEE", "CFEE"}:
                        self.record_external(
                            session,
                            dict(
                                event_id=str(raw["id"]),
                                symbol="",
                                owner="ACCOUNT_NONTRADE",
                                quantity_delta="0",
                                net_cash=None,
                                source_document=document_id,
                                account_id=self.account_id,
                                occurred_at=None,
                                raw_creation=creation,
                                activity_type=str(raw["activity_type"]),
                                cash_authority_reason="NONTRADE_CASH_BASELINE_BOUNDARY_UNVERIFIED",
                            ),
                        )
                        continue
                    raise BrokerProtocolError("NONTRADE_CASH_BASELINE_BOUNDARY_UNVERIFIED")
                if occurred > commission.approved_at:
                    self.record_external(
                        session,
                        dict(
                            event_id=str(raw["id"]),
                            symbol="",
                            owner="ACCOUNT_NONTRADE",
                            quantity_delta="0",
                            net_cash=str(amount),
                            source_document=document_id,
                            account_id=self.account_id,
                            occurred_at=occurred.isoformat(),
                            activity_type=str(raw["activity_type"]),
                        ),
                    )
            for fill in fills:
                if fill.occurred_at <= commission.approved_at:
                    continue
                core_order = repo.get_broker_order(fill.broker_order_id)
                linked_core = (
                    core_order is not None
                    and session.execute(
                        select(table("core_intent_links").c.intent_id)
                        .join(
                            table("order_intents"),
                            table("order_intents").c.intent_id
                            == table("core_intent_links").c.intent_id,
                        )
                        .where(
                            table("order_intents").c.client_order_id == core_order.client_order_id
                        )
                    ).first()
                )
                if not linked_core:
                    # Only retained order/intent provenance identifies an external exit owner.
                    owner = dict(commission.legacy_owners).get(fill.symbol)
                    raw = next(r for r in rows if r.get("id") == fill.fill_id)
                    recorded_owner = (
                        None
                        if core_order is None
                        else session.execute(
                            select(table("order_intents").c.strategy_id).where(
                                table("order_intents").c.client_order_id
                                == core_order.client_order_id
                            )
                        ).scalar_one_or_none()
                    )
                    if owner and recorded_owner == owner:
                        external = dict(
                            event_id=fill.fill_id,
                            symbol=fill.symbol,
                            owner=owner,
                            quantity_delta=str(
                                fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
                            ),
                            net_cash=str(raw["net_amount"]) if "net_amount" in raw else None,
                            order_id=fill.broker_order_id,
                            owner_source="RETAINED_DURABLE_INTENT",
                            source_document=document_id,
                            account_id=self.account_id,
                            occurred_at=fill.occurred_at,
                        )
                        self.record_external(session, external)
                    else:
                        document["external_owner_blocker"] = (
                            "INHERITED_ORDER_OWNER_PROVENANCE_UNVERIFIED"
                        )
                    continue
                if self.config.cost_contract is not None:
                    item = self.config.cost_contract.fee(
                        fill,
                        account_id=self.account_id,
                        at=self.now,
                        release_id=store.config.release_id,
                    )
                    if (
                        not contract_conflicted
                        and item is not None
                        and (fill.fill_id, "FEE") not in costs
                    ):
                        events.append(
                            BrokerEvent(
                                recorded_at_utc=self.now,
                                occurred_at=self.now,
                                evidence_lane="BROKER_PAPER",
                                release_id=store.config.release_id,
                                account_id=self.account_id,
                                provider="alpaca",
                                endpoint=self.endpoint + "/cost-contract",
                                client_order_id=core_order.client_order_id if core_order else "",
                                event_id=item.id,
                                raw_digest=content_id(item),
                                cost=item,
                            )
                        )
        return tuple(events)

    @staticmethod
    def record_external(session: Session, external: dict[str, Any]) -> None:
        identity = content_id(
            {
                "account": external["account_id"],
                "event": external["event_id"],
                "owner": external["owner"],
            }
        )
        target = table("core_external_owner_events")
        previous = session.execute(
            select(target.c.payload).where(target.c.id == identity)
        ).scalar_one_or_none()
        if previous is not None:
            import json

            old = json.loads(previous)
            current = json.loads(canonical_result_json(external))
            old.pop("source_document", None)
            current.pop("source_document", None)
            if old != current:
                raise StateConflictError("EXTERNAL_OWNER_ACTIVITY_CONFLICT")
            return
        put(session, "core_external_owner_events", identity, external)

    def _revise_conflicted_contract(
        self,
        store: CoreStore,
        session: Session,
        document: dict[str, Any],
        progress: Any,
        costs: dict[tuple[str, str], CostItem],
        events: list[BrokerEvent],
    ) -> bool:
        """A fee contradicts its exact contract, irrespective of the FILL overlap."""
        contract = self.config.cost_contract
        if contract is None:
            return False
        cursor = table("core_source_cursors")
        from quantbot.paper_core.operational_costs import cost_contract_conflict_source

        conflict_key = cost_contract_conflict_source(self.account_id, contract.id)
        latch = (
            session.execute(select(cursor).where(cursor.c.source_id == conflict_key))
            .mappings()
            .one_or_none()
        )
        observed_conflict = any(
            r.get("activity_type") in {"FEE", "CFEE"}
            and "net_amount" in r
            and Decimal(str(r["net_amount"])) != 0
            for r in document["rows"]
        )
        if not observed_conflict and latch is None:
            return False
        previous_doc = (
            {}
            if progress is None
            else json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == progress["document_id"]
                    )
                ).scalar_one()
            )
        )
        first_doc = (
            None
            if latch is None
            else json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == latch["event_id"]
                    )
                ).scalar_one()
            )
        )
        first_seen = self.now if latch is None else datetime.fromisoformat(latch["available_at"])
        high = (
            session.execute(select(func.max(table("core_cost_items").c.id))).scalar_one() or ""
            if first_doc is None
            else first_doc["fee_scan_high"]
        )
        after = (
            previous_doc.get("fee_scan_after", "")
            if previous_doc.get("cost_conflict_contract_id") == contract.id
            else ""
        )
        original_costs = (
            select(table("core_cost_items"))
            .where(
                table("core_cost_items").c.id <= high,
                func.json_extract(table("core_cost_items").c.payload, "$.source")
                == "CONTRACTUAL_ZERO",
                func.json_extract(table("core_cost_items").c.payload, "$.document_id")
                == contract.id,
                func.json_extract(table("core_cost_items").c.payload, "$.component") == "FEE",
            )
            .order_by(table("core_cost_items").c.id)
        )
        batch = (
            session.execute(
                original_costs.where(table("core_cost_items").c.id > after).limit(
                    self.cost_revision_batch_size
                )
            )
            .mappings()
            .all()
        )
        scan_after = str(batch[-1]["id"]) if batch else after
        complete = scan_after >= high or not batch
        repo = StorageRepository(session)
        outcome_after = (
            previous_doc.get("fee_outcome_scan_after", "")
            if previous_doc.get("cost_conflict_contract_id") == contract.id
            else ""
        )
        outcome_complete = False
        if complete:
            checked = (
                session.execute(
                    original_costs.where(table("core_cost_items").c.id > outcome_after).limit(
                        self.cost_revision_batch_size
                    )
                )
                .mappings()
                .all()
            )
            waiting = False
            for row in checked:
                old = CostItem.model_validate_json(row["payload"])
                current = costs.get((old.fill_id, "FEE"))
                fill = repo.get_fill(old.fill_id)
                order = None if fill is None else repo.get_broker_order(fill.broker_order_id)
                owned_lot = (
                    None
                    if order is None
                    else session.execute(
                        select(table("core_intent_links").c.lot_id)
                        .join(
                            table("order_intents"),
                            table("order_intents").c.intent_id
                            == table("core_intent_links").c.intent_id,
                        )
                        .where(table("order_intents").c.client_order_id == order.client_order_id)
                    ).scalar_one_or_none()
                )
                applicable = (
                    fill is not None
                    and contract.fee(
                        fill,
                        account_id=self.account_id,
                        at=first_seen,
                        release_id=store.config.release_id,
                    )
                    is not None
                    and owned_lot is not None
                )
                if applicable:
                    payload = session.execute(
                        select(table("core_outcome_revisions").c.payload)
                        .join(
                            table("core_outcome_index"),
                            table("core_outcome_index").c.outcome_id
                            == table("core_outcome_revisions").c.id,
                        )
                        .where(table("core_outcome_index").c.lot_id == owned_lot)
                    ).scalar_one_or_none()
                    outcome = None if payload is None else json.loads(payload)
                    waiting = (
                        current is None
                        or current.id == old.id
                        or (
                            outcome is not None
                            and (
                                current.id not in outcome["cost_item_ids"]
                                or (
                                    current.source == "UNKNOWN"
                                    and (
                                        outcome["status"] != "COSTS_PENDING"
                                        or outcome["actual_fees"] is not None
                                        or outcome["net_pnl"] is not None
                                    )
                                )
                            )
                        )
                    )
                    if waiting:
                        break
                outcome_after = str(row["id"])
            outcome_complete = not waiting and (outcome_after >= high or not checked)
        document.update(
            cost_conflict_contract_id=contract.id,
            fee_scan_high=high,
            fee_scan_after=scan_after,
            fee_scan_complete=complete,
            fee_outcome_scan_after=outcome_after,
            fee_outcome_scan_complete=outcome_complete,
        )
        document_id = content_id(document)
        put(session, "core_provider_acquisitions", document_id, document)
        if latch is None:
            session.execute(
                cursor.insert().values(
                    source_id=conflict_key,
                    available_at=first_seen.isoformat(),
                    event_id=document_id,
                )
            )
        first_source = document_id if latch is None else str(latch["event_id"])
        pending = pending_cost_revision_source(self.account_id)
        if (
            batch
            and session.execute(
                select(cursor.c.source_id).where(cursor.c.source_id == pending)
            ).first()
            is None
        ):
            session.execute(
                cursor.insert().values(
                    source_id=pending, available_at=first_seen.isoformat(), event_id=first_source
                )
            )
        for row in batch:
            previous = CostItem.model_validate_json(row["payload"])
            current = costs.get((previous.fill_id, "FEE"))
            if (
                previous.id != row["id"]
                or previous.component != "FEE"
                or previous.source != "CONTRACTUAL_ZERO"
                or previous.document_id != contract.id
                or current is None
                or current.id != previous.id
            ):
                continue
            fill = repo.get_fill(previous.fill_id)
            order = None if fill is None else repo.get_broker_order(fill.broker_order_id)
            if (
                fill is None
                or order is None
                or contract.fee(
                    fill,
                    account_id=self.account_id,
                    at=first_seen,
                    release_id=store.config.release_id,
                )
                is None
            ):
                continue
            owned = session.execute(
                select(table("core_intent_links").c.intent_id)
                .join(
                    table("order_intents"),
                    table("order_intents").c.intent_id == table("core_intent_links").c.intent_id,
                )
                .where(table("order_intents").c.client_order_id == order.client_order_id)
            ).first()
            if owned is None:
                continue
            unknown = CostItem(
                recorded_at_utc=first_seen,
                evidence_lane="BROKER_PAPER",
                release_id=store.config.release_id,
                fill_id=fill.fill_id,
                source="UNKNOWN",
                effective_start=previous.effective_start,
                effective_end=previous.effective_end,
                revision=previous.revision + 1,
                supersedes=previous.id,
                source_refs=(first_source,),
            )
            events.append(
                BrokerEvent(
                    recorded_at_utc=first_seen,
                    occurred_at=first_seen,
                    evidence_lane="BROKER_PAPER",
                    release_id=store.config.release_id,
                    account_id=self.account_id,
                    provider="alpaca",
                    endpoint=self.endpoint + "/conflicting-fee",
                    client_order_id=order.client_order_id,
                    event_id=unknown.id,
                    raw_digest=content_id(unknown),
                    cost=unknown,
                )
            )
        return True

    def accept_activity_cursor(self, store: CoreStore) -> None:
        if self.pending_progress is None:
            return
        watermark, document_id, previous, previous_document = self.pending_progress
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            progress = table("core_provider_progress")
            document = json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == document_id
                    )
                ).scalar_one()
            )
            actual = session.execute(
                select(progress.c.watermark, progress.c.document_id).where(
                    progress.c.account_id == self.account_id
                )
            ).one_or_none()
            if (None if actual is None else tuple(actual)) == (
                None if previous is None else (previous, previous_document)
            ):
                values = dict(watermark=watermark, document_id=document_id)
                if actual is None:
                    session.execute(progress.insert().values(account_id=self.account_id, **values))
                else:
                    session.execute(
                        update(progress)
                        .where(progress.c.account_id == self.account_id)
                        .values(**values)
                    )
                if document.get("fee_scan_complete") and document.get("fee_outcome_scan_complete"):
                    session.execute(
                        table("core_source_cursors")
                        .delete()
                        .where(
                            table("core_source_cursors").c.source_id
                            == pending_cost_revision_source(self.account_id)
                        )
                    )
        self.pending_progress = None
