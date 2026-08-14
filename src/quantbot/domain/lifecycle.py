"""Explicit order-intent lifecycle transitions."""

from collections.abc import Mapping
from types import MappingProxyType

from quantbot.domain.enums import IntentState
from quantbot.domain.errors import InvalidOrderTransition

ALLOWED_TRANSITIONS: Mapping[IntentState, frozenset[IntentState]] = MappingProxyType(
    {
        IntentState.RISK_APPROVED: frozenset(
            {
                IntentState.ORDER_CREATED,
                IntentState.ERROR,
            }
        ),
        IntentState.ORDER_CREATED: frozenset(
            {
                IntentState.SUBMITTING,
                IntentState.CANCELLED,
                IntentState.ERROR,
            }
        ),
        IntentState.SUBMITTING: frozenset(
            {
                IntentState.BROKER_ACCEPTED,
                IntentState.REJECTED,
                IntentState.SUBMISSION_UNKNOWN,
                IntentState.ERROR,
            }
        ),
        IntentState.BROKER_ACCEPTED: frozenset(
            {
                IntentState.PARTIALLY_FILLED,
                IntentState.FILLED,
                IntentState.REJECTED,
                IntentState.CANCEL_PENDING,
                IntentState.CANCELLED,
                IntentState.EXPIRED,
                IntentState.ERROR,
            }
        ),
        IntentState.PARTIALLY_FILLED: frozenset(
            {
                IntentState.PARTIALLY_FILLED,
                IntentState.FILLED,
                IntentState.CANCEL_PENDING,
                IntentState.CANCELLED,
                IntentState.EXPIRED,
                IntentState.ERROR,
            }
        ),
        IntentState.CANCEL_PENDING: frozenset(
            {
                IntentState.CANCELLED,
                IntentState.PARTIALLY_FILLED,
                IntentState.FILLED,
                IntentState.ERROR,
            }
        ),
        IntentState.SUBMISSION_UNKNOWN: frozenset(
            {
                IntentState.SUBMITTING,
                IntentState.BROKER_ACCEPTED,
                IntentState.REJECTED,
                IntentState.ERROR,
            }
        ),
        IntentState.FILLED: frozenset(),
        IntentState.REJECTED: frozenset(),
        IntentState.CANCELLED: frozenset(),
        IntentState.EXPIRED: frozenset(),
        IntentState.ERROR: frozenset(),
    }
)


def transition_order(current: IntentState, target: IntentState) -> IntentState:
    """Validate and return an order state transition.

    Repeating any state is intentionally idempotent. All other transitions must
    be listed explicitly so unexpected broker events fail closed.
    """
    if current is target:
        return target
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidOrderTransition(current, target)
    return target
