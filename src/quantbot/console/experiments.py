"""One read-only experiment panel shared by HTML and JSON; no execution objects."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from quantbot.learner.experiment_activation import active_artifact
from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    Digest,
    ExperimentManifest,
    WorkerReceipt,
)
from quantbot.learner.experiment_coordinator import ResolvedMarketLabel, TickReceipt, feature_key
from quantbot.learner.experiment_evaluation import EvaluationReport, evaluate_experiment
from quantbot.learner.experiment_lots import LotEvent
from quantbot.learner.experiment_news import NewsEvidence
from quantbot.learner.experiment_observation import ObservationBatch
from quantbot.learner.experiment_research import FitReceipt, FitRequest, FitStart
from quantbot.learner.experiment_store import ConsentNotice, ExperimentStore, StoredEvent
from quantbot.learner.models import LearnerModel, Text
from quantbot.operations.daily_performance import DailyPerformance


class TimelineEvent(LearnerModel):
    recorded_at: datetime
    kind: Text
    evidence_hash: Digest


class ExperimentSummary(LearnerModel):
    experiment_id: Text
    state: Text
    label: Text
    blocker_kind: Literal[
        "SERVICE_SETUP",
        "CLOCK",
        "NORMAL_WAIT",
        "COVERAGE",
        "EVIDENCE",
        "ARTIFACT_APPROVAL",
        "FAILURE",
        "NONE",
    ]
    owner: Text
    next_action: Text
    model_approval_required: bool
    consent_recorded: bool
    trading_authority: Literal["NONE", "EXACT_EXPERIMENT_PAPER_ONLY"]
    last_tick_at: datetime | None
    next_expected_event: str | None
    next_wakeup_at: datetime | None
    models: tuple[str, ...]
    charged_fits: int
    reserved_fits: int
    fit_budget: int
    observation_rows: int = 0
    resolved_market_labels: int = 0
    decisions_recorded: int = 0
    journaled_fill_receipts: int = 0
    synthetic: bool


class ExperimentDetail(LearnerModel):
    manifest: ExperimentManifest
    summary: ExperimentSummary
    evaluation: EvaluationReport
    timeline: tuple[TimelineEvent, ...]
    timeline_truncated: bool
    news: tuple[NewsEvidence, ...] = ()
    historical_fits: tuple[FitReceipt, ...] = ()
    fit_families: dict[str, str] = Field(default_factory=dict)
    synthetic: bool


class ExperimentPanel(LearnerModel):
    available: bool
    reason: str | None = None
    experiments: tuple[ExperimentSummary, ...] = ()
    detail: ExperimentDetail | None = None


def _worker_receipt(
    path: Path, manifest: ExperimentManifest, now: datetime
) -> WorkerReceipt | None:
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_size > 8192:
        raise ValueError("WORKER_RECEIPT_UNVERIFIED")
    worker = WorkerReceipt.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if worker.experiment_id != manifest.experiment_id:
        return None
    if worker.synthetic != manifest.synthetic or worker.recorded_at > now:
        raise ValueError("WORKER_RECEIPT_UNVERIFIED")
    return worker


def _summary(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    events: Sequence[StoredEvent],
    now: datetime,
    worker: WorkerReceipt | None = None,
) -> ExperimentSummary:
    state = store.state(manifest.experiment_id)
    ticks = [event.decode(TickReceipt) for event in events if event.kind == "TickReceipt"]
    tick = max(ticks, key=lambda row: row.recorded_at) if ticks else None
    completed_fit_requests = {
        receipt.request_id
        for event in events if event.kind == "FitReceipt"
        for receipt in (event.decode(FitReceipt),)
        if receipt.status == "SUCCEEDED"
    }
    consent = state.consent_recorded or any(
        event.kind == ConsentNotice.__name__ for event in events
    )
    approval = False
    kind = "NONE"
    owner, label, action = (
        "SCHEDULED_COORDINATOR",
        "Forward research accumulating",
        "Observe the next scheduled evidence receipt; PAPER only.",
    )
    activation = active_artifact(store, manifest, now=now)
    if state.state == "FAULTED" or tick is not None and tick.status == "FAULTED":
        kind, owner, label, action = (
            "FAILURE",
            "DEVELOPMENT_OPERATOR",
            "Experiment requires repair",
            "Inspect the authoritative failed receipt; retain position protection.",
        )
    elif tick is not None and (not tick.clock_verified or tick.status == "CLOCK_SYNC_REQUIRED"):
        kind, owner, label, action = (
            "CLOCK",
            "DEVELOPMENT_OPERATOR",
            "Clock verification required",
            "Restore and verify the configured clock source; new entries remain gated.",
        )
    elif tick is not None and now > tick.next_wakeup_at + timedelta(minutes=2):
        kind, owner, label, action = (
            "FAILURE",
            "DEVELOPMENT_OPERATOR",
            "Research coordinator receipt is stale",
            "Check the scheduled worker and its newest receipt; closed market does not stop ticks.",
        )
    elif worker is not None and (
        now > worker.recorded_at + timedelta(seconds=1020 if worker.status == "STARTED" else 240)
    ):
        kind, owner, label, action = (
            "FAILURE",
            "DEVELOPMENT_OPERATOR",
            "Research worker receipt is stale",
            "Inspect the independent worker; bounded fitting grace has expired. Retain protection.",
        )
    elif worker is not None and (
        worker.status
        in {
            "FAULTED",
            "FAILED",
            "TIMEOUT",
            "CHARGE_REFUSED",
            "STARTED_UNRESOLVED",
        }
        or worker.reason
        in {"FAILED_SUCCESSOR_GENERATION_REVIEW_REQUIRED", "STARTED_SUCCESSOR_REVIEW_REQUIRED"}
    ):
        kind, owner, label, action = (
            "FAILURE",
            "DEVELOPMENT_OPERATOR",
            "Research worker requires repair",
            "Inspect the bounded worker receipt and charged-fit ledger. Do not retry unknown fits.",
        )
    elif worker is not None and worker.reason == "PREDECLARED_RESEARCH_SOURCES_REQUIRED":
        kind, owner, label, action = (
            "SERVICE_SETUP",
            "DEVELOPMENT_OPERATOR",
            "Research input setup incomplete",
            "Configure immutable training and seen-validation archives with canonical guards.",
        )
    elif worker is not None and worker.reason == "CALENDAR_COVERAGE_REQUIRED":
        kind, owner, label, action = (
            "COVERAGE",
            "DEVELOPMENT_OPERATOR",
            "Evaluation calendar coverage incomplete",
            "Restore authoritative calendar coverage. New fits and entries remain gated.",
        )
    elif worker is not None and worker.status == "STARTED":
        kind, owner, label, action = (
            "NORMAL_WAIT",
            "BOUNDED_RESEARCH_WORKER",
            "Bounded research worker running",
            "Wait for the charged fit receipt within its 15-minute wall limit; "
            "no model approval is requested.",
        )
    elif state.state == "ACTIVATION_REQUIRED" and not any(
        event.kind == "ExactArtifactApproval" for event in events
    ):
        kind, owner, label, action, approval = (
            "ARTIFACT_APPROVAL",
            "USER",
            "Exact trading artifact approval required",
            "Review the trading strategy's pinned artifact, risk limits and rollback package.",
            True,
        )
    elif activation is None and consent:
        kind, owner, label, action = (
            "SERVICE_SETUP",
            "DEVELOPMENT_OPERATOR",
            "Paper trading setup incomplete",
            "Complete the recorded setup scope. Each new artifact needs exact activation.",
        )
    elif tick is None:
        kind, owner, label, action = (
            "SERVICE_SETUP",
            "DEVELOPMENT_OPERATOR",
            "Research coordinator not started",
            "Configure and start the pinned shadow coordinator; no model choice is requested.",
        )
    elif worker is None and any(
        event.decode(FitRequest).request_id not in completed_fit_requests
        for event in events if event.kind == FitRequest.__name__
    ):
        kind, owner, label, action = (
            "SERVICE_SETUP",
            "DEVELOPMENT_OPERATOR",
            "Research worker receipt unavailable",
            "Verify the bounded worker is running and producing completion receipts.",
        )
    elif tick.status == "NORMAL_WAIT":
        kind, label, action = "NORMAL_WAIT", "Scheduled waiting", tick.reason
    elif tick.status == "WAITING_FOR_COVERAGE":
        kind, owner, label, action = (
            "COVERAGE",
            "DEVELOPMENT_OPERATOR",
            "Research data coverage incomplete",
            "Restore supported source coverage; missing information remains unknown.",
        )
    elif state.state in {"CANDIDATE_SHADOW", "EVALUATING", "INSUFFICIENT_EVIDENCE", "COMPLETED"}:
        kind, label, action = (
            "EVIDENCE",
            "Shadow research evaluation",
            "Review fresh sample counts and outcomes; no automatic promotion or live switch.",
        )
    candidates = [
        event.decode(CandidateArtifact) for event in events if event.kind == "CandidateArtifact"
    ]
    return ExperimentSummary(
        experiment_id=manifest.experiment_id,
        state=state.state,
        label=label,
        blocker_kind=kind,
        owner=owner,
        next_action=action,
        model_approval_required=approval,
        consent_recorded=consent,
        trading_authority="EXACT_EXPERIMENT_PAPER_ONLY"
        if activation is not None and not activation.synthetic
        else "NONE",
        last_tick_at=tick.recorded_at if tick else None,
        next_expected_event=tick.next_expected_event if tick else None,
        next_wakeup_at=tick.next_wakeup_at if tick else None,
        models=tuple(sorted({row.model_id for row in candidates})),
        charged_fits=sum(
            event.decode(FitReceipt).charged is True
            for event in events
            if event.kind == "FitReceipt"
        ),
        reserved_fits=sum(event.kind == FitStart.__name__ for event in events),
        fit_budget=manifest.fit_budget,
        observation_rows=len(
            {
                feature_key(row)
                for event in events
                if event.kind == "ObservationBatch"
                for row in event.decode(ObservationBatch).features
            }
        ),
        resolved_market_labels=sum(event.kind == ResolvedMarketLabel.__name__ for event in events),
        decisions_recorded=sum(event.kind == "DecisionRecord" for event in events),
        journaled_fill_receipts=sum(
            event.decode(LotEvent).kind == "FILL" for event in events if event.kind == "LotEvent"
        ),
        synthetic=manifest.synthetic,
    )


def read_experiment_panel(
    store_path: Path | None,
    *,
    experiment_id: str | None = None,
    now: datetime,
    account_days: Sequence[DailyPerformance] = (),
) -> ExperimentPanel:
    if store_path is None:
        return ExperimentPanel(available=False, reason="EXPERIMENT_STORE_NOT_CONFIGURED")
    try:
        store = ExperimentStore(store_path, read_only=True)
        with store._transaction(write=False) as db:
            ids = tuple(
                row[0]
                for row in db.execute("SELECT experiment_id FROM states ORDER BY experiment_id")
            )
        if experiment_id is not None and experiment_id not in ids:
            return ExperimentPanel(available=False, reason="EXPERIMENT_NOT_FOUND")
        summaries, detail = [], None
        for identity in ids:
            events = store.read_as_of(identity, now)
            manifests = [
                event.decode(ExperimentManifest)
                for event in events
                if event.kind == "ExperimentManifest"
            ]
            if len(manifests) != 1:
                raise ValueError("AUTHORITATIVE_MANIFEST_REQUIRED")
            manifest = manifests[0]
            worker = _worker_receipt(store_path.parent / "worker-receipt.json", manifest, now)
            summary = _summary(store, manifest, events, now, worker)
            summaries.append(summary)
            if identity == experiment_id:
                detail = ExperimentDetail(
                    manifest=manifest,
                    summary=summary,
                    evaluation=evaluate_experiment(
                        store, manifest, now=now, account_days=account_days
                    ),
                    timeline=tuple(
                        TimelineEvent(
                            recorded_at=event.recorded_at,
                            kind=event.kind,
                            evidence_hash=event.record_id,
                        )
                        for event in events[-100:]
                    ),
                    timeline_truncated=len(events) > 100,
                    historical_fits=tuple(
                        event.decode(FitReceipt) for event in events if event.kind == "FitReceipt"
                    ),
                    fit_families={
                        candidate.model_id: candidate.artifact.family
                        for event in events if event.kind == "CandidateArtifact"
                        for candidate in (event.decode(CandidateArtifact),)
                    },
                    news=tuple(
                        event.decode(NewsEvidence)
                        for event in events
                        if event.kind == "NewsEvidence"
                    )[-50:],
                    synthetic=manifest.synthetic,
                )
        return ExperimentPanel(available=True, experiments=tuple(summaries), detail=detail)
    except (FileNotFoundError, OSError, ValueError, sqlite3.DatabaseError):
        return ExperimentPanel(available=False, reason="EXPERIMENT_STORE_UNAVAILABLE_OR_UNVERIFIED")
