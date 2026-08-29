"""Policy decides eligibility; detection keeps the facts (#38).

The assertions here are written against the two machines this project actually has, because the
defect being corrected is invisible against synthetic hardware. An AMD host with no CUDA runtime
is the case where the old probe returned `0` and erased an 8 GB card, and it is the case the
operator's second machine presents.
"""

from __future__ import annotations

from datetime import UTC, datetime

from quantbot.node.capabilities import (
    CpuInfo,
    GpuDevice,
    GpuVendor,
    MemoryInfo,
    NodeCapabilities,
    RuntimeSupport,
)
from quantbot.node.policy import (
    ROLE_PROFILES,
    AcceleratorRuntime,
    NodeRole,
    cuda_vram_mb,
    role_is_supportable,
    suggested_role,
    supportable_roles,
    usable_accelerators,
    windows_host_notes,
)
from quantbot.placement import HostProvenance

NOW = datetime(2026, 8, 28, 22, tzinfo=UTC)

#: Measured on the HP Omen 16-n0xxx, 2026-08-28. The discrete card and the 680M that shares
#: system memory, exactly as the driver registry reports them.
OMEN_DISCRETE = GpuDevice(GpuVendor.AMD, "AMD Radeon RX 6650M", 8192, integrated=False)
OMEN_INTEGRATED = GpuDevice(GpuVendor.AMD, "AMD Radeon(TM) Graphics", 512, integrated=True)
#: The RTX desktop, from the profile recorded in docs/runtime-topology.md.
DESKTOP_CUDA = GpuDevice(GpuVendor.NVIDIA, "NVIDIA GeForce RTX 4080 SUPER", 16376)


def measured(
    *,
    gpus: tuple[GpuDevice, ...] | None,
    nvidia_smi: str | None = None,
    rocm_smi: str | None = None,
    operating_system: str = "linux",
    logical_cores: int | None = 16,
    total_memory_mb: int | None = 31955,
) -> NodeCapabilities:
    """Capabilities carrying measured provenance, built the way `measure()` builds them.

    Reaches for the module-private token deliberately, matching `tests/unit/test_placement.py`:
    the production route reads the machine under test, which would make every assertion here a
    description of the CI runner rather than of the rule.
    """
    from quantbot.node import capabilities as module  # noqa: PLC0415

    return NodeCapabilities(
        hostname="node-under-test",
        operating_system=operating_system,
        os_release="test",
        python_version="3.11.0",
        cpu=CpuInfo(architecture="amd64", logical_cores=logical_cores, model="test cpu"),
        memory=MemoryInfo(total_mb=total_memory_mb, available_mb=total_memory_mb),
        storage=(),
        gpus=gpus,
        runtimes=RuntimeSupport(nvidia_smi=nvidia_smi, rocm_smi=rocm_smi),
        measured_at=NOW,
        provenance=HostProvenance.MEASURED,
        _token=module._MEASURED,
    )


def test_an_amd_node_keeps_its_card_and_is_still_refused_cuda() -> None:
    """The defect this module exists to correct, stated as one assertion.

    `placement._gpu_vram_mb` returns `0` on this machine because `nvidia-smi` is absent. That
    answer is correct about CUDA and false about the hardware, and the old code had no way to
    say both. Detection must still report 8192 MB after policy has decided the card is
    ineligible -- otherwise the operator is told they own a machine with no GPU.
    """
    omen = measured(gpus=(OMEN_DISCRETE, OMEN_INTEGRATED), operating_system="win32")

    assert omen.gpus is not None
    assert omen.gpus[0].vram_mb == 8192, "detection must not lose the card policy rejected"
    assert omen.gpus[0].vendor is GpuVendor.AMD

    assert AcceleratorRuntime.CUDA not in usable_accelerators(omen)
    assert cuda_vram_mb(omen) == 0, "no CUDA memory is reachable, which is a measured 0"


