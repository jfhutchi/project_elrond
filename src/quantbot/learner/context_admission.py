"""Predeclared context evaluations use the existing canonical registry and trial burden."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import SerializerFunctionWrapHandler, model_serializer, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from quantbot.learner.context_integration import (
    CONTEXT_EVENT_KINDS,
    ContextEvaluation,
    ContextInvalidationRecord,
    ContextPermissionRecord,
    evaluate_retained_context,
)
from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_research import experiment_trial_prefixes
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.forward import _REGISTRY, _registry_session
from quantbot.learner.models import LearnerModel
from quantbot.learner.service import guard_range
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import aware
from quantbot.research.intraday_accounting import (
    record_intraday_attempt,
    verified_intraday_attempt_ids,
)
from quantbot.research.manifest import content_id
from quantbot.research.registry import HypothesisRegistry


class ContextReadingScope(LearnerModel):
    """Explicit interpretation-only extension; the parent price manifest stays frozen."""

    contract: Literal["BROAD_NEWS_READING_SCOPE_V1"] = "BROAD_NEWS_READING_SCOPE_V1"
    asset_classes: tuple[Literal["EQUITY"], Literal["ETF"], Literal["CRYPTO"]] = (
        "EQUITY",
        "ETF",
        "CRYPTO",
    )
    source_policy_id: Digest
    target: Literal["INDEPENDENT_READING_POLARITY", "MODEL_TEACHER_AGREEMENT_V1"] = (
        "INDEPENDENT_READING_POLARITY"
    )
    grants_execution: Literal[False] = False


class ContextEvaluationPlan(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    manifest_hash: Digest
    source_start: datetime
    training_start: datetime
    split_at: datetime
    evaluation_end: datetime
    interpreter_version: Literal["news-lexical-negation-v1"] = "news-lexical-negation-v1"
    estimator: Literal["ridge-alpha1-seed0"] = "ridge-alpha1-seed0"
    target: Literal["INDEPENDENT_READING_POLARITY", "MODEL_TEACHER_AGREEMENT_V1"] = (
        "INDEPENDENT_READING_POLARITY"
    )
    accounting_dataset: Literal["us-equities-daily"] = "us-equities-daily"
    grants_activation: Literal[False] = False
    reading_scope: ContextReadingScope | None = None
    teacher_policy_id: Digest | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_plan(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        if "reading_scope" not in self.model_fields_set:
            payload.pop("reading_scope", None)
        if "teacher_policy_id" not in self.model_fields_set:
            payload.pop("teacher_policy_id", None)
        return payload

    @model_validator(mode="after")
    def predeclared(self) -> Self:
        if (self.target == "MODEL_TEACHER_AGREEMENT_V1") != (self.teacher_policy_id is not None):
            raise ValueError("EXACT_TEACHER_POLICY_REQUIRED")
        if self.reading_scope is not None and self.reading_scope.target != self.target:
            raise ValueError("READING_SCOPE_AND_EVALUATION_TARGET_MUST_AGREE")
        for at in (
            self.recorded_at,
            self.source_start,
            self.training_start,
            self.split_at,
            self.evaluation_end,
        ):
            aware(at)
        if not (
            self.recorded_at <= self.training_start < self.split_at < self.evaluation_end
            and self.source_start <= self.training_start
        ):
            raise ValueError("ordered predeclared context evaluation windows required")
        if self.reading_scope is not None and self.source_start < self.training_start - timedelta(
            hours=48
        ):
            raise ValueError("broad reading scope permits at most 48 hours of source lookback")
        return self

    @property
    def plan_id(self) -> str:
        return content_id(self)


class ContextPlanSupersession(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    previous_plan_id: Digest
    successor_plan_id: Digest
    manifest_hash: Digest
    reason: Literal["PROSPECTIVE_TEACHER_POLICY_ADOPTION"] = "PROSPECTIVE_TEACHER_POLICY_ADOPTION"
    grants_activation: Literal[False] = False


def load_context_plan(
    store: ExperimentStore, *, experiment_id: str, plan_id: str, at: datetime
) -> ContextEvaluationPlan:
    events = store.read_as_of(
        experiment_id,
        at,
        maximum_records=4000,
        kinds=("ContextEvaluationPlan", "ContextPlanSupersession"),
    )
    if any(
        e.kind == "ContextPlanSupersession"
        and e.decode(ContextPlanSupersession).previous_plan_id == plan_id
        for e in events
    ):
        raise ValueError("CONTEXT_PLAN_SUPERSEDED")
    all_plans = [
        e.decode(ContextEvaluationPlan) for e in events if e.kind == "ContextEvaluationPlan"
    ]
    plans = [
        e.decode(ContextEvaluationPlan)
        for e in events
        if e.kind == "ContextEvaluationPlan" and e.decode(ContextEvaluationPlan).plan_id == plan_id
    ]
    if len(plans) != 1 or plans[0].manifest_hash != store.state(experiment_id).manifest_hash:
        raise ValueError("EXACT_REGISTERED_CONTEXT_PLAN_REQUIRED")
    if len(all_plans) > 1 and not any(
        e.kind == "ContextPlanSupersession"
        and e.decode(ContextPlanSupersession).successor_plan_id == plan_id
        for e in events
    ):
        raise ValueError("EXACT_CONTEXT_PLAN_ADOPTION_REQUIRED")
    return plans[0]


def adopt_context_plan(
    store: ExperimentStore,
    plan: ContextEvaluationPlan,
    *,
    supersedes_plan_id: str,
    registry_path: Path,
    declaration_path: Path,
    now: datetime,
) -> bool:
    now = aware(now)
    plan = ContextEvaluationPlan.model_validate(plan.model_dump())
    if not plan.recorded_at <= now < plan.training_start:
        raise ValueError("PLAN_ADOPTION_REQUIRES_FUTURE_TRAINING")
    old = load_context_plan(
        store, experiment_id=plan.experiment_id, plan_id=supersedes_plan_id, at=now
    )
    if (
        not now < old.training_start
        or plan.manifest_hash != old.manifest_hash
        or (plan.source_start, plan.training_start, plan.split_at, plan.evaluation_end)
        != (old.source_start, old.training_start, old.split_at, old.evaluation_end)
    ):
        raise ValueError("FUTURE_UNCHANGED_WINDOW_AND_PARENT_REQUIRED")
    if plan.teacher_policy_id is None or plan.plan_id == old.plan_id:
        raise ValueError("EXPLICIT_NEW_TEACHER_POLICY_REQUIRED")
    events = store.read_as_of(
        plan.experiment_id,
        now,
        maximum_records=4000,
        kinds=("ContextEvaluation", "ContextFitReservation"),
    )
    if events:
        raise ValueError("EVALUATED_CONTEXT_PLAN_CANNOT_BE_SUPERSEDED")
    for path in {registry_path, declaration_path}:
        with _session(path) as session:
            guard_range(
                session, "us-equities-daily", plan.source_start.date(), plan.evaluation_end.date()
            )
            if any(
                identity.startswith("intraday:context-" + content_id(plan.experiment_id)[:16] + ":")
                for identity in verified_intraday_attempt_ids(session)
            ):
                raise ValueError("CHARGED_CONTEXT_PLAN_CANNOT_BE_SUPERSEDED")
    supersession = ContextPlanSupersession(
        experiment_id=plan.experiment_id,
        recorded_at=now,
        previous_plan_id=old.plan_id,
        successor_plan_id=plan.plan_id,
        manifest_hash=plan.manifest_hash,
    )
    # Both records become visible atomically; no unattached successor can be selected.
    with store._transaction() as db:
        if db.execute(
            "SELECT 1 FROM events WHERE experiment_id=? AND "
            "(kind IN ('ContextEvaluation','ContextFitReservation') OR idempotency_key=?) LIMIT 1",
            (plan.experiment_id, "context-plan-supersession:" + old.plan_id),
        ).fetchone():
            raise ValueError("CONTEXT_PLAN_ADOPTION_STATE_CHANGED")
        for record, key in (
            (plan, "context-evaluation-plan:" + plan.plan_id),
            (supersession, "context-plan-supersession:" + old.plan_id),
        ):
            document = json.dumps(
                record.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?)",
                (
                    plan.experiment_id,
                    key,
                    type(record).__name__,
                    record.recorded_at.isoformat(timespec="microseconds"),
                    document,
                    hashlib.sha256(document.encode()).hexdigest(),
                ),
            )
    return True


def register_context_plan(
    store: ExperimentStore,
    plan: ContextEvaluationPlan,
    *,
    now: datetime,
) -> bool:
    plan = ContextEvaluationPlan.model_validate(plan.model_dump())
    now = aware(now)
    if not plan.recorded_at <= now <= plan.training_start:
        raise ValueError("CONTEXT_PLAN_MUST_PRECEDE_TRAINING")
    events = store.read_as_of(
        plan.experiment_id,
        now,
        maximum_records=4000,
        kinds=("ExperimentManifest", "ContextEvaluationPlan"),
    )
    manifests = [
        e.decode(ExperimentManifest) for e in events if e.kind == ExperimentManifest.__name__
    ]
    if (
        len(manifests) != 1
        or content_id(manifests[0]) != plan.manifest_hash
        or store.state(plan.experiment_id).manifest_hash != plan.manifest_hash
        or manifests[0].registered_at > plan.recorded_at
    ):
        raise ValueError("EXACT_REGISTERED_EXPERIMENT_MANIFEST_REQUIRED")
    for event in events:
        if event.kind == ContextEvaluationPlan.__name__:
            if event.decode(ContextEvaluationPlan) == plan:
                return False
            raise ValueError("CONTEXT_PLAN_ALREADY_REGISTERED")
    return store.append(plan, idempotency_key="context-evaluation-plan")


@contextmanager
def _session(path: Path, *, write: bool = False) -> Iterator[Session]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError("EXISTING_ABSOLUTE_CANONICAL_REGISTRY_REQUIRED")
    token = _REGISTRY.set(path)
    try:
        with _registry_session(write=write) as session:
            yield session
    finally:
        _REGISTRY.reset(token)


class CanonicalContextAdmission:
    """Both admission protocols, with permanent atomic accounting before the engine."""

    def __init__(
        self,
        plan: ContextEvaluationPlan,
        *,
        registry_path: Path,
        declaration_path: Path,
        now: datetime,
    ) -> None:
        self.plan = ContextEvaluationPlan.model_validate(plan.model_dump())
        self.registry_path, self.declaration_path = registry_path, declaration_path
        self.now = aware(now)

    def permits(self, start: datetime, end: datetime) -> bool:
        start, end = aware(start), aware(end)
        if not self.plan.source_start <= start <= end <= self.plan.evaluation_end <= self.now:
            return False
        try:
            for path in {self.registry_path, self.declaration_path}:
                with _session(path) as session:
                    if HypothesisRegistry(session).blocking_protected_windows(
                        self.plan.accounting_dataset,
                        start.date(),
                        end.date(),
                        now=self.now.date(),
                        include_forward_paper=True,
                    ):
                        return False
                    guard_range(session, self.plan.accounting_dataset, start.date(), end.date())
        except ValueError:
            return False
        return True

    def used_calls(self, experiment_id: str) -> int:
        if experiment_id != self.plan.experiment_id:
            raise ValueError("EXACT_TRIAL_EXPERIMENT_REQUIRED")
        with _session(self.registry_path) as session:
            return sum(
                identity.startswith(experiment_trial_prefixes(experiment_id))
                for identity in verified_intraday_attempt_ids(session)
            )

    def charge(self, trial_id: str, manifest_hash: str) -> None:
        prefix = "intraday:context-" + content_id(self.plan.experiment_id)[:16] + ":"
        if (
            manifest_hash != self.plan.manifest_hash
            or not trial_id.startswith(prefix)
            or not re.fullmatch("[a-f0-9]{64}", trial_id.removeprefix(prefix))
        ):
            raise ValueError("EXACT_CONTEXT_TRIAL_INPUTS_REQUIRED")
        if not self.permits(self.plan.source_start, self.plan.evaluation_end):
            raise ValueError("PROTECTED_OR_UNREGISTERED_CONTEXT_HISTORY")
        with _session(self.registry_path, write=True) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            # Recheck canonical protection under the same write reservation as the charge.
            guard_range(
                session,
                self.plan.accounting_dataset,
                self.plan.source_start.date(),
                self.plan.evaluation_end.date(),
            )
            used = verified_intraday_attempt_ids(session)
            if trial_id in used:
                raise ValueError("CANONICAL_TRIAL_ALREADY_RESERVED")
            if (
                sum(
                    identity.startswith(experiment_trial_prefixes(self.plan.experiment_id))
                    for identity in used
                )
                >= 6
            ):
                raise ValueError("CANONICAL_SIX_CALL_FIT_BUDGET_EXHAUSTED")
            # One frozen context fold per registered experiment; changing evidence is not
            # permission to inspect the same holdout again after a failed/local-lost run.
            if any(identity.startswith(prefix) for identity in used):
                raise ValueError("PREDECLARED_CONTEXT_FOLD_ALREADY_SPENT")
            record_intraday_attempt(
                session,
                search_id=trial_id,
                configuration_hash=content_id(dict(plan_id=self.plan.plan_id, trial_id=trial_id)),
                start=self.plan.source_start.date(),
                end=self.plan.evaluation_end.date(),
                now=self.now,
            )


def run_registered_context_evaluation(
    store: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    registry_path: Path,
    declaration_path: Path,
    now: datetime | None = None,
    plan_id: str | None = None,
) -> ContextEvaluation:
    """Operational learner API; no fake admission, creation, activation or network calls."""
    now = aware(now or datetime.now(UTC))
    events = store.read_as_of(experiment_id, now, maximum_records=4000, kinds=CONTEXT_EVENT_KINDS)
    plans = [
        e.decode(ContextEvaluationPlan) for e in events if e.kind == ContextEvaluationPlan.__name__
    ]
    if plan_id is None and len(plans) != 1:
        raise ValueError("ONE_PREDECLARED_CONTEXT_PLAN_REQUIRED")
    plan = load_context_plan(
        store, experiment_id=experiment_id, plan_id=plan_id or plans[0].plan_id, at=now
    )
    if plan.manifest_hash != store.state(experiment_id).manifest_hash:
        raise ValueError("EXACT_REGISTERED_EXPERIMENT_MANIFEST_REQUIRED")
    if now < plan.evaluation_end:
        raise ValueError("PREDECLARED_EVALUATION_CUTOFF_NOT_REACHED")
    if any(
        e.kind == ContextInvalidationRecord.__name__ and e.recorded_at > plan.evaluation_end
        for e in events
    ):
        raise ValueError("CONTEXT_EVIDENCE_INVALIDATED_AFTER_FROZEN_CUTOFF")
    for event in events:
        if (
            event.kind == ContextPermissionRecord.__name__
            and event.recorded_at > plan.evaluation_end
        ):
            permission = event.decode(ContextPermissionRecord).permission
            if permission.training != "VERIFIED" or "DENIED" in (
                permission.access,
                permission.metadata_retention,
                permission.retention,
                permission.processing,
            ):
                raise ValueError("CONTEXT_PERMISSION_CHANGED_AFTER_FROZEN_CUTOFF")
    admission = CanonicalContextAdmission(
        plan, registry_path=registry_path, declaration_path=declaration_path, now=now
    )
    if not admission.permits(plan.source_start, plan.evaluation_end):
        raise ValueError("PROTECTED_OR_UNREGISTERED_CONTEXT_HISTORY")
    return evaluate_retained_context(
        store,
        research,
        experiment_id=experiment_id,
        split_at=plan.split_at,
        as_of=plan.evaluation_end,
        guard=admission,
        charger=admission,
        evaluation_plan_id=plan.plan_id,
        training_start=plan.training_start,
        recorded_at=now,
        teacher_policy_id=plan.teacher_policy_id,
    )
