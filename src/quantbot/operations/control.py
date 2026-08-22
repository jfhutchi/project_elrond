"""A small operator control surface, so the kill switch can be worked from the dashboard (#38).

The dashboard is a generated HTML file served by `python -m http.server`. That server can only
read files, which is why it was the right tool -- but it means the page can show the kill switch
and not touch it. This module is the smallest thing that lets a browser act, without turning the
page into an application.

**Engaging and clearing are deliberately asymmetric, and that asymmetry is the design.**

Engaging is unconditional. Stopping is always safe, it is the direction an operator reaches for
when something looks wrong, and a stop button that can refuse is a stop button that fails when
it matters. It needs a reason, because an unexplained halt is one the next person clears out of
frustration.

Clearing goes through `measure_readiness` and refuses unless the conditions actually hold. It
cannot be talked into it: the endpoint has no way to construct `MeasuredReadiness`, so the worst
a caller can do is ask, and the answer comes from probing the broker and the ledger. This is why
`readiness.py` exists at all -- exposing a clear button over a network on top of six booleans
read from a dotenv would have been a control with a verification step drawn on it.

**A token is required, and it is not authentication.** It stops a stray request or a device on
the LAN blundering into the trading system; it does not make this safe to expose beyond the LAN,
and the deployment notes say so rather than implying otherwise. Compared with a byte-for-byte
constant so a wrong token cannot be narrowed down by timing.

**It never returns broker credentials or ledger contents.** The response says what the switch is
and why a clear was refused, and nothing else. The static file server holds no secrets and this
holds no data.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from quantbot.operations.kill_switch import (
    KillSwitchClearBlocked,
    KillSwitchController,
    MeasuredReadiness,
)

#: Actions this surface accepts. There is no "disable risk", no "submit order", no "resolve
#: incident": every one of those is a decision that should cost more than a click, and a control
#: surface grows by accretion unless the list is short and deliberate.
ENGAGE = "engage"
CLEAR = "clear"


class ControlRefused(RuntimeError):
    """Raised when a control request cannot be honoured. Carries no internal detail."""

    def __init__(self, reason: str, *, status: int = 400) -> None:
        self.status = status
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class ControlResult:
    status: int
    payload: Mapping[str, object]

    def body(self) -> bytes:
        return json.dumps(dict(self.payload), sort_keys=True).encode("utf-8")


class OperatorControl:
    """Handles kill-switch requests from the dashboard.

    Transport-free on purpose. It takes a decoded action and returns a result, so the whole of
    it is testable without binding a socket -- and the HTTP layer above stays thin enough to
    have nothing worth testing.
    """

    def __init__(
        self,
        controller: KillSwitchController,
        measure: Callable[[], Awaitable[MeasuredReadiness]],
        *,
        token: str,
        clock: Callable[[], datetime],
    ) -> None:
        if len(token.strip()) < 16:
            # Short tokens invite guessing, and this one guards the control that stops trading.
            raise ValueError("the control token must be at least 16 characters")
        self._controller = controller
        self._measure = measure
        self._token = token.strip()
        self._clock = clock

    def authorized(self, presented: str | None) -> bool:
        """Constant-time comparison, so a wrong token cannot be narrowed down by timing."""
        if presented is None:
            return False
        return hmac.compare_digest(presented, self._token)

    async def handle(
        self, action: str, *, reason: str, presented_token: str | None
    ) -> ControlResult:
        if not self.authorized(presented_token):
            # Deliberately identical for a missing and a wrong token: distinguishing them tells
            # a caller which half they got right.
            raise ControlRefused("not authorized", status=403)
        cleaned = reason.strip()
        if not cleaned:
            raise ControlRefused("a reason is required")
        now = self._clock()

        if action == ENGAGE:
            # Unconditional. A stop that can refuse is a stop that fails when it matters.
            state = self._controller.engage(reason=f"operator: {cleaned}", updated_at=now)
            return ControlResult(
                status=200,
                payload={"engaged": state.engaged, "reason": state.reason, "action": ENGAGE},
            )

        if action == CLEAR:
            measured = await self._measure()
            try:
                state = self._controller.clear(
                    reason=f"operator: {cleaned}", measured=measured, updated_at=now
                )
            except KillSwitchClearBlocked as blocked:
                # A refusal is a successful answer to the question that was asked, and the
                # reasons are the useful part -- an operator who is told only "no" starts
                # looking for a way around it.
                return ControlResult(
                    status=409,
                    payload={
                        "engaged": True,
                        "action": CLEAR,
                        "refused": True,
                        "blocking": list(blocked.reasons),
                        "detail": list(measured.detail),
                        "probes": list(measured.probes),
                        "unprobed": list(measured.unprobed),
                    },
                )
            return ControlResult(
                status=200,
                payload={
                    "engaged": state.engaged,
                    "reason": state.reason,
                    "action": CLEAR,
                    "probes": list(measured.probes),
                },
            )

        raise ControlRefused(f"unknown action {action!r}")


__all__ = ["CLEAR", "ENGAGE", "ControlRefused", "ControlResult", "OperatorControl"]
