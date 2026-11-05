"""Closed, versioned economic contracts; these are not operator-adjustable risk knobs."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.entry_policy import EntryPolicy

LEGACY_PROFILE = "LEGACY_30M_V1"
THROUGHPUT_PROFILE = "RESOURCE_BOUNDED_PAPER_V1"
THROUGHPUT_POLICY = "collect-owned-30m-stop100-v1"


@dataclass(frozen=True)
class EconomicLimits:
    max_positions: int | None
    position_bps: int
    gross_bps: int | None
    reserve_bps: int
    entry_risk_bps: int
    stop_bps: int
    open_risk_bps: int | None
    daily_loss_bps: int | None
    hold_seconds: int
    daily_entry_limit: int | None


def parallel_policy(config: "CoreConfig", policy: "EntryPolicy | None") -> bool:
    return config.economic_profile == THROUGHPUT_PROFILE or (
        policy is not None and policy.contract == THROUGHPUT_PROFILE
    )


def limits(config: "CoreConfig", *, entry_policy: "EntryPolicy | None" = None) -> EconomicLimits:
    if parallel_policy(config, entry_policy):
        return EconomicLimits(None, 50, 9500, 500, 25, 100, 25, 200, 1800, None)
    return EconomicLimits(1, 50, None, 500, 25, 100, None, None, 1800, 6)
