"""What this machine is, measured rather than declared (#38).

**Detection only.** Nothing here decides whether a capability is *sufficient* for anything.
That judgement lives in `quantbot.node.policy`, and keeping the two apart is the point rather
than a tidiness preference.

The defect this corrects is in `quantbot.placement._gpu_vram_mb`, which returns `0` when
`nvidia-smi` is absent. That is the right answer to "how much CUDA memory is there" and the
wrong answer to "what GPU does this machine have". On an AMD host it erases an 8 GB Radeon and
reports a machine with no graphics hardware at all -- so a job needing VRAM without needing
CUDA would be refused a card that could have run it, and an operator reading the number is told
something false about their own machine.

A probe that answers "not eligible" has already made a decision, and that decision is invisible
to whoever reads the number later. So detection reports the device; policy decides eligibility.

Two properties are carried over from `quantbot.placement`, because they are why that module is
trustworthy:

- **Measured, not declared.** `NodeCapabilities.measure()` reads the machine and stamps a
  module-private token. A profile built any other way is `DECLARED` and policy refuses to act
  on it, *including* one that sets `provenance=MEASURED` explicitly -- the enum value is not
  the evidence.
- **Unknown is not zero.** A field that could not be read is `None`. A host with no GPU has
  been measured; a host whose GPUs could not be enumerated has not. Collapsing those two into
  the same decision is how an undersized machine gets work it cannot carry.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self

from quantbot.placement import HostProvenance

#: Module-private, mirroring `quantbot.placement._MEASURED`. Capabilities carrying this were
#: produced by reading a machine. Constructing one elsewhere requires reaching into this module.
_MEASURED = object()

#: Windows device class GUID for display adapters. Reading the driver registry avoids spawning
#: PowerShell for what is a handful of key lookups.
_DISPLAY_CLASS_GUID = "{4d36e968-e325-11ce-bfc1-08002be10318}"

#: PCI vendor IDs as reported by Linux sysfs.
_PCI_VENDORS = {
    "0x10de": "NVIDIA",
    "0x1002": "AMD",
    "0x1022": "AMD",
    "0x8086": "INTEL",
    "0x106b": "APPLE",
}

_BYTES_PER_MB = 1024 * 1024
_BYTES_PER_GB = 1024 * 1024 * 1024


class GpuVendor(StrEnum):
    """Who made the card. Not a statement about what it can run."""

    NVIDIA = "NVIDIA"
    AMD = "AMD"
    INTEL = "INTEL"
    APPLE = "APPLE"
    #: The adapter was enumerated but its vendor could not be identified. Distinct from the
    #: device being absent, and distinct from it being unusable.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class GpuDevice:
    """One display adapter as the machine describes it.

    `vram_mb` is `None` when the adapter was found but its memory could not be read. An
    integrated GPU sharing system memory legitimately reports a small number here; that is a
    measurement, not a defect.
    """

    vendor: GpuVendor
    model: str
    vram_mb: int | None
    #: True when this device is an integrated adapter sharing system memory. Recorded because
    #: it changes what the VRAM figure means, not because it disqualifies anything.
    integrated: bool = False


@dataclass(frozen=True, slots=True)
class CpuInfo:
    architecture: str
    logical_cores: int | None
    model: str


@dataclass(frozen=True, slots=True)
class MemoryInfo:
    total_mb: int | None
    available_mb: int | None


@dataclass(frozen=True, slots=True)
class StorageVolume:
    """Free space where Elrond would actually write, not the size of the boot disk."""

    path: str
    total_gb: int | None
    free_gb: int | None


@dataclass(frozen=True, slots=True)
class RuntimeSupport:
    """Which host runtimes answered.

    A version string means the tool ran, not merely that a file exists on `PATH`. An
    unexecutable stub is the failure mode this distinction avoids.
    """

    docker: str | None = None
    wsl: str | None = None
    nvidia_smi: str | None = None
    rocm_smi: str | None = None


@dataclass(frozen=True, slots=True)
class NodeCapabilities:
    """Everything this node can report about itself, with no policy applied."""

    hostname: str
    operating_system: str
    os_release: str
    python_version: str
    cpu: CpuInfo
    memory: MemoryInfo
    storage: tuple[StorageVolume, ...]
    #: `None` means enumeration failed. An empty tuple means the machine was read and has no
    #: display adapter, which is a normal and decision-grade answer.
    gpus: tuple[GpuDevice, ...] | None
    runtimes: RuntimeSupport
    measured_at: datetime
    provenance: HostProvenance = HostProvenance.DECLARED
    _token: object | None = field(default=None, repr=False, compare=False)

    @classmethod
    def measure(cls, *, now: datetime | None = None, state_dir: Path | None = None) -> Self:
        """Read this machine. The only route to capabilities that policy will act on."""
        return cls(
            hostname=platform.node() or "unknown",
            operating_system=sys.platform,
            os_release=platform.release(),
            python_version=platform.python_version(),
            cpu=_cpu_info(),
            memory=_memory_info(),
            storage=_storage_volumes(state_dir),
            gpus=_detect_gpus(),
            runtimes=_runtime_support(),
            measured_at=now or datetime.now(UTC),
            provenance=HostProvenance.MEASURED,
            _token=_MEASURED,
        )

    @property
    def is_measured(self) -> bool:
        return self._token is _MEASURED and self.provenance is HostProvenance.MEASURED

    def summary(self) -> dict[str, object]:
        """A rendering safe to log. Contains no credential."""
        return {
            "hostname": self.hostname,
            "operating_system": self.operating_system,
            "architecture": self.cpu.architecture,
            "logical_cores": self.cpu.logical_cores,
            "total_memory_mb": self.memory.total_mb,
            "gpus": (
                None
                if self.gpus is None
                else [
                    {"vendor": gpu.vendor.value, "model": gpu.model, "vram_mb": gpu.vram_mb}
                    for gpu in self.gpus
                ]
            ),
            "docker": self.runtimes.docker,
            "wsl": self.runtimes.wsl,
            "provenance": self.provenance.value,
        }


def _cpu_info() -> CpuInfo:
    return CpuInfo(
        architecture=platform.machine().lower(),
        logical_cores=os.cpu_count(),
        model=_cpu_model(),
    )


def _cpu_model() -> str:
    """The marketing name where the platform offers one, else the architecture."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            if isinstance(value, str) and value.strip():
                return value.strip()
        except OSError:
            pass
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    _, _, name = line.partition(":")
                    if name.strip():
                        return name.strip()
        except OSError:
            pass
    return platform.processor() or platform.machine() or "unknown"


