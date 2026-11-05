"""Explicit additive-store verification; no broker actions, migration or entry resumption."""

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from quantbot.paper_core.authority import current_runtime_event, executable_in_session
from quantbot.paper_core.release import PreparedRelease, file_hash
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError

ADDITIONS = frozenset(
    (
        "core_session_openings",
        "core_batches",
        "core_entry_reservations",
        "core_holding_deadlines",
        "core_session_opening_index",
        "core_batch_members",
        "core_holding_deadline_index",
    )
)


def _fingerprints(c: sqlite3.Connection) -> dict[str, tuple[int, str]]:
    result = {}
    names = [
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for name in sorted(set(names) - ADDITIONS - {"schema_version", "alembic_version"}):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = [r[1] for r in c.execute("PRAGMA table_info(" + quoted + ")")]
        order = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
        digest, count = hashlib.sha256(), 0
        for row in c.execute("SELECT * FROM " + quoted + " ORDER BY " + order):
            # repr preserves SQLite scalar type distinctions, including byte blobs.
            encoded = repr(tuple(row)).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
            count += 1
        result[name] = (count, digest.hexdigest())
    return result


def record_additive_migration(
    store: CoreStore,
    successor: PreparedRelease,
    *,
    backup: Path,
    backup_sha256: str,
    approval_reference: str,
) -> str:
    if (
        not backup.is_absolute()
        or not backup.is_file()
        or backup.is_symlink()
        or backup.resolve() == store.database.path
        or file_hash(backup) != backup_sha256
        or not approval_reference
    ):
        raise StateConflictError("MIGRATION_VERIFIED_BACKUP_REQUIRED")
    assert store.database.path is not None
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        prior, fence = executable_in_session(session), current_runtime_event(session)
        if (
            prior is None
            or fence.action != "FENCE_ENTRY"
            or (prior.release.schema_version, successor.schema_version) != (36, 37)
        ):
            raise StateConflictError("MIGRATION_EXACT_FENCED_ADDITIVE_TRANSITION_REQUIRED")
        with (
            sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as old,
            sqlite3.connect(store.database.path.as_uri() + "?mode=ro", uri=True) as current,
        ):
            old.execute("PRAGMA query_only=ON")
            current.execute("PRAGMA query_only=ON")
            old.execute("BEGIN")
            current.execute("BEGIN")
            old_schema = old.execute("SELECT version,store_identity FROM schema_version").fetchone()
            new_schema = current.execute(
                "SELECT version,store_identity FROM schema_version"
            ).fetchone()
            if (
                old_schema != (36, store.database.store_identity)
                or new_schema != (37, store.database.store_identity)
                or old.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or (current.execute("PRAGMA integrity_check").fetchone()[0] != "ok")
            ):
                raise StateConflictError("MIGRATION_STORE_OR_INTEGRITY_MISMATCH")
            before, after = _fingerprints(old), _fingerprints(current)
            if before != after or any(
                current.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
                for name in ADDITIONS
            ):
                raise StateConflictError("MIGRATION_EXISTING_HISTORY_CHANGED")
        document = dict(
            kind="ENTRY_POLICY_ADDITIVE_MIGRATION_V1",
            from_schema=36,
            to_schema=37,
            store_identity=store.database.store_identity,
            old_binding_id=prior.id,
            old_release_id=prior.release.id,
            new_release_id=successor.id,
            fence_id=fence.id,
            backup_path=str(backup),
            backup_sha256=backup_sha256,
            preserved_tables=before,
            preserved_digest=content_id(before),
            recorded_at=store.clocks.sample()[0].isoformat(),
            approval_reference=approval_reference,
        )
        identity = content_id(document)
        put(session, "core_runtime_config_history", identity, document)
        return identity


def compatible_schema_transition(
    store: CoreStore,
    session: Any,
    prior: Any,
    successor: PreparedRelease,
    approval: dict[str, Any],
) -> bool:
    if prior.release.schema_version == successor.schema_version:
        return True
    identity = approval.get("migration_evidence_id")
    encoded = session.execute(
        select(table("core_runtime_config_history").c.payload).where(
            table("core_runtime_config_history").c.id == identity
        )
    ).scalar_one_or_none()
    if encoded is None:
        return False
    proof = json.loads(encoded)
    return bool(
        content_id(proof) == identity
        and proof.get("kind") == "ENTRY_POLICY_ADDITIVE_MIGRATION_V1"
        and (proof.get("from_schema"), proof.get("to_schema")) == (36, 37)
        and (prior.release.schema_version, successor.schema_version) == (36, 37)
        and proof.get("store_identity") == store.database.store_identity
        and proof.get("old_binding_id") == prior.id
        and proof.get("new_release_id") == successor.id
        and proof.get("fence_id") == approval.get("expected_fence_id")
        and 0
        <= (store.clocks.sample()[0] - datetime.fromisoformat(proof["recorded_at"])).total_seconds()
        <= 120
    )


def bind_runtime_entries(store: CoreStore, config: Any, *, expected_generation: int) -> str:
    """Explicit binding only while entries are fenced and independent quantity proof is flat."""
    from quantbot.paper_core.account_control import bind_entry_account, require_resource_control
    from quantbot.paper_core.authority import commission_in_session
    from quantbot.paper_core.funds import PaperFundsEvidence, entry_inventory_objections
    from quantbot.storage import StorageRepository

    require_resource_control(config.entry_policy, config.request_control)
    if config.request_control is None:
        raise StateConflictError("ACCOUNT_ENTRY_CONTROL_REQUIRED")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        commission_in_session(store, session)
        current, fence = executable_in_session(session), current_runtime_event(session)
        proof = store.latest_proof()
        if current is None or current.release.config != config or fence.action != "FENCE_ENTRY":
            raise StateConflictError("ACCOUNT_ENTRY_EXACT_CURRENT_FENCE_REQUIRED")
        source = session.execute(
            select(table("core_provider_acquisitions").c.payload).where(
                table("core_provider_acquisitions").c.id == proof.acquisition_id
            )
        ).scalar_one_or_none()
        try:
            funds = PaperFundsEvidence.model_validate_json(source or "{}")
        except ValueError as error:
            raise StateConflictError("ACCOUNT_ENTRY_CURRENT_FLAT_PROOF_REQUIRED") from error
        if (
            not proof.complete
            or proof.expected_quantities
            or proof.actual_quantities
            or funds.first_open_orders
            or funds.second_open_orders
            or entry_inventory_objections(store, session, proof, now=store.clocks.sample()[0])
            or not 0 <= (store.clocks.sample()[0] - proof.recorded_at_utc).total_seconds() <= 120
            or StorageRepository(session).list_unresolved_order_intents()
        ):
            raise StateConflictError("ACCOUNT_ENTRY_CURRENT_FLAT_PROOF_REQUIRED")
        evidence = dict(
            kind="ACCOUNT_ENTRY_FENCED_FLAT_PROOF_V1",
            fence_id=fence.id,
            binding_id=current.id,
            proof_id=proof.id,
        )
        evidence_id = content_id(evidence)
        put(session, "core_runtime_config_history", evidence_id, evidence)
        identity = bind_entry_account(
            config.request_control,
            store_identity=store.database.store_identity,
            release_id=current.release.id,
            expected_generation=expected_generation,
            evidence_id=evidence_id,
        )
        # The identical sidecar document is required at trader startup. An interrupted
        # two-store operation stays fenced and can recover with these same identities.
        from quantbot.paper_core.account_control import AccountEntryLease

        lease = AccountEntryLease(
            config.request_control, store.database.store_identity, current.release.id
        )
        try:
            lease.acquire()
            assert lease.document is not None
            put(session, "core_runtime_config_history", identity, lease.document)
        finally:
            lease.release()
        return identity
