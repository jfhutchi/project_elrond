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

import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.forecasting.burden import forecast_burden
from quantbot.forecasting.models import ForecastRecord, ForecastStatus
from quantbot.research.hindsight import HindsightAssessment
from quantbot.research.manifest import WorkerProvenance, content_id
from quantbot.research.models import ModelResponse
from quantbot.research.semantic import SemanticAnalysis
from quantbot.research.sources import Source
from quantbot.storage import encode_utc
from quantbot.storage.schema import search_runs

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


def measured_kronos_provenance(session: Session, record: ForecastRecord) -> WorkerProvenance:
    """Kronos provenance whose search burden is counted from the forecast ledger.

    The difference from passing `search_cardinality` by hand is the whole point of #23: this
    number is derived from `model_forecasts` rows, not asserted by the party that benefits from a
    smaller one. A caller that swept twenty lookbacks and kept the best has twenty rows, and this
    counts twenty whether or not it mentions them.

    The count covers every configuration run against this symbol, because the burden a result
    carries is the search it came out of, not the single configuration that won.
    """
    burden = forecast_burden(session, symbols=[record.request.snapshot.symbol])
    if burden.configurations == 0:
        # The record has to be in the ledger it is being counted against. A zero here means it is
        # not, and reporting a burden of zero for an inference that demonstrably happened would
        # be the most flattering possible answer to a question nobody could check.
        raise ValueError(
            f"forecast {record.forecast_id} is not present in the ledger being counted; a "
            "measured burden cannot be taken from a ledger that does not hold the artifact"
        )
    return kronos_provenance(record, search_cardinality=burden.configurations)


def record_semantic_analysis(
    session: Session,
    provenance: WorkerProvenance,
    sources: Sequence[Source],
    *,
    hypothesis_id: str,
    started_at: datetime,
    finished_at: datetime,
) -> str:
    """Persist one semantic analysis as a durable search record (#23, #37).

    One call is one search of cardinality one. A caller that sweeps five prompts and keeps the
    best answer leaves five rows, and the burden it carries is five whether or not it says so.
    That is the entire point: an agent's search volume has to be countable from records rather
    than disclosed by the agent, because the party that swept is the party that benefits from a
    smaller number.

    Reuses `search_runs` rather than adding a table. A source bundle genuinely is data the
    worker searched over, and its publication range genuinely is the window that search spent,
    so nothing here is forced into a column that means something else.

    Append-only, inheriting `record_search_run`'s reasoning: a search that happened cannot
    un-happen, and a count that can be revised downward is a count that can be understated.
    """
    if not sources:
        raise ValueError("a semantic analysis over no sources has no search to record")
    if provenance.name != SEMANTIC_WORKER:
        raise ValueError(f"{provenance.name} is not a semantic analysis")
    search_id = f"semantic:{hypothesis_id}:{provenance.artifact_id}"
    existing = session.execute(
        select(search_runs.c.search_id).where(search_runs.c.search_id == search_id)
    ).one_or_none()
    if existing is not None:
        # The same analysis over the same bundle under the same configuration is the same
        # search. Counting it twice would overstate the burden, which is its own dishonesty.
        return search_id

    published = sorted(source.published_at for source in sources)
    session.execute(
        search_runs.insert().values(
            search_id=search_id,
            actor=SEMANTIC_WORKER,
            actor_version=provenance.version,
            candidates_evaluated=1,
            dataset=f"sources:{provenance.input_hash}",
            data_role="DISCOVERY",
            start_date=published[0].date().isoformat(),
            end_date=published[-1].date().isoformat(),
            started_at=encode_utc(started_at),
            finished_at=encode_utc(finished_at),
            configuration_hash=provenance.fingerprint,
            detail_json=json.dumps(
                {
                    "artifact_id": provenance.artifact_id,
                    "configuration": dict(provenance.configuration),
                    "hypothesis_id": hypothesis_id,
                    "sources": sorted(source.source_id for source in sources),
                },
                sort_keys=True,
            ),
        )
    )
    return search_id


def measured_semantic_cardinality(session: Session, *, hypothesis_id: str) -> int:
    """Count the semantic analyses run against one hypothesis, from rows.

    Keyed on the hypothesis rather than the source bundle, deliberately. A sweep that varies the
    sources as well as the prompt has searched more, not less, and keying on the bundle would
    quietly drop every variation onto its own untouched counter -- the understatement this
    accounting exists to prevent.

    Filtered in Python rather than through `json_extract` so the query does not depend on the
    SQLite build. Semantic analyses are expensive enough that this table stays small; when it
    does not, this wants an index and a column rather than a cleverer query.
    """
    rows = session.execute(
        select(search_runs.c.candidates_evaluated, search_runs.c.detail_json).where(
            search_runs.c.actor == SEMANTIC_WORKER
        )
    ).all()
    return sum(
        int(evaluated)
        for evaluated, detail in rows
        if json.loads(str(detail)).get("hypothesis_id") == hypothesis_id
    )


def measured_semantic_provenance(
    session: Session, provenance: WorkerProvenance, *, hypothesis_id: str
) -> WorkerProvenance:
    """Attach the counted burden to a semantic analysis's provenance.

    Refuses a count of zero for the same reason the Kronos counterpart does: the analysis has to
    be in the ledger it is being counted against, and a burden of zero for work that
    demonstrably happened is the most flattering possible answer to a question nobody could
    check.
    """
    counted = measured_semantic_cardinality(session, hypothesis_id=hypothesis_id)
    if counted == 0:
        raise ValueError(
            f"no semantic analysis is recorded against {hypothesis_id}; a measured burden "
            "cannot be taken from a ledger that does not hold the artifact"
        )
    return provenance.model_copy(update={"search_cardinality": counted})


__all__ = [
    "KRONOS_WORKER",
    "SEMANTIC_WORKER",
    "kronos_provenance",
    "measured_kronos_provenance",
    "measured_semantic_cardinality",
    "measured_semantic_provenance",
    "record_semantic_analysis",
    "semantic_provenance",
]
