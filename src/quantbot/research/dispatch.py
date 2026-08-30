"""Choose where a worker runs, instead of assuming the local host (#136).

`workers.py` defined `ResearchWorker` with no transport in the contract, and `worker_adapter.py`
implemented it once, for a child process on this machine. That was the right first target. It is
also the only one, and until something can choose between execution targets, "the worker runs
here" is encoded in the fact that there is nowhere else.

This adds the choice. Target shape, from the operator's direction -- *Kubernetes-ready without a
Kubernetes rewrite*:

    Elrond -> WorkerDispatcher -> Local/Subprocess | Docker | KubernetesJob | RemoteGPU

Only the first backend exists here. The others land behind the same protocol, and core code never
learns which one ran.

**What actually changes: a job the local host cannot carry becomes a routing decision.**

`SubprocessWorker._require_capable_host` raises `NoCapableHost` when this machine cannot meet the
job's `JobRequirements`. That is correct for a system with one execution target, and its reasoning
is worth preserving: a Kronos job that starts on a 512MB host does not fail cleanly, it thrashes
onto swap and takes the always-on control plane with it -- and the broker, risk engine and
reconciliation live there.

Under a dispatcher the question is unchanged; *who asks it* changes. The dispatcher asks every
backend, and refuses only when none can run the job. That is `place()`'s existing semantics, and
the refusal names every backend's reason for the same reason `unmet_requirement` returns a string
rather than raising: an operator debugging a job that will not start needs to see that three
backends declined for three different reasons, not that dispatch failed.

**The dispatcher owns the deadline.**

`SubprocessWorker` enforces a wall clock through `subprocess.run(timeout=...)`. A container's
lifetime belongs to the runtime and a Kubernetes Job's to the cluster (`activeDeadlineSeconds`).
Two authorities on one deadline is how a job gets killed twice, or not at all. So `dispatch()`
requires the deadline and hands it to whichever backend enforces it, rather than each backend
carrying a default that quietly differs from the others.

**What this deliberately is not.**

No queue, broker, priority scheduler, backpressure or rate limiter. Dispatch is synchronous
request/response, and a future queued dispatcher satisfies the same `dispatch()` signature without
a caller change -- which is the point of putting the seam in now.

Building the scheduler now would mean choosing its policy from imagination: `search_runs` has no
rows, `rejected_candidates` is empty, and the measured peak concurrent research job count in this
system's history is zero, because nothing has ever dispatched one. Those constants would be
load-bearing and untestable, and a scheduler asserted by a test that constructs its own queue
proves only that the test can construct a queue. #143's market loop is what can produce a real
event rate; the scheduler belongs after that number exists, not before it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from quantbot.placement import (
    HostProfile,
    JobRequirements,
    NoCapableHost,
    unmet_requirement,
)
from quantbot.research.worker_adapter import SubprocessWorker
from quantbot.research.workers import ResearchWorker, WorkerSpec


class WorkerBackend(Protocol):
    """One place a worker can run.

    `can_run` returns a *reason string or None*, matching `unmet_requirement` rather than
    returning a bool. A backend that declines without saying why produces a dispatch failure
    nobody can act on, which is how a routing layer becomes a black box.
    """

    @property
    def name(self) -> str: ...

    def can_run(self, requirements: JobRequirements, *, now: datetime | None = None) -> str | None:
        """Why this backend cannot run the job, or `None` if it can."""
        ...

    def worker_for(
        self,
        spec: WorkerSpec,
        command: Sequence[str],
        requirements: JobRequirements,
        *,
        deadline_seconds: float,
    ) -> ResearchWorker:
        """Build a worker bound to this backend. Only called after `can_run` returned `None`."""
        ...


@dataclass(frozen=True, slots=True)
class LocalSubprocessBackend:
    """Run the worker as a child process on this machine.

    The host is measured once at construction rather than per dispatch: reading the GPU spawns a
    process, and a machine does not grow RAM between two jobs. `SubprocessWorker` already makes
    that trade for the same reason.

    A declared profile is refused by `unmet_requirement` rather than here -- a host cannot receive
    work on the strength of its own description, and that rule belongs in one place.
    """

    host: HostProfile
    runner: object | None = None

    @property
    def name(self) -> str:
        return f"local-subprocess:{self.host.name}"

    def can_run(self, requirements: JobRequirements, *, now: datetime | None = None) -> str | None:
        return unmet_requirement(requirements, self.host, now=now)

    def worker_for(
        self,
        spec: WorkerSpec,
        command: Sequence[str],
        requirements: JobRequirements,
        *,
        deadline_seconds: float,
    ) -> ResearchWorker:
        # `requirements` and `host` are passed through so `SubprocessWorker` keeps its own
        # pre-spawn check. Under dispatch that check is a second look at what `can_run` just
        # decided, and it is kept deliberately: `SubprocessWorker` is constructible directly,
        # and removing the guard would weaken a control that protects the control plane for
        # every caller that does not come through here.
        return SubprocessWorker(
            spec,
            command,
            timeout_seconds=deadline_seconds,
            requirements=requirements,
            host=self.host,
            runner=self.runner,
        )


class WorkerDispatcher:
    """Pick a backend that can run the job, or refuse naming every backend's reason.

    First capable backend wins, in the order given. Ordering is the caller's policy statement --
    put the cheapest or most local backend first -- rather than a ranking this class invents,
    because "best backend" depends on a cost model nothing has measured.
    """

    def __init__(self, backends: Sequence[WorkerBackend]) -> None:
        if not backends:
            raise ValueError(
                "a dispatcher with no backends can only refuse; construct it with at least one"
            )
        self._backends = tuple(backends)

    @property
    def backends(self) -> tuple[WorkerBackend, ...]:
        return self._backends

    def dispatch(
        self,
        spec: WorkerSpec,
        command: Sequence[str],
        requirements: JobRequirements,
        *,
        deadline_seconds: float,
        now: datetime | None = None,
    ) -> ResearchWorker:
        """A worker bound to the first backend that can run the job.

        `deadline_seconds` is required rather than defaulted. Two backends with two different
        default wall clocks is the ambiguity this class exists to remove, and a caller that has
        not decided how long a job may take has not finished specifying the job.
        """
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")

        unmet: dict[str, str] = {}
        for backend in self._backends:
            reason = backend.can_run(requirements, now=now)
            if reason is None:
                return backend.worker_for(
                    spec, command, requirements, deadline_seconds=deadline_seconds
                )
            unmet[backend.name] = reason

        raise NoCapableHost(
            f"no backend can run {requirements.name}: "
            + "; ".join(f"{name} -- {reason}" for name, reason in sorted(unmet.items())),
            unmet=unmet,
        )


def _assert_protocol(backend: LocalSubprocessBackend) -> WorkerBackend:
    """Compile-time proof that the local backend satisfies the protocol it claims to."""
    return backend


__all__ = [
    "LocalSubprocessBackend",
    "WorkerBackend",
    "WorkerDispatcher",
]
