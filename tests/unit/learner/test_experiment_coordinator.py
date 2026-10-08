"""Synthetic clocks and receipts exercise restartable shadow coordination."""

from datetime import timedelta
from importlib import import_module

import pytest

from quantbot.learner.experiment_observation import build_observation_batch
from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import OPEN, manifest, snapshot


def module():
    return import_module("quantbot.learner.experiment_coordinator")


@pytest.fixture
def context(tmp_path):
    store = ExperimentStore(tmp_path / "workflow.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    return store, build_observation_batch(manifest(), snapshot(), now=snapshot().received_at)


async def test_observation_restart_and_closed_session_resolve_labels(context):
    store, observation = context
    first = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    assert first.observations_recorded == 6
    assert first.fits == first.intents == first.labels_recorded == 0
    later = OPEN + timedelta(hours=7)
    second = await module().coordinator_tick(
        ExperimentStore(store.path),
        manifest(),
        now=later,
        observation=build_observation_batch(manifest(), snapshot(65), now=later),
        label_snapshots=(snapshot(65),),
        execution=None,
        clock_verified=True,
    )
    assert second.labels_recorded == 6
    assert second.status == "NORMAL_WAIT"
    assert second.next_expected_event == "NEXT_EXCHANGE_SESSION"
    third = await module().coordinator_tick(
        store,
        manifest(),
        now=later + timedelta(minutes=2),
        observation=None,
        label_snapshots=(snapshot(65),),
        execution=None,
        clock_verified=True,
    )
    assert third.labels_recorded == 0


async def test_clock_unverified_never_credits_observations_or_trades(context):
    store, observation = context
    result = await module().coordinator_tick(
        store, manifest(), now=observation.recorded_at, observation=observation, execution=None
    )
    assert result.status == "CLOCK_SYNC_REQUIRED"
    assert result.observations_recorded == result.intents == 0


async def test_completed_labels_do_not_repeat_historical_resolution(context, monkeypatch):
    store, observation = context
    await module().coordinator_tick(store, manifest(), now=observation.recorded_at,
                                    observation=observation, execution=None, clock_verified=True)
    later = OPEN + timedelta(hours=7)
    result = await module().coordinator_tick(store, manifest(), now=later, observation=None,
                                            label_snapshots=(snapshot(65),), execution=None,
                                            clock_verified=True)
    assert result.labels_recorded == 6
    def duplicate_work(*args, **kwargs):
        raise AssertionError("already-completed labels must not be recomputed")
    monkeypatch.setattr(module(), "resolve_batch_labels", duplicate_work)
    result = await module().coordinator_tick(store, manifest(), now=later+timedelta(minutes=2),
                                            observation=None, label_snapshots=(snapshot(65),),
                                            execution=None, clock_verified=True)
    assert result.labels_recorded == 0
    # Skipping completed labels must never skip validation of newly supplied data.
    captured = snapshot(65)
    future_at = later + timedelta(hours=1)
    future = captured.model_copy(update={"received_at": future_at, "receipts": tuple(
        r.model_copy(update={"received_at": future_at}) for r in captured.receipts),
        "inputs": tuple(row.model_copy(update={"available_at": future_at})
                        for row in captured.inputs)})
    with pytest.raises(ValueError, match="FUTURE_SOURCE_RECEIPT"):
        await module().coordinator_tick(store, manifest(), now=later+timedelta(minutes=4),
                                       observation=None, label_snapshots=(future,), execution=None,
                                       clock_verified=True)


async def test_same_wakeup_receipt_replays_without_duplicate_evidence(context):
    store, observation = context
    first = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    second = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    assert first == second


async def test_concurrent_owner_cannot_advance_same_tick(context):
    store, observation = context
    store.acquire_lease(
        manifest().experiment_id,
        owner="other",
        now=observation.recorded_at,
        duration=timedelta(minutes=3),
    )
    result = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
        owner="new",
    )
    assert result.status == "LEASE_BUSY"
    assert result.observations_recorded == 0


async def test_collection_resamples_clock_after_provider_response(context):
    store, _ = context
    times = iter((OPEN + timedelta(minutes=35), OPEN + timedelta(minutes=35, seconds=121)))

    async def provider(identity):
        return snapshot()

    result = await module().collect_observations(
        manifest(), provider=provider, clock=lambda: next(times), clock_health=lambda _: True
    )
    assert result.features == ()
    assert all(row.status == "WAITING_BOUNDARY" for row in result.coverage)


