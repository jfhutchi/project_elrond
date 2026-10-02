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
    config = _read(record.active_root / "paper-execution.json")
    if content_id(config) != record.paper_configuration_hash:
        raise StandbyVerificationError("STANDBY_CONFIGURATION_MISMATCH")
    policy = config.get("policy", {})
    if (
        config.get("mode") != "PAPER"
        or config.get("experimental_symbol") != "DIA"
        or config.get("operational_database") != str(record.operational_database)
        or not 0 < config.get("risk_per_trade_bps", 10000) <= 25
        or not 0 < config.get("max_position_value_bps", 10000) <= 500
        or policy.get("exclusive_learner_entry") is not True
        or policy.get("stop_loss_bps") != 100
        or not policy.get("inherited")
    ):
        raise StandbyVerificationError("STANDBY_CONFIGURATION_MISMATCH")
    if not consent_recorded(record.active_root, now=now):
        raise StandbyVerificationError("STANDBY_CONSENT_UNVERIFIED")
    target = _read(record.active_root / "worker.json")
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
        blockers=list(paper.get("blockers", [])),
        cleared_gate_codes=[
            "PAPER_PROMOTION_REQUIRED",
            "PAPER_EXECUTION_NOT_CONFIGURED",
            "PAPER_OBSERVATION_NOT_EARNED",
        ],
        orders_submitted=0,
    ), finished


def standby_status(root: Path, *, now: datetime) -> dict[str, Any]:
    """Selection plus fresh heartbeat; no broker, database writes or entry authority."""
    record = EntryStandby.model_validate(_read(root / "entry-standby.json", 4096))
    result, finished = _validated_selection(root, record, now=now)
    if not timedelta(0) <= now - finished <= timedelta(minutes=10):
        raise StandbyVerificationError("STANDBY_ENTRY_HEARTBEAT_STALE")
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
