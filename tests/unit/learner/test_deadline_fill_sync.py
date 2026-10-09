"""No broker orders or deployed account access: post-fill safety gates."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from quantbot.domain import TradingMode
from tests.integration.test_execution_coordinator import _settings
from tests.unit.learner.test_paper import NOW


@dataclass
class Protection:
    outcome: str = "HEALTHY"
    unprotected_count: int = 0
    submitter: object = "order authority must be removed"

    async def evaluate(self, *, now):
        assert self.submitter is None
        return self


def runtime(*, allowed=True, healthy=True, unresolved=(), protection=None):
    async def recover(*args, **kwargs):
        return SimpleNamespace(
            ready=False, reasons=("KILL_SWITCH_ENGAGED",),
            reconciliation=SimpleNamespace(allows_new_orders=allowed),
            broker_health=SimpleNamespace(healthy=healthy),
            unresolved_intent_ids=unresolved,
        )
    return SimpleNamespace(settings=_settings(), recovery=SimpleNamespace(recover=recover),
                           protector=lambda: protection or Protection())


def sync():
    from quantbot.learner import execution_worker
    function = getattr(execution_worker, "reconcile_deadline_fills", None)
    assert callable(function), "missing no-orders post-fill reconciliation hook"
    return function


async def test_post_fill_health_verification_does_not_require_clearing_entry_stop():
    assert await sync()(runtime(), NOW) == ()


@pytest.mark.parametrize("kwargs", [{"allowed": False}, {"healthy": False},
                                    {"unresolved": ("unknown-intent",)}])
async def test_failed_reconciliation_cannot_acknowledge_confirmed_fill_sync(kwargs):
    assert await sync()(runtime(**kwargs), NOW) == ("POST_FILL_RECONCILIATION_REQUIRED",)


async def test_degraded_protection_cannot_acknowledge_confirmed_fill_sync():
    r = runtime(protection=Protection(outcome="DEGRADED", unprotected_count=1))
    assert await sync()(r, NOW) == ("POST_FILL_PROTECTION_REQUIRED",)


async def test_live_runtime_cannot_use_paper_fill_sync():
    r = runtime()
    r.settings = r.settings.model_copy(update={"TRADING_MODE": TradingMode.LIVE})
    with pytest.raises(ValueError, match="PAPER"):
        await sync()(r, NOW)
