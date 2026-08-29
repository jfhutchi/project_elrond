"""What a measured machine is *allowed* to do (#38).

The companion to `quantbot.node.capabilities`, and deliberately a separate module. Detection
answers "there is an AMD Radeon RX 6650M with 8192 MB". This answers "that card is not eligible
for CUDA work". Both are true, and a probe that returned only the second would have destroyed
the first: an operator reading a `0` cannot tell whether the machine has no GPU, has a GPU
nobody could measure, or has a perfectly good GPU that this project cannot currently use.

Two rules govern everything below.

**A role is assigned, not derived.** Hardware says what a machine *could* carry; the node's
configuration says what it *will*. A 32-thread box with a 16 GB accelerator that has been given
`WATCHDOG` is a watchdog, and routing must respect that. Conflating the two would mean the only
way to restrict a powerful machine is to lie about its hardware.

**Unknown blocks.** Capabilities that could not be read are insufficient, never adequate. This
follows `quantbot.placement` rather than reinventing the judgement, and for the same reason:
refusing work because nobody could read the RAM is recoverable, granting it because the reading
failed open is not.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import StrEnum

from quantbot.node.capabilities import GpuVendor, NodeCapabilities
from quantbot.placement import CONTROL_PLANE, KRONOS_INFERENCE, WATCHDOG, JobRequirements


class AcceleratorRuntime(StrEnum):
    """A GPU compute stack that is actually usable here, not merely installed."""

    CUDA = "CUDA"
    ROCM = "ROCM"


class NodeRole(StrEnum):
    """What a node is for.

    These are the functional units this repository already has, rather than a generic cluster
    taxonomy imported from somewhere else. `quantbot.placement` declares exactly three job
    archetypes -- control plane, dispatched compute, watchdog -- and the roles map onto them.
    """

    #: The always-on control plane: durable ledger, broker and paper execution, risk engine,
    #: reconciliation, kill switch, scheduler. Exactly one node may hold this, because the
    #: ledger is guarded by a single-writer lock.
    COORDINATOR = "coordinator"
    #: Receives dispatched compute: backtests, Kronos inference, semantic analysis. CPU-bound.
    WORKER = "worker"
    #: A worker that additionally advertises a usable accelerator.
    GPU_WORKER = "gpu-worker"
    #: Heartbeat freshness, endpoint checks, alerting. Nothing the trading path depends on.
    WATCHDOG = "watchdog"
    #: A workstation running tests and interactive research. Never receives dispatched work,
    #: so a developer restarting their machine cannot strand a queued job.
    DEVELOPMENT = "development"
    #: Coordinator and worker on one machine. The current single-host reality, and the correct
    #: role for an installation that has not yet been split.
    ALL_IN_ONE = "all-in-one"


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """The capability floor a role needs, and whether it accepts dispatched work."""

    role: NodeRole
    #: Reuses the requirements `quantbot.placement` already declares. CLAUDE.md's tiebreak puts
    #: an existing deployed value ahead of a fresh opinion, and these numbers were measured.
    floor: JobRequirements
    accepts_dispatch: bool
    requires_accelerator: bool = False


ROLE_PROFILES: dict[NodeRole, RoleProfile] = {
    NodeRole.COORDINATOR: RoleProfile(NodeRole.COORDINATOR, CONTROL_PLANE, accepts_dispatch=False),
    NodeRole.WORKER: RoleProfile(NodeRole.WORKER, KRONOS_INFERENCE, accepts_dispatch=True),
    NodeRole.GPU_WORKER: RoleProfile(
        NodeRole.GPU_WORKER, KRONOS_INFERENCE, accepts_dispatch=True, requires_accelerator=True
    ),
    NodeRole.WATCHDOG: RoleProfile(NodeRole.WATCHDOG, WATCHDOG, accepts_dispatch=False),
    NodeRole.DEVELOPMENT: RoleProfile(
        NodeRole.DEVELOPMENT, KRONOS_INFERENCE, accepts_dispatch=False
    ),
    NodeRole.ALL_IN_ONE: RoleProfile(NodeRole.ALL_IN_ONE, KRONOS_INFERENCE, accepts_dispatch=True),
}


def usable_accelerators(capabilities: NodeCapabilities) -> frozenset[AcceleratorRuntime]:
    """Which GPU compute stacks this node can actually dispatch work to.

    A card is not a runtime. CUDA requires an NVIDIA device *and* a `nvidia-smi` that answered;
    a driver-less card and a stale toolkit both fail here, which is the point.

    ROCm is treated as unusable on Windows regardless of what is installed. That is a policy
    statement with a reason rather than a hardware fact: AMD does not ship a supported Windows
    ROCm runtime for consumer RDNA2 parts, so advertising ROCm from a Windows host would route
    work to a machine that cannot run it. Detection still reports the card and its memory in
    full, so the day that changes this is a one-line policy edit and not a re-probe.
    """
    if not capabilities.is_measured:
        return frozenset()
    devices = capabilities.gpus
    if devices is None:
        # Enumeration failed. Unknown blocks.
        return frozenset()

    runtimes: set[AcceleratorRuntime] = set()
    vendors = {device.vendor for device in devices}
    if GpuVendor.NVIDIA in vendors and capabilities.runtimes.nvidia_smi is not None:
        runtimes.add(AcceleratorRuntime.CUDA)
    if (
        GpuVendor.AMD in vendors
        and capabilities.runtimes.rocm_smi is not None
        and capabilities.operating_system != "win32"
    ):
        runtimes.add(AcceleratorRuntime.ROCM)
    return frozenset(runtimes)


def cuda_vram_mb(capabilities: NodeCapabilities) -> int | None:
    """VRAM reachable through CUDA: `0` when none, `None` when it could not be established.

    This is the number `quantbot.placement` means by `gpu_vram_mb`, stated where its meaning is
    visible. A machine with an 8 GB Radeon and no CUDA runtime returns `0` here and still
    reports 8192 MB from detection, which is the distinction the old probe collapsed.
    """
    if not capabilities.is_measured:
        return None
    devices = capabilities.gpus
    if devices is None:
        return None
    if AcceleratorRuntime.CUDA not in usable_accelerators(capabilities):
        return 0
    readings = [
        device.vram_mb
        for device in devices
        if device.vendor is GpuVendor.NVIDIA and device.vram_mb is not None
    ]
    if not readings:
        # An NVIDIA card with a live runtime whose memory could not be read is unknown, not zero.
        return None
    return max(readings)


def role_is_supportable(role: NodeRole, capabilities: NodeCapabilities) -> str | None:
    """Why this machine cannot carry this role, or `None` if it can.

    Returned rather than raised, matching `quantbot.placement.unmet_requirement`, so an
    installer can report every rejected role at once instead of failing on the first.
    """
    if not capabilities.is_measured:
        return (
            f"capabilities are {capabilities.provenance.value.lower()}, not measured; a node "
            "cannot be assigned a role on the strength of its own description"
        )
    profile = ROLE_PROFILES[role]
    floor = profile.floor

    cores = capabilities.cpu.logical_cores
    if cores is None or cores < floor.minimum_cores:
        return f"{role.value} needs {floor.minimum_cores} cores, node reports {cores}"

    memory = capabilities.memory.total_mb
    if memory is None or memory < floor.minimum_memory_mb:
        return f"{role.value} needs {floor.minimum_memory_mb}MB, node reports {memory}"

    if floor.architectures and capabilities.cpu.architecture not in floor.architectures:
        return (
            f"{role.value} needs one of {sorted(floor.architectures)}, node reports "
            f"{capabilities.cpu.architecture}"
        )

    if profile.requires_accelerator and not usable_accelerators(capabilities):
        return (
            f"{role.value} needs a usable GPU runtime; node has none "
            f"({_accelerator_detail(capabilities)})"
        )
    return None


def _accelerator_detail(capabilities: NodeCapabilities) -> str:
    """Say what was found, so a refusal is diagnosable rather than merely negative."""
    devices = capabilities.gpus
    if devices is None:
        return "GPU enumeration failed"
    if not devices:
        return "no display adapter"
    described = ", ".join(
        f"{device.vendor.value} {device.model}"
        + ("" if device.vram_mb is None else f" {device.vram_mb}MB")
        for device in devices
    )
    return f"detected {described}, no supported runtime answered"


def suggested_role(capabilities: NodeCapabilities) -> NodeRole:
    """The role an installer should *propose* for this machine. A suggestion, never an
    assignment.

    Deliberately never proposes `COORDINATOR`. Exactly one node may own the ledger, and which
    one is not a fact about hardware -- a second machine inferring the control-plane role from
    its own specs is how two writers end up on one durable store. Promoting a node to
    coordinator stays an explicit configuration act.

    The rule is deterministic, per CLAUDE.md's requirement that an unspecified preference be
    resolved by a stated rule rather than a judgement call:

    1. A machine that cannot meet the worker floor is a `WATCHDOG`.
    2. A machine with a usable accelerator is a `GPU_WORKER`.
    3. Everything else is a `WORKER`.
    """
    if role_is_supportable(NodeRole.WORKER, capabilities) is not None:
        return NodeRole.WATCHDOG
    if usable_accelerators(capabilities):
        return NodeRole.GPU_WORKER
    return NodeRole.WORKER


def supportable_roles(capabilities: NodeCapabilities) -> tuple[NodeRole, ...]:
    """Every role this machine could carry, for an installer to offer as alternatives."""
    return tuple(role for role in NodeRole if role_is_supportable(role, capabilities) is None)


def windows_host_notes(capabilities: NodeCapabilities) -> tuple[str, ...]:
    """Platform caveats worth surfacing to an operator, stated as findings rather than errors.

    None of these block an installation. They exist because a node that silently underperforms
    is worse than one that says what it cannot do.
    """
    if capabilities.operating_system != "win32":
        return ()
    notes: list[str] = []
    devices = capabilities.gpus or ()
    amd = [device for device in devices if device.vendor is GpuVendor.AMD and not device.integrated]
    if amd and sys.platform == "win32":
        described = ", ".join(f"{device.model}" for device in amd)
        notes.append(
            f"{described} is present but no CUDA runtime exists for it, and ROCm has no "
            "supported Windows runtime for consumer RDNA parts. This node will not be routed "
            "CUDA work."
        )
    if capabilities.runtimes.wsl is None:
        notes.append(
            "No WSL distribution is installed. Linux-only sandboxing and any systemd unit "
            "deployment are unavailable until one is."
        )
    if capabilities.runtimes.docker is None:
        notes.append("No container runtime answered; container-isolated jobs cannot run here.")
    return tuple(notes)


__all__ = [
    "ROLE_PROFILES",
    "AcceleratorRuntime",
    "NodeRole",
    "RoleProfile",
    "cuda_vram_mb",
    "role_is_supportable",
    "suggested_role",
    "supportable_roles",
    "usable_accelerators",
    "windows_host_notes",
]
