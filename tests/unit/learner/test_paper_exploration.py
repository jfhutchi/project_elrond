"""Synthetic exploration contracts are not forward broker evidence."""

from datetime import timedelta
from importlib import import_module

import pytest

from quantbot.learner.experiment_contract import DecisionRecord
from quantbot.learner.experiment_observation import build_observation_batch
from quantbot.research.manifest import content_id
from tests.fixtures.adaptive_paper import manifest, snapshot


def example():
    frozen, source = manifest(), snapshot()
    policy = import_module("quantbot.learner.exploration").ExplorationPolicy(
        experiment_id=frozen.experiment_id, manifest_hash=content_id(frozen),
        recorded_at=frozen.registered_at, code_hash="a" * 64, synthetic=True,
    )
    return frozen, source, policy


def test_negative_forecasts_do_not_prevent_round_robin_exploration():
    module = import_module("quantbot.learner.exploration")
    frozen, source, policy = example()
    batch = build_observation_batch(frozen, source, now=source.received_at)
    diagnostics = {row.symbol: {"a" * 64: -0.002, "b" * 64: -0.001}
                   for row in batch.features}
    decisions = tuple(DecisionRecord(experiment_id=frozen.experiment_id,
                      recorded_at=batch.recorded_at, model_id=model_id, features=row,
                      score=score, action="WAIT", reason="SYNTHETIC_BASELINE", synthetic=True)
                      for row in batch.features
                      for model_id, score in diagnostics[row.symbol].items())
    observations = module.exploration_observations(
        frozen, policy, batch, source=source, baseline_decisions=decisions,
        holdout_guard=lambda start, end: True,
    )
    assert len(observations) == 6
    assert all(row.baseline_scores == diagnostics[row.features.symbol] for row in observations)
    assert all(row.order_authority is False for row in observations)
    ordered = module.round_robin_observations(policy, observations, completed_lots=0)
    later = module.round_robin_observations(policy, observations, completed_lots=1)
    assert later[0].features.symbol == ordered[1].features.symbol
    assert module.round_robin_observations(policy, observations, completed_lots=6) == ordered


async def test_real_collector_per_instrument_shape_preserves_each_source():
    from quantbot.learner.experiment_coordinator import collect_observations

    module = import_module("quantbot.learner.exploration")
    frozen, source, policy = example()
    sources = {}

    async def provider(identity):
        symbols = (identity.symbol, "SPY")
        capture = source.model_copy(update={
            "query": source.query.model_copy(update={"symbols": symbols}),
            "inputs": tuple(row for row in source.inputs if row.bar.symbol in symbols),
        })
        sources[identity.symbol] = capture
        return capture

    batch = await collect_observations(frozen, provider=provider,
                                      clock=lambda: source.received_at, clock_health=lambda _: True)
    observations = module.exploration_observations(
        frozen, policy, batch, source=sources, baseline_decisions=(),
        holdout_guard=lambda start, end: True,
    )
    assert len(observations) == len(frozen.roster)
    assert all(row.features.source_snapshot_hash == sources[row.features.symbol].identity
               for row in observations)
    assert batch.snapshot_hash not in {capture.identity for capture in sources.values()}
    with pytest.raises(ValueError, match="COMPLETE_GENUINE"):
        module.exploration_observations(frozen, policy, batch,
            source={key: source for key in sources}, baseline_decisions=(),
            holdout_guard=lambda start, end: True)


@pytest.mark.parametrize("failure", ["protected", "missing", "source", "lookahead"])
def test_exploration_never_replaces_source_and_holdout_validation(failure):
    module = import_module("quantbot.learner.exploration")
    frozen, source, policy = example()
    batch = build_observation_batch(frozen, source, now=source.received_at)
    if failure == "missing":
        batch = batch.model_copy(update={"features": batch.features[:-1]})
    elif failure == "source":
        batch = batch.model_copy(update={"snapshot_hash": "f" * 64})
    elif failure == "lookahead":
        policy = policy.model_copy(update={"recorded_at": batch.recorded_at + timedelta(seconds=1)})
    with pytest.raises(ValueError):
        module.exploration_observations(
            frozen, policy, batch, source=source, baseline_decisions=(),
            holdout_guard=lambda start, end: failure != "protected",
        )


