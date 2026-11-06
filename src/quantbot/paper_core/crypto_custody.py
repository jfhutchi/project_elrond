"""Separate, short-lived custody authority for protective sells only.

This module never certifies quantity reconciliation, allocates a fee, closes a lot,
or grants entry funds. Callers retain the ordinary runtime and protection gates.
"""

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from quantbot.brokers.base import BrokerAccountSnapshot, BrokerModel
from quantbot.domain import BrokerOrder, Position
from quantbot.paper_core.funds import stable_account_reads, stable_position_reads
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.asset_capability import AssetCapability
    from quantbot.paper_core.contracts import ReconciliationProof
    from quantbot.paper_core.entry_policy import AssetEligibility
    from quantbot.paper_core.store import CoreStore

Quantity = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class CryptoLotBasis(BrokerModel):
    """Read under the writer lock from canonical ownership and reservation records."""

    account_id: str
    store_identity: str
    config_id: str
    entry_policy_id: str | None = None
    release_id: str
    owner: str
    evidence_lane: Literal["BROKER_PAPER", "SYNTHETIC_TEST"]
    lot_id: str
    symbol: str
    baseline_id: str
    ledger_state_id: str
    ledger_generation: int = Field(ge=0)
    activity_document_id: str
    activity_watermark: datetime
    gross_remaining: Quantity
    proven_asset_fee_quantity: Quantity = Decimal(0)
    fee_allocation_ids: tuple[str, ...] = ()
    fee_activity_ids: tuple[str, ...] = ()
    fee_source_ids: tuple[str, ...] = ()
    fee_external_event_ids: tuple[str, ...] = ()
    expected_account_quantity: Quantity
    quantity_step: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    minimum_quantity: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    asset_eligibility_id: str
    reserved_exits: Quantity
    reservation_state_id: str
    source_refs: tuple[str, ...]
    open_exit_orders: tuple[BrokerOrder, ...]

    @model_serializer(mode="wrap")
    def preserve_legacy(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        payload = dict(handler(self))
        for name in (
            "proven_asset_fee_quantity",
            "fee_allocation_ids",
            "fee_activity_ids",
            "fee_source_ids",
            "fee_external_event_ids",
        ):
            if not getattr(self, name):
                payload.pop(name, None)
        return payload

    @property
    def owned_remaining(self) -> Decimal:
        """Canonical gross fills minus independently allocated base-asset debits."""
        return self.gross_remaining - self.proven_asset_fee_quantity

    @property
    def id(self) -> str:
        return content_id(self)


class CryptoCustodyEvidence(BrokerModel):
    contract: Literal["CRYPTO_PROTECTIVE_CUSTODY_V1"] = "CRYPTO_PROTECTIVE_CUSTODY_V1"
    authority: Literal["PROTECTIVE_SELL_UPPER_BOUND_ONLY"] = "PROTECTIVE_SELL_UPPER_BOUND_ONLY"
    basis: CryptoLotBasis
    funds_evidence_id: str
    first_account: BrokerAccountSnapshot
    second_account: BrokerAccountSnapshot
    first_positions: tuple[Position, ...]
    second_positions: tuple[Position, ...]
    first_open_orders: tuple[BrokerOrder, ...]
    second_open_orders: tuple[BrokerOrder, ...]
    capture_started: datetime
    captured_at: datetime
    elapsed_seconds: float

    @property
    def id(self) -> str:
        return content_id(self)

    @property
    def custody_quantity(self) -> Decimal:
        return next(
            (p.quantity for p in self.second_positions if p.symbol == self.basis.symbol), Decimal(0)
        )

    @property
    def unexplained_quantity_shortfall(self) -> Decimal:
        return max(Decimal(0), self.basis.expected_account_quantity - self.custody_quantity)

    @property
    def bounded_quantity(self) -> Decimal:
        raw = max(
            Decimal(0),
            min(self.basis.owned_remaining, self.custody_quantity) - self.basis.reserved_exits,
        )
        bounded = (raw / self.basis.quantity_step).to_integral_value(rounding=ROUND_DOWN)
        bounded *= self.basis.quantity_step
        return bounded if bounded >= self.basis.minimum_quantity else Decimal(0)

    @property
    def exit_reason(self) -> str:
        if self.custody_quantity == 0:
            return "NO_OBSERVED_CUSTODY"
        if self.basis.reserved_exits >= min(self.basis.owned_remaining, self.custody_quantity):
            return "CUSTODY_FULLY_RESERVED"
        return (
            "PROTECTIVE_QUANTITY_AVAILABLE"
            if self.bounded_quantity
            else "BELOW_BROKER_MINIMUM_DUST"
        )


def _validate_capture(evidence: CryptoCustodyEvidence) -> None:
    basis = evidence.basis
    fee_sources = (
        basis.fee_allocation_ids,
        basis.fee_activity_ids,
        basis.fee_source_ids,
        basis.fee_external_event_ids,
    )
    if (
        basis.owned_remaining < 0
        or any(bool(ids) != bool(basis.proven_asset_fee_quantity) for ids in fee_sources)
        or any(
            len(set(ids)) != len(ids) or any(not identity for identity in ids)
            for ids in fee_sources
        )
    ):
        raise StateConflictError("CRYPTO_CUSTODY_FEE_AUTHORITY_INVALID")
    if (
        not basis.symbol.endswith("/USD")
        or not basis.source_refs
        or not 0 <= evidence.elapsed_seconds <= 5
        or not 0 <= (evidence.captured_at - evidence.capture_started).total_seconds() <= 5
        or not evidence.capture_started <= basis.activity_watermark <= evidence.captured_at
        or evidence.first_account.account.account_id != basis.account_id
        or evidence.second_account.account.account_id != basis.account_id
        or not stable_account_reads(evidence.first_account, evidence.second_account)
        or not stable_position_reads(evidence.first_positions, evidence.second_positions)
        or sorted(evidence.first_open_orders, key=lambda o: o.broker_order_id)
        != sorted(evidence.second_open_orders, key=lambda o: o.broker_order_id)
    ):
        raise StateConflictError("CRYPTO_CUSTODY_CAPTURE_UNVERIFIED")
    for positions in (evidence.first_positions, evidence.second_positions):
        if len({p.symbol for p in positions}) != len(positions):
            raise StateConflictError("CRYPTO_CUSTODY_DUPLICATE_POSITION")
    if not 0 <= evidence.custody_quantity <= basis.owned_remaining:
        raise StateConflictError("CRYPTO_CUSTODY_UNOWNED_QUANTITY")
    observed = tuple(
        sorted(
            (o for o in evidence.second_open_orders if o.symbol == basis.symbol),
            key=lambda o: o.broker_order_id,
        )
    )
    if observed != tuple(sorted(basis.open_exit_orders, key=lambda o: o.broker_order_id)):
        raise StateConflictError("CRYPTO_CUSTODY_FOREIGN_OR_CHANGED_ORDER")


def build_crypto_custody(
    *,
    basis: CryptoLotBasis,
    funds_evidence_id: str,
    first_account: BrokerAccountSnapshot,
    second_account: BrokerAccountSnapshot,
    first_positions: tuple[Position, ...],
    second_positions: tuple[Position, ...],
    first_open_orders: tuple[BrokerOrder, ...],
    second_open_orders: tuple[BrokerOrder, ...],
    capture_started: datetime,
    captured_at: datetime,
    elapsed_seconds: float,
) -> CryptoCustodyEvidence:
    evidence = CryptoCustodyEvidence(
        basis=basis,
        funds_evidence_id=funds_evidence_id,
        first_account=first_account,
        second_account=second_account,
        first_positions=first_positions,
        second_positions=second_positions,
        first_open_orders=first_open_orders,
        second_open_orders=second_open_orders,
        capture_started=capture_started,
        captured_at=captured_at,
        elapsed_seconds=elapsed_seconds,
    )
    _validate_capture(evidence)
    return evidence


def crypto_custody_limit(
    evidence: CryptoCustodyEvidence,
    current_basis: CryptoLotBasis,
    *,
    now: datetime,
    requested_quantity: Decimal | None = None,
) -> Decimal:
    """Recheck the source under the send lock; reservations reduce both upper bounds."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise StateConflictError("CRYPTO_CUSTODY_STALE")
    if not 0 <= (now - evidence.captured_at).total_seconds() <= 5:
        raise StateConflictError("CRYPTO_CUSTODY_STALE")
    if current_basis.id != evidence.basis.id:
        raise StateConflictError("CRYPTO_CUSTODY_BASIS_CHANGED")
    _validate_capture(evidence)
    limit = evidence.bounded_quantity
    if requested_quantity is not None and (
        not requested_quantity.is_finite()
        or not current_basis.minimum_quantity <= requested_quantity <= limit
        or requested_quantity % current_basis.quantity_step != 0
    ):
        raise StateConflictError("CRYPTO_CUSTODY_QUANTITY_LIMIT")
    return limit


def read_crypto_lot_basis(
    store: "CoreStore",
    session: "Session",
    lot_id: str,
    *,
    now: datetime,
    capture_started: datetime,
    activity_document_id: str,
    exclude_intent: str | None = None,
    ledger_generation: int | None = None,
    require_latest_activity: bool = True,
) -> CryptoLotBasis:
    """Derive exclusive ownership from this caller's locked durable snapshot.

    A new capture can bind the immediately next certification generation. Later
    consumers omit that argument and must see the now-current generation.
    """
    import json

    from sqlalchemy import select

    from quantbot.domain import OrderSide
    from quantbot.execution.coordinator import _assert_ack_matches_intent
    from quantbot.paper_core.contracts import Observation, ProtectionPlan
    from quantbot.paper_core.crypto_fee_allocation import lot_asset_fees
    from quantbot.paper_core.entry_policy import entry_policy_in_session
    from quantbot.paper_core.outcomes import quarantined_lots, terminal_evidence
    from quantbot.paper_core.settlement import definitively_unsent
    from quantbot.paper_core.store import latest_certification, table
    from quantbot.storage import StorageRepository

    if store.config.evidence_lane not in {"BROKER_PAPER", "SYNTHETIC_TEST"}:
        raise StateConflictError("CRYPTO_CUSTODY_LANE_UNSUPPORTED")
    repo = StorageRepository(session)
    links = table("core_intent_links")
    entries = list(
        session.execute(
            select(links).where(links.c.lot_id == lot_id, links.c.role == "ENTRY")
        ).mappings()
    )
    if len(entries) != 1 or lot_id in quarantined_lots(session):
        raise StateConflictError("CRYPTO_CUSTODY_EXCLUSIVE_ENTRY_UNAVAILABLE")
    entry = entries[0]
    intent = repo.get_order_intent(entry["intent_id"])
    if (
        intent is None
        or intent.strategy_id != store.config.owner
        or intent.side is not OrderSide.BUY
        or not intent.symbol.endswith("/USD")
    ):
        raise StateConflictError("CRYPTO_CUSTODY_ENTRY_OWNERSHIP_UNAVAILABLE")
    symbol = intent.symbol
    plan = ProtectionPlan.model_validate_json(
        session.execute(
            select(table("core_protection_plans").c.payload).where(
                table("core_protection_plans").c.id == entry["protection_id"]
            )
        ).scalar_one()
    )
    if plan.id != entry["protection_id"] or plan.owner != store.config.owner:
        raise StateConflictError("CRYPTO_CUSTODY_PROTECTION_UNVERIFIED")
    observation = Observation.model_validate_json(
        session.execute(
            select(table("core_observations").c.payload).where(
                table("core_observations").c.id == entry["observation_id"]
            )
        ).scalar_one()
    )
    terms: AssetEligibility | AssetCapability | None = observation.asset_eligibility
    if terms is None and store.config.evidence_lane == "SYNTHETIC_TEST":
        from quantbot.paper_core.asset_capability import capability_for

        terms = capability_for(store.config, symbol)
    if (
        observation.id != entry["observation_id"]
        or terms is None
        or terms.symbol != symbol
        or terms.asset_class != "crypto"
        or terms.quantity_step is None
        or terms.minimum_quantity is None
    ):
        raise StateConflictError("CRYPTO_CUSTODY_RETAINED_ASSET_TERMS_UNVERIFIED")
    baseline_row = session.execute(select(table("core_fixture_baselines"))).mappings().one()
    baseline = json.loads(baseline_row["payload"])
    if (
        content_id(baseline) != baseline_row["id"]
        or baseline["account"]["account_id"] != store.config.account_id
        or Decimal(dict(baseline["quantities"]).get(symbol, "0")) != 0
    ):
        raise StateConflictError("CRYPTO_CUSTODY_BASELINE_NOT_ZERO")
    same_symbol = list(
        session.execute(
            select(table("order_intents").c.intent_id).where(
                table("order_intents").c.symbol == symbol
            )
        ).scalars()
    )
    for candidate in repo.get_order_intents(same_symbol):
        if candidate is not None and candidate.strategy_id != store.config.owner:
            if not definitively_unsent(repo, candidate.intent_id):
                raise StateConflictError("CRYPTO_CUSTODY_FOREIGN_OWNERSHIP")
    unresolved = list(
        session.execute(
            select(links)
            .join(table("order_intents"), table("order_intents").c.intent_id == links.c.intent_id)
            .where(
                links.c.role == "ENTRY",
                table("order_intents").c.symbol == symbol,
                links.c.lot_id.not_in(select(table("core_round_trips").c.id)),
            )
        ).mappings()
    )
    if {r["lot_id"] for r in unresolved if not definitively_unsent(repo, r["intent_id"])} != {
        lot_id
    }:
        raise StateConflictError("CRYPTO_CUSTODY_AMBIGUOUS_LOTS")
    relative_fees: list[tuple[str, str]] = []
    for external_row in session.execute(select(table("core_external_owner_events"))).mappings():
        external = json.loads(external_row["payload"])
        if external.get("symbol") != symbol:
            continue
        if external.get("owner") != "ACCOUNT_CRYPTO_FEE":
            raise StateConflictError("CRYPTO_CUSTODY_FOREIGN_OWNERSHIP")
        if external.get("baseline_binding") == "ORDER_FILTER_POST_BASELINE":
            if not isinstance(external.get("lot_id"), str) or not external["lot_id"]:
                raise StateConflictError("CRYPTO_CUSTODY_FEE_ALLOCATION_MISSING")
            relative_fees.append((external_row["id"], external["lot_id"]))
    order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
    if order is None:
        raise StateConflictError("CRYPTO_CUSTODY_ENTRY_NOT_SETTLED")
    _assert_ack_matches_intent(intent, order)
    terminal = terminal_evidence(session, order, cutoff=now)
    if terminal is None:
        raise StateConflictError("CRYPTO_CUSTODY_ENTRY_NOT_SETTLED")
    buys = repo.list_fills(broker_order_id=order.broker_order_id)
    if not buys or any(
        f.symbol != symbol or f.side is not OrderSide.BUY or f.occurred_at > now for f in buys
    ):
        raise StateConflictError("CRYPTO_CUSTODY_ENTRY_FILLS_UNVERIFIED")
    sources = {terminal, entry["protection_id"], intent.intent_id, *(f.fill_id for f in buys)}
    reserved, sold = Decimal(0), Decimal(0)
    reservation_state = []
    open_exits = []
    owned_order_ids = {order.broker_order_id}
    owned_fill_ids = {f.fill_id for f in buys}
    sells = []
    for link in session.execute(
        select(links).where(links.c.lot_id == lot_id, links.c.role == "EXIT")
    ).mappings():
        exit_intent = repo.get_order_intent(link["intent_id"])
        if (
            exit_intent is None
            or exit_intent.symbol != symbol
            or exit_intent.strategy_id != store.config.owner
            or exit_intent.side is not OrderSide.SELL
        ):
            raise StateConflictError("CRYPTO_CUSTODY_EXIT_OWNERSHIP_UNVERIFIED")
        if definitively_unsent(repo, exit_intent.intent_id):
            continue
        exit_order = repo.get_broker_order_by_client_order_id(exit_intent.client_order_id)
        fills = (
            []
            if exit_order is None
            else repo.list_fills(broker_order_id=exit_order.broker_order_id)
        )
        filled = sum((f.quantity for f in fills), Decimal(0))
        if exclude_intent == exit_intent.intent_id:
            if exit_order is not None or fills:
                raise StateConflictError("CRYPTO_CUSTODY_EXCLUDED_EXIT_ALREADY_SENT")
            continue
        if (
            any(
                f.symbol != symbol or f.side is not OrderSide.SELL or f.occurred_at > now
                for f in fills
            )
            or filled > exit_intent.quantity
        ):
            raise StateConflictError("CRYPTO_CUSTODY_EXIT_FILLS_UNVERIFIED")
        settled = None
        if exit_order is not None:
            _assert_ack_matches_intent(exit_intent, exit_order)
            if filled != exit_order.filled_quantity:
                raise StateConflictError("CRYPTO_CUSTODY_EXIT_FILLS_UNVERIFIED")
            settled = terminal_evidence(session, exit_order, cutoff=now)
            owned_order_ids.add(exit_order.broker_order_id)
            from quantbot.execution.recovery import is_open_order

            if is_open_order(exit_order):
                open_exits.append(exit_order)
        if settled is None:
            reserved += exit_intent.quantity - filled
        else:
            sources.add(settled)
        reservation_state.append((exit_intent, exit_order, tuple(fills), settled))
        owned_fill_ids.update(f.fill_id for f in fills)
        sells.extend(fills)
        sold += filled
    remaining = sum((f.quantity for f in buys), Decimal(0)) - sold
    fees = lot_asset_fees(store, session, lot_id, as_of=now)
    for event_id, fee_lot in relative_fees:
        allocated = (
            fees if fee_lot == lot_id else lot_asset_fees(store, session, fee_lot, as_of=now)
        )
        if event_id not in allocated.external_event_ids:
            raise StateConflictError("CRYPTO_CUSTODY_FEE_ALLOCATION_MISSING")
    owned_remaining = remaining - fees.quantity
    sources.update(
        (*fees.allocation_ids, *fees.fee_ids, *fees.source_ids, *fees.external_event_ids)
    )
    chronological = Decimal(0)
    for fill in sorted(
        (*buys, *sells), key=lambda f: (f.occurred_at, f.side is OrderSide.SELL, f.fill_id)
    ):
        chronological += fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        if chronological < 0:
            raise StateConflictError("CRYPTO_CUSTODY_CHRONOLOGICAL_OVERSELL")
    if owned_remaining < 0 or reserved > owned_remaining:
        raise StateConflictError("CRYPTO_CUSTODY_RESERVATIONS_EXCEED_OWNERSHIP")
    progress = (
        session.execute(
            select(table("core_provider_progress")).where(
                table("core_provider_progress").c.account_id == store.config.account_id
            )
        )
        .mappings()
        .one_or_none()
    )
    encoded = session.execute(
        select(table("core_provider_acquisitions").c.payload).where(
            table("core_provider_acquisitions").c.id == activity_document_id
        )
    ).scalar_one_or_none()
    if (
        progress is None
        or encoded is None
        or require_latest_activity
        and progress["document_id"] != activity_document_id
    ):
        raise StateConflictError("CRYPTO_CUSTODY_ACTIVITY_COVERAGE_UNVERIFIED")
    activity = json.loads(encoded)
    watermark = datetime.fromisoformat(activity["until"])
    endpoint = (
        "https://paper-api.alpaca.markets"
        if store.config.evidence_lane == "BROKER_PAPER"
        else "synthetic/activities"
    )
    if (
        content_id(activity) != activity_document_id
        or activity.get("account_id") != store.config.account_id
        or activity.get("release_id") != store.config.release_id
        or activity.get("endpoint") != endpoint
        or watermark.tzinfo is None
        or not capture_started <= watermark <= now
        or (
            datetime.fromisoformat(progress["watermark"]) != watermark
            if require_latest_activity
            else datetime.fromisoformat(progress["watermark"]) < watermark
        )
    ):
        raise StateConflictError("CRYPTO_CUSTODY_ACTIVITY_COVERAGE_UNVERIFIED")
    activity_documents = [activity]
    if progress["document_id"] != activity_document_id:
        current_raw = session.execute(
            select(table("core_provider_acquisitions").c.payload).where(
                table("core_provider_acquisitions").c.id == progress["document_id"]
            )
        ).scalar_one()
        current_activity = json.loads(current_raw)
        if (
            content_id(current_activity) != progress["document_id"]
            or current_activity.get("account_id") != store.config.account_id
            or current_activity.get("release_id") != store.config.release_id
            or current_activity.get("endpoint") != endpoint
            or datetime.fromisoformat(current_activity["until"]) > now
            or datetime.fromisoformat(current_activity["until"])
            != datetime.fromisoformat(progress["watermark"])
        ):
            raise StateConflictError("CRYPTO_CUSTODY_ACTIVITY_COVERAGE_UNVERIFIED")
        activity_documents.append(current_activity)
    for raw in (raw for document in activity_documents for raw in document["rows"]):
        if raw.get("activity_type") == "FILL" and raw.get("symbol") in {
            symbol,
            symbol.replace("/", ""),
        }:
            if raw.get("order_id") not in owned_order_ids or raw.get("id") not in owned_fill_ids:
                # A retained, already closed owned lot may share the replay window.
                prior_fill = repo.get_fill(str(raw.get("id", "")))
                prior_order = repo.get_broker_order(str(raw.get("order_id", "")))
                prior = (
                    None
                    if prior_order is None
                    else session.execute(
                        select(links.c.lot_id)
                        .join(
                            table("order_intents"),
                            table("order_intents").c.intent_id == links.c.intent_id,
                        )
                        .where(
                            table("order_intents").c.client_order_id == prior_order.client_order_id,
                            table("order_intents").c.strategy_id == store.config.owner,
                            table("order_intents").c.symbol == symbol,
                            links.c.lot_id.in_(select(table("core_round_trips").c.id)),
                        )
                    ).scalar_one_or_none()
                )
                if (
                    prior is None
                    or prior_fill is None
                    or prior_order is None
                    or prior_fill.broker_order_id != prior_order.broker_order_id
                    or prior_fill.symbol != symbol
                ):
                    raise StateConflictError("CRYPTO_CUSTODY_FOREIGN_OR_UNIMPORTED_FILL")
    quantities, _, _ = store.ledger(session)
    expected = quantities.get(symbol, Decimal(0))
    if not 0 <= expected <= owned_remaining:
        raise StateConflictError("CRYPTO_CUSTODY_LEDGER_OWNERSHIP_CONTRADICTION")
    latest = latest_certification(session)
    generation = latest[0] if latest else 0
    if ledger_generation is not None:
        if ledger_generation not in {generation, generation + 1}:
            raise StateConflictError("CRYPTO_CUSTODY_GENERATION_UNVERIFIED")
        generation = ledger_generation
    policy = entry_policy_in_session(store, session)
    return CryptoLotBasis(
        account_id=store.config.account_id,
        store_identity=store.database.store_identity,
        config_id=store.config_id,
        entry_policy_id=policy.id if policy else None,
        release_id=store.config.release_id,
        owner=store.config.owner,
        evidence_lane=store.config.evidence_lane,
        lot_id=lot_id,
        symbol=symbol,
        baseline_id=baseline_row["id"],
        ledger_state_id=store.ledger_state_id(session),
        ledger_generation=generation,
        activity_document_id=activity_document_id,
        activity_watermark=watermark,
        gross_remaining=remaining,
        proven_asset_fee_quantity=fees.quantity,
        fee_allocation_ids=fees.allocation_ids,
        fee_activity_ids=fees.fee_ids,
        fee_source_ids=fees.source_ids,
        fee_external_event_ids=fees.external_event_ids,
        expected_account_quantity=expected,
        reserved_exits=reserved,
        quantity_step=terms.quantity_step,
        minimum_quantity=terms.minimum_quantity,
        asset_eligibility_id=terms.id,
        reservation_state_id=content_id(reservation_state),
        source_refs=tuple(sorted(sources | owned_fill_ids)),
        open_exit_orders=tuple(sorted(open_exits, key=lambda o: o.broker_order_id)),
    )


def scoped_quantity_objections(
    store: "CoreStore",
    session: "Session",
    proof: "ReconciliationProof",
    *,
    now: datetime,
    latest: bool = True,
) -> tuple[str, ...]:
    """Validate partial account evidence without certifying the quarantined quantities.

    Ordinary entry-funds receipt lifetime is 120 seconds. This function does not
    grant a protective sell; that separate operation requires the five-second limit.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import NoResultFound

    from quantbot.brokers.base import BrokerProtocolError
    from quantbot.paper_core.authority import commission_in_session
    from quantbot.paper_core.contracts import ReconciliationProof
    from quantbot.paper_core.funds import PaperFundsEvidence
    from quantbot.paper_core.store import latest_certification, table

    if not proof.quantity_scope_shape_valid:
        return ("CRYPTO_CUSTODY_QUANTITY_SCOPE_INVALID",)
    if not proof.quarantined_symbols:
        return ()
    try:
        if now.tzinfo is None or not 0 <= (now - proof.recorded_at_utc).total_seconds() <= 120:
            return ("CRYPTO_CUSTODY_SCOPED_PROOF_STALE",)
        if (
            proof.evidence_lane != store.config.evidence_lane
            or proof.account.account_id != store.config.account_id
            or proof.release_id != store.config.release_id
        ):
            return ("CRYPTO_CUSTODY_PROOF_BINDING",)
        raw = session.execute(
            select(table("core_reconciliation_proofs").c.payload).where(
                table("core_reconciliation_proofs").c.id == proof.id
            )
        ).scalar_one_or_none()
        if raw is None or ReconciliationProof.model_validate_json(raw) != proof:
            return ("CRYPTO_CUSTODY_PROOF_NOT_RETAINED",)
        current = latest_certification(session)
        current_generation = current[0] if current else 0
        if (
            proof.generation > current_generation + 1
            or latest
            and current is not None
            and proof.generation != current_generation + 1
            and current != (proof.generation, proof.id)
        ):
            return ("CRYPTO_CUSTODY_GENERATION_UNVERIFIED",)
        raw = session.execute(
            select(table("core_provider_acquisitions").c.payload).where(
                table("core_provider_acquisitions").c.id == proof.acquisition_id
            )
        ).scalar_one_or_none()
        if raw is None:
            return ("CRYPTO_CUSTODY_FUNDS_SOURCE_MISSING",)
        funds = PaperFundsEvidence.model_validate_json(raw)
        if (
            funds.id != proof.acquisition_id
            or funds.account_id != store.config.account_id
            or funds.store_identity != store.database.store_identity
            or funds.config_id != store.config_id
            or funds.release_id != store.config.release_id
            or funds.evidence_lane != "BROKER_PAPER"
            or funds.commission_id != commission_in_session(store, session).id
            or funds.recorded_at_utc != proof.recorded_at_utc
            or funds.first_account.account != proof.account
            or funds.first_positions != proof.positions
        ):
            return ("CRYPTO_CUSTODY_FUNDS_SOURCE_BINDING",)
        covered = set()
        for identity in proof.custody_evidence_ids:
            raw = session.execute(
                select(table("core_provider_acquisitions").c.payload).where(
                    table("core_provider_acquisitions").c.id == identity
                )
            ).scalar_one_or_none()
            if raw is None:
                return ("CRYPTO_CUSTODY_RECEIPT_MISSING",)
            evidence = CryptoCustodyEvidence.model_validate_json(raw)
            basis = evidence.basis
            if (
                evidence.id != identity
                or basis.symbol in covered
                or evidence.funds_evidence_id != funds.id
                or evidence.capture_started != funds.capture_started
                or evidence.captured_at != funds.recorded_at_utc
                or evidence.elapsed_seconds != funds.elapsed_seconds
                or evidence.first_account != funds.first_account
                or evidence.second_account != funds.second_account
                or evidence.first_positions != funds.first_positions
                or evidence.second_positions != funds.second_positions
                or evidence.first_open_orders != funds.first_open_orders
                or evidence.second_open_orders != funds.second_open_orders
                or basis.account_id != store.config.account_id
                or basis.store_identity != store.database.store_identity
                or basis.config_id != store.config_id
                or basis.release_id != proof.release_id
                or basis.evidence_lane != "BROKER_PAPER"
                or basis.ledger_state_id != proof.ledger_state_id
                or basis.ledger_generation != proof.generation
                or basis.baseline_id != proof.baseline_id
                or basis.expected_account_quantity
                != dict(proof.expected_quantities).get(basis.symbol)
                or evidence.custody_quantity
                != dict(proof.actual_quantities).get(basis.symbol, Decimal(0))
            ):
                return ("CRYPTO_CUSTODY_RECEIPT_SOURCE_BINDING",)
            _validate_capture(evidence)
            fresh = read_crypto_lot_basis(
                store,
                session,
                basis.lot_id,
                now=now,
                capture_started=evidence.capture_started,
                activity_document_id=basis.activity_document_id,
                ledger_generation=proof.generation
                if proof.generation >= current_generation
                else None,
                require_latest_activity=False,
            )
            if not latest and proof.generation < current_generation:
                fresh = fresh.model_copy(update={"ledger_generation": proof.generation})
            if fresh.id != basis.id:
                return ("CRYPTO_CUSTODY_BASIS_CHANGED",)
            covered.add(basis.symbol)
        if covered != set(proof.quarantined_symbols):
            return ("CRYPTO_CUSTODY_QUARANTINE_COVERAGE",)
    except (StateConflictError, BrokerProtocolError) as error:
        return (
            str(error)
            if str(error).startswith("CRYPTO_CUSTODY_")
            else "CRYPTO_CUSTODY_SOURCE_UNVERIFIED",
        )
    except (ValueError, TypeError, KeyError, NoResultFound):
        return ("CRYPTO_CUSTODY_SOURCE_UNVERIFIED",)
    return ()


async def capture_crypto_custody(
    store: "CoreStore",
    broker: "AlpacaCoreBroker",
    lot_id: str,
    *,
    exclude_intent: str | None = None,
) -> CryptoCustodyEvidence:
    """Fresh bounded provider capture for an actual protective exit; never certify funds.

    Recovery reuses original client identities. All reads precede the writer lock;
    local ownership and reservations are then rederived before retaining the receipt.
    """
    import asyncio

    from sqlalchemy import text

    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.authority import commission_in_session
    from quantbot.paper_core.funds import PaperFundsEvidence
    from quantbot.paper_core.guardian import _capture_crossing_sends
    from quantbot.paper_core.outcomes import recover_intents
    from quantbot.paper_core.store import put

    if (
        not isinstance(broker, AlpacaCoreBroker)
        or store.config.evidence_lane != "BROKER_PAPER"
        or broker.account_id != store.config.account_id
        or broker.endpoint != "https://paper-api.alpaca.markets"
    ):
        raise StateConflictError("CRYPTO_CUSTODY_CAPTURE_BROKER_UNVERIFIED")
    capture_started, monotonic_start = store.clocks.sample()
    async with asyncio.timeout(5):
        recovery = await recover_intents(store, broker)
        if len(recovery.source_refs) != 1:
            raise StateConflictError("CRYPTO_CUSTODY_ACTIVITY_COVERAGE_UNVERIFIED")
        first_account = await broker.get_account()
        first_positions = tuple(await broker.get_positions())
        first_orders = tuple(await broker.get_open_orders())
        second_account = await broker.get_account()
        second_positions = tuple(await broker.get_positions())
        second_orders = tuple(await broker.get_open_orders())
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        now, monotonic_now = store.clocks.sample()
        elapsed = monotonic_now - monotonic_start
        if not 0 <= elapsed <= 5:
            raise StateConflictError("CRYPTO_CUSTODY_CAPTURE_UNVERIFIED")
        if _capture_crossing_sends(store, session, capture_started=capture_started, now=now):
            raise StateConflictError("CRYPTO_CUSTODY_CAPTURE_CROSSED_SEND")
        basis = read_crypto_lot_basis(
            store,
            session,
            lot_id,
            now=now,
            capture_started=capture_started,
            activity_document_id=recovery.source_refs[0],
            exclude_intent=exclude_intent,
        )
        commission = commission_in_session(store, session)
        funds = PaperFundsEvidence(
            recorded_at_utc=now,
            evidence_lane="BROKER_PAPER",
            release_id=store.config.release_id,
            endpoint=broker.endpoint,
            account_id=store.config.account_id,
            store_identity=store.database.store_identity,
            config_id=store.config_id,
            commission_id=commission.id,
            process_id=store.clocks.process_id,
            capture_started=capture_started,
            elapsed_seconds=elapsed,
            first_account=first_account,
            second_account=second_account,
            first_positions=first_positions,
            second_positions=second_positions,
            first_open_orders=first_orders,
            second_open_orders=second_orders,
        )
        evidence = build_crypto_custody(
            basis=basis,
            funds_evidence_id=funds.id,
            first_account=first_account,
            second_account=second_account,
            first_positions=first_positions,
            second_positions=second_positions,
            first_open_orders=first_orders,
            second_open_orders=second_orders,
            capture_started=capture_started,
            captured_at=now,
            elapsed_seconds=elapsed,
        )
        if store.clocks.sample()[1] - monotonic_start > 5:
            raise StateConflictError("CRYPTO_CUSTODY_CAPTURE_UNVERIFIED")
        put(session, "core_provider_acquisitions", funds.id, funds)
        put(session, "core_provider_acquisitions", evidence.id, evidence)
    return evidence
