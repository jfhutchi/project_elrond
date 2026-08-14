from __future__ import annotations

import pytest

from quantbot.domain import (
    ALLOWED_TRANSITIONS,
    IntentState,
    InvalidOrderTransition,
    transition_order,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IntentState.RISK_APPROVED, IntentState.ORDER_CREATED),
        (IntentState.ORDER_CREATED, IntentState.SUBMITTING),
        (IntentState.SUBMITTING, IntentState.BROKER_ACCEPTED),
        (IntentState.SUBMITTING, IntentState.REJECTED),
        (IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN),
        (IntentState.SUBMITTING, IntentState.ERROR),
        (IntentState.SUBMISSION_UNKNOWN, IntentState.SUBMITTING),
        (IntentState.SUBMISSION_UNKNOWN, IntentState.BROKER_ACCEPTED),
        (IntentState.SUBMISSION_UNKNOWN, IntentState.REJECTED),
        (IntentState.SUBMISSION_UNKNOWN, IntentState.ERROR),
        (IntentState.BROKER_ACCEPTED, IntentState.PARTIALLY_FILLED),
        (IntentState.BROKER_ACCEPTED, IntentState.FILLED),
        (IntentState.BROKER_ACCEPTED, IntentState.REJECTED),
        (IntentState.BROKER_ACCEPTED, IntentState.CANCEL_PENDING),
        (IntentState.BROKER_ACCEPTED, IntentState.CANCELLED),
        (IntentState.BROKER_ACCEPTED, IntentState.EXPIRED),
        (IntentState.BROKER_ACCEPTED, IntentState.ERROR),
        (IntentState.PARTIALLY_FILLED, IntentState.PARTIALLY_FILLED),
        (IntentState.PARTIALLY_FILLED, IntentState.FILLED),
        (IntentState.PARTIALLY_FILLED, IntentState.CANCEL_PENDING),
        (IntentState.PARTIALLY_FILLED, IntentState.CANCELLED),
        (IntentState.PARTIALLY_FILLED, IntentState.EXPIRED),
        (IntentState.PARTIALLY_FILLED, IntentState.ERROR),
        (IntentState.CANCEL_PENDING, IntentState.CANCELLED),
        (IntentState.CANCEL_PENDING, IntentState.PARTIALLY_FILLED),
        (IntentState.CANCEL_PENDING, IntentState.FILLED),
        (IntentState.CANCEL_PENDING, IntentState.ERROR),
    ],
)
def test_allowed_order_lifecycle_transitions(current: IntentState, target: IntentState) -> None:
    assert transition_order(current, target) is target


@pytest.mark.parametrize("state", list(IntentState))
def test_same_state_transition_is_idempotent(state: IntentState) -> None:
    assert transition_order(state, state) is state


@pytest.mark.parametrize(
    "terminal_state",
    [IntentState.FILLED, IntentState.REJECTED, IntentState.CANCELLED, IntentState.EXPIRED],
)
def test_terminal_states_cannot_move(terminal_state: IntentState) -> None:
    assert ALLOWED_TRANSITIONS[terminal_state] == frozenset()

    with pytest.raises(InvalidOrderTransition, match=terminal_state.value):
        transition_order(terminal_state, IntentState.ERROR)


def test_backward_transition_is_rejected() -> None:
    with pytest.raises(InvalidOrderTransition) as exc_info:
        transition_order(IntentState.BROKER_ACCEPTED, IntentState.SUBMITTING)

    assert exc_info.value.current is IntentState.BROKER_ACCEPTED
    assert exc_info.value.target is IntentState.SUBMITTING
