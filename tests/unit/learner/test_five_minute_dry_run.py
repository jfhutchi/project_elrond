"""Synthetic eligibility checks, never broker submission or forward evidence."""

from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.learner.estimators import fit_estimator
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.learner.five_minute_dry_run import DryRunContext, evaluate
from quantbot.learner.five_minute_forward import forecast
from quantbot.learner.paper import PaperEligibility
from quantbot.learner.paper_runner import PaperExecutionPolicy
from quantbot.research.manifest import content_id
from quantbot.risk import EntrySizingRequest, calculate_drawdown
from quantbot.risk.vetoes import PortfolioVetoInputs, PortfolioVetoPolicy, VetoName
from tests.unit.learner.test_five_minute import features
from tests.unit.learner.test_five_minute_forward import model
from tests.unit.risk.test_sizing import portfolio, strategy_config


def inputs():
    m = model()
    rows = features()
    (p,) = forecast(m, content_id(m), rows, recorded_at=rows[0].decision_at)
    config = strategy_config(
        risk_per_trade_bps=25, max_position_value_bps=500, universe=("DIA", "SPY"), roster_size=1
    )
    request = EntrySizingRequest(
        symbol="DIA",
        reference_price=Decimal(str(p.features.reference_close)),
        stop_distance=Decimal(str(p.features.reference_close)) / 100,
        portfolio=portfolio(target_symbol="DIA"),
        drawdown=calculate_drawdown(Decimal("100000"), Decimal("100000"), config),
    )
    return m, p, config, request


def test_frozen_cost_gate_and_unmeasured_controls_never_become_orders():
    m, p, config, request = inputs()
    result = evaluate(
        m,
        p,
        now=p.recorded_at,
        config=config,
        policy=PaperExecutionPolicy(),
        context=DryRunContext(sizing=request),
    )
    assert "PAPER_SCORE_BELOW_COST_FLOOR" in result.reasons
    assert "PAPER_BROKER_STATE_UNVERIFIED" in result.reasons
    assert "PAPER_VETO_INPUTS_UNAVAILABLE" in result.reasons
    assert result.orders == 0 and result.order_authority is False
    assert result.exit_authority is False and result.intent is None
    assert p.score == 0 and p.fixed_cost_floor == 0.001


@pytest.mark.parametrize("seconds", [-1, 121, 1800])
def test_freshness_never_expands_to_five_minute_feed_interval(seconds):
    m, p, config, request = inputs()
    result = evaluate(
        m,
        p,
        now=p.features.feature_at + timedelta(seconds=seconds),
        config=config,
        policy=PaperExecutionPolicy(),
        context=DryRunContext(sizing=request),
    )
    assert "PAPER_REFERENCE_STALE_OR_FUTURE" in result.reasons


@pytest.mark.parametrize("mutation", ["score", "contract", "model"])
def test_cannot_relabel_one_minute_or_change_frozen_prediction(mutation):
    m, p, config, request = inputs()
    if mutation == "score":
        p = p.model_copy(update={"score": 0.02, "cost_status": "ABOVE_FIXED_COST_FLOOR"})
    elif mutation == "contract":
        m["contract"] = "minute-alpha-v1"
    else:
        m["created_at"] = p.recorded_at.isoformat()
    result = evaluate(
        m,
        p,
        now=p.recorded_at,
        config=config,
        policy=PaperExecutionPolicy(),
        context=DryRunContext(sizing=request),
    )
    assert "FIVE_MINUTE_FROZEN_CONTRACT_MISMATCH" in result.reasons
    assert result.intent is None


def test_duplicate_key_does_not_depend_on_review_time_and_no_legacy_exit_ownership():
    m, p, config, request = inputs()
    first = evaluate(
        m,
        p,
        now=p.recorded_at,
        config=config,
        policy=PaperExecutionPolicy(),
        context=DryRunContext(sizing=request),
    )
    again = evaluate(
        m,
        p,
        now=p.recorded_at + timedelta(seconds=1),
        config=config,
        policy=PaperExecutionPolicy(),
        context=DryRunContext(sizing=request, seen_intents=(first.intent_id,)),
    )
    assert first.intent_id == again.intent_id
    assert "PAPER_DUPLICATE_DRY_RUN_INTENT" in again.reasons
    assert again.exit_authority is False


def test_missing_stops_and_existing_aggregate_cap_are_not_grandfathered_away():
    m, p, config, request = inputs()
    request = request.model_copy(
        update={
            "portfolio": request.portfolio.model_copy(
                update={
                    "open_risk_available": False,
                    "reasons": ("OPEN_RISK_UNAVAILABLE",),
                    "committed_position_count": 9,
                }
            )
        }
    )
    result = evaluate(
        m,
        p,
        now=p.recorded_at,
        config=config,
        policy=PaperExecutionPolicy(),
        context=DryRunContext(
            sizing=request, eligibility=PaperEligibility(reasons=("PAPER_DEPLOYMENT_MISSING",))
        ),
    )
    assert "OPEN_RISK_UNAVAILABLE" in result.reasons
    assert "PAPER_DEPLOYMENT_MISSING" in result.reasons
    assert result.intent is None


