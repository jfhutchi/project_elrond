"""Narrow PAPER sizing authority; never independent cash or fee reconciliation."""

from datetime import datetime
from typing import Literal

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerAccountSnapshot
from quantbot.domain import BrokerOrder, Position
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
    if (
        encoded is None
        or ReconciliationProof.model_validate_json(encoded) != proof
        or generation != proof.generation
        or latest
        and latest_certification(session) != (proof.generation, proof.id)
        or not proof.complete
        or proof.account.account_id != store.config.account_id
        or proof.release_id != store.config.release_id
        or proof.evidence_lane != store.config.evidence_lane
        or proof.baseline_id
        != session.execute(select(table("core_fixture_baselines").c.id)).scalar_one()
        or proof.expected_quantities != tuple(sorted(quantities.items()))
        or proof.actual_quantities != proof.expected_quantities
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
        or receipt.first_account != receipt.second_account
        or receipt.first_positions != receipt.second_positions
        or receipt.first_account.account != proof.account
        or receipt.first_positions != proof.positions
        or receipt.recorded_at_utc != proof.recorded_at_utc
        or not 0 <= (receipt.recorded_at_utc - receipt.capture_started).total_seconds() <= 5
        or not 0 <= (now - receipt.recorded_at_utc).total_seconds() <= 120
        or receipt.first_open_orders
        or receipt.second_open_orders
        or not receipt.first_account.order_entry_allowed
        or receipt.first_account.restrictions
        or proof.account.equity <= 0
        or proof.account.cash <= 0
        or proof.account.buying_power <= 0
    ):
        return ("CORE_PAPER_FUNDS_SOURCE_INVALID",)
    return ()
