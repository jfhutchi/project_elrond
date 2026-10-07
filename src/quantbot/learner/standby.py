"""Read-only, explicit original-entry standby; never grants order authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from quantbot.console.market_status import MarketStatus
from quantbot.learner.consent import consent_recorded
from quantbot.learner.models import LearnerModel
from quantbot.learner.paper import PaperModelBinding
from quantbot.research.manifest import content_id
from quantbot.storage import SingleWriterLock


class StandbyVerificationError(ValueError):
    """A fixed public code; never provider errors or user-supplied text."""

    def __init__(self, code: str) -> None:
        allowed = {
            "STANDBY_CONFIGURATION_MISMATCH",
            "STANDBY_BINDING_MISMATCH",
            "STANDBY_CONSENT_UNVERIFIED",
            "STANDBY_ENTRY_HEARTBEAT_STALE",
            "STANDBY_VERIFICATION_FAILED",
        }
        self.code = code if code in allowed else "STANDBY_VERIFICATION_FAILED"
        super().__init__(self.code)


class EntryStandby(LearnerModel):
    schema_version: Literal["ELROND_ENTRY_STANDBY_V1"] = Field(alias="schema")
    original_strategy_id: str = Field(pattern=r"^learner-[a-f0-9]{64}$")
    active_strategy_id: str = Field(pattern=r"^learner-[a-f0-9]{64}$")
    active_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    paper_configuration_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    active_root: Path
    operational_database: Path
    active_contract: Literal["LEGACY_DIA", "PAPER_EXPLORATION"] = "LEGACY_DIA"
    recorded_at: datetime
    authority: Literal["EXPLICIT_OPERATOR_PAPER_PILOT_SWITCH"]


def _read(path: Path, maximum: int = 65536) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("standby input absent or oversized")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("standby input must be a document")
    return value


def _validated_selection(
    root: Path,
    record: EntryStandby,
    *,
    now: datetime,
) -> tuple[dict[str, Any], datetime]:
    """Verify immutable selection; this alone makes no heartbeat/entry-health claim."""
    if (
        not record.active_root.is_absolute()
        or not record.operational_database.is_absolute()
        or record.active_root.resolve() == root.resolve()
        or record.recorded_at > now
    ):
        raise StandbyVerificationError("STANDBY_VERIFICATION_FAILED")
    if record.active_contract == "PAPER_EXPLORATION":
        config, target = _exploration_selection(record, now=now)
    else:
        config = _read(record.active_root / "paper-execution.json")
        target = _read(record.active_root / "worker.json")
    if (record.active_contract == "LEGACY_DIA"
            and content_id(config) != record.paper_configuration_hash):
        raise StandbyVerificationError("STANDBY_CONFIGURATION_MISMATCH")
    policy = config.get("policy", {})
    if (
        config.get("mode") != "PAPER"
        or (record.active_contract == "LEGACY_DIA" and config.get("experimental_symbol") != "DIA")
        or config.get("operational_database") != str(record.operational_database)
        or not 0 < config.get("risk_per_trade_bps", 10000) <= 25
        or not 0 < config.get("max_position_value_bps", 10000) <= 500
        or policy.get("exclusive_learner_entry") is not True
        or policy.get("stop_loss_bps") != 100
        or not policy.get("inherited")
    ):
        raise StandbyVerificationError("STANDBY_CONFIGURATION_MISMATCH")
    if (record.active_contract == "LEGACY_DIA"
            and not consent_recorded(record.active_root, now=now)):
        raise StandbyVerificationError("STANDBY_CONSENT_UNVERIFIED")
    finished = datetime.fromisoformat(target["finished_at"])
    paper = target.get("paper", {})
    binding = PaperModelBinding.model_validate(paper["binding"])
    identity = binding.strategy_identity(
        git_commit=record.active_commit,
        deployed_at=datetime.fromisoformat(
            paper["identity"]["deployment_timestamp"].replace("Z", "+00:00")
        ),
    )
    if (
        identity.strategy_id != record.active_strategy_id
        or identity.model_dump(mode="json") != paper["identity"]
        or binding.model_id != config.get("model_id")
        or (
            config.get("expected_binding_hash") is not None
            and content_id(binding) != config["expected_binding_hash"]
        )
        or target.get("code_commit") != record.active_commit
    ):
        raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
    uri = record.operational_database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT strategy_id,stage,strategy_version,configuration_hash "
                "FROM promotion_state WHERE strategy_id IN (?,?)",
                (record.original_strategy_id, record.active_strategy_id),
            )
        }
        deployed = connection.execute(
            "SELECT git_commit,configuration_hash,deployment_timestamp FROM "
            "strategy_deployments WHERE strategy_id=? AND version=?",
            (identity.strategy_id, identity.version),
        ).fetchone()
    if (
        rows.get(record.original_strategy_id, (None,))[0] != "SHADOW"
        or rows.get(record.active_strategy_id)
        != ("PAPER_OBSERVATION", identity.version, identity.configuration_hash)
        or deployed is None
        or deployed[:2] != (record.active_commit, identity.configuration_hash)
        or datetime.fromisoformat(deployed[2].replace("Z", "+00:00"))
        != identity.deployment_timestamp
    ):
        raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
    return dict(
        status="STANDBY",
        trading_enabled=False,
        promotion="SHADOW",
        reason="Explicit pilot switch; original forecasts and outcomes remain active.",
        active_strategy_id=record.active_strategy_id,
        active_root=str(record.active_root),
        active_pilot_status=paper.get("status"),
        active_worker_status=target.get("status"),
        blockers=list(paper.get("blockers", [])),
        cleared_gate_codes=[
            "PAPER_PROMOTION_REQUIRED",
            "PAPER_EXECUTION_NOT_CONFIGURED",
            "PAPER_OBSERVATION_NOT_EARNED",
        ],
        orders_submitted=0,
    ), finished


def _exploration_selection(
    record: EntryStandby, *, now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read actual coordinator completion and separate explicit policy commissioning.

    Static selection metadata makes no heartbeat or profitable-model claim. A matching
    journaled TickReceipt, binding, operator commission and registry remain mandatory.
    """
    from quantbot.learner.experiment_coordinator import ExplorationCycleReceipt, TickReceipt
    from quantbot.learner.experiment_launch import load_manifest
    from quantbot.learner.experiment_store import ExperimentStore
    from quantbot.learner.exploration_execution import ExplorationCommission
    from quantbot.learner.exploration_launch import (
        ExplorationLaunchConfig,
        configured_source,
        read_policy,
    )

    proof = _read(record.active_root / "exploration-selection.json")
    if (proof.get("schema") != "PAPER_EXPLORATION_SELECTION_V1"
            or content_id(proof) != record.paper_configuration_hash):
        raise StandbyVerificationError("STANDBY_CONFIGURATION_MISMATCH")
    path = Path(proof["launch_config"])
    launch_document = _read(path)
    if (not path.is_absolute() or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != proof["launch_hash"]):
        raise StandbyVerificationError("STANDBY_CONFIGURATION_MISMATCH")
    launch = ExplorationLaunchConfig.model_validate(launch_document)
    source, policy = configured_source(launch), read_policy(launch)
    manifest = load_manifest(source)
    binding = PaperModelBinding.model_validate(proof["binding"])
    if (source.root.resolve() != record.active_root.resolve()
            or source.operations_database.resolve() != record.operational_database.resolve()
            or source.code_commit != record.active_commit
            or policy.manifest_hash != content_id(manifest)
            or binding.model_id != policy.policy_id or binding.code_hash != policy.code_hash
            or binding.artifact_hash != content_id(policy)
            or binding.configuration_hash != launch.binding_hash):
        raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
    target = _read(record.active_root / "receipt.json")
    receipt = TickReceipt.model_validate({field: target[field] for field in TickReceipt.model_fields
                                          if field in target})
    if (target.get("code_commit") != record.active_commit
            or target.get("config_hash") != content_id(source.model_dump(mode="json"))
            or receipt.synthetic or not receipt.clock_verified
            or receipt.experiment_id != manifest.experiment_id
            or receipt.status in {"FAULTED", "CLOCK_SYNC_REQUIRED", "LEASE_BUSY"}):
        raise StandbyVerificationError("STANDBY_VERIFICATION_FAILED")
    store = ExperimentStore(source.store, read_only=True)
    events = store.read_as_of(manifest.experiment_id, now)
    commissions = tuple(event.decode(ExplorationCommission) for event in events
                        if event.kind == "ExplorationCommission"
                        and event.decode(ExplorationCommission).policy_id == policy.policy_id)
    if (len(commissions) != 1 or commissions[0].commission_id != proof["commission_id"]
            or commissions[0].binding_hash != binding.configuration_hash
            or commissions[0].manifest_hash != policy.manifest_hash
            or commissions[0].code_commit != record.active_commit
            or not commissions[0].source_authorization_ref.startswith("chat:")
            or not policy.recorded_at <= commissions[0].recorded_at <= receipt.recorded_at
            or not any(event.kind == "ExplorationPolicy" and event.decode(type(policy)) == policy
                       for event in events)
            or not any(event.kind == "TickReceipt" and event.decode(TickReceipt) == receipt
                       for event in events)):
        raise StandbyVerificationError("STANDBY_CONSENT_UNVERIFIED")
    cycles = tuple(event.decode(ExplorationCycleReceipt) for event in events
                   if event.kind == "ExplorationCycleReceipt"
                   and event.decode(ExplorationCycleReceipt).tick_id == receipt.tick_id)
    if len(cycles) > 1 or any(row.commission_id != commissions[0].commission_id
                             or row.policy_id != policy.policy_id for row in cycles):
        raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
    # Adapter only in memory: never invent or write a legacy worker completion.
    config = dict(mode="PAPER", model_id=policy.policy_id,
                  operational_database=str(record.operational_database),
                  risk_per_trade_bps=policy.maximum_entry_risk_bps,
                  max_position_value_bps=policy.maximum_position_bps,
                  policy=launch.policy.model_dump(mode="json"),
                  expected_binding_hash=content_id(binding))
    selected = dict(code_commit=record.active_commit, finished_at=receipt.recorded_at.isoformat(),
                    status="FAILED" if cycles and cycles[0].result.status != "SUCCEEDED" else "OK",
                    paper=dict(binding=binding.model_dump(), identity=proof["identity"],
                               status=cycles[0].result.status if cycles else "WAITING",
                               blockers=list(cycles[0].result.reasons)
                               if cycles else [receipt.reason]))
    return config, selected


