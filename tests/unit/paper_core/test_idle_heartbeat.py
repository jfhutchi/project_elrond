"""Compaction is limited to new non-authoritative idle observations."""

from datetime import timedelta

import pytest
from tests.fixtures.core_m2 import NOW, prepare

from quantbot.paper_core.authority import RoleHeartbeat, publish_heartbeat


@pytest.mark.parametrize("mismatch", ["ready", "receipt", "process", "evidence"])
def test_compaction_refuses_authoritative_or_unrelated_heartbeat(tmp_path, mismatch):
    db, store, *_ = prepare(tmp_path)
    old = RoleHeartbeat(
        role="guardian",
        release_id=store.runtime_release_id,
        config_id=store.config_id,
        store_identity=db.store_identity,
        owner=store.config.owner,
        generation=store.config.generation,
        process_epoch="idle-process",
        pid=1234,
        recorded_at=NOW,
        valid_until=NOW + timedelta(seconds=30),
        last_useful_progress=NOW,
        objections=("CLOSED_SESSION_IDLE",),
        evidence_ids=(),
        proof_age_seconds=0,
    )
    if mismatch == "ready":
        old = old.model_copy(update={"objections": ()})
    elif mismatch == "receipt":
        old = old.model_copy(update={"guardian_receipt_id": "retained-authority"})
    new = old.model_copy(
        update={
            "recorded_at": NOW + timedelta(seconds=5),
            "guardian_receipt_id": None,
            "objections": ("CLOSED_SESSION_IDLE",),
        }
    )
    if mismatch == "process":
        new = new.model_copy(update={"process_epoch": "different-process"})
    elif mismatch == "evidence":
        new = new.model_copy(update={"evidence_ids": ("different-evidence",)})
    try:
        publish_heartbeat(store, old)
        with pytest.raises(ValueError, match="ONLY_MATCHING_PROCESS_IDLE"):
            publish_heartbeat(store, new, replace_idle_id=old.id)
        assert [
            RoleHeartbeat.model_validate(row) for row in store.documents("core_role_heartbeats")
        ] == [old]
    finally:
        db.close()
