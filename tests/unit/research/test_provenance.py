"""External-worker artifacts linked into manifests (#18, #36, #37).

The acceptance criteria these cover are all the same shape: a result citing a worker must be
traceable to the exact inference behind it, and two runs claiming to be the same experiment must
be mechanically separable when they are not. `name` and `version` cannot do that -- the same
Kronos version against different weights, or the same prompt against a revised source bundle, is
a different experiment wearing an identical label.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.domain import Bar
from quantbot.forecasting.models import (
    ForecastCandle,
    ForecastRecord,
    ForecastStatus,
    KronosConfig,
    WorkerForecast,
)
from quantbot.forecasting.snapshots import build_forecast_request
from quantbot.research.hindsight import assess_hindsight
from quantbot.research.manifest import ExperimentManifest, WorkerProvenance
from quantbot.research.models import (
    CostClass,
    ModelCapabilities,
    ModelResponse,
    ModelSpec,
    PromptTemplate,
)
from quantbot.research.provenance import kronos_provenance, semantic_provenance
from quantbot.research.reproducibility import compare
from quantbot.research.semantic import SemanticAnalysis
from quantbot.research.sources import Source, SourceKind

AS_OF = datetime(2026, 8, 14, 20, tzinfo=UTC)
SOURCE_PROVIDER = "alpaca:iex:all:1day"
SOURCE_METADATA = {"adjustment": "all", "feed": "iex", "provider": "alpaca", "timeframe": "1Day"}


def bars(count: int = 40) -> list[Bar]:
    return [
        Bar(
            symbol="AAPL",
            timestamp=AS_OF + timedelta(days=offset),
            open=Decimal(99 + offset),
            high=Decimal(101 + offset),
            low=Decimal(97 + offset),
            close=Decimal(100 + offset),
            volume="1000",
            adjustment="1",
        )
        for offset in range(-(count - 1), 1)
    ]


def forecast_record(
    *,
    config: KronosConfig | None = None,
    model_revision: str | None = None,
    source: list[Bar] | None = None,
) -> ForecastRecord:
    resolved = config or KronosConfig(sample_count=2)
    request = build_forecast_request(
        bars=source or bars(),
        symbol="AAPL",
        as_of=AS_OF,
        provider=SOURCE_PROVIDER,
        vintage_id="source-vintage-1",
        available_at=AS_OF,
        adjustment_metadata=SOURCE_METADATA,
        lookback=40,
        horizon=2,
        config=resolved,
    )
    candle = ForecastCandle(open="101", high="106", low="99", close="105", volume="1000")
    worker = WorkerForecast(
        request_id=request.request_id,
        sample_paths=tuple((candle, candle) for _ in range(resolved.sample_count)),
        model_revision=model_revision or resolved.model_revision,
        tokenizer_revision=resolved.tokenizer_revision,
        kronos_code_revision=resolved.kronos_code_revision,
        generated_at=AS_OF + timedelta(minutes=2),
        runtime_environment_hash="env-sha256",
        device="cpu",
        model_initialization_seconds=Decimal("1.25"),
        inference_seconds=Decimal("0.75"),
        peak_process_memory_bytes=1234,
    )
    return ForecastRecord.succeeded(request, worker)


def test_a_kronos_feature_traces_to_the_snapshot_and_checkpoint_that_produced_it() -> None:
    """The permitting case: everything needed to find the exact inference again."""
    record = forecast_record()

    worker = kronos_provenance(record)

    assert worker.artifact_id == record.forecast_id
    assert worker.input_hash == record.request.snapshot.source_data_hash
    assert worker.configuration["model_revision"] == KronosConfig().model_revision
    assert worker.configuration["seed"] == "0"
    assert worker.configuration["lookback"] == "40"
    assert worker.produced_at == record.worker_forecast.generated_at


def test_the_same_inference_twice_is_recognisably_the_same_inference() -> None:
    """A rerun reproducing the fingerprint ran nothing new.

    Without this, "we reproduced it" and "we re-inferred it under different weights" are the
    same statement.
    """
    assert kronos_provenance(forecast_record()).fingerprint == (
        kronos_provenance(forecast_record()).fingerprint
    )


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("seed", {"config": KronosConfig(sample_count=2, seed=99)}),
        ("temperature", {"config": KronosConfig(sample_count=2, temperature=Decimal("0.9"))}),
        ("sample_count", {"config": KronosConfig(sample_count=3)}),
        ("source_snapshot", {"source": bars(39)}),
    ],
)
def test_changing_what_produced_the_inference_moves_the_fingerprint(
    label: str, kwargs: object
) -> None:
    """Each of these is a different experiment, and the manifest has to say so.

    A checkpoint or a seed change with an unchanged fingerprint would let a rerun claim
    equivalence to an artifact it did not reproduce.
    """
    assert isinstance(kwargs, dict)
    baseline = kronos_provenance(forecast_record()).fingerprint

    assert kronos_provenance(forecast_record(**kwargs)).fingerprint != baseline, (
        f"a changed {label} left the fingerprint unmoved"
    )


def test_a_different_checkpoint_moves_the_fingerprint() -> None:
    """Asserted on the provenance directly, because `KronosConfig` pins the checkpoint.

    The v1 integration refuses any weights but the pinned Kronos-small, so a second checkpoint
    cannot currently reach a `ForecastRecord` at all. That pin is a property of this integration
    and not of the manifest, and it will be relaxed the first time a second model is evaluated --
    at which point provenance that ignored the checkpoint would let two different models share
    one identity. This guards the property now rather than after that becomes possible.
    """
    baseline = kronos_provenance(forecast_record())

    reweighted = baseline.model_copy(
        update={"configuration": {**baseline.configuration, "model_revision": "0" * 40}}
    )

    assert reweighted.fingerprint != baseline.fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_id", "0" * 64),
        ("input_hash", "0" * 64),
        ("as_of", datetime(2020, 1, 2, tzinfo=UTC)),
        ("version", "kronos-shadow-v2"),
    ],
)
def test_every_identity_field_contributes_to_the_fingerprint(field: str, value: object) -> None:
    """Each field the fingerprint claims to cover has to actually move it.

    Written because a mutation removing `input_hash` from the fingerprint left the whole suite
    green: for both adapters the artifact id already encodes the input, so the field's own
    contribution was never pinned. Redundancy in the happy path is not coverage -- a caller can
    supply a mislabelled artifact whose id and snapshot disagree, and the fingerprint must not
    collapse that onto the original.
    """
    baseline = kronos_provenance(forecast_record())

    assert baseline.model_copy(update={field: value}).fingerprint != baseline.fingerprint, (
        f"{field} does not contribute to the fingerprint"
    )


def test_search_cardinality_is_not_part_of_the_fingerprint() -> None:
    """How much was searched around an artifact is not what produced it.

    Cardinality is measured from persisted rows after the fact (#23). If it moved the
    fingerprint, counting more thoroughly would masquerade as a different inference.
    """
    record = forecast_record()

    assert (
        kronos_provenance(record, search_cardinality=23).fingerprint
        == kronos_provenance(record).fingerprint
    )


def test_an_unmeasured_search_burden_leaves_the_artifact_exploratory() -> None:
    """Absent is not zero. An uncounted burden must not read as a counted one of none."""
    assert kronos_provenance(forecast_record()).exploratory_only is True
    assert kronos_provenance(forecast_record(), search_cardinality=23).exploratory_only is False


def test_a_forecast_that_produced_no_inference_has_no_provenance_to_record() -> None:
    """Citing a failed forecast would name an inference that never happened."""
    record = forecast_record().model_copy(
        update={"status": ForecastStatus.FAILED, "worker_forecast": None, "features": None}
    )

    with pytest.raises(ValueError, match="produced no inference"):
        kronos_provenance(record)


def test_the_search_burden_is_counted_from_rows_not_taken_on_trust(tmp_path) -> None:
    """The #23 property, applied to the manifest linkage.

    A caller that swept several configurations and kept the best has as many rows as it swept.
    `measured_kronos_provenance` counts them; nothing lets the sweeping party name a smaller
    number. Here two configurations are run and both are found, from the ledger, without either
    being declared.
    """
    from quantbot.forecasting.database import ForecastDatabase  # noqa: PLC0415
    from quantbot.forecasting.ledger import ForecastLedger  # noqa: PLC0415
    from quantbot.research.provenance import measured_kronos_provenance  # noqa: PLC0415

    swept = [forecast_record(), forecast_record(config=KronosConfig(sample_count=2, seed=99))]
    database = ForecastDatabase(tmp_path / "forecast.db")
    try:
        with database.transaction() as session:
            ledger = ForecastLedger(session)
            for entry in swept:
                ledger.save_forecast(entry)

            worker = measured_kronos_provenance(session, swept[0])

        assert worker.search_cardinality == 2, "a swept configuration went uncounted"
        assert worker.exploratory_only is False
    finally:
        database.close()


def test_a_burden_cannot_be_taken_from_a_ledger_that_lacks_the_artifact(tmp_path) -> None:
    """Zero would be the most flattering possible answer to a question nobody could check."""
    from quantbot.forecasting.database import ForecastDatabase  # noqa: PLC0415
    from quantbot.research.provenance import measured_kronos_provenance  # noqa: PLC0415

    database = ForecastDatabase(tmp_path / "empty.db")
    try:
        with database.transaction() as session, pytest.raises(ValueError, match="not present"):
            measured_kronos_provenance(session, forecast_record())
    finally:
        database.close()


# --- semantic ---------------------------------------------------------------------------------

CUTOFF = date(2024, 6, 1)
ANALYSIS = SemanticAnalysis(
    confounders=["sector beta could produce the same excess return"],
    falsification_tests=["the effect should vanish after controlling for market beta"],
    disagreements=["bull: margin expansion is structural; bear: it is a one-off tax item"],
)


def source(source_id: str, *, content_hash: str = "c" * 64) -> Source:
    return Source(
        source_id=source_id,
        kind=SourceKind.NEWS,
        uri=f"https://example.invalid/{source_id}",
        title="Quarterly update",
        publisher="Example Wire",
        published_at=datetime(2025, 4, 1, tzinfo=UTC),
        retrieved_at=AS_OF,
        content_hash=content_hash,
        parser_version="v1",
    )


def response() -> ModelResponse:
    return ModelResponse(
        spec=ModelSpec(
            provider="openai-compatible",
            model="critic-model",
            version="1",
            endpoint="http://localhost:11434/v1",
            capabilities=ModelCapabilities(
                context_tokens=32768,
                structured_output=True,
                local=True,
                cost_class=CostClass.LOCAL,
            ),
        ),
        template=PromptTemplate(name="semantic-falsification", version="1", text="review {x}"),
        prompt="review this",
        text="{}",
        prompt_tokens=400,
        completion_tokens=120,
        produced_at=AS_OF,
    )


def semantic(sources: list[Source]) -> WorkerProvenance:
    assessment = assess_hindsight(
        sources, model="critic-model:1", cutoff=CUTOFF, cutoff_source="model card"
    )
    return semantic_provenance(ANALYSIS, assessment, response(), sources, as_of=AS_OF)


def test_a_semantic_claim_traces_to_its_source_bundle_and_configuration() -> None:
    worker = semantic([source("post-1"), source("post-2")])

    assert (
        worker.configuration["prompt_template_hash"] == response().provenance().prompt_template_hash
    )
    assert worker.configuration["preserved_disagreements"] == "1"
    assert worker.configuration["hindsight_cutoff"] == "2024-06-01"
    assert worker.input_hash


def test_rerunning_over_revised_sources_cannot_claim_equivalence() -> None:
    """The same hypothesis, prompt and model over a revised source is a different artifact.

    This is the criterion about historical semantic analysis: silent source revision would
    otherwise let a later run inherit the original's point-in-time standing.
    """
    original = semantic([source("post-1"), source("post-2")])

    revised = semantic([source("post-1"), source("post-2", content_hash="d" * 64)])
    added = semantic([source("post-1"), source("post-2"), source("post-3")])

    assert revised.input_hash != original.input_hash, "a revised source left the bundle unmoved"
    assert added.input_hash != original.input_hash, "an added source left the bundle unmoved"
    assert revised.fingerprint != original.fingerprint


def test_source_order_is_not_a_change() -> None:
    """Two bundles holding the same sources are the same bundle.

    A fingerprint that moved on ordering would report spurious differences, and a comparison
    that cries wolf is one people stop reading.
    """
    forward = semantic([source("post-1"), source("post-2")])
    backward = semantic([source("post-2"), source("post-1")])

    assert forward.fingerprint == backward.fingerprint


def test_a_contaminated_analysis_has_no_provenance_to_record() -> None:
    """Recording provenance for it would make it citable, which the refusal exists to prevent."""
    pre_cutoff = Source(
        source_id="pre-1",
        kind=SourceKind.NEWS,
        uri="https://example.invalid/pre-1",
        title="Old news",
        publisher="Example Wire",
        published_at=datetime(2019, 4, 1, tzinfo=UTC),
        retrieved_at=AS_OF,
        content_hash="e" * 64,
        parser_version="v1",
    )
    assessment = assess_hindsight(
        [pre_cutoff], model="critic-model:1", cutoff=CUTOFF, cutoff_source="model card"
    )

    with pytest.raises(ValueError, match="not evidence"):
        semantic_provenance(ANALYSIS, assessment, response(), [pre_cutoff], as_of=AS_OF)


# --- comparison -------------------------------------------------------------------------------


def manifest_with(*workers: WorkerProvenance) -> ExperimentManifest:
    """Reuses the reproducibility suite's builder rather than growing a second one."""
    from tests.unit.research.test_reproducibility import manifest  # noqa: PLC0415

    return manifest(workers=tuple(workers))


def test_comparison_explains_a_changed_checkpoint_between_two_runs() -> None:
    """Before this, two runs differing only in Kronos weights compared as the same experiment."""
    baseline = kronos_provenance(forecast_record())
    before = manifest_with(baseline)
    after = manifest_with(
        baseline.model_copy(
            update={"configuration": {**baseline.configuration, "model_revision": "0" * 40}}
        )
    )

    fields = {difference.field for difference in compare(before, after)}

    assert "workers[kronos].fingerprint" in fields
    assert "workers[kronos].model_revision" in fields


def test_comparison_reports_a_worker_that_only_one_run_used() -> None:
    fields = {
        difference.field
        for difference in compare(manifest_with(), manifest_with(semantic([source("post-1")])))
    }

    assert "workers[semantic]" in fields


def test_comparison_is_silent_when_the_same_worker_ran_the_same_way() -> None:
    """A comparison that reports differences between identical runs is noise."""
    identical = kronos_provenance(forecast_record())

    assert compare(manifest_with(identical), manifest_with(identical)) == []