def test_synthetic_hypothetical_intent_is_not_a_submittable_order():
    # Positive score is synthetic only; no real frozen model is refitted.
    m, _, config, request = inputs()
    m["artifact"] = fit_estimator(
        ((0.0,) * 10, (1.0,) * 10), (0.02, 0.02), FEATURE_NAMES, family="mean", alpha=1.0, seed=0
    ).model_dump(mode="json")
    (p,) = forecast(m, content_id(m), features(), recorded_at=features()[0].decision_at)
    from quantbot.risk import size_entry

    sized = size_entry(request, config)
    veto = PortfolioVetoInputs(
        symbol="DIA",
        equity=request.portfolio.equity,
        committed_net_exposure=Decimal("0"),
        order_net_exposure=sized.quantity * request.reference_price,
        order_quantity=sized.quantity,
        session_date=p.recorded_at.date(),
        observed_at=p.recorded_at,
    )
    # Single-rule unit fixture exercises the intent branch, not production acceptance.
    context = DryRunContext(
        source_features=p.features,
        new_position_count=0,
        sizing=request,
        eligibility=PaperEligibility(reasons=()),
        operational_reasons=(),
        broker_verified_at=p.recorded_at,
        session_exit_at=p.recorded_at + timedelta(hours=1),
        veto_inputs=veto,
        veto_policy=PortfolioVetoPolicy(enabled=frozenset({VetoName.NET_EXPOSURE})),
    )
    result = evaluate(
        m, p, now=p.recorded_at, config=config, policy=PaperExecutionPolicy(), context=context
    )
    assert result.reasons == () and result.intent is not None
    assert result.intent.quantity > 0
    assert result.intent.order_authority is result.intent.exit_authority is False
    assert not hasattr(result.intent, "client_order_id")


def test_readonly_store_refuses_mutation_and_existing_deadline_gate_is_reused(tmp_path):
    import sqlite3

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from quantbot.learner.five_minute_dry_run import ReadOnlyStore
    from quantbot.learner.paper_deadlines import supervisor_objections

    path = tmp_path / "read-only.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES('unchanged')")
    store = ReadOnlyStore(path)
    with pytest.raises(OperationalError):
        with store.transaction() as session:
            session.execute(text("UPDATE sentinel SET value='bad'"))
    with store.connect() as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "unchanged"
    # Missing durable gate table is a failure, never a fabricated healthy heartbeat.
    with pytest.raises(OperationalError):
        supervisor_objections(store, account_id="synthetic", now=features()[0].decision_at)
    store.close()


@pytest.mark.parametrize(
    "case,code",
    [
        ("lineage", "FIVE_MINUTE_SOURCE_LINEAGE_MISMATCH"),
        ("broker", "PAPER_BROKER_STATE_UNVERIFIED"),
        ("early-close", "PAPER_HORIZON_EXCEEDS_SESSION_EXIT"),
        ("one-new-position", "PAPER_MODEL_POSITION_LIMIT"),
        ("wider-stop", "PAPER_STOP_EXCEEDS_APPROVED_PILOT"),
        ("sizing", "PAPER_SIZING_CONTRACT_MISMATCH"),
    ],
)
def test_unmeasured_stale_or_mismatched_controls_fail_closed(case, code):
    from dataclasses import replace

    m, p, config, request = inputs()
    policy = PaperExecutionPolicy()
    context = DryRunContext(sizing=request, source_features=p.features, new_position_count=0)
    if case == "lineage":
        context = replace(
            context, source_features=p.features.model_copy(update={"reference_close": 1.0})
        )
    elif case == "broker":
        context = replace(context, broker_verified_at=p.recorded_at - timedelta(seconds=121))
    elif case == "early-close":
        context = replace(context, session_exit_at=p.features.target_at - timedelta(seconds=1))
    elif case == "one-new-position":
        context = replace(context, new_position_count=1)
    elif case == "wider-stop":
        policy = PaperExecutionPolicy(stop_loss_bps=200)
    elif case == "sizing":
        context = replace(
            context, sizing=request.model_copy(update={"reference_price": Decimal("1")})
        )
    result = evaluate(m, p, now=p.recorded_at, config=config, policy=policy, context=context)
    assert code in result.reasons and result.intent is None


def test_durable_reader_uses_real_holdings_and_refuses_future_account_or_missing_history(tmp_path):
    from quantbot.domain import Account
    from quantbot.learner.five_minute_dry_run import ReadOnlyStore, read_sizing_inputs
    from quantbot.storage import Database, StorageRepository
    from tests.unit.risk.test_vetoes import position

    _, p, config, _ = inputs()
    database = Database.create(tmp_path / "reader.db")
    account = Account(
        account_id="synthetic",
        cash=Decimal("90000"),
        buying_power=Decimal("90000"),
        equity=Decimal("100000"),
        currency="USD",
    )
    with database.transaction() as session:
        StorageRepository(session).save_account_snapshot(
            "synthetic", account, [position("IWM", "10000")], captured_at=p.recorded_at
        )
    store = ReadOnlyStore(database.path)
    request, reasons = read_sizing_inputs(
        store,
        account_id="synthetic",
        prediction=p,
        config=config,
        policy=PaperExecutionPolicy(),
        now=p.recorded_at,
    )
    assert request is not None
    assert request.portfolio.committed_position_count == 1
    assert request.portfolio.committed_gross_exposure == 10000
    assert request.portfolio.open_risk_available is False  # no stop is invented
    future, reasons = read_sizing_inputs(
        store,
        account_id="synthetic",
        prediction=p,
        config=config,
        policy=PaperExecutionPolicy(),
        now=p.recorded_at - timedelta(seconds=1),
    )
    assert future is None and "PAPER_ACCOUNT_SNAPSHOT_NOT_CURRENT" in reasons
    missing, reasons = read_sizing_inputs(
        store,
        account_id="missing",
        prediction=p,
        config=config,
        policy=PaperExecutionPolicy(),
        now=p.recorded_at,
    )
    assert missing is None and reasons == ("PAPER_ACCOUNT_SNAPSHOT_UNAVAILABLE",)
    store.close()
    database.close()
