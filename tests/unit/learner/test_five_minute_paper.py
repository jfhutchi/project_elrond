"""Synthetic PAPER adapter integration. No network, real fit, or runtime activation."""

import importlib
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.brokers.base import BrokerHealth
from quantbot.domain import Bar, ReconciliationStatus
from quantbot.execution import ExecutionCoordinator
from quantbot.learner.five_minute import CONTRACT, FEATURE_NAMES, build_features
from quantbot.learner.five_minute_forward import forecast
from quantbot.learner.forward_source import collect_snapshot
from quantbot.learner.inherited import InheritedBaseline
from quantbot.learner.paper_deadlines import (
    PaperDeadline,
    PaperDeadlineSupervisor,
    deadline_entry_objections,
)
from quantbot.learner.paper_runner import PaperExecutionPolicy
from quantbot.market_data import MarketDataCache
from quantbot.market_data.base import TransportResponse
from quantbot.operations.runners import MarketDataSettings
from quantbot.research.manifest import content_id
from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
from quantbot.risk.policy_versions import active_policy_version
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import position_protection_cycles
from quantbot.strategy import StrategyConfig, XNYSSession
from tests.integration.test_execution_coordinator import _settings
from tests.integration.test_runtime_wiring import _config_payload
from tests.unit.learner.test_five_minute import OPEN, bars
from tests.unit.learner.test_forward_source import credentials
from tests.unit.learner.test_paper_deadlines import Broker


def adapter():
    try:
        return importlib.import_module("quantbot.learner.five_minute_paper")
    except ModuleNotFoundError:
        pytest.fail("five-minute PAPER adapter is not implemented")


