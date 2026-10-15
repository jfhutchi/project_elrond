"""Small immutable-document store around the existing SQLite order/fill ledger."""

from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import Table, select, text, union_all
from sqlalchemy.orm import Session

from quantbot.domain import Account, OrderSide, Position, ReconciliationStatus
from quantbot.paper_core.contracts import CoreConfig, Evidence, Observation, ReconciliationProof
from quantbot.paper_core.time_evidence import Clocks, SystemClocks
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage import Database, StateConflictError, StorageRepository
from quantbot.storage.schema import SCHEMA_VERSION, metadata

T = TypeVar("T", bound=BaseModel)


def table(name: str) -> Table:
    return metadata.tables[name]


def put(session: Session, name: str, identity: str, payload: object) -> None:
    target = table(name)
    encoded = canonical_result_json(payload)
    existing = session.execute(
        select(target.c.payload).where(target.c.id == identity)
    ).scalar_one_or_none()
    if existing is not None:
        if existing != encoded:
            raise StateConflictError(f"conflicting immutable {name} {identity}")
        return
    session.execute(target.insert().values(id=identity, payload=encoded))
    from quantbot.paper_core.work import notify_document

    notify_document(session, name, identity, encoded)


def latest_certification(session: Session) -> tuple[int, str] | None:
    full, quantity = table("core_inventory_certifications"), table("core_quantity_certifications")
    combined = union_all(
        select(full.c.generation, full.c.proof_id),
        select(quantity.c.generation, quantity.c.proof_id),
    ).subquery()
    row = session.execute(select(combined).order_by(combined.c.generation.desc()).limit(1)).first()
    return (int(row[0]), str(row[1])) if row else None