def test_a_card_is_not_a_runtime() -> None:
    """An NVIDIA GPU with no `nvidia-smi` cannot be dispatched CUDA work.

    A machine whose driver has been removed or whose toolkit never landed still enumerates the
    adapter. Treating the presence of silicon as the capability would route work to a host that
    cannot execute it, which is the failure `place()` was written to prevent one layer up.
    """
    without_runtime = measured(gpus=(DESKTOP_CUDA,), nvidia_smi=None)
    assert usable_accelerators(without_runtime) == frozenset()
    assert cuda_vram_mb(without_runtime) == 0

    with_runtime = measured(gpus=(DESKTOP_CUDA,), nvidia_smi="NVIDIA-SMI 560.94")
    assert AcceleratorRuntime.CUDA in usable_accelerators(with_runtime)
    assert cuda_vram_mb(with_runtime) == 16376


def test_unreadable_gpus_are_unknown_rather_than_absent() -> None:
    """`None` and `0` must not collapse.

    A node whose adapters could not be enumerated has not been shown to lack a GPU; it has been
    shown to be unreadable. Returning `0` would let it satisfy a "no GPU required" check by
    accident and would report a hardware fact nobody established.
    """
    unreadable = measured(gpus=None, nvidia_smi="NVIDIA-SMI 560.94")

    assert cuda_vram_mb(unreadable) is None
    assert usable_accelerators(unreadable) == frozenset()

    no_gpu = measured(gpus=())
    assert cuda_vram_mb(no_gpu) == 0, "a machine read and found to have no adapter is a 0"


def test_an_nvidia_card_whose_memory_is_unreadable_is_not_reported_as_zero() -> None:
    """The same distinction on the other side of the runtime check.

    A live CUDA runtime with an unreadable memory figure is unknown capacity. Reporting `0`
    would make it indistinguishable from a machine with no CUDA at all, and reporting anything
    larger would be invention.
    """
    node = measured(
        gpus=(GpuDevice(GpuVendor.NVIDIA, "NVIDIA GeForce RTX 4080 SUPER", None),),
        nvidia_smi="NVIDIA-SMI 560.94",
    )
    assert cuda_vram_mb(node) is None


def test_rocm_is_refused_on_windows_and_permitted_on_linux() -> None:
    """A policy decision with a stated reason, asserted where it can be seen.

    AMD ships no supported Windows ROCm runtime for consumer RDNA2 parts. The card and its
    memory are still detected on both platforms -- only eligibility differs -- so the day this
    changes it is one edit here and not a re-probe.
    """
    windows = measured(gpus=(OMEN_DISCRETE,), rocm_smi="ROCm 6.0", operating_system="win32")
    assert AcceleratorRuntime.ROCM not in usable_accelerators(windows)
    assert windows.gpus is not None and windows.gpus[0].vram_mb == 8192

    linux = measured(gpus=(OMEN_DISCRETE,), rocm_smi="ROCm 6.0", operating_system="linux")
    assert AcceleratorRuntime.ROCM in usable_accelerators(linux)


def test_declared_capabilities_cannot_earn_a_role() -> None:
    """The token check, inherited from placement and worth re-asserting here.

    A node that could describe itself into a role is not gated. The enum value is not the
    evidence, so setting `provenance=MEASURED` by hand must not be enough.
    """
    forged = NodeCapabilities(
        hostname="generous",
        operating_system="linux",
        os_release="test",
        python_version="3.11.0",
        cpu=CpuInfo(architecture="amd64", logical_cores=64, model="claimed"),
        memory=MemoryInfo(total_mb=131072, available_mb=131072),
        storage=(),
        gpus=(DESKTOP_CUDA,),
        runtimes=RuntimeSupport(nvidia_smi="NVIDIA-SMI 560.94"),
        measured_at=NOW,
        provenance=HostProvenance.MEASURED,
    )

    assert forged.is_measured is False
    assert role_is_supportable(NodeRole.WORKER, forged) is not None
    assert "not measured" in (role_is_supportable(NodeRole.WORKER, forged) or "")
    assert usable_accelerators(forged) == frozenset()
    assert supportable_roles(forged) == ()


def test_a_capable_machine_may_hold_a_restricted_role() -> None:
    """Role is assigned, not derived.

    The operator must be able to give a powerful machine a small job. If the only way to
    restrict a node were to understate its hardware, the capability system would be teaching
    people to lie to it.
    """
    powerful = measured(
        gpus=(DESKTOP_CUDA,),
        nvidia_smi="NVIDIA-SMI 560.94",
        logical_cores=32,
        total_memory_mb=32486,
    )

    assert role_is_supportable(NodeRole.WATCHDOG, powerful) is None
    assert ROLE_PROFILES[NodeRole.WATCHDOG].accepts_dispatch is False
    assert NodeRole.GPU_WORKER in supportable_roles(powerful)


