"""Small immutable-document store around the existing SQLite order/fill ledger."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy import Table, select, text
from sqlalchemy.orm import Session

from quantbot.domain import Account, OrderSide, Position, ReconciliationStatus
from quantbot.paper_core.contracts import CoreConfig, Evidence, Observation, ReconciliationProof
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.storage import Database, StateConflictError, StorageRepository
from quantbot.storage.schema import metadata

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


class CoreStore:
    def __init__(self, database: Database, config: CoreConfig) -> None:
        self.database = database
        self.config = config
        self.config_id = content_id(config)
        with database.transaction() as session:
            existing = session.execute(select(table("core_identities").c.id)).scalars().all()
            if existing and existing != [self.config_id]:
                raise StateConflictError("store has another owner/configuration")
            put(session, "core_identities", self.config_id, config)

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
        certs = table("core_inventory_certifications")
        with self.database.transaction() as session:
            identity = session.execute(
                select(certs.c.proof_id).order_by(certs.c.generation.desc()).limit(1)
            ).scalar_one()
        return self.read("core_reconciliation_proofs", identity, ReconciliationProof)

    def links(self) -> list[dict[str, Any]]:
        with self.database.transaction() as session:
            return [
                dict(row) for row in session.execute(select(table("core_intent_links"))).mappings()
            ]

    def ledger(self, session: Session) -> tuple[dict[str, Decimal], Decimal, tuple[str, ...]]:
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
        fills = repo.list_fills()
        for fill in fills:
            order = repo.get_broker_order(fill.broker_order_id)
            assert order is not None
            intents = [repo.get_order_intent(i) for i in links]
            intent = next(
                (
                    i
                    for i in intents
                    if i is not None and i.client_order_id == order.client_order_id
                ),
                None,
            )
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
        return (
            {s: q for s, q in quantities.items() if q != 0},
            cash,
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
    ) -> ReconciliationProof:
        with self.database.transaction() as session:
            expected, cash, fill_ids = self.ledger(session)
            baseline_id = session.execute(select(table("core_fixture_baselines").c.id)).scalar_one()
            generation = session.execute(
                select(table("core_inventory_certifications").c.generation)
                .order_by(table("core_inventory_certifications").c.generation.desc())
                .limit(1)
            ).scalar_one_or_none()
            orders = tuple(
                o.broker_order_id for o in StorageRepository(session).list_broker_orders()
            )
        return ReconciliationProof(
            recorded_at_utc=now,
            release_id=self.config.release_id,
            source_refs=fill_ids,
            account=account,
            baseline_id=baseline_id,
            generation=(generation or 0) + 1,
            fill_ids=fill_ids,
            expected_quantities=tuple(sorted(expected.items())),
            actual_quantities=tuple(sorted(quantities)),
            expected_cash=cash,
            acquisition_id=acquisition_id,
            complete=complete,
            order_ids=orders,
            positions=positions,
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
            "schema_version": 30,
            "evidence_lane": "SYNTHETIC_TEST",
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
        }


def certify_inventory(store: CoreStore, proof: ReconciliationProof) -> int:
    certs = table("core_inventory_certifications")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        put(session, "core_reconciliation_proofs", proof.id, proof)
        existing = session.execute(
            select(certs.c.generation).where(certs.c.proof_id == proof.id)
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        expected, cash, fills = store.ledger(session)
        last = session.execute(
            select(certs.c.generation).order_by(certs.c.generation.desc()).limit(1)
        ).scalar_one_or_none()
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
            or proof.expected_cash != cash
            or proof.account.cash != cash
            or proof.generation != (last or 0) + 1
        ):
            # Refusal evidence must survive; certification authority must not.
            session.commit()
            raise StateConflictError("INVENTORY_MISMATCH")
        snapshot_id = "core-certified-" + proof.id
        repo = StorageRepository(session)
        # Preserve actual marks; cost basis is independently reconstructed below.
        baseline = json.loads(
            session.execute(select(table("core_fixture_baselines").c.payload)).scalar_one()
        )
        basis = {p["symbol"]: Decimal(p["average_entry_price"]) for p in baseline["positions"]}
        links = list(session.execute(select(table("core_intent_links"))).mappings())
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
        repo.save_reconciliation(
            "core-" + proof.id,
            status=ReconciliationStatus.RECONCILED,
            started_at=proof.recorded_at_utc,
            completed_at=proof.recorded_at_utc,
            diffs=(),
            summary={"core_proof_id": proof.id, "lane": "SYNTHETIC_TEST"},
        )
        return proof.generation
