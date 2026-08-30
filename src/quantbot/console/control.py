"""Changing a threshold means minting a new rule, never editing the old one (#82 slice 3).

Slice 2 could render the system and not touch it. This is the control plane, and the whole
design problem is that Elrond's thresholds are not values -- they are *evidence-bearing*.
`PortfolioVetoPolicy.identity` is written into every durable refusal, so editing a field in place
would make every historical veto record a claim about a rule that no longer exists.

**So an apply is an append.** A proposal produces a *successor version*: new field values, new
identity, new row. The prior version stays exactly as it was, and every refusal measured under it
keeps the authority it was measured under. #72 settled this for mandates; it holds identically
here.

**Rollback is a forward operation.** Reverting to version 2 creates version 5 whose body equals
version 2's. Rewinding would destroy the record that versions 3 and 4 were ever in force, and
evidence gathered under them would become unattributable.

**Optimistic concurrency, because two stale tabs are the normal case.** A proposal names the
version it was composed against. If the head has moved, it is refused with both identities rather
than silently applied on top of a change the author never saw.

**Some settings are not proposable at all, by construction.** Credentials never round-trip
through a browser, and the capital boundary is not an agent's or a console's to move. Those are
refused before validation rather than filtered from a form, because a filtered form is a
convention and a refusal is a rule.

**An agent's choice is never recorded as an operator's.** `applied_by` is written from the actor
the caller declares, and `PolicyAuthority.OPERATOR` is unreachable from any automated path. A
default chosen autonomously must never be rendered as approval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.operations.registry import (
    SafetyClass,
    config_registry,
)
from quantbot.risk.vetoes import PortfolioVetoPolicy

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ProposalRefused(RuntimeError):
    """A proposal that must not be applied, with the reason an operator needs."""


class ChangeAuthority(StrEnum):
    """Who chose the values in a version. Recorded, never inferred.

    Mirrors `PolicyAuthority` deliberately: a console apply and an agent-selected default are
    different provenance even when the numbers are identical, and #72 requires that a reader be
    able to tell them apart.
    """

    #: A human operator applied it through the console.
    OPERATOR = "OPERATOR"
    #: An agent selected it as a versioned project default. Never rendered as approval.
    PROJECT_DEFAULT = "PROJECT_DEFAULT"


class FieldChange(BaseModel):
    """One field moving, with both values kept so a diff needs no second lookup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: Text
    previous: str
    proposed: str

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        if self.previous == self.proposed:
            raise ValueError(
                f"{self.key} is unchanged; a no-op in a change set makes a diff misleading "
                "about what an apply actually did"
            )
        return self


class PolicyVersion(BaseModel):
    """One frozen version of the veto policy, with the provenance of the change that made it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: Text
    version: int = Field(ge=1)
    #: The version this succeeded. `None` only for the first, which is the shipped default.
    supersedes: str | None
    authority: ChangeAuthority
    applied_by: Text
    applied_at: datetime
    reason: Text
    #: The full policy body, so a version can be reconstructed without replaying every change.
    body: Mapping[str, Any]
    changes: tuple[FieldChange, ...] = ()

    def policy(self) -> PortfolioVetoPolicy:
        """Rebuild the frozen policy this version describes."""
        return PortfolioVetoPolicy.model_validate(dict(self.body))

    def rollback_identity(self, version: int) -> str:
        """The identity a rollback restoring this body would carry.

        Distinct from this version's own identity. The *policy* is identical, but the version is
        not the same object: applied at a different time, by a different actor, for a different
        reason. Reusing the restored identity would make two rows indistinguishable and break the
        `supersedes` chain that explains how the system got here.
        """
        payload = {"restores": self.identity, "version": version, "body": dict(self.body)}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"veto-policy-rollback-v{version}-{digest[:16]}"

    def as_row(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "version": self.version,
            "supersedes": self.supersedes,
            "authority": self.authority.value,
            "applied_by": self.applied_by,
            "applied_at": self.applied_at.isoformat(),
            "reason": self.reason,
            "body": json.dumps(dict(self.body), sort_keys=True, separators=(",", ":")),
            "changes": json.dumps(
                [json.loads(item.model_dump_json()) for item in self.changes],
                sort_keys=True,
                separators=(",", ":"),
            ),
        }


class ConfigProposal(BaseModel):
    """A requested change, composed against a specific version of the policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The version this was composed against. A stale one is refused, never merged.
    base_identity: Text
    #: Field name to proposed value, as the browser supplied it. Coerced by the policy model,
    #: which is what validates them -- there is no second schema here to drift from the first.
    updates: Mapping[str, Any]
    actor: Text
    reason: Text
    authority: ChangeAuthority = ChangeAuthority.PROJECT_DEFAULT

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        if not self.updates:
            raise ValueError("a proposal with no updates is not a change")
        return self


