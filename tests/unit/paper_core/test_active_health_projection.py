"""Active health hydration is bounded by current blockers and recovery authority."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quantbot.paper_core import health as health_module
from quantbot.paper_core.contracts import Health
from quantbot.paper_core.health import active_health
from quantbot.paper_core.store import table
from quantbot.storage import StateConflictError

NOW = datetime(2026, 11, 6, 16, 30, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    table("core_health_events").create(engine)
    with Session(engine) as current:
        yield current
    engine.dispose()


def health(sequence, *, state="RETRYABLE_FAILURE", recovered_from=(), scope_id="AAPL"):
    return Health(
        recorded_at_utc=NOW,
        role="TRADER",
        scope="SYMBOL",
        scope_id=scope_id,
        state=state,
        objections=() if state == "READY" else ("BLOCKED",),
        first_blocker=None if state == "READY" else "BLOCKED",
        first_seen=NOW,
        last_seen=NOW,
        sequence=sequence,
        recovered_from=recovered_from,
    )


def insert(session, *events):
    session.execute(
        table("core_health_events").insert(),
        [{"id": h.id, "payload": h.model_dump_json()} for h in events],
    )


def test_resolved_history_is_not_rehydrated_and_active_sequence_ties_are_stable(
    session, monkeypatch
):
    historical = [health(n) for n in range(1000)]
    recovery = health(1000, state="READY", recovered_from=tuple(h.id for h in historical))
    later = health(1002, scope_id="LATER")
    tie_first = health(1001, scope_id="FIRST")
    tie_second = health(1001, scope_id="SECOND")
    insert(session, *historical, recovery, later, tie_first, tie_second)
    assert active_health(session) == (tie_first, tie_second, later)
    parsed = []
    validate = Health.model_validate_json

    def counted(payload, **kwargs):
        parsed.append(1)
        return validate(payload, **kwargs)

    monkeypatch.setattr(Health, "model_validate_json", counted)
    assert active_health(session) == (tie_first, tie_second, later)
    assert len(parsed) == 4  # Three active blockers and the one recovery authority.


def test_recovery_chains_and_same_transaction_writes_remain_visible(session):
    first = health(1)
    recovery_failure = health(2, recovered_from=(first.id,), scope_id="RECOVERY")
    insert(session, first, recovery_failure)
    assert active_health(session) == (recovery_failure,)
    final = health(3, state="READY", recovered_from=(recovery_failure.id,))
    insert(session, final)
    assert active_health(session) == ()
    late = health(4, state="MANUAL_PAUSE")
    insert(session, late)
    assert active_health(session) == (late,)


@pytest.mark.parametrize(
    "change", [{"role": "INVALID"}, {"recovered_from": "not-an-array"}, {"recovered_from": [None]}]
)
def test_invalid_recovery_source_cannot_hide_an_active_blocker(session, change):
    blocked = health(1, state="INTEGRITY_FAILURE")
    recovery = health(2, state="READY", recovered_from=(blocked.id,))
    insert(session, blocked)
    payload = recovery.model_dump(mode="json")
    payload.update(change)
    session.execute(
        table("core_health_events").insert().values(id=recovery.id, payload=json.dumps(payload))
    )
    with pytest.raises(ValidationError):
        active_health(session)


def test_invalid_active_evidence_fails_closed(session):
    blocked = health(1)
    payload = blocked.model_dump(mode="json")
    payload["state"] = "INVALID"
    session.execute(
        table("core_health_events").insert().values(id=blocked.id, payload=json.dumps(payload))
    )
    with pytest.raises(ValidationError):
        active_health(session)


def test_recovery_payload_must_match_its_immutable_identity(session):
    blocked = health(1, state="MANUAL_PAUSE")
    recovery = health(2, state="READY", recovered_from=(blocked.id,))
    insert(session, blocked)
    session.execute(
        table("core_health_events").insert().values(id="0" * 64, payload=recovery.model_dump_json())
    )
    with pytest.raises(StateConflictError, match="HEALTH_EVENT_IDENTITY_MISMATCH"):
        active_health(session)


@pytest.mark.parametrize("invalid_target", ["identity", "model"])
def test_recovered_targets_are_validated_before_sql_exclusion(session, invalid_target):
    target = health(1, state="INTEGRITY_FAILURE")
    identity = "0" * 64 if invalid_target == "identity" else target.id
    payload = target.model_dump(mode="json")
    if invalid_target == "model":
        payload["role"] = "INVALID"
    session.execute(
        table("core_health_events").insert().values(id=identity, payload=json.dumps(payload))
    )
    insert(session, health(2, state="READY", recovered_from=(identity,)))
    expected = StateConflictError if invalid_target == "identity" else ValidationError
    with pytest.raises(expected):
        active_health(session)


@pytest.mark.parametrize("corruption", ["identity", "model"])
def test_changed_recovered_payload_is_revalidated_after_warm_cache(session, corruption):
    target = health(1)
    insert(session, target, health(2, state="READY", recovered_from=(target.id,)))
    session.commit()
    assert active_health(session) == ()
    payload = target.model_dump(mode="json")
    payload["role" if corruption == "model" else "scope_id"] = "ALTERED"
    session.execute(
        table("core_health_events")
        .update()
        .where(table("core_health_events").c.id == target.id)
        .values(payload=json.dumps(payload))
    )
    expected = ValidationError if corruption == "model" else StateConflictError
    with pytest.raises(expected):
        active_health(session)
    session.rollback()
    assert active_health(session) == ()


def test_validation_and_projection_share_real_sqlite_snapshot(session):
    insert(session, health(1))
    session.commit()
    assert active_health(session)
    assert session.connection().connection.driver_connection.in_transaction


def test_rolled_back_recovery_cannot_be_cached_as_authority(session):
    target = health(1)
    insert(session, target)
    session.commit()
    assert active_health(session) == (target,)
    insert(session, health(2, state="READY", recovered_from=(target.id,)))
    assert active_health(session) == ()
    session.rollback()
    assert active_health(session) == (target,)


def test_validation_memoization_is_bounded_and_retains_no_payloads(session, monkeypatch):
    monkeypatch.setattr(health_module, "_HEALTH_VALIDATION_CACHE_SIZE", 3)
    health_module._health_validation_cache.clear()
    events = [health(n) for n in range(8)]
    insert(session, *events)
    assert active_health(session) == tuple(events)
    cache = health_module._health_validation_cache
    assert len(cache) == 3
    assert all(isinstance(key, bytes) and len(key) == 32 for key in cache)
    assert all(isinstance(value, str) and len(value) == 64 for value in cache.values())


@pytest.mark.parametrize("field", ["state", "recovered_from"])
def test_duplicate_fields_cannot_disagree_between_validation_and_sql_projection(session, field):
    target = health(1, state="MANUAL_PAUSE")
    if field == "state":
        value = target
        payload = '{"state":"READY",' + value.model_dump_json()[1:]
    else:
        insert(session, target)
        value = health(2, state="READY")
        payload = '{"recovered_from":[' + json.dumps(target.id) + "]," + value.model_dump_json()[1:]
    session.execute(table("core_health_events").insert().values(id=value.id, payload=payload))
    with pytest.raises(StateConflictError, match="HEALTH_EVENT_DUPLICATE_FIELD"):
        active_health(session)