def test_holdout_refusal_precedes_panel_reconstruction(monkeypatch):
    module = import_module("quantbot.learner.exploration")
    frozen, source, policy = example()
    batch = build_observation_batch(frozen, source, now=source.received_at)
    monkeypatch.setattr(module, "build_observation_batch", lambda *args, **kwargs:
                        pytest.fail("protected prices must not be consumed"))
    with pytest.raises(ValueError, match="PROTECTED_OBSERVATION_WINDOW"):
        module.exploration_observations(frozen, policy, batch, source=source,
            baseline_decisions=(), holdout_guard=lambda start, end: False)


def test_exploration_risk_is_tighter_not_an_unlimited_paper_route():
    module = import_module("quantbot.learner.exploration")
    frozen, _, policy = example()
    assert policy.maximum_position_bps == 50
    assert policy.maximum_concurrent_positions == 1
    assert policy.maximum_entry_risk_bps == 25
    assert policy.stop_bps == 100 and policy.horizon_minutes == 30
    assert not policy.forecast_gate_required
    for field, value in (("mode", "LIVE"), ("maximum_position_bps", 51),
                         ("maximum_concurrent_positions", 2), ("stop_bps", 101)):
        with pytest.raises(ValueError):
            module.ExplorationPolicy.model_validate(policy.model_dump() | {field: value})


@pytest.mark.parametrize("filename", ["exploration_execution.py", "exploration_feedback.py"])
def test_exploration_hash_binds_execution_and_feedback(monkeypatch, filename):
    from pathlib import Path

    module = import_module("quantbot.learner.exploration_paper")
    original = module.exploration_code_hash()
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read_bytes(path) +
                        (b"\n# synthetic hash change" if path.name == filename else b""))
    assert module.exploration_code_hash() != original


def test_forecaster_hooks_keep_economic_gates_and_reject_exploration():
    from decimal import Decimal

    from quantbot.learner.paper_runner import IntradayPaperRunner, PaperExecutionPolicy
    from tests.unit.learner.test_experiment_paper import cohort

    frozen, candidate, decisions, sources = cohort()
    from quantbot.learner.experiment_paper import pooled_predictions
    prediction = pooled_predictions(frozen, candidate, decisions, sources=sources)[0]
    runner = object.__new__(IntradayPaperRunner)
    runner.policy = PaperExecutionPolicy()
    assert runner._economic_edge_objection(prediction, Decimal("0.001")) is None
    below = prediction.model_copy(update={"score": -0.002})
    assert runner._economic_edge_objection(below, Decimal("0.001")) == (
        "PAPER_SCORE_BELOW_COST_FLOOR"
    )


@pytest.fixture
async def exploration_context(tmp_path):
    from quantbot.learner.experiment_observation import build_observation_batch
    from tests.unit.learner.test_experiment_paper import _commissioned_pool, runtime_context
    module = import_module("quantbot.learner.exploration_paper")
    async for retained in runtime_context.__wrapped__(tmp_path):
        kwargs, broker, _, supervisor = await _commissioned_pool(retained)
        frozen = kwargs["manifest"]
        source = next(iter(kwargs["sources"].values()))
        batch = build_observation_batch(frozen, source, now=source.received_at)
        policy = import_module("quantbot.learner.exploration").ExplorationPolicy(
            experiment_id=frozen.experiment_id, manifest_hash=content_id(frozen),
            recorded_at=frozen.registered_at, code_hash=module.exploration_code_hash(),
            synthetic=True,
        )
        # Negative pretrade baselines remain diagnostics, never substituted high scores.
        decisions = tuple(row.model_copy(update={"score": -0.003, "action": "WAIT"})
                          for row in kwargs["decisions"])
        config = kwargs["config"].model_copy(update={"max_position_value_bps": 50})
        binding = module.exploration_paper_binding(
            frozen, policy, config=config, policy=kwargs["policy"],
            veto_policy=kwargs["veto_policy"], market_data=kwargs["market_data"],
            classification=kwargs["classification"],
        )
        identity = binding.strategy_identity(git_commit="e" * 40, deployed_at=policy.recorded_at)
        arguments = {key: value for key, value in kwargs.items()
                     if key not in {"candidate", "decisions", "sources"}}
        arguments.update(exploration=policy, observation=batch, source=source,
                         baseline_decisions=decisions, completed_lots=0,
                         config=config, binding=binding, identity=identity)
        yield arguments, broker, supervisor


async def test_exploration_without_exact_deployment_cannot_create_intent(exploration_context):
    module = import_module("quantbot.learner.exploration_paper")
    kwargs, broker, _ = exploration_context
    runner = module.ExplorationPaperRunner(**kwargs)
    runner.bind_run("exploration-test-uncommissioned")
    assert (await runner.evaluate()).submissions == ()
    assert any("DEPLOYMENT" in code or "PROMOTION" in code for code in runner.last_reasons)
    assert broker.submitted == []