def _memory_info() -> MemoryInfo:
    """Total and available physical memory, or `None` for whichever cannot be read.

    Deliberately reads *available* as well as total. Placement sizes jobs against a machine
    capacity; an installer and `doctor` need to know what is free right now, and reporting only
    the nameplate figure would let a node look healthy while it is out of memory.

    `else`, not a fallthrough, with each platform's import inside its own branch. That is what
    lets mypy check both halves from either runner. A Windows-only body in a separate function
    is analysed on Linux too, where `ctypes.windll` does not exist -- which is the defect class
    the CI platform matrix was added to catch.
    """
    if sys.platform == "win32":
        import ctypes

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
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return MemoryInfo(total_mb=None, available_mb=None)
        return MemoryInfo(
            total_mb=int(status.ullTotalPhys) // _BYTES_PER_MB,
            available_mb=int(status.ullAvailPhys) // _BYTES_PER_MB,
        )
    else:
        try:
            text = Path("/proc/meminfo").read_text(encoding="utf-8")
        except OSError:
            return MemoryInfo(total_mb=None, available_mb=None)
        values: dict[str, int] = {}
        for line in text.splitlines():
            label, _, rest = line.partition(":")
            parts = rest.split()
            if label in {"MemTotal", "MemAvailable"} and parts and parts[0].isdigit():
                values[label] = int(parts[0]) // 1024
        return MemoryInfo(total_mb=values.get("MemTotal"), available_mb=values.get("MemAvailable"))


