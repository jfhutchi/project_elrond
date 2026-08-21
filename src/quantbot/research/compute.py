"""Charge sandboxed work to a budget, and let the budget shrink the sandbox (#14).

`BudgetGovernor` could account for `WALL_SECONDS` and `SandboxPolicy` could enforce a wall clock,
and the two had never met. So a research loop could run a hundred sandboxed experiments inside a
budget that recorded none of them, which is the same defect as a factor miner's trials not
entering the multiple-testing burden -- an accounting system that only sees the work somebody
remembered to tell it about.

Two directions, and the second is the one that matters:

* `record_sandbox_run` charges what a run actually cost. Backward-looking, and honest.
* `policy_within_budget` shrinks the wall clock to what the budget can still afford, and refuses
  when it can afford nothing. Forward-looking, and the reason this is a control rather than a
  report. A budget that only records overspending is a diary.

**A terminated run is charged in full.** It used the wall clock; being killed at the ceiling does
not refund the seconds. Charging only successful runs would make failing a way to get free
compute, which is the same argument that puts holdout consumption before the measurement rather
than after it.

Peak memory is reported, never budgeted. A peak is not a cumulative spend -- two runs each
peaking at 500MB have not used a gigabyte of anything -- and adding it to a running total would
produce a number that looks like a budget and means nothing.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quantbot.research.budget import BudgetExceeded, BudgetGovernor, BudgetOverride, Resource
from quantbot.sandbox.policy import SandboxPolicy
from quantbot.sandbox.runner import SandboxResult

#: Below this there is no point starting: process spawn, venv staging and the integrity preamble
#: cost more than this before the script runs at all, so a run admitted with less would be killed
#: by its own wall clock having measured nothing. Refusing is the honest answer.
MINIMUM_USEFUL_SECONDS = Decimal("5")


class ComputeExhausted(RuntimeError):
    """Raised when the remaining compute budget cannot support a useful run."""

    def __init__(self, budget_key: str, remaining: Decimal) -> None:
        self.budget_key = budget_key
        self.remaining = remaining
        super().__init__(
            f"{budget_key} has {remaining} wall-clock seconds left, below the "
            f"{MINIMUM_USEFUL_SECONDS} needed for a run that could measure anything"
        )


def policy_within_budget(
    governor: BudgetGovernor,
    policy: SandboxPolicy,
    *,
    budget_key: str,
) -> SandboxPolicy:
    """Return `policy` with its wall clock capped at what the budget can still afford.

    Never widens. A policy asking for 60 seconds against 600 remaining comes back unchanged --
    the budget is a ceiling, not a target, and handing a short experiment the whole remaining
    allowance would spend it on the first thing that asked.

    Raises rather than returning a policy nobody should run. A one-second sandbox is not a
    smaller experiment; it is a wall-clock termination with extra steps, and returning one would
    turn budget exhaustion into a stream of `terminated_reason="wall-clock"` results that read
    like flaky experiments.
    """
    cap = next(
        (
            item
            for item in governor.caps()
            if item.budget_key == budget_key and item.resource is Resource.WALL_SECONDS
        ),
        None,
    )
    if cap is None:
        # No cap declared for this key. Unbudgeted, not free -- `record_sandbox_run` will still
        # refuse to charge it, which is where the caller finds out.
        return policy

    remaining = cap.limit - governor.spent(budget_key, Resource.WALL_SECONDS)
    if remaining < MINIMUM_USEFUL_SECONDS:
        raise ComputeExhausted(budget_key, remaining)
    allowed = min(Decimal(str(policy.wall_clock_seconds)), remaining)
    if allowed == Decimal(str(policy.wall_clock_seconds)):
        return policy
    return policy.model_copy(update={"wall_clock_seconds": float(allowed)})


def record_sandbox_run(
    governor: BudgetGovernor,
    result: SandboxResult,
    *,
    budget_key: str,
    task: str,
    now: datetime,
    override: BudgetOverride | None = None,
) -> Decimal:
    """Charge one sandboxed run's wall clock to the budget, terminated or not.

    Returns the seconds charged. Rounded to milliseconds so the ledger holds a stable decimal
    rather than a float's tail, which would make two identical runs record differently.

    A run that overshoots its remaining budget is still recorded. `admit` would refuse it and the
    time is already gone, so the spend is committed directly: a budget that silently omits the
    overrun it failed to prevent understates itself permanently, and understating a spend is the
    failure mode this whole module exists to prevent.
    """
    seconds = Decimal(str(result.duration_seconds)).quantize(Decimal("0.001"))
    note = _note(result)
    try:
        governor.spend(
            budget_key,
            Resource.WALL_SECONDS,
            seconds,
            task=task,
            now=now,
            override=override,
            note=note,
        )
    except BudgetExceeded:
        admission = governor.admit(
            budget_key,
            Resource.WALL_SECONDS,
            seconds,
            override=BudgetOverride(
                authorized_by="compute-accounting",
                reason="run had already completed when the overrun was detected",
            ),
        )
        governor.record(
            admission,
            task=task,
            now=now,
            override=BudgetOverride(
                authorized_by="compute-accounting",
                reason="run had already completed when the overrun was detected",
            ),
            note=note,
        )
    return seconds


def _note(result: SandboxResult) -> str:
    """What the run cost, in the terms the ceiling is set in."""
    memory = (
        "peak memory unmeasured"
        if result.peak_memory_mb is None
        else f"peak {result.peak_memory_mb:.1f}MB"
    )
    ending = result.terminated_reason or ("ok" if result.ok else "failed")
    return f"{ending}; {memory}"


__all__ = [
    "MINIMUM_USEFUL_SECONDS",
    "ComputeExhausted",
    "policy_within_budget",
    "record_sandbox_run",
]
