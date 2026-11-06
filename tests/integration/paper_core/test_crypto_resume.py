"""24-hour PAPER resume retains clock and account checks outside stock hours."""

from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.unit.paper_core.test_operational_crypto_policy import crypto_policy

from quantbot.paper_core.activation import active_in_session
from quantbot.paper_core.authority import commission_in_session, fence_entries, resume_entries
from quantbot.paper_core.time_evidence import acquire_time_evidence
from quantbot.storage import StateConflictError


@pytest.mark.asyncio
@pytest.mark.parametrize("crypto_enabled", [False, True])
async def test_resume_outside_equity_hours_requires_explicit_crypto_amendment(
    tmp_path, crypto_enabled
):
    db, store, broker, _, clocks = await prepare(
        tmp_path,
        entry_policy=crypto_policy() if crypto_enabled else None,
        initial_wall=datetime(2026, 10, 14, 22, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        fence = fence_entries(store, reason="FIXTURE_CRYPTO_RESUME", evidence_ids=("fixture",))
        process = clocks.process_id
        clocks.process_id = "OFFLINE_INDEPENDENT_GUARDIAN"
        timing = await acquire_time_evidence(broker, clocks)
        clocks.process_id = process
        store.record("core_time_evidence", timing)
        receipt, heartbeat = ready(store, timing)
        proof = store.latest_proof()
        with db.transaction() as session:
            commission = commission_in_session(store, session)
            active = active_in_session(store, session)[1]
        request = dict(
            commission_id=commission.id,
            expected_fence_id=fence,
            runtime_release_id=store.runtime_release_id,
            expected_activation_id=active.id,
            guardian_receipt_id=receipt.id,
            guardian_heartbeat_id=heartbeat.id,
            proof_id=proof.id,
            time_evidence_id=timing.id,
            approved_at=clocks.wall.isoformat(),
            approval_reference="FIXTURE_AUTHORIZED_CRYPTO_RESUME",
        )
        if crypto_enabled:
            assert resume_entries(store, request)
        else:
            with pytest.raises(StateConflictError, match="RESUME_FRESH_TIME_REQUIRED"):
                resume_entries(store, request)
    finally:
        db.close()
