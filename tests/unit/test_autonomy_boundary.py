"""Where human authorization is still required, asserted rather than described (#72).

#72 removed a set of human gates from the non-live ladder because they had become a development
deadlock: paper qualification waited on a ratified mandate, and so did the stage whose meaning is
"a human should look at this" -- which made review a prerequisite for its own eligibility.

Loosening a boundary is the moment to pin what is left of it. These tests assert the surviving
line: **classifications are autonomous, actions that touch real money are not.**

They are written against reachability, not against wording. A grep for "operator" would flag the
comments explaining why a boundary exists, and would train the next change to delete the
explanation rather than the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantbot.research.mandate import (
    DEFAULT_MANDATE_PATH,
    USABLE_STATUSES,
    EconomicObjective,
    MandateStatus,
    load_economic_objective,
)
from quantbot.research.promotion import ALLOWED_PROMOTIONS, Stage

REPOSITORY = Path(__file__).resolve().parents[2]


def test_no_autonomous_path_can_produce_a_ratified_mandate() -> None:
    """`RATIFIED` means a person accepted this. A status an agent can write is not a signature.

    `PROJECT_DEFAULT` exists so nothing ever needs to reach for `RATIFIED` to get work done,
    which is what makes keeping it unreachable cost-free rather than obstructive.
    """
    payload = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH).model_dump(mode="json")

    # Promoting a default to ratified requires naming a human and a date...
    with pytest.raises(ValueError, match="records who ratified it"):
        EconomicObjective.model_validate({**payload, "status": MandateStatus.RATIFIED.value})

    # ...and the obvious workaround, signing as the tool, is refused by name.
    for identity in ("claude", "claude-opus-5", "gpt-5.6 sol", "an agent", "a bot", "automation"):
        with pytest.raises(ValueError, match="automated ratifier"):
            EconomicObjective.model_validate(
                {
                    **payload,
                    "status": MandateStatus.RATIFIED.value,
                    "ratified_by": identity,
                    "ratified_at": "2026-08-29",
                }
            )


def test_the_shipped_mandate_is_a_default_and_never_claims_approval() -> None:
    """The label that replaced the gate has to actually be on the object."""
    frozen = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)

    assert frozen.status is MandateStatus.PROJECT_DEFAULT
    assert frozen.usable()
    assert not frozen.ratified()
    assert frozen.ratified_by is None and frozen.ratified_at is None
    assert "not approved by the operator" in frozen.authority


def test_a_usable_mandate_is_frozen_and_a_draft_is_not() -> None:
    """What makes a bar honest is that it could not move after the evidence arrived.

    Both usable statuses hash identically over their economic content, so a `PROJECT_DEFAULT` is
    exactly as pinned as a `RATIFIED` one -- which is the argument for letting it qualify.
    """
    assert USABLE_STATUSES == {MandateStatus.PROJECT_DEFAULT, MandateStatus.RATIFIED}
    assert MandateStatus.PROVISIONAL not in USABLE_STATUSES
    assert MandateStatus.SUPERSEDED not in USABLE_STATUSES

    default = load_economic_objective(REPOSITORY / DEFAULT_MANDATE_PATH)
    signed = default.model_copy(
        update={
            "status": MandateStatus.RATIFIED,
            "ratified_by": "hutch",
            "ratified_at": default.authored_at,
        }
    )
    assert signed.content_hash == default.content_hash, (
        "a signature does not change which numbers were frozen, only who stands behind them"
    )
    assert signed.authority != default.authority, "and the reader must still be able to tell"


def test_the_top_research_stage_is_terminal_and_grants_nothing() -> None:
    """`LIVE_REVIEW_ELIGIBLE` says a package is worth reading. It is not an authorization.

    This is the assertion that makes opening the stage to project defaults safe: reaching it
    cannot lead anywhere on its own, so an autonomous system can classify freely without that
    classification ever becoming permission.
    """
    assert ALLOWED_PROMOTIONS[Stage.LIVE_REVIEW_ELIGIBLE] == frozenset(), (
        "a destination past human review would make the review skippable"
    )
    assert not any(stage.value == "LIVE" for stage in Stage), (
        "there is no live stage on the research ladder, and adding one would put real capital "
        "at the end of a path an agent can walk"
    )


def test_live_trading_stays_shut_and_a_mode_switch_is_not_enough() -> None:
    """The boundary #72 explicitly did not move.

    Everything above loosened a *classification*. This is an action, and switching the mode is
    not sufficient on its own -- the acknowledgement is a separate deliberate act, which is what
    keeps an agent from reaching live capital by editing one field.
    """
    from quantbot.config import Settings, TradingMode, validate_trading_safety

    assert Settings().TRADING_MODE is TradingMode.PAPER, "paper is the default mode"
    assert Settings().LIVE_TRADING_ACKNOWLEDGED is False

    switched = Settings(TRADING_MODE=TradingMode.LIVE)
    verdict = validate_trading_safety(switched, reported_account_id=None)

    assert not verdict.allowed
    assert "LIVE_TRADING_NOT_ACKNOWLEDGED" in verdict.reasons, (
        "flipping the mode must not by itself clear the live-capital boundary"
    )