def test_a_gpu_worker_role_is_refused_without_a_usable_runtime() -> None:
    """The role floor consults policy, not the presence of a card.

    The Omen supports every CPU role and not this one, and the refusal names what was found so
    an operator can tell "no GPU" from "GPU this project cannot use".
    """
    omen = measured(gpus=(OMEN_DISCRETE, OMEN_INTEGRATED), operating_system="win32")

    reason = role_is_supportable(NodeRole.GPU_WORKER, omen)
    assert reason is not None
    assert "RX 6650M" in reason and "8192MB" in reason
    assert NodeRole.GPU_WORKER not in supportable_roles(omen)
    assert NodeRole.WORKER in supportable_roles(omen)
    # The model string already carries the vendor. Concatenating both produced
    # "AMD AMD Radeon RX 6650M" against the real machine, which makes a refusal harder to read
    # than it needs to be.
    assert "AMD AMD" not in reason


def test_a_card_whose_vendor_is_unrecognised_is_still_named_in_a_refusal() -> None:
    """An unknown vendor must not prepend the literal word UNKNOWN to the model.

    The refusal exists to tell an operator which adapter was rejected. "UNKNOWN Microsoft Basic
    Display Adapter" reads as a second failure rather than as the card's name.
    """
    node = measured(gpus=(GpuDevice(GpuVendor.UNKNOWN, "Microsoft Basic Display Adapter", None),))

    reason = role_is_supportable(NodeRole.GPU_WORKER, node)
    assert reason is not None
    assert "Microsoft Basic Display Adapter" in reason
    assert "UNKNOWN" not in reason


def test_no_measured_machine_is_ever_suggested_as_coordinator() -> None:
    """Exactly one node owns the ledger, and hardware cannot decide which.

    A second machine inferring the control-plane role from its own specs is how two writers end
    up on one durable store. Asserted across the range rather than on one example, because the
    property must hold for hardware this project has not seen yet.
    """
    candidates = [
        measured(gpus=(), logical_cores=1, total_memory_mb=434),
        measured(gpus=(OMEN_DISCRETE,), operating_system="win32"),
        measured(gpus=(DESKTOP_CUDA,), nvidia_smi="NVIDIA-SMI 560.94", total_memory_mb=131072),
        measured(gpus=None),
    ]
    for node in candidates:
        assert suggested_role(node) is not NodeRole.COORDINATOR


def test_suggested_roles_follow_the_stated_rule() -> None:
    """The deterministic rule CLAUDE.md requires when a preference is unspecified."""
    tiny = measured(gpus=(), logical_cores=1, total_memory_mb=434)
    assert suggested_role(tiny) is NodeRole.WATCHDOG

    omen = measured(gpus=(OMEN_DISCRETE,), operating_system="win32")
    assert suggested_role(omen) is NodeRole.WORKER

    desktop = measured(gpus=(DESKTOP_CUDA,), nvidia_smi="NVIDIA-SMI 560.94")
    assert suggested_role(desktop) is NodeRole.GPU_WORKER


def test_windows_notes_explain_the_amd_refusal_without_blocking_it() -> None:
    """A node that silently underperforms is worse than one that says what it cannot do."""
    omen = measured(gpus=(OMEN_DISCRETE, OMEN_INTEGRATED), operating_system="win32")

    notes = windows_host_notes(omen)
    assert any("RX 6650M" in note for note in notes)
    assert any("CUDA" in note for note in notes)
    # The integrated adapter is not named: it was never a candidate for dispatch.
    assert not any("Radeon(TM) Graphics" in note for note in notes)
    assert role_is_supportable(NodeRole.WORKER, omen) is None, "notes are findings, not blocks"


def test_linux_hosts_get_no_windows_notes() -> None:
    assert windows_host_notes(measured(gpus=(OMEN_DISCRETE,), operating_system="linux")) == ()
