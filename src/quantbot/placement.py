"""Which host a job may run on, decided from measured capacity (#38).

The failure this prevents is specific and has a name in the issue: *no silent fallback from
GPU/large-memory workflows to the Pi Zero W*. A 512MB single-core ARM11 host will accept a
Kronos job and then thrash, swap onto a microSD, and take the always-on control plane down with
it -- and the trading daemon, risk engine and reconciliation live on that control plane.

So placement refuses rather than degrades. A job that no host can satisfy is not dispatched to
the least-bad option; it raises, naming the requirement nothing met.

**A host profile must be measured, not declared.** This is the same defect class that has come
up repeatedly in this project: a gate that reads its input from the thing it is supposed to
constrain. If a worker could announce "16GB, x86_64" the gate would be checking a claim rather
than a capacity, and the one host that must never receive heavy work is exactly the host whose
operator is most likely to have copied a config from somewhere else. `HostProfile.measure()`
reads the machine; a profile built any other way carries `DECLARED` provenance and is refused
for placement, so an undersized host cannot talk its way into a job.

**Unknown blocks.** A capacity that could not be read is not treated as sufficient. Refusing to
place a job because nobody could measure the RAM is recoverable; placing it because the reading
failed open is how the control plane goes down.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

#: Module-private. A `HostProfile` carrying this was produced by reading a machine, and only
#: such a profile can satisfy a requirement. Constructing one elsewhere is not possible without
#: reaching into this module, which is the point.
_MEASURED = object()


class HostProvenance(StrEnum):
    MEASURED = "MEASURED"
    #: Written down by a human or a config file. Useful for describing a host that is currently
    #: switched off; never sufficient to receive a job.
    DECLARED = "DECLARED"


#: How recently an endpoint must have answered for its capability to count as current. A
#: `HostProfile` is measured once and can be held for the life of a worker, so a verification
#: from an hour ago is a fact about an hour ago.
ENDPOINT_FRESHNESS = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class ModelEndpointStatus:
    """Whether a model endpoint actually answered, and when.

    A configured URL is not a capability. `has_local_model_endpoint` used to be
    `bool(model_endpoint)`, which admitted a dead, stale or misconfigured endpoint as a measured
    property -- inside the one module whose whole argument is that capacity is measured rather
    than declared. Sol caught it; it was mine.
    """

    url: str
    reachable: bool
    verified_at: datetime | None = None
    detail: str = ""

    def is_current(self, now: datetime) -> bool:
        """Reachable, and verified recently enough that the answer still describes now."""
        if not self.reachable or self.verified_at is None:
            return False
        return now - self.verified_at <= ENDPOINT_FRESHNESS


def probe_model_endpoint(
    url: str, *, now: datetime, timeout_seconds: float = 5.0
) -> ModelEndpointStatus:
    """Ask the endpoint whether it is there, rather than assuming its URL implies it.

    An OpenAI-compatible `/models` listing is the cheapest request that distinguishes a live
    runtime from an open port. A failure is recorded rather than raised: a host without a model
    endpoint is a normal host, and the job that needs one is refused by `unmet_requirement`.
    """
    listing = url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(listing, timeout=timeout_seconds) as response:
            reachable = 200 <= response.status < 300
            detail = f"HTTP {response.status}"
    except (urllib.error.URLError, OSError, ValueError) as error:
        return ModelEndpointStatus(url=url, reachable=False, detail=type(error).__name__)
    return ModelEndpointStatus(
        url=url, reachable=reachable, verified_at=now if reachable else None, detail=detail
    )


class NoCapableHost(RuntimeError):
    """Raised when no measured host satisfies a job's declared requirements."""

    def __init__(self, message: str, *, unmet: dict[str, str]) -> None:
        super().__init__(message)
        #: Host name to the first requirement it failed, so an operator sees why each was
        #: rejected rather than only that everything was.
        self.unmet = unmet


