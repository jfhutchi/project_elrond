"""Remove only redundant guardian membership histories from a CLOSED offline backup.

Default is read-only dry-run. --candidate must be a NEW file; never use an operating
store. Raw receipts, ledger rows, identities and pointers are retained. This tool
never installs a candidate. Rollback requires the preserved unpruned backup or an
explicit verified --rebuild-selected copy BEFORE running old projection writers.
An optional --verified-backup-receipt must be a trusted completed validator artifact:
prior integrity/FK checks and hash must have shared a deny-write/delete lease. This
tool freshly hashes every source byte under its own continuous lease before reuse;
candidate integrity/FK checks always run.
Run with the project environment: python -m scripts.offline_guardian_projection_cleanup.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from itertools import groupby, zip_longest
from pathlib import Path

from scripts.offline_sqlite_compaction import check, connect, quote, standalone
from sqlalchemy import create_engine, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from quantbot.domain import IntentState, OrderSide, OrderType, TimeInForce
from quantbot.paper_core.contracts import (
    CoreConfig,
    Decision,
    EntryIntent,
    GuardianReceipt,
    Observation,
    ProtectionPlan,
)
from quantbot.paper_core.settlement import REFUSAL, definitively_unsent
from quantbot.paper_core.store import table
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository

MEMBERSHIPS = "core_guardian_memberships"


@contextmanager
def read_lease(source):
    """Windows kernel lease denies writers and deletion throughout the source audit/copy."""
    source = standalone(Path(source))
    if os.name != "nt":
        raise ValueError("Windows deny-write/delete source lease required")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = create(str(source), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        standalone(source)
        yield source
    finally:
        kernel.CloseHandle(handle)


class Budget:
    def __init__(
        self,
        seconds=None,
        progress=None,
        scratch_directory=None,
        verified_backup_receipt=None,
        source_check="full",
    ):
        if source_check not in {"full", "derived-proof"} or (
            source_check != "full" and verified_backup_receipt is not None
        ):
            raise ValueError("unsupported or conflicting source validation")
        self.source_check = source_check
        self.deadline = None if seconds is None else time.monotonic() + seconds
        self.progress = progress or (
            lambda item: print(json.dumps(item), file=sys.stderr, flush=True)
        )
        self.last_progress = 0.0
        self.phase = "starting"
        self.rows = self.deleted_rows = 0
        self.scratch_root = scratch_directory
        self.scratch = None
        self.scratch_bytes = 0
        self.verified_backup_receipt = verified_backup_receipt

    def expired(self):
        return self.deadline is not None and time.monotonic() >= self.deadline

    def tick(self, rows=None, *, phase=None, deleted=None):
        if self.expired():
            raise TimeoutError("incomplete offline audit: time budget expired")
        if phase is not None:
            self.phase = phase
            self.rows = self.deleted_rows = 0
        if rows is not None:
            self.rows = rows
        if deleted is not None:
            self.deleted_rows = deleted
        if phase is not None or time.monotonic() - self.last_progress >= 5:
            self.progress(
                {
                    "event": "OFFLINE_CLEANUP_PROGRESS",
                    "phase": self.phase,
                    "rows": self.rows,
                    "deleted_rows": self.deleted_rows,
                }
            )
            self.last_progress = time.monotonic()

    def attach(self, connection):
        connection.execute("PRAGMA cache_size=-524288")
        connection.execute("PRAGMA temp_store=FILE")
        if connection.execute("PRAGMA temp_store").fetchone()[0] != 1:
            raise ValueError("file-backed SQLite temporary storage required")

        def progress():
            if self.expired():
                return 1
            self.tick()  # No SQL recursion: emits bounded progress during long SQL phases.
            return 0

        connection.set_progress_handler(progress, 1000)


@contextmanager
def file_scratch(source, budget):
    """Configure process-global SQLite temp location before opening any data connection."""
    source = standalone(Path(source))
    root = Path(budget.scratch_root or source.parent).resolve()
    if not root.is_dir():
        raise ValueError("existing checked scratch directory required")
    budget.scratch_bytes = source.stat().st_size
    if shutil.disk_usage(root).free < budget.scratch_bytes:
        raise ValueError("insufficient file-backed scratch capacity")
    directory = Path(tempfile.mkdtemp(prefix="guardian-cleanup-", dir=root)).resolve()
    previous_env = {key: os.environ.get(key) for key in ("TEMP", "TMP", "SQLITE_TMPDIR")}
    previous_directory, configured = "", False
    try:
        for key in previous_env:
            os.environ[key] = str(directory)
        with closing(sqlite3.connect(":memory:")) as control:
            if any(row[0] == "TEMP_STORE=3" for row in control.execute("PRAGMA compile_options")):
                raise ValueError("SQLite compiled memory-only temporary storage is unsupported")
            old = control.execute("PRAGMA temp_store_directory").fetchone()
            previous_directory = old[0] if old else ""
            control.execute(
                "PRAGMA temp_store_directory='" + str(directory).replace("'", "''") + "'"
            )
            configured = True
            effective = control.execute("PRAGMA temp_store_directory").fetchone()
            if not effective or Path(effective[0]).resolve() != directory:
                raise ValueError("SQLite explicit file-backed scratch directory unsupported")
        budget.scratch = directory
        yield
    finally:
        # All data connections close before this operation-scoped context exits.
        try:
            if configured:
                with closing(sqlite3.connect(":memory:")) as control:
                    control.execute(
                        "PRAGMA temp_store_directory='"
                        + previous_directory.replace("'", "''")
                        + "'"
                    )
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            try:
                # Preserve unexpected failure artifacts; never delete recursively.
                directory.rmdir()
            except OSError:
                pass


def _identity_digest(digest, identity):
    encoded = identity.encode("ascii")
    digest.update(len(encoded).to_bytes(8, "big") + encoded)


def receipt_witness(connection, budget):
    """One raw pass; bounded per-plan count/SHA256 and handoff ownership metadata."""
    budget.tick(phase="source_receipts")
    proofs = {}
    for count, (identity, payload) in enumerate(
        connection.execute("SELECT id,payload FROM core_guardian_receipts ORDER BY id"), 1
    ):
        budget.tick(count)
        receipt = GuardianReceipt.model_validate_json(payload)
        plans = set(receipt.protection_ids)
        if receipt.id != identity or len(plans) != len(receipt.protection_ids):
            raise ValueError("guardian receipt identity or duplicate membership invalid")
        refs = set(receipt.source_refs)
        for plan in plans:
            proof = proofs.get(plan)
            if proof is None:
                proof = proofs[plan] = {"count": 0, "digest": hashlib.sha256(), "metadata": set()}
            proof["count"] += 1
            _identity_digest(proof["digest"], identity)
            proof["metadata"].add(
                (
                    receipt.owner,
                    receipt.generation,
                    receipt.release_id,
                    receipt.evidence_lane,
                    plan in refs,
                )
            )
    for proof in proofs.values():
        proof["sha256"] = proof.pop("digest").hexdigest()
    return proofs


def projection(connection, proofs, budget, removed=()):
    """Cryptographic projection equality; PK-order scan avoids a huge reverse-key sort."""
    budget.tick(phase="source_memberships")
    removed, seen, total = set(removed), set(), 0
    query = "SELECT plan_id,receipt_id FROM core_guardian_memberships ORDER BY plan_id,receipt_id"
    for plan, rows in groupby(connection.execute(query), key=lambda row: row[0]):
        digest, count = hashlib.sha256(), 0
        for _, receipt_id in rows:
            _identity_digest(digest, receipt_id)
            count += 1
            total += 1
            budget.tick(total)
        if (
            plan not in proofs
            or plan in removed
            or count != proofs[plan]["count"]
            or digest.hexdigest() != proofs[plan]["sha256"]
        ):
            raise ValueError("incomplete or unexpected guardian membership projection")
        seen.add(plan)
    if seen != set(proofs) - removed:
        raise ValueError("incomplete guardian membership projection")
    return total


def document(session, name, identity, model):
    payload = session.execute(
        select(table(name).c.payload).where(table(name).c.id == identity)
    ).scalar_one()
    result = model.model_validate_json(payload)
    if result.id != identity:
        raise ValueError("document hash mismatch")
    return result


def eligible(session, plan_id, config, receipts, sources, outcomes, quarantined):
    repo = StorageRepository(session)
    plan = document(session, "core_protection_plans", plan_id, ProtectionPlan)
    decision = document(session, "core_decisions", plan.decision_id, Decision)
    observation = document(session, "core_observations", decision.observation_id, Observation)
    if (
        decision.config_id != content_id(config)
        or decision.action != "BUY"
        or decision.symbol != observation.symbol
        or plan != ProtectionPlan.for_decision(decision, config)
        or plan.owner != config.owner
        or plan.generation != config.generation
        or decision.release_id != config.release_id
        or decision.evidence_lane != config.evidence_lane
    ):
        return False
    links = list(
        session.execute(
            select(table("core_intent_links")).where(
                (table("core_intent_links").c.protection_id == plan_id)
                | (table("core_intent_links").c.lot_id == decision.id)
                | (table("core_intent_links").c.decision_id == decision.id)
            )
        ).mappings()
    )
    if len(links) != 1:
        return False
    link = links[0]
    intent = repo.get_order_intent(link["intent_id"])
    wrapper = EntryIntent.model_validate_json(link["payload"])
    digest = (
        content_id({"boundary": decision.selection.boundary_id, "decision": decision.id})
        if decision.selection
        else content_id({"binding": decision.config_id, "observation": observation.id})
    )
    if (
        link["role"] != "ENTRY"
        or link["protection_id"] != plan_id
        or link["lot_id"] != decision.id
        or link["decision_id"] != decision.id
        or link["observation_id"] != observation.id
        or wrapper.decision_id != decision.id
        or wrapper.protection_id != plan_id
        or intent is None
        or intent.state is not IntentState.ERROR
        or wrapper.intent.state is not IntentState.RISK_APPROVED
        or wrapper.intent != intent.model_copy(update={"state": IntentState.RISK_APPROVED})
        or intent.intent_id != "core-entry-" + digest
        or intent.client_order_id != "qb-" + digest[:40]
        or intent.strategy_id != config.owner
        or intent.side is not OrderSide.BUY
        or intent.symbol != decision.symbol
        or intent.quantity != decision.quantity
        or intent.created_at != decision.recorded_at_utc
        or intent.signal_date != decision.recorded_at_utc.date()
        or intent.order_type is not OrderType.MARKET
        or intent.time_in_force is not TimeInForce.DAY
        or intent.limit_price is not None
        or intent.stop_price is not None
        or intent.extended_hours
        or not definitively_unsent(repo, intent.intent_id)
    ):
        return False
    events = repo.list_order_events(intent_id=intent.intent_id)
    if len(events) != 1:
        return False
    event = events[0]
    if (
        event.event_type != REFUSAL
        or event.broker_order_id is not None
        or event.event_id != "core-send-refused-" + content_id({"intent": intent.intent_id})
        or event.detail.get("guard_contract") != "core-final-send-v1"
        or event.detail.get("state_at_refusal") != "ORDER_CREATED"
        or event.detail.get("definitively_unsent") is not True
    ):
        return False
    detail = event.detail
    if "source" in detail or "purpose" in detail:
        if detail.get("source") != "coordinator-pre-post-gate" or detail.get("purpose") != "ENTRY":
            return False
    else:
        allowed = {
            "guard_contract",
            "reasons",
            "definitively_unsent",
            "state_at_refusal",
            "decision_id",
            "protection_id",
            "guardian_receipt_id",
            "entry_policy_id",
            "evidence_ids",
        }
        if (
            set(detail) - allowed
            or detail.get("decision_id") != decision.id
            or detail.get("protection_id") != plan_id
            or not detail.get("guardian_receipt_id")
        ):
            return False
        handoff = document(
            session, "core_guardian_receipts", detail["guardian_receipt_id"], GuardianReceipt
        )
        if (
            plan_id not in handoff.protection_ids
            or plan_id not in handoff.source_refs
            or handoff.owner != plan.owner
            or handoff.generation != plan.generation
            or handoff.release_id != plan.release_id
            or handoff.evidence_lane != plan.evidence_lane
        ):
            return False
        if "entry_policy_id" in detail or "evidence_ids" in detail:
            evidence = detail.get("evidence_ids")
            if (
                not decision.entry_policy_id
                or detail.get("entry_policy_id") != decision.entry_policy_id
                or not isinstance(evidence, list)
                or len(evidence) != 2
                or evidence[1] != decision.entry_policy_id
                or not isinstance(evidence[0], str)
                or len(evidence[0]) != 64
                or any(c not in "0123456789abcdef" for c in evidence[0])
            ):
                return False
    if receipts != {(plan.owner, plan.generation, plan.release_id, plan.evidence_lane, True)}:
        return False
    # No execution source, exit link, outcome, or quarantine may contradict pre-POST proof.
    return (
        intent.client_order_id not in sources
        and decision.id not in outcomes
        and (None not in quarantined and decision.id not in quarantined)
    )


def _verified_source_receipt(source, receipt_path, budget):
    """Recheck all source bytes against an explicitly trusted artifact under the held lease."""
    receipt_path = Path(receipt_path).resolve(strict=True)
    with receipt_path.open("rb") as artifact:
        raw = artifact.read(65537)
    if len(raw) > 65536:
        raise ValueError("verified backup receipt exceeds bounded schema size")

    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate verified backup receipt key")
        return result

    try:
        receipt = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as error:
        raise ValueError("malformed verified backup receipt") from error
    fields = {
        "kind",
        "backup",
        "bytes",
        "sha256",
        "integrity_check",
        "foreign_key_check",
        "sqlite_version",
        "checks_complete",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ValueError("verified backup receipt schema mismatch")
    version = receipt["sqlite_version"]
    digest = receipt["sha256"]
    backup = receipt["backup"]
    if (
        receipt["kind"] != "ELROND_VERIFIED_SQLITE_BACKUP_V1"
        or receipt["checks_complete"] is not True
        or receipt["integrity_check"] != "ok"
        or receipt["foreign_key_check"] != "ok"
        or not isinstance(backup, str)
        or not Path(backup).is_absolute()
        or Path(backup).resolve() != source
        or type(receipt["bytes"]) is not int
        or receipt["bytes"] != source.stat().st_size
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or not isinstance(version, str)
        or len(version) > 32
        or len(version.split(".")) != 3
        or not all(part.isascii() and part.isdecimal() for part in version.split("."))
    ):
        raise ValueError("verified backup receipt identity or completed checks mismatch")
    budget.tick(phase="source_receipt_sha256")
    actual, total = hashlib.sha256(), 0
    with source.open("rb") as backup_file:
        while chunk := backup_file.read(8 * 1024 * 1024):
            actual.update(chunk)
            total += len(chunk)
            budget.tick(total)
    if total != receipt["bytes"] or actual.hexdigest() != digest:
        raise ValueError("verified backup receipt full-source SHA256 mismatch")
    return {
        "source_integrity_reused": True,
        "source_sha256": actual.hexdigest(),
        "verified_backup_receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "source_integrity_sqlite_version": version,
        "source_integrity_provenance": (
            "trusted local receipt; full source SHA256 rechecked under read lease"
        ),
    }


def check_source_with_derived_proof(connection, budget):
    """Fully check eligibility/history indexes; defer only the redundant projection index.

    quick_check alone is insufficient: a wrong broker lookup index can falsely
    classify a submitted order as unsent. Every other table gets its full check
    BEFORE any eligibility query. The unchanged raw receipt witness and complete
    projection check below remain mandatory, as does full candidate integrity.
    """
    budget.tick(phase="source_quick_check")
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite source quick check failed")
    budget.tick(phase="source_foreign_keys")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite source foreign key check failed")
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name<>? ORDER BY name",
            (MEMBERSHIPS,),
        )
    ]
    for name in names:
        budget.tick(phase="source_integrity_" + name)
        if connection.execute("PRAGMA integrity_check(" + quote(name) + ")").fetchall() != [
            ("ok",)
        ]:
            raise ValueError("SQLite source table integrity check failed: " + name)
    return {
        "source_check": "derived-proof",
        "source_quick_check": "ok",
        "source_foreign_key_check": "ok",
        "source_fully_checked_tables": names,
        "source_deferred_index_check": MEMBERSHIPS,
        "source_integrity_reused": False,
    }


def _audit(source, connection, budget, removed=()):
    if budget.verified_backup_receipt is None:
        if budget.source_check == "derived-proof":
            provenance = check_source_with_derived_proof(connection, budget)
        else:
            budget.tick(phase="source_integrity")
            check(connection)
            provenance = {"source_integrity_reused": False, "source_check": "full"}
    else:
        provenance = _verified_source_receipt(source, budget.verified_backup_receipt, budget)
    proofs = receipt_witness(connection, budget)
    membership_count = projection(connection, proofs, budget, removed)
    budget.tick(phase="source_eligibility")

    def session_connection():
        result = connect(source)
        budget.attach(result)
        return result

    engine = create_engine("sqlite://", creator=session_connection)
    try:
        with Session(engine) as session:
            repo = StorageRepository(session)
            if repo.list_unresolved_order_intents():
                raise ValueError("zero unresolved order intents required")
            configs = list(session.execute(select(table("core_identities"))).mappings())
            if len(configs) != 1:
                raise ValueError("exactly one canonical core identity required")
            config = CoreConfig.model_validate_json(configs[0]["payload"])
            if content_id(config) != configs[0]["id"]:
                raise ValueError("core configuration hash mismatch")
            clients, lots = set(), set()
            links, intents = table("core_intent_links"), table("order_intents")
            for plan, client, lot in session.execute(
                select(links.c.protection_id, intents.c.client_order_id, links.c.lot_id).join(
                    intents, intents.c.intent_id == links.c.intent_id
                )
            ):
                budget.tick()
                if plan in proofs:
                    clients.add(client)
                    lots.add(lot)
            sources, outcomes = set(), set()
            for name, key, targets, result in (
                ("core_broker_event_sources", "client_order_id", clients, sources),
                ("core_round_trips", "lot_id", lots, outcomes),
                ("core_outcome_revisions", "lot_id", lots, outcomes),
            ):
                for payload in session.execute(select(table(name).c.payload)).scalars():
                    budget.tick()
                    value = json.loads(payload).get(key)
                    if value is None:
                        raise ValueError("unbound contradictory source/outcome evidence")
                    if value in targets:
                        result.add(value)
            for (lot,) in connection.execute("SELECT lot_id FROM core_outcome_index"):
                budget.tick()
                if lot in lots:
                    outcomes.add(lot)
            quarantined = set()
            for (payload,) in connection.execute(
                "SELECT detail_json FROM incidents WHERE kind='CORE_EVIDENCE_CONFLICT'"
            ):
                budget.tick()
                detail = json.loads(payload)
                affected = detail.get("lot_ids")
                if detail.get("account_scope") or not isinstance(affected, list) or not affected:
                    quarantined.add(None)
                else:
                    quarantined.update(lots.intersection(affected))
            selected = []
            for plan_id, proof in sorted(proofs.items()):
                budget.tick()
                try:
                    accepted = eligible(
                        session, plan_id, config, proof["metadata"], sources, outcomes, quarantined
                    )
                except (ValueError, LookupError, NoResultFound):
                    accepted = False
                if accepted:
                    selected.append(plan_id)
            report = {
                **provenance,
                "source": str(source),
                "eligible_plan_ids": selected,
                "eligible_plans": len(selected),
                "excluded_plans": len(proofs) - len(selected),
                "eligible_memberships": sum(proofs[plan]["count"] for plan in selected),
                "memberships_before": membership_count,
                "unresolved_intents": 0,
                "projection_equality": "per-plan ordered receipt-ID count and SHA256",
                "dry_run": True,
                "complete": True,
                "installed": False,
            }
            return report, proofs
    finally:
        engine.dispose()


@contextmanager
def _audited_source(source, budget, removed=()):
    try:
        with (
            file_scratch(source, budget),
            read_lease(source) as source,
            closing(connect(source)) as connection,
        ):
            budget.attach(connection)
            report, proofs = _audit(source, connection, budget, removed)
            yield source, connection, report, proofs
    except sqlite3.OperationalError:
        budget.tick()  # Turns SQL progress-handler interruption into explicit incomplete timeout.
        raise


def preflight(
    source,
    *,
    removed=(),
    seconds=30,
    progress=None,
    scratch_directory=None,
    verified_backup_receipt=None,
):
    budget = Budget(seconds, progress, scratch_directory, verified_backup_receipt)
    with _audited_source(source, budget, removed) as (_, _, report, _):
        budget.tick(phase="complete")
        return report


def _ordered_rows(connection, name):
    columns = connection.execute("PRAGMA table_info(" + quote(name) + ")").fetchall()
    primary = sorted((column[5], column[1]) for column in columns if column[5])
    ordering = ",".join(quote(column) for _, column in primary) if primary else "rowid"
    query = "SELECT * FROM " + quote(name) + " ORDER BY " + ordering
    if any(
        "TEMP B-TREE" in row[-1].upper()
        for row in connection.execute("EXPLAIN QUERY PLAN " + query)
    ):
        raise ValueError("verification query would require an external sort: " + name)
    return connection.execute(query)


def _verify_rows(old, new, selected, proofs, budget, *, rebuilt=False):
    """Literal table-row equality; selected absence is tested against UNFILTERED candidate rows."""
    schema_sql = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    schema = old.execute(schema_sql).fetchall()
    if schema != new.execute(schema_sql).fetchall():
        raise ValueError("schema changed")
    result = {}
    for kind, name, _, _ in schema:
        if kind != "table":
            continue
        budget.tick(phase="compare_" + name)
        left_rows, right_rows = _ordered_rows(old, name), _ordered_rows(new, name)
        if name == MEMBERSHIPS:
            if rebuilt:
                # Selected rebuilds use the raw witness; all other rows compare literally.
                selected_hashes, seen, counts = {}, set(), {}

                def checked_rows(rows=right_rows, seen=seen, counts=counts, hashes=selected_hashes):
                    for row in rows:
                        budget.tick()
                        if row[0] in selected:
                            seen.add(row[0])
                            counts[row[0]] = counts.get(row[0], 0) + 1
                            _identity_digest(hashes.setdefault(row[0], hashlib.sha256()), row[1])
                        else:
                            yield row
                    if seen != selected or any(
                        counts[plan] != proofs[plan]["count"]
                        or hashes[plan].hexdigest() != proofs[plan]["sha256"]
                        for plan in selected
                    ):
                        raise ValueError("incomplete rebuilt selected membership projection")

                right_rows = checked_rows()
            else:
                left_rows = (row for row in left_rows if row[0] not in selected)
        digest, count, sentinel = hashlib.sha256(), 0, object()
        for left, right in zip_longest(left_rows, right_rows, fillvalue=sentinel):
            budget.tick(count)
            if left is sentinel or right is sentinel or repr(left) != repr(right):
                raise ValueError("unauthorized table difference or selected membership: " + name)
            encoded = repr(left).encode()
            digest.update(len(encoded).to_bytes(8, "big") + encoded)
            count += 1
        result[name] = {"unchanged_rows": count, "sha256": digest.hexdigest()}
    return result


def verify_cleanup(
    source,
    candidate,
    selected,
    *,
    rebuilt=False,
    seconds=None,
    progress=None,
    scratch_directory=None,
    verified_backup_receipt=None,
):
    selected, budget = (
        set(selected),
        Budget(seconds, progress, scratch_directory, verified_backup_receipt),
    )
    with _audited_source(source, budget, selected if rebuilt else ()) as (_, old, report, proofs):
        if not selected <= set(report["eligible_plan_ids"]):
            raise ValueError("selected plans are not eligible")
        with read_lease(candidate) as candidate, closing(connect(candidate)) as new:
            budget.attach(new)
            budget.tick(phase="candidate_integrity")
            check(new)
            return _verify_rows(old, new, selected, proofs, budget, rebuilt=rebuilt)


def delete_selected(connection, selected, *, window_size=4096, progress=None):
    if not 1 <= window_size <= 4096:
        raise ValueError("bounded rowid window required")
    selected, last = set(selected), None
    scanned = deleted = 0
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        while True:
            if last is None:
                rows = connection.execute(
                    "SELECT rowid,plan_id FROM core_guardian_memberships ORDER BY rowid LIMIT ?",
                    (window_size,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT rowid,plan_id FROM core_guardian_memberships "
                    "WHERE rowid>? ORDER BY rowid LIMIT ?",
                    (last, window_size),
                ).fetchall()
            if not rows:
                break
            last = rows[-1][0]  # Advance even across windows containing no selected rows.
            scanned += len(rows)
            matching = [rowid for rowid, plan in rows if plan in selected]
            for offset in range(0, len(matching), 500):
                chunk = matching[offset : offset + 500]
                deleted += connection.execute(
                    "DELETE FROM core_guardian_memberships WHERE rowid IN ("
                    + ",".join("?" for _ in chunk)
                    + ")",
                    chunk,
                ).rowcount
            if progress is not None:
                progress(scanned, deleted)


def volume_key(directory):
    """Identify the underlying Windows volume, including mounted-folder aliases."""
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise ValueError("existing capacity-check directory required")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    mount, identity = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(1024)
    mount_path = kernel.GetVolumePathNameW
    mount_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    volume_name = kernel.GetVolumeNameForVolumeMountPointW
    volume_name.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    if not mount_path(str(directory), mount, len(mount)) or not volume_name(
        mount.value, identity, len(identity)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return identity.value.lower()


def capacity_requirements(
    source_bytes,
    candidate_directory,
    scratch_directory,
    *,
    vacuum=False,
    rebuild_extra=0,
    phase="initial",
    candidate_bytes=None,
):
    """Peak *additional* bytes per volume for sequential audit/copy/mutation/VACUUM phases.

    SQLite VACUUM section 3 requires up to two database sizes free: one transient
    database and one rollback journal. The already-copied candidate is counted only
    in the initial plan; the DELETE journal is gone before VACUUM begins.
    https://www.sqlite.org/lang_vacuum.html#how_vacuum_works
    """
    candidate_directory, scratch_directory = Path(candidate_directory), Path(scratch_directory)
    candidate_volume, scratch_volume = (
        volume_key(candidate_directory),
        volume_key(scratch_directory),
    )
    upper = max(source_bytes + rebuild_extra, candidate_bytes or 0)
    headroom = max(64 * 1024 * 1024, (upper + 19) // 20)
    window_temp = min(8 * 1024 * 1024, source_bytes)
    if phase == "initial":
        allocations = [
            (0, source_bytes),
            (source_bytes, 0),
            (2 * source_bytes + rebuild_extra, window_temp),
            (upper, window_temp),
        ]
        if vacuum:
            allocations.append((2 * upper, upper))
    elif phase == "mutation":
        allocations = [(source_bytes + rebuild_extra, window_temp)]
    elif phase == "vacuum":
        allocations = [(candidate_bytes, candidate_bytes)]
    else:
        raise ValueError("unknown capacity phase")
    volumes = {}
    for candidate_need, scratch_need in allocations:
        current = {}
        for key, directory, needed in (
            (candidate_volume, candidate_directory, candidate_need),
            (scratch_volume, scratch_directory, scratch_need),
        ):
            current[key] = current.get(key, 0) + needed
            volumes.setdefault(key, {"path": str(directory), "required_bytes": 0})
        for key, needed in current.items():
            volumes[key]["required_bytes"] = max(volumes[key]["required_bytes"], needed + headroom)
    return {"phase": phase, "headroom_bytes": headroom, "volumes": volumes}


def require_capacity(plan):
    for volume in plan["volumes"].values():
        if shutil.disk_usage(volume["path"]).free < volume["required_bytes"]:
            raise ValueError("insufficient capacity before " + plan["phase"] + " phase")


def candidate_digest(candidate, budget):
    """Called after verification while the SAME candidate deny-write/delete lease is held."""
    budget.tick(phase="candidate_sha256")
    digest, count = hashlib.sha256(), 0
    with candidate.open("rb") as reader:
        while chunk := reader.read(8 * 1024 * 1024):
            digest.update(chunk)
            count += len(chunk)
            budget.tick(count)
    return {"candidate_sha256": digest.hexdigest(), "candidate_bytes": count}


def _candidate_copy(
    source,
    destination,
    selected=None,
    *,
    rebuild=False,
    vacuum=False,
    seconds=None,
    progress=None,
    scratch_directory=None,
    verified_backup_receipt=None,
    source_check="full",
):
    destination = Path(destination).absolute()
    partial = destination.with_name(destination.name + ".unverified")
    if destination.exists() or partial.exists():
        raise ValueError("candidate must be a new path")
    source = standalone(Path(source))
    scratch_root = Path(scratch_directory or source.parent).resolve()
    # Refuse an impossible baseline before hashing/scanning a large backup. Rebuild
    # expansion is added and checked once its raw receipt witness is available.
    require_capacity(
        capacity_requirements(
            source.stat().st_size, destination.parent, scratch_root, vacuum=vacuum
        )
    )
    selected = set(selected) if selected is not None else None
    budget = Budget(seconds, progress, scratch_directory, verified_backup_receipt, source_check)
    with _audited_source(source, budget, (selected or ()) if rebuild else ()) as (
        source,
        old,
        report,
        proofs,
    ):
        if selected is None:
            selected = set(report["eligible_plan_ids"])
        if not selected <= set(report["eligible_plan_ids"]):
            raise ValueError("selected plans are not eligible for rebuild")
        page_size = old.execute("PRAGMA page_size").fetchone()[0]
        rebuilt_rows = sum(proofs[plan]["count"] for plan in selected) if rebuild else 0
        # Separate conservative expansion allowance: four SQLite pages per restored row,
        # in addition to copy+journal. Never infer rebuild capacity from compacted bytes alone.
        source_bytes, expansion = source.stat().st_size, rebuilt_rows * 4 * page_size
        capacity = capacity_requirements(
            source_bytes, destination.parent, budget.scratch, vacuum=vacuum, rebuild_extra=expansion
        )
        require_capacity(capacity)
        budget.tick(phase="copy_source")
        with partial.open("xb") as output, source.open("rb") as original:
            while chunk := original.read(1024 * 1024):
                output.write(chunk)
                budget.tick()
        require_capacity(
            capacity_requirements(
                source_bytes,
                destination.parent,
                budget.scratch,
                phase="mutation",
                rebuild_extra=expansion,
            )
        )
        with closing(sqlite3.connect(partial)) as connection:
            budget.attach(connection)
            connection.execute("PRAGMA journal_mode=DELETE")
            budget.tick(phase="rebuild_memberships" if rebuild else "delete_memberships")
            if rebuild:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    for identity, payload in connection.execute(
                        "SELECT id,payload FROM core_guardian_receipts"
                    ):
                        budget.tick()
                        plans = json.loads(payload)["protection_ids"]
                        connection.executemany(
                            "INSERT INTO core_guardian_memberships VALUES(?,?)",
                            [(plan, identity) for plan in plans if plan in selected],
                        )
            else:
                delete_selected(
                    connection,
                    selected,
                    progress=lambda scanned, deleted: budget.tick(scanned, deleted=deleted),
                )
            if vacuum:
                if Path(str(partial) + "-journal").exists():
                    raise ValueError("mutation rollback journal still exists before VACUUM")
                require_capacity(
                    capacity_requirements(
                        source_bytes,
                        destination.parent,
                        budget.scratch,
                        phase="vacuum",
                        candidate_bytes=partial.stat().st_size,
                        rebuild_extra=expansion,
                    )
                )
                budget.tick(phase="vacuum_candidate")
                connection.execute("VACUUM")
        with read_lease(partial), closing(connect(partial)) as new:
            budget.attach(new)
            budget.tick(phase="candidate_integrity")
            check(new)
            tables = _verify_rows(old, new, selected, proofs, budget, rebuilt=rebuild)
            digest = candidate_digest(partial, budget)
        if destination.exists():
            raise ValueError("destination already exists")
        partial.rename(destination)
        budget.tick(phase="complete")
        return {
            **report,
            **digest,
            "candidate": str(destination),
            "selected_plan_ids": sorted(selected),
            "removed_memberships": 0 if rebuild else sum(proofs[p]["count"] for p in selected),
            "rebuild_memberships": rebuilt_rows,
            "capacity_plan": capacity,
            "required_scratch_bytes": sum(
                volume["required_bytes"] for volume in capacity["volumes"].values()
            ),
            "rebuilt": rebuild,
            "dry_run": False,
            "verified": True,
            "tables": tables,
            "table_equality": "literal ordered rows except complete selected membership delta",
            "rollback": "retain unpruned backup or rebuild selected before old code",
            "installed": False,
        }


def cleanup_copy(
    source,
    destination,
    *,
    vacuum=False,
    seconds=None,
    progress=None,
    scratch_directory=None,
    verified_backup_receipt=None,
    source_check="full",
):
    return _candidate_copy(
        source,
        destination,
        vacuum=vacuum,
        seconds=seconds,
        progress=progress,
        scratch_directory=scratch_directory,
        verified_backup_receipt=verified_backup_receipt,
        source_check=source_check,
    )


def rebuild_selected(
    source,
    destination,
    selected,
    *,
    seconds=None,
    progress=None,
    scratch_directory=None,
    verified_backup_receipt=None,
):
    return _candidate_copy(
        source,
        destination,
        selected,
        rebuild=True,
        seconds=seconds,
        progress=progress,
        scratch_directory=scratch_directory,
        verified_backup_receipt=verified_backup_receipt,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-copy", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--dry-run-seconds", type=float, default=30)
    parser.add_argument(
        "--scratch-directory",
        type=Path,
        help="existing scratch root; default is the closed backup's volume/directory",
    )
    parser.add_argument("--rebuild-selected", nargs="+", metavar="PLAN_ID")
    parser.add_argument(
        "--verified-backup-receipt",
        type=Path,
        help="explicit trusted local V1 validator receipt; source hash is always rechecked",
    )
    args = parser.parse_args()
    if args.rebuild_selected and not args.candidate:
        parser.error("rebuild requires a new --candidate")
    if args.rebuild_selected:
        result = rebuild_selected(
            args.offline_copy,
            args.candidate,
            args.rebuild_selected,
            scratch_directory=args.scratch_directory,
            verified_backup_receipt=args.verified_backup_receipt,
        )
    elif args.candidate:
        result = cleanup_copy(
            args.offline_copy,
            args.candidate,
            vacuum=args.vacuum,
            scratch_directory=args.scratch_directory,
            verified_backup_receipt=args.verified_backup_receipt,
        )
    else:
        result = preflight(
            args.offline_copy,
            seconds=args.dry_run_seconds,
            scratch_directory=args.scratch_directory,
            verified_backup_receipt=args.verified_backup_receipt,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