#: Fields on the veto policy that a console may never propose, whatever their safety class.
#:
#: `authority` records *who chose these numbers*. Letting a proposal set it would let an agent
#: write `OPERATOR` and render its own default as approval -- the exact thing #72 forbids. It is
#: derived from the version row instead, where it is written from the declared actor.
#:
#: `version` is minted by the apply path. A proposal that could set it could rewrite the
#: ordering of the history it is appending to.
UNPROPOSABLE_POLICY_FIELDS: frozenset[str] = frozenset({"authority", "version"})


def proposable_fields() -> frozenset[str]:
    """Which settings a console may propose at all.

    Scoped to the veto policy's **own** evidence-bearing fields, which is two filters doing two
    different jobs:

    * the **safety class** keeps the capital boundary and the credentials out. A console may
      propose evidence-bearing thresholds and nothing else, so `KILL_SWITCH` and an API key are
      unreachable from here by classification rather than by a list someone has to maintain.
    * the **scoping** keeps other frozen policies out. The registry reports every evidence-bearing
      setting across subsystems, and a proposal naming `cadence` would be composed against a
      policy that does not have it.

    Either alone would currently hold the capital boundary, and that redundancy is deliberate --
    but it is worth recording *how* it was established, because the first version of this
    function got it wrong. It also excluded two hand-maintained sets, `CAPITAL_BOUNDARY_FIELDS`
    and `IDENTIFYING_FIELDS`, and the docstring credited them with keeping credentials out.
    Mutation testing showed they did no such thing: `IDENTIFYING_FIELDS` is `EXPECTED_ACCOUNT_ID`
    alone, so all four Alpaca keys were held out by the two filters above and never by the line
    claiming to hold them. Deleting both changed nothing. They are gone rather than kept as
    defence in depth, because a guard that is named for a job it does not do is worse than no
    guard: it is what a future reader will trust when they widen this function.

    `authority` and `version` are excluded separately -- they are provenance, not settings.
    """
    registry = {row.key: row for row in config_registry()}
    return frozenset(
        key
        for key in PortfolioVetoPolicy.model_fields
        if key in registry
        and registry[key].safety_class is SafetyClass.EVIDENCE_BEARING
        and key not in UNPROPOSABLE_POLICY_FIELDS
    )


def _body(policy: PortfolioVetoPolicy) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads(policy.model_dump_json())
    return decoded


def initial_version(*, now: datetime, actor: str = "project-default") -> PolicyVersion:
    """The shipped policy as version 1, so history starts where the system started.

    Recorded rather than implied. Without it the first apply would have nothing to supersede, and
    every refusal already measured under the shipped defaults would sit outside the version
    history that is supposed to explain them.
    """
    policy = PortfolioVetoPolicy()
    return PolicyVersion(
        identity=policy.identity,
        version=1,
        supersedes=None,
        authority=ChangeAuthority.PROJECT_DEFAULT,
        applied_by=actor,
        applied_at=now,
        reason="the shipped policy, recorded so version history starts where the system did",
        body=_body(policy),
    )


