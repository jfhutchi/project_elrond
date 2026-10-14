"""Bounded shadow learner over guarded owned outcomes; no activation or broker interface."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from quantbot.learner.estimators import fit_estimator, predict
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.paper_core.contracts import (
    LearningEpisode,
    LearningPolicy,
    LearningReceipt,
    ModelArtifact,
    Observation,
    RoundTrip,
    SessionMetrics,
)
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.datasets import observation_identity
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.lock import AlreadyLockedError, SingleWriterLock
from quantbot.storage.schema import learner_datasets, search_runs


def predict_bps(model: ModelArtifact, features: Sequence[Mapping[str, float]]) -> tuple[float, ...]:
    """Estimator parameters and prediction outputs use bps, clipped exactly once here."""
    rows = tuple(tuple(row[n] for n in FEATURE_NAMES) for row in features)
    if model.mode == "COLLECT":
        # An explicit zero constant comparison, never relabel COLLECT as fitted.
        values = (0.0,) * len(rows)
    elif model.estimator is not None:
        values = predict(model.estimator, rows)
    elif model.fixture_authority and model.constant_bps is not None:
        values = (model.constant_bps,) * len(rows)
    else:
        raise ValueError("NONPREDICTIVE_INCUMBENT")
    return tuple(max(-100.0, min(100.0, v)) for v in values)


def _target(episode: LearningEpisode) -> float:
    trip = episode.outcome
    if trip.status != "COMPLETE" or trip.net_pnl is None or trip.entry_notional <= 0:
        raise ValueError("UNKNOWN_COSTS_OR_NOTIONAL")
    value = float(trip.net_pnl / trip.entry_notional * 10000)
    if not math.isfinite(value):
        raise ValueError("NONFINITE_EXECUTED_RETURN")
    return value


def fit_candidate(
    outcomes: tuple[LearningEpisode, ...],
    parent: ModelArtifact,
    policy: LearningPolicy,
    *,
    run_id: str = "DIRECT_SHADOW_FIT",
) -> ModelArtifact:
    """Fit only the immutable guarded join, never fixed-horizon market labels."""
    if not outcomes:
        raise ValueError("EMPTY_TRAINING")
    for episode in outcomes:
        trip, obs = episode.outcome, episode.observation
        if (
            trip.policy_id != policy.policy_id
            or trip.dataset != policy.dataset
            or trip.evidence_lane != policy.evidence_lane
            or obs.feature_version != policy.feature_version
            or tuple(n for n, _ in obs.features) != FEATURE_NAMES
            or obs.available_at > obs.recorded_at_utc
            or obs.source_end > obs.available_at
            or obs.source_start >= obs.source_end
            or trip.source_start != obs.source_start
            or trip.source_end is None
            or trip.available_at is None
            or trip.available_at < trip.source_end
        ):
            raise ValueError("INCOMPATIBLE_OR_NONCAUSAL_INPUT")
    estimator = fit_estimator(
        tuple(tuple(v for _, v in e.observation.features) for e in outcomes),
        tuple(_target(e) for e in outcomes),
        FEATURE_NAMES,
        family=policy.family,
        alpha=policy.alpha,
        seed=policy.seed,
    )
    return ModelArtifact(
        mode="PREDICT",
        description="shadow owned after-cost ridge candidate",
        feature_version=policy.feature_version,
        estimator=estimator,
        target_contract=policy.target_contract,
        policy_id=policy.policy_id,
        parent_model_id=parent.id,
        input_ids=tuple(e.outcome.id for e in outcomes),
        learning_receipt_id=run_id,
    )


def _mse(actual: Sequence[float], values: Sequence[float]) -> float:
    return math.fsum((a - v) ** 2 for a, v in zip(actual, values, strict=True)) / len(actual)


def _validation(
    candidate: ModelArtifact, parent: ModelArtifact, validation: tuple[LearningEpisode, ...]
) -> tuple[dict[str, float], tuple[SessionMetrics, ...]]:
    features = tuple(dict(e.observation.features) for e in validation)
    actual = tuple(_target(e) for e in validation)
    predicted, incumbent = predict_bps(candidate, features), predict_bps(parent, features)
    assert candidate.estimator is not None
    # Constant is training mean, frozen before later validation is opened.
    constant = (candidate.estimator.intercept,) * len(validation)
    sessions = []
    for day in sorted({e.session for e in validation}):
        indexes = [i for i, e in enumerate(validation) if e.session == day]
        a, c, p, z = (
            tuple(values[i] for i in indexes) for values in (actual, predicted, incumbent, constant)
        )
        sessions.append(
            SessionMetrics(
                session=day,
                outcome_ids=tuple(validation[i].outcome.id for i in indexes),
                actual_bps=a,
                candidate_bps=c,
                incumbent_bps=p,
                constant_bps=z,
                candidate_mse=_mse(a, c),
                incumbent_mse=_mse(a, p),
                constant_mse=_mse(a, z),
            )
        )
    return {
        "candidate_mse": _mse(actual, predicted),
        "incumbent_mse": _mse(actual, incumbent),
        "constant_mse": _mse(actual, constant),
        "candidate_worst_session_mse": max(s.candidate_mse for s in sessions),
        "incumbent_worst_session_mse": max(s.incumbent_mse for s in sessions),
        "constant_worst_session_mse": max(s.constant_mse for s in sessions),
    }, tuple(sessions)


def validate_candidate(
    candidate: ModelArtifact, parent: ModelArtifact, validation: tuple[LearningEpisode, ...]
) -> dict[str, float]:
    return _validation(candidate, parent, validation)[0]


def _passes_validation(metrics: dict[str, float], improvement: float) -> bool:
    return (
        metrics["candidate_mse"] < min(metrics["incumbent_mse"], metrics["constant_mse"])
        and metrics["candidate_mse"] <= (1 - improvement) * metrics["incumbent_mse"]
        and metrics["candidate_mse"] <= (1 - improvement) * metrics["constant_mse"]
        and metrics["candidate_worst_session_mse"]
        <= min(metrics["incumbent_worst_session_mse"], metrics["constant_worst_session_mse"])
    )


def _guard(session: Session, dataset: str, start: date, end: date, *, now: date) -> str | None:
    if HypothesisRegistry(session).blocking_protected_windows(
        dataset, start, end, now=now, include_forward_paper=True
    ):
        return "CANONICAL_PROTECTED_OR_FORWARD_WINDOW"
    # Extract only metadata in SQL. Full Dataset documents contain feature/target rows.
    holds = session.execute(
        select(
            func.json_extract(learner_datasets.c.document_json, "$.holdout_window.dataset"),
            func.json_extract(learner_datasets.c.document_json, "$.holdout_window.start"),
            func.json_extract(learner_datasets.c.document_json, "$.holdout_window.end"),
        )
    )
    for source, first, last in holds:
        if source is None or first is None or last is None:
            raise ValueError("MALFORMED_CANONICAL_HOLDOUT_METADATA")
        if (
            observation_identity(str(source)) == observation_identity(dataset)
            and start <= date.fromisoformat(str(last))
            and date.fromisoformat(str(first)) <= end
        ):
            return "DECLARED_LEARNER_HOLDOUT"
    return None


def _metadata(session: Session) -> list[dict[str, Any]]:
    index, decisions, observations = (
        table(n) for n in ("core_outcome_index", "core_decisions", "core_observations")
    )
    join = index.join(decisions, decisions.c.id == index.c.lot_id).join(
        observations,
        observations.c.id == func.json_extract(decisions.c.payload, "$.observation_id"),
    )
    return [
        dict(row)
        for row in session.execute(
            select(
                index,
                observations.c.id.label("observation_id"),
                func.json_extract(observations.c.payload, "$.feature_version").label(
                    "feature_version"
                ),
                func.json_extract(observations.c.payload, "$.session_close").label("session_close"),
            ).select_from(join)
        ).mappings()
    ]


def _select(
    session: Session, policy: LearningPolicy, cutoff: datetime
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], tuple[str, ...]]:
    rows, excluded = [], []
    metadata = _metadata(session)
    considered = tuple(sorted(str(r["outcome_id"]) for r in metadata))
    for row in metadata:
        reason = None
        start, end, available = (
            decode_utc(str(row[k])) for k in ("source_start", "source_end", "available_at")
        )
        if (
            row["dataset"] != policy.dataset
            or row["policy_id"] != policy.policy_id
            or row["evidence_lane"] != policy.evidence_lane
            or row["feature_version"] != policy.feature_version
        ):
            reason = "INCOMPATIBLE_CONTRACT_OR_LANE"
        elif start >= end or available < end:
            reason = "NONCAUSAL_METADATA"
        elif available > cutoff or end > cutoff:
            reason = "UNAVAILABLE_AT_CUTOFF"
        elif row["status"] != "COMPLETE":
            reason = "COSTS_PENDING"
        elif decode_utc(str(row["session_close"])) > cutoff:
            reason = "SESSION_NOT_COMPLETED"
        else:
            reason = _guard(session, policy.dataset, start.date(), end.date(), now=cutoff.date())
        row.update(
            start=start, end=end, session=end.astimezone(ZoneInfo("America/New_York")).date()
        )
        if reason:
            excluded.append((str(row["outcome_id"]), reason))
        else:
            rows.append(row)
    days = sorted({r["session"] for r in rows})[-policy.max_sessions :]
    selected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (r["end"], r["outcome_id"])):
        reason = None
        if row["session"] not in days:
            reason = "OUTSIDE_LAST_60_SESSIONS"
        elif selected and row["start"] < selected[-1]["end"]:
            reason = "INTERSECTING_FEATURE_HOLDING_WINDOW"
        if reason:
            excluded.append((str(row["outcome_id"]), reason))
        else:
            selected.append(row)
    return selected, sorted(excluded), considered


def _materialize(session: Session, rows: list[dict[str, Any]]) -> tuple[LearningEpisode, ...]:
    result = []
    for row in rows:

        def payload(name: str, identity: str) -> str:
            return str(
                session.execute(
                    select(table(name).c.payload).where(table(name).c.id == identity)
                ).scalar_one()
            )

        outcome = RoundTrip.model_validate_json(
            payload("core_outcome_revisions", row["outcome_id"])
        )
        observation = Observation.model_validate_json(
            payload("core_observations", row["observation_id"])
        )
        if (
            outcome.id != row["outcome_id"]
            or observation.id != row["observation_id"]
            or outcome.source_start != row["start"]
            or outcome.source_end != row["end"]
        ):
            raise ValueError("IMMUTABLE_INPUT_METADATA_MISMATCH")
        result.append(
            LearningEpisode(outcome=outcome, observation=observation, session=row["session"])
        )
    return tuple(result)


def _fit_subprocess(
    export: dict[str, Any], policy: LearningPolicy, *, scratch_root: Path
) -> ModelArtifact:
    """Restricted file interface; no operational DB or credential environment passed."""
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC") if key in os.environ}
    folder = scratch_root / str(export["run_id"])
    folder.mkdir(parents=True, exist_ok=True)
    source, result, permission = folder / "input.json", folder / "result.json", folder / "allowed"
    encoded = canonical_result_json(export)
    if source.exists() and source.read_text(encoding="utf-8") != encoded:
        raise ValueError("IMMUTABLE_SCRATCH_INPUT_CONFLICT")
    source.write_text(encoded, encoding="utf-8")
    permission.write_text(content_id(export), encoding="utf-8")
    env.update(TEMP=str(folder), TMP=str(folder))
    try:
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "quantbot.paper_core.learning",
                str(source),
                str(result),
            ],
            cwd=folder,
            env=env,
            check=True,
            timeout=policy.fit_timeout_seconds,
            capture_output=True,
        )
        candidate = ModelArtifact.model_validate_json(result.read_text(encoding="utf-8"))
    finally:
        permission.unlink(missing_ok=True)
    # Reproducibility check recomputes the pure fixed fit without a tuning search.
    # The numerical replay runs in the same bounded child, verified through its output hash.
    return candidate


def _save_receipt(session: Session, receipt: LearningReceipt) -> LearningReceipt:
    put(session, "core_learning_runs", receipt.id, receipt)
    session.execute(
        table("core_learning_considerations")
        .insert()
        .values(run_id=receipt.run_id, receipt_id=receipt.id)
    )
    return receipt


def consider_learning(
    store: CoreStore, policy: LearningPolicy, *, cutoff: datetime
) -> LearningReceipt:
    """Serialize admission and recover crashed fits with fresh process-lifetime evidence."""
    assert store.database.path is not None
    scratch_root = store.database.path.parent / (store.database.path.name + ".learning-scratch")
    admission = SingleWriterLock(scratch_root / "admission.lock")
    try:
        admission.acquire()
    except AlreadyLockedError:
        return _busy_receipt(store, policy, cutoff)
    try:
        worker = SingleWriterLock(scratch_root / "worker.lock")
        try:
            worker.acquire()
        except AlreadyLockedError:
            return _busy_receipt(store, policy, cutoff)
        try:
            # While holding both locks, cancel a pre-lock orphan before it can fit.
            # A currently fitting orphan owns worker.lock and cannot reach this branch.
            with store.database.transaction() as session:
                session.execute(text("BEGIN IMMEDIATE"))
                attempts, pointers = (
                    table("core_learning_attempts"),
                    table("core_learning_considerations"),
                )
                pending = (
                    session.execute(
                        select(attempts).where(attempts.c.run_id.not_in(select(pointers.c.run_id)))
                    )
                    .mappings()
                    .all()
                )
                for row in pending:
                    (scratch_root / str(row["run_id"]) / "allowed").unlink(missing_ok=True)
                    fields = json.loads(str(row["reservation_payload"]))
                    _save_receipt(
                        session,
                        LearningReceipt(
                            **fields,
                            status="FIT_FAILED",
                            reason="PRIOR_PROCESS_LOST",
                            validation_absent_reason="NO_COMPLETION_AFTER_FRESH_WORKER_LOCK",
                        ),
                    )
        finally:
            worker.release()
        return _consider_learning(store, policy, cutoff=cutoff, scratch_root=scratch_root)
    finally:
        admission.release()


def _busy_receipt(store: CoreStore, policy: LearningPolicy, cutoff: datetime) -> LearningReceipt:
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _, exclusions, considered = _select(session, policy, cutoff)
        run_id = content_id(
            {"busy": True, "policy": policy, "cutoff": cutoff, "inputs": considered}
        )
        existing = session.execute(
            select(table("core_learning_considerations").c.receipt_id).where(
                table("core_learning_considerations").c.run_id == run_id
            )
        ).scalar_one_or_none()
        if existing:
            return LearningReceipt.model_validate_json(
                session.execute(
                    select(table("core_learning_runs").c.payload).where(
                        table("core_learning_runs").c.id == existing
                    )
                ).scalar_one()
            )
        receipt = LearningReceipt(
            recorded_at_utc=cutoff,
            cutoff=cutoff,
            run_id=run_id,
            policy=policy,
            parent_model_id=policy.parent_model.id,
            resulting_model_id=policy.parent_model.id,
            considered_ids=considered,
            exclusions=tuple(exclusions),
            split_digest=content_id({}),
            status="NO_CHANGE",
            reason="LEARNING_WORKER_BUSY",
            validation_absent_reason="LEARNING_WORKER_BUSY",
        )
        put(session, "core_models", policy.parent_model.id, policy.parent_model)
        return _save_receipt(session, receipt)


def _consider_learning(
    store: CoreStore, policy: LearningPolicy, *, cutoff: datetime, scratch_root: Path
) -> LearningReceipt:
    cutoff = decode_utc(encode_utc(cutoff))
    parent = policy.parent_model
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        # Fail closed on malformed historical accounting; never erase its burden.
        HypothesisRegistry(session).learner_trials_spent()
        rows, excluded, considered = _select(session, policy, cutoff)
        run_id = content_id(
            {"cutoff": cutoff, "policy": policy, "considered": considered, "excluded": excluded}
        )
        pointer = table("core_learning_considerations")
        previous = session.execute(
            select(pointer.c.receipt_id).where(pointer.c.run_id == run_id)
        ).scalar_one_or_none()
        if previous:
            return LearningReceipt.model_validate_json(
                session.execute(
                    select(table("core_learning_runs").c.payload).where(
                        table("core_learning_runs").c.id == previous
                    )
                ).scalar_one()
            )
        put(session, "core_models", parent.id, parent)
        days = sorted({r["session"] for r in rows})
        validation_days = days[-policy.min_validation_sessions :]
        validation_rows = [r for r in rows if r["session"] in validation_days]
        training_rows = [r for r in rows if r["session"] not in validation_days]
        if validation_rows:
            boundary = validation_rows[0]["start"] - timedelta(minutes=policy.embargo_minutes)
            purged = [r for r in training_rows if r["end"] > boundary]
            training_rows = [r for r in training_rows if r["end"] <= boundary]
            excluded.extend((r["outcome_id"], "TRAIN_VALIDATION_EMBARGO") for r in purged)
        training_ids = tuple(r["outcome_id"] for r in training_rows)
        validation_ids = tuple(r["outcome_id"] for r in validation_rows)
        split = content_id({"training": training_ids, "validation": validation_ids})
        fields: dict[str, Any] = dict(
            recorded_at_utc=cutoff,
            run_id=run_id,
            cutoff=cutoff,
            parent_model_id=parent.id,
            resulting_model_id=parent.id,
            policy=policy,
            considered_ids=considered,
            exclusions=tuple(sorted(excluded)),
            split_digest=split,
            training_ids=training_ids,
            validation_ids=validation_ids,
        )
        reason = None
        if parent.mode == "PREDICT" and (
            parent.target_contract != policy.target_contract or parent.policy_id != policy.policy_id
        ):
            reason = "INCOMPATIBLE_INCUMBENT"
        elif (
            len(training_rows) < policy.min_training
            or len(validation_rows) < policy.min_validation
            or len({r["session"] for r in training_rows}) < policy.min_training_sessions
            or len(validation_days) < policy.min_validation_sessions
        ):
            reason = "INSUFFICIENT_EPISODES_OR_SESSIONS"
        attempts = table("core_learning_attempts")
        revision_digest = content_id({"training": training_ids, "validation": validation_ids})
        completed_session = max(days).isoformat() if days else cutoff.date().isoformat()
        existing = (
            session.execute(
                select(attempts).where(
                    (attempts.c.session == completed_session)
                    | (attempts.c.input_revision_digest == revision_digest)
                )
            )
            .mappings()
            .first()
        )
        if not reason and existing:
            reason = (
                "PRIOR_ATTEMPT_UNFINISHED"
                if existing["run_id"] == run_id
                else "SESSION_BUDGET_OR_INPUT_REUSED"
            )
        if reason:
            # A contender/restart must not preempt the original run's final receipt.
            if reason == "PRIOR_ATTEMPT_UNFINISHED":
                fields["run_id"] = content_id({"interrupted": run_id})
                fields["trial_charge_id"] = existing["search_id"] if existing else None
            receipt = LearningReceipt(
                **fields,
                status="FIT_FAILED" if reason == "PRIOR_ATTEMPT_UNFINISHED" else "NO_CHANGE",
                reason=reason,
                validation_absent_reason=reason,
            )
            if reason == "PRIOR_ATTEMPT_UNFINISHED":
                put(session, "core_learning_runs", receipt.id, receipt)
                return receipt
            return _save_receipt(session, receipt)
        charge = "learner:paper-core-" + run_id
        fields["trial_charge_id"] = charge
        # Full train+validation inspected range is permanently spent canonical data.
        session.execute(
            search_runs.insert().values(
                search_id=charge,
                actor="paper-core-shadow",
                actor_version="owned-executed-net-return-v1",
                candidates_evaluated=1,
                dataset=policy.dataset,
                start_date=training_rows[0]["start"].date().isoformat(),
                end_date=validation_rows[-1]["end"].date().isoformat(),
                data_role=DataRole.VALIDATION.value,
                started_at=encode_utc(cutoff),
                finished_at=encode_utc(cutoff),
                configuration_hash=policy.id,
                detail_json=canonical_result_json(
                    {
                        "phase": "attempt_committed_before_fit",
                        "run_id": run_id,
                        "split_digest": split,
                    }
                ),
            )
        )
        session.execute(
            attempts.insert().values(
                session=completed_session,
                run_id=run_id,
                search_id=charge,
                input_id=split,
                input_revision_digest=revision_digest,
                reservation_payload=canonical_result_json(fields),
            )
        )
    # Charge is durable even if materialization, export, fit or validation crashes.
    candidate = None
    metrics: dict[str, float] = {}
    sessions: tuple[SessionMetrics, ...] = ()
    try:
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Recheck canonical holds at the actual data boundary, under the writer fence.
            for row in (*training_rows, *validation_rows):
                if _guard(
                    session,
                    policy.dataset,
                    row["start"].date(),
                    row["end"].date(),
                    now=cutoff.date(),
                ):
                    raise ValueError("HOLDOUT_CHANGED_BEFORE_MATERIALIZATION")
            training = _materialize(session, training_rows)
            validation = _materialize(session, validation_rows)
            export = {
                "training": training,
                "validation": validation,
                "parent": parent,
                "policy": policy,
                "run_id": run_id,
            }
            input_id = content_id(export)
            put(session, "core_learning_inputs", input_id, export)
        fields["input_export_id"] = input_id
        candidate = _fit_subprocess(export, policy, scratch_root=scratch_root)
        if (
            candidate.parent_model_id != parent.id
            or candidate.input_ids != training_ids
            or candidate.target_contract != policy.target_contract
            or candidate.policy_id != policy.policy_id
            or candidate.learning_receipt_id != run_id
        ):
            raise ValueError("FIT_RESULT_CONTRACT_MISMATCH")
        metrics, sessions = _validation(candidate, parent, validation)
        acceptable = _passes_validation(metrics, policy.min_improvement)
        status = "AWAITING_ACTIVATION" if acceptable else "CANDIDATE_REJECTED"
        reason = "PASSED_FIXED_SHADOW_GATES" if acceptable else "VALIDATION_GATE_FAILED"
        fields["candidate_model_id"] = candidate.id
        if acceptable:
            fields["resulting_model_id"] = candidate.id
    except Exception as error:
        status, reason = (
            "FIT_FAILED",
            str(error) if isinstance(error, ValueError) else type(error).__name__,
        )
        fields["validation_absent_reason"] = "FIT_OR_INPUT_FAILED_BEFORE_VALIDATION"
    receipt = LearningReceipt(
        **fields,
        status=status,
        reason=reason,
        metrics=tuple(sorted(metrics.items())),
        session_metrics=sessions,
    )
    with store.database.transaction() as session:
        if candidate is not None:
            put(session, "core_models", candidate.id, candidate)
        return _save_receipt(session, receipt)


def _main() -> None:
    # Child accepts exactly an immutable file and scratch result path, no database handle.
    # The child's own deadline survives parent loss. This is a program watchdog,
    # not an OS scheduling/isolated-sandbox guarantee.
    watchdog = threading.Timer(30, lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    try:
        _child_fit()
    finally:
        watchdog.cancel()


def _child_fit() -> None:
    source, target = (Path(p) for p in sys.argv[1:])
    with SingleWriterLock(target.parent.parent / "worker.lock"):
        data = json.loads(source.read_text(encoding="utf-8"))
        permission = target.parent / "allowed"
        if not permission.exists() or permission.read_text(encoding="utf-8") != content_id(data):
            raise ValueError("CANCELED_OR_CONFLICTING_ATTEMPT")
        if target.parent.name != data["run_id"]:
            raise ValueError("RUN_SCRATCH_ID_MISMATCH")
        training = tuple(LearningEpisode.model_validate(e) for e in data["training"])
        parent, policy = (
            ModelArtifact.model_validate(data["parent"]),
            LearningPolicy.model_validate(data["policy"]),
        )
        first = fit_candidate(training, parent, policy, run_id=data["run_id"])
        second = fit_candidate(training, parent, policy, run_id=data["run_id"])
        if first.id != second.id:
            raise ValueError("NONREPRODUCIBLE_FIT")
        target.write_text(first.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    _main()
