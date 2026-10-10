"""Exact approved sidecar switches; operational registration stays external.

Staging fences entries before a separately authorized binding change. Completion
rechecks current operational gates. No task, promotion, stop, order or fit is changed
here. Recovery never selects a model automatically or deletes later evidence.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import Field

from quantbot.learner.experience_continuity import _insert_record
from quantbot.learner.experiment_activation import (
    ArtifactActivation,
    ExactArtifactApproval,
    _validate_activation_request,
    active_artifact,
)
from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_lots import LotEvent, resolve_lot
from quantbot.learner.experiment_rejections import verified_refused_lots
from quantbot.learner.experiment_research import ActivationPackage, FitReceipt, activation_package
from quantbot.learner.experiment_store import ExperimentState, ExperimentStore, _digest, _stamp
from quantbot.learner.models import LearnerModel, Text
from quantbot.learner.paper_runner import IntradayPaperServices
from quantbot.research.manifest import content_id


class ArtifactSwitchProposal(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    manifest_hash: Digest
    incumbent_activation_id: Digest
    incumbent_revision: int = Field(ge=0)
    target_package_id: Digest
    target_model_id: Digest
    target_binding_hash: Digest
    purpose: Literal["SWITCH", "ROLLBACK", "RECOVER_PENDING"]
    retained_activation_id: str | None = None
    replaces_stage_id: str | None = None
    synthetic: bool
    grants_execution: Literal[False] = False

    @property
    def proposal_id(self) -> str:
        return content_id(self)


class ExactSwitchApproval(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    proposal_id: Digest
    operator: Text
    authority: Literal["EXPLICIT_OPERATOR_CHAT_APPROVAL", "SYNTHETIC_TEST_ONLY"]
    reference: Text
    synthetic: bool

    @property
    def approval_id(self) -> str:
        return content_id(self)


class ArtifactSwitchStage(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    proposal_id: Digest
    approval_id: Digest
    incumbent_activation_id: Digest
    pending_revision: int
    synthetic: bool


class ArtifactSwitchCompletion(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    proposal_id: Digest
    approval_id: Digest
    activation_id: Digest
    purpose: Literal["SWITCH", "ROLLBACK", "RECOVER_PENDING"]
    synthetic: bool


def _package(
    store: ExperimentStore, manifest: ExperimentManifest, package_id: str, now: datetime
) -> ActivationPackage:
    events = store.read_as_of(manifest.experiment_id, now)
    packages = [
        e.decode(ActivationPackage)
        for e in events
        if e.kind == "ActivationPackage" and e.decode(ActivationPackage).package_id == package_id
    ]
    if len(packages) != 1:
        raise ValueError("EXACT_SWITCH_PACKAGE_REQUIRED")
    package = packages[0]
    fits = [
        e.decode(FitReceipt)
        for e in events
        if e.kind == "FitReceipt" and e.decode(FitReceipt).request_id == package.fit_request_id
    ]
    if len(fits) != 1:
        raise ValueError("EXACT_SWITCH_FIT_LINEAGE_REQUIRED")
    rebuilt = activation_package(
        store,
        manifest,
        fit=fits[0],
        now=package.recorded_at,
        config_hash=package.configuration_hash,
        owner=package.owner,
        rollback_model_id=package.rollback_model_id,
    )
    if rebuilt != package:
        raise ValueError("EXACT_SWITCH_PACKAGE_REQUIRED")
    return package


def _retained(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    activation_id: str,
    now: datetime,
) -> tuple[ArtifactActivation, ExactArtifactApproval, ActivationPackage]:
    events = store.read_as_of(manifest.experiment_id, now)
    activations = [
        e.decode(ArtifactActivation)
        for e in events
        if e.kind == "ArtifactActivation"
        and e.decode(ArtifactActivation).activation_id == activation_id
    ]
    states = [
        e.decode(ExperimentState)
        for e in events
        if e.kind == "ExperimentState"
        and e.decode(ExperimentState).state == "ACTIVE_PAPER"
        and e.decode(ExperimentState).activation_id == activation_id
    ]
    if len(activations) != 1 or not states:
        raise ValueError("EXACT_RETAINED_ACTIVATION_REQUIRED")
    activation = activations[0]
    package = _package(store, manifest, activation.package_id, now)
    approvals = [
        e.decode(ExactArtifactApproval)
        for e in events
        if e.kind == "ExactArtifactApproval"
        and e.decode(ExactArtifactApproval).approval_id == activation.approval_id
    ]
    if len(approvals) != 1:
        raise ValueError("EXACT_RETAINED_APPROVAL_REQUIRED")
    approval = approvals[0]
    if (
        activation.manifest_hash != content_id(manifest)
        or activation.model_id != package.model_id
        or activation.binding_hash != package.configuration_hash
        or activation.synthetic != manifest.synthetic
        or approval.synthetic != manifest.synthetic
        or approval.package_id != package.package_id
        or approval.model_id != package.model_id
        or approval.operator != package.owner
        or not package.recorded_at <= approval.recorded_at <= activation.recorded_at <= now
        or not any(s.changed_at == activation.recorded_at for s in states)
    ):
        raise ValueError("EXACT_RETAINED_ACTIVATION_REQUIRED")
    return activation, approval, package


def _pending(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    stage: ArtifactSwitchStage,
    now: datetime,
) -> tuple[ArtifactSwitchProposal, ExactSwitchApproval]:
    events = store.read_as_of(manifest.experiment_id, now)
    stages = [
        e.decode(ArtifactSwitchStage)
        for e in events
        if e.kind == "ArtifactSwitchStage"
        and e.decode(ArtifactSwitchStage).proposal_id == stage.proposal_id
    ]
    proposals = [
        e.decode(ArtifactSwitchProposal)
        for e in events
        if e.kind == "ArtifactSwitchProposal"
        and e.decode(ArtifactSwitchProposal).proposal_id == stage.proposal_id
    ]
    approvals = [
        e.decode(ExactSwitchApproval)
        for e in events
        if e.kind == "ExactSwitchApproval"
        and e.decode(ExactSwitchApproval).approval_id == stage.approval_id
    ]
    if stages != [stage] or len(proposals) != 1 or len(approvals) != 1:
        raise ValueError("EXACT_SWITCH_STAGE_EVIDENCE_REQUIRED")
    proposal, approval = proposals[0], approvals[0]
    package = _package(store, manifest, proposal.target_package_id, now)
    _approval(proposal, approval, package, stage.recorded_at)
    if (
        stage.incumbent_activation_id != proposal.incumbent_activation_id
        or stage.pending_revision != proposal.incumbent_revision + 1
        or stage.synthetic != manifest.synthetic
        or proposal.manifest_hash != content_id(manifest)
    ):
        raise ValueError("EXACT_SWITCH_STAGE_EVIDENCE_REQUIRED")
    return proposal, approval


def _approval(
    proposal: ArtifactSwitchProposal,
    approval: ExactSwitchApproval,
    package: ActivationPackage,
    now: datetime,
) -> None:
    if (
        approval.proposal_id != proposal.proposal_id
        or approval.experiment_id != proposal.experiment_id
        or approval.operator != package.owner
        or approval.synthetic != proposal.synthetic
        or approval.synthetic != (approval.authority == "SYNTHETIC_TEST_ONLY")
        or not package.recorded_at <= proposal.recorded_at <= approval.recorded_at <= now
        or not approval.synthetic
        and not approval.reference.startswith("chat:")
    ):
        raise ValueError("EXACT_SWITCH_APPROVAL_REQUIRED")


def _clear_inventory(store: ExperimentStore, manifest: ExperimentManifest, now: datetime) -> None:
    if store.pending_intents(manifest.experiment_id):
        raise ValueError("PENDING_INTENT_BLOCKS_SWITCH")
    refused = verified_refused_lots(store, manifest.experiment_id, now=now)
    lots = {
        e.decode(LotEvent).lot_id
        for e in store.read_as_of(manifest.experiment_id, now)
        if e.kind == "LotEvent" and e.decode(LotEvent).kind == "ENTRY_DECLARED"
    }
    if any(
        lot not in refused and resolve_lot(store, lot, now=now).status != "COMPLETE" for lot in lots
    ):
        raise ValueError("INCOMPLETE_OWNED_EPISODE_BLOCKS_SWITCH")


def _snapshot_db(db: sqlite3.Connection) -> str:
    return content_id(
        [
            [tuple(row) for row in db.execute(query).fetchall()]
            for query in (
                "SELECT experiment_id,idempotency_key,digest FROM events "
                "ORDER BY experiment_id,idempotency_key",
                "SELECT experiment_id,document FROM states ORDER BY experiment_id",
                "SELECT intent_id,experiment_id,document,state,owner FROM outbox "
                "ORDER BY intent_id",
            )
        ]
    )


def _snapshot(store: ExperimentStore) -> str:
    with store._transaction(write=False) as db:
        return _snapshot_db(db)


def _save_transition(
    store: ExperimentStore,
    db: sqlite3.Connection,
    state: ExperimentState,
) -> None:
    document = state.model_dump_json()
    db.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
        (
            state.experiment_id,
            "transition:" + str(state.revision),
            "ExperimentState",
            _stamp(state.changed_at),
            document,
            _digest(document),
        ),
    )
    store._save_state(db, state)


def propose_artifact_switch(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    target: ActivationPackage,
    *,
    expected_revision: int,
    now: datetime,
    purpose: Literal["SWITCH", "ROLLBACK", "RECOVER_PENDING"] = "SWITCH",
) -> ArtifactSwitchProposal:
    state = store.state(manifest.experiment_id)
    if (
        state.revision != expected_revision
        or state.activation_id is None
        or state.manifest_hash != content_id(manifest)
    ):
        raise ValueError("EXACT_INCUMBENT_REVISION_REQUIRED")
    target = _package(store, manifest, target.package_id, now)
    current, _, incumbent = _retained(store, manifest, state.activation_id, now)
    replaces = None
    if purpose == "RECOVER_PENDING":
        if not state.reason.startswith("EXACT_SWITCH_PENDING:"):
            raise ValueError("PENDING_SWITCH_REQUIRED")
        replaces = state.reason.split(":", 1)[1]
        stages = [
            e.decode(ArtifactSwitchStage)
            for e in store.read_as_of(manifest.experiment_id, now)
            if e.kind == "ArtifactSwitchStage"
            and e.decode(ArtifactSwitchStage).proposal_id == replaces
        ]
        if len(stages) != 1:
            raise ValueError("EXACT_SWITCH_STAGE_EVIDENCE_REQUIRED")
        _pending(store, manifest, stages[0], now)
        if target != incumbent:
            raise ValueError("EXACT_INCUMBENT_RECOVERY_REQUIRED")
    elif state.state != "ACTIVE_PAPER" or active_artifact(store, manifest, now=now) != current:
        raise ValueError("EXACT_ACTIVE_INCUMBENT_REQUIRED")
    retained = None
    if purpose in {"ROLLBACK", "RECOVER_PENDING"}:
        matches = [
            e.decode(ArtifactActivation)
            for e in store.read_as_of(manifest.experiment_id, now)
            if e.kind == "ArtifactActivation"
            and e.decode(ArtifactActivation).package_id == target.package_id
        ]
        if not matches or (
            purpose == "ROLLBACK" and incumbent.rollback_model_id != target.model_id
        ):
            raise ValueError("EXACT_RETAINED_ROLLBACK_REQUIRED")
        retained = matches[-1].activation_id
        _retained(store, manifest, retained, now)
    elif target.model_id == current.model_id or target.rollback_model_id != current.model_id:
        raise ValueError("EXACT_INCUMBENT_ROLLBACK_ID_REQUIRED")
    proposal = ArtifactSwitchProposal(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        manifest_hash=content_id(manifest),
        incumbent_activation_id=current.activation_id,
        incumbent_revision=state.revision,
        target_package_id=target.package_id,
        target_model_id=target.model_id,
        target_binding_hash=target.configuration_hash,
        purpose=purpose,
        retained_activation_id=retained,
        replaces_stage_id=replaces,
        synthetic=manifest.synthetic,
    )
    store.append(proposal, idempotency_key="switch-proposal:" + proposal.proposal_id)
    return proposal


def stage_artifact_switch(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    proposal: ArtifactSwitchProposal,
    *,
    approval: ExactSwitchApproval,
    services: IntradayPaperServices,
    simulation: bool = False,
) -> ArtifactSwitchStage:
    approval = ExactSwitchApproval.model_validate(approval.model_dump())
    snapshot = _snapshot(store)
    now = services.runner._clock()
    package = _package(store, manifest, proposal.target_package_id, now)
    _approval(proposal, approval, package, now)
    events = store.read_as_of(manifest.experiment_id, now)
    if [
        e.decode(ArtifactSwitchProposal)
        for e in events
        if e.kind == "ArtifactSwitchProposal"
        and e.decode(ArtifactSwitchProposal).proposal_id == proposal.proposal_id
    ] != [proposal]:
        raise ValueError("EXACT_REGISTERED_SWITCH_PROPOSAL_REQUIRED")
    existing = [
        e.decode(ArtifactSwitchStage)
        for e in events
        if e.kind == "ArtifactSwitchStage"
        and e.decode(ArtifactSwitchStage).proposal_id == proposal.proposal_id
    ]
    if existing:
        if len(existing) != 1 or existing[0].approval_id != approval.approval_id:
            raise ValueError("EXACT_SWITCH_STAGE_EVIDENCE_REQUIRED")
        _pending(store, manifest, existing[0], now)
        return existing[0]
    rebuilt = propose_artifact_switch(
        store,
        manifest,
        package,
        expected_revision=proposal.incumbent_revision,
        now=proposal.recorded_at,
        purpose=proposal.purpose,
    )
    if rebuilt != proposal:
        raise ValueError("EXACT_INCUMBENT_SWITCH_PROPOSAL_REQUIRED")
    _, previous_approval, incumbent = _retained(
        store, manifest, proposal.incumbent_activation_id, now
    )
    now = _validate_activation_request(
        store,
        manifest,
        incumbent,
        approval=previous_approval,
        services=services,
        simulation=simulation,
    )
    _clear_inventory(store, manifest, now)
    stage = ArtifactSwitchStage(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        proposal_id=proposal.proposal_id,
        approval_id=approval.approval_id,
        incumbent_activation_id=proposal.incumbent_activation_id,
        pending_revision=proposal.incumbent_revision + 1,
        synthetic=manifest.synthetic,
    )
    with store._transaction() as db:
        if _snapshot_db(db) != snapshot:
            raise ValueError("SWITCH_EVIDENCE_CHANGED_RETRY_PREFLIGHT")
        current = store._state(db, manifest.experiment_id)
        if (
            current.revision != proposal.incumbent_revision
            or current.activation_id != proposal.incumbent_activation_id
            or current.state not in {"ACTIVE_PAPER", "ACTIVATION_REQUIRED"}
        ):
            raise ValueError("EXACT_INCUMBENT_REVISION_REQUIRED")
        _clear_inventory(store, manifest, now)
        _insert_record(db, approval, "switch-approval:" + approval.approval_id)
        _insert_record(db, stage, "switch-stage:" + proposal.proposal_id)
        _save_transition(
            store,
            db,
            current.model_copy(
                update={
                    "state": "ACTIVATION_REQUIRED",
                    "revision": stage.pending_revision,
                    "changed_at": now,
                    "reason": "EXACT_SWITCH_PENDING:" + proposal.proposal_id,
                }
            ),
        )
    return stage


def complete_artifact_switch(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    stage: ArtifactSwitchStage,
    *,
    services: IntradayPaperServices,
    simulation: bool = False,
) -> ArtifactActivation:
    snapshot = _snapshot(store)
    now = services.runner._clock()
    proposal, consent = _pending(store, manifest, stage, now)
    package = _package(store, manifest, proposal.target_package_id, now)
    if (
        package.model_id != proposal.target_model_id
        or package.configuration_hash != proposal.target_binding_hash
    ):
        raise ValueError("EXACT_TARGET_BINDING_REQUIRED")
    approval = ExactArtifactApproval(
        experiment_id=manifest.experiment_id,
        recorded_at=consent.recorded_at,
        package_id=package.package_id,
        model_id=package.model_id,
        operator=consent.operator,
        authority=consent.authority,
        reference=consent.reference,
        synthetic=consent.synthetic,
    )
    now = _validate_activation_request(
        store, manifest, package, approval=approval, services=services, simulation=simulation
    )
    completed = [
        e.decode(ArtifactSwitchCompletion)
        for e in store.read_as_of(manifest.experiment_id, now)
        if e.kind == "ArtifactSwitchCompletion"
        and e.decode(ArtifactSwitchCompletion).proposal_id == proposal.proposal_id
    ]
    if completed:
        cached_activation = active_artifact(store, manifest, now=now)
        if (
            len(completed) != 1
            or cached_activation is None
            or cached_activation.activation_id != completed[0].activation_id
        ):
            raise ValueError("STALE_COMPLETED_SWITCH")
        return cached_activation
    _clear_inventory(store, manifest, now)
    activation = ArtifactActivation(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        manifest_hash=content_id(manifest),
        package_id=package.package_id,
        model_id=package.model_id,
        approval_id=approval.approval_id,
        binding_hash=package.configuration_hash,
        synthetic=manifest.synthetic,
    )
    completion = ArtifactSwitchCompletion(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        proposal_id=proposal.proposal_id,
        approval_id=consent.approval_id,
        activation_id=activation.activation_id,
        purpose=proposal.purpose,
        synthetic=manifest.synthetic,
    )
    with store._transaction() as db:
        if _snapshot_db(db) != snapshot:
            raise ValueError("SWITCH_EVIDENCE_CHANGED_RETRY_PREFLIGHT")
        current = store._state(db, manifest.experiment_id)
        if (
            current.state != "ACTIVATION_REQUIRED"
            or current.revision != stage.pending_revision
            or current.activation_id != stage.incumbent_activation_id
            or current.reason != "EXACT_SWITCH_PENDING:" + proposal.proposal_id
            or now < current.changed_at
        ):
            raise ValueError("STALE_SWITCH_STATE_REVISION")
        _clear_inventory(store, manifest, now)
        _insert_record(db, approval, "exact-approval:" + approval.approval_id)
        _insert_record(db, activation, "artifact-activation:" + activation.activation_id)
        _insert_record(db, completion, "switch-completion:" + proposal.proposal_id)
        _save_transition(
            store,
            db,
            current.model_copy(
                update={
                    "state": "ACTIVE_PAPER",
                    "activation_id": activation.activation_id,
                    "revision": current.revision + 1,
                    "changed_at": now,
                    "reason": "EXACT_APPROVED_" + proposal.purpose,
                    "consent_recorded": True,
                }
            ),
        )
    return activation