def validate_proposal(proposal: ConfigProposal, *, head: PolicyVersion) -> tuple[FieldChange, ...]:
    """Check a proposal without applying it, returning the diff it would produce.

    Separated from `apply_proposal` because #82 asks for validate-without-applying, and because
    a diff an operator can read before committing is the difference between a control plane and a
    form that does something.
    """
    if proposal.base_identity != head.identity:
        raise ProposalRefused(
            f"this change was composed against {proposal.base_identity} and the current policy "
            f"is {head.identity}; applying it would silently overwrite a change its author never "
            "saw. Reload and re-apply"
        )

    allowed = proposable_fields()
    forbidden = sorted(set(proposal.updates) - allowed)
    if forbidden:
        raise ProposalRefused(
            f"{forbidden} cannot be changed from a console: credentials never round-trip through "
            "a browser, and the capital boundary is not this surface's to move"
        )

    current = head.policy()
    merged = {**_body(current), **dict(proposal.updates)}
    try:
        candidate = PortfolioVetoPolicy.model_validate(merged)
    except Exception as error:  # noqa: BLE001 - surfaced verbatim to the operator
        raise ProposalRefused(f"the proposed policy is not valid: {error}") from error

    before, after = _body(current), _body(candidate)
    changes = tuple(
        FieldChange(key=key, previous=str(before[key]), proposed=str(after[key]))
        for key in sorted(proposal.updates)
        if str(before[key]) != str(after[key])
    )
    if not changes:
        raise ProposalRefused(
            "every proposed value already holds; applying it would mint a version identical to "
            "the current one and make the history longer without making it more informative"
        )
    return changes


def apply_proposal(
    proposal: ConfigProposal, *, head: PolicyVersion, now: datetime
) -> PolicyVersion:
    """Mint the successor version this proposal describes. Never edits `head`.

    The returned version carries a new `identity`, so every refusal already recorded against the
    old one keeps pointing at the rule that actually produced it.
    """
    changes = validate_proposal(proposal, head=head)
    merged = {**_body(head.policy()), **dict(proposal.updates)}
    candidate = PortfolioVetoPolicy.model_validate(merged)
    return PolicyVersion(
        identity=candidate.identity,
        version=head.version + 1,
        supersedes=head.identity,
        authority=proposal.authority,
        applied_by=proposal.actor,
        applied_at=now,
        reason=proposal.reason,
        body=_body(candidate),
        changes=changes,
    )


def rollback_to(
    target: PolicyVersion, *, head: PolicyVersion, actor: str, reason: str, now: datetime
) -> PolicyVersion:
    """Restore an earlier body by minting a *new* version, never by rewinding.

    Rewinding would delete the record that the intervening versions were ever in force, and
    every refusal measured under them would become unattributable. So a rollback moves forward:
    the numbers return, the history does not.
    """
    if target.identity == head.identity:
        raise ProposalRefused(
            f"{target.identity} is already in force; rolling back to it would mint an identical "
            "version and record a change that did not happen"
        )
    before, after = _body(head.policy()), dict(target.body)
    changes = tuple(
        FieldChange(key=key, previous=str(before[key]), proposed=str(after[key]))
        for key in sorted(after)
        if str(before.get(key)) != str(after[key])
    )
    return PolicyVersion(
        identity=target.rollback_identity(head.version + 1),
        version=head.version + 1,
        supersedes=head.identity,
        authority=ChangeAuthority.PROJECT_DEFAULT,
        applied_by=actor,
        applied_at=now,
        reason=f"rollback to {target.identity}: {reason}",
        body=after,
        changes=changes,
    )


__all__ = [
    "UNPROPOSABLE_POLICY_FIELDS",
    "ChangeAuthority",
    "ConfigProposal",
    "FieldChange",
    "PolicyVersion",
    "ProposalRefused",
    "apply_proposal",
    "initial_version",
    "proposable_fields",
    "rollback_to",
    "validate_proposal",
]