def _storage_volumes(state_dir: Path | None) -> tuple[StorageVolume, ...]:
    """Where Elrond writes, plus every mounted volume the platform exposes cheaply.

    The state directory comes first when one is given, because that is the volume whose free
    space actually constrains the node.
    """
    candidates: list[Path] = []
    if state_dir is not None:
        candidates.append(state_dir)
    if sys.platform == "win32":
        candidates.extend(
            Path(f"{letter}:\\")
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").exists()
        )
    else:
        candidates.append(Path("/"))
        candidates.append(Path.home())

    volumes: list[StorageVolume] = []
    seen: set[str] = set()
    for candidate in candidates:
        probe = candidate
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        key = str(probe).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            usage = shutil.disk_usage(probe)
        except OSError:
            volumes.append(StorageVolume(path=str(candidate), total_gb=None, free_gb=None))
            continue
        volumes.append(
            StorageVolume(
                path=str(candidate),
                total_gb=usage.total // _BYTES_PER_GB,
                free_gb=usage.free // _BYTES_PER_GB,
            )
        )
    return tuple(volumes)


def _detect_gpus() -> tuple[GpuDevice, ...] | None:
    """Enumerate display adapters, or `None` when the platform could not be read.

    Every vendor is reported, not only the one with a CUDA runtime. Whether any of them can run
    a given job is a question for `quantbot.node.policy`.
    """
    if sys.platform == "win32":
        return _detect_gpus_windows()
    else:
        # Covers Linux and, by returning `None` when `/sys/class/drm` is absent, every other
        # platform. An unreadable machine is unknown rather than GPU-less.
        return _detect_gpus_linux()


def _detect_gpus_windows() -> tuple[GpuDevice, ...] | None:
    """Read the display adapters out of the driver registry.

    Registry reads rather than a PowerShell round trip: this runs during installation and on
    every `doctor`, and spawning a shell for a handful of key lookups costs a second for
    nothing.

    `HardwareInformation.qwMemorySize` is the adapter's real memory. The WMI `AdapterRAM` field
    is a 32-bit value that saturates near 4 GB, so a probe reading it would report an 8 GB card
    as 4 GB and a 16 GB card as 4 GB -- understating exactly the hardware a placement decision
    turns on.
    """
    if sys.platform == "win32":
        import winreg

        def read_str(key: winreg.HKEYType, name: str) -> str | None:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                return None
            return value.strip() if isinstance(value, str) and value.strip() else None

        def read_int(key: winreg.HKEYType, name: str) -> int | None:
            try:
                value, _ = winreg.QueryValueEx(key, name)
            except OSError:
                return None
            if isinstance(value, int):
                return value
            if isinstance(value, bytes):
                return int.from_bytes(value, "little")
            return None

        try:
            root = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                rf"SYSTEM\CurrentControlSet\Control\Class\{_DISPLAY_CLASS_GUID}",
            )
        except OSError:
            return None

        devices: list[GpuDevice] = []
        with root:
            for index in range(64):
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not name.isdigit():
                    continue
                try:
                    sub = winreg.OpenKey(root, name)
                except OSError:
                    continue
                with sub:
                    description = read_str(sub, "DriverDesc")
                    if description is None:
                        continue
                    vram = read_int(sub, "HardwareInformation.qwMemorySize")
                    devices.append(
                        GpuDevice(
                            vendor=_vendor_from_text(description),
                            model=description,
                            vram_mb=None if vram is None else vram // _BYTES_PER_MB,
                            integrated=_looks_integrated(description),
                        )
                    )
        return tuple(devices)
    else:
        return None


