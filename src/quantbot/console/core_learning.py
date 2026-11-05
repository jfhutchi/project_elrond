"""Bounded recorded learning evidence; deliberately grants no execution authority."""

import math
from datetime import date, datetime
from typing import TypeVar

from pydantic import BaseModel, ConfigDict
from sqlalchemy import LargeBinary, func, literal_column, select
from sqlalchemy.orm import Session

from quantbot.paper_core.activation import Activation
from quantbot.paper_core.contracts import Decision, LearningReceipt, ModelArtifact
from quantbot.storage.schema import metadata

REASONS = frozenset(
    {
        "VALIDATION_GATE_FAILED",
        "PASSED_FIXED_PROCEDURE_GATES",
        "INSUFFICIENT_EPISODES_OR_SESSIONS",
        "TEN_COMPLETED_SESSION_WARMUP",
        "SESSION_BUDGET_OR_INPUT_REUSED",
        "MISSING_VERIFIED_SESSION_SEQUENCE",
    }
)
METRICS = (
    "candidate_mse",
    "incumbent_mse",
    "constant_mse",
    "candidate_worst_session_mse",
    "incumbent_worst_session_mse",
    "constant_worst_session_mse",
)
MAX_BYTES = 2 * 1024 * 1024
Document = TypeVar("Document", Activation, LearningReceipt, ModelArtifact, Decision)