@dataclass(frozen=True, slots=True)
class HostProfile:
    """What a host can actually do.

    `None` means "could not be read", which is deliberately distinct from `0`. A host reporting
    zero GPU memory has been measured and has no GPU; a host reporting `None` was never probed,
    and the two must not lead to the same decision.
    """

    name: str
    architecture: str
    cpu_cores: int | None
    total_memory_mb: int | None
    gpu_vram_mb: int | None
    model_endpoint: ModelEndpointStatus | None = None
    provenance: HostProvenance = HostProvenance.DECLARED
    _token: object | None = field(default=None, repr=False, compare=False)

    @classmethod
    def measure(
        cls,
        name: str,
        *,
        model_endpoint: str | None = None,
        now: datetime | None = None,
    ) -> Self:
        """Read this machine. The only route to a profile that can receive work.

        Probes the model endpoint when one is given, rather than recording that a string was
        supplied. A single short-timeout request, skipped entirely when no endpoint is
        configured, so a host with no model runtime pays nothing for the check.
        """
        moment = now or datetime.now(UTC)
        return cls(
            name=name,
            architecture=platform.machine().lower(),
            cpu_cores=os.cpu_count(),
            total_memory_mb=_total_memory_mb(),
            gpu_vram_mb=_gpu_vram_mb(),
            model_endpoint=(
                None if not model_endpoint else probe_model_endpoint(model_endpoint, now=moment)
            ),
            provenance=HostProvenance.MEASURED,
            _token=_MEASURED,
        )

    @property
    def is_measured(self) -> bool:
        return self._token is _MEASURED and self.provenance is HostProvenance.MEASURED


@dataclass(frozen=True, slots=True)
class JobRequirements:
    """What a job needs before it may be dispatched anywhere.

    Declared by the job, which is safe in a way a host's self-report is not: a job overstating
    its needs is refused placement, and a job understating them is the caller harming its own
    work rather than the control plane.
    """

    name: str
    #: Empty means any architecture. Otherwise the job runs only on one of these.
    architectures: frozenset[str] = frozenset()
    minimum_cores: int = 1
    minimum_memory_mb: int = 0
    minimum_gpu_vram_mb: int = 0
    requires_local_model_endpoint: bool = False


def unmet_requirement(
    job: JobRequirements, host: HostProfile, *, now: datetime | None = None
) -> str | None:
    """Why this host cannot run this job, or `None` if it can.

    Returned rather than raised so a caller can report every host's reason at once. An operator
    debugging a stuck queue needs to see that three hosts were rejected for three different
    reasons, not that placement failed.
    """
    if not host.is_measured:
        return (
            f"{host.name} is {host.provenance.value.lower()}, not measured; a host cannot "
            "receive work on the strength of its own description"
        )
    if job.architectures and host.architecture not in job.architectures:
        return f"architecture {host.architecture} is not one of {sorted(job.architectures)}"
    # Each of these treats an unreadable capacity as insufficient. A job refused because nobody
    # could measure the RAM is recoverable; one placed because the reading failed is not.
    if host.cpu_cores is None or host.cpu_cores < job.minimum_cores:
        return f"needs {job.minimum_cores} cores, host reports {host.cpu_cores}"
    if host.total_memory_mb is None or host.total_memory_mb < job.minimum_memory_mb:
        return f"needs {job.minimum_memory_mb}MB, host reports {host.total_memory_mb}"
    if job.minimum_gpu_vram_mb:
        if host.gpu_vram_mb is None or host.gpu_vram_mb < job.minimum_gpu_vram_mb:
            return f"needs {job.minimum_gpu_vram_mb}MB VRAM, host reports {host.gpu_vram_mb}"
    if job.requires_local_model_endpoint:
        endpoint = host.model_endpoint
        if endpoint is None:
            return "needs a local model endpoint, host has none configured"
        if not endpoint.reachable:
            return (
                f"needs a local model endpoint; {endpoint.url} did not answer ({endpoint.detail})"
            )
        if not endpoint.is_current(now or datetime.now(UTC)):
            # Stale blocks for the same reason unknown blocks: a verification from an hour ago
            # is a fact about an hour ago, and this profile may have been held that long.
            return f"the endpoint check for {endpoint.url} is stale; re-measure the host"
    return None


