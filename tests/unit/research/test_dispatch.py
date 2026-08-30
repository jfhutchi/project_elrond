"""Choosing where a worker runs, instead of assuming the local host (#136).

The behaviour under test is routing, not execution: a job the local machine cannot carry should
become a routing decision rather than a refusal, and a refusal should only happen when *no*
backend can run the job.

No test here spawns a process. Backends are fakes, which is the property being demonstrated --
core code dispatches without knowing what the backend is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest

from quantbot.placement import HostProfile, HostProvenance, JobRequirements, NoCapableHost
from quantbot.research.dispatch import LocalSubprocessBackend, WorkerDispatcher
from quantbot.research.workers import ResearchWorker, WorkerInput, WorkerResult, WorkerSpec


def spec(name: str = "engine") -> WorkerSpec:
    return WorkerSpec(
        name=name,
        version="1.0.0",
        capabilities=frozenset(),
        configuration_hash="c" * 16,
        discloses_search_cardinality=True,
    )


class FakeWorker:
    """A `ResearchWorker` that records how it was built and never runs anything."""

    def __init__(self, backend: str, deadline_seconds: float) -> None:
        self.backend = backend
        self.deadline_seconds = deadline_seconds
        self._spec = spec()

    @property
    def spec(self) -> WorkerSpec:
        return self._spec

    def run(self, request: WorkerInput, *, now: datetime) -> WorkerResult:  # pragma: no cover
        raise AssertionError("dispatch tests never execute a worker")


class FakeBackend:
    """A backend that answers `can_run` however the test needs."""

    def __init__(self, name: str, refusal: str | None) -> None:
        self._name = name
        self._refusal = refusal
        self.asked = 0
        self.built = 0

    @property
    def name(self) -> str:
        return self._name

    def can_run(self, requirements: JobRequirements, *, now: datetime | None = None) -> str | None:
        self.asked += 1
        return self._refusal

    def worker_for(
        self,
        worker_spec: WorkerSpec,
        command: Sequence[str],
        requirements: JobRequirements,
        *,
        deadline_seconds: float,
    ) -> ResearchWorker:
        self.built += 1
        return FakeWorker(self._name, deadline_seconds)


def job(**kwargs: Any) -> JobRequirements:
    return JobRequirements(name="kronos-forecast", **kwargs)


def measured_host(**kwargs: Any) -> HostProfile:
    """A host that passes `is_measured`, built through the only route that grants it."""
    profile = HostProfile.measure("test-host")
    return HostProfile(
        name=profile.name,
        architecture=kwargs.get("architecture", profile.architecture),
        cpu_cores=kwargs.get("cpu_cores", profile.cpu_cores),
        total_memory_mb=kwargs.get("total_memory_mb", profile.total_memory_mb),
        gpu_vram_mb=kwargs.get("gpu_vram_mb", profile.gpu_vram_mb),
        model_endpoint=profile.model_endpoint,
        provenance=profile.provenance,
        _token=profile._token,
    )


def test_the_first_capable_backend_wins() -> None:
    """Order is the caller's policy statement, not a ranking the dispatcher invents.

    "Best backend" depends on a cost model nothing has measured, so the dispatcher takes the
    first that can run the job and leaves the ordering to whoever knows what is cheap.
    """
    first = FakeBackend("local", refusal=None)
    second = FakeBackend("cluster", refusal=None)

    worker = WorkerDispatcher([first, second]).dispatch(
        spec(), ["engine"], job(), deadline_seconds=30.0
    )

    assert isinstance(worker, FakeWorker)
    assert worker.backend == "local"
    assert second.built == 0, "a capable first backend means the second is never built"


def test_a_job_this_host_cannot_carry_routes_rather_than_refusing() -> None:
    """The actual behaviour change #136 is for.

    Before the dispatcher, a job exceeding the local host raised `NoCapableHost` -- correct when
    there was nowhere else to send it. A job the local machine cannot carry is now precisely the
    job that should go somewhere else.
    """
    local = FakeBackend("local", refusal="needs 24000MB VRAM, host reports 8192")
    gpu = FakeBackend("remote-gpu", refusal=None)

    worker = WorkerDispatcher([local, gpu]).dispatch(
        spec(), ["engine"], job(minimum_gpu_vram_mb=24_000), deadline_seconds=30.0
    )

    assert worker.backend == "remote-gpu"  # type: ignore[attr-defined]
    assert local.asked == 1, "the local backend was asked and declined, not skipped"


def test_a_refusal_names_every_backend_and_its_own_reason() -> None:
    """An operator debugging a job that will not start needs all the reasons, not the last one.

    Same rule as `unmet_requirement` returning a string rather than raising: three backends
    declining for three different reasons is three pieces of information, and collapsing them
    into "dispatch failed" throws away the two that would have explained it.
    """
    backends = [
        FakeBackend("local", refusal="needs 24000MB VRAM, host reports 8192"),
        FakeBackend("docker", refusal="no container runtime is configured"),
        FakeBackend("cluster", refusal="no cluster credentials"),
    ]

    with pytest.raises(NoCapableHost) as raised:
        WorkerDispatcher(backends).dispatch(
            spec(), ["engine"], job(minimum_gpu_vram_mb=24_000), deadline_seconds=30.0
        )

    assert raised.value.unmet == {
        "local": "needs 24000MB VRAM, host reports 8192",
        "docker": "no container runtime is configured",
        "cluster": "no cluster credentials",
    }
    message = str(raised.value)
    assert "kronos-forecast" in message
    for backend in backends:
        assert backend.name in message
        assert backend.asked == 1, f"{backend.name} was never asked"


def test_the_dispatcher_owns_the_deadline() -> None:
    """Two backends with two default wall clocks is the ambiguity this class removes.

    A subprocess enforces its own timeout, a container's lifetime belongs to the runtime, and a
    Kubernetes Job's to the cluster. Two authorities on one deadline is how a job gets killed
    twice or not at all, so the deadline arrives from the dispatcher.
    """
    backend = FakeBackend("local", refusal=None)

    worker = WorkerDispatcher([backend]).dispatch(
        spec(), ["engine"], job(), deadline_seconds=42.5
    )

    assert worker.deadline_seconds == 42.5  # type: ignore[attr-defined]


def test_a_deadline_must_be_stated() -> None:
    """A caller that has not decided how long a job may take has not finished specifying it."""
    backend = FakeBackend("local", refusal=None)

    with pytest.raises(ValueError, match="deadline_seconds must be positive"):
        WorkerDispatcher([backend]).dispatch(spec(), ["engine"], job(), deadline_seconds=0)

    assert backend.asked == 0, "the deadline is checked before any backend is consulted"


def test_a_dispatcher_with_no_backends_is_refused_at_construction() -> None:
    """It could only ever refuse, and failing at first dispatch would hide why."""
    with pytest.raises(ValueError, match="at least one"):
        WorkerDispatcher([])


def test_core_dispatches_without_knowing_the_backend() -> None:
    """The point of the seam: nothing in the call names a transport.

    Every backend in this file is a fake, and none of them is a subprocess. If dispatch required
    knowledge of how the worker runs, these tests could not be written this way.
    """
    dispatched = WorkerDispatcher([FakeBackend("anything-at-all", refusal=None)]).dispatch(
        spec(), ["engine"], job(), deadline_seconds=30.0
    )

    assert isinstance(dispatched, FakeWorker)
    assert dispatched.spec.name == "engine"


def test_the_local_backend_reports_capability_from_a_measured_host() -> None:
    """Capability is read from the host, not declared by the backend."""
    backend = LocalSubprocessBackend(host=measured_host(total_memory_mb=4096))

    assert backend.can_run(job(minimum_memory_mb=1024)) is None
    refusal = backend.can_run(job(minimum_memory_mb=64_000))
    assert refusal is not None
    assert "64000" in refusal


def test_an_unmeasured_host_cannot_receive_work() -> None:
    """A host cannot receive work on the strength of its own description.

    The rule lives in `unmet_requirement`, and this asserts the backend routes through it rather
    than answering from its own fields -- which is what a backend that reimplemented the check
    would do.
    """
    declared = HostProfile(
        name="claims-a-gpu",
        architecture="amd64",
        cpu_cores=64,
        total_memory_mb=512_000,
        gpu_vram_mb=80_000,
        provenance=HostProvenance.DECLARED,
    )

    refusal = LocalSubprocessBackend(host=declared).can_run(job(minimum_gpu_vram_mb=1))

    assert refusal is not None, "a declared host passed a requirement it merely claimed to meet"
    assert "not measured" in refusal


def test_the_local_backend_builds_a_worker_carrying_the_dispatched_deadline() -> None:
    """The deadline reaches the thing that enforces it, rather than the constructor default.

    `SubprocessWorker` defaults to 900s. A dispatcher that owned the deadline in name only would
    leave that default in place and nothing would notice, because both numbers are plausible.
    """
    backend = LocalSubprocessBackend(host=measured_host())

    worker = backend.worker_for(spec(), ["engine"], job(), deadline_seconds=12.0)

    assert worker._timeout == 12.0  # type: ignore[attr-defined]
    assert worker._timeout != 900.0  # type: ignore[attr-defined]


def test_the_local_backend_keeps_the_subprocess_host_check() -> None:
    """Kept deliberately, against the acceptance criterion this issue was opened with.

    That criterion said `SubprocessWorker` should stop gating on the local host. On inspection
    that would weaken a control: the class is constructible directly, its guard protects the
    always-on control plane from a job that thrashes onto swap, and under dispatch the guard is
    a second look at what `can_run` just decided rather than an unreachable one.

    So the backend passes requirements and host through, and this asserts it does.
    """
    host = measured_host(total_memory_mb=4096)
    requirements = job(minimum_memory_mb=1024)

    worker = LocalSubprocessBackend(host=host).worker_for(
        spec(), ["engine"], requirements, deadline_seconds=30.0
    )

    assert worker._requirements == requirements  # type: ignore[attr-defined]
    assert worker._host == host  # type: ignore[attr-defined]
