from datetime import UTC, datetime
from pathlib import Path

from quantbot.config import Settings
from quantbot.reporting.status import (
    ProjectStatusContext,
    build_project_status,
)
from quantbot.storage import Database


def test_empty_database_project_status_matches_golden_without_fabrication(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "quantbot.db")
    context = ProjectStatusContext(
        settings=Settings(),
        strategy_id="full-v1:test",
        account_id="paper-1",
        current_phase="Phase 4 - engineering verification",
        known_limitations=("Alpaca Paper connectivity has not been observed.",),
        research_underway=(),
        next_priority="Complete security and clean-process verification.",
        generated_at=datetime(2026, 8, 17, tzinfo=UTC),
    )

    status = build_project_status(database, context)
    expected = (Path(__file__).parents[2] / "golden" / "project_status_empty.md").read_text(
        encoding="utf-8"
    )

    assert status.render_markdown() == expected
    assert status.current_equity is None
    assert status.trading_days_observed == 0
    assert status.completed_trades is None
    database.close()


def test_project_status_contains_every_required_persistent_field(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    status = build_project_status(
        database,
        ProjectStatusContext(
            settings=Settings(),
            strategy_id="full-v1:test",
            account_id="paper-1",
            current_phase="Phase 4",
            known_limitations=(),
            research_underway=(),
            next_priority="Task 12",
            generated_at=datetime(2026, 8, 17, tzinfo=UTC),
        ),
    ).render_markdown()

    for field in (
        "Current phase",
        "Current strategy version",
        "Current broker/environment",
        "Last successful run",
        "Current equity",
        "Paper trading start date",
        "Trading days observed",
        "Completed trades",
        "Current drawdown",
        "Open bugs",
        "Known limitations",
        "Research underway",
        "Next highest-priority task",
    ):
        assert f"| {field} |" in status
    database.close()
