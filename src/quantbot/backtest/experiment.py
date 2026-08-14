"""Immutable research manifests and canonical result serialization."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f") if normalized != 0 else "0"
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    return value


def canonical_result_json(value: object) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


class ExperimentPeriod(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> ExperimentPeriod:
        if self.end < self.start:
            raise ValueError("experiment period end cannot precede start")
        return self


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    git_commit: str
    data_hash: str
    configuration_hash: str
    costs: dict[str, str]
    periods: tuple[ExperimentPeriod, ...]
    walk_forward_splits: tuple[ExperimentPeriod, ...]
    holdout: ExperimentPeriod
    neighborhood_grid: dict[str, tuple[int, ...]]
    ablations: tuple[str, ...]
    results: dict[str, dict[str, str]]

    @field_validator("experiment_id", "git_commit", "data_hash", "configuration_hash")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("manifest identity fields must be nonempty without whitespace")
        return value

    @model_validator(mode="after")
    def validate_research_matrix(self) -> ExperimentManifest:
        required_costs = {"slippage_bps", "commission_per_order"}
        if not required_costs <= set(self.costs):
            raise ValueError("manifest costs must include slippage and commission")
        if not self.periods or not self.walk_forward_splits:
            raise ValueError("manifest requires research periods and walk-forward splits")
        names = [period.name for period in (*self.periods, *self.walk_forward_splits)]
        if len(names) != len(set(names)):
            raise ValueError("experiment period names must be unique")
        if not self.neighborhood_grid or any(
            not values for values in self.neighborhood_grid.values()
        ):
            raise ValueError("neighborhood grid entries must be nonempty")
        if len(self.ablations) != len(set(self.ablations)):
            raise ValueError("experiment ablations must be unique")
        if not self.results:
            raise ValueError("experiment manifest requires result summaries")
        return self

    def canonical_json(self) -> str:
        return canonical_result_json(self)

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def write_experiment_manifest(path: str | Path, manifest: ExperimentManifest) -> bool:
    """Create an immutable manifest, accepting only an exact repeat."""
    destination = Path(path)
    encoded = manifest.canonical_json() + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as output:
            output.write(encoded)
    except FileExistsError:
        if destination.read_text(encoding="utf-8") == encoded:
            return False
        raise FileExistsError(
            f"experiment manifest already exists with different data: {destination}"
        ) from None
    return True
