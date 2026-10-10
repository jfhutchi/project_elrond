"""Explicit code-only lineage audit; never transfers ownership or order authority."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_store import ExperimentStore, _digest, _stamp
from quantbot.learner.exploration import ExplorationPolicy
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id


def _insert_record(db: sqlite3.Connection, record: BaseModel, key: str) -> None:
    verified = type(record).model_validate(record.model_dump())
    values = verified.model_dump(mode="json")
    document = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    experiment = values["experiment_id"]
    old = db.execute(
        "SELECT digest FROM events WHERE experiment_id=? AND idempotency_key=?", (experiment, key)
    ).fetchone()
    if old is not None:
        if old[0] != _digest(document):
            raise ValueError("CONFLICTING_IMMUTABLE_EVIDENCE")
        return
    db.execute(
        "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
        (
            experiment,
            key,
            type(record).__name__,
            _stamp(datetime.fromisoformat(values["recorded_at"])),
            document,
            _digest(document),
        ),
    )


class PolicyTransitionProposal(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    source_policy_id: Digest
    target_policy_id: Digest
    manifest_hash: Digest
    semantics_hash: Digest
    synthetic: bool
    grants_execution: Literal[False] = False

    @property
    def proposal_id(self) -> str:
        return content_id(self)


class PolicyContinuityApproval(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    proposal_id: Digest
    operator: Literal["jfhutchi"]
    reference: Text
    authority: Literal["EXPLICIT_OPERATOR_CHAT_APPROVAL", "SYNTHETIC_TEST_ONLY"]
    synthetic: bool

    @property
    def approval_id(self) -> str:
        return content_id(self)


class PolicyContinuityReceipt(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    proposal_id: Digest
    approval_id: Digest
    source_policy_id: Digest
    target_policy_id: Digest
    synthetic: bool
    grants_execution: Literal[False] = False


def _registered(store: ExperimentStore, policy: ExplorationPolicy, now: datetime) -> None:
    events = store.read_as_of(policy.experiment_id, now)
    matches = [
        event.decode(ExplorationPolicy)
        for event in events
        if event.kind == "ExplorationPolicy"
        and event.decode(ExplorationPolicy).policy_id == policy.policy_id
    ]
    manifests = [
        event.decode(ExperimentManifest) for event in events if event.kind == "ExperimentManifest"
    ]
    if (
        matches != [policy]
        or len(manifests) != 1
        or content_id(manifests[0]) != policy.manifest_hash
        or manifests[0].synthetic != policy.synthetic
        or store.state(policy.experiment_id).manifest_hash != policy.manifest_hash
    ):
        raise ValueError("EXACT_REGISTERED_POLICY_REQUIRED")


def _semantics(policy: ExplorationPolicy) -> str:
    return content_id(policy.model_dump(exclude={"code_hash", "recorded_at"}))


def _compatible(source: ExplorationPolicy, target: ExplorationPolicy) -> None:
    if (
        _semantics(source) != _semantics(target)
        or source.code_hash == target.code_hash
        or source.recorded_at >= target.recorded_at
    ):
        raise ValueError("COMPATIBLE_CODE_ONLY_POLICY_REQUIRED")


def propose_policy_transition(
    store: ExperimentStore,
    source: ExplorationPolicy,
    target: ExplorationPolicy,
    *,
    now: datetime,
) -> PolicyTransitionProposal:
    _registered(store, source, now)
    _registered(store, target, now)
    _compatible(source, target)
    proposal = PolicyTransitionProposal(
        experiment_id=target.experiment_id,
        recorded_at=now,
        source_policy_id=source.policy_id,
        target_policy_id=target.policy_id,
        manifest_hash=target.manifest_hash,
        semantics_hash=_semantics(target),
        synthetic=target.synthetic,
    )
    store.append(proposal, idempotency_key="policy-proposal:" + proposal.proposal_id)
    return proposal


def _verify_approval(
    proposal: PolicyTransitionProposal,
    approval: PolicyContinuityApproval,
    now: datetime,
) -> None:
    if (
        approval.proposal_id != proposal.proposal_id
        or approval.experiment_id != proposal.experiment_id
        or approval.synthetic != proposal.synthetic
        or approval.synthetic != (approval.authority == "SYNTHETIC_TEST_ONLY")
        or not proposal.recorded_at <= approval.recorded_at <= now
        or not approval.synthetic
        and not approval.reference.startswith("chat:")
    ):
        raise ValueError("EXACT_POLICY_CONTINUITY_APPROVAL_REQUIRED")


def verified_policy_lineage(
    store: ExperimentStore,
    policy: ExplorationPolicy,
    *,
    now: datetime,
) -> tuple[ExplorationPolicy, ...]:
    _registered(store, policy, now)
    events = store.read_as_of(policy.experiment_id, now)
    proposals = [
        e.decode(PolicyTransitionProposal) for e in events if e.kind == "PolicyTransitionProposal"
    ]
    approvals = [
        e.decode(PolicyContinuityApproval) for e in events if e.kind == "PolicyContinuityApproval"
    ]
    receipts = [
        e.decode(PolicyContinuityReceipt) for e in events if e.kind == "PolicyContinuityReceipt"
    ]
    policies = [e.decode(ExplorationPolicy) for e in events if e.kind == "ExplorationPolicy"]
    chain, seen = [policy], {policy.policy_id}
    while True:
        edges = [row for row in receipts if row.target_policy_id == chain[0].policy_id]
        if not edges:
            return tuple(chain)
        if len(edges) != 1:
            raise ValueError("AMBIGUOUS_POLICY_PARENT")
        edge = edges[0]
        matches = [row for row in proposals if row.proposal_id == edge.proposal_id]
        consents = [row for row in approvals if row.approval_id == edge.approval_id]
        parents = [row for row in policies if row.policy_id == edge.source_policy_id]
        if len(matches) != 1 or len(consents) != 1 or len(parents) != 1:
            raise ValueError("EXACT_POLICY_CONTINUITY_EVIDENCE_REQUIRED")
        proposal, consent, parent = matches[0], consents[0], parents[0]
        _verify_approval(proposal, consent, edge.recorded_at)
        _registered(store, parent, proposal.recorded_at)
        _registered(store, chain[0], proposal.recorded_at)
        _registered(store, parent, edge.recorded_at)
        _registered(store, chain[0], edge.recorded_at)
        _compatible(parent, chain[0])
        if (
            edge.source_policy_id != proposal.source_policy_id
            or edge.target_policy_id != proposal.target_policy_id
            or edge.synthetic != proposal.synthetic
            or edge.synthetic != policy.synthetic
            or proposal.manifest_hash != policy.manifest_hash
            or proposal.semantics_hash != _semantics(chain[0])
            or parent.policy_id in seen
        ):
            raise ValueError("EXACT_POLICY_CONTINUITY_EVIDENCE_REQUIRED")
        seen.add(parent.policy_id)
        chain.insert(0, parent)


def approve_policy_transition(
    store: ExperimentStore,
    proposal: PolicyTransitionProposal,
    *,
    approval: PolicyContinuityApproval,
    now: datetime,
) -> PolicyContinuityReceipt:
    proposal = PolicyTransitionProposal.model_validate(proposal.model_dump())
    approval = PolicyContinuityApproval.model_validate(approval.model_dump())
    _verify_approval(proposal, approval, now)
    events = store.read_as_of(proposal.experiment_id, now)
    if [
        e.decode(PolicyTransitionProposal)
        for e in events
        if e.kind == "PolicyTransitionProposal"
        and e.decode(PolicyTransitionProposal).proposal_id == proposal.proposal_id
    ] != [proposal]:
        raise ValueError("EXACT_REGISTERED_PROPOSAL_REQUIRED")
    policies = [e.decode(ExplorationPolicy) for e in events if e.kind == "ExplorationPolicy"]
    source = [p for p in policies if p.policy_id == proposal.source_policy_id]
    target = [p for p in policies if p.policy_id == proposal.target_policy_id]
    if len(source) != 1 or len(target) != 1:
        raise ValueError("EXACT_REGISTERED_POLICY_REQUIRED")
    _compatible(source[0], target[0])
    _registered(store, source[0], proposal.recorded_at)
    _registered(store, target[0], proposal.recorded_at)
    if (
        proposal.manifest_hash != target[0].manifest_hash
        or proposal.semantics_hash != _semantics(target[0])
        or proposal.synthetic != target[0].synthetic
    ):
        raise ValueError("EXACT_POLICY_PROPOSAL_REQUIRED")
    verified_policy_lineage(store, source[0], now=now)
    verified_policy_lineage(store, target[0], now=now)
    receipt = PolicyContinuityReceipt(
        experiment_id=proposal.experiment_id,
        recorded_at=approval.recorded_at,
        proposal_id=proposal.proposal_id,
        approval_id=approval.approval_id,
        source_policy_id=proposal.source_policy_id,
        target_policy_id=proposal.target_policy_id,
        synthetic=proposal.synthetic,
    )
    with store._transaction() as db:
        rows = db.execute(
            "SELECT document FROM events WHERE experiment_id=? AND kind='PolicyContinuityReceipt'",
            (proposal.experiment_id,),
        ).fetchall()
        existing = [
            PolicyContinuityReceipt.model_validate_json(row[0])
            for row in rows
            if PolicyContinuityReceipt.model_validate_json(row[0]).target_policy_id
            == proposal.target_policy_id
        ]
        if existing:
            if existing == [receipt]:
                return receipt
            raise ValueError("AMBIGUOUS_POLICY_PARENT")
        _insert_record(db, approval, "policy-approval:" + approval.approval_id)
        _insert_record(db, receipt, "policy-continuity:" + proposal.target_policy_id)
    return receipt
