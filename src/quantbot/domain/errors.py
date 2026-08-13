"""Domain-specific exceptions."""

from quantbot.domain.enums import IntentState


class InvalidOrderTransition(ValueError):
    """Raised when an order intent attempts an illegal lifecycle transition."""

    def __init__(self, current: IntentState, target: IntentState) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid order transition: {current.value} -> {target.value}")