def standby_status(root: Path, *, now: datetime) -> dict[str, Any]:
    """Selection plus fresh heartbeat; no broker, database writes or entry authority."""
    record = EntryStandby.model_validate(_read(root / "entry-standby.json", 4096))
    result, finished = _validated_selection(root, record, now=now)
    if not timedelta(0) <= now - finished <= timedelta(minutes=10):
        raise StandbyVerificationError("STANDBY_ENTRY_HEARTBEAT_STALE")
    return result


def observation_standby_status(
    root: Path, *, now: datetime, market: MarketStatus
) -> dict[str, Any]:
    """Classify a verified closure without verifying entry liveness or granting authority.

    Immutable selection, consent and deployment must still agree. Only heartbeat expiry
    is an expected wait; configuration failures and unknown market health remain failures.
    """
    try:
        return standby_status(root, now=now)
    except StandbyVerificationError as error:
        if error.code != "STANDBY_ENTRY_HEARTBEAT_STALE":
            raise
        current = market.current(now)
        if not (
            current.clock_status == "HEALTHY"
            and current.reported_open is False
            and current.expected_open is False
            and current.closed_session_consistent
        ):
            raise
    record = EntryStandby.model_validate(_read(root / "entry-standby.json", 4096))
    result, finished = _validated_selection(root, record, now=now)
    if (
        now - finished <= timedelta(minutes=10)
        or result.get("active_pilot_status") in {"HALTED", "ERROR", "FAILED"}
        or result.get("active_worker_status") in {"ERROR", "FAILED"}
    ):
        raise StandbyVerificationError("STANDBY_ENTRY_HEARTBEAT_STALE")
    result.update(
        status="STANDBY_UNVERIFIED",
        entry_expected_wait=True,
        reason="Fresh PAPER clock and calendar verify closure; entry heartbeat remains stale.",
        blockers=[*result["blockers"], "SELECTED_ENTRY_MARKET_CLOSED"],
    )
    return result