@pytest.fixture
async def context(tmp_path):
    module = adapter()
    now = OPEN + timedelta(minutes=35, seconds=1)
    database = Database.create(tmp_path / "isolated-paper.db")

    class Transport:
        async def request(self, method, url, *, headers, params, timeout_seconds):
            if url.endswith("/calendar"):
                payload = [
                    dict(
                        date=(OPEN + timedelta(days=i)).date().isoformat(),
                        open="09:30",
                        close="16:00",
                    )
                    for i in range(2)
                ]
            else:
                payload = {
                    "bars": {
                        s: [
                            dict(
                                t=b.timestamp.isoformat(),
                                o=float(b.open),
                                h=float(b.high),
                                l=float(b.low),
                                c=float(b.close),
                                v=float(b.volume),
                            )
                            for b in bars(7)
                            if b.symbol == s
                        ]
                        for s in ("DIA", "SPY")
                    },
                    "next_page_token": None,
                }
            return TransportResponse(
                status_code=200, headers={}, content=json.dumps(payload).encode()
            )

    snapshot = await collect_snapshot(
        tmp_path / "synthetic-capture",
        ("DIA",),
        OPEN.date(),
        OPEN.date(),
        credentials(),
        transport=Transport(),
        timeframe="5Min",
        clock=lambda: now,
    )
    rows = build_features(
        tuple(i.bar for i in snapshot.inputs),
        snapshot.sessions,
        decision_at=now,
        symbols=("DIA",),
        source_snapshot_hash=snapshot.identity,
        receipt_at=snapshot.received_at,
    )
    # Hand-authored synthetic mean artifact: no training/search call is made here.
    model = dict(
        contract=CONTRACT,
        feature_names=list(FEATURE_NAMES),
        training_symbols=["DIA"],
        created_at=(OPEN - timedelta(days=1)).isoformat(),
        research_only_not_runtime_compatible=True,
        artifact=dict(
            family="mean",
            feature_names=list(FEATURE_NAMES),
            means=[0.0] * 10,
            scales=[1.0] * 10,
            coefficients=[0.0] * 10,
            intercept=0.02,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="mean-v1",
        ),
    )
    predictions = forecast(model, content_id(model), rows, recorded_at=now)
    config = StrategyConfig.model_validate(
        _config_payload()
        | dict(
            universe=["DIA", "SPY"],
            roster_size=1,
            risk_per_trade_bps=25,
            max_position_value_bps=500,
        )
    )
    policy = PaperExecutionPolicy(
        exclusive_learner_entry=True,
        inherited=InheritedBaseline(
            account_id_hash=PaperDeadline.account_hash("paper-1"), captured_at=OPEN, positions=()
        ),
    )
    market_data = MarketDataSettings()
    with database.transaction() as session:
        version, _ = active_policy_version(StorageRepository(session), now=now)
    classification = module.dia_exposure_classification()
    binding = module.five_minute_paper_binding(
        model,
        config=config,
        policy=policy,
        veto_policy=version.policy(),
        market_data=market_data,
        classification=classification,
    )
    identity = binding.strategy_identity(git_commit="e" * 40, deployed_at=now)
    sessions = tuple(
        XNYSSession(
            session_date=(OPEN - timedelta(days=i)).date(),
            open_at=OPEN - timedelta(days=i),
            close_at=OPEN - timedelta(days=i) + timedelta(minutes=390),
        )
        for i in range(130, -1, -1)
    )

    class Calendar:
        async def sessions(self):
            return sessions

    broker = Broker(database)
    broker.environment = "PAPER"
    broker.provider = "synthetic"
    broker.now = now

    async def health():
        return BrokerHealth(
            healthy=True,
            provider="synthetic",
            environment="PAPER",
            account_id="paper-1",
            reasons=(),
        )

    broker.health_check = health
    with database.transaction() as session:
        repo = StorageRepository(session)
        repo.save_strategy_deployment(identity)
        PromotionLedger(session).enter(
            PromotionState(
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                stage=Stage.PAPER_OBSERVATION,
                reason="synthetic adapter fixture",
                actor="test",
                updated_at=now,
            )
        )
        repo.create_run("entry-run", identity, started_at=now)
        repo.set_kill_switch(False, reason="synthetic fixture", updated_at=now)
        repo.save_reconciliation(
            "synthetic-recon",
            status=ReconciliationStatus.RECONCILED,
            started_at=now,
            completed_at=now,
            diffs=(),
        )
        session.execute(
            position_protection_cycles.insert().values(
                cycle_id="synthetic-protection",
                observed_at=now.isoformat(),
                completed_at=now.isoformat(),
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                account_id="paper-1",
                policy_version="synthetic",
                policy_hash="a" * 64,
                outcome="HEALTHY",
                reason="",
                position_count=0,
                evaluated_count=0,
                unprotected_count=0,
                exit_required_count=0,
                detail="{}",
            )
        )
        repo.save_account_snapshot("opening", broker.account_snapshot.account, (), captured_at=OPEN)
        source = MarketDataCache.provider_key(
            market_data.provider, market_data.feed, market_data.adjustment, market_data.timeframe
        )
        for index, day in enumerate(sessions[:-1]):
            price = Decimal("100") + Decimal(index) / 10
            repo.save_bars(
                [
                    Bar(
                        symbol=s,
                        timestamp=day.close_at,
                        open=price,
                        high=price,
                        low=price,
                        close=price,
                        volume=Decimal("10000000"),
                        adjustment=Decimal(1),
                    )
                    for s in ("DIA", "SPY")
                ],
                provider=source,
            )
    supervisor = PaperDeadlineSupervisor(
        database=database,
        broker=broker,
        coordinator=ExecutionCoordinator(database, broker, _settings()),
        identity=identity,
        expected_account_id="paper-1",
        lock_path=database.path.with_suffix(".lock"),
        clock=lambda: broker.now,
    )
    runner = module.FiveMinutePaperRunner(
        database=database,
        broker=broker,
        sessions=Calendar(),
        model=model,
        predictions=predictions,
        snapshot=snapshot,
        config=config,
        identity=identity,
        binding=binding,
        policy=policy,
        veto_policy=version.policy(),
        market_data=market_data,
        classification=classification,
        expected_account_id="paper-1",
        clock=lambda: broker.now,
        clock_health=lambda _: True,
        holdout_guard=lambda *_: True,
    )
    runner.bind_run("entry-run")
    yield database, broker, runner, supervisor
    database.close()


