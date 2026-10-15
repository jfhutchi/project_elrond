"""Read-only legacy evidence export. No broker client, settings, migration or runtime.

Run ``python -m quantbot.paper_core.inventory --config manifest.json --as-of ISO``.
JSON goes to stdout; the caller chooses a NEW output file. Separate database read
transactions are not an atomic cross-store snapshot. Recorded claims grant no authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantbot.operations.store_inventory import digest

Row = dict[str, Any]
FILL_FIELDS = ("fill_id", "broker_order_id", "symbol", "side", "quantity", "price",
               "occurred_at", "fee")
EVENT_FIELDS = ("experiment_id", "lot_id", "decision_id", "observation_id", "policy_id",
                "recorded_at", "occurred_at", "kind", "order_id", "quantity", "source_hash",
                "synthetic", "position_before_entry", "side", "owner", "fill_id", "price",
                "fee", "terminal", "reconciled", "intent_id", "request_hash", "predictor_fit",
                "source_state_hash", "model_id", "activation_id", "code_hash", "account_id_hash")
EVENT_KINDS = ("LotEvent", "ExplorationObservation", "ExplorationFeedback", "ExecutionReceipt",
               "ExplorationPolicy", "ExplorationCycleReceipt")
REASONS = {"MARKET_CLOSED", "NO_FRESH_CURRENT_COHORT", "KILL_SWITCH_ENGAGED",
           "ACQUISITION_UNSTABLE", "SNAPSHOT_ACQUISITION_TIMEOUT", "BROKER_UNAVAILABLE",
           "CLOCK_UNAVAILABLE", "CLOCK_SKEW", "NO_ELIGIBLE_CANDIDATE"}


@dataclass(frozen=True)
class CoreConfig:
    operational_db: Path
    learner_db: Path
    experiment_db: Path
    account_hash: str
    deployment_json: Path | None = None
    runtime_metadata: Path | None = None
    entry_receipt: Path | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", self.account_hash):
            raise ValueError("account_hash must be SHA256, never an account number")
        for path in (self.operational_db, self.learner_db, self.experiment_db,
                     self.deployment_json, self.runtime_metadata, self.entry_receipt):
            if path is not None and not path.is_absolute():
                raise ValueError("explicit absolute paths required")


@dataclass(frozen=True)
class InventorySnapshot:
    document: str

    def to_dict(self) -> Row:
        return dict(json.loads(self.document))


@dataclass(frozen=True)
class EvidenceBundle:
    document: str

    def to_dict(self) -> Row:
        return dict(json.loads(self.document))


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False)


def _time(value: str | datetime) -> datetime:
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timezone required")
    return moment.astimezone(UTC)


def _project(row: Row, fields: tuple[str, ...]) -> Row:
    # No arbitrary nested JSON, provider messages, account numbers or environment values.
    return {key: row[key] for key in fields if key in row
            and (row[key] is None or isinstance(row[key], str | int | float | bool))}


def _rows(db: sqlite3.Connection, table: str, fields: tuple[str, ...],
          gaps: list[str], *, stamp: str | None = None, cutoff: datetime) -> list[Row]:
    columns = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
    if not set(fields) <= columns:
        gaps.append(f"SCHEMA_COLUMNS_MISSING:{table}")
        return []
    # Identifiers come only from module constants; bounded iteration, no implicit truncation.
    names = ','.join(f'"{name}"' for name in fields)
    records = []
    for index, row in enumerate(db.execute(f'SELECT {names} FROM "{table}"')):
        if index >= 100_000:
            raise ValueError("ROW_BUDGET_EXCEEDED")
        item = dict(row)
        if stamp:
            try:
                if item[stamp] is None or _time(item[stamp]) > cutoff:
                    continue
            except (ValueError, TypeError):
                gaps.append(f"INVALID_TIMESTAMP:{table}")
                continue
        records.append(item)
    return sorted(records, key=_json)


def _events(db: sqlite3.Connection, cutoff: datetime, gaps: list[str],
            account_hash: str) -> list[Row]:
    result = []
    for row in _rows(db, "events", ("kind", "recorded_at", "document", "digest"), gaps,
                     stamp="recorded_at", cutoff=cutoff):
        if row["kind"] not in EVENT_KINDS:
            continue
        raw = row["document"]
        if hashlib.sha256(raw.encode()).hexdigest() != row["digest"]:
            gaps.append("EVENT_DIGEST_MISMATCH")
            continue
        try:
            doc = json.loads(raw)
            if _time(doc["recorded_at"]) != _time(row["recorded_at"]):
                raise ValueError("timestamp mismatch")
            if "occurred_at" in doc and _time(doc["occurred_at"]) > _time(doc["recorded_at"]):
                raise ValueError("future occurrence")
            account = doc.get("account_id_hash") if row["kind"] == "LotEvent" else \
                doc.get("account_id_hash", account_hash)
            if account != account_hash:
                gaps.append("EVENT_ACCOUNT_MISMATCH")
                continue
            item = _project(doc, EVENT_FIELDS)
            item.update(event_type=row["kind"], document_sha256=row["digest"])
            if row["kind"] == "ExplorationObservation":
                # The existing model's canonical ID normalizes datetimes and Decimals.
                from quantbot.learner.exploration import ExplorationObservation
                observation = ExplorationObservation.model_validate(doc)
                item.update(observation_id=observation.prediction_id,
                            symbol=observation.features.symbol,
                            source_snapshot_hash=observation.features.source_snapshot_hash,
                            feature_at=observation.features.feature_at.isoformat(),
                            decision_at=observation.features.decision_at.isoformat())
            elif row["kind"] == "ExplorationCycleReceipt":
                item["result"] = _project(doc.get("result", {}),
                    ("status", "orders_submitted", "started_at", "finished_at"))
                item["result"]["reasons"] = [reason if reason in REASONS else
                    "UNRECOGNIZED_REASON" for reason in doc.get("result", {}).get("reasons", [])
                    if isinstance(reason, str)]
            elif row["kind"] == "ExecutionReceipt":
                item["broker_order"] = _project(doc.get("result", {}).get("broker_order", {}),
                    ("broker_order_id", "client_order_id", "status", "submitted_at"))
            elif row["kind"] == "ExplorationPolicy":
                from quantbot.learner.exploration import ExplorationPolicy
                item["policy_id"] = ExplorationPolicy.model_validate(doc).policy_id
            result.append(item)
        except (ValueError, TypeError, KeyError, AttributeError):
            gaps.append(f"INVALID_EVENT:{row['kind']}")
    return sorted(result, key=_json)


def _store(path: Path, role: str, cutoff: datetime, account_hash: str) -> Row:
    result: Row = {"path": str(path), "role": role, "cutoff": cutoff.isoformat(),
                   "authority": "CONFIGURED_OPERATIONS" if role == "operations" else
                   "HISTORICAL_COPY" if role == "learner" else "EXPERIMENT_JOURNAL",
                   "gaps": [], "fills": [], "events": []}
    gaps = result["gaps"]
    if not path.is_file():
        gaps.append(f"STORE_MISSING:{role}")
        return result
    stat = path.stat()
    result["file_identity"] = f"{stat.st_dev}:{stat.st_ino}"
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            ticks = 0

            def stop() -> int:
                nonlocal ticks
                ticks += 1
                return int(ticks > 100_000)

            db.set_progress_handler(stop, 1000)
            if role == "experiment":
                result["events"] = _events(db, cutoff, gaps, account_hash)
            else:
                result["identity"] = _rows(db, "schema_version", ("version", "store_identity"),
                                           gaps, cutoff=cutoff)
                result["fills"] = _rows(db, "fills", FILL_FIELDS, gaps,
                                        stamp="occurred_at", cutoff=cutoff)
                if role == "operations":
                    for key, table, fields, stamp in (
                        ("intents", "order_intents", ("intent_id", "client_order_id", "symbol",
                            "side", "created_at"), "created_at"),
                        ("runtime", "runtime_deployments", ("role", "git_commit", "observed_at",
                            "process_started_at"), "observed_at"),
                        ("stop", "kill_switch_state", ("engaged", "updated_at"), "updated_at"),
                        ("reconciliation", "reconciliation_runs", ("reconciliation_id", "status",
                            "completed_at"), "completed_at"),
                    ):
                        result[key] = _rows(db, table, fields, gaps, stamp=stamp, cutoff=cutoff)
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError):
        # Discard any partial query result; no claims from an interrupted snapshot.
        return {"path": str(path), "role": role, "cutoff": cutoff.isoformat(),
                "gaps": [f"STORE_UNREADABLE_OR_LIMIT:{role}"], "fills": [], "events": []}
    result["selected_evidence_sha256"] = digest(result)
    return result


def _artifact(path: Path | None, gaps: list[str]) -> Row:
    if path is None:
        return {}
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError("too large")
        return dict(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, TypeError):
        gaps.append("ARTIFACT_MISSING_OR_INVALID:" + path.name)
        return {}


def _entry_state(path: Path | None, cutoff: datetime, gaps: list[str]) -> Row:
    raw = _artifact(path, gaps)
    if not raw:
        return {}
    try:
        at = _time(raw["recorded_at"])
        if at > cutoff:
            raise ValueError("future receipt")
        result = _project(raw, ("recorded_at", "code_commit", "config_hash", "tick_id"))
        result["status"] = raw.get("status") if raw.get("status") in {
            "NORMAL_WAIT", "WAITING", "HALTED", "BLOCKED", "SUCCEEDED", "FAILED"} else "UNKNOWN"
        result["reason"] = raw.get("reason") if raw.get("reason") in REASONS else \
            "UNRECOGNIZED_REASON"
        result.update(age_seconds=(cutoff-at).total_seconds(), source_path=str(path),
                      document_sha256=digest(raw), scope="RECORDED_ENTRY_STATE")
        return result
    except (ValueError, TypeError, KeyError):
        gaps.append("ENTRY_RECEIPT_INVALID_OR_OUTSIDE_CUTOFF")
        return {}


def inspect_inventory(config: CoreConfig, *, as_of: datetime) -> InventorySnapshot:
    cutoff = _time(as_of)
    stores = [_store(path, role, cutoff, config.account_hash) for path, role in (
        (config.operational_db, "operations"), (config.learner_db, "learner"),
        (config.experiment_db, "experiment"))]
    operations, learner, experiment = stores
    gaps = [gap for store in stores for gap in store["gaps"]]
    authoritative = {fill["fill_id"]: fill for fill in operations["fills"]}
    duplicate = conflict = copy_only = 0
    for fill in learner["fills"]:
        original = authoritative.get(fill["fill_id"])
        if original is None:
            copy_only += 1
        elif original == fill:
            duplicate += 1
        else:
            conflict += 1
            gaps.append("COPIED_FILL_CONFLICT:" + fill["fill_id"])
    runtime_by_role = {}
    for row in sorted(operations.get("runtime", []), key=lambda r: _time(r["observed_at"])):
        runtime_by_role[row["role"]] = row
    deployment = _artifact(config.deployment_json, gaps)
    declared = {key.removesuffix("_commit"): value for key, value in deployment.items()
                if key.endswith("_commit") and isinstance(value, str)
                and re.fullmatch(r"[a-f0-9]{40}", value)}
    for role, row in runtime_by_role.items():
        if role in declared and declared[role] != row["git_commit"]:
            gaps.append("DEPLOYMENT_RELEASE_CONFLICT:" + role)
    metadata = _artifact(config.runtime_metadata, gaps)
    if metadata:
        try:
            if _time(metadata["observed_at"]) > cutoff:
                gaps.append("HOST_METADATA_OUTSIDE_CUTOFF")
                metadata = {}
        except (KeyError, ValueError, TypeError):
            gaps.append("HOST_METADATA_TIMESTAMP_INVALID")
            metadata = {}
    host: Row = {"observed_at": metadata.get("observed_at"), "tasks": [], "processes": []}
    for kind, fields in (("tasks", ("name", "state", "release", "release_path")),
                         ("processes", ("pid", "created_at", "executable", "release_path"))):
        collection = metadata.get(kind, [])
        if not isinstance(collection, list) or not all(isinstance(r, dict) for r in collection):
            gaps.append("HOST_METADATA_COLLECTION_INVALID:" + kind)
            continue
        host[kind] = [_project(row, fields) for row in collection]
    if host["observed_at"] is None:
        gaps.append("HOST_METADATA_NOT_CAPTURED")
    for task in host["tasks"]:
        role = {"Elrond Console": "console", "Elrond Learner": "learner",
                "Elrond Paper Deadline": "deadline", "Elrond Protection": "trading"}.get(
                    task.get("name"))
        if role in declared and task.get("release") and task["release"] != declared[role]:
            gaps.append("TASK_DEPLOYMENT_CONFLICT:" + str(role))
    stops = sorted(operations.get("stop", []), key=lambda r: _time(r["updated_at"]))
    entry_state = _entry_state(config.entry_receipt, cutoff, gaps)
    first_blocker = "RECORDED_STOP_ENGAGED" if stops and stops[-1]["engaged"] else \
        entry_state.get("reason", "CURRENT_BROKER_READINESS_NOT_PROBED")
    if operations["gaps"]:
        first_blocker = operations["gaps"][0]
    # Store content remains separately attributed; no copy becomes a second operational ledger.
    result = {"version": 1, "as_of": cutoff.isoformat(), "account_hash": config.account_hash,
              "lane": "LEGACY_RECORDED_EVIDENCE", "broker_freshness": "NOT_PROBED",
              "cross_store_atomic": False, "grants_execution": False,
              "first_blocker": first_blocker, "runtime": list(runtime_by_role.values()),
              "entry_state": entry_state,
              "deployment_claims": declared, "host_metadata": host, "stores": stores,
              "fill_summary": {"authoritative": len(authoritative), "copied_duplicates": duplicate,
                               "copy_only": copy_only, "conflicts": conflict},
              "gaps": sorted(set(gaps))}
    return InventorySnapshot(_json(result))


def trace_legacy_lot(snapshot: InventorySnapshot, lot_id: str) -> EvidenceBundle:
    doc = snapshot.to_dict()
    operations, _, experiment = doc["stores"]
    events = experiment["events"]
    lot = [r for r in events if r.get("lot_id") == lot_id and r["event_type"] == "LotEvent"]
    feedback = [r for r in events if r.get("lot_id") == lot_id
                and r["event_type"] == "ExplorationFeedback"]
    decision_ids = {r.get("decision_id") for r in lot}
    observations = [r for r in events if r["event_type"] == "ExplorationObservation"
                    and r.get("observation_id") in decision_ids]
    policy_ids = {r.get("policy_id") for r in observations}
    policies = [r for r in events if r["event_type"] == "ExplorationPolicy"
                and r.get("policy_id") in policy_ids]
    intent_ids = {r["order_id"] for r in lot if r["kind"] == "ENTRY_DECLARED"}
    intents = [r for r in operations.get("intents", []) if r["intent_id"] in intent_ids]
    fill_ids = {r.get("fill_id") for r in lot if r["kind"] == "FILL"}
    fills = [r for r in operations["fills"] if r["fill_id"] in fill_ids]
    receipts = [r for r in events if r["event_type"] == "ExecutionReceipt"
                and r.get("intent_id") in intent_ids]
    stages = {"observation": bool(observations), "intent": bool(intents),
              "entry_fill": any(r["side"] == "BUY" for r in fills),
              "protection": any(r["kind"] == "PROTECTION_BOUND" for r in lot),
              "exit_fill": any(r["side"] == "SELL" for r in fills),
              "flat": any(r["kind"] == "RECONCILED_FLAT" for r in lot),
              "feedback": bool(feedback)}
    gaps = ["COST_PROVENANCE_UNKNOWN", "NO_LINKED_PARAMETER_UPDATE"]
    gaps.extend(key.upper() + "_MISSING" for key, present in stages.items() if not present)
    if not lot:
        gaps.append("LOT_NOT_FOUND")
    if fill_ids - {r["fill_id"] for r in fills}:
        gaps.append("OPERATIONAL_FILL_MISSING")
    gross = None
    if stages["entry_fill"] and stages["exit_fill"]:
        buy = sum(Decimal(r["quantity"]) for r in fills if r["side"] == "BUY")
        sell = sum(Decimal(r["quantity"]) for r in fills if r["side"] == "SELL")
        if buy == sell:
            gross = str(sum(Decimal(r["quantity"]) * Decimal(r["price"]) *
                            (1 if r["side"] == "SELL" else -1) for r in fills))
        else:
            gaps.append("QUANTITY_NOT_FLAT")
    synthetic_values = {r.get("synthetic") for r in lot}
    lane = "SYNTHETIC" if synthetic_values == {True} else \
        "BROKER_PAPER_RECORDED" if synthetic_values == {False} else "UNKNOWN"
    return EvidenceBundle(_json({"lot_id": lot_id, "as_of": doc["as_of"], "lane": lane,
        "account_hash": doc["account_hash"],
        "observed_lot_account_hashes": sorted({r["account_id_hash"] for r in lot}),
        "account_scope": "configured account matched to journal; broker account not probed",
        "scope": "historical recorded links; not a new reconciliation or certification",
        "sources": [{k: s.get(k) for k in ("role", "path", "identity", "cutoff",
                                          "selected_evidence_sha256")} for s in doc["stores"]],
        "stages": stages, "observations": observations, "intents": intents,
        "source_policies": policies,
        "execution_receipts": receipts, "lot_events": lot, "fills": fills, "feedback": feedback,
        "gross_pnl": gross, "verified_net_pnl": None, "cost_status": "COSTS_PENDING",
        "gaps": sorted(set(gaps)), "inventory_gaps": doc["gaps"]}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--lot", action="append", default=[])
    args = parser.parse_args()
    raw = json.loads(args.config.read_text(encoding="utf-8-sig"))
    for key in ("operational_db", "learner_db", "experiment_db", "deployment_json",
                "runtime_metadata", "entry_receipt"):
        if raw.get(key) is not None:
            raw[key] = Path(raw[key])
    snapshot = inspect_inventory(CoreConfig(**raw), as_of=_time(args.as_of))
    print(_json({"inventory": snapshot.to_dict(),
                 "lots": [trace_legacy_lot(snapshot, lot).to_dict() for lot in args.lot]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
