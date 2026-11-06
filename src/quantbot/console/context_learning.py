"""Bounded read-only context-learning metadata; no trading or training imports."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from quantbot.research.manifest import content_id

MAX_BYTES = 256 * 1024
KEYS = ("context:learning-status", "context:learning-binding", "context:interpreter-current")


class ContextLearningPanel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state: str = "UNCOMMISSIONED"
    status_changed_at: datetime | None = None
    observed_snapshots: int | None = None
    observed_documents: int | None = None
    used_documents: int | None = None
    feedback: int | None = None
    independent_reviews: int | None = None
    model_generated_reviews: int | None = None
    semantic_target: str | None = None
    interpreter_kind: str | None = None
    eligible_examples: int | None = None
    fitted_candidates: int | None = None
    trial_fits: int | None = None
    shadow_preparations: int | None = None
    interpreter_mode: str = "UNVERIFIED"
    interpreter_version: str | None = None
    current_model_inputs: str = "PRICE_ONLY_NO_VERIFIED_CONTEXT_FILTER"
    training_start: datetime | None = None
    split_at: datetime | None = None
    evaluation_end: datetime | None = None
    impact_status: str | None = None
    impact_due: datetime | None = None
    gates: tuple[tuple[str, int], ...] = ()


def _stamp(value: Any) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("aware status time required")
    return parsed.astimezone(UTC)


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 10**9:
        raise ValueError("bounded nonnegative count required")
    return value


def read_context_learning(path: Path | None, *, now: datetime) -> ContextLearningPanel:
    """Read exact metadata keys only. Unchanged status time is not a worker heartbeat."""
    if path is None or not path.is_file():
        return ContextLearningPanel()
    try:
        if path.is_symlink():
            raise ValueError("research symlink unavailable")
        with closing(
            sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: 1, 10000)
            if (
                db.execute(
                    "select 1 from sqlite_master where type='table' and name='metadata'"
                ).fetchone()
                is None
            ):
                return ContextLearningPanel()
            rows = db.execute(
                "select key,case when length(cast(payload as blob))<=? then payload else null end "
                "from metadata where key in (?,?,?)",
                (MAX_BYTES, *KEYS),
            ).fetchall()
            data = {key: json.loads(payload) for key, payload in rows}
            binding = data.get("context:learning-binding")
            if (
                not isinstance(binding, dict)
                or binding.get("contract") != "SCHEDULED_CONTEXT_LEARNING_V1"
            ):
                return ContextLearningPanel()
            status = data.get("context:learning-status")
            if status is None:
                return ContextLearningPanel(state="PENDING")
            if (
                not isinstance(status, dict)
                or status.get("contract") != "SCHEDULED_CONTEXT_LEARNING_STATUS_V1"
            ):
                raise ValueError("unsupported status")
            changed = _stamp(status.get("recorded_at"))
            if changed is None or changed > now:
                raise ValueError("future or missing status timestamp")
            state = status.get("state")
            if state not in {"UNAVAILABLE", "COLLECTING", "WAITING", "EVALUATED"}:
                raise ValueError("invalid status state")
            gates: list[tuple[str, int]] = []
            for code, count in status.get("gate_counts", ()):
                if len(gates) >= 64:
                    raise ValueError("too many status gates")
                safe = (
                    code
                    if isinstance(code, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}(?::[A-Za-z][A-Za-z0-9_]{0,40})?", code)
                    else "UNRECOGNIZED_PRIVATE_GATE"
                )
                gates.append((safe, _count(count) or 0))
            mode, version_id = "UNVERIFIED", None
            pointer = data.get("context:interpreter-current")
            if isinstance(pointer, dict):
                pointer = pointer.get("version_id")
            if isinstance(pointer, str) and re.fullmatch(r"[a-f0-9]{64}", pointer):
                row = db.execute(
                    "select payload from receipts where id=? and length(cast(payload as blob))<=?",
                    (pointer, MAX_BYTES),
                ).fetchone()
                if row:
                    version = json.loads(row[0])
                    version_at = (
                        _stamp(version.get("recorded_at")) if isinstance(version, dict) else None
                    )
                    if (
                        isinstance(version, dict)
                        and content_id(version) == pointer
                        and version.get("mode") in {"SHADOW_ONLY", "TRADING_FILTER", "DISABLED"}
                        and version_at is not None
                        and version_at <= now
                    ):
                        mode, version_id = version["mode"], pointer
            return ContextLearningPanel(
                state=state,
                status_changed_at=changed,
                observed_snapshots=_count(status.get("snapshots_journaled")),
                observed_documents=_count(status.get("observed_documents")),
                used_documents=_count(status.get("used_documents")),
                feedback=_count(status.get("feedback_journaled")),
                independent_reviews=_count(status.get("independent_reviews")),
                model_generated_reviews=_count(status.get("model_generated_reviews")),
                semantic_target=status.get("semantic_target")
                if status.get("semantic_target")
                in {"INDEPENDENT_READING_POLARITY", "MODEL_TEACHER_AGREEMENT_V1"}
                else None,
                interpreter_kind=status.get("interpreter_kind")
                if status.get("interpreter_kind")
                in {"UNTRAINED_FIXED_LEXICAL_BASELINE", "REVIEW_CALIBRATED_CANDIDATE"}
                else None,
                eligible_examples=_count(status.get("eligible_examples")),
                fitted_candidates=_count(status.get("fitted_candidates")),
                trial_fits=_count(status.get("trial_fits")),
                shadow_preparations=_count(status.get("shadow_preparations")),
                interpreter_mode=mode,
                interpreter_version=version_id,
                current_model_inputs="PRICE_MODEL_WITH_APPROVED_CONTEXT_FILTER"
                if mode == "TRADING_FILTER"
                else "PRICE_ONLY_NO_VERIFIED_CONTEXT_FILTER",
                training_start=_stamp(status.get("training_start")),
                split_at=_stamp(status.get("split_at")),
                evaluation_end=_stamp(status.get("evaluation_end")),
                impact_status=status.get("impact_status")
                if status.get("impact_status")
                in {"WAITING", "EVALUATED", "INSUFFICIENT_EVIDENCE", "UNAVAILABLE"}
                else None,
                impact_due=_stamp(status.get("impact_evaluation_end")),
                gates=tuple(gates),
            )
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return ContextLearningPanel(state="ERROR", gates=(("CONTEXT_STATUS_UNAVAILABLE", 1),))