async def test_five_minute_forecast_gets_measured_risk_and_durable_deadline_but_no_submission(
    context,
):
    database, broker, runner, supervisor = context
    await supervisor.run_once()
    result = await runner.evaluate()
    assert len(result.submissions) == 1, runner.last_reasons
    request = result.submissions[0]
    assert request.risk_approved and request.vetoes.allowed
    assert len(request.vetoes.evaluated) == 6
    assert request.intent.symbol == "DIA"
    assert (
        request.intent.quantity * Decimal(str(runner.predictions[0].features.reference_close))
        <= 500
    )
    assert not broker.submitted
    assert (
        deadline_entry_objections(database, request.intent, account_id="paper-1", now=broker.now)
        == ()
    )
    with database.transaction() as session:
        rows = StorageRepository(session).list_signals()
        handoff = next(r for r in rows if r.payload.get("kind") == "LEARNER_PAPER_DEADLINE_V1")
        assert handoff.payload["document"]["deadline_at"] == runner.predictions[
            0
        ].features.target_at.isoformat().replace("+00:00", "Z")
    assert (await runner.evaluate()).submissions == ()
    assert "PAPER_PREDICTION_ALREADY_HANDED_OFF" in runner.last_reasons
    assert (await runner.evaluate_exits()).submissions == ()


@pytest.mark.parametrize(
    "case,reason",
    [
        ("closed", "PAPER_BROKER_CLOCK_UNAVAILABLE"),
        ("stale", "PAPER_PREDICTION_STALE"),
        ("restricted", "PAPER_ACCOUNT_RESTRICTED"),
        ("clock", "PAPER_VERIFIED_CLOCK_UNAVAILABLE"),
        ("holdout", "PAPER_PROTECTED_OBSERVATION_WINDOW"),
    ],
)
async def test_fail_closed_without_order_handoff(context, case, reason):
    database, broker, runner, supervisor = context
    await supervisor.run_once()
    if case == "closed":
        broker.open = False
    elif case == "stale":
        broker.now += timedelta(seconds=121)
    elif case == "restricted":
        broker.account_snapshot = broker.account_snapshot.model_copy(
            update={"order_entry_allowed": False}
        )
    elif case == "clock":
        runner.clock_health = lambda _: False
    elif case == "holdout":
        runner.holdout_guard = lambda *_: False
    result = await runner.evaluate()
    assert not result.submissions and reason in runner.last_reasons
    assert not broker.submitted
    with database.transaction() as session:
        assert not any(
            r.payload.get("kind") == "LEARNER_PAPER_DEADLINE_V1"
            for r in StorageRepository(session).list_signals()
        )


def constructor_arguments(context):
    database, broker, runner, _ = context
    return dict(
        database=database,
        broker=broker,
        sessions=runner._sessions,
        model=runner._five_document,
        predictions=runner.predictions,
        snapshot=runner._five_capture,
        config=runner._config,
        identity=runner._identity,
        binding=runner.binding,
        policy=runner.policy,
        veto_policy=runner._veto_policy,
        market_data=runner._market_data,
        classification=runner._exposure,
        expected_account_id=runner.account_id,
        clock=runner._clock,
        clock_health=runner.clock_health,
        holdout_guard=runner.holdout_guard,
    )


@pytest.mark.parametrize(
    "change",
    ["one-minute", "live", "score", "missing-baseline", "wider-stop", "risk", "value", "binding"],
)
async def test_constructor_rejects_contract_or_authority_mismatch(context, change):
    args = constructor_arguments(context)
    if change == "one-minute":
        args["model"] = {**args["model"], "contract": "minute-alpha-v1"}
    elif change == "live":
        args["broker"].environment = "LIVE"
    elif change == "score":
        args["predictions"] = (args["predictions"][0].model_copy(update={"score": 0.03}),)
    elif change == "missing-baseline":
        args["policy"] = args["policy"].model_copy(update={"inherited": None})
    elif change == "wider-stop":
        args["policy"] = args["policy"].model_copy(update={"stop_loss_bps": 200})
    elif change == "risk":
        args["config"] = args["config"].model_copy(update={"risk_per_trade_bps": 26})
    elif change == "value":
        args["config"] = args["config"].model_copy(update={"max_position_value_bps": 501})
    elif change == "binding":
        args["binding"] = args["binding"].model_copy(update={"model_id": "wrong"})
    with pytest.raises(ValueError):
        adapter().FiveMinutePaperRunner(**args)