def _detect_gpus_linux() -> tuple[GpuDevice, ...] | None:
    drm = Path("/sys/class/drm")
    if not drm.exists():
        return None
    devices: list[GpuDevice] = []
    for card in sorted(drm.glob("card[0-9]*")):
        if "-" in card.name:  # connector entries such as card0-DP-1
            continue
        device = card / "device"
        vendor_id = _read_text(device / "vendor")
        model = _read_text(device / "product_name") or _read_text(device / "label") or card.name
        vram_raw = _read_text(device / "mem_info_vram_total")
        vram_mb: int | None = None
        if vram_raw is not None and vram_raw.isdigit():
            vram_mb = int(vram_raw) // _BYTES_PER_MB
        vendor = GpuVendor.UNKNOWN
        if vendor_id is not None:
            vendor = _vendor_from_text(_PCI_VENDORS.get(vendor_id.lower(), ""))
        devices.append(
            GpuDevice(
                vendor=vendor,
                model=model,
                vram_mb=vram_mb,
                integrated=_looks_integrated(model),
            )
        )
    return tuple(devices)


def _read_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _vendor_from_text(text: str) -> GpuVendor:
    """Identify the maker from the string the device reports. Identification, not judgement."""
    lowered = text.lower()
    if any(token in lowered for token in ("nvidia", "geforce", "quadro", "rtx", "gtx")):
        return GpuVendor.NVIDIA
    if any(token in lowered for token in ("amd", "radeon", "advanced micro")):
        return GpuVendor.AMD
    if any(token in lowered for token in ("intel", "iris", "arc a")):
        return GpuVendor.INTEL
    if "apple" in lowered:
        return GpuVendor.APPLE
    return GpuVendor.UNKNOWN


def _looks_integrated(model: str) -> bool:
    """Whether the adapter shares system memory, inferred from how it names itself.

    Recorded rather than acted on. A discrete marker wins over a generic one so that
    "AMD Radeon RX 6650M" is not read as integrated merely for containing "Radeon".
    """
    lowered = model.lower()
    discrete = ("rtx", "gtx", "rx ", "arc a", "quadro", "radeon pro")
    if any(marker in lowered for marker in discrete):
        return False
    integrated = ("uhd graphics", "hd graphics", "iris", "vega", "integrated", "(tm) graphics")
    return any(marker in lowered for marker in integrated)


def _runtime_support() -> RuntimeSupport:
    return RuntimeSupport(
        docker=_tool_version("docker", ["--version"]),
        wsl=_wsl_distributions(),
        nvidia_smi=_tool_version("nvidia-smi", ["--version"]),
        rocm_smi=_tool_version("rocm-smi", ["--version"]),
    )


def _tool_version(tool: str, args: list[str]) -> str | None:
    """Run the tool and keep its first line, or `None` if it is absent or does not answer.

    Presence on `PATH` is not capability. Windows ships app-execution-alias stubs that exist,
    resolve, and then refuse to run -- `python.exe` on a machine that has never installed
    Python is the standard example -- so a probe stopping at `shutil.which` would report a
    runtime this machine does not have.
    """
    executable = shutil.which(tool)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[0] if lines else None


def _wsl_distributions() -> str | None:
    """WSL only counts when a distribution is installed.

    `wsl.exe` exists on a stock Windows 11 that has never had WSL enabled; run with no
    distribution it prints its usage text and exits non-zero. Treating the presence of the
    binary as the capability would report WSL support on a machine that cannot run a Linux
    process, which is exactly the claim an installer must not make on the operator behalf.
    """
    executable = shutil.which("wsl")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--list", "--quiet"],
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    # wsl.exe emits UTF-16LE; decoding as UTF-8 would yield NUL-separated text.
    decoded = completed.stdout.decode("utf-16-le", errors="ignore").replace("\x00", "")
    distributions = [line.strip() for line in decoded.splitlines() if line.strip()]
    return ", ".join(distributions) if distributions else None


__all__ = [
    "CpuInfo",
    "GpuDevice",
    "GpuVendor",
    "MemoryInfo",
    "NodeCapabilities",
    "RuntimeSupport",
    "StorageVolume",
]