@pytest.mark.parametrize("affordable", [False, True])
async def test_negative_baseline_can_produce_bounded_protected_paper_request(
    exploration_context, affordable,
):
    from decimal import Decimal

    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
    from quantbot.storage import StorageRepository
    from quantbot.storage.schema import promotion_state
    module = import_module("quantbot.learner.exploration_paper")
    kwargs, broker, _ = exploration_context
    if affordable:
        broker.account_snapshot = broker.account_snapshot.model_copy(update={
            "account": broker.account_snapshot.account.model_copy(update={
                "equity": Decimal("100000"), "cash": Decimal("100000"),
                "buying_power": Decimal("100000"),
            }),
        })
    runner = module.ExplorationPaperRunner(**kwargs)
    with kwargs["database"].transaction() as session:
        for row in session.execute(promotion_state.select()).mappings():
            if row["stage"] == "SHADOW":
                continue
            PromotionLedger(session).demote(row["strategy_id"], Stage.SHADOW,
                                            reason="isolated synthetic exploration test",
                                            actor="test", now=broker.now)
        StorageRepository(session).save_strategy_deployment(kwargs["identity"])
        PromotionLedger(session).enter(PromotionState(
            strategy_id=kwargs["identity"].strategy_id,
            strategy_version=kwargs["identity"].version,
            configuration_hash=kwargs["identity"].configuration_hash,
            stage=Stage.PAPER_OBSERVATION,
            reason="isolated synthetic exploration test", actor="test", updated_at=broker.now,
        ))
        StorageRepository(session).create_run("exploration-test-negative-baseline",
                                              kwargs["identity"], started_at=broker.now)
    runner.bind_run("exploration-test-negative-baseline")
    result = await runner.evaluate()
    if not affordable:
        assert result.submissions == ()
        assert "QUANTITY_BELOW_ONE_SHARE" in runner.last_reasons
        assert broker.submitted == []
        return
    assert len(result.submissions) == 1, runner.last_reasons
    request = result.submissions[0]
    assert request.risk_approved and request.vetoes.allowed
    assert request.intent.symbol in {row.symbol for row in kwargs["manifest"].roster}
    assert runner.observation_kind == "LEARNER_PAPER_EXPLORATION_V1"
    assert all(next(iter(row.baseline_scores.values())) < 0 for row in runner.predictions)
    assert broker.submitted == []  # Request generation is not broker execution.


def test_exploration_factory_rejects_live_before_opening_any_store():
    from types import SimpleNamespace

    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.learner.paper_runner import PaperExecutionNotConfigured

    module = import_module("quantbot.learner.exploration_paper")
    runtime = SimpleNamespace(settings=SimpleNamespace(
        TRADING_MODE=TradingMode.LIVE, BROKER_ENVIRONMENT=BrokerEnvironment.LIVE,
        LIVE_TRADING_ACKNOWLEDGED=False, EXPECTED_ACCOUNT_ID=None,
    ))
    with pytest.raises(PaperExecutionNotConfigured, match="PAPER"):
        module.build_exploration_paper_services(
            runtime, manifest=None, exploration=None, observation=None, source=None,
            baseline_decisions=(), completed_lots=0, policy=None, classification=None,
            clock=lambda: None, clock_health=lambda now: False,
            holdout_guard=lambda start, end: False,
        )


@pytest.mark.parametrize("field,value", [
    ("maximum_entry_risk_bps", 20), ("maximum_position_bps", 40), ("stop_bps", 50),
])
def test_exploration_cannot_exceed_tighter_frozen_manifest(exploration_context, field, value):
    module = import_module("quantbot.learner.exploration_paper")
    kwargs, _, _ = exploration_context
    frozen = kwargs["manifest"].model_copy(update={
        "risk": kwargs["manifest"].risk.model_copy(update={field: value}),
    })
    policy = kwargs["exploration"].model_copy(update={"manifest_hash": content_id(frozen)})
    with pytest.raises(ValueError, match="BOUNDED_EXPLORATION"):
        module.exploration_paper_binding(
            frozen, policy, config=kwargs["config"], policy=kwargs["policy"],
            veto_policy=kwargs["veto_policy"], market_data=kwargs["market_data"],
            classification=kwargs["classification"],
        )