@pytest.mark.parametrize(
    "change",
    [
        "health-account",
        "health-env",
        "health-failure",
        "after-response-clock",
        "protection",
        "recon",
        "kill",
        "protection-count",
    ],
)
async def test_readiness_and_protection_fail_closed(context, change):
    database, broker, runner, supervisor = context
    await supervisor.run_once()
    if change.startswith("health"):

        async def unhealthy():
            return BrokerHealth(
                healthy=change != "health-failure",
                provider="synthetic",
                environment="LIVE" if change == "health-env" else "PAPER",
                account_id="wrong" if change == "health-account" else "paper-1",
                reasons=(),
            )

        broker.health_check = unhealthy
    elif change == "after-response-clock":
        original = broker.get_account

        async def delayed():
            result = await original()
            broker.now += timedelta(seconds=121)
            return result

        broker.get_account = delayed
    else:
        from sqlalchemy import text

        with database.transaction() as session:
            if change == "protection":
                session.execute(
                    text(
                        "UPDATE position_protection_cycles SET outcome='DEGRADED',"
                        "reason='synthetic failure',unprotected_count=1,"
                        "evaluated_count=1,position_count=1"
                    )
                )
            elif change == "recon":
                session.execute(
                    text("UPDATE reconciliation_runs SET status='RECONCILIATION_REQUIRED'")
                )
            elif change == "protection-count":
                session.execute(
                    text("UPDATE position_protection_cycles SET position_count=1,evaluated_count=1")
                )
            elif change == "kill":
                StorageRepository(session).set_kill_switch(
                    True, reason="synthetic", updated_at=broker.now
                )
    result = await runner.evaluate()
    assert not result.submissions and runner.last_reasons and not broker.submitted
    with database.transaction() as session:
        assert not any(
            r.payload.get("kind") == "LEARNER_PAPER_DEADLINE_V1"
            for r in StorageRepository(session).list_signals()
        )


async def test_inherited_holdings_are_audited_without_granting_exit_ownership(context):
    from quantbot.domain import Position

    database, broker, runner, supervisor = context
    broker.positions = (
        Position(
            symbol="IWM",
            quantity=Decimal("1"),
            market_price=Decimal("100"),
            market_value=Decimal("100"),
            average_entry_price=Decimal("100"),
        ),
    )
    inherited = InheritedBaseline(
        account_id_hash=PaperDeadline.account_hash("paper-1"),
        captured_at=OPEN,
        positions=(dict(symbol="IWM", quantity="1", average_entry_price="100"),),
    )
    args = constructor_arguments(context)
    args["policy"] = runner.policy.model_copy(update={"inherited": inherited})
    args["binding"] = adapter().five_minute_paper_binding(
        args["model"],
        config=args["config"],
        policy=args["policy"],
        veto_policy=args["veto_policy"],
        market_data=args["market_data"],
        classification=args["classification"],
    )
    args["identity"] = args["binding"].strategy_identity(
        git_commit="e" * 40, deployed_at=broker.now
    )
    with database.transaction() as session:
        repo = StorageRepository(session)
        PromotionLedger(session).demote(
            runner._identity.strategy_id,
            Stage.SHADOW,
            reason="superseded synthetic fixture",
            actor="test",
            now=broker.now,
        )
        repo.save_strategy_deployment(args["identity"])
        PromotionLedger(session).enter(
            PromotionState(
                strategy_id=args["identity"].strategy_id,
                strategy_version=args["identity"].version,
                configuration_hash=args["identity"].configuration_hash,
                stage=Stage.PAPER_OBSERVATION,
                reason="synthetic fixture",
                actor="test",
                updated_at=broker.now,
            )
        )
        repo.create_run("inherited-run", args["identity"], started_at=broker.now)
    new = adapter().FiveMinutePaperRunner(**args)
    new.bind_run("inherited-run")
    await supervisor.run_once()
    assert (await new.evaluate()).submissions == ()
    assert "PAPER_DAILY_RISK_HISTORY_UNAVAILABLE" in new.last_reasons
    with database.transaction() as session:
        baseline = next(
            r
            for r in StorageRepository(session).list_signals()
            if r.payload.get("kind") == "LEARNER_INHERITED_BASELINE_V1"
        )
        assert baseline.payload["entry_authority"] is baseline.payload["exit_authority"] is False
    assert (await new.evaluate_exits()).submissions == () and not broker.submitted


