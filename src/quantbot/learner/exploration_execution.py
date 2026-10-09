"""Outcome-first PAPER intent journal around the unchanged execution gateway.

Real use requires its own explicit policy commissioning, not fitted-model approval.
No consent creation, registration, promotion, orders or schema changes on import.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from quantbot.domain import OrderIntent
from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_execution import (
    ExperimentOutboxSubmitter,
    _paper_only,
    journal_entry_request,
)
from quantbot.learner.experiment_lots import LotEvent, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.exploration import ExplorationObservation, ExplorationPolicy
from quantbot.learner.exploration_feedback import journal_exploration_feedback, verified_feedback
from quantbot.learner.exploration_paper import ExplorationPaperRunner
from quantbot.learner.models import LearnerModel, Text
from quantbot.learner.paper import build_learner_paper_cycle
from quantbot.learner.paper_runner import IntradayPaperServices
from quantbot.operations.cycle import (
    CycleResult,
    CycleStrategyResult,
    DataSyncRunner,
    SubmissionRequest,
)
from quantbot.research.manifest import content_id

if TYPE_CHECKING:
    from quantbot.runtime import Runtime


class ExplorationCommission(LearnerModel):
    experiment_id: Text
    policy_id: Digest
    manifest_hash: Digest
    binding_hash: Digest
    code_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    recorded_at: datetime
    actor: Literal["jfhutchi"] = "jfhutchi"
    source_authorization_ref: Text
    authority: Literal["EXPLICIT_OPERATOR_OUTCOME_FIRST_PAPER_EXPLORATION"] = (
        "EXPLICIT_OPERATOR_OUTCOME_FIRST_PAPER_EXPLORATION"
    )
    mode: Literal["PAPER"] = "PAPER"
    predictive_model_approved: Literal[False] = False
    legacy_exit_transfer: Literal[False] = False
    synthetic: Literal[False] = False

    @property
    def commission_id(self) -> str:
        return content_id(self)


class ExplorationEntryRefusal(LearnerModel):
    """Diagnostic evidence only; cannot grant or alter any entry authority."""
    experiment_id: Text
    policy_id: Digest
    binding_hash: Digest
    run_id: Text
    recorded_at: datetime
    reasons: tuple[Text, ...] = Field(min_length=1)
    grants_execution: Literal[False] = False
    synthetic: bool


def prior_episode_objections(
    store: ExperimentStore, policy: ExplorationPolicy, *, cohort_at: datetime, now: datetime,
) -> tuple[str, ...]:
    """A flat broker snapshot alone cannot authorize another learning episode."""
    events = store.read_as_of(policy.experiment_id, now)
    observations = {row.prediction_id: row for event in events
                    if event.kind == "ExplorationObservation"
                    for row in (event.decode(ExplorationObservation),)
                    if row.policy_id == policy.policy_id}
    lots = tuple(event.decode(LotEvent) for event in events if event.kind == "LotEvent")
    from quantbot.learner.experiment_rejections import verified_refused_lots

    refused = verified_refused_lots(store, policy.experiment_id, now=now)
    reasons: set[str] = set()
    for declaration in (row for row in lots if row.kind == "ENTRY_DECLARED"):
        observation = observations.get(declaration.decision_id)
        if declaration.lot_id in refused:
            if (cohort_at <= refused[declaration.lot_id].recorded_at
                    or observation is not None and cohort_at <= observation.features.feature_at):
                reasons.add("FRESH_POST_EPISODE_COHORT_REQUIRED")
            continue
        # Other owned experiment lots also count toward the concurrent-position fence.
        if resolve_lot(store, declaration.lot_id, now=now).status != "COMPLETE":
            reasons.add("PRIOR_OWNED_EPISODE_INCOMPLETE")
        if observation is not None:
            if cohort_at <= observation.features.feature_at:
                reasons.add("FRESH_POST_EPISODE_COHORT_REQUIRED")
            flat = tuple(row for row in lots if row.lot_id == declaration.lot_id
                         and row.kind == "RECONCILED_FLAT" and row.reconciled)
            if not flat or cohort_at <= max(row.occurred_at for row in flat):
                reasons.add("FRESH_POST_EPISODE_COHORT_REQUIRED")
    return tuple(sorted(reasons))


def verified_commission(
    store: ExperimentStore, policy: ExplorationPolicy, services: IntradayPaperServices,
    *, now: datetime,
) -> ExplorationCommission:
    rows = tuple(event.decode(ExplorationCommission)
                 for event in store.read_as_of(policy.experiment_id, now)
                 if event.kind == "ExplorationCommission"
                 and event.decode(ExplorationCommission).policy_id == policy.policy_id)
    runner = services.runner
    if (policy.synthetic or len(rows) != 1
            or rows[0].manifest_hash != policy.manifest_hash
            or rows[0].binding_hash != runner.binding.configuration_hash
            or rows[0].code_commit != runner._identity.git_commit
            or not policy.recorded_at <= rows[0].recorded_at <= now
            or not rows[0].source_authorization_ref.startswith("chat:")):
        raise ValueError("EXACT_OPERATOR_EXPLORATION_COMMISSION_REQUIRED")
    return rows[0]


async def prepare_exploration_entry(
    manifest: ExperimentManifest, policy: ExplorationPolicy,
    *, store: ExperimentStore, services: IntradayPaperServices, simulation: bool = False,
) -> OrderIntent | None:
    _paper_only(services)
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    policy = ExplorationPolicy.model_validate(policy.model_dump())
    runner = services.runner
    if not isinstance(runner, ExplorationPaperRunner) or runner.exploration != policy:
        raise ValueError("SEPARATE_EXPLORATION_RUNNER_REQUIRED")
    if (policy.manifest_hash != content_id(manifest)
            or store.state(manifest.experiment_id).manifest_hash != content_id(manifest)):
        raise ValueError("EXACT_EXPLORATION_MANIFEST_REQUIRED")
    now = runner._clock()
    def refuse(*reasons: str) -> None:
        record = ExplorationEntryRefusal(
            experiment_id=manifest.experiment_id, policy_id=policy.policy_id,
            binding_hash=runner.binding.configuration_hash, run_id=runner._run_id,
            recorded_at=runner._clock(),
            reasons=tuple(dict.fromkeys(reasons)) or ("NO_ELIGIBLE_EXPLORATION_ENTRY",),
            synthetic=simulation,
        )
        store.append(record, idempotency_key="exploration-entry-refusal:" + content_id(record))
    if simulation:
        if (not manifest.synthetic or not policy.synthetic
                or str(runner._broker.provider) != "synthetic"):
            raise ValueError("SYNTHETIC_SANDBOX_ONLY")
    else:
        if manifest.synthetic or policy.synthetic:
            raise ValueError("SYNTHETIC_EVIDENCE_HAS_NO_REAL_PAPER_AUTHORITY")
        commission = verified_commission(store, policy, services, now=now)
        if any(commission.recorded_at > row.features.decision_at for row in runner.predictions):
            refuse("POST_COMMISSION_COHORT_REQUIRED")
            return None
    if (now < manifest.prospective_start_at
            or store.state(manifest.experiment_id).state in
            {"FAULTED", "COMPLETED", "REJECTED", "INSUFFICIENT_EVIDENCE"}):
        refuse("EXPERIMENT_NOT_IN_ACTIVE_PROSPECTIVE_WINDOW")
        return None
    from quantbot.learner.experiment_evaluation import evaluate_experiment

    evaluation = evaluate_experiment(store, manifest, now=now)
    if not evaluation.new_entries_allowed:
        refuse("EXPERIMENT_EVALUATION_ENTRY_NOT_ALLOWED")
        return None  # Unknown calendar or exhausted window never grants entry authority.
    if store.pending_intents(manifest.experiment_id):
        refuse("EXPERIMENT_PENDING_INTENT_REQUIRES_RECOVERY")
        return None  # Recovery never releases an unknown/unsent ID for another broker send.
    prior = tuple(sorted({reason for row in runner.predictions for reason in
                          prior_episode_objections(store, policy,
                                                   cohort_at=row.features.feature_at, now=now)}))
    if prior:
        refuse(*prior)
        return None
    journal_exploration_feedback(store, policy, now=now)
    completed = len(verified_feedback(store, policy, now=now))
    if runner.completed_lots != completed:
        refuse("IMMUTABLE_EXPERIENCE_REFRESH_REQUIRED")
        return None  # Rebuild the next selection from newly available immutable experience.
    result = await runner.evaluate()
    if not result.submissions:
        refuse(*runner.last_reasons)
        return None
    if len(result.submissions) != 1:
        raise ValueError("ONE_CONCURRENT_EXPLORATION_ENTRY_REQUIRED")
    request = result.submissions[0]
    matching = tuple(row for row in runner.predictions
                     if isinstance(row, ExplorationObservation)
                     and row.features.symbol == request.intent.symbol)
    if len(matching) != 1:
        raise ValueError("EXACT_EXPLORATION_OBSERVATION_REQUIRED")
    return await journal_entry_request(manifest, matching[0], store=store,
                                       services=services, request=request)


class ExplorationStrategyRunner:
    """Policy-specific journal; the retained cycle alone owns actual submissions."""
    def __init__(
        self, manifest: ExperimentManifest, policy: ExplorationPolicy, *, store: ExperimentStore,
        services: IntradayPaperServices, runtime: Runtime, clock: Callable[[], datetime],
        simulation: bool = False,
    ) -> None:
        self.manifest, self.policy, self.store = manifest, policy, store
        self.services, self.simulation = services, simulation
        self.runtime, self.clock = runtime, clock

    async def evaluate_exits(self) -> CycleStrategyResult:
        return await self.services.runner.evaluate_exits()

    async def evaluate(self) -> CycleStrategyResult:
        await refresh_entry_protection(self.runtime, now=self.clock())
        intent = await prepare_exploration_entry(
            self.manifest, self.policy, store=self.store, services=self.services,
            simulation=self.simulation,
        )
        queued = self.store.get_intent(intent.intent_id) if intent is not None else None
        return CycleStrategyResult(
            submissions=() if queued is None
            else (SubmissionRequest.model_validate_json(queued.document),),
            signal_count=len(self.services.runner.predictions),
        )


async def refresh_entry_protection(runtime: Runtime, *, now: datetime) -> None:
    """Genuine fresh assessment under the cycle lock; no additional sell authority.

    Reuse the existing protection identity/policy and retain the separate daemon.
    Its durable assessment can still block entry for failed state or required exits.
    The original supervisor and its submitter are never modified.
    """
    evaluator = replace(runtime.protector(), submitter=None)
    await evaluator.evaluate(now=now)


async def run_exploration_cycle(
    runtime: Runtime, services: IntradayPaperServices, *, manifest: ExperimentManifest,
    policy: ExplorationPolicy, store: ExperimentStore, owner: str, data_sync: DataSyncRunner,
    clock: Callable[[], datetime], run_id: str, simulation: bool = False,
) -> CycleResult:
    """Complete retained lifecycle; no inline fitting, registration or consent creation."""
    _paper_only(services)
    if (runtime.database.path.resolve() != services.runner._database.path.resolve()
            or runtime.coordinator is not services.deadlines.coordinator):
        raise ValueError("OPERATIONAL_SERVICE_GRAPH_MISMATCH")
    now = clock()
    if simulation:
        if not policy.synthetic or str(services.runner._broker.provider) != "synthetic":
            raise ValueError("SYNTHETIC_SANDBOX_ONLY")
    else:
        verified_commission(store, policy, services, now=now)
    if not run_id or run_id != run_id.strip():
        raise ValueError("EXACT_OPERATIONAL_RUN_ID_REQUIRED")
    services.runner.bind_run(run_id)
    strategy = ExplorationStrategyRunner(manifest, policy, store=store, services=services,
                                          runtime=runtime, clock=clock, simulation=simulation)
    cycle = build_learner_paper_cycle(
        runtime, strategy, binding=services.runner.binding, identity=services.runner._identity,
        data_sync=data_sync, max_data_age_seconds=services.runner.policy.max_reference_age_seconds,
        clock=clock, submission_wrapper=lambda lower: ExperimentOutboxSubmitter(
            store, services=services, lower=lower, owner=owner, clock=clock,
        ),
    )
    return await cycle.run(run_id, started_at=now,
                           trading_date=now.astimezone(ZoneInfo("America/New_York")).date())
