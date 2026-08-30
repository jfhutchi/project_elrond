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

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from quantbot.operations.registry import (
    SafetyClass,
    config_registry,
)
from quantbot.risk.policy_versions import (
    ChangeAuthority,
    FieldChange,
    PolicyVersion,
    ProposalRefused,
    initial_version,
    policy_body,
)
from quantbot.risk.vetoes import PortfolioVetoPolicy

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


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

#: Field types a text form can honestly round-trip.
#:
#: Found by rendering the page rather than by a test: `enabled` is a `frozenset[VetoName]` and
#: `freshness` is a nested policy, and both appeared as editable text boxes containing a Python
#: repr -- `frozenset({<VetoName.EXPOSURE_CONCENTRATION: ...`. Nobody can type that, and every
#: attempt would fail validation, so the box was an invitation to an error.
#:
#: Excluded here rather than filtered in the template, because a form that shows a field it
#: cannot accept is the same class of lie as a console that shows a rule the runtime is not
#: running. These fields are still versioned, still evidence-bearing and still changeable -- by
#: a caller that can express them. They need an editor, and until one exists the honest surface
#: is one that says so.
EDITABLE_TYPES: tuple[type, ...] = (int, str, Decimal)


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

    `authority` and `version` are excluded separately -- they are provenance, not settings, and
    fields a text form cannot express are excluded by type; see `EDITABLE_TYPES`.
    """
    registry = {row.key: row for row in config_registry()}
    shipped = PortfolioVetoPolicy()
    return frozenset(
        key
        for key in PortfolioVetoPolicy.model_fields
        if key in registry
        and registry[key].safety_class is SafetyClass.EVIDENCE_BEARING
        and key not in UNPROPOSABLE_POLICY_FIELDS
        and isinstance(getattr(shipped, key, None), EDITABLE_TYPES)
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
    merged = {**policy_body(current), **dict(proposal.updates)}
    try:
        candidate = PortfolioVetoPolicy.model_validate(merged)
    except Exception as error:  # noqa: BLE001 - surfaced verbatim to the operator
        raise ProposalRefused(f"the proposed policy is not valid: {error}") from error

    before, after = policy_body(current), policy_body(candidate)
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
    merged = {**policy_body(head.policy()), **dict(proposal.updates)}
    candidate = PortfolioVetoPolicy.model_validate(merged)
    return PolicyVersion(
        identity=candidate.identity,
        version=head.version + 1,
        supersedes=head.identity,
        authority=proposal.authority,
        applied_by=proposal.actor,
        applied_at=now,
        reason=proposal.reason,
        body=policy_body(candidate),
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
    before, after = policy_body(head.policy()), dict(target.body)
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
