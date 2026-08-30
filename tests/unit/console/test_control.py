"""Changing a threshold means minting a new rule (#82 slice 3).

Elrond's thresholds are not values, they are evidence-bearing: `PortfolioVetoPolicy.identity` is
written into every durable refusal. So the tests below are mostly about what an apply must *not*
do -- edit in place, rewind, merge over a change its author never saw, or let an agent record its
own choice as an operator's.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantbot.console.control import (
    UNPROPOSABLE_POLICY_FIELDS,
    ChangeAuthority,
    ConfigProposal,
    FieldChange,
    ProposalRefused,
    apply_proposal,
    initial_version,
    proposable_fields,
    rollback_to,
    validate_proposal,
)
from quantbot.risk.vetoes import PortfolioVetoPolicy

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def head():
    return initial_version(now=NOW)


def proposal(**overrides: object) -> ConfigProposal:
    fields: dict[str, object] = {
        "base_identity": head().identity,
        "updates": {"correlation_threshold": "0.85"},
        "actor": "hutch",
        "reason": "wider clusters after re-measuring",
    }
    fields.update(overrides)
    return ConfigProposal(**fields)  # type: ignore[arg-type]


# --- an apply is an append -------------------------------------------------------------------


def test_applying_a_change_leaves_the_previous_version_exactly_as_it_was() -> None:
    """The whole design problem. Editing in place would make every historical refusal a claim
    about a rule that no longer exists.
    """
    first = head()
    second = apply_proposal(proposal(), head=first, now=NOW)

    assert second.identity != first.identity
    assert second.supersedes == first.identity
    assert second.version == first.version + 1
    assert first.policy().correlation_threshold == Decimal("0.90")
    assert second.policy().correlation_threshold == Decimal("0.85")


def test_the_new_identity_is_the_one_future_refusals_will_cite() -> None:
    """A version whose identity did not move would be indistinguishable from its predecessor."""
    second = apply_proposal(proposal(), head=head(), now=NOW)

    assert second.identity == second.policy().identity
    assert second.identity != PortfolioVetoPolicy().identity


def test_a_diff_can_be_read_before_anything_is_committed() -> None:
    """Validate-without-applying is what separates a control plane from a form that does things."""
    changes = validate_proposal(proposal(), head=head())

    assert len(changes) == 1
    assert changes[0].key == "correlation_threshold"
    assert (changes[0].previous, changes[0].proposed) == ("0.90", "0.85")


def test_a_no_op_change_is_refused_rather_than_recorded() -> None:
    """A version identical to its predecessor makes the history longer, not more informative."""
    with pytest.raises(ProposalRefused, match="already hold"):
        validate_proposal(proposal(updates={"correlation_threshold": "0.90"}), head=head())

    with pytest.raises(ValueError, match="unchanged"):
        FieldChange(key="correlation_threshold", previous="0.90", proposed="0.90")


def test_an_invalid_policy_is_refused_with_the_reason() -> None:
    """The policy model is the validator; there is no second schema here to drift from it."""
    with pytest.raises(ProposalRefused, match="not valid"):
        validate_proposal(proposal(updates={"correlation_threshold": "5.0"}), head=head())


# --- optimistic concurrency ------------------------------------------------------------------


def test_a_proposal_composed_against_a_stale_version_is_refused() -> None:
    """Two stale tabs are the normal case, not the exception.

    Applying on top of a change the author never saw is the silent failure this prevents, so the
    refusal names both identities rather than just saying no.
    """
    first = head()
    second = apply_proposal(proposal(), head=first, now=NOW)
    stale = proposal(base_identity=first.identity, updates={"max_daily_loss_bps": 400})

    with pytest.raises(ProposalRefused) as refusal:
        apply_proposal(stale, head=second, now=NOW)

    assert first.identity in str(refusal.value)
    assert second.identity in str(refusal.value)
    assert "never saw" in str(refusal.value)


# --- what may never be proposed ----------------------------------------------------------------


def test_an_agent_cannot_propose_its_own_authority() -> None:
    """"A default is not an approval" (#72), enforced rather than documented.

    `authority` records *who chose these numbers*. A proposal able to set it could write
    `OPERATOR` over an agent's own choice, which is exactly the provenance laundering #72
    forbids. It is derived from the version row instead.
    """
    assert "authority" in UNPROPOSABLE_POLICY_FIELDS
    assert "authority" not in proposable_fields()

    with pytest.raises(ProposalRefused, match="cannot be changed from a console"):
        validate_proposal(proposal(updates={"authority": "OPERATOR"}), head=head())


def test_the_version_number_cannot_be_proposed() -> None:
    """A proposal able to set it could rewrite the ordering of the history it appends to."""
    with pytest.raises(ProposalRefused, match="cannot be changed from a console"):
        validate_proposal(proposal(updates={"version": 99}), head=head())


def test_no_capital_boundary_or_credential_field_is_ever_proposable() -> None:
    """The invariant, asserted against the registry rather than a hand-listed sample.

    This test names a property, not a mechanism, and that is deliberate: two independent filters
    in `proposable_fields` each hold it on their own, so removing either one leaves this green.
    Removing **both** turns it red, which is the mutation that matters and the one that would
    catch a future edit widening this surface.

    Asserting against the registry rather than a sample is what makes that work. A test listing
    `KILL_SWITCH` and one API key would keep passing while a newly added credential became
    proposable; this one fails the moment any key of either class appears in the set.
    """
    from quantbot.operations.registry import SafetyClass, config_registry

    registry = {row.key: row for row in config_registry()}
    fields = proposable_fields()

    unsafe = {
        key
        for key in fields
        if registry[key].safety_class
        in {SafetyClass.CAPITAL_BOUNDARY, SafetyClass.CREDENTIAL}
    }
    assert not unsafe, f"{sorted(unsafe)} are proposable from a console"


def test_a_field_outside_the_policy_is_refused_by_name() -> None:
    """Whatever the reason it is excluded, a proposal naming one is refused rather than ignored.

    Silently dropping an unknown key would apply a change set that is not the one submitted,
    which is worse than refusing: the operator sees success and gets something else.
    """
    for field in ("KILL_SWITCH", "TRADING_MODE", "ALPACA_PAPER_API_KEY"):
        with pytest.raises(ProposalRefused, match="cannot be changed from a console"):
            validate_proposal(proposal(updates={field: "x"}), head=head())


def test_only_the_policys_own_fields_are_proposable() -> None:
    """The registry spans subsystems; a proposal naming another policy's field is meaningless.

    Without this scoping, `cadence` and `calendar` -- which belong to `MarketFreshnessPolicy` --
    appeared proposable against the veto policy, which does not have them.
    """
    fields = proposable_fields()

    assert fields <= set(PortfolioVetoPolicy.model_fields)
    assert fields, "an empty set would satisfy the assertion above"
    assert "correlation_threshold" in fields
    assert "cadence" not in fields


# --- rollback is forward -----------------------------------------------------------------------


def test_rolling_back_mints_a_new_version_rather_than_rewinding() -> None:
    """Rewinding would delete the record that the intervening versions were ever in force.

    Evidence gathered under them would become unattributable, which is worse than a longer
    history: the numbers return, the history does not.
    """
    first = head()
    second = apply_proposal(proposal(), head=first, now=NOW)
    third = rollback_to(first, head=second, actor="hutch", reason="reverting", now=NOW)

    assert third.version == 3, "a rollback moves forward"
    assert third.supersedes == second.identity
    assert third.policy().correlation_threshold == first.policy().correlation_threshold
    assert third.identity != first.identity, (
        "a restored body is not the same version: different time, actor and reason"
    )
    assert "rollback to" in third.reason


def test_rolling_back_to_the_version_already_in_force_is_refused() -> None:
    """It would record a change that did not happen."""
    first = head()

    with pytest.raises(ProposalRefused, match="already in force"):
        rollback_to(first, head=first, actor="hutch", reason="noop", now=NOW)


# --- provenance --------------------------------------------------------------------------------


def test_the_version_records_who_applied_it_and_why() -> None:
    """Every mutation auditable with actor, reason, prior value and new value."""
    second = apply_proposal(
        proposal(authority=ChangeAuthority.OPERATOR), head=head(), now=NOW
    )
    row = second.as_row()

    assert row["applied_by"] == "hutch"
    assert row["authority"] == "OPERATOR"
    assert row["supersedes"] == head().identity
    assert "wider clusters" in str(row["reason"])
    assert "0.90" in str(row["changes"]) and "0.85" in str(row["changes"])


def test_an_automated_apply_defaults_to_project_default_authority() -> None:
    """An agent's choice is never recorded as an operator's unless the caller says so."""
    automated = apply_proposal(proposal(), head=head(), now=NOW)

    assert automated.authority is ChangeAuthority.PROJECT_DEFAULT


def test_the_first_version_is_recorded_so_history_starts_where_the_system_did() -> None:
    """Without it, refusals already measured under the shipped defaults sit outside the history
    that is supposed to explain them.
    """
    first = head()

    assert first.version == 1
    assert first.supersedes is None
    assert first.identity == PortfolioVetoPolicy().identity
    assert first.authority is ChangeAuthority.PROJECT_DEFAULT


def test_a_version_can_be_rebuilt_from_its_stored_body_alone() -> None:
    """So reading history never requires replaying every change that produced it."""
    second = apply_proposal(proposal(), head=head(), now=NOW)
    rebuilt = second.policy()

    assert rebuilt.identity == second.identity
    assert rebuilt.correlation_threshold == Decimal("0.85")


def test_only_fields_a_text_form_can_express_are_offered() -> None:
    """Found by rendering the page, not by a test, which is the point of recording it here.

    `enabled` is a `frozenset[VetoName]` and `freshness` is a nested policy. Both were offered as
    editable text boxes containing a Python repr -- `frozenset({<VetoName.EXPOSURE_CONCENTRATION:
    ...` -- which nobody can type and which would fail validation on every attempt. A form that
    shows a field it cannot accept is the same class of lie as a console showing a rule the
    runtime is not running.

    They remain versioned, evidence-bearing and changeable by a caller that can express them.
    What is refused is pretending a text box is that caller.
    """
    shipped = PortfolioVetoPolicy()
    fields = proposable_fields()

    assert "enabled" not in fields
    assert "freshness" not in fields
    assert fields, "an empty set would satisfy the assertions above"
    for key in fields:
        value = getattr(shipped, key)
        assert isinstance(value, int | str | Decimal), f"{key} is {type(value).__name__}"


def test_the_scalar_thresholds_are_still_offered() -> None:
    """The exclusion must be by expressibility, not a convenient way to narrow the surface.

    A rule that quietly dropped the numbers an operator actually needs to tune would make the
    control plane useless while looking safer.
    """
    fields = proposable_fields()

    assert {"correlation_threshold", "max_daily_loss_bps", "max_net_exposure_bps"} <= fields
