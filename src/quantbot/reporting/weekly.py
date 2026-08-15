"""Evidence-only weekly paper-performance aggregation and Markdown rendering."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from quantbot.domain import Fill, OrderSide, ReconciliationStatus
from quantbot.reporting.performance import FillLedgerAnalysis, analyze_fills
from quantbot.storage import Database, OrderEventRecord, StorageRepository

NOT_YET_OBSERVED = "NOT_YET_OBSERVED"


class WeeklyModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class WeeklyReportRequest(WeeklyModel):
    iso_year: int = Field(ge=2000, le=9999)
    iso_week: int = Field(ge=1, le=53)
    generated_at: datetime
    strategy_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_completed_week(self) -> Self:
        try:
            end = date.fromisocalendar(self.iso_year, self.iso_week, 1) + timedelta(days=7)
        except ValueError as error:
            raise ValueError("iso_year and iso_week do not identify a real ISO week") from error
        if self.generated_at.date() < end:
            raise ValueError("weekly reports require a completed ISO week")
        if self.strategy_id != self.strategy_id.strip():
            raise ValueError("strategy_id cannot contain surrounding whitespace")
        if self.account_id != self.account_id.strip():
            raise ValueError("account_id cannot contain surrounding whitespace")
        return self

    @property
    def start_at(self) -> datetime:
        start = date.fromisocalendar(self.iso_year, self.iso_week, 1)
        return datetime.combine(start, time.min, tzinfo=UTC)

    @property
    def end_at(self) -> datetime:
        return self.start_at + timedelta(days=7)


class WeeklyReport(WeeklyModel):
    request: WeeklyReportRequest
    observed_days: int = Field(ge=0)
    starting_equity: Decimal | None = None
    ending_equity: Decimal | None = None
    weekly_return: Decimal | None = None
    spy_weekly_return: Decimal | None = None
    realized_pnl: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    trades_entered: int | None = Field(default=None, ge=0)
    trades_exited: int | None = Field(default=None, ge=0)
    winning_trades: int | None = Field(default=None, ge=0)
    losing_trades: int | None = Field(default=None, ge=0)
    average_winner: Decimal | None = None
    average_loser: Decimal | None = None
    expectancy: Decimal | None = None
    profit_factor: Decimal | str | None = None
    maximum_intraweek_drawdown: Decimal | None = None
    total_exposure: Decimal | None = None
    slippage: Decimal | None = None
    rejected_orders: int | None = Field(default=None, ge=0)
    reconciliation_problems: int | None = Field(default=None, ge=0)
    data_api_incidents: int | None = Field(default=None, ge=0)
    risk_rejected_signals: int | None = Field(default=None, ge=0)
    bugs_discovered: tuple[str, ...] | None = None
    conclusions: tuple[str, ...]

    def render_markdown(self) -> str:
        rows = (
            ("Starting equity", _money(self.starting_equity)),
            ("Ending equity", _money(self.ending_equity)),
            ("Weekly return", _percentage(self.weekly_return)),
            ("SPY weekly return", _percentage(self.spy_weekly_return)),
            ("Realized P&L", _money(self.realized_pnl)),
            ("Unrealized P&L", _money(self.unrealized_pnl)),
            ("Trades entered", _count(self.trades_entered)),
            ("Trades exited", _count(self.trades_exited)),
            ("Winning trades", _count(self.winning_trades)),
            ("Losing trades", _count(self.losing_trades)),
            ("Average winner", _money(self.average_winner)),
            ("Average loser", _money(self.average_loser)),
            ("Expectancy", _money(self.expectancy)),
            ("Profit factor", _ratio(self.profit_factor)),
            (
                "Maximum intraweek drawdown",
                _percentage(self.maximum_intraweek_drawdown),
            ),
            ("Total exposure", _percentage(self.total_exposure)),
            ("Slippage", _money(self.slippage)),
            ("Rejected orders", _count(self.rejected_orders)),
            ("Reconciliation problems", _count(self.reconciliation_problems)),
            ("Data/API incidents", _count(self.data_api_incidents)),
            (
                "Strategy signals rejected by risk controls",
                _count(self.risk_rejected_signals),
            ),
            ("Bugs discovered", _items(self.bugs_discovered, empty="NONE_OBSERVED")),
            ("Conclusions", _items(self.conclusions, empty=NOT_YET_OBSERVED)),
        )
        lines = [
            f"# QuantBot Weekly Performance Review - {self.request.iso_year}-W{self.request.iso_week:02d}",
            "",
            f"Generated at: {_timestamp(self.request.generated_at)}",
            f"Strategy: {_markdown(self.request.strategy_id)}",
            f"Account: {_markdown(self.request.account_id)}",
            "",
            "| # | Required field | Observed value |",
            "|---:|---|---|",
        ]
        lines.extend(
            f"| {index} | {name} | {_markdown(value)} |"
            for index, (name, value) in enumerate(rows, start=1)
        )
        if self.observed_days:
            noun = "record" if self.observed_days == 1 else "records"
            count = "one" if self.observed_days == 1 else str(self.observed_days)
            note = (
                f"Evidence note: {count} durable qualification-day {noun} "
                "supports the observed zero counts. Missing measurements remain "
                "NOT_YET_OBSERVED. Strategy parameters were not modified."
            )
        else:
            note = (
                "Evidence note: no durable qualification-day record exists for this week. "
                "Missing measurements remain NOT_YET_OBSERVED. Strategy parameters were "
                "not modified."
            )
        return "\n".join((*lines, "", note, ""))


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _money(value: Decimal | None) -> str:
    if value is None:
        return NOT_YET_OBSERVED
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f}"


def _percentage(value: Decimal | None) -> str:
    if value is None:
        return NOT_YET_OBSERVED
    return f"{value * Decimal('100'):.4f}%"


def _ratio(value: Decimal | str | None) -> str:
    if value is None:
        return NOT_YET_OBSERVED
    return value if isinstance(value, str) else f"{value:.4f}"


def _count(value: int | None) -> str:
    return NOT_YET_OBSERVED if value is None else str(value)


def _items(value: tuple[str, ...] | None, *, empty: str) -> str:
    if value is None:
        return NOT_YET_OBSERVED
    return empty if not value else "; ".join(value)


def _in_window(value: datetime, request: WeeklyReportRequest) -> bool:
    return request.start_at <= value < request.end_at


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal | None:
    if len(values) < 2:
        return None
    high_water = values[0]
    maximum = Decimal("0")
    for value in values:
        high_water = max(high_water, value)
        if high_water > 0:
            maximum = max(maximum, (high_water - value) / high_water)
    return maximum


def _mean(values: tuple[Decimal, ...]) -> Decimal | None:
    return None if not values else sum(values, Decimal("0")) / Decimal(len(values))


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _event_fill_id(event: OrderEventRecord) -> str | None:
    raw_fill = event.detail.get("fill")
    if not isinstance(raw_fill, dict):
        return None
    fill_id = raw_fill.get("fill_id")
    return fill_id if isinstance(fill_id, str) else None


def _slippage(
    fills: tuple[Fill, ...],
    events: tuple[OrderEventRecord, ...],
    *,
    observed: bool,
) -> Decimal | None:
    if not observed:
        return None
    if not fills:
        return Decimal("0")
    by_fill = {_event_fill_id(event): event for event in events if _event_fill_id(event)}
    total = Decimal("0")
    for fill in fills:
        event = by_fill.get(fill.fill_id)
        if event is None:
            return None
        explicit = _decimal(event.detail.get("slippage_cost"))
        if explicit is not None and explicit >= 0:
            total += explicit
            continue
        reference = _decimal(event.detail.get("reference_price"))
        if reference is None or reference <= 0:
            return None
        adverse = (
            max(fill.price - reference, Decimal("0"))
            if fill.side is OrderSide.BUY
            else max(reference - fill.price, Decimal("0"))
        )
        total += adverse * fill.quantity
    return total


def _risk_rejected(payload: dict[str, object]) -> bool:
    if payload.get("risk_approved") is False:
        return True
    reasons = payload.get("risk_reasons")
    return isinstance(reasons, list | tuple) and bool(reasons)


def _valid_signal(payload: dict[str, object]) -> bool:
    return payload.get("action") in {"ENTER", "EXIT"} and not _risk_rejected(payload)


def build_weekly_report(database: Database, request: WeeklyReportRequest) -> WeeklyReport:
    """Aggregate one completed week exclusively from durable forward observations."""
    with database.transaction() as session:
        repository = StorageRepository(session)
        qualifications = repository.list_qualification_days(request.strategy_id)
        equity = repository.list_equity_snapshots(account_id=request.account_id)
        account_snapshots = repository.list_account_snapshots(account_id=request.account_id)
        fills = tuple(repository.list_fills())
        events = tuple(repository.list_order_events())
        orders = tuple(repository.list_broker_orders())
        reconciliations = tuple(repository.list_reconciliations())
        incidents = tuple(repository.list_incidents())
        signals = tuple(repository.list_signals(strategy_id=request.strategy_id))
        spy_bars = tuple(repository.list_bars(symbol="SPY"))

    week_dates = {
        qualification.trading_date
        for qualification in qualifications
        if request.start_at.date() <= qualification.trading_date < request.end_at.date()
    }
    observed = bool(week_dates)

    equity_before = [point for point in equity if point.captured_at < request.start_at]
    equity_week = [point for point in equity if _in_window(point.captured_at, request)]
    starting = equity_before[-1] if equity_before else (equity_week[0] if equity_week else None)
    ending = equity_week[-1] if equity_week else None
    weekly_return = (
        ending.equity / starting.equity - Decimal("1")
        if starting is not None and ending is not None and starting.equity > 0
        else None
    )
    drawdown_values = tuple(
        point.equity for point in ([starting] if starting is not None else []) + equity_week
    )

    spy_before = [bar for bar in spy_bars if bar.timestamp < request.start_at]
    spy_week = [bar for bar in spy_bars if _in_window(bar.timestamp, request)]
    spy_start = spy_before[-1] if spy_before else (spy_week[0] if spy_week else None)
    spy_end = spy_week[-1] if spy_week else None
    spy_return = (
        spy_end.close / spy_start.close - Decimal("1")
        if spy_start is not None
        and spy_end is not None
        and spy_start.timestamp != spy_end.timestamp
        else None
    )

    durable_fills = tuple(fill for fill in fills if fill.occurred_at < request.end_at)
    analysis: FillLedgerAnalysis = analyze_fills(durable_fills)
    week_fills = tuple(fill for fill in durable_fills if _in_window(fill.occurred_at, request))
    week_events = tuple(event for event in events if _in_window(event.occurred_at, request))
    realized = (
        sum(
            (
                movement.amount
                for movement in analysis.realized_movements
                if _in_window(movement.occurred_at, request)
            ),
            Decimal("0"),
        )
        if observed
        else None
    )
    entries = tuple(item for item in analysis.entries if _in_window(item.occurred_at, request))
    exits = tuple(item for item in analysis.exits if _in_window(item.occurred_at, request))
    outcomes = tuple(
        trade for trade in analysis.completed_trades if _in_window(trade.closed_at, request)
    )
    winners = tuple(trade for trade in outcomes if trade.net_pnl > 0)
    losers = tuple(trade for trade in outcomes if trade.net_pnl < 0)
    profit_factor: Decimal | str | None = None
    if losers:
        profit_factor = sum(
            (trade.net_pnl for trade in winners), Decimal("0")
        ) / abs(sum((trade.net_pnl for trade in losers), Decimal("0")))
    elif winners:
        profit_factor = "INFINITE"

    account_week = [
        snapshot for snapshot in account_snapshots if _in_window(snapshot.captured_at, request)
    ]
    ending_account = account_week[-1] if account_week else None
    unrealized = (
        sum(
            (
                (position.market_price - position.average_entry_price) * position.quantity
                for position in ending_account.positions
            ),
            Decimal("0"),
        )
        if ending_account is not None
        else None
    )
    exposure_values = tuple(
        sum((abs(position.market_value) for position in snapshot.positions), Decimal("0"))
        / snapshot.account.equity
        for snapshot in account_week
        if snapshot.account.equity > 0
    )

    rejected_ids = {
        order.broker_order_id
        for order in orders
        if _in_window(order.submitted_at, request) and order.status.lower() == "rejected"
    }
    rejected_ids.update(
        event.broker_order_id or event.event_id
        for event in week_events
        if "reject" in event.event_type.lower()
    )
    reconciliation_problems = sum(
        max(1, len(record.diffs))
        for record in reconciliations
        if _in_window(record.completed_at, request)
        and (record.status is not ReconciliationStatus.RECONCILED or record.diffs)
    )
    week_incidents = tuple(
        incident for incident in incidents if _in_window(incident.occurred_at, request)
    )
    data_incidents = tuple(
        incident
        for incident in week_incidents
        if any(
            token in incident.kind.upper()
            for token in ("DATA", "API", "BROKER", "STREAM", "NETWORK")
        )
    )
    bugs = tuple(
        f"{incident.incident_id}: {incident.kind}"
        for incident in week_incidents
        if "BUG" in incident.kind.upper()
    )
    week_signals = tuple(signal for signal in signals if _in_window(signal.occurred_at, request))
    risk_rejections = sum(_risk_rejected(signal.payload) for signal in week_signals)
    valid_signal_count = sum(_valid_signal(signal.payload) for signal in week_signals)

    if not observed:
        conclusions = (NOT_YET_OBSERVED,)
    elif valid_signal_count == 0:
        conclusions = ("NO_VALID_SIGNALS",)
    elif bugs or data_incidents or reconciliation_problems:
        conclusions = ("OPERATIONAL_REVIEW_REQUIRED",)
    else:
        conclusions = ("OBSERVATION_ONLY_NO_PARAMETER_CHANGE",)

    return WeeklyReport(
        request=request,
        observed_days=len(week_dates),
        starting_equity=starting.equity if starting is not None else None,
        ending_equity=ending.equity if ending is not None else None,
        weekly_return=weekly_return,
        spy_weekly_return=spy_return,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        trades_entered=len(entries) if observed else None,
        trades_exited=len(exits) if observed else None,
        winning_trades=len(winners) if observed else None,
        losing_trades=len(losers) if observed else None,
        average_winner=_mean(tuple(trade.net_pnl for trade in winners)),
        average_loser=_mean(tuple(trade.net_pnl for trade in losers)),
        expectancy=_mean(tuple(trade.net_pnl for trade in outcomes)),
        profit_factor=profit_factor,
        maximum_intraweek_drawdown=_maximum_drawdown(drawdown_values),
        total_exposure=_mean(exposure_values),
        slippage=_slippage(week_fills, week_events, observed=observed),
        rejected_orders=len(rejected_ids) if observed else None,
        reconciliation_problems=reconciliation_problems if observed else None,
        data_api_incidents=len(data_incidents) if observed else None,
        risk_rejected_signals=risk_rejections if observed else None,
        bugs_discovered=bugs if observed else None,
        conclusions=conclusions,
    )
