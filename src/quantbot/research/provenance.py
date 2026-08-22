"""Linking external-worker artifacts into experiment manifests (#18, #36, #37).

A manifest that records `workers=[("kronos", "kronos-shadow-v1")]` says an engine ran. It does
not say *what it ran as*, and for a model or agent worker that is the entire question. The same
Kronos version against a different checkpoint, or the same prompt template against a source
bundle retrieved a month later, is a different experiment wearing an identical label.

So each worker's own artifact is translated into a `WorkerProvenance` here, in research, rather
than the workers reaching into the manifest. That direction is deliberate: `quantbot.forecasting`
is a worker boundary and must not acquire an opinion about evidence. Elrond is the evidence
authority; it pulls.

Two properties this exists to make mechanical:

- **A changed checkpoint, prompt, seed or source snapshot moves the fingerprint**, so an
  equivalence claim between two runs can be checked rather than asserted.
- **A rerun that reproduces the artifact id ran nothing new.** That distinguishes a cached
  original from a fresh inference, which matters because a "reproduction" that silently
  re-inferred under a newer checkpoint has reproduced nothing.

Neither adapter reports a search cardinality. Cardinality is measured from persisted worker
records, never disclosed by the worker that did the searching (#23), so it is attached by the
caller that can count rows. Absent, `exploratory_only` is true and #11 marks the artifact
`EXPLORATORY_ONLY` -- which is the correct default for an unmeasured burden.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from quantbot.forecasting.models import ForecastRecord, ForecastStatus
from quantbot.research.hindsight import HindsightAssessment
from quantbot.research.manifest import WorkerProvenance, content_id
from quantbot.research.models import ModelResponse
from quantbot.research.semantic import SemanticAnalysis
from quantbot.research.sources import Source

KRONOS_WORKER = "kronos"
SEMANTIC_WORKER = "semantic"


def kronos_provenance(
    record: ForecastRecord, *, search_cardinality: int | None = None
) -> WorkerProvenance:
    """Describe the exact Kronos inference behind a forecast-derived feature.

    Refuses a record that did not succeed. A failed or skipped forecast produced no feature, and
    citing one as the provenance of a result would name an inference that never happened.
    """
    if record.status is not ForecastStatus.SUCCESS or record.worker_forecast is None:
        raise ValueError(
            f"forecast {record.forecast_id} is {record.status.value} and produced no inference "
            "to attribute a result to"
        )
    worker = record.worker_forecast
    request = record.request
    configuration = {
        # Checkpoint identity. The whole point: same version, different weights, different
        # experiment.
        "model_id": request.config.model_id,
        "model_revision": worker.model_revision,
        "model_weight_sha256": request.config.model_weight_sha256,
        "tokenizer_id": request.config.tokenizer_id,
        "tokenizer_revision": worker.tokenizer_revision,
        "tokenizer_weight_sha256": request.config.tokenizer_weight_sha256,
        "kronos_code_revision": worker.kronos_code_revision,
        "integration_version": request.config.integration_version,
        # Inference and sampling parameters, which change the output without changing the model.
        "device": worker.device,
        "horizon": str(request.horizon),
        "lookback": str(request.lookback),
        "sample_count": str(request.config.sample_count),
        "seed": str(request.config.seed),
        "temperature": str(request.config.temperature),
        "top_k": str(request.config.top_k),
        "top_p": str(request.config.top_p),
        # The environment the inference actually ran in, including package versions and platform.
        "runtime_environment_hash": worker.runtime_environment_hash,
        "input_transform_version": request.config.input_transform_version,
        # Recorded rather than filtered on (#36): a reader conditioning on forecast quality needs
        # to know how much of this artifact could not have been real market data.
        "inconsistent_candles": (
            "unmeasured"
            if record.features is None or record.features.inconsistent_candles is None
            else str(record.features.inconsistent_candles)
        ),
    }
    return WorkerProvenance(
        name=KRONOS_WORKER,
        version=request.config.integration_version,
        search_cardinality=search_cardinality,
        artifact_id=record.forecast_id,
        configuration=configuration,
        # The immutable snapshot the model saw, not the symbol or the date. Two runs over the
        # same symbol and date with a revised vintage have different input hashes.
        input_hash=request.snapshot.source_data_hash,
        as_of=request.snapshot.as_of,
        produced_at=worker.generated_at,
    )


def semantic_provenance(
    analysis: SemanticAnalysis,
    assessment: HindsightAssessment,
    response: ModelResponse,
    sources: Sequence[Source],
    *,
    as_of: datetime,
    search_cardinality: int | None = None,
) -> WorkerProvenance:
    """Describe the exact semantic analysis behind a claim or candidate.

    The source bundle is content-addressed over each source's identity **and content hash**, so
    re-running the same analysis after a source was revised or a new one appeared produces a
    different `input_hash`. A later run therefore cannot claim equivalence to the original
    point-in-time artifact even though the hypothesis, prompt and model are unchanged -- which is
    the failure this linkage exists to prevent.

    Refuses an analysis the hindsight assessment did not clear. Recording provenance for a
    contaminated analysis would make it citable, and the refusal in `SemanticWorker` exists
    precisely so it is not.
    """
    if not assessment.usable_as_evidence:
        raise ValueError(
            "a semantic analysis that failed its hindsight assessment has no provenance to "
            "record; it is not evidence"
        )
    if not sources:
        raise ValueError("a semantic analysis over no sources has no input to attribute")

    provenance = response.provenance()
    bundle = content_id(
        [
            {
                "content_hash": source.content_hash,
                "parser_version": source.parser_version,
                "published_at": source.published_at.isoformat(),
                "source_id": source.source_id,
            }
            for source in sorted(sources, key=lambda item: item.source_id)
        ]
    )
    configuration = {
        "model": provenance.model,
        "prompt_template_hash": provenance.prompt_template_hash,
        # Disagreement is a first-class output (#37) and part of what a later reader is citing.
        # Recorded as a count so its presence is visible without the manifest carrying prose.
        "preserved_disagreements": str(len(analysis.disagreements)),
        "confounders": str(len(analysis.confounders)),
        "falsification_tests": str(len(analysis.falsification_tests)),
    }
    configuration.update(assessment.manifest_entry())
    return WorkerProvenance(
        name=SEMANTIC_WORKER,
        version=provenance.model,
        search_cardinality=search_cardinality,
        # One analysis is one artifact; its identity is its inputs and what it returned.
        artifact_id=content_id(
            {
                "analysis": analysis.model_dump(mode="json"),
                "bundle": bundle,
                "configuration": configuration,
            }
        ),
        configuration=configuration,
        input_hash=bundle,
        as_of=as_of,
        produced_at=as_of,
    )


__all__ = ["KRONOS_WORKER", "SEMANTIC_WORKER", "kronos_provenance", "semantic_provenance"]