class CoreLearning(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    available: bool = False
    model_id: str | None = None
    model_status: str = "Evidence unavailable"
    activation_at: datetime | None = None
    # Receipt associated with the active selection, not a globally latest consideration.
    consideration_at: datetime | None = None
    consideration_reason: str | None = None
    attempt_session: date | None = None
    attempt_at: datetime | None = None
    attempt_verdict: str = "No evaluated attempt recorded"
    attempt_reason: str | None = None
    training_samples: int | None = None
    validation_samples: int | None = None
    validation_sessions: int | None = None
    metrics: tuple[tuple[str, float], ...] = ()
    target: str | None = None
    decision_model_id: str | None = None
    decision_selection: str | None = None
    decision_at: datetime | None = None
    decision_prediction_bps: float | None = None


def _reason(value: str) -> str:
    return value if value in REASONS else "Unrecognized reason; inspect private receipts"


def _document(session: Session, name: str, identity: str, kind: type[Document]) -> Document:
    table = metadata.tables[name]
    # Enforce the cap in SQL before materializing any potentially large payload.
    payload = session.execute(
        select(table.c.payload).where(
            table.c.id == identity,
            func.length(func.cast(table.c.payload, LargeBinary)) <= MAX_BYTES,
        )
    ).scalar_one_or_none()
    if payload is None:
        raise ValueError("Missing or oversized evidence")
    document = kind.model_validate_json(payload)
    if document.id != identity:
        raise ValueError("Evidence hash mismatch")
    return document


def _receipt(session: Session, identity: str, now: datetime) -> LearningReceipt:
    receipt = _document(session, "core_learning_runs", identity, LearningReceipt)
    mapping = metadata.tables["core_learning_considerations"]
    mapped = session.execute(
        select(mapping.c.receipt_id).where(mapping.c.run_id == receipt.run_id)
    ).scalar_one_or_none()
    timestamps = (
        receipt.recorded_at_utc,
        receipt.cutoff,
        receipt.started_at,
        receipt.fit_started_at,
        receipt.completed_at,
        receipt.available_at,
    )
    if (
        mapped != receipt.id
        or receipt.evidence_lane != "BROKER_PAPER"
        or receipt.policy.evidence_lane != receipt.evidence_lane
        or receipt.policy.parent_model.id != receipt.parent_model_id
        or any(at > now for at in timestamps if at is not None)
        or (receipt.completed_session and receipt.completed_session > now.date())
    ):
        raise ValueError("Receipt binding unavailable")
    return receipt


def _selection(
    session: Session, event_id: str, config_id: str, now: datetime
) -> tuple[ModelArtifact, Activation, LearningReceipt | None, LearningReceipt | None]:
    event = _document(session, "core_activations", event_id, Activation)
    model = _document(session, "core_models", event.model_id, ModelArtifact)
    if (
        event.config_id != config_id
        or event.evidence_lane != "BROKER_PAPER"
        or event.generation < 1
        or event.at > now
        or event.model_digest != model.id
    ):
        raise ValueError("Selection binding unavailable")
    receipt = _receipt(session, event.receipt_id, now) if event.receipt_id else None
    if receipt and (
        receipt.resulting_model_id != model.id
        or receipt.recorded_at_utc > event.at
        or event.learning_run_id != receipt.run_id
        or receipt.status not in {"NO_CHANGE", "AWAITING_ACTIVATION"}
        or (receipt.status == "NO_CHANGE" and receipt.parent_model_id != model.id)
        or (
            receipt.status == "AWAITING_ACTIVATION"
            and (
                receipt.candidate_model_id != model.id
                or model.learning_receipt_id != receipt.run_id
            )
        )
    ):
        raise ValueError("Selection receipt mismatch")
    if receipt is None and event.action != "INITIALIZE":
        raise ValueError("Selection receipt missing")
    training = None
    if model.estimator is not None:
        mapping = metadata.tables["core_learning_considerations"]
        training_id = session.execute(
            select(mapping.c.receipt_id).where(mapping.c.run_id == model.learning_receipt_id)
        ).scalar_one_or_none()
        if training_id is None:
            raise ValueError("Final training receipt missing")
        training = _receipt(session, training_id, now)
        if (
            training.status != "AWAITING_ACTIVATION"
            or training.resulting_model_id != model.id
            or training.candidate_model_id != model.id
            or training.parent_model_id != model.parent_model_id
            or training.recorded_at_utc > event.at
            or training.policy.target_contract != model.target_contract
            or training.policy.policy_id != model.policy_id
        ):
            raise ValueError("Final training receipt mismatch")
    return model, event, receipt, training


def read_core_learning(session: Session, *, config_id: str, now: datetime) -> CoreLearning:
    """Read fixed-size selections only, without replaying authority or loading history."""
    try:
        pointer_table = metadata.tables["core_active_model"]
        pointer = (
            session.execute(select(pointer_table).where(pointer_table.c.id == 1))
            .mappings()
            .one_or_none()
        )
        if pointer is None:
            return CoreLearning()
        model, activation, consideration, training = _selection(
            session, pointer["activation_id"], config_id, now
        )
        if (
            pointer["model_id"] != model.id
            or pointer["generation"] != activation.generation
            or pointer["receipt_id"] != activation.receipt_id
        ):
            raise ValueError("Pointer mismatch")
        attempts = metadata.tables["core_learning_attempts"]
        attempt = (
            session.execute(
                select(attempts.c.session, attempts.c.run_id, attempts.c.search_id)
                .order_by(attempts.c.session.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        evaluated = None
        attempt_session = None
        if attempt:
            attempt_session = date.fromisoformat(attempt["session"])
            mapping = metadata.tables["core_learning_considerations"]
            receipt_id = session.execute(
                select(mapping.c.receipt_id).where(mapping.c.run_id == attempt["run_id"])
            ).scalar_one_or_none()
            if receipt_id is None:
                raise ValueError("Attempt receipt missing")
            evaluated = _receipt(session, receipt_id, now)
            if (
                attempt_session > now.date()
                or evaluated.run_id != attempt["run_id"]
                or evaluated.trial_charge_id != attempt["search_id"]
                or evaluated.status == "NO_CHANGE"
                or (
                    evaluated.status in {"CANDIDATE_REJECTED", "FIT_FAILED"}
                    and evaluated.resulting_model_id != evaluated.parent_model_id
                )
                or (evaluated.completed_session and evaluated.completed_session != attempt_session)
            ):
                raise ValueError("Attempt binding mismatch")
        # Last inserted decision, including a historical model: never date-sort all history.
        decisions = metadata.tables["core_decisions"]
        decision_id = session.execute(
            select(decisions.c.id).order_by(literal_column("core_decisions.rowid").desc()).limit(1)
        ).scalar_one_or_none()
        decision = None
        if decision_id:
            decision = _document(session, "core_decisions", decision_id, Decision)
            if not decision.activation_id:
                raise ValueError("Decision selection missing")
            source_model, source_event, _, _ = _selection(
                session, decision.activation_id, config_id, now
            )
            if (
                decision.config_id != config_id
                or decision.evidence_lane != "BROKER_PAPER"
                or not source_event.at <= decision.recorded_at_utc <= now
                or decision.model_id != source_model.id
                or decision.model_digest != source_model.id
                or decision.model_generation != source_event.generation
                or decision.model_mode != source_model.mode
                or decision.learning_receipt_id
                != (source_event.receipt_id or source_model.learning_receipt_id)
            ):
                raise ValueError("Decision binding mismatch")
        coverage = evaluated or training or consideration
        metrics = dict(coverage.metrics) if coverage else {}
        if any(not math.isfinite(value) for key, value in metrics.items() if key in METRICS):
            raise ValueError("Nonfinite metrics")
        return CoreLearning(
            available=True,
            model_id=model.id,
            model_status=(
                "COLLECT · no fitted model"
                if model.mode == "COLLECT"
                else "PREDICT · fitted model"
                if model.estimator
                else "PREDICT · fixture model"
            ),
            activation_at=activation.at,
            consideration_at=consideration.recorded_at_utc if consideration else None,
            consideration_reason=_reason(consideration.reason) if consideration else None,
            attempt_session=attempt_session,
            attempt_at=evaluated.recorded_at_utc if evaluated else None,
            attempt_verdict=evaluated.status if evaluated else "No evaluated attempt recorded",
            attempt_reason=_reason(evaluated.reason) if evaluated else None,
            training_samples=len(coverage.training_ids) if coverage else None,
            validation_samples=len(coverage.validation_ids) if coverage else None,
            validation_sessions=len({m.session for m in coverage.session_metrics})
            if coverage
            else None,
            metrics=tuple((key, metrics[key]) for key in METRICS if key in metrics),
            target=(
                "Gross"
                if coverage.policy.target_contract == "owned-executed-paper-gross-return-v1"
                else "Net"
            )
            if coverage
            else None,
            decision_model_id=decision.model_id if decision else None,
            decision_selection=decision.selection.kind if decision and decision.selection else None,
            decision_at=decision.recorded_at_utc if decision else None,
            decision_prediction_bps=decision.prediction_bps if decision else None,
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return CoreLearning()
