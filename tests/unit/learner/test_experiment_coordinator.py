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
