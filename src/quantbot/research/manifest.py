"""Immutable result bundles: what was run, on what, under which assumptions (#18).

Faithful reproduction of a wrong number is not the goal. Two of the three errors in cycles
11-12 were valid code computing the wrong statistic, and a manifest recording "Sharpe = 1.09"
reproduces that error perfectly. One recording "Sharpe difference, independent-samples
assumption, on two series of the same asset" makes it visible. So the statistical plan is a
first-class part of the bundle, not a note.

Four things here are load-bearing rather than administrative:

1. **Dataset snapshots and vintages.** Silent data revision is the failure mode that makes an
   old result unfalsifiable: you cannot reproduce a conclusion whose inputs changed underneath
   it, and you cannot tell that is what happened.
2. **The cumulative trial count and luck bar as they stood at the run.** That count only moves
   upward, so a result read a year later needs to know which bar it actually cleared.
   Recomputing later gives a harsher answer and makes old results look worse than they were.
3. **The execution path.** A manifest can be complete and reproducible while describing a
   bespoke script that never exercised Elrond. Three of this project's recorded analysis errors
   came from a harness that was not connected to the code under test.
4. **No secrets, enforced rather than intended.** The manifest refuses to serialise a value
   that looks like a credential, so a redaction bug fails loudly instead of publishing a key.

This module deliberately imports nothing from `quantbot.research.registry`: the registry
depends on the canonical JSON here, and a bundle must be writable without the registry present.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Values shaped like credentials. Matched against every string in a manifest before it is
#: serialised, because "we redact secrets" is a claim that should fail loudly when it is wrong.
CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bPK[A-Z0-9]{16,}\b"),  # Alpaca key id
    re.compile(r"\b[A-Za-z0-9/+]{40}\b"),  # 40-char secret
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),  # OpenAI-style
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),  # GitHub token
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
)

#: Placeholder written in place of anything the redactor removes, so the shape of the
#: configuration survives even though the value does not.
REDACTED = "[REDACTED]"


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


def content_id(value: object) -> str:
    """Content address for any manifest fragment: the same inputs give the same id."""
    return hashlib.sha256(canonical_result_json(value).encode("utf-8")).hexdigest()


def looks_like_credential(value: str) -> bool:
    return any(pattern.search(value) for pattern in CREDENTIAL_PATTERNS)


def redact(value: object) -> object:
    """Replace credential-shaped strings while leaving the surrounding structure intact."""
    if isinstance(value, str):
        return REDACTED if looks_like_credential(value) else value
    if isinstance(value, Mapping):
        return {str(key): redact(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [redact(item) for item in value]
    return value


class StatisticalTest(StrEnum):
    """The test actually applied. Named, because the metric alone hides which one was used."""

    ONE_SAMPLE_T = "ONE_SAMPLE_T"
    PAIRED_T = "PAIRED_T"
    WELCH_T = "WELCH_T"
    #: Correct test for a Sharpe difference between two correlated series.
    JOBSON_KORKIE_MEMMEL = "JOBSON_KORKIE_MEMMEL"
    STATIONARY_BOOTSTRAP = "STATIONARY_BOOTSTRAP"
    BINOMIAL = "BINOMIAL"
    SPEARMAN = "SPEARMAN"


#: Which dependence structure each test is valid for. Cycle 11 ran `ONE_SAMPLE_T` on paired
#: data and the overnight premium showed t=2.74; paired, it is 0.63. The same cycle compared two
#: Sharpes of the *same asset* with an independent-samples test; Jobson-Korkie-Memmel gives
#: z=1.01. Both are mechanically detectable from this table.
VALID_FOR: dict[StatisticalTest, frozenset[str]] = {
    StatisticalTest.ONE_SAMPLE_T: frozenset({"SINGLE_SAMPLE"}),
    StatisticalTest.PAIRED_T: frozenset({"PAIRED"}),
    StatisticalTest.WELCH_T: frozenset({"UNPAIRED"}),
    StatisticalTest.JOBSON_KORKIE_MEMMEL: frozenset({"PAIRED"}),
    StatisticalTest.STATIONARY_BOOTSTRAP: frozenset({"PAIRED", "UNPAIRED", "SINGLE_SAMPLE"}),
    StatisticalTest.BINOMIAL: frozenset({"SINGLE_SAMPLE"}),
    StatisticalTest.SPEARMAN: frozenset({"SINGLE_SAMPLE"}),
}


class ExecutionPath(StrEnum):
    """Whether the number came from the system that will trade, or from something beside it."""

    #: Ran through `BacktestEngine` with the config under test actually applied.
    PRODUCTION_ENGINE = "PRODUCTION_ENGINE"
    #: A study script. Legitimate, and the ambiguity is recorded rather than left implicit.
    RESEARCH_SCRIPT = "RESEARCH_SCRIPT"
    #: Agent-generated code under `quantbot.sandbox`.
    SANDBOX = "SANDBOX"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExperimentPeriod(FrozenModel):
    name: str = Field(min_length=1)
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end < self.start:
            raise ValueError("experiment period end cannot precede start")
        return self


class CodeProvenance(FrozenModel):
    """What code ran. A dirty tree means the commit does not describe it."""

    git_commit: Text
    #: True when the working tree had uncommitted changes, which makes the commit insufficient.
    dirty: bool
    configuration_hash: Text
    execution_path: ExecutionPath


class EnvironmentFingerprint(FrozenModel):
    """Enough of the machine to tell two runs apart without identifying the machine."""

    python_version: Text
    platform: Text
    #: Hash of the dependency lock, so an upgraded package produces a distinct manifest.
    dependency_lock_hash: Text
    #: A class, not a hostname: "operator-workstation", "worker", "ci".
    host_class: Text
    random_seed: int | None = None
    deterministic: bool = True


class DatasetSnapshot(FrozenModel):
    """One dataset vintage, with the role it played. Silent revision is the failure mode."""

    dataset: Text
    #: Vintage identifier. A re-pull is a different snapshot even at the same date range.
    snapshot: Text
    role: Literal["DISCOVERY", "VALIDATION", "PROTECTED_EVALUATION", "FORWARD_PAPER"]
    start: date
    end: date
    #: Content hash of the observations actually loaded.
    content_hash: Text
    observations: int = Field(ge=0)
    #: Lineage (#17). Who served it, when it was pulled, the earliest moment any value in it
    #: could have been known, and the version of the code that transformed it. Without the
    #: availability timestamp a bundle cannot show that it avoided look-ahead, only assert it.
    provider: Text = "unrecorded"
    retrieved_at: datetime | None = None
    earliest_available_at: datetime | None = None
    transformation_version: Text = "unrecorded"


class StatisticalPlan(FrozenModel):
    """The test and what it assumes. Recorded so a wrong one is visible, not reproduced."""

    test: StatisticalTest
    #: The dependence structure the *data* has, which the test must be valid for.
    comparison: Literal["PAIRED", "UNPAIRED", "SINGLE_SAMPLE"]
    primary_estimand: Text
    #: Frozen at the run. This number only rises, so recomputing it later judges the result
    #: against a bar it was never asked to clear.
    cumulative_trials: int = Field(ge=0)
    luck_threshold_z: Decimal = Field(allow_inf_nan=False)
    minimum_detectable_effect: Decimal = Field(allow_inf_nan=False)
    #: The variance inflation applied when sizing the sample, from #19.
    variance_inflation: Decimal = Field(gt=0, allow_inf_nan=False)
    multiple_testing_correction: Text
    assumptions: tuple[Text, ...] = ()

    @property
    def test_matches_data(self) -> bool:
        return self.comparison in VALID_FOR[self.test]


class ModelProvenance(FrozenModel):
    """Which model produced a generated artifact, and under what prompt."""

    provider: Text
    model: Text
    #: Hash rather than text, so a prompt change is visible without publishing the prompt.
    prompt_template_hash: Text
    parameters_hash: Text


class WorkerProvenance(FrozenModel):
    """An external research engine. Undisclosed search cardinality bars confirmatory use."""

    name: Text
    version: Text
    search_cardinality: int | None = None

    @property
    def exploratory_only(self) -> bool:
        return self.search_cardinality is None


class ResourceUsage(FrozenModel):
    started_at: datetime
    finished_at: datetime
    peak_memory_mb: int = Field(ge=0)
    cpu_seconds: Decimal = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_span(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("an experiment cannot finish before it starts")
        return self


class ExperimentManifest(FrozenModel):
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

    #: Confirmatory runs must name the registration they were frozen against (#5). The default
    #: is deliberately the weaker claim: a run that forgets to declare one is exploratory, and
    #: therefore cannot be cited as evidence, rather than silently passing as confirmatory.
    mode: Literal["CONFIRMATORY", "EXPLORATORY"] = "EXPLORATORY"
    hypothesis_id: str | None = None
    hypothesis_version: int | None = None
    registration_hash: str | None = None
    #: Carried into the bundle rather than left behind a hash lookup, so a null result reads
    #: "no effect larger than X" instead of the far weaker "no effect found" (#19).
    minimum_detectable_effect: str | None = None
    #: POWERED or OVERRIDDEN. An overridden experiment stays labelled underpowered everywhere
    #: its result is displayed.
    power_verdict: Literal["POWERED", "OVERRIDDEN"] | None = None

    #: Provenance (#18). Optional on an exploratory run and required on a confirmatory one:
    #: a result cited as evidence has to say what produced it.
    code: CodeProvenance | None = None
    environment: EnvironmentFingerprint | None = None
    datasets: tuple[DatasetSnapshot, ...] = ()
    statistics: StatisticalPlan | None = None
    features: dict[str, str] = Field(default_factory=dict)
    models: tuple[ModelProvenance, ...] = ()
    workers: tuple[WorkerProvenance, ...] = ()
    resources: ResourceUsage | None = None
    #: Argv that reproduces this run, recorded so the bundle is actionable rather than merely
    #: descriptive.
    reproduction_command: tuple[str, ...] = ()

    @field_validator("experiment_id", "git_commit", "data_hash", "configuration_hash")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("manifest identity fields must be nonempty without whitespace")
        return value

    @model_validator(mode="after")
    def validate_research_matrix(self) -> Self:
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

        registration = (
            self.hypothesis_id,
            self.hypothesis_version,
            self.registration_hash,
            self.minimum_detectable_effect,
            self.power_verdict,
        )
        provenance = (self.code, self.environment, self.statistics, self.resources)
        if self.mode == "CONFIRMATORY":
            if any(field is None for field in registration):
                raise ValueError(
                    "a confirmatory experiment must name the hypothesis, version, and "
                    "registration hash it was frozen against, and carry the minimum "
                    "detectable effect and power verdict it cleared"
                )
            if len(self.registration_hash or "") != 64:
                raise ValueError("registration_hash must be a SHA-256 hex digest")
            if any(field is None for field in provenance):
                raise ValueError(
                    "a confirmatory experiment must record its code, environment, statistical "
                    "plan, and resource usage"
                )
            if not self.datasets:
                raise ValueError("a confirmatory experiment must record its dataset snapshots")
            unlineaged = [
                snapshot.dataset
                for snapshot in self.datasets
                if snapshot.retrieved_at is None
                or snapshot.earliest_available_at is None
                or snapshot.provider == "unrecorded"
                or snapshot.transformation_version == "unrecorded"
            ]
            if unlineaged:
                raise ValueError(
                    "a confirmatory dataset snapshot must name its provider, retrieval time, "
                    f"availability time and transformation version: {', '.join(unlineaged)}"
                )
        elif any(field is not None for field in registration):
            raise ValueError("an exploratory experiment cannot claim a registration")

        leaked = [
            found
            for found in _strings(self.model_dump(mode="python"))
            if looks_like_credential(found)
        ]
        if leaked:
            raise ValueError("a manifest must not carry credentials; redact before building it")
        return self

    def canonical_json(self) -> str:
        return canonical_result_json(self)

    @property
    def manifest_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def inputs_hash(self) -> str:
        """Content address of everything that determines the result, excluding the result.

        Two runs sharing this must produce the same numbers. Two runs producing the same
        numbers without sharing it are the harness-not-connected failure (#18 comparison).
        """
        payload = self.model_dump(mode="python")
        for produced in ("results", "resources", "experiment_id"):
            payload.pop(produced, None)
        return content_id(payload)


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [found for item in value.values() for found in _strings(item)]
    if isinstance(value, tuple | list):
        return [found for item in value for found in _strings(item)]
    return []


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
