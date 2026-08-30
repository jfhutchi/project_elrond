"""What rule was in force, and when. The durable record a refusal's identity resolves to.

Elrond's thresholds are evidence-bearing: `PortfolioVetoPolicy.identity` is hashed into every
durable refusal. So a change is an append, never an edit -- editing a field in place would turn
every historical veto into a claim about a rule that no longer exists.

This module holds the *record*: the version, its provenance, and the chain linking it to what it
superseded. The workflow that composes and validates a change lives in `quantbot.console.control`,
because that is a console concern. The split is not cosmetic: `quantbot.operations` imports this
package, so anything the trading loop needs to read must not sit behind an import of the web
console. The runtime has to be able to answer "which rule am I running under" with the console
package absent entirely.

**Rollback is a forward operation.** Reverting to version 2 creates version 5 whose *body* equals
version 2's. Note what that means for identity: the version row is new, but `policy().identity`
is the same as version 2's, because the rule genuinely is the same rule. Refusals recorded under
both are measured under identical thresholds and are legitimately comparable; the version rows
are what distinguish *when* each was in force.

**An agent's choice is never recorded as an operator's.** `applied_by` is written from the actor
the caller declares, and `ChangeAuthority.OPERATOR` is unreachable from any automated path (#72).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.risk.vetoes import PortfolioVetoPolicy

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ProposalRefused(RuntimeError):
    """A proposal that must not be applied, with the reason an operator needs."""


class PolicyUnreadable(RuntimeError):
    """A version is recorded but cannot be rebuilt into a policy. Fatal, never a fallback."""


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


def policy_body(policy: PortfolioVetoPolicy) -> dict[str, Any]:
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
        body=policy_body(policy),
    )




__all__ = [
    "ChangeAuthority",
    "FieldChange",
    "PolicySource",
    "PolicyUnreadable",
    "PolicyVersion",
    "ProposalRefused",
    "active_policy_version",
    "initial_version",
    "policy_body",
]


class PolicySource(StrEnum):
    """Where the policy the runtime is running under came from."""

    #: Read from the recorded head. The normal case once anything has been applied.
    RECORDED = "RECORDED"
    #: Nothing was recorded, so the shipped defaults were written as version 1. Happens once.
    BOOTSTRAPPED = "BOOTSTRAPPED"


def active_policy_version(
    repository: Any, *, now: datetime, actor: str = "project-default"
) -> tuple[PolicyVersion, PolicySource]:
    """The version currently in force, recording the shipped defaults as version 1 if none is.

    The runtime calls this instead of constructing `PortfolioVetoPolicy()` directly, which is the
    step that makes the whole version chain reachable. Without it every apply would produce a
    correct, fully audited history that changed nothing about what the system does -- a control
    correct in isolation and never reached in production, which is the failure class this project
    keeps finding.

    **Bootstrapping is not a default, it is a record.** An empty table means the system has been
    running under the shipped defaults all along, so version 1 states that as a fact with
    `PROJECT_DEFAULT` authority. The alternative -- starting the history at whatever the first
    console edit happened to be -- would leave every refusal recorded before that edit citing an
    identity the history cannot explain.

    **A stored head that will not rebuild is fatal rather than ignored.** Falling back to the
    defaults would mean trading under a rule nobody chose while a different one sits recorded, and
    every refusal produced would cite an identity that does not match what was enforced. Refusing
    to start costs a missed session; the fallback costs the meaning of the evidence.
    """
    version = recorded_policy_version(repository)
    if version is None:
        first = initial_version(now=now, actor=actor)
        repository.record_policy_version(first.as_row())
        return first, PolicySource.BOOTSTRAPPED
    return version, PolicySource.RECORDED


def recorded_policy_version(repository: Any) -> PolicyVersion | None:
    """The recorded head rebuilt, or None if nothing is recorded. Never writes.

    Split out so a read-only surface can ask what rule is in force without bootstrapping one.
    The console is read-only by construction -- asserted against its route table -- and a display
    that wrote a row to answer a GET would break that for the sake of a label.
    """
    head = repository.policy_version_head()
    if head is None:
        return None
    try:
        version = PolicyVersion.model_validate(
            {
                "identity": head["identity"],
                "version": head["version"],
                "supersedes": head["supersedes"],
                "authority": head["authority"],
                "applied_by": head["applied_by"],
                "applied_at": head["applied_at"],
                "reason": head["reason"],
                "body": json.loads(str(head["body"])),
                "changes": json.loads(str(head["changes"])),
            }
        )
        version.policy()
    except Exception as error:
        raise PolicyUnreadable(
            f"the recorded veto policy {head.get('identity')!r} cannot be rebuilt, so the rule "
            "the system would enforce is not the rule it has on record; refusing to run under an "
            "unknown policy rather than falling back to the defaults"
        ) from error
    return version