def runtime_fixture(context, tmp_path):
    from types import SimpleNamespace

    from quantbot.operations.metrics import OperationalMetrics

    database, broker, runner, _ = context
    return SimpleNamespace(
        database=database,
        broker=broker,
        config=runner._config,
        settings=_settings(),
        identity=runner._identity,
        running_git_commit=runner._identity.git_commit,
        market_data_config=runner._market_data,
        sessions=runner._sessions,
        coordinator=ExecutionCoordinator(database, broker, _settings()),
        market_data=object(),
        recovery=object(),
        metrics=OperationalMetrics(),
        paths=SimpleNamespace(lock=tmp_path / "factory.lock"),
        runtime_context=lambda: None,
    )


async def test_factory_composes_existing_gates_without_creating_deployments(context, tmp_path):
    database, broker, runner, _ = context
    runtime = runtime_fixture(context, tmp_path)
    args = dict(
        model=runner._five_document,
        predictions=runner.predictions,
        snapshot=runner._five_capture,
        policy=runner.policy,
        classification=runner._exposure,
        clock=runner._clock,
        clock_health=runner.clock_health,
        holdout_guard=runner.holdout_guard,
    )
    with database.transaction() as session:
        before = len(StorageRepository(session).list_signals())
    services = adapter().build_five_minute_paper_services(runtime, **args)
    assert services.runner.binding == runner.binding
    assert services.deadlines.account_id == "paper-1"
    assert services.runner._entry_freshness().expected_interval_seconds == 300
    with database.transaction() as session:
        assert len(StorageRepository(session).list_signals()) == before
    assert not broker.submitted and not (tmp_path / "factory.lock").exists()


async def test_factory_cannot_create_live_authority_or_borrow_old_model_deployment(
    context, tmp_path
):
    from quantbot.learner.paper_runner import PaperExecutionNotConfigured

    _, _, runner, _ = context
    runtime = runtime_fixture(context, tmp_path)
    args = dict(
        model=runner._five_document,
        predictions=runner.predictions,
        snapshot=runner._five_capture,
        policy=runner.policy,
        classification=runner._exposure,
        clock=runner._clock,
        clock_health=runner.clock_health,
        holdout_guard=runner.holdout_guard,
    )
    runtime.settings = runtime.settings.model_copy(update={"LIVE_TRADING_ACKNOWLEDGED": True})
    with pytest.raises(PaperExecutionNotConfigured):
        adapter().build_five_minute_paper_services(runtime, **args)
    runtime.settings = _settings()
    args["policy"] = runner.policy.model_copy(update={"round_trip_cost_bps": 11})
    with pytest.raises(PaperExecutionNotConfigured, match="deployment missing"):
        adapter().build_five_minute_paper_services(runtime, **args)