async def test_missing_whole_symbol_does_not_discard_other_receipts(context):
    async def provider(identity):
        if identity.symbol == "SYN0":
            raise ValueError("synthetic whole-symbol missing")
        return snapshot()

    result = await module().collect_observations(
        manifest(),
        provider=provider,
        clock=lambda: snapshot().received_at,
        clock_health=lambda _: True,
    )
    assert len(result.features) == 5
    assert next(row for row in result.coverage if row.symbol == "SYN0").status == "MISSING"


async def test_collection_clock_failure_has_one_explicit_root_cause(context):
    called = []

    async def provider(identity):
        called.append(identity)
        return snapshot()

    with pytest.raises(ValueError, match="CLOCK_SYNC_REQUIRED"):
        await module().collect_observations(
            manifest(),
            provider=provider,
            clock=lambda: snapshot().received_at,
            clock_health=lambda _: False,
        )
    assert called == []


async def test_pooled_shadow_scores_eligible_members_without_granting_orders(context):
    from quantbot.learner.estimators import FittedEstimator
    from quantbot.research.manifest import content_id

    store, observation = context
    candidate = module().CandidateArtifact(
        experiment_id=manifest().experiment_id,
        recorded_at=OPEN - timedelta(seconds=1),
        manifest_hash=content_id(manifest()),
        trial_id="synthetic-charged-mean",
        training_source_hash="a" * 64,
        validation_source_hash="b" * 64,
        synthetic=True,
        artifact=FittedEstimator(
            family="mean",
            feature_names=manifest().feature_names,
            means=(0.0,) * 10,
            scales=(1.0,) * 10,
            coefficients=(0.0,) * 10,
            intercept=0.02,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="mean-v1",
        ),
    )
    store.append(candidate, idempotency_key="synthetic-candidate")
    result = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    assert result.decisions_recorded == 6
    assert result.intents == result.fits == 0
    decisions = [
        row.decode(module().DecisionRecord)
        for row in store.read_as_of(manifest().experiment_id, observation.recorded_at)
        if row.kind == "DecisionRecord"
    ]
    assert len(decisions) == 6
    assert all(row.score == 0.02 and row.synthetic and not row.paper_eligible for row in decisions)
    from quantbot.learner.experiment_news import NewsEvidence, ThesisRecord

    events = store.read_as_of(manifest().experiment_id, observation.recorded_at)
    theses = [event.decode(ThesisRecord) for event in events if event.kind == "ThesisRecord"]
    news = [event.decode(NewsEvidence) for event in events if event.kind == "NewsEvidence"]
    assert len(theses) == 6
    assert all(
        not row.order_authority and row.reason == "RESEARCH_COVERAGE_INCOMPLETE_SHADOW"
        for row in theses
    )
    assert len(news) == 6
    assert all(row.status == "UNAVAILABLE" for row in news)
    later = observation.recorded_at + timedelta(seconds=30)
    second = await module().coordinator_tick(
        store,
        manifest(),
        now=later,
        observation=build_observation_batch(manifest(), snapshot(), now=later),
        execution=None,
        clock_verified=True,
    )
    assert second.observations_recorded == second.decisions_recorded == 0


async def test_protected_boundary_is_calendar_wait_not_provider_failure(context):
    store, _ = context

    async def provider(identity):
        raise ValueError("PROTECTED_OBSERVATION_WINDOW")

    observation = await module().collect_observations(
        manifest(),
        provider=provider,
        clock=lambda: snapshot().received_at,
        clock_health=lambda _: True,
    )
    assert all(row.status == "PROTECTED_WINDOW" for row in observation.coverage)
    result = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    assert result.status == "NORMAL_WAIT"
    assert result.next_expected_event == "CANONICAL_WINDOW_ELIGIBILITY"


