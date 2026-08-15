from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from quantbot.domain import (
    Account,
    Bar,
    BrokerOrder,
    Fill,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    ReconciliationStatus,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.reporting.performance import analyze_fills, render_benchmark_comparison
from quantbot.reporting.weekly import WeeklyReportRequest, build_weekly_report
from quantbot.storage import Database, StorageRepository

GENERATED_AT = datetime(2026, 8, 17, tzinfo=UTC)


def _request() -> WeeklyReportRequest:
    return WeeklyReportRequest(
        iso_year=2026,
        iso_week=33,
        generated_at=GENERATED_AT,
        strategy_id="full-v1:test",
        account_id="paper-1",
    )


def test_observed_week_without_signals_matches_golden_report(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    with database.transaction() as session:
        StorageRepository(session).record_qualification_day(
            "full-v1:test",
            date(2026, 8, 14),
            qualified=True,
            detail={"source": "completed paper cycle"},
        )

    report = build_weekly_report(database, _request())
    expected = (Path(__file__).parents[2] / "golden" / "weekly_observed_no_signals.md").read_text(
        encoding="utf-8"
    )

    assert report.render_markdown() == expected
    assert report.conclusions == ("NO_VALID_SIGNALS",)
    assert report.trades_entered == 0
    database.close()


def test_empty_week_never_fabricates_zero_observations(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")

    report = build_weekly_report(database, _request())
    rendered = report.render_markdown()

    assert report.trades_entered is None
    assert report.rejected_orders is None
    assert "| 7 | Trades entered | NOT_YET_OBSERVED |" in rendered
    assert "| 18 | Rejected orders | NOT_YET_OBSERVED |" in rendered
    database.close()


def test_fill_analysis_counts_completed_round_trips_and_net_outcomes() -> None:
    fills = (
        Fill(
            fill_id="fill-buy",
            broker_order_id="order-buy",
            symbol="SPY",
            side=OrderSide.BUY,
            quantity=Decimal("2"),
            price=Decimal("100"),
            occurred_at=datetime(2026, 8, 11, 14, 30, tzinfo=UTC),
            fee=Decimal("1"),
        ),
        Fill(
            fill_id="fill-sell",
            broker_order_id="order-sell",
            symbol="SPY",
            side=OrderSide.SELL,
            quantity=Decimal("2"),
            price=Decimal("110"),
            occurred_at=datetime(2026, 8, 14, 14, 30, tzinfo=UTC),
            fee=Decimal("1"),
        ),
    )

    analysis = analyze_fills(fills)

    assert len(analysis.completed_trades) == 1
    assert analysis.completed_trades[0].gross_pnl == Decimal("20")
    assert analysis.completed_trades[0].net_pnl == Decimal("18")
    assert analysis.realized_movements[-1].amount == Decimal("19")


def test_benchmark_comparison_requires_all_six_named_variants() -> None:
    rendered = render_benchmark_comparison(
        {
            "SPY buy-and-hold": Decimal("0.10"),
            "SMA200": Decimal("0.08"),
            "Donchian baseline": Decimal("0.07"),
            "pure 12-1 momentum": Decimal("0.12"),
            "momentum + trend": Decimal("0.11"),
            "full strategy": Decimal("0.13"),
        }
    )

    for name in (
        "SPY buy-and-hold",
        "SMA200",
        "Donchian baseline",
        "pure 12-1 momentum",
        "momentum + trend",
        "full strategy",
    ):
        assert name in rendered


def test_weekly_report_counts_durable_operational_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    observed_at = datetime(2026, 8, 14, 20, tzinfo=UTC)
    identity = StrategyIdentity(
        strategy_id="full-v1:test",
        version="1.0.0",
        git_commit="abc123",
        configuration_hash="a" * 64,
        deployment_timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )
    intent = OrderIntent(
        intent_id="intent-rejected",
        client_order_id="client-rejected",
        strategy_id=identity.strategy_id,
        symbol="SPY",
        signal_date=date(2026, 8, 14),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("1"),
        created_at=observed_at,
    )
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_run("run-1", identity, started_at=observed_at)
        repository.record_qualification_day(
            identity.strategy_id,
            date(2026, 8, 14),
            qualified=True,
        )
        repository.record_signal(
            "signal-risk-rejected",
            run_id="run-1",
            strategy_id=identity.strategy_id,
            symbol="SPY",
            occurred_at=observed_at,
            payload={"action": "ENTER", "risk_approved": False},
        )
        repository.create_order_intent(intent)
        repository.save_broker_order(
            BrokerOrder(
                broker_order_id="broker-rejected",
                client_order_id=intent.client_order_id,
                symbol="SPY",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                quantity=Decimal("1"),
                filled_quantity=Decimal("0"),
                status="rejected",
                submitted_at=observed_at,
            )
        )
        repository.save_reconciliation(
            "reconciliation-problem",
            status=ReconciliationStatus.RECONCILIATION_REQUIRED,
            started_at=observed_at,
            completed_at=observed_at,
            diffs=(
                {
                    "diff_id": "diff-1",
                    "category": "ACCOUNT",
                    "key": "equity",
                    "expected": {"value": "1000"},
                    "actual": {"value": "999"},
                },
            ),
        )
        repository.create_incident(
            "incident-data",
            severity="ERROR",
            kind="MARKET_DATA_STALE",
            message="Daily bar did not arrive before the freshness deadline",
            occurred_at=observed_at,
        )
        repository.save_account_snapshot(
            "snapshot-before",
            Account(
                account_id="paper-1",
                cash=Decimal("1000"),
                buying_power=Decimal("1000"),
                equity=Decimal("1000"),
                currency="USD",
            ),
            (),
            captured_at=datetime(2026, 8, 7, 20, tzinfo=UTC),
        )
        repository.save_account_snapshot(
            "snapshot-end",
            Account(
                account_id="paper-1",
                cash=Decimal("880"),
                buying_power=Decimal("880"),
                equity=Decimal("1100"),
                currency="USD",
            ),
            (
                Position(
                    symbol="SPY",
                    quantity=Decimal("2"),
                    market_price=Decimal("110"),
                    market_value=Decimal("220"),
                    average_entry_price=Decimal("100"),
                ),
            ),
            captured_at=observed_at,
        )
        repository.save_bars(
            (
                Bar(
                    symbol="SPY",
                    timestamp=datetime(2026, 8, 7, 20, tzinfo=UTC),
                    open=Decimal("100"),
                    high=Decimal("100"),
                    low=Decimal("100"),
                    close=Decimal("100"),
                    volume=Decimal("1"),
                    adjustment=Decimal("1"),
                ),
                Bar(
                    symbol="SPY",
                    timestamp=observed_at,
                    open=Decimal("110"),
                    high=Decimal("110"),
                    low=Decimal("110"),
                    close=Decimal("110"),
                    volume=Decimal("1"),
                    adjustment=Decimal("1"),
                ),
            ),
            provider="alpaca",
            adjustment_metadata={"mode": "all"},
        )

    report = build_weekly_report(database, _request())

    assert report.weekly_return == Decimal("0.1")
    assert report.spy_weekly_return == Decimal("0.1")
    assert report.unrealized_pnl == Decimal("20")
    assert report.total_exposure == Decimal("0.2")
    assert report.rejected_orders == 1
    assert report.reconciliation_problems == 1
    assert report.data_api_incidents == 1
    assert report.risk_rejected_signals == 1
    database.close()
