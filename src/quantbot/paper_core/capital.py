"""Account cash/quantity conservation for entry readiness; no fee allocation or ledger balancing."""

from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from quantbot.domain import Fill
from quantbot.paper_core.contracts import ReconciliationProof, SessionOpening
from quantbot.paper_core.portfolio import (
    SessionCapitalEvidence,
    record_session_capital,
    record_session_opening,
)
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.paper_core.time_evidence import TimeEvidence, acquire_time_evidence
from quantbot.research.manifest import content_id

if TYPE_CHECKING:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker


def _opening_from_sources(
    store: CoreStore, proof: ReconciliationProof, timing: TimeEvidence, session_open: datetime
) -> SessionOpening | None:
    candidate = SessionOpening(
        recorded_at_utc=store.clocks.sample()[0],
        evidence_lane=store.config.evidence_lane,
        release_id=store.config.release_id,
        account_id=store.config.account_id,
        store_identity=store.database.store_identity,
        config_id=store.config_id,
        policy_id=store.config.policy_id,
        session_date=session_open.date(),
        session_open=session_open,
        captured_at=proof.recorded_at_utc,
        equity=proof.account.equity,
        proof_id=proof.id,
        time_evidence_id=timing.id,
        source_store_identity=store.database.store_identity,
        source_release_id=proof.release_id,
    )
    try:
        record_session_opening(store, candidate)
    except ValueError:
        return None
    return candidate


def _retained_opening(store: CoreStore, session_open: datetime) -> SessionOpening | None:
    with store.database.transaction() as session:
        index, documents = table("core_session_opening_index"), table("core_session_openings")
        raw = session.execute(
            select(documents.c.payload)
            .join(index, index.c.document_id == documents.c.id)
            .where(
                index.c.account_id == store.config.account_id,
                index.c.policy_id == store.config.policy_id,
                index.c.session_date == session_open.date().isoformat(),
            )
        ).scalar_one_or_none()
        if raw:
            return SessionOpening.model_validate_json(raw)
    return None


def opening_for_session(
    store: CoreStore, timing: TimeEvidence, *, preferred_proof: ReconciliationProof | None = None
) -> SessionOpening | None:
    if not timing.samples or timing.objections:
        return None
    session_open = timing.samples[-1].provider.session_open
    if timing.samples[-1].provider.timestamp < session_open:
        # Closed-market clocks point at the next session. Scanning every old
        # proof/time receipt cannot supply an opening that has not happened.
        return None
    existing = _retained_opening(store, session_open)
    if existing is not None:
        return existing
    if preferred_proof is not None:
        candidate = _opening_from_sources(store, preferred_proof, timing, session_open)
        if candidate is not None:
            return candidate
    with store.database.transaction() as session:
        proof_index = table("core_document_index")
        proofs = tuple(
            ReconciliationProof.model_validate_json(r)
            for r in session.execute(
                select(table("core_reconciliation_proofs").c.payload)
                .join(proof_index, proof_index.c.id == table("core_reconciliation_proofs").c.id)
                .where(
                    proof_index.c.kind == "core_reconciliation_proofs",
                    proof_index.c.reference == store.config.account_id,
                    func.julianday(proof_index.c.available_at)
                    >= func.julianday(session_open.isoformat()),
                    func.julianday(proof_index.c.available_at)
                    < func.julianday((session_open + timedelta(seconds=60)).isoformat()),
                )
            ).scalars()
        )
        times = tuple(
            TimeEvidence.model_validate_json(r)
            for r in session.execute(
                select(table("core_time_evidence").c.payload).where(
                    func.julianday(
                        func.json_extract(
                            table("core_time_evidence").c.payload, "$.recorded_at_utc"
                        )
                    )
                    >= func.julianday((session_open - timedelta(seconds=5)).isoformat()),
                    func.julianday(
                        func.json_extract(
                            table("core_time_evidence").c.payload, "$.recorded_at_utc"
                        )
                    )
                    < func.julianday((session_open + timedelta(seconds=65)).isoformat()),
                )
            ).scalars()
        )
    # Retained first-minute evidence is legitimate after a fenced midday code/policy amendment.
    # Never re-anchor at the current balance when that evidence is absent.
    for proof in sorted(proofs, key=lambda p: p.recorded_at_utc):
        for source in sorted(times, key=lambda t: t.recorded_at_utc):
            candidate = _opening_from_sources(store, proof, source, session_open)
            if candidate is not None:
                return candidate
    return None


