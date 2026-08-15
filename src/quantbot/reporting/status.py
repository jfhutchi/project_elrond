"""Durable project-status snapshot with explicit missing-evidence markers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quantbot.config import Settings
from quantbot.reporting.performance import analyze_fills
from quantbot.reporting.weekly import NOT_YET_OBSERVED
from quantbot.storage import Database, StorageRepository


class ProjectStatusContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    settings: Settings
    strategy_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    current_phase: str = Field(min_length=1)
    known_limitations: tuple[str, ...]
    research_underway: tuple[str, ...]
    next_priority: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("strategy_id", "account_id", "current_phase", "next_priority")
    @classmethod
    def reject_padded_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("status text cannot contain surrounding whitespace")
        return value

    @field_validator("known_limitations", "research_underway")
    @classmethod
    def validate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or item != item.strip() for item in value):
            raise ValueError("status items must be nonempty without surrounding whitespace")
        return value

    @field_validator("generated_at", mode="after")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)


class ProjectStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    current_phase: str
    current_strategy_version: str | None
    current_broker_environment: str
    last_successful_run: str | None
    current_equity: Decimal | None
    paper_trading_start_date: str | None
    trading_days_observed: int = Field(ge=0)
    completed_trades: int | None = Field(default=None, ge=0)
    current_drawdown: Decimal | None
    open_bugs: tuple[str, ...]
    known_limitations: tuple[str, ...]
    research_underway: tuple[str, ...]
    next_priority: str

    @field_validator("generated_at", mode="after")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

    def render_markdown(self) -> str:
        fields = (
            ("Current phase", self.current_phase),
            ("Current strategy version", self.current_strategy_version),
            ("Current broker/environment", self.current_broker_environment),
            ("Last successful run", self.last_successful_run),
            (
                "Current equity",
                None if self.current_equity is None else _money(self.current_equity),
            ),
            ("Paper trading start date", self.paper_trading_start_date),
            ("Trading days observed", str(self.trading_days_observed)),
            (
                "Completed trades",
                None if self.completed_trades is None else str(self.completed_trades),
            ),
            (
                "Current drawdown",
                None if self.current_drawdown is None else _percentage(self.current_drawdown),
            ),
            ("Open bugs", _items(self.open_bugs, "NONE_OBSERVED")),
            ("Known limitations", _items(self.known_limitations, "NONE")),
            ("Research underway", _items(self.research_underway, "NONE")),
            ("Next highest-priority task", self.next_priority),
        )
        lines = [
            "# QuantBot Project Status",
            "",
            f"Generated at: {_timestamp(self.generated_at)}",
            "",
            "| Field | Verified value |",
            "|---|---|",
        ]
        for name, value in fields:
            rendered = NOT_YET_OBSERVED if value is None else value
            lines.append(f"| {name} | {_markdown(rendered)} |")
        lines.extend(
            (
                "",
                "Evidence policy: NOT_YET_OBSERVED is used whenever the durable ledger does "
                "not contain authentic forward evidence. Historical backtests never count as "
                "paper-trading observations.",
                "",
            )
        )
        return "\n".join(lines)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):.2f}"


def _percentage(value: Decimal) -> str:
    return f"{value * Decimal('100'):.4f}%"


def _items(values: tuple[str, ...], empty: str) -> str:
    return empty if not values else "; ".join(values)


def _markdown(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _current_drawdown(equity: tuple[Decimal, ...]) -> Decimal | None:
    if not equity:
        return None
    high_water = max(equity)
    return Decimal("0") if high_water == 0 else (high_water - equity[-1]) / high_water


def build_project_status(database: Database, context: ProjectStatusContext) -> ProjectStatus:
    """Build resumable status from configuration labels and durable forward evidence."""
    with database.transaction() as session:
        repository = StorageRepository(session)
        identity = repository.get_strategy_deployment(context.strategy_id)
        runs = repository.list_runs(strategy_id=context.strategy_id)
        equity_records = repository.list_equity_snapshots(account_id=context.account_id)
        qualification_days = repository.list_qualification_days(context.strategy_id)
        fills = tuple(repository.list_fills())
        incidents = tuple(repository.list_incidents(unresolved_only=True))

    successful = [run for run in runs if run.status == "SUCCEEDED" and run.finished_at is not None]
    last_run = successful[-1] if successful else None
    qualified_dates = sorted(
        record.trading_date for record in qualification_days if record.qualified
    )
    fill_analysis = analyze_fills(fills)
    has_forward_trade_evidence = bool(fills or qualification_days)
    open_bugs = tuple(
        f"{incident.incident_id}: {incident.kind}"
        for incident in incidents
        if "BUG" in incident.kind.upper()
    )
    broker_environment = (
        f"{context.settings.BROKER_PROVIDER.value.upper()} / "
        f"{context.settings.BROKER_ENVIRONMENT.value} "
        "(configured; connection NOT_YET_OBSERVED)"
    )
    return ProjectStatus(
        generated_at=context.generated_at,
        current_phase=context.current_phase,
        current_strategy_version=(
            f"{identity.version} ({identity.strategy_id})" if identity is not None else None
        ),
        current_broker_environment=broker_environment,
        last_successful_run=(
            f"{last_run.run_id} at {_timestamp(last_run.finished_at)}"
            if last_run is not None and last_run.finished_at is not None
            else None
        ),
        current_equity=equity_records[-1].equity if equity_records else None,
        paper_trading_start_date=(qualified_dates[0].isoformat() if qualified_dates else None),
        trading_days_observed=len(qualified_dates),
        completed_trades=(
            len(fill_analysis.completed_trades) if has_forward_trade_evidence else None
        ),
        current_drawdown=_current_drawdown(
            tuple(record.equity for record in equity_records)
        ),
        open_bugs=open_bugs,
        known_limitations=context.known_limitations,
        research_underway=context.research_underway,
        next_priority=context.next_priority,
    )
