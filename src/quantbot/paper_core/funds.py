"""Narrow PAPER sizing authority; never independent cash or fee reconciliation."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerAccountSnapshot
from quantbot.domain import Account, BrokerOrder, Position
from quantbot.paper_core.contracts import CoreConfig, Evidence, ReconciliationProof
from quantbot.paper_core.store import CoreStore, latest_certification, table
from quantbot.storage import StateConflictError, StorageRepository


class PaperFundsEvidence(Evidence):
    """Local receipt times, not a claim that account endpoints publish provider as-of times."""

    contract: Literal["BROKER_PAPER_BALANCE_V1"] = "BROKER_PAPER_BALANCE_V1"
    source: Literal["ALPACA_PAPER_STABLE_ACCOUNT_READS"] = "ALPACA_PAPER_STABLE_ACCOUNT_READS"
    endpoint: Literal["https://paper-api.alpaca.markets"]
    account_id: str
    store_identity: str
    config_id: str
    commission_id: str
    process_id: str
    capture_started: datetime
    elapsed_seconds: float = Field(ge=0, le=5, allow_inf_nan=False)
    first_account: BrokerAccountSnapshot
    second_account: BrokerAccountSnapshot
    first_positions: tuple[Position, ...]
    second_positions: tuple[Position, ...]
    first_open_orders: tuple[BrokerOrder, ...]
    second_open_orders: tuple[BrokerOrder, ...]


def paper_balance_policy(config: CoreConfig) -> bool:
    return (
        config.selection_policy == "SIGNAL_OR_EXPLORE_V1"
        and config.evidence_lane == "BROKER_PAPER"
        and config.clock_profile == "PAPER_TIME_V2"
    )


def same_spending_authority(first: Account, second: Account) -> bool:
    """Market equity can move without changing certified spending authority."""
    return all(
        getattr(first, field) == getattr(second, field)
        for field in (
            "account_id",
            "currency",
            "cash",
            "buying_power",
            "non_marginable_buying_power",
        )
    )


def stable_account_reads(first: BrokerAccountSnapshot, second: BrokerAccountSnapshot) -> bool:
    return (
        same_spending_authority(first.account, second.account)
        and first.model_copy(update={"account": second.account}) == second
    )


def stable_position_reads(first: tuple[Position, ...], second: tuple[Position, ...]) -> bool:
    def basis(positions: tuple[Position, ...]) -> list[tuple[str, Decimal, Decimal]]:
        return sorted((p.symbol, p.quantity, p.average_entry_price) for p in positions)

    return basis(first) == basis(second)


def structural_funds_allowed(config: CoreConfig, proof: ReconciliationProof) -> bool:
    """Pure candidate eligibility only; claim/send require the persisted receipt below."""
    exact = proof.cash_complete and proof.expected_cash is not None
    if exact:
        return proof.account.cash == proof.expected_cash
    return (
        paper_balance_policy(config)
        and proof.funds_authority == "BROKER_PAPER_BALANCE_V1"
        and not proof.cash_complete
        and proof.expected_cash is None
    )


def open_order_objections(
    store: CoreStore, session: Session, orders: tuple[BrokerOrder, ...]
) -> tuple[str, ...]:
    """Only exact durable, quantity-accounted batch ownership can explain an open order."""
    from quantbot.domain import IntentState
    from quantbot.paper_core.batch import member_objections
    from quantbot.paper_core.contracts import Decision
    from quantbot.paper_core.send_guard import owned_exit_objections

    if not orders:
        return ()
    if not store.parallel_entries:
        return ("CORE_COMPETING_OPEN_ORDER",)
    repo = StorageRepository(session)
    seen_clients, seen_symbols = set(), set()
    for order in orders:
        link = (
            session.execute(
                select(table("core_intent_links"))
                .join(
                    table("order_intents"),
                    table("order_intents").c.intent_id == table("core_intent_links").c.intent_id,
                )
                .where(table("order_intents").c.client_order_id == order.client_order_id)
            )
            .mappings()
            .one_or_none()
        )
        intent = repo.get_order_intent(link["intent_id"]) if link else None
        if (
            link is None
            or intent is None
            or intent.strategy_id != store.config.owner
            or order.client_order_id in seen_clients
            or order.symbol in seen_symbols
            or repo.get_broker_order_by_client_order_id(order.client_order_id) != order
            or (order.symbol, order.side, order.quantity, order.order_type, order.time_in_force)
            != (
                intent.symbol,
                intent.side,
                intent.quantity,
                intent.order_type,
                intent.time_in_force,
            )
            or intent.state
            not in {
                IntentState.SUBMITTING,
                IntentState.SUBMISSION_UNKNOWN,
                IntentState.BROKER_ACCEPTED,
                IntentState.PARTIALLY_FILLED,
            }
            or not 0 <= order.filled_quantity < order.quantity
            or order.status in {"filled", "canceled", "rejected", "expired"}
            or sum(
                (f.quantity for f in repo.list_fills(broker_order_id=order.broker_order_id)),
                Decimal(0),
            )
            != order.filled_quantity
        ):
            return ("CORE_UNEXPLAINED_OPEN_ORDER",)
        decision = Decision.model_validate_json(
            session.execute(
                select(table("core_decisions").c.payload).where(
                    table("core_decisions").c.id == link["lot_id"]
                )
            ).scalar_one()
        )
        entry_link = session.execute(
            select(table("core_intent_links").c.intent_id).where(
                table("core_intent_links").c.role == "ENTRY",
                table("core_intent_links").c.lot_id == link["lot_id"],
            )
        ).scalar_one_or_none()
        if member_objections(store, session, decision, entry_link):
            return ("CORE_UNEXPLAINED_OPEN_ORDER",)
        if link["role"] == "EXIT" and owned_exit_objections(
            store,
            session,
            intent.model_copy(update={"quantity": order.quantity - order.filled_quantity}),
        ):
            return ("CORE_UNEXPLAINED_OPEN_ORDER",)
        if link["role"] not in {"ENTRY", "EXIT"}:
            return ("CORE_UNEXPLAINED_OPEN_ORDER",)
        seen_clients.add(order.client_order_id)
        seen_symbols.add(order.symbol)
    return ()


def entry_inventory_objections(
    store: CoreStore,
    session: Session,
    proof: ReconciliationProof,
    *,
    now: datetime,
    latest: bool = True,
) -> tuple[str, ...]:
    """Recheck immutable certification and current locked ledger at each authority boundary."""
    encoded = session.execute(
        select(table("core_reconciliation_proofs").c.payload).where(
            table("core_reconciliation_proofs").c.id == proof.id
        )
    ).scalar_one_or_none()
    certs = table(
        "core_inventory_certifications" if proof.cash_complete else "core_quantity_certifications"
    )
    generation = session.execute(
        select(certs.c.generation).where(certs.c.proof_id == proof.id)
    ).scalar_one_or_none()
    quantities, cash, fills = store.ledger(session)
    scope_reasons: tuple[str, ...] = ()
    if proof.quarantined_symbols:
        from quantbot.paper_core.crypto_custody import scoped_quantity_objections

        scope_reasons = scoped_quantity_objections(store, session, proof, now=now, latest=latest)
    if (
        encoded is None
        or ReconciliationProof.model_validate_json(encoded) != proof
        or generation != proof.generation
        or latest
        and latest_certification(session) != (proof.generation, proof.id)
        or not proof.quantity_scope_shape_valid
        or scope_reasons
        or proof.account.account_id != store.config.account_id
        or proof.release_id != store.config.release_id
        or proof.evidence_lane != store.config.evidence_lane
        or proof.baseline_id
        != session.execute(select(table("core_fixture_baselines").c.id)).scalar_one()
        or proof.expected_quantities != tuple(sorted(quantities.items()))
        or proof.fill_ids != fills
        or proof.order_ids
        != tuple(o.broker_order_id for o in StorageRepository(session).list_broker_orders())
        or proof.expected_cash != cash
        or proof.cash_complete != (cash is not None)
        or cash is not None
        and cash != proof.account.cash
        or proof.ledger_state_id is not None
        and proof.ledger_state_id != store.ledger_state_id(session)
        or session.execute(
            select(table("core_cash_authority_revocations").c.snapshot_id).where(
                table("core_cash_authority_revocations").c.snapshot_id
                == "core-certified-" + proof.id
            )
        ).first()
    ):
        return ("CORE_INVENTORY_AUTHORITY_UNAVAILABLE",)
    if not paper_balance_policy(store.config):
        return (
            ()
            if store.cash_authority_current(session, proof)
            else ("CORE_CASH_AUTHORITY_UNAVAILABLE",)
        )
    payload = session.execute(
        select(table("core_provider_acquisitions").c.payload).where(
            table("core_provider_acquisitions").c.id == proof.acquisition_id
        )
    ).scalar_one_or_none()
    try:
        from quantbot.paper_core.authority import commission_in_session

        receipt = PaperFundsEvidence.model_validate_json(payload or "{}")
        commission = commission_in_session(store, session)
    except (ValueError, ValidationError, StateConflictError):
        return ("CORE_PAPER_FUNDS_SOURCE_INVALID",)
    if (
        proof.funds_authority != receipt.contract
        or proof.ledger_state_id is None
        or receipt.id != proof.acquisition_id
        or receipt.evidence_lane != "BROKER_PAPER"
        or receipt.release_id != store.config.release_id
        or receipt.account_id != store.config.account_id
        or receipt.store_identity != store.database.store_identity
        or receipt.config_id != store.config_id
        or receipt.commission_id != commission.id
        or receipt.endpoint != commission.endpoint
        or not stable_account_reads(receipt.first_account, receipt.second_account)
        or not stable_position_reads(receipt.first_positions, receipt.second_positions)
        or receipt.first_account.account != proof.account
        or receipt.first_positions != proof.positions
        or receipt.recorded_at_utc != proof.recorded_at_utc
        or not 0 <= (receipt.recorded_at_utc - receipt.capture_started).total_seconds() <= 5
        or not 0 <= (now - receipt.recorded_at_utc).total_seconds() <= 120
        or sorted(receipt.first_open_orders, key=lambda o: o.client_order_id)
        != sorted(receipt.second_open_orders, key=lambda o: o.client_order_id)
        or open_order_objections(store, session, receipt.first_open_orders)
        or not receipt.first_account.order_entry_allowed
        or receipt.first_account.restrictions
        or proof.account.equity <= 0
        or proof.account.cash <= 0
        or proof.account.buying_power <= 0
    ):
        return ("CORE_PAPER_FUNDS_SOURCE_INVALID",)
    return ()
