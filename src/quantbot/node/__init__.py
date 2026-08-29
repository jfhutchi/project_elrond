"""Node identity, hardware capability detection, and role policy (#38).

Three modules with a deliberate boundary between them:

- `capabilities` reads the machine and reports what is there. It makes no judgements.
- `policy` decides what a measured machine is allowed to do. It reads nothing.
- `identity` holds the node's durable ID, assigned role, and state directory layout.

The split exists because the previous GPU probe conflated the two. `placement._gpu_vram_mb`
returns `0` when `nvidia-smi` is missing, which is a correct CUDA answer and a false hardware
one -- an 8 GB Radeon disappears entirely. Detection now reports the card; policy decides it is
not CUDA-eligible; both facts survive to whoever reads them next.
"""

from quantbot.node.capabilities import (
    CpuInfo,
    GpuDevice,
    GpuVendor,
    MemoryInfo,
    NodeCapabilities,
    RuntimeSupport,
    StorageVolume,
)
from quantbot.node.identity import (
    NodeIdentity,
    NodeIdentityError,
    StateLayout,
    assign_role,
    ensure_identity,
    load_identity,
    resolve_state_dir,
)
from quantbot.node.policy import (
    AcceleratorRuntime,
    NodeRole,
    cuda_vram_mb,
    role_is_supportable,
    suggested_role,
    supportable_roles,
    usable_accelerators,
    windows_host_notes,
)

__all__ = [
    "AcceleratorRuntime",
    "CpuInfo",
    "GpuDevice",
    "GpuVendor",
    "MemoryInfo",
    "NodeCapabilities",
    "NodeIdentity",
    "NodeIdentityError",
    "NodeRole",
    "RuntimeSupport",
    "StateLayout",
    "StorageVolume",
    "assign_role",
    "cuda_vram_mb",
    "ensure_identity",
    "load_identity",
    "resolve_state_dir",
    "role_is_supportable",
    "suggested_role",
    "supportable_roles",
    "usable_accelerators",
    "windows_host_notes",
]