def refresh_standby_binding(
    root: Path,
    *,
    expected_commit: str,
    expected_model_id: str,
    expected_previous_hash: str,
    backup_directory: Path,
    now: datetime,
) -> dict[str, Any]:
    """Audited operator transition metadata only, never consent, promotion or health.

    The exact deployed receipt, existing consent and both registry records must agree.
    Heartbeat validity remains independently measured and is not fabricated for a paused
    entry task. Serialize with the original learner and compare the old bytes before write.
    """
    path = root / "entry-standby.json"
    with SingleWriterLock(root / "state" / "learner-worker.lock"):
        before = path.read_bytes()
        if hashlib.sha256(before).hexdigest() != expected_previous_hash:
            raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
        previous = EntryStandby.model_validate_json(before)
        config = _read(previous.active_root / "paper-execution.json")
        target = _read(previous.active_root / "worker.json")
        paper = target.get("paper", {})
        binding = PaperModelBinding.model_validate(paper["binding"])
        if (
            target.get("code_commit") != expected_commit
            or config.get("model_id") != expected_model_id
            or binding.model_id != expected_model_id
        ):
            raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
        candidate = EntryStandby.model_validate(
            {
                **previous.model_dump(by_alias=True),
                "active_commit": expected_commit,
                "active_strategy_id": paper["identity"]["strategy_id"],
                "paper_configuration_hash": content_id(config),
                "recorded_at": now,
            }
        )
        _, finished = _validated_selection(root, candidate, now=now)
        fresh = timedelta(0) <= now - finished <= timedelta(minutes=10)
        result = dict(
            metadata_bound=True,
            runtime_health_verified=False,
            current_status_code="FRESH_ENTRY_RECEIPT_NOT_ENTRY_AUTHORITY"
            if fresh
            else "STANDBY_ENTRY_HEARTBEAT_STALE",
            prior_hash=hashlib.sha256(before).hexdigest(),
            active_commit=candidate.active_commit,
            active_strategy_id=candidate.active_strategy_id,
            model_id=binding.model_id,
            authority_changed=False,
            consent_written=False,
            orders_submitted=0,
            operational_writes=0,
        )
        backup_directory.mkdir(parents=True, exist_ok=False)
        (backup_directory / "entry-standby-before.json").write_bytes(before)
        after = candidate.model_dump_json(by_alias=True, indent=2).encode()
        result["new_hash"] = hashlib.sha256(after).hexdigest()
        (backup_directory / "transition.json").write_text(json.dumps(result, indent=2))
        temporary = path.with_suffix(".transition-tmp")
        try:
            with temporary.open("xb") as output:
                output.write(after)
                output.flush()
                os.fsync(output.fileno())
            if path.read_bytes() != before:
                raise StandbyVerificationError("STANDBY_BINDING_MISMATCH")
            os.replace(temporary, path)
            if path.read_bytes() != after:
                raise RuntimeError("STANDBY_METADATA_READBACK_FAILED")
        except Exception:
            # Restore metadata only; never rewind alerts, evidence, consent or the registry.
            if path.read_bytes() == after:
                rollback = path.with_suffix(".rollback-tmp")
                with rollback.open("xb") as output:
                    output.write(before)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(rollback, path)
            raise
        return result
