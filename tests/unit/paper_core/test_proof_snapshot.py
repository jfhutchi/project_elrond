"""Caller-owned SQLite snapshots must govern proof and budget reads together."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from tests.fixtures.core_m2 import prepare

from quantbot.paper_core.funds import entry_inventory_objections
from quantbot.paper_core.health import universe_health_objections
from quantbot.paper_core.roles import claim_objections
from quantbot.paper_core.store import CoreStore, certify_inventory, latest_certification, table
from quantbot.storage import StateConflictError


def publish_successor(store, original):
    proof = store.proof(
        original.account,
        original.actual_quantities,
        now=original.recorded_at_utc + timedelta(seconds=1),
        positions=original.positions,
        acquisition_id="independent-next-capture",
    )
    certify_inventory(store, proof)
    return proof


def test_latest_proof_reuses_callers_snapshot_and_keeps_session_open(tmp_path):
    db, store, _, original, _, _ = prepare(tmp_path)
    try:
        with db.transaction() as reader:
            reader.execute(text("BEGIN"))
            assert latest_certification(reader) == (original.generation, original.id)
            successor = publish_successor(store, original)
            assert store.latest_proof().id == successor.id
            assert store.latest_proof(session=reader).id == original.id
            assert latest_certification(reader) == (original.generation, original.id)
            assert (
                entry_inventory_objections(store, reader, original, now=successor.recorded_at_utc)
                == ()
            )
    finally:
        db.close()


def test_latest_proof_in_caller_session_still_rejects_revocation(tmp_path):
    db, store, _, original, _, _ = prepare(tmp_path)
    try:
        with db.transaction() as session:
            session.execute(
                table("core_cash_authority_revocations")
                .insert()
                .values(
                    snapshot_id="core-certified-" + original.id,
                    reason="EXPLICIT_SYNTHETIC_REVOCATION",
                )
            )
            with pytest.raises(StateConflictError, match="CASH_AUTHORITY_REVOKED"):
                store.latest_proof(session=session)
    finally:
        db.close()


def test_claim_budget_reads_same_snapshot_when_guardian_publishes_mid_claim(tmp_path, monkeypatch):
    db, store, _, original, _, _ = prepare(tmp_path)
    observed = []
    try:

        def health_then_publication(current_store, session):
            reasons = universe_health_objections(current_store, session)
            assert latest_certification(session) == (original.generation, original.id)
            publish_successor(store, original)
            return reasons

        def budget(current_store, session, proof, *, now):
            observed.append(proof.id)
            return SimpleNamespace(
                objections=entry_inventory_objections(
                    current_store,
                    session,
                    proof,
                    now=original.recorded_at_utc + timedelta(seconds=1),
                )
            )

        monkeypatch.setattr(CoreStore, "parallel_entries", property(lambda _: True))
        monkeypatch.setattr(
            "quantbot.paper_core.health.universe_health_objections", health_then_publication
        )
        monkeypatch.setattr("quantbot.paper_core.portfolio.portfolio_budget", budget)
        reasons = claim_objections(store)
        assert observed == [original.id]
        assert "CORE_INVENTORY_AUTHORITY_UNAVAILABLE" not in reasons
        assert store.latest_proof().generation == original.generation + 1
    finally:
        db.close()