async def test_mock_cycle_and_independent_deadline_exit_are_same_broker_neutral_flow(
    context, tmp_path
):
    from types import SimpleNamespace

    from quantbot.domain import Fill, OrderSide, Position
    from quantbot.learner.paper import build_learner_paper_cycle
    from quantbot.learner.paper_deadlines import CLOSED_EVENT, EXIT_EVENT
    from quantbot.learner.paper_runner import IntradayCohortSync
    from quantbot.operations.cycle import DataSyncResult

    database, broker, runner, supervisor = context
    runtime = runtime_fixture(context, tmp_path)

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=True)

    class RiskSync:
        async def sync(self):
            return DataSyncResult(
                healthy=True,
                completed_through=OPEN - timedelta(days=1),
                bars_stored=0,
                stale_symbols=(),
                reasons=(),
            )

    runtime.recovery = Recovery()
    cycle = build_learner_paper_cycle(
        runtime,
        runner,
        binding=runner.binding,
        identity=runner._identity,
        data_sync=IntradayCohortSync(RiskSync(), runner),
        max_data_age_seconds=120,
        clock=runner._clock,
    )
    await supervisor.run_once()
    runner.bind_run("mock-cycle")
    result = await cycle.run("mock-cycle", started_at=broker.now, trading_date=broker.now.date())
    assert result.orders_submitted == 1 and len(broker.submitted) == 1
    entry = broker.submitted[0]
    price = Decimal(str(runner.predictions[0].features.reference_close))
    order = broker.orders[entry.client_order_id].model_copy(
        update={
            "filled_quantity": entry.quantity,
            "filled_average_price": price,
            "status": "filled",
        }
    )
    broker.orders[entry.client_order_id] = order
    broker.fills = [
        Fill(
            fill_id="synthetic-entry-fill",
            broker_order_id=order.broker_order_id,
            symbol="DIA",
            side=OrderSide.BUY,
            quantity=entry.quantity,
            price=price,
            occurred_at=broker.now,
        )
    ]
    broker.positions = (
        Position(
            symbol="DIA",
            quantity=entry.quantity,
            market_price=price,
            market_value=entry.quantity * price,
            average_entry_price=price,
        ),
    )
    broker.now = runner.predictions[0].features.target_at
    with database.transaction() as session:
        PromotionLedger(session).demote(
            runner._identity.strategy_id,
            Stage.SHADOW,
            actor="test",
            reason="synthetic entry disable",
            now=broker.now,
        )
    recovered = await supervisor.run_once()
    assert recovered.exits_submitted == 1 and len(broker.submitted) == 2
    assert broker.submitted[-1].side is OrderSide.SELL
    with database.transaction() as session:
        assert any(
            e.event_type == EXIT_EVENT for e in StorageRepository(session).list_order_events()
        )
    exit_intent = broker.submitted[-1]
    exit_order = broker.orders[exit_intent.client_order_id].model_copy(
        update={
            "filled_quantity": exit_intent.quantity,
            "filled_average_price": price + 1,
            "status": "filled",
        }
    )
    broker.orders[exit_intent.client_order_id] = exit_order
    broker.fills.append(
        Fill(
            fill_id="synthetic-exit-fill",
            broker_order_id=exit_order.broker_order_id,
            symbol="DIA",
            side=OrderSide.SELL,
            quantity=exit_intent.quantity,
            price=price + 1,
            occurred_at=broker.now,
        )
    )
    broker.positions = ()
    broker.now += timedelta(seconds=1)
    closed = await supervisor.run_once()
    assert closed.healthy and len(broker.submitted) == 2
    with database.transaction() as session:
        repo = StorageRepository(session)
        assert any(e.event_type == CLOSED_EVENT for e in repo.list_order_events())
        assert len(repo.list_fills()) == 2


async def test_frozen_observation_loader_is_readonly_and_cannot_select_new_model(
    context, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from quantbot.learner.forward import _save

    module = adapter()
    _, broker, runner, _ = context
    cfg = SimpleNamespace(
        root=tmp_path / "existing-observer",
        mean_model=runner.model.model_id,
        ridge_model="f" * 64,
        clock_receipt=tmp_path / "clock.json",
    )
    path = cfg.root / "predictions" / "2026-10-01" / "1405.json"
    _save(path, runner.predictions[0].model_dump(mode="json"))
    market = cfg.root / "market" / "snapshots" / (runner._five_capture.identity + ".json")
    market.parent.mkdir(parents=True)
    original = tmp_path / "synthetic-capture" / "market" / "snapshots" / market.name
    market.write_bytes(original.read_bytes())
    before = {p: p.read_bytes() for p in cfg.root.rglob("*.json")}
    if not hasattr(module, "load_five_minute_cohort"):
        pytest.fail("frozen five-minute loader missing")
    monkeypatch.setattr(module, "bound_models", lambda _: [runner._five_document])
    monkeypatch.setattr(module, "clock_healthy", lambda *_: True)
    loaded = module.load_five_minute_cohort(cfg, runner.model.model_id, now=broker.now)
    assert loaded.predictions == runner.predictions and loaded.snapshot == runner._five_capture
    assert (
        module.load_five_minute_cohort(
            cfg, runner.model.model_id, now=broker.now + timedelta(seconds=121)
        )
        is None
    )
    with pytest.raises(ValueError):
        module.load_five_minute_cohort(cfg, "a" * 64, now=broker.now)
    assert before == {p: p.read_bytes() for p in cfg.root.rglob("*.json")}