async def test_label_receipt_cannot_backdate_availability(context):
    store, observation = context
    await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    later = OPEN + timedelta(hours=7)
    await module().coordinator_tick(
        store,
        manifest(),
        now=later,
        observation=None,
        label_snapshots=(snapshot(65),),
        execution=None,
        clock_verified=True,
    )
    event = next(
        e
        for e in store.read_as_of(manifest().experiment_id, now=later)
        if e.kind == "ResolvedMarketLabel"
    )
    resolved = event.decode(module().ResolvedMarketLabel)
    with pytest.raises(ValueError, match="LABEL_AVAILABILITY"):
        module().ResolvedMarketLabel(
            **(
                resolved.model_dump()
                | {"recorded_at": resolved.label.available_at - timedelta(seconds=1)}
            )
        )


async def test_tick_reports_charged_worker_queue_without_fitting(context):
    from quantbot.learner.experiment_research import schedule_initial_fits
    from tests.unit.learner.test_experiment_research import Guard, inputs

    store, observation = context
    training, validation = inputs()
    queued = schedule_initial_fits(
        store,
        manifest(),
        training=training,
        validation=validation,
        now=manifest().registered_at,
        guard=Guard(),
    )
    result = await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    assert result.fit_request_ids == tuple(row.request_id for row in queued)
    assert result.fits == 0
    assert not any(
        e.kind == "FitStart"
        for e in store.read_as_of(manifest().experiment_id, observation.recorded_at)
    )


async def test_active_cycle_context_cannot_commission_unapproved_experiment(context):
    from types import SimpleNamespace

    store, observation = context
    active = module().ActiveCycleContext(
        runtime=SimpleNamespace(),
        data_sync=SimpleNamespace(),
        clock=lambda: observation.recorded_at,
    )
    with pytest.raises(ValueError, match="EXACT_ARTIFACT_BINDING_ACTIVATION_REQUIRED"):
        await module().coordinator_tick(
            store,
            manifest(),
            now=observation.recorded_at,
            observation=observation,
            execution=None,
            active_cycle=active,
            clock_verified=True,
        )
    assert not store.pending_intents(manifest().experiment_id)


async def test_overnight_target_label_uses_actual_retrieval_availability(context):
    store, observation = context
    await module().coordinator_tick(
        store,
        manifest(),
        now=observation.recorded_at,
        observation=observation,
        execution=None,
        clock_verified=True,
    )
    now = OPEN + timedelta(days=1)
    original = snapshot(65)
    target = original.model_copy(
        update={
            "received_at": now,
            "receipts": tuple(
                row.model_copy(update={"received_at": now}) for row in original.receipts
            ),
            "inputs": tuple(
                row.model_copy(update={"available_at": now}) for row in original.inputs
            ),
        }
    )
    result = await module().coordinator_tick(
        store,
        manifest(),
        now=now,
        observation=None,
        execution=None,
        clock_verified=True,
        label_snapshots=(target,),
    )
    assert result.labels_recorded == 6
    labels = [
        event.decode(module().ResolvedMarketLabel)
        for event in store.read_as_of(manifest().experiment_id, now)
        if event.kind == "ResolvedMarketLabel"
    ]
    assert all(row.label.available_at == now for row in labels)
    assert all(row.recorded_at == now for row in labels)


async def test_expired_feature_keeps_evidence_and_labels_without_fault_or_decision(context):
    from quantbot.learner.estimators import FittedEstimator
    from quantbot.research.manifest import content_id

    store, observation = context
    candidate = module().CandidateArtifact(
        experiment_id=manifest().experiment_id,
        recorded_at=OPEN - timedelta(seconds=1),
        manifest_hash=content_id(manifest()),
        trial_id="synthetic-expiry",
        training_source_hash="a" * 64,
        validation_source_hash="b" * 64,
        synthetic=True,
        artifact=FittedEstimator(
            family="mean",
            feature_names=manifest().feature_names,
            means=(0.0,) * 10,
            scales=(1.0,) * 10,
            coefficients=(0.0,) * 10,
            intercept=0.02,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="mean-v1",
        ),
    )
    store.append(candidate, idempotency_key="candidate")
    now = observation.features[0].feature_at + timedelta(seconds=121)
    result = await module().coordinator_tick(
        store, manifest(), now=now, observation=observation, execution=None, clock_verified=True
    )
    assert result.observations_recorded == 6
    assert result.decisions_recorded == result.intents == 0
    assert result.status == "NORMAL_WAIT"
    assert result.reason == "CURRENT_FEATURE_WINDOW_EXPIRED"
    assert result.next_expected_event == "NEXT_FIVE_MINUTE_BOUNDARY"
