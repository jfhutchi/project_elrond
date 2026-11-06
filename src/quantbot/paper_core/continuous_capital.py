"""Continuous PAPER spending authority and a conservative carried equity loss floor.

These receipts do not reconcile historical cash, assign fees, or measure midnight equity.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo

from pydantic import Field
from sqlalchemy import select, text, true
from sqlalchemy.orm import Session

from quantbot.paper_core.contracts import PortfolioEvidence, ReconciliationProof, SessionOpening
from quantbot.paper_core.crypto_policy import account_risk_day_bounds
from quantbot.paper_core.funds import entry_inventory_objections
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.paper_core.time_evidence import TimeEvidence
from quantbot.research.manifest import content_id
from quantbot.storage.database import encode_utc

if TYPE_CHECKING:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker

ANCHOR_KIND = "core_continuous_risk_anchor"
CAPITAL_KIND = "core_continuous_capital"
CURSOR_KIND = "core_continuous_activity_cursor"
FLOW_KIND = "core_continuous_external_flow"


class AccountRiskAnchor(PortfolioEvidence):
    contract: Literal["ACCOUNT_RISK_ANCHOR_V1"] = "ACCOUNT_RISK_ANCHOR_V1"
    interpretation: Literal["PROJECT_DEFAULT_CONSERVATIVE_CARRY_FORWARD_LOSS_FLOOR"] = (
        "PROJECT_DEFAULT_CONSERVATIVE_CARRY_FORWARD_LOSS_FLOOR"
    )
    entry_policy_id: str
    currency: Literal["USD"] = "USD"
    risk_day: date
    risk_day_start: datetime
    risk_day_end: datetime
    captured_at: datetime
    measured_equity: Decimal = Field(gt=0, allow_inf_nan=False)
    equity_floor: Decimal = Field(gt=0, allow_inf_nan=False)
    proof_id: str
    funds_evidence_id: str
    bootstrap_opening_id: str
    stock_opening_id: str | None = None
    predecessor_id: str | None = None
    source_document_id: str
    coverage_start: datetime


class ContinuousActivityCoverage(PortfolioEvidence):
    contract: Literal["CONTINUOUS_ACTIVITY_COVERAGE_V1"] = "CONTINUOUS_ACTIVITY_COVERAGE_V1"
    endpoint: Literal["https://paper-api.alpaca.markets/v2/account/activities"] = (
        "https://paper-api.alpaca.markets/v2/account/activities"
    )
    scope_id: str
    bootstrap_opening_id: str
    previous_id: str | None
    coverage_start: datetime
    query_start: datetime
    activity_watermark: datetime
    protocol_complete: bool
    external_flow_ids: tuple[str, ...]
    fee_uncertainty_ids: tuple[str, ...]
    raw_digest: str
    rows: tuple[dict[str, Any], ...]


class ContinuousCapitalEvidence(PortfolioEvidence):
    contract: Literal["CONTINUOUS_PAPER_SPENDING_AUTHORITY_V1"] = (
        "CONTINUOUS_PAPER_SPENDING_AUTHORITY_V1"
    )
    interpretation: Literal["CURRENT_FUNDS_AND_FLOW_COVERAGE_NOT_CASH_RECONCILIATION"] = (
        "CURRENT_FUNDS_AND_FLOW_COVERAGE_NOT_CASH_RECONCILIATION"
    )
    entry_policy_id: str
    anchor_id: str | None
    proof_id: str
    funds_evidence_id: str
    source_document_id: str
    activity_start: datetime
    activity_watermark: datetime
    complete: bool
    external_flow_ids: tuple[str, ...] = ()
    fee_uncertainty_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def _scope(store: CoreStore) -> str:
    # Deliberately excludes the mutable release/amendment: neither clears known flows.
    return content_id(
        (store.config.account_id, store.database.store_identity, store.config.policy_id)
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


def _read(session: Session, identity: str, kind: Any) -> Any:
    raw = session.execute(
        select(table("core_provider_acquisitions").c.payload).where(
            table("core_provider_acquisitions").c.id == identity
        )
    ).scalar_one_or_none()
    if raw is None:
        return None
    document = kind.model_validate_json(raw)
    if document.id != identity:
        raise ValueError("CONTINUOUS_DOCUMENT_IDENTITY_INVALID")
    return document


def _index(session: Session, kind: str, identity: str, reference: str, at: datetime) -> None:
    index = table("core_document_index")
    if (
        session.execute(
            select(index.c.id).where(index.c.kind == kind, index.c.id == identity)
        ).first()
        is None
    ):
        session.execute(
            index.insert().values(
                kind=kind, id=identity, reference=reference, available_at=encode_utc(at)
            )
        )


def _anchors(store: CoreStore, session: Session) -> list[AccountRiskAnchor]:
    index, docs = table("core_document_index"), table("core_provider_acquisitions")
    rows = session.execute(
        select(docs.c.id, docs.c.payload)
        .join(index, index.c.id == docs.c.id)
        .where(index.c.kind == ANCHOR_KIND, index.c.reference.like(_scope(store) + ":%"))
        .order_by(index.c.available_at.desc())
        .limit(2)
    ).all()
    anchors = [AccountRiskAnchor.model_validate_json(payload) for _, payload in rows]
    if any(anchor.id != identity for anchor, (identity, _) in zip(anchors, rows, strict=True)):
        raise ValueError("CONTINUOUS_DOCUMENT_IDENTITY_INVALID")
    return anchors


def _known_flows(store: CoreStore, session: Session) -> tuple[str, ...]:
    index = table("core_document_index")
    identities = session.execute(
        select(index.c.id).where(index.c.kind == FLOW_KIND, index.c.reference == _scope(store))
    ).scalars()
    flows = {
        flow
        for identity in identities
        for flow in _read(session, identity, ContinuousActivityCoverage).external_flow_ids
    }
    flows.update(
        "LEGACY_SOURCE:" + identity
        for identity in session.execute(
            select(index.c.id).where(index.c.kind == "core_session_external_flow")
        ).scalars()
    )
    return tuple(sorted(flows))


def _trusted_opening(
    store: CoreStore, session: Session, now: datetime, *, identity: str | None = None
) -> SessionOpening | None:
    from quantbot.paper_core.portfolio import opening_balance_source_valid

    index, docs = table("core_session_opening_index"), table("core_session_openings")
    raw = session.execute(
        select(docs.c.payload)
        .join(index, index.c.document_id == docs.c.id)
        .where(
            index.c.account_id == store.config.account_id,
            index.c.store_identity == store.database.store_identity,
            index.c.policy_id == store.config.policy_id,
            index.c.session_date <= now.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
            index.c.document_id == identity if identity is not None else true(),
        )
        .order_by(index.c.session_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if raw is None:
        return None
    opening = SessionOpening.model_validate_json(raw)
    proof_raw = session.execute(
        select(table("core_reconciliation_proofs").c.payload).where(
            table("core_reconciliation_proofs").c.id == opening.proof_id
        )
    ).scalar_one_or_none()
    time_raw = session.execute(
        select(table("core_time_evidence").c.payload).where(
            table("core_time_evidence").c.id == opening.time_evidence_id
        )
    ).scalar_one_or_none()
    if proof_raw is None or time_raw is None:
        return None
    proof, timing = (
        ReconciliationProof.model_validate_json(proof_raw),
        TimeEvidence.model_validate_json(time_raw),
    )
    cert = table(
        "core_inventory_certifications" if proof.cash_complete else "core_quantity_certifications"
    )
    if (
        opening.config_id != store.config_id
        or opening.evidence_lane != "BROKER_PAPER"
        or opening.account_id != store.config.account_id
        or opening.store_identity != store.database.store_identity
        or opening.release_id != store.config.release_id
        or opening.source_release_id != proof.release_id
        or opening.source_store_identity != store.database.store_identity
        or proof.account.account_id != opening.account_id
        or proof.account.currency != "USD"
        or not opening_balance_source_valid(store, session, proof, current=False)
        or opening.equity != proof.account.equity
        or opening.captured_at != proof.recorded_at_utc
        or not 0 <= (opening.captured_at - opening.session_open).total_seconds() < 60
        or opening.captured_at > now
        or timing.objections
        or len(timing.samples) != 2
        or timing.samples[0].provider.timestamp >= timing.samples[1].provider.timestamp
        or any(
            s.provider.account_id != opening.account_id
            or s.provider.session_open != opening.session_open
            or abs((s.wall_end - opening.captured_at).total_seconds()) > 5
            for s in timing.samples
        )
        or session.execute(select(cert.c.proof_id).where(cert.c.proof_id == proof.id)).first()
        is None
        or session.execute(
            select(table("core_cash_authority_revocations").c.snapshot_id).where(
                table("core_cash_authority_revocations").c.snapshot_id
                == "core-certified-" + proof.id
            )
        ).first()
    ):
        return None
    return opening


def _anchor_verified(
    store: CoreStore, session: Session, anchor: AccountRiskAnchor, now: datetime
) -> bool:
    """Retained authority must still bind its measured proof and carried floor."""
    proofs = table("core_reconciliation_proofs")
    raw = session.execute(
        select(proofs.c.payload).where(proofs.c.id == anchor.proof_id)
    ).scalar_one_or_none()
    if raw is None:
        return False
    measured = ReconciliationProof.model_validate_json(raw)
    bootstrap = _trusted_opening(
        store, session, anchor.captured_at, identity=anchor.bootstrap_opening_id
    )
    stock = (
        _trusted_opening(store, session, anchor.captured_at, identity=anchor.stock_opening_id)
        if anchor.stock_opening_id
        else bootstrap
    )
    predecessor = (
        _read(session, anchor.predecessor_id, AccountRiskAnchor) if anchor.predecessor_id else None
    )
    source = _read(session, anchor.source_document_id, ContinuousActivityCoverage)
    bounds = account_risk_day_bounds(anchor.captured_at)
    if bootstrap is None or stock is None or source is None:
        return False
    if anchor.predecessor_id is not None and (
        predecessor is None
        or predecessor.risk_day >= anchor.risk_day
        or predecessor.account_id != anchor.account_id
        or predecessor.store_identity != anchor.store_identity
        or predecessor.bootstrap_opening_id != anchor.bootstrap_opening_id
        or predecessor.coverage_start != anchor.coverage_start
    ):
        return False
    return bool(
        anchor.account_id == store.config.account_id == measured.account.account_id
        and anchor.store_identity == store.database.store_identity == source.store_identity
        and anchor.config_id == store.config_id == source.config_id
        and anchor.release_id == store.config.release_id == measured.release_id
        and anchor.evidence_lane == measured.evidence_lane == "BROKER_PAPER"
        and anchor.currency == measured.account.currency == "USD"
        and anchor.proof_id == measured.id
        and anchor.funds_evidence_id == measured.acquisition_id
        and anchor.measured_equity == measured.account.equity
        and anchor.captured_at == measured.recorded_at_utc <= now
        and (anchor.risk_day_start, anchor.risk_day_end) == bounds
        and anchor.risk_day == anchor.captured_at.astimezone(ZoneInfo("America/New_York")).date()
        and anchor.equity_floor
        >= max(
            anchor.measured_equity,
            stock.equity,
            predecessor.equity_floor if predecessor else Decimal(0),
        )
        and anchor.coverage_start == bootstrap.session_open == source.coverage_start
        and source.bootstrap_opening_id == bootstrap.id
        and source.scope_id == _scope(store)
        and source.protocol_complete
        and not source.external_flow_ids
        and source.activity_watermark >= anchor.captured_at
        and source.raw_digest == content_id(source.rows)
    )


def _activity_flags(
    rows: tuple[dict[str, Any], ...], until: datetime
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    valid, seen, flows, fees = True, set(), set(), set()
    for row in rows:
        identity, kind = row.get("id"), row.get("activity_type")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in seen
            or not isinstance(kind, str)
        ):
            valid = False
            continue
        seen.add(identity)
        if kind not in {"FILL", "FEE", "CFEE"}:
            flows.add(identity)  # Includes unknown activity classes, never guessed amounts.
        raw = row.get("transaction_time") or row.get("created_at") or row.get("date")
        try:
            at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if at.tzinfo is None:
                if kind in {"FEE", "CFEE"}:
                    fees.add(identity)
                elif kind == "FILL":
                    valid = False
            elif at > until:
                valid = False
        except (ValueError, TypeError):
            valid = False
    return valid, tuple(sorted(flows)), tuple(sorted(fees))


async def acquire_continuous_capital(
    broker: AlpacaCoreBroker, store: CoreStore, proof: ReconciliationProof
) -> str | None:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker

    policy, now = store.entry_policy, store.clocks.sample()[0]
    if policy is None or policy.crypto is None or type(broker) is not AlpacaCoreBroker:
        raise ValueError("CONTINUOUS_POLICY_OR_ADAPTER_UNAVAILABLE")
    scope = _scope(store)
    with store.database.transaction() as session:
        if (
            entry_inventory_objections(store, session, proof, now=now)
            or proof.account.currency != "USD"
        ):
            raise ValueError("CONTINUOUS_FUNDS_UNVERIFIED")
        index = table("core_document_index")
        existing = session.execute(
            select(index.c.id)
            .where(index.c.kind == CAPITAL_KIND, index.c.reference == proof.id)
            .order_by(index.c.available_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing:
            old = _read(session, existing, ContinuousCapitalEvidence)
            old_anchor = _read(session, old.anchor_id, AccountRiskAnchor) if old.anchor_id else None
            if (
                old.complete
                and old.entry_policy_id == policy.id
                and not _known_flows(store, session)
                and old_anchor is not None
                and _anchor_verified(store, session, old_anchor, now)
            ):
                return cast(str, old.id)
        opening = _trusted_opening(store, session, now)
        anchors = _anchors(store, session)
        prior = anchors[0] if anchors else None
        if prior is not None and not _anchor_verified(store, session, prior, now):
            raise ValueError("ACCOUNT_RISK_ANCHOR_UNVERIFIED")
        if opening is None and prior is None:
            return None
        cursor = session.execute(
            select(index.c.reference).where(index.c.kind == CURSOR_KIND, index.c.id == scope)
        ).scalar_one_or_none()
        previous = _read(session, cursor, ContinuousActivityCoverage) if cursor else None
    if prior is not None:
        bootstrap_id, coverage_start = prior.bootstrap_opening_id, prior.coverage_start
    elif previous is not None:
        bootstrap_id, coverage_start = previous.bootstrap_opening_id, previous.coverage_start
    elif opening is not None:
        bootstrap_id, coverage_start = opening.id, opening.session_open
    else:
        return None
    if previous is not None and (
        previous.scope_id != scope
        or previous.bootstrap_opening_id != bootstrap_id
        or previous.coverage_start != coverage_start
        or not previous.protocol_complete
    ):
        raise ValueError("CONTINUOUS_ACTIVITY_LINEAGE_INVALID")
    watermark = previous.activity_watermark if previous else coverage_start
    query_start = max(coverage_start, watermark - timedelta(days=2))
    until = min(now, watermark + timedelta(hours=6))
    rows = tuple(
        dict(row)
        for row in await broker.adapter.get_performance_activities(
            after=query_start - timedelta(microseconds=1), until=until
        )
    )
    captured = store.clocks.sample()[0]
    valid, external, fees = _activity_flags(rows, until)
    source = ContinuousActivityCoverage(
        **_binding(store, captured),
        scope_id=scope,
        bootstrap_opening_id=bootstrap_id,
        previous_id=previous.id if previous else None,
        coverage_start=coverage_start,
        query_start=query_start,
        activity_watermark=until,
        protocol_complete=valid,
        external_flow_ids=external,
        fee_uncertainty_ids=fees,
        raw_digest=content_id(rows),
        rows=rows,
    )
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        if entry_inventory_objections(store, session, proof, now=captured):
            raise ValueError("CONTINUOUS_FUNDS_UNVERIFIED")
        current = session.execute(
            select(index.c.reference).where(index.c.kind == CURSOR_KIND, index.c.id == scope)
        ).scalar_one_or_none()
        if current != cursor:
            if external:
                # A racing successful capture cannot erase newly observed capital activity.
                put(session, "core_provider_acquisitions", source.id, source)
                _index(session, FLOW_KIND, source.id, scope, captured)
                session.commit()
                raise ValueError("ACCOUNT_EXTERNAL_CAPITAL_FLOW")
            winner_id = session.execute(
                select(index.c.id)
                .where(index.c.kind == CAPITAL_KIND, index.c.reference == proof.id)
                .order_by(index.c.available_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            winner = _read(session, winner_id, ContinuousCapitalEvidence) if winner_id else None
            winner_source = (
                _read(session, winner.source_document_id, ContinuousActivityCoverage)
                if winner is not None
                else None
            )
            if (
                winner is not None
                and winner.complete
                and winner.entry_policy_id == policy.id
                and winner_source is not None
                and winner_source.raw_digest == source.raw_digest
                and winner_source.query_start == source.query_start
                and winner_source.activity_watermark >= source.activity_watermark
                and not _known_flows(store, session)
            ):
                return cast(str, winner.id)
            raise ValueError("CONTINUOUS_ACTIVITY_AUTHORITY_CHANGED")
        put(session, "core_provider_acquisitions", source.id, source)
        if external:
            _index(session, FLOW_KIND, source.id, scope, captured)
        known = _known_flows(store, session)
        if valid:
            if cursor:
                session.execute(
                    index.update()
                    .where(index.c.kind == CURSOR_KIND, index.c.id == scope)
                    .values(reference=source.id, available_at=encode_utc(captured))
                )
            else:
                _index(session, CURSOR_KIND, scope, source.id, captured)
        reasons: tuple[str, ...] = () if valid else ("CONTINUOUS_ACTIVITY_PROTOCOL_INVALID",)
        if until < proof.recorded_at_utc:
            reasons += ("CONTINUOUS_ACTIVITY_CATCHUP_PENDING",)
        if known:
            reasons += ("ACCOUNT_EXTERNAL_CAPITAL_FLOW",)
        day_start, day_end = account_risk_day_bounds(proof.recorded_at_utc)
        day = day_start.astimezone(ZoneInfo("America/New_York")).date()
        anchors = _anchors(store, session)
        anchor = next((a for a in anchors if a.risk_day == day), None)
        predecessor = next((a for a in anchors if a.risk_day < day), None)
        if anchor is not None and anchor.entry_policy_id != policy.id:
            reasons += ("CONTINUOUS_ANCHOR_POLICY_CHANGED",)
        if anchor is None and not reasons:
            floor = max(
                proof.account.equity,
                predecessor.equity_floor if predecessor else Decimal(0),
                opening.equity if opening else Decimal(0),
            )
            anchor = AccountRiskAnchor(
                **_binding(store, captured),
                entry_policy_id=policy.id,
                risk_day=day,
                risk_day_start=day_start,
                risk_day_end=day_end,
                captured_at=proof.recorded_at_utc,
                measured_equity=proof.account.equity,
                equity_floor=floor,
                proof_id=proof.id,
                funds_evidence_id=proof.acquisition_id,
                bootstrap_opening_id=bootstrap_id,
                stock_opening_id=opening.id if opening else None,
                predecessor_id=predecessor.id if predecessor else None,
                source_document_id=source.id,
                coverage_start=coverage_start,
            )
            put(session, "core_provider_acquisitions", anchor.id, anchor)
            _index(session, ANCHOR_KIND, anchor.id, scope + ":" + day.isoformat(), captured)
        receipt = ContinuousCapitalEvidence(
            **_binding(store, captured),
            entry_policy_id=policy.id,
            anchor_id=anchor.id if anchor else None,
            proof_id=proof.id,
            funds_evidence_id=proof.acquisition_id,
            source_document_id=source.id,
            activity_start=coverage_start,
            activity_watermark=until,
            complete=not reasons and anchor is not None,
            external_flow_ids=known,
            fee_uncertainty_ids=fees,
            reasons=reasons,
        )
        put(session, "core_provider_acquisitions", receipt.id, receipt)
        _index(session, CAPITAL_KIND, receipt.id, proof.id, captured)
    return receipt.id


def continuous_budget_authority(
    store: CoreStore,
    session: Session,
    proof: ReconciliationProof,
    *,
    now: datetime,
    account_equity: Decimal,
    loss_bps: int,
    latest: bool = True,
) -> tuple[str | None, tuple[str, ...]]:
    policy = store.entry_policy
    reasons = list(entry_inventory_objections(store, session, proof, now=now, latest=latest))
    if policy is None or policy.crypto is None:
        return None, ("CONTINUOUS_POLICY_UNAVAILABLE",)
    if _known_flows(store, session):
        reasons.append("ACCOUNT_EXTERNAL_CAPITAL_FLOW")
    start, end = account_risk_day_bounds(now)
    anchor = next(
        (
            a
            for a in _anchors(store, session)
            if a.risk_day_start == start and a.risk_day_end == end
        ),
        None,
    )
    if anchor is None:
        return None, tuple(dict.fromkeys((*reasons, "ACCOUNT_RISK_ANCHOR_UNVERIFIED")))
    if (
        anchor.entry_policy_id != policy.id
        or anchor.config_id != store.config_id
        or anchor.currency != proof.account.currency
        or anchor.captured_at > now
        or anchor.equity_floor < anchor.measured_equity
        or not _anchor_verified(store, session, anchor, now)
    ):
        reasons.append("ACCOUNT_RISK_ANCHOR_UNVERIFIED")
    if account_equity * 10000 <= anchor.equity_floor * (10000 - loss_bps):
        reasons.append("ACCOUNT_CARRY_FORWARD_LOSS_HALT")
    stock_opening = _trusted_opening(store, session, now)
    if (
        stock_opening is not None
        and start <= stock_opening.captured_at < end
        and account_equity * 10000 <= stock_opening.equity * (10000 - loss_bps)
    ):
        reasons.append("SESSION_DAILY_LOSS_HALT")
    index = table("core_document_index")
    receipts = [
        _read(session, identity, ContinuousCapitalEvidence)
        for identity in session.execute(
            select(index.c.id).where(index.c.kind == CAPITAL_KIND, index.c.reference == proof.id)
        ).scalars()
    ]
    valid = False
    for receipt in receipts:
        source = _read(session, receipt.source_document_id, ContinuousActivityCoverage)
        if (
            receipt.complete
            and receipt.anchor_id == anchor.id
            and receipt.entry_policy_id == policy.id
            and receipt.funds_evidence_id == proof.acquisition_id
            and receipt.config_id == store.config_id
            and receipt.account_id == store.config.account_id
            and receipt.store_identity == store.database.store_identity
            and receipt.release_id == store.config.release_id
            and receipt.evidence_lane == "BROKER_PAPER"
            and not receipt.external_flow_ids
            and not receipt.reasons
            and 0 <= (now - receipt.recorded_at_utc).total_seconds() <= 120
            and source is not None
            and source.protocol_complete
            and source.scope_id == _scope(store)
            and source.coverage_start == anchor.coverage_start == receipt.activity_start
            and source.activity_watermark == receipt.activity_watermark >= proof.recorded_at_utc
            and source.raw_digest == content_id(source.rows)
        ):
            valid = True
    if not valid:
        reasons.append("CONTINUOUS_CAPITAL_UNVERIFIED")
    return anchor.id, tuple(dict.fromkeys(reasons))
