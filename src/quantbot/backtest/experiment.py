"""Backward-compatible re-export of the research manifest.

The manifest moved to `quantbot.research.manifest` when #18 gave it full provenance: the
registry needs its canonical JSON and the reproducibility tooling needs its types, and leaving
it under `backtest` made that a cycle. Research scripts import from here, so the names stay.
"""

from quantbot.research.manifest import (
    CodeProvenance,
    DatasetSnapshot,
    EnvironmentFingerprint,
    ExecutionPath,
    ExperimentManifest,
    ExperimentPeriod,
    ModelProvenance,
    ResourceUsage,
    StatisticalPlan,
    StatisticalTest,
    WorkerProvenance,
    canonical_result_json,
    content_id,
    redact,
    write_experiment_manifest,
)

__all__ = [
    "CodeProvenance",
    "DatasetSnapshot",
    "EnvironmentFingerprint",
    "ExecutionPath",
    "ExperimentManifest",
    "ExperimentPeriod",
    "ModelProvenance",
    "ResourceUsage",
    "StatisticalPlan",
    "StatisticalTest",
    "WorkerProvenance",
    "canonical_result_json",
    "content_id",
    "redact",
    "write_experiment_manifest",
]
