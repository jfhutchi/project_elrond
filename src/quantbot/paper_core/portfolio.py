"""Locked portfolio budgets and independently sourced daily-loss authority."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from quantbot.domain import Account, OrderIntent, Position
from quantbot.paper_core.contracts import (
    Decision,
    PortfolioEvidence,
    ProtectionPlan,
    ReconciliationProof,
    SessionOpening,
)
from quantbot.paper_core.economics import limits
from quantbot.paper_core.funds import entry_inventory_objections, same_spending_authority
from quantbot.paper_core.health import universe_health_objections
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.paper_core.time_evidence import TimeEvidence, project_time_evidence
from quantbot.storage import StorageRepository
from quantbot.storage.database import encode_utc

ZERO = Decimal(0)


class SessionCapitalEvidence(PortfolioEvidence):
    contract: Literal["SESSION_CAPITAL_COVERAGE_V1"] = "SESSION_CAPITAL_COVERAGE_V1"
    session_open: datetime
    proof_id: str
    activity_start: datetime
    activity_watermark: datetime
    complete: bool
    external_flow_ids: tuple[str, ...] = ()
    raw_digest: str
    opening_id: str | None = None
    source_document_id: str | None = None
    expected_cash: Decimal | None = None
    observed_cash: Decimal | None = None
    cash_residual: Decimal | None = None
    quantities_match: bool = False


class EntryQuoteEvidence(PortfolioEvidence):
    symbol: str
    ask: Decimal
    bid: Decimal
    source_at: datetime
    endpoint: Literal["https://data.alpaca.markets/v2/stocks/quotes/latest"] = (
        "https://data.alpaca.markets/v2/stocks/quotes/latest"
    )
    feed: Literal["iex"] = "iex"
    raw_digest: str


class CurrentPortfolioSnapshot(PortfolioEvidence):
    proof_id: str
    account: Account
    positions: tuple[Position, ...]
    entry_symbol: str | None = None
    entry_price: Decimal | None = None
    entry_price_asof: datetime | None = None
    entry_price_source: Literal["IEX_ASK_V1", "EXPLICIT_OFFLINE_MARK"] | None = None
    entry_quote_id: str | None = None


def entry_quote_time_bounds(store: CoreStore, *, now: datetime) -> tuple[datetime, datetime]:
    """Use the existing verified clock proof; an invalid retained proof is never a fallback."""
    identity = store.runtime_time_evidence_id
    if identity is None:
        return now, now
    try:
        timing = store.read("core_time_evidence", identity, TimeEvidence)
        projection = project_time_evidence(timing, store.clocks)
    except (ValueError, NoResultFound) as error:
        raise ValueError("ENTRY_QUOTE_TIME_UNVERIFIED") from error
    if (
        store.config.clock_profile != "PAPER_TIME_V2"
        or timing.id != identity
        or timing.contract != "PAPER_TIME_V2"
        or timing.release_id != store.config.release_id
        or timing.evidence_lane != store.config.evidence_lane
        or store.config_id not in timing.source_refs
        or len(timing.samples) != 2
        or any(
            s.provider.account_id != store.config.account_id or s.provider.provider != "alpaca"
            for s in timing.samples
        )
        or projection.objections
    ):
        raise ValueError("ENTRY_QUOTE_TIME_UNVERIFIED")
    return projection.provider_earliest, projection.provider_latest


def current_entry_price(
    store: CoreStore,
    mark: CurrentPortfolioSnapshot | None,
    intent: OrderIntent,
    decision: Decision,
    *,
    now: datetime,
) -> tuple[tuple[str, ...], Decimal]:
    """Conservative price authority; a lower quote never increases the original quantity."""
    if mark is None and store.config.evidence_lane != "BROKER_PAPER":
        return (), decision.price
    try:
        lower, upper = (
            entry_quote_time_bounds(store, now=now)
            if (store.config.evidence_lane == "BROKER_PAPER")
            else (now, now)
        )
    except ValueError:
        return ("ENTRY_QUOTE_TIME_UNVERIFIED",), decision.price
    if (
        mark is None
        or mark.entry_symbol != intent.symbol
        or mark.entry_price is None
        or mark.entry_price <= 0
        or mark.entry_price_asof is None
        or mark.entry_price_source is None
        or not mark.entry_price_asof
        <= lower
        <= upper
        <= mark.entry_price_asof + timedelta(seconds=5)
        or store.config.evidence_lane == "BROKER_PAPER"
        and mark.entry_price_source != "IEX_ASK_V1"
    ):
        return ("ENTRY_PRICE_UNAVAILABLE_OR_STALE",), decision.price
    if store.config.evidence_lane == "BROKER_PAPER":
        try:
            quote = store.read(
                "core_provider_acquisitions", mark.entry_quote_id or "", EntryQuoteEvidence
            )
            if (
                quote.id != mark.entry_quote_id
                or quote.account_id != store.config.account_id
                or quote.store_identity != store.database.store_identity
                or quote.config_id != store.config_id
                or quote.release_id != store.config.release_id
                or quote.policy_id != store.config.policy_id
                or quote.symbol != intent.symbol
                or quote.ask != mark.entry_price
                or quote.source_at != mark.entry_price_asof
                or not 0 <= (now - quote.recorded_at_utc).total_seconds() <= 5
            ):
                return ("ENTRY_QUOTE_SOURCE_UNVERIFIED",), decision.price
        except (ValueError, NoResultFound):
            return ("ENTRY_QUOTE_SOURCE_UNVERIFIED",), decision.price
    return (), max(decision.price, mark.entry_price)


@dataclass(frozen=True)
class PortfolioBudget:
    proof_id: str
    opening_id: str | None
    remaining_slots: int
    spendable_cash: Decimal
    gross_headroom: Decimal
    risk_headroom: Decimal
    session_reservations: int
    objections: tuple[str, ...]
    occupied_symbols: tuple[str, ...] = ()

    @property
    def available_notional(self) -> Decimal:
        return max(ZERO, min(self.spendable_cash, self.gross_headroom, self.risk_headroom * 100))


def record_session_capital(store: CoreStore, evidence: SessionCapitalEvidence) -> str:
    if (
        evidence.account_id != store.config.account_id
        or evidence.store_identity != store.database.store_identity
        or evidence.config_id != store.config_id
        or evidence.evidence_lane != store.config.evidence_lane
        or evidence.release_id != store.config.release_id
    ):
        raise ValueError("SESSION_CAPITAL_BINDING")
    with store.database.transaction() as session:
        put(session, "core_provider_acquisitions", evidence.id, evidence)
        index = table("core_document_index")
        if (
            session.execute(
                select(index.c.id).where(
                    index.c.kind == "core_session_capital", index.c.id == evidence.id
                )
            ).first()
            is None
        ):
            session.execute(
                index.insert().values(
                    kind="core_session_capital",
                    id=evidence.id,
                    reference=evidence.proof_id,
                    available_at=encode_utc(evidence.recorded_at_utc),
                )
            )
            if evidence.external_flow_ids:
                session.execute(
                    index.insert().values(
                        kind="core_session_external_flow",
                        id=evidence.id,
                        reference=evidence.session_open.date().isoformat(),
                        available_at=encode_utc(evidence.recorded_at_utc),
                    )
                )
    return evidence.id


def record_session_opening(store: CoreStore, evidence: SessionOpening) -> str:
    """A retained balance and advancing provider clock must witness the original opening."""
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        proofs = table("core_reconciliation_proofs")
        raw = session.execute(
            select(proofs.c.payload).where(proofs.c.id == evidence.proof_id)
        ).scalar_one_or_none()
        timing_raw = session.execute(
            select(table("core_time_evidence").c.payload).where(
                table("core_time_evidence").c.id == evidence.time_evidence_id
            )
        ).scalar_one_or_none()
        if raw is None or timing_raw is None:
            raise ValueError("OPENING_SOURCE_UNAVAILABLE")
        proof = ReconciliationProof.model_validate_json(raw)
        timing = TimeEvidence.model_validate_json(timing_raw)
        if (
            evidence.account_id != store.config.account_id
            or evidence.store_identity != store.database.store_identity
            or evidence.source_store_identity != store.database.store_identity
            or evidence.config_id != store.config_id
            or evidence.policy_id != store.config.policy_id
            or evidence.evidence_lane != store.config.evidence_lane
            or evidence.release_id != store.config.release_id
            or evidence.source_release_id != proof.release_id
            or proof.account.account_id != evidence.account_id
            or not proof.complete
            or proof.actual_quantities != proof.expected_quantities
            or proof.recorded_at_utc != evidence.captured_at
            or proof.account.equity != evidence.equity
            or not 0 <= (evidence.captured_at - evidence.session_open).total_seconds() < 60
            or evidence.session_date
            != evidence.session_open.astimezone(ZoneInfo("America/New_York")).date()
            or timing.objections
            or len(timing.samples) != 2
            or timing.samples[0].provider.timestamp >= timing.samples[-1].provider.timestamp
            or any(
                s.provider.account_id != evidence.account_id
                or s.provider.session_open != evidence.session_open
                or abs((s.wall_end - evidence.captured_at).total_seconds()) > 5
                for s in timing.samples
            )
        ):
            raise ValueError("OPENING_SOURCE_INVALID")
        cert = table(
            "core_inventory_certifications"
            if proof.cash_complete
            else "core_quantity_certifications"
        )
        if (
            session.execute(select(cert.c.proof_id).where(cert.c.proof_id == proof.id)).first()
            is None
        ):
            raise ValueError("OPENING_SOURCE_NOT_CERTIFIED")
        index = table("core_session_opening_index")
        existing = session.execute(
            select(index.c.document_id).where(
                index.c.account_id == evidence.account_id,
                index.c.policy_id == evidence.policy_id,
                index.c.session_date == evidence.session_date.isoformat(),
            )
        ).scalar_one_or_none()
        if existing is not None and existing != evidence.id:
            raise ValueError("OPENING_SOURCE_ALREADY_FIXED")
        put(session, "core_session_openings", evidence.id, evidence)
        if existing is None:
            session.execute(
                index.insert().values(
                    account_id=evidence.account_id,
                    policy_id=evidence.policy_id,
                    session_date=evidence.session_date.isoformat(),
                    store_identity=evidence.store_identity,
                    document_id=evidence.id,
                )
            )
    return evidence.id


def portfolio_budget(
    store: CoreStore,
    session: Session,
    proof: ReconciliationProof,
    *,
    now: datetime,
    exclude_intent: str | None = None,
    latest: bool = True,
    market_snapshot: CurrentPortfolioSnapshot | None = None,
) -> PortfolioBudget:
    """Cash already spent is in the proof; only remaining possible sends are reserved again."""
    if not store.parallel_entries:
        raise ValueError("portfolio budgets require explicit resource-bounded PAPER admission")
    connection = session.connection()
    driver = connection.connection.driver_connection
    assert driver is not None, "SQLITE_DRIVER_UNAVAILABLE"
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN")
    bound = limits(store.config, entry_policy=store.entry_policy)
    reasons = list(entry_inventory_objections(store, session, proof, now=now, latest=latest))
    account = proof.account
    current_positions = proof.positions
    if market_snapshot is not None:
        mark = market_snapshot
        if (
            mark.account_id != store.config.account_id
            or mark.store_identity != store.database.store_identity
            or mark.config_id != store.config_id
            or mark.policy_id != store.config.policy_id
            or mark.release_id != store.config.release_id
            or mark.evidence_lane != store.config.evidence_lane
            or mark.proof_id != proof.id
            or not 0 <= (now - mark.recorded_at_utc).total_seconds() <= 5
            or not same_spending_authority(mark.account, proof.account)
            or sorted((p.symbol, p.quantity, p.average_entry_price) for p in mark.positions)
            != sorted((p.symbol, p.quantity, p.average_entry_price) for p in proof.positions)
        ):
            reasons.append("PORTFOLIO_MARKS_STALE_OR_MISMATCHED")
        else:
            account, current_positions = mark.account, mark.positions
    if not 0 <= (now - proof.recorded_at_utc).total_seconds() <= 120:
        reasons.append("PORTFOLIO_INVENTORY_STALE")
    reasons.extend(universe_health_objections(store, session))
    repo = StorageRepository(session)
    if repo.get_kill_switch_state().engaged:
        reasons.append("MANUAL_ENTRY_STOP")
    day = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    index = table("core_session_opening_index")
    opening_raw = session.execute(
        select(table("core_session_openings").c.payload)
        .join(index, index.c.document_id == table("core_session_openings").c.id)
        .where(
            index.c.account_id == store.config.account_id,
            index.c.policy_id == store.config.policy_id,
            index.c.session_date == day,
            index.c.store_identity == store.database.store_identity,
        )
    ).scalar_one_or_none()
    opening = SessionOpening.model_validate_json(opening_raw) if opening_raw else None
    if opening is None:
        reasons.append("SESSION_OPENING_UNVERIFIED")
    elif account.equity * 10000 <= opening.equity * (10000 - (bound.daily_loss_bps or 0)):
        reasons.append("SESSION_DAILY_LOSS_HALT")
    generic = table("core_document_index")
    capital_rows = session.execute(
        select(table("core_provider_acquisitions").c.payload)
        .join(generic, generic.c.id == table("core_provider_acquisitions").c.id)
        .where(generic.c.kind == "core_session_capital", generic.c.reference == proof.id)
    ).scalars()
    capital = [SessionCapitalEvidence.model_validate_json(r) for r in capital_rows]
    valid = [
        e
        for e in capital
        if e.complete
        and e.account_id == store.config.account_id
        and e.store_identity == store.database.store_identity
        and e.config_id == store.config_id
        and e.release_id == store.config.release_id
        and e.evidence_lane == store.config.evidence_lane
        and opening is not None
        and e.session_open == opening.session_open
        and e.activity_start <= e.session_open
        and e.activity_watermark >= proof.recorded_at_utc
        and 0 <= (now - e.recorded_at_utc).total_seconds() <= 120
        and (
            store.config.evidence_lane != "BROKER_PAPER"
            or (
                e.opening_id == opening.id
                and e.source_document_id is not None
                and e.cash_residual is not None
                and e.cash_residual.is_finite()
                and abs(e.cash_residual) <= Decimal(".01")
                and e.quantities_match
                and e.observed_cash == proof.account.cash
            )
        )
    ]
    if not valid:
        reasons.append("SESSION_CAPITAL_UNVERIFIED")
    if session.execute(
        select(generic.c.id)
        .where(generic.c.kind == "core_session_external_flow", generic.c.reference == day)
        .limit(1)
    ).first():
        reasons.append("SESSION_EXTERNAL_CAPITAL_FLOW")
    positions = {p.symbol: p for p in current_positions}
    gross = sum(
        (max(abs(p.market_value), abs(p.quantity * p.market_price)) for p in current_positions),
        ZERO,
    )
    pending = ZERO
    risk = ZERO
    held: dict[str, Decimal] = {}
    reserved_symbols = set()
    count = 0
    links = table("core_intent_links")
    intents = table("order_intents")
    # Hydrate only today's usage and genuinely outstanding older work. Closed
    # historical outcomes remain retained without becoming a per-send full scan.
    entries = list(
        session.execute(
            select(links)
            .join(intents, intents.c.intent_id == links.c.intent_id)
            .where(
                links.c.role == "ENTRY",
                or_(
                    func.substr(intents.c.created_at, 1, 10) == day,
                    links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
                ),
            )
        ).mappings()
    )
    from quantbot.paper_core.settlement import _definitively_unsent_loaded

    current_intents = repo.get_order_intents([link["intent_id"] for link in entries])
    unsent_by_id = _definitively_unsent_loaded(
        repo, [intent for intent in current_intents if intent is not None]
    )
    for link, intent in zip(entries, current_intents, strict=True):
        if intent is None:
            reasons.append("PORTFOLIO_INTENT_UNAVAILABLE")
            continue
        unsent = unsent_by_id[intent.intent_id]
        if unsent:
            continue
        if (
            intent.intent_id != exclude_intent
            and intent.created_at.astimezone(ZoneInfo("America/New_York")).date().isoformat() == day
        ):
            count += 1
        decision = Decision.model_validate_json(
            session.execute(
                select(table("core_decisions").c.payload).where(
                    table("core_decisions").c.id == link["decision_id"]
                )
            ).scalar_one()
        )
        protection = ProtectionPlan.model_validate_json(
            session.execute(
                select(table("core_protection_plans").c.payload).where(
                    table("core_protection_plans").c.id == link["protection_id"]
                )
            ).scalar_one()
        )
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
        fills = repo.list_fills(broker_order_id=order.broker_order_id) if order else []
        acquired = sum((f.quantity for f in fills), ZERO)
        exited = ZERO
        for exit_link in session.execute(
            select(links).where(links.c.lot_id == link["lot_id"], links.c.role == "EXIT")
        ).mappings():
            exit_intent = repo.get_order_intent(exit_link["intent_id"])
            exit_order = (
                repo.get_broker_order_by_client_order_id(exit_intent.client_order_id)
                if exit_intent
                else None
            )
            if exit_order:
                exited += sum(
                    (
                        f.quantity
                        for f in repo.list_fills(broker_order_id=exit_order.broker_order_id)
                    ),
                    ZERO,
                )
        owned = acquired - exited
        if owned < 0 or acquired > intent.quantity:
            reasons.append("PORTFOLIO_QUANTITY_CONTRADICTION")
        if owned > 0:
            held[intent.symbol] = held.get(intent.symbol, ZERO) + owned
            average = sum((f.quantity * f.price for f in fills), ZERO) / acquired
            # The unchanged guardian stops at 99% of actual acquisition cost.
            # Original decision-price metadata is not the executed stop basis.
            risk += owned * average * Decimal(".01")
        from quantbot.paper_core.outcomes import terminal_evidence

        terminal = order is not None and terminal_evidence(session, order, cutoff=now) is not None
        if order is not None and order.filled_quantity != acquired:
            reasons.append("PORTFOLIO_CANONICAL_FILLS_PENDING")
        unfilled = (
            max(ZERO, intent.quantity - acquired)
            if not terminal and intent.intent_id != exclude_intent
            else ZERO
        )
        if unfilled:
            price = max(
                decision.price,
                positions[intent.symbol].market_price
                if intent.symbol in positions
                else decision.price,
            )
            pending += unfilled * price
            risk += unfilled * max(ZERO, price - protection.stop_price)
            reserved_symbols.add(intent.symbol)
    if any(held.get(s, ZERO) != p.quantity for s, p in positions.items()):
        reasons.append("PORTFOLIO_UNATTRIBUTED_POSITION")
    cash = account.cash - account.equity * bound.reserve_bps / 10000 - pending
    gross_headroom = account.equity * (bound.gross_bps or 0) / 10000 - gross - pending
    risk_headroom = account.equity * (bound.open_risk_bps or 0) / 10000 - risk
    slots = len(store.entry_symbols) - len(set(positions) | reserved_symbols)
    policy = store.entry_policy
    if policy is not None and policy.dynamic:
        # Candidate batch membership is bounded separately. Economic capacity replaces list size.
        target = account.equity * Decimal(".005") * Decimal(".99")
        slots = (
            int((max(ZERO, cash) / target).to_integral_value(rounding="ROUND_CEILING"))
            if target > 0
            else 0
        )
    if min(cash, gross_headroom, risk_headroom) < 0 or slots < 0:
        reasons.append("PORTFOLIO_EXPOSURE_LIMIT")
    if bound.daily_entry_limit is not None and count >= bound.daily_entry_limit:
        reasons.append("SESSION_RESERVATION_LIMIT")
    return PortfolioBudget(
        proof.id,
        opening.id if opening else None,
        max(0, slots),
        max(ZERO, cash),
        max(ZERO, gross_headroom),
        max(ZERO, risk_headroom),
        count,
        tuple(dict.fromkeys(reasons)),
        tuple(sorted(set(positions) | reserved_symbols)),
    )
