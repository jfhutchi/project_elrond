"""Detection reads the machine and makes no judgements (#38)."""

from __future__ import annotations

from datetime import UTC, datetime

from quantbot.node.capabilities import (
    CpuInfo,
    GpuVendor,
    MemoryInfo,
    NodeCapabilities,
    RuntimeSupport,
    _looks_integrated,
    _vendor_from_text,
)
from quantbot.placement import HostProvenance


def test_measure_reads_this_machine_and_stamps_measured_provenance() -> None:
    """Assertions are invariants, not values: the numbers belong to whichever host runs this."""
    capabilities = NodeCapabilities.measure()

    assert capabilities.is_measured is True
    assert capabilities.provenance is HostProvenance.MEASURED
    assert capabilities.hostname
    assert capabilities.cpu.architecture
    assert capabilities.cpu.logical_cores is None or capabilities.cpu.logical_cores >= 1
    assert capabilities.memory.total_mb is None or capabilities.memory.total_mb > 0
    assert capabilities.storage, "at least the volume the process is running from"
    assert capabilities.measured_at.tzinfo is not None


def test_capabilities_built_by_hand_are_declared_even_when_they_claim_otherwise() -> None:
    """The token is the evidence. Setting the enum is one keyword argument, and must not work."""
    forged = NodeCapabilities(
        hostname="claimed",
        operating_system="linux",
        os_release="test",
        python_version="3.11.0",
        cpu=CpuInfo(architecture="amd64", logical_cores=64, model="claimed"),
        memory=MemoryInfo(total_mb=131072, available_mb=131072),
        storage=(),
        gpus=(),
        runtimes=RuntimeSupport(),
        measured_at=datetime(2026, 8, 28, tzinfo=UTC),
        provenance=HostProvenance.MEASURED,
    )
    assert forged.is_measured is False


def test_vendors_are_identified_from_what_the_device_calls_itself() -> None:
    assert _vendor_from_text("NVIDIA GeForce RTX 4080 SUPER") is GpuVendor.NVIDIA
    assert _vendor_from_text("AMD Radeon RX 6650M") is GpuVendor.AMD
    assert _vendor_from_text("AMD Radeon(TM) Graphics") is GpuVendor.AMD
    assert _vendor_from_text("Intel(R) UHD Graphics 770") is GpuVendor.INTEL
    assert _vendor_from_text("Apple M2") is GpuVendor.APPLE
    # An adapter nobody recognises is UNKNOWN, which is distinct from there being no adapter.
    assert _vendor_from_text("Microsoft Basic Display Adapter") is GpuVendor.UNKNOWN


def test_a_discrete_card_is_not_called_integrated_merely_for_saying_radeon() -> None:
    """Both adapters in the Omen are AMD and only one shares system memory.

    A substring rule that stopped at "graphics" would mark the RX 6650M integrated and then
    report its 8 GB as shared system memory, which is the kind of quiet misreading that makes a
    capacity figure untrustworthy.
    """
    assert _looks_integrated("AMD Radeon RX 6650M") is False
    assert _looks_integrated("NVIDIA GeForce RTX 4080 SUPER") is False
    assert _looks_integrated("AMD Radeon(TM) Graphics") is True
    assert _looks_integrated("Intel(R) UHD Graphics 770") is True


def test_summary_is_loggable_and_carries_no_path_or_credential() -> None:
    """`doctor` and the installer both log this. It must be safe to put in a file."""
    summary = NodeCapabilities.measure().summary()

    assert set(summary) == {
        "hostname",
        "operating_system",
        "architecture",
        "logical_cores",
        "total_memory_mb",
        "gpus",
        "docker",
        "wsl",
        "provenance",
    }
    assert "storage" not in summary, "volume paths are node-local and not needed in a log line"
    assert summary["provenance"] == "MEASURED"
