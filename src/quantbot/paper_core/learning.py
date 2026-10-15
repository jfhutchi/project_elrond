"""Bounded shadow learner over guarded owned outcomes; no activation or broker interface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, text, union_all
from sqlalchemy.orm import Session

from quantbot.paper_core.contracts import (
    Decision,
    LearningCalendar,
    LearningEpisode,
    LearningFold,
    LearningPolicy,
    LearningReceipt,
    ModelArtifact,
    Observation,
    ProcedureResult,
    RoundTrip,
    SessionMetrics,
)
from quantbot.paper_core.learning_procedure import (
    _passes_validation,
    _validation,
    run_procedure,
)
from quantbot.paper_core.learning_procedure import (
    fit_candidate as fit_candidate,
)
from quantbot.paper_core.learning_procedure import (
    predict_bps as predict_bps,
)
from quantbot.paper_core.learning_procedure import (
    validate_candidate as validate_candidate,
)
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.paper_core.time_evidence import Clocks
from quantbot.research.datasets import observation_identity
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.lock import AlreadyLockedError, SingleWriterLock
from quantbot.storage.schema import incidents, learner_datasets, search_runs


def _quarantined_lots(session: Session) -> set[str]:
    """Read incident scope only; event evidence and labels remain untouched."""
    values = session.execute(
        select(func.json_extract(incidents.c.detail_json, "$.lot_ids")).where(
            incidents.c.kind == "CORE_EVIDENCE_CONFLICT", incidents.c.resolved_at.is_(None)
        )
    ).scalars()
    result: set[str] = set()
    for value in values:
        if value is None:
            continue
        lots = json.loads(str(value))
        if not isinstance(lots, list) or any(not isinstance(lot, str) for lot in lots):
            raise ValueError("MALFORMED_INCIDENT_LOT_METADATA")
        result.update(lots)
    return result


def _guard(session: Session, dataset: str, start: date, end: date, *, now: date) -> str | None:
    from quantbot.paper_core.operational_costs import pending_cost_revision_source

    account_id = session.execute(
        select(func.json_extract(table("core_identities").c.payload, "$.account_id"))
    ).scalar_one_or_none()
    if (
        account_id is not None
        and session.execute(
            select(table("core_source_cursors").c.source_id).where(
                table("core_source_cursors").c.source_id
                == pending_cost_revision_source(str(account_id))
            )
        ).first()
    ):
        return "COST_CONTRACT_CONFLICT_REVISIONS_PENDING"
    snapshots = session.execute(
        select(table("core_canonical_sources").c.payload).join(
            table("core_canonical_current"),
            table("core_canonical_current").c.source_id == table("core_canonical_sources").c.id,
        )
    ).scalars()
    for encoded in snapshots:
        source = json.loads(encoded)
        identities = {observation_identity(d) for d in source["canonical_datasets"]}
        # The executed-outcome cohort shares the input bars' canonical protection burden.
        for hold in source["holdouts"]:
            if (
                observation_identity(hold["dataset"]) in identities
                and start <= date.fromisoformat(hold["end_date"])
                and date.fromisoformat(hold["start_date"]) <= end
            ):
                return "DECLARED_IMPORTED_CANONICAL_HOLDOUT"
        for window in source["windows"]:
            if (
                observation_identity(window["dataset"]) in identities
                and window["role"] in {"PROTECTED_EVALUATION", "FORWARD_PAPER"}
                and start <= date.fromisoformat(window["end_date"])
                and date.fromisoformat(window["start_date"]) <= end
                and (
                    window["state"] == "CONSUMED"
                    or window["state"] == "RESERVED"
                    and (
                        window["reserved_until"] is None
                        or date.fromisoformat(window["reserved_until"]) >= now
                    )
                )
            ):
                return "CANONICAL_IMPORTED_PROTECTED_OR_FORWARD_WINDOW"
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


def _revision_metadata(session: Session, cutoff: datetime) -> list[dict[str, Any]]:
    """As-of revisions using SQL metadata only; never hydrate labels before holds."""
    revisions, decisions, observations = (
        table(n) for n in ("core_outcome_revisions", "core_decisions", "core_observations")
    )
    lot = func.json_extract(revisions.c.payload, "$.lot_id")
    join = revisions.join(decisions, decisions.c.id == lot).join(
        observations,
        observations.c.id == func.json_extract(decisions.c.payload, "$.observation_id"),
    )
    columns = [
        func.json_extract(revisions.c.payload, "$." + n).label(n)
        for n in (
            "lot_id",
            "available_at",
            "source_start",
            "source_end",
            "dataset",
            "policy_id",
            "evidence_lane",
            "status",
            "revision",
        )
    ]
    metadata = [
        dict(r)
        for r in session.execute(
            select(
                revisions.c.id.label("outcome_id"),
                *columns,
                observations.c.id.label("observation_id"),
                func.json_extract(observations.c.payload, "$.feature_version").label(
                    "feature_version"
                ),
                func.json_extract(observations.c.payload, "$.session_close").label("session_close"),
            ).select_from(join)
        ).mappings()
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in metadata:
        grouped.setdefault(str(row["lot_id"]), []).append(row)
    result = []
    for history in grouped.values():
        eligible = [
            r
            for r in history
            if r["available_at"] is not None and decode_utc(str(r["available_at"])) <= cutoff
        ]
        pool = eligible or history
        result.append(
            max(
                pool,
                key=lambda r: (
                    int(r["revision"] or 1),
                    str(r["available_at"]),
                    str(r["outcome_id"]),
                ),
            )
        )
    return result


def _select(
    session: Session, policy: LearningPolicy, cutoff: datetime, *, execution_now: datetime
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], tuple[str, ...]]:
    rows, excluded = [], []
    metadata = (
        _revision_metadata(session, cutoff) if policy.procedure_version else _metadata(session)
    )
    quarantined = _quarantined_lots(session)
    work = table("core_outcome_work")
    from quantbot.paper_core.work import NOTIFICATIONS

    index, notifications = (
        table("core_outcome_index"),
        table("core_document_index").alias("notifications"),
    )
    # Dirty existing work is pending regardless of origin. Missing runtime coverage
    # applies only to owned ENTRY lots that bounded outcome recovery can reconcile;
    # archived immutable learning corpora have no runtime notification journal.
    owned = select(table("core_intent_links").c.lot_id).where(
        table("core_intent_links").c.role == "ENTRY"
    )
    missing = (
        select(index.c.lot_id)
        .outerjoin(work, work.c.lot_id == index.c.lot_id)
        .outerjoin(
            notifications,
            and_(notifications.c.kind == NOTIFICATIONS, notifications.c.id == index.c.lot_id),
        )
        .where(
            index.c.lot_id.in_(owned),
            or_(work.c.version.is_(None), notifications.c.id.is_(None)),
        )
    )
    pending = set(
        session.execute(
            union_all(
                select(work.c.lot_id).where(work.c.version > work.c.processed_version),
                missing,
            )
        ).scalars()
    )
    considered = tuple(sorted(str(r["outcome_id"]) for r in metadata))
    for row in metadata:
        reason = None
        if row["lot_id"] in quarantined:
            reason = "LOT_EVIDENCE_QUARANTINED"
        elif row["lot_id"] in pending:
            reason = "OUTCOME_RECONCILIATION_PENDING"
        elif (
            row["dataset"] != policy.dataset
            or row["policy_id"] != policy.policy_id
            or row["evidence_lane"] != policy.evidence_lane
            or row["feature_version"] != policy.feature_version
        ):
            reason = "INCOMPATIBLE_CONTRACT_OR_LANE"
        if reason:
            excluded.append((str(row["outcome_id"]), reason))
            continue
        try:
            start, end, available = (
                decode_utc(str(row[k])) for k in ("source_start", "source_end", "available_at")
            )
            session_close = decode_utc(str(row["session_close"]))
        except (ValueError, TypeError):
            excluded.append((str(row["outcome_id"]), "NONCAUSAL_METADATA"))
            continue
        if start >= end or available < end:
            reason = "NONCAUSAL_METADATA"
        elif available > cutoff or end > cutoff:
            reason = "UNAVAILABLE_AT_CUTOFF"
        elif available > execution_now or end > execution_now:
            reason = "UNAVAILABLE_AT_EXECUTION"
        elif row["status"] != "COMPLETE":
            reason = "COSTS_PENDING"
        elif session_close > min(cutoff, execution_now):
            reason = "SESSION_NOT_COMPLETED"
        else:
            reason = _guard(
                session, policy.dataset, start.date(), end.date(), now=execution_now.date()
            )
        row.update(
            start=start, end=end, session=end.astimezone(ZoneInfo("America/New_York")).date()
        )
        if reason:
            excluded.append((str(row["outcome_id"]), reason))
        else:
            rows.append(row)
    days = sorted({r["session"] for r in rows})
    if policy.max_sessions is not None:
        days = days[-policy.max_sessions :]
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


def _historical_comparator(
    session: Session, row: dict[str, Any], domain: str
) -> tuple[
    ModelArtifact, datetime | None, str | None, str | None, datetime | None, str | None, str | None
]:
    """Resolve the exact contemporaneous model, with completion proof for PREDICT."""
    decision = Decision.model_validate_json(
        session.execute(
            select(table("core_decisions").c.payload).where(
                table("core_decisions").c.id == row["lot_id"]
            )
        ).scalar_one()
    )
    observation = Observation.model_validate_json(
        session.execute(
            select(table("core_observations").c.payload).where(
                table("core_observations").c.id == row["observation_id"]
            )
        ).scalar_one()
    )
    if (
        observation.id != decision.observation_id
        or observation.available_at > decision.recorded_at_utc
    ):
        raise ValueError("HISTORICAL_OBSERVATION_UNAVAILABLE_AT_DECISION")
    collect = ModelArtifact(feature_version=observation.feature_version)
    if (
        decision.model_id == collect.id
        and decision.learning_receipt_id == collect.learning_receipt_id
    ):
        return collect, None, None, None, None, None, None
    payload = session.execute(
        select(table("core_models").c.payload).where(table("core_models").c.id == decision.model_id)
    ).scalar_one_or_none()
    if payload is None:
        raise ValueError("MISSING_HISTORICAL_MODEL_PROVENANCE")
    model = ModelArtifact.model_validate_json(payload)
    if (
        model.id != decision.model_id
        or model.estimator is None
        or model.feature_version != observation.feature_version
        or model.policy_id != decision.policy_id
    ):
        raise ValueError("INVALID_HISTORICAL_PREDICTIVE_MODEL")

    def resolve(identity: str) -> LearningReceipt:
        pointer = table("core_learning_considerations")
        receipt_id = (
            session.execute(
                select(pointer.c.receipt_id).where(pointer.c.run_id == identity)
            ).scalar_one_or_none()
            or identity
        )
        document = session.execute(
            select(table("core_learning_runs").c.payload).where(
                table("core_learning_runs").c.id == receipt_id
            )
        ).scalar_one_or_none()
        if document is None:
            raise ValueError("MISSING_HISTORICAL_RECEIPT_PROVENANCE")
        receipt = LearningReceipt.model_validate_json(document)
        if receipt.id != receipt_id:
            raise ValueError("HISTORICAL_RECEIPT_ID_MISMATCH")
        return receipt

    receipt = resolve(model.learning_receipt_id)
    if (
        receipt.available_at is None
        or receipt.available_at > decision.recorded_at_utc
        or receipt.execution_clock_domain != domain
        or receipt.resulting_model_id != model.id
        or receipt.status != "AWAITING_ACTIVATION"
        or receipt.policy.procedure_version is None
    ):
        raise ValueError("HISTORICAL_MODEL_UNAVAILABLE_AT_DECISION")
    ack = resolve(decision.learning_receipt_id)
    if ack.id != receipt.id and (
        ack.status != "NO_CHANGE"
        or ack.parent_model_id != model.id
        or ack.resulting_model_id != model.id
    ):
        raise ValueError("INVALID_HISTORICAL_ACKNOWLEDGEMENT")
    if (
        ack.available_at is None
        or ack.available_at > decision.recorded_at_utc
        or ack.execution_clock_domain != domain
    ):
        raise ValueError("HISTORICAL_ACKNOWLEDGEMENT_UNAVAILABLE_AT_DECISION")
    return (
        model,
        receipt.available_at,
        receipt.id,
        ack.id,
        ack.available_at,
        receipt.model_dump_json(),
        ack.model_dump_json(),
    )


def _fit_subprocess(
    export: dict[str, Any], policy: LearningPolicy, *, scratch_root: Path
) -> ModelArtifact | ProcedureResult:
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
        candidate = (
            ProcedureResult if policy.procedure_version else ModelArtifact
        ).model_validate_json(result.read_text(encoding="utf-8"))
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


def _execution_now(clock: Clocks) -> datetime:
    return decode_utc(encode_utc(clock.sample()[0]))


def _complete(fields: dict[str, Any], clock: Clocks, domain: str) -> bool:
    """Stamp actual completion, never infer result availability from input cutoff."""
    now = _execution_now(clock)
    fields.setdefault("started_at", now)
    original_domain = fields.get("execution_clock_domain")
    fields.update(
        recorded_at_utc=now,
        completed_at=now,
        completion_clock_domain=domain,
    )
    fields.setdefault("execution_clock_domain", domain)
    fields.setdefault("fit_started_at", None)
    # Old reservations lack execution/start evidence. Recovery starts a new
    # completion event rather than adopting their cutoff as an actual start.
    if fields["started_at"] is None:
        fields["started_at"] = now
    started = fields["started_at"]
    if not isinstance(started, datetime):
        started = decode_utc(str(started))
    fit_start = fields["fit_started_at"]
    if fit_start is not None and not isinstance(fit_start, datetime):
        fit_start = decode_utc(str(fit_start))
    valid = (
        original_domain in (None, domain)
        and now >= started
        and (fit_start is None or started <= fit_start <= now)
    )
    fields["available_at"] = now if valid else None
    return valid


def consider_learning(
    store: CoreStore,
    policy: LearningPolicy,
    *,
    cutoff: datetime,
    execution_clock: Clocks | None = None,
    calendar: LearningCalendar | None = None,
    legacy_fixture_replay: bool = False,
) -> LearningReceipt:
    """Serialize admission and recover crashed fits with fresh process-lifetime evidence."""
    if store.config.evidence_lane == "BROKER_PAPER":
        from quantbot.paper_core.canonical import refresh_canonical_metadata

        refresh_canonical_metadata(store)
    clock = execution_clock or store.clocks
    domain = getattr(clock, "execution_domain", "HOST_UTC")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("execution clock needs a declared domain")
    started_at = _execution_now(clock)
    cutoff = decode_utc(encode_utc(cutoff))
    if policy.procedure_version and cutoff > started_at:
        return _refusal_receipt(
            store,
            policy,
            cutoff,
            clock,
            domain,
            started_at,
            reason="FUTURE_CUTOFF_BEYOND_EXECUTION_TIME",
            calendar=calendar,
        )
    assert store.database.path is not None
    scratch_root = store.database.path.parent / (store.database.path.name + ".learning-scratch")
    admission = SingleWriterLock(scratch_root / "admission.lock")
    try:
        admission.acquire()
    except AlreadyLockedError:
        return _refusal_receipt(store, policy, cutoff, clock, domain, started_at)
    try:
        worker = SingleWriterLock(scratch_root / "worker.lock")
        try:
            worker.acquire()
        except AlreadyLockedError:
            return _refusal_receipt(store, policy, cutoff, clock, domain, started_at)
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
                    _complete(fields, clock, domain)
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
        return _consider_learning(
            store,
            policy,
            cutoff=cutoff,
            scratch_root=scratch_root,
            clock=clock,
            domain=domain,
            started_at=started_at,
            calendar=calendar,
            legacy_fixture_replay=legacy_fixture_replay,
        )
    finally:
        admission.release()


def _refusal_receipt(
    store: CoreStore,
    policy: LearningPolicy,
    cutoff: datetime,
    clock: Clocks,
    domain: str,
    started_at: datetime,
    *,
    reason: str = "LEARNING_WORKER_BUSY",
    calendar: LearningCalendar | None = None,
) -> LearningReceipt:
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _, exclusions, considered = _select(
            session, policy, cutoff, execution_now=_execution_now(clock)
        )
        identity: dict[str, Any] = {
            "busy": True,
            "policy": policy,
            "cutoff": cutoff,
            "inputs": considered,
        }
        if reason != "LEARNING_WORKER_BUSY":
            # Refusal must replay while future, without memoizing the later valid
            # consideration once fresh execution time reaches the same cutoff.
            identity.update(refusal_reason=reason, calendar=calendar.id if calendar else None)
        run_id = content_id(identity)
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
        fields: dict[str, Any] = {
            "started_at": started_at,
            "evidence_lane": policy.evidence_lane,
            "release_id": store.config.release_id,
        }
        if reason != "LEARNING_WORKER_BUSY" and calendar is not None:
            fields["calendar_id"] = calendar.id
        _complete(fields, clock, domain)
        receipt = LearningReceipt(
            **fields,
            cutoff=cutoff,
            run_id=run_id,
            policy=policy,
            parent_model_id=policy.parent_model.id,
            resulting_model_id=policy.parent_model.id,
            considered_ids=considered,
            exclusions=tuple(exclusions),
            split_digest=content_id({}),
            status="NO_CHANGE",
            reason=reason,
            validation_absent_reason=reason,
        )
        put(session, "core_models", policy.parent_model.id, policy.parent_model)
        return _save_receipt(session, receipt)


def _consider_learning(
    store: CoreStore,
    policy: LearningPolicy,
    *,
    cutoff: datetime,
    scratch_root: Path,
    clock: Clocks,
    domain: str,
    started_at: datetime,
    calendar: LearningCalendar | None = None,
    legacy_fixture_replay: bool = False,
) -> LearningReceipt:
    cutoff = decode_utc(encode_utc(cutoff))
    parent = policy.parent_model
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        # Fail closed on malformed historical accounting; never erase its burden.
        HypothesisRegistry(session).learner_trials_spent()
        rows, excluded, considered = _select(
            session, policy, cutoff, execution_now=_execution_now(clock)
        )
        run_id = content_id(
            {
                "cutoff": cutoff,
                "policy": policy,
                "considered": considered,
                "excluded": excluded,
                **(
                    {"calendar": calendar.id if calendar else None}
                    if policy.procedure_version
                    else {}
                ),
            }
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
        fold_rows: list[tuple[date, datetime, list[dict[str, Any]], list[dict[str, Any]]]] = []
        calendar_reason = None
        completed = (
            ()
            if calendar is None
            else tuple(
                s
                for s in calendar.sequence.sessions
                if s.close_at <= min(cutoff, _execution_now(clock))
            )
        )
        if policy.procedure_version:
            if calendar is None:
                calendar_reason = "MISSING_VERIFIED_SESSION_SEQUENCE"
            elif len(completed) < 10:
                calendar_reason = "TEN_COMPLETED_SESSION_WARMUP"
            else:
                by_day = {s.session_date: s for s in calendar.sequence.sessions}
                if any(
                    r["session"] not in by_day
                    or decode_utc(str(r["session_close"])) != by_day[r["session"]].close_at
                    for r in rows
                ):
                    calendar_reason = "INPUT_OUTSIDE_VERIFIED_SESSION_SEQUENCE"
                else:
                    for day in validation_days:
                        fold_cutoff = by_day[day].open_at
                        earlier, fold_excluded, _ = _select(
                            session, policy, fold_cutoff, execution_now=_execution_now(clock)
                        )
                        scored = [r for r in validation_rows if r["session"] == day]
                        boundary = min(r["start"] for r in scored) - timedelta(
                            minutes=policy.embargo_minutes
                        )
                        earlier = [
                            r for r in earlier if r["session"] < day and r["end"] <= boundary
                        ]
                        excluded.extend(
                            (identity, "FOLD:" + day.isoformat() + ":" + reason)
                            for identity, reason in fold_excluded
                            if reason not in ("UNAVAILABLE_AT_CUTOFF", "SESSION_NOT_COMPLETED")
                        )
                        fold_rows.append((day, fold_cutoff, earlier, scored))
                    if fold_rows:
                        training_rows = fold_rows[0][2]
        if validation_rows:
            boundary = validation_rows[0]["start"] - timedelta(minutes=policy.embargo_minutes)
            purged = [r for r in training_rows if r["end"] > boundary]
            training_rows = [r for r in training_rows if r["end"] <= boundary]
            excluded.extend((r["outcome_id"], "TRAIN_VALIDATION_EMBARGO") for r in purged)
        training_ids = tuple(r["outcome_id"] for r in training_rows)
        validation_ids = tuple(r["outcome_id"] for r in validation_rows)
        split = content_id({"training": training_ids, "validation": validation_ids})
        fields: dict[str, Any] = dict(
            recorded_at_utc=started_at,
            evidence_lane=policy.evidence_lane,
            release_id=store.config.release_id,
            started_at=started_at,
            execution_clock_domain=domain,
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
        if policy.procedure_version:
            fields.update(
                calendar_id=calendar.id if calendar else None,
                completed_session=completed[-1].session_date if completed else None,
                final_validation_absent_reason="FINAL_REFIT_HAS_NO_INDEPENDENT_HOLDOUT",
            )
        reason = calendar_reason or (
            "LEGACY_POLICY_REPLAY_ONLY"
            if policy.procedure_version is None and not legacy_fixture_replay
            else None
        )
        if (
            not reason
            and parent.mode == "PREDICT"
            and (
                parent.target_contract != policy.target_contract
                or parent.policy_id != policy.policy_id
            )
        ):
            reason = "INCOMPATIBLE_INCUMBENT"
        elif not reason and (
            len(training_rows) < policy.min_training
            or len(validation_rows) < policy.min_validation
            or len({r["session"] for r in training_rows}) < policy.min_training_sessions
            or len(validation_days) < policy.min_validation_sessions
        ):
            reason = "INSUFFICIENT_EPISODES_OR_SESSIONS"
        attempts = table("core_learning_attempts")
        revision_digest = (
            content_id(tuple(r["outcome_id"] for r in rows))
            if policy.procedure_version
            else content_id({"training": training_ids, "validation": validation_ids})
        )
        completed_session = (
            completed[-1].session_date.isoformat()
            if policy.procedure_version and completed
            else (max(days).isoformat() if days else cutoff.date().isoformat())
        )
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
            _complete(fields, clock, domain)
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
        charged_at = _execution_now(clock)
        # Full train+validation inspected range is permanently spent canonical data.
        session.execute(
            search_runs.insert().values(
                search_id=charge,
                actor="paper-core-shadow-trial-charge",
                actor_version="owned-executed-net-return-v1",
                candidates_evaluated=1,
                dataset=policy.dataset,
                start_date=min(r["start"] for r in rows).date().isoformat(),
                end_date=max(r["end"] for r in rows).date().isoformat(),
                data_role=DataRole.VALIDATION.value,
                started_at=encode_utc(charged_at),
                finished_at=encode_utc(charged_at),
                configuration_hash=policy.id,
                detail_json=canonical_result_json(
                    {
                        "phase": "attempt_committed_before_fit",
                        "event": "PERMANENT_TRIAL_CHARGE",
                        "execution_clock_domain": domain,
                        "fit_completed_at": None,
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
        if store.config.evidence_lane == "BROKER_PAPER":
            from quantbot.paper_core.canonical import refresh_canonical_metadata

            canonical_id = refresh_canonical_metadata(store)
            fields["source_refs"] = (*fields.get("source_refs", ()), canonical_id)
        with store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Recheck canonical holds at the actual data boundary, under the writer fence.
            execution_now = _execution_now(clock)
            quarantined = _quarantined_lots(session)
            materialization_rows = (
                [*rows, *(r for _, _, train, _ in fold_rows for r in train)]
                if policy.procedure_version
                else [*training_rows, *validation_rows]
            )
            for row in materialization_rows:
                if row["lot_id"] in quarantined:
                    raise ValueError("LOT_QUARANTINED_BEFORE_MATERIALIZATION")
                if _guard(
                    session,
                    policy.dataset,
                    row["start"].date(),
                    row["end"].date(),
                    now=execution_now.date(),
                ):
                    raise ValueError("HOLDOUT_CHANGED_BEFORE_MATERIALIZATION")
                if (
                    decode_utc(str(row["available_at"])) > execution_now
                    or row["end"] > execution_now
                ):
                    raise ValueError("INPUT_UNAVAILABLE_AT_MATERIALIZATION")
            training = _materialize(session, training_rows)
            validation = _materialize(session, validation_rows)
            export = {
                "training": training,
                "validation": validation,
                "parent": parent,
                "policy": policy,
                "run_id": run_id,
            }
            if policy.procedure_version:
                assert calendar is not None
                folds = []
                for day, fold_cutoff, train, scored in fold_rows:
                    historical = [_historical_comparator(session, r, domain) for r in scored]
                    folds.append(
                        LearningFold(
                            session=day,
                            cutoff=fold_cutoff,
                            training=_materialize(session, train),
                            validation=_materialize(session, scored),
                            historical_model_ids=tuple(h[0].id for h in historical),
                            historical_models=tuple(h[0] for h in historical),
                            historical_available_at=tuple(h[1] for h in historical),
                            historical_receipt_ids=tuple(h[2] for h in historical),
                            historical_acknowledgement_ids=tuple(h[3] for h in historical),
                            historical_acknowledgement_available_at=tuple(h[4] for h in historical),
                            historical_receipt_payloads=tuple(h[5] for h in historical),
                            historical_acknowledgement_payloads=tuple(h[6] for h in historical),
                        )
                    )
                export.update(
                    folds=tuple(folds),
                    final=_materialize(session, rows),
                    calendar=calendar,
                    cutoff=cutoff,
                )
            input_id = content_id(export)
            put(session, "core_learning_inputs", input_id, export)
        fields["input_export_id"] = input_id
        fields["fit_started_at"] = _execution_now(clock)
        fitted = _fit_subprocess(export, policy, scratch_root=scratch_root)
        if policy.procedure_version:
            if not isinstance(fitted, ProcedureResult):
                raise ValueError("PROCEDURE_RESULT_CONTRACT_MISMATCH")
            candidate = fitted.candidate
            metrics, sessions = dict(fitted.metrics), fitted.session_metrics
            acceptable = fitted.gates_passed
            fields.update(
                fold_results=fitted.folds,
                final_input_ids=tuple(r["outcome_id"] for r in rows) if acceptable else (),
                final_raw_weights=fitted.final_raw_weights,
            )
        else:
            if not isinstance(fitted, ModelArtifact):
                raise ValueError("FIT_RESULT_CONTRACT_MISMATCH")
            candidate = fitted
            metrics, sessions = _validation(candidate, parent, validation)
            acceptable = _passes_validation(metrics, policy.min_improvement)
        if candidate is not None and (
            candidate.parent_model_id != parent.id
            or candidate.input_ids
            != (tuple(r["outcome_id"] for r in rows) if policy.procedure_version else training_ids)
            or candidate.target_contract != policy.target_contract
            or candidate.policy_id != policy.policy_id
            or candidate.learning_receipt_id != run_id
        ):
            raise ValueError("FIT_RESULT_CONTRACT_MISMATCH")
        status = "AWAITING_ACTIVATION" if acceptable else "CANDIDATE_REJECTED"
        reason = (
            (
                "PASSED_FIXED_PROCEDURE_GATES"
                if policy.procedure_version
                else "PASSED_FIXED_SHADOW_GATES"
            )
            if acceptable
            else "VALIDATION_GATE_FAILED"
        )
        if candidate is not None:
            fields["candidate_model_id"] = candidate.id
        if acceptable:
            assert candidate is not None
            fields["resulting_model_id"] = candidate.id
    except Exception as error:
        status, reason = (
            "FIT_FAILED",
            str(error) if isinstance(error, ValueError) else type(error).__name__,
        )
        fields["validation_absent_reason"] = "FIT_OR_INPUT_FAILED_BEFORE_VALIDATION"
    if not _complete(fields, clock, domain):
        status, reason = "FIT_FAILED", "EXECUTION_CLOCK_REGRESSED"
        fields["resulting_model_id"] = parent.id
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
        if policy.procedure_version:
            calendar = LearningCalendar.model_validate(data["calendar"])
            folds = tuple(LearningFold.model_validate(f) for f in data["folds"])
            final = tuple(LearningEpisode.model_validate(e) for e in data["final"])
            cutoff = decode_utc(data["cutoff"])
            first_result = run_procedure(
                folds, final, parent, policy, calendar, cutoff, data["run_id"]
            )
            second_result = run_procedure(
                folds, final, parent, policy, calendar, cutoff, data["run_id"]
            )
            if content_id(first_result) != content_id(second_result):
                raise ValueError("NONREPRODUCIBLE_PROCEDURE")
            target.write_text(first_result.model_dump_json(), encoding="utf-8")
            return
        first = fit_candidate(training, parent, policy, run_id=data["run_id"])
        second = fit_candidate(training, parent, policy, run_id=data["run_id"])
        if first.id != second.id:
            raise ValueError("NONREPRODUCIBLE_FIT")
        target.write_text(first.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    _main()