class CoreStore:
    runtime_time_evidence_id: str | None = None
    runtime_role: str | None = None
    runtime_submission_enabled: bool = False
    runtime_entry_enabled: bool = True
    runtime_settings: Any = None

    def __init__(
        self,
        database: Database,
        config: CoreConfig,
        *,
        clocks: Clocks | None = None,
        runtime_release_id: str | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.clocks = clocks or SystemClocks()
        self.config_id = content_id(config)
        self.runtime_release_id = runtime_release_id or config.release_id
        with database.transaction() as session:
            existing = session.execute(select(table("core_identities").c.id)).scalars().all()
            if existing and existing != [self.config_id]:
                raise StateConflictError("store has another owner/configuration")
            put(session, "core_identities", self.config_id, config)
            # Explicit raw historical fixtures can populate a new schema after
            # creation. Seed their projections once; ordinary migration seeds in SQL.
            if session.execute(select(table("core_document_index").c.id).limit(1)).first() is None:
                from quantbot.paper_core.work import DOCUMENT_REFERENCES, index_document

                for name in DOCUMENT_REFERENCES:
                    for row in session.execute(select(table(name))).mappings():
                        index_document(session, name, row["id"], json.loads(row["payload"]))

    def record(self, name: str, value: Evidence) -> str:
        with self.database.transaction() as session:
            put(session, name, value.id, value)
        return value.id

    def documents(self, name: str) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            rows = session.execute(
                select(table(name).c.payload).order_by(table(name).c.id)
            ).scalars()
            return [json.loads(row) for row in rows]

    def read(self, name: str, identity: str, kind: type[T]) -> T:
        with self.database.transaction() as session:
            payload = session.execute(
                select(table(name).c.payload).where(table(name).c.id == identity)
            ).scalar_one()
        return kind.model_validate_json(payload)

    def latest_proof(self) -> ReconciliationProof:
        with self.database.transaction() as session:
            latest = latest_certification(session)
            assert latest is not None
            identity = latest[1]
            revoked = table("core_cash_authority_revocations")
            if session.execute(
                select(revoked.c.snapshot_id).where(
                    revoked.c.snapshot_id == "core-certified-" + identity
                )
            ).first():
                raise StateConflictError("CASH_AUTHORITY_REVOKED_RECONCILIATION_REQUIRED")
            proof = ReconciliationProof.model_validate_json(
                session.execute(
                    select(table("core_reconciliation_proofs").c.payload).where(
                        table("core_reconciliation_proofs").c.id == identity
                    )
                ).scalar_one()
            )
            if proof.cash_complete and not self.cash_authority_current(session, proof):
                raise StateConflictError("CASH_AUTHORITY_STALE_RECONCILIATION_REQUIRED")
            return proof

    def cash_authority_current(self, session: Session, proof: ReconciliationProof) -> bool:
        quantities, cash, fills = self.ledger(session)
        return (
            proof.cash_complete
            and cash is not None
            and proof.expected_cash == cash
            and proof.account.cash == cash
            and set(proof.fill_ids) == set(fills)
            and dict(proof.expected_quantities) == {s: q for s, q in quantities.items() if q != 0}
        )

    def revoke_stale_cash(self, session: Session, *, source_ids: tuple[str, ...]) -> None:
        """Revocation and accepted ledger changes share one business transaction."""
        revoked = table("core_cash_authority_revocations")
        certifications = table("core_inventory_certifications")
        quantities, cash, fills = self.ledger(session)
        for row in session.execute(
            select(certifications).where(
                certifications.c.snapshot_id.not_in(select(revoked.c.snapshot_id))
            )
        ).mappings():
            proof = ReconciliationProof.model_validate_json(
                session.execute(
                    select(table("core_reconciliation_proofs").c.payload).where(
                        table("core_reconciliation_proofs").c.id == row["proof_id"]
                    )
                ).scalar_one()
            )
            if not (
                proof.cash_complete
                and cash is not None
                and proof.expected_cash == cash
                and proof.account.cash == cash
                and set(proof.fill_ids) == set(fills)
                and dict(proof.expected_quantities) == quantities
            ):
                session.execute(
                    revoked.insert().values(
                        snapshot_id=row["snapshot_id"],
                        reason=canonical_result_json(
                            {"reason": "CURRENT_LEDGER_CHANGED", "source_ids": source_ids}
                        ),
                    )
                )
                from quantbot.paper_core.work import revoked_proof

                revoked_proof(session, row["proof_id"])

    def links(self) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            return [
                dict(row) for row in session.execute(select(table("core_intent_links"))).mappings()
            ]

    def ledger(
        self, session: Session
    ) -> tuple[dict[str, Decimal], Decimal | None, tuple[str, ...]]:
        """Derive holdings from frozen inherited baseline + attributable canonical fills."""
        baseline = session.execute(select(table("core_fixture_baselines").c.payload)).scalar_one()
        payload = json.loads(baseline)
        quantities = {s: Decimal(q) for s, q in payload["quantities"]}
        cash = Decimal(payload["account"]["cash"])
        repo = StorageRepository(session)
        links = {
            row["intent_id"]: row
            for row in session.execute(select(table("core_intent_links"))).mappings()
        }
        totals: dict[str, Decimal] = {}
        intents_by_client = {
            intent.client_order_id: intent
            for i in links
            if (intent := repo.get_order_intent(i)) is not None
        }
        orders_by_id = {o.broker_order_id: o for o in repo.list_broker_orders()}
        fills = repo.list_fills()
        if self.config.evidence_lane == "BROKER_PAPER":
            clients = set(intents_by_client)
            fills = [
                f
                for f in fills
                if (order := orders_by_id.get(f.broker_order_id)) is not None
                and order.client_order_id in clients
            ]
        from quantbot.paper_core.costs import actual_fee, current_costs

        costs = current_costs(session, cutoff=datetime.max.replace(tzinfo=UTC))
        cash_complete = True
        for row in session.execute(select(table("core_external_owner_events").c.payload)).scalars():
            external = json.loads(row)
            quantities[external["symbol"]] = quantities.get(
                external["symbol"], Decimal(0)
            ) + Decimal(external["quantity_delta"])
            if external["net_cash"] is None:
                cash_complete = False
            else:
                cash += Decimal(external["net_cash"])
        for fill in fills:
            order = orders_by_id.get(fill.broker_order_id)
            assert order is not None
            intent = intents_by_client.get(order.client_order_id)
            if intent is None or fill.symbol != intent.symbol or fill.side is not intent.side:
                raise StateConflictError("unowned or inconsistent fill")
            totals[fill.broker_order_id] = (
                totals.get(fill.broker_order_id, Decimal(0)) + fill.quantity
            )
            if totals[fill.broker_order_id] > order.filled_quantity:
                raise StateConflictError("fill quantity exceeds acknowledged ownership")
            sign = Decimal(1) if fill.side is OrderSide.BUY else Decimal(-1)
            quantities[fill.symbol] = quantities.get(fill.symbol, Decimal(0)) + sign * fill.quantity
            cash -= sign * fill.quantity * fill.price
            fee = actual_fee(fill, costs)
            if fee is None:
                cash_complete = False
            else:
                cash -= fee
        # Cash movements are an independent provider ledger, never a balancing
        # delta derived from the observed cash we are about to compare.
        from quantbot.paper_core.contracts import CashActivity

        activities = [
            CashActivity.model_validate_json(p)
            for p in session.execute(select(table("core_cash_activities").c.payload)).scalars()
        ]
        complete = [
            a
            for a in activities
            if a.complete and set(dict(a.movements)) == {f.fill_id for f in fills}
        ]
        if complete:
            authority = max(complete, key=lambda a: (a.watermark, a.id))
            cash = Decimal(payload["account"]["cash"]) + sum(
                (v for _, v in authority.movements), Decimal(0)
            )
            cash_complete = True
        return (
            {s: q for s, q in quantities.items() if q != 0},
            cash if cash_complete else None,
            tuple(f.fill_id for f in fills),
        )

    def proof(
        self,
        account: Account,
        quantities: tuple[tuple[str, Decimal], ...],
        *,
        now: datetime,
        positions: tuple[Position, ...],
        acquisition_id: str,
        complete: bool = True,
        session: Session | None = None,
    ) -> ReconciliationProof:
        with (
            nullcontext(session) if session is not None else self.database.transaction()
        ) as session:
            expected, cash, fill_ids = self.ledger(session)
            baseline_id = session.execute(select(table("core_fixture_baselines").c.id)).scalar_one()
            certification = latest_certification(session)
            generation = certification[0] if certification else 0
            orders = tuple(
                o.broker_order_id for o in StorageRepository(session).list_broker_orders()
            )
            ledger_state_id = self.ledger_state_id(session)
        return ReconciliationProof(
            ledger_state_id=ledger_state_id,
            recorded_at_utc=now,
            evidence_lane=self.config.evidence_lane,
            release_id=self.config.release_id,
            source_refs=fill_ids,
            account=account,
            baseline_id=baseline_id,
            generation=(generation or 0) + 1,
            fill_ids=fill_ids,
            expected_quantities=tuple(sorted(expected.items())),
            actual_quantities=tuple(sorted(quantities)),
            expected_cash=cash,
            cash_complete=cash is not None,
            acquisition_id=acquisition_id,
            complete=complete,
            order_ids=orders,
            positions=positions,
        )

    def ledger_state_id(self, session: Session) -> str:
        """Stable economics only; polling receipts and market marks grant no authority."""
        from quantbot.paper_core.contracts import CashActivity
        from quantbot.paper_core.costs import current_costs

        repo = StorageRepository(session)
        external = [
            {
                key: json.loads(payload).get(key)
                for key in (
                    "event_id",
                    "account_id",
                    "owner",
                    "symbol",
                    "quantity_delta",
                    "net_cash",
                    "order_id",
                    "occurred_at",
                    "activity_type",
                )
            }
            for payload in session.execute(
                select(table("core_external_owner_events").c.payload)
            ).scalars()
        ]
        cash_movements = {
            content_id((a.account_id, a.movements, a.activity_ids, a.complete))
            for payload in session.execute(
                select(table("core_cash_activities").c.payload)
            ).scalars()
            for a in (CashActivity.model_validate_json(payload),)
        }
        actual_costs = current_costs(session, cutoff=datetime.max.replace(tzinfo=UTC))
        from quantbot.paper_core.health import independent_cash_available

        independent_cash = independent_cash_available(self, session)
        return content_id(
            {
                "cash_derivation_contract": "EXACT_INDEPENDENT_CASH_V2"
                if independent_cash
                else "FILL_COST_LEDGER_V2",
                "orders": sorted(
                    (o.model_dump(exclude={"submitted_at"}) for o in repo.list_broker_orders()),
                    key=canonical_result_json,
                ),
                "fills": repo.list_fills(),
                "external": sorted(external, key=canonical_result_json),
                "cash_movements": sorted(cash_movements),
                "actual_cost_revisions": sorted(
                    item.id for item in actual_costs.values() if item.source != "MODELED"
                )
                if not independent_cash
                else [],
            }
        )

    def export_loop(self, decision_id: str) -> dict[str, Any]:
        decisions = self.documents("core_decisions")
        decision = next(d for d in decisions if content_id(d) == decision_id)
        links = [link for link in self.links() if link["decision_id"] == decision_id]
        with self.database.transaction() as session:
            repo = StorageRepository(session)
            intents = [repo.get_order_intent(link["intent_id"]) for link in links]
            orders = [
                repo.get_broker_order_by_client_order_id(i.client_order_id) for i in intents if i
            ]
            fills = [
                f for o in orders if o for f in repo.list_fills(broker_order_id=o.broker_order_id)
            ]
        round_trips = [
            r for r in self.documents("core_round_trips") if r["decision_id"] == decision_id
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "evidence_lane": self.config.evidence_lane,
            "config": self.config.model_dump(mode="json"),
            "decision_id": decision_id,
            "decision": decision,
            "observation": self.read(
                "core_observations",
                decision["observation_id"],
                Observation,
            ).model_dump(mode="json"),
            "links": links,
            "intents": [i.model_dump(mode="json") for i in intents if i],
            "orders": [o.model_dump(mode="json") for o in orders if o],
            "fills": [f.model_dump(mode="json") for f in fills],
            "protection_plans": self.documents("core_protection_plans"),
            "guardian_receipts": self.documents("core_guardian_receipts"),
            "proofs": self.documents("core_reconciliation_proofs"),
            "round_trip": round_trips[0] if round_trips else None,
            "outcome_revisions": self.documents("core_outcome_revisions"),
            "cost_items": self.documents("core_cost_items"),
            "broker_sources": self.documents("core_broker_event_sources"),
        }


def certify_inventory(
    store: CoreStore, proof: ReconciliationProof, *, session: Session | None = None
) -> int:
    certs = table(
        "core_inventory_certifications" if proof.cash_complete else "core_quantity_certifications"
    )
    owns_transaction = session is None
    with nullcontext(session) if session is not None else store.database.transaction() as session:
        if owns_transaction:
            session.execute(text("BEGIN IMMEDIATE"))
        put(session, "core_reconciliation_proofs", proof.id, proof)
        existing = session.execute(
            select(certs.c.generation).where(certs.c.proof_id == proof.id)
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        expected, cash, fills = store.ledger(session)
        latest = latest_certification(session)
        last = latest[0] if latest else 0
        if (
            not proof.complete
            or proof.account.account_id != store.config.account_id
            or proof.expected_quantities != tuple(sorted(expected.items()))
            or proof.actual_quantities != tuple(sorted(expected.items()))
            or proof.baseline_id
            != session.execute(select(table("core_fixture_baselines").c.id)).scalar_one()
            or proof.order_ids
            != tuple(o.broker_order_id for o in StorageRepository(session).list_broker_orders())
            or proof.release_id != store.config.release_id
            or proof.fill_ids != fills
            or (
                proof.ledger_state_id is not None
                and proof.ledger_state_id != store.ledger_state_id(session)
            )
            or proof.expected_cash != cash
            or proof.cash_complete != (cash is not None)
            or (cash is not None and proof.account.cash != cash)
            or proof.generation != (last or 0) + 1
        ):
            # Refusal evidence must survive; certification authority must not.
            session.commit()
            raise StateConflictError("INVENTORY_MISMATCH")
        snapshot_id = ("core-certified-" if proof.cash_complete else "core-quantity-") + proof.id
        repo = StorageRepository(session)
        # Preserve actual marks; cost basis is independently reconstructed below.
        baseline = json.loads(
            session.execute(select(table("core_fixture_baselines").c.payload)).scalar_one()
        )
        basis = {p["symbol"]: Decimal(p["average_entry_price"]) for p in baseline["positions"]}
        links = list(
            session.execute(
                select(table("core_intent_links")).where(
                    table("core_intent_links").c.lot_id.not_in(
                        select(table("core_round_trips").c.id)
                    )
                )
            ).mappings()
        )
        for entry in (link for link in links if link["role"] == "ENTRY"):
            intent = repo.get_order_intent(entry["intent_id"])
            assert intent is not None
            order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
            if order is None:
                continue
            entry_fills = repo.list_fills(broker_order_id=order.broker_order_id)
            owned = sum((f.quantity for f in entry_fills), Decimal(0))
            exited = Decimal(0)
            for exit_link in (
                link
                for link in links
                if link["lot_id"] == entry["lot_id"] and link["role"] == "EXIT"
            ):
                exit_intent = repo.get_order_intent(exit_link["intent_id"])
                assert exit_intent is not None
                exit_order = repo.get_broker_order_by_client_order_id(exit_intent.client_order_id)
                if exit_order:
                    exited += sum(
                        (
                            f.quantity
                            for f in repo.list_fills(broker_order_id=exit_order.broker_order_id)
                        ),
                        Decimal(0),
                    )
            if exited > owned:
                raise StateConflictError("owned exit exceeds owned entry")
            if owned > exited:
                basis[intent.symbol] = (
                    sum((f.price * f.quantity for f in entry_fills), Decimal(0)) / owned
                )
        if (
            len({p.symbol for p in proof.positions}) != len(proof.positions)
            or {p.symbol: p.quantity for p in proof.positions} != expected
            or any(p.average_entry_price != basis[p.symbol] for p in proof.positions)
        ):
            session.commit()
            raise StateConflictError("INVENTORY_BASIS_MISMATCH")
        repo.save_account_snapshot(
            snapshot_id, proof.account, proof.positions, captured_at=proof.recorded_at_utc
        )
        session.execute(
            certs.insert().values(
                generation=proof.generation, proof_id=proof.id, snapshot_id=snapshot_id
            )
        )
        if not proof.cash_complete:
            revoked = table("core_cash_authority_revocations")
            full = table("core_inventory_certifications")
            for snapshot in session.execute(
                select(full.c.snapshot_id).where(
                    full.c.snapshot_id.not_in(select(revoked.c.snapshot_id))
                )
            ).scalars():
                session.execute(
                    revoked.insert().values(snapshot_id=snapshot, reason="CURRENT_CASH_INCOMPLETE")
                )
                from quantbot.paper_core.work import revoked_proof

                revoked_proof(session, snapshot.removeprefix("core-certified-"))
            return proof.generation
        repo.save_reconciliation(
            "core-" + proof.id,
            status=ReconciliationStatus.RECONCILED,
            started_at=proof.recorded_at_utc,
            completed_at=proof.recorded_at_utc,
            diffs=(),
            summary={"core_proof_id": proof.id, "lane": store.config.evidence_lane},
        )
        return proof.generation