def place(
    job: JobRequirements, hosts: list[HostProfile], *, now: datetime | None = None
) -> HostProfile:
    """Choose a host that satisfies the job, or refuse.

    Refuses rather than picking the closest match. Degrading a GPU job onto a 512MB ARM host is
    how the always-on control plane -- broker, risk, reconciliation -- goes down for a research
    task that was never urgent.

    Among capable hosts the smallest sufficient one wins, so a large machine stays free for work
    that genuinely needs it rather than being taken by the first job to ask.
    """
    unmet: dict[str, str] = {}
    capable: list[HostProfile] = []
    for host in hosts:
        reason = unmet_requirement(job, host, now=now)
        if reason is None:
            capable.append(host)
        else:
            unmet[host.name] = reason
    if not capable:
        raise NoCapableHost(
            f"no measured host can run {job.name}: "
            + "; ".join(f"{name} -- {reason}" for name, reason in sorted(unmet.items())),
            unmet=unmet,
        )
    return min(capable, key=lambda host: (host.total_memory_mb or 0, host.cpu_cores or 0))


def _total_memory_mb() -> int | None:
    """Physical memory, or `None` when it cannot be read on this platform."""
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError):
            pass
        else:
            if isinstance(pages, int) and isinstance(page_size, int) and pages > 0:
                return pages * page_size // (1024 * 1024)
    if sys.platform == "win32":

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys) // (1024 * 1024)
    return None


def _gpu_vram_mb() -> int | None:
    """Largest CUDA device's memory, `0` when none is visible, `None` when unreadable.

    Absent `nvidia-smi` is reported as `0` rather than `None`: it means no CUDA GPU is usable
    here, and that is a decision-grade fact. The distinction still matters in the other
    direction -- a present `nvidia-smi` that fails or returns something unparseable gives `None`,
    because a GPU that could not be measured must not be treated as one that is absent *or* as
    one that is adequate.
    """
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return 0
    try:
        completed = subprocess.run(
            [executable, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    values = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not values:
        return 0
    try:
        return max(int(value) for value in values)
    except ValueError:
        return None


#: The three workloads this project actually dispatches, with the requirements measured rather
#: than guessed. Kronos-small's peak was 660MB on the operator's machine (#36); the ceiling here
#: is that plus room for the interpreter and the source snapshot, and it is comfortably above
#: anything a Pi Zero W can offer.
KRONOS_INFERENCE = JobRequirements(
    name="kronos-inference",
    architectures=frozenset({"x86_64", "amd64", "aarch64", "arm64"}),
    minimum_cores=2,
    minimum_memory_mb=2048,
)
SEMANTIC_ANALYSIS = JobRequirements(
    name="semantic-analysis",
    minimum_cores=2,
    minimum_memory_mb=1024,
    requires_local_model_endpoint=True,
)
#: The always-on control plane: scheduler, ledger, risk, reconciliation, broker. Deliberately
#: modest, because this is the one thing that must keep running when every worker is offline --
#: and still more than a Pi Zero W can offer. A nominally 512MB Zero W reports about 434MB once
#: the VideoCore split is taken, so this floor excludes it, which is #38's conclusion rather
#: than an accident: the control plane moves to a stronger host.
CONTROL_PLANE = JobRequirements(name="control-plane", minimum_cores=1, minimum_memory_mb=512)
#: What the Pi keeps. Heartbeat freshness, endpoint checks, an alert trigger -- and nothing the
#: trading path depends on, so the Pi being off is an inconvenience rather than an outage.
WATCHDOG = JobRequirements(name="watchdog", minimum_cores=1, minimum_memory_mb=128)

__all__ = [
    "CONTROL_PLANE",
    "ENDPOINT_FRESHNESS",
    "ModelEndpointStatus",
    "probe_model_endpoint",
    "WATCHDOG",
    "KRONOS_INFERENCE",
    "SEMANTIC_ANALYSIS",
    "HostProfile",
    "HostProvenance",
    "JobRequirements",
    "NoCapableHost",
    "place",
    "unmet_requirement",
]