async def acquire_session_capital(
    broker: "AlpacaCoreBroker", store: CoreStore, proof: ReconciliationProof
) -> str | None:
    if store.runtime_time_evidence_id is None:
        if store.entry_policy is not None and store.entry_policy.crypto is not None:
            from quantbot.paper_core.continuous_capital import acquire_continuous_capital

            return await acquire_continuous_capital(broker, store, proof)
        return None
    timing = store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence)
    # Reconciliation may outlast the clock captured before it. Only a new,
    # genuinely adjacent source can certify a new opening; never backdate a proof.
    if timing.samples and not timing.objections:
        session_open = timing.samples[-1].provider.session_open
        now = store.clocks.sample()[0]
        if (
            0 <= (proof.recorded_at_utc - session_open).total_seconds() < 60
            and 0 <= (now - proof.recorded_at_utc).total_seconds() < 5
            and _retained_opening(store, session_open) is None
            and any(
                abs((s.wall_end - proof.recorded_at_utc).total_seconds()) > 5
                for s in timing.samples
            )
        ):
            timing = await acquire_time_evidence(broker, store.clocks, context=store.config)
            store.record("core_time_evidence", timing)
            store.runtime_time_evidence_id = timing.id
    opening = opening_for_session(store, timing, preferred_proof=proof)
    if store.entry_policy is not None and store.entry_policy.crypto is not None:
        from quantbot.paper_core.continuous_capital import acquire_continuous_capital

        return await acquire_continuous_capital(broker, store, proof)
    if opening is None:
        return None
    origin = store.read("core_reconciliation_proofs", opening.proof_id, ReconciliationProof)
    until = store.clocks.sample()[0]
    rows = await broker.adapter.get_performance_activities(
        after=opening.session_open - timedelta(microseconds=1), until=until
    )
    expected_cash = origin.account.cash
    quantities = dict(origin.actual_quantities)
    external = []
    valid = True
    seen = set()
    order_fills: dict[str, list[tuple[Fill, Decimal]]] = {}
    rounding_objections = []
    for row in rows:
        identity = str(row.get("id", ""))
        if not identity or identity in seen:
            valid = False
            continue
        seen.add(identity)
        kind = row.get("activity_type")
        if kind == "FILL":
            fill = broker.adapter._fill_from_payload(row)
            if fill.occurred_at > until:
                valid = False
                continue
            try:
                cumulative = Decimal(str(row["cum_qty"]))
                if not cumulative.is_finite() or cumulative < fill.quantity:
                    raise ValueError("INVALID_CUMULATIVE_QUANTITY")
                order_fills.setdefault(fill.broker_order_id, []).append((fill, cumulative))
            except (KeyError, InvalidOperation, ValueError):
                valid = False
                rounding_objections.append("ORDER_CUMULATIVE_QUANTITY_UNAVAILABLE")
            if fill.occurred_at <= opening.captured_at:
                continue
            sign = Decimal(1) if fill.side.value == "BUY" else Decimal(-1)
            quantities[fill.symbol] = quantities.get(fill.symbol, Decimal(0)) + sign * fill.quantity
            expected_cash -= sign * fill.quantity * fill.price
        else:
            raw_time = row.get("created_at") or row.get("transaction_time") or row.get("date")
            if not isinstance(raw_time, str):
                valid = False
                continue
            at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if at.tzinfo is None:
                if at.date() != opening.session_date:
                    continue
                if kind not in {"FEE", "CFEE"}:
                    external.append(identity)
                valid = False  # A date alone cannot say which side of opening captured this amount.
                continue
            if at <= opening.captured_at:
                continue
            if at > until:
                valid = False
                continue
            amount = Decimal(str(row.get("net_amount")))
            if not amount.is_finite():
                valid = False
                continue
            expected_cash += amount
            if kind not in {"FEE", "CFEE"}:
                external.append(identity)
    exact_fill_implied_cash = expected_cash
    order_cash_components = []
    for order_id, executions in sorted(order_fills.items()):
        executions.sort(key=lambda item: (item[0].occurred_at, item[1]))
        first = executions[0][0]
        cumulative, total, before = Decimal(0), Decimal(0), Decimal(0)
        complete_prefix = True
        for fill, reported_cumulative in executions:
            if (
                fill.symbol != first.symbol
                or fill.side != first.side
                or reported_cumulative != cumulative + fill.quantity
            ):
                complete_prefix = False
            cumulative += fill.quantity
            total += fill.quantity * fill.price
            if fill.occurred_at <= opening.captured_at:
                before += fill.quantity * fill.price
        if not complete_prefix:
            valid = False
            rounding_objections.append("ORDER_EXECUTION_PREFIX_INCOMPLETE:" + order_id)
            continue
        # Alpaca's fractional-share disclosure specifies nearest-cent transaction
        # values. Round each order's cumulative value, never the session's net P&L
        # or each partial execution. Preserve the exact execution ledger and fees.
        rounded = total.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
        rounded_before = before.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
        if abs(total - rounded) == Decimal(".005") or abs(before - rounded_before) == Decimal(
            ".005"
        ):
            # The public contract does not specify ties; require stronger evidence.
            valid = False
            rounding_objections.append("ORDER_CENT_TIE_UNVERIFIED:" + order_id)
        sign = Decimal(1) if first.side.value == "BUY" else Decimal(-1)
        expected_cash += sign * ((total - before) - (rounded - rounded_before))
        order_cash_components.append(
            dict(
                order_id=order_id,
                symbol=first.symbol,
                side=first.side.value,
                fill_ids=[fill.fill_id for fill, _ in executions],
                exact_value=total,
                exact_value_at_opening=before,
                reported_cent_value=rounded,
                reported_cent_value_at_opening=rounded_before,
            )
        )
    expected = tuple(sorted((s, q) for s, q in quantities.items() if q))
    matching = expected == tuple(sorted(proof.actual_quantities))
    residual = proof.account.cash - expected_cash
    document = dict(
        contract="SESSION_CAPITAL_SOURCE_V1",
        account_id=store.config.account_id,
        store_identity=store.database.store_identity,
        config_id=store.config_id,
        opening_id=opening.id,
        proof_id=proof.id,
        endpoint=broker.endpoint + "/v2/account/activities",
        after=opening.session_open - timedelta(microseconds=1),
        until=until,
        rows=rows,
        expected_cash=expected_cash,
        observed_cash=proof.account.cash,
        cash_residual=residual,
        quantities_match=matching,
        rounding_contract="USD_NEAREST_CENT_PER_ORDER_COMPLETE_PREFIX_V1_BOUND_0_01",
        rounding_source="https://files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf",
        exact_fill_implied_cash=exact_fill_implied_cash,
        rounding_adjustment=expected_cash - exact_fill_implied_cash,
        order_cash_components=order_cash_components,
        rounding_objections=rounding_objections,
        fee_attribution="UNKNOWN_UNLESS_CANONICALLY_PROVIDED",
    )
    identity = content_id(document)
    with store.database.transaction() as session:
        put(session, "core_provider_acquisitions", identity, document)
    evidence = SessionCapitalEvidence(
        recorded_at_utc=store.clocks.sample()[0],
        evidence_lane=store.config.evidence_lane,
        release_id=store.config.release_id,
        account_id=store.config.account_id,
        store_identity=store.database.store_identity,
        config_id=store.config_id,
        policy_id=store.config.policy_id,
        session_open=opening.session_open,
        proof_id=proof.id,
        opening_id=opening.id,
        source_document_id=identity,
        activity_start=opening.session_open - timedelta(microseconds=1),
        activity_watermark=until,
        complete=valid and matching and abs(residual) <= Decimal(".01"),
        external_flow_ids=tuple(sorted(external)),
        raw_digest=content_id(rows),
        expected_cash=expected_cash,
        observed_cash=proof.account.cash,
        cash_residual=residual,
        quantities_match=matching,
    )
    return record_session_capital(store, evidence)
