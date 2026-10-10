"""One-shot operator activation audit; automated services never create approvals.

Approval provenance is supplied by the authenticated operator workflow at action
time. A proposal, broad consent notice, or successor is not that approval. This
module does not publish an approval endpoint, register an operational deployment,
promote a strategy, start a service, change credentials, or submit an order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import model_validator

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_research import ActivationPackage, FitReceipt, activation_package
from quantbot.learner.experiment_store import ExperimentState, ExperimentStore, _digest, _stamp
from quantbot.learner.models import LearnerModel, Text
from quantbot.learner.paper import assess_paper_entry
from quantbot.learner.paper_runner import IntradayPaperServices
from quantbot.research.manifest import content_id


class ExactArtifactApproval(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    package_id: Digest
    model_id: Digest
    operator: Text
    authority: Literal["EXPLICIT_OPERATOR_CHAT_APPROVAL", "SYNTHETIC_TEST_ONLY"]
    reference: Text
    mode: Literal["PAPER"] = "PAPER"
    synthetic: bool = False

    @property
    def approval_id(self) -> str:
        return content_id(self)

    @model_validator(mode="after")
    def provenance(self) -> Self:
        if self.synthetic != (self.authority == "SYNTHETIC_TEST_ONLY"):
            raise ValueError("SYNTHETIC_APPROVAL_CANNOT_CLAIM_REAL_CONSENT")
        return self


class ArtifactActivation(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    manifest_hash: Digest
    package_id: Digest
    model_id: Digest
    approval_id: Digest
    binding_hash: Digest
    mode: Literal["PAPER"] = "PAPER"
    synthetic: bool

    @property
    def activation_id(self) -> str:
        return content_id(self)


def active_artifact(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    now: datetime,
) -> ArtifactActivation | None:
    state = store.state(manifest.experiment_id)
    if state.state != "ACTIVE_PAPER":
        return None
    events = store.read_as_of(manifest.experiment_id, now)
    matches = [
        event.decode(ArtifactActivation)
        for event in events
        if event.kind == "ArtifactActivation"
        and event.decode(ArtifactActivation).activation_id == state.activation_id
    ]
    if len(matches) != 1 or matches[0].manifest_hash != content_id(manifest):
        raise ValueError("EXACT_ARTIFACT_ACTIVATION_UNVERIFIED")
    activation = matches[0]
    approvals = [
        event.decode(ExactArtifactApproval)
        for event in events
        if event.kind == "ExactArtifactApproval"
        and event.decode(ExactArtifactApproval).approval_id == activation.approval_id
    ]
    if len(approvals) != 1 or (
        approvals[0].package_id != activation.package_id
        or approvals[0].model_id != activation.model_id
        or approvals[0].synthetic != activation.synthetic
        or manifest.synthetic != activation.synthetic
        or approvals[0].experiment_id != manifest.experiment_id
        or activation.experiment_id != manifest.experiment_id
        or approvals[0].recorded_at > activation.recorded_at
    ):
        raise ValueError("EXACT_OPERATOR_APPROVAL_UNVERIFIED")
    packages = [
        event.decode(ActivationPackage)
        for event in events
        if event.kind == "ActivationPackage"
        and event.decode(ActivationPackage).package_id == activation.package_id
    ]
    if len(packages) != 1 or (
        packages[0].experiment_id != manifest.experiment_id
        or packages[0].model_id != activation.model_id
        or packages[0].configuration_hash != activation.binding_hash
        or packages[0].synthetic != activation.synthetic
        or packages[0].recorded_at > approvals[0].recorded_at
    ):
        raise ValueError("EXACT_ACTIVATION_PACKAGE_UNVERIFIED")
    return activation


def _validate_activation_request(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    package: ActivationPackage,
    *,
    approval: ExactArtifactApproval,
    services: IntradayPaperServices,
    simulation: bool = False,
) -> datetime:
    approval = ExactArtifactApproval.model_validate(approval.model_dump())
    package = ActivationPackage.model_validate(package.model_dump())
    now = services.runner._clock()
    if simulation:
        if (
            not manifest.synthetic
            or not package.synthetic
            or not approval.synthetic
            or str(services.runner._broker.provider) != "synthetic"
        ):
            raise ValueError("SYNTHETIC_SANDBOX_ONLY")
        if str(services.runner._broker.environment).upper() != "PAPER":
            raise ValueError("PAPER_ONLY")
    else:
        if manifest.synthetic or package.synthetic or approval.synthetic:
            raise ValueError("SYNTHETIC_APPROVAL_NOT_REAL_ROUTE")
        from quantbot.learner.experiment_execution import _paper_only

        _paper_only(services)
    if (
        approval.experiment_id != manifest.experiment_id
        or package.experiment_id != manifest.experiment_id
        or approval.package_id != package.package_id
        or approval.model_id != package.model_id
        or approval.operator != package.owner
        or not package.recorded_at <= approval.recorded_at <= now
    ):
        raise ValueError("EXACT_OPERATOR_PACKAGE_APPROVAL_REQUIRED")
    if not bool(getattr(services.runner, "clock_health", lambda _: False)(now)):
        raise ValueError("CLOCK_SYNC_REQUIRED")
    events = store.read_as_of(manifest.experiment_id, now)
    if not any(
        event.kind == "ActivationPackage" and event.decode(ActivationPackage) == package
        for event in events
    ):
        raise ValueError("AUTHORITATIVE_ACTIVATION_PACKAGE_REQUIRED")
    fits = [
        event.decode(FitReceipt)
        for event in events
        if event.kind == "FitReceipt"
        and event.decode(FitReceipt).request_id == package.fit_request_id
    ]
    if len(fits) != 1:
        raise ValueError("AUTHORITATIVE_FIT_RECEIPT_REQUIRED")
    verified = activation_package(
        store,
        manifest,
        fit=fits[0],
        now=package.recorded_at,
        config_hash=package.configuration_hash,
        owner=package.owner,
        rollback_model_id=package.rollback_model_id,
    )
    binding = services.runner.binding
    if (
        verified != package
        or services.runner.model.model_id != package.model_id
        or binding.model_id != package.model_id
        or binding.code_hash != package.code_hash
        or binding.configuration_hash != package.configuration_hash
    ):
        raise ValueError("EXACT_OPERATIONAL_BINDING_REQUIRED")
    gate = assess_paper_entry(
        services.runner._database.path, binding=binding, identity=services.runner._identity
    )
    if not gate.allowed:
        raise ValueError("OPERATIONAL_DEPLOYMENT_AND_PROMOTION_REQUIRED")
    completed_at = services.runner._clock()
    if completed_at < now or not bool(
        getattr(services.runner, "clock_health", lambda _: False)(completed_at)
    ):
        raise ValueError("CLOCK_SYNC_REQUIRED")
    return completed_at


def activate_exact_package(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    package: ActivationPackage,
    *,
    approval: ExactArtifactApproval,
    services: IntradayPaperServices,
    simulation: bool = False,
) -> ArtifactActivation:
    approval = ExactArtifactApproval.model_validate(approval.model_dump())
    package = ActivationPackage.model_validate(package.model_dump())
    now = _validate_activation_request(store, manifest, package, approval=approval,
                                       services=services, simulation=simulation)
    if store.state(manifest.experiment_id).reason.startswith('EXACT_SWITCH_PENDING:'):
        raise ValueError('EXPLICIT_ACTIVE_MODEL_SWITCH_PROCEDURE_REQUIRED')
    previous = active_artifact(store, manifest, now=now)
    if previous is not None:
        if (
            previous.approval_id == approval.approval_id
            and previous.package_id == package.package_id
        ):
            return previous
        raise ValueError("EXPLICIT_ACTIVE_MODEL_SWITCH_PROCEDURE_REQUIRED")
    state = store.state(manifest.experiment_id)
    if state.activation_id is not None:
        raise ValueError('EXPLICIT_ACTIVE_MODEL_SWITCH_PROCEDURE_REQUIRED')
    if state.state not in {"CANDIDATE_SHADOW", "ACTIVATION_REQUIRED", "EVALUATING"}:
        raise ValueError("EXPERIMENT_NOT_ACTIVATION_READY")
    store.append(approval, idempotency_key="exact-approval:" + approval.approval_id)
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
    store.append(activation, idempotency_key="artifact-activation:" + activation.activation_id)
    if state.state != "ACTIVATION_REQUIRED":
        state = store.transition(
            manifest.experiment_id,
            expected_revision=state.revision,
            target="ACTIVATION_REQUIRED",
            reason="EXACT_CONSENT_RECORDED_COMMISSIONING",
            now=now,
        )
    with store._transaction() as db:
        current = store._state(db, manifest.experiment_id)
        if current.revision != state.revision or current.state != "ACTIVATION_REQUIRED":
            raise ValueError("STALE_ACTIVATION_STATE_REVISION")
        successor = ExperimentState.model_validate(
            current.model_dump()
            | {
                "state": "ACTIVE_PAPER",
                "activation_id": activation.activation_id,
                "consent_recorded": True,
                "revision": current.revision + 1,
                "changed_at": now,
                "reason": "EXACT_OPERATOR_ARTIFACT_ACTIVATED_PAPER_ONLY",
            }
        )
        document = successor.model_dump_json()
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
            (
                manifest.experiment_id,
                f"transition:{successor.revision}",
                "ExperimentState",
                _stamp(now),
                document,
                _digest(document),
            ),
        )
        store._save_state(db, successor)
    return activation
