from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pytest

from quantbot.cli import CLIContext, build_parser, main
from quantbot.config import Settings
from quantbot.domain import ReconciliationStatus
from quantbot.operations.cycle import PAPER_SMOKE_ACKNOWLEDGEMENT, authorize_paper_smoke
from quantbot.operations.kill_switch import KillSwitchController, ReadinessEvidence
from quantbot.storage import Database, StorageRepository


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["doctor"],
        ["sync-data"],
        ["reconcile"],
        ["run-once"],
        ["daemon"],
        ["kill-switch", "engage", "--reason", "operator"],
        ["kill-switch", "clear", "--reason", "operator"],
        ["backtest"],
        ["report-weekly"],
        ["paper-smoke", "--acknowledgement", PAPER_SMOKE_ACKNOWLEDGEMENT],
        ["hypotheses"],
        ["register-hypothesis", "--draft", "d.json", "--critique", "c.json"],
        ["research-status"],
    ],
)
def test_required_cli_commands_are_registered(argv: list[str]) -> None:
    parser = build_parser(output=StringIO())

    parsed = parser.parse_args(argv)

    assert parsed.command == argv[0]


def _ready_settings() -> Settings:
    return Settings(
        EXPECTED_ACCOUNT_ID="paper-1",
        ALPACA_PAPER_API_KEY="paper-key",
        ALPACA_PAPER_API_SECRET="paper-secret",
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=True,
    )


def _ready_evidence() -> ReadinessEvidence:
    return ReadinessEvidence(
        paper_mode=True,
        account_verified=True,
        broker_healthy=True,
        data_healthy=True,
        risk_healthy=True,
        reconciliation_successful=True,
    )


def test_paper_smoke_requires_separate_exact_acknowledgement() -> None:
    with pytest.raises(ValueError, match="acknowledgement"):
        authorize_paper_smoke(
            _ready_settings(),
            reported_account_id="paper-1",
            durable_kill_switch_engaged=False,
            durable_reconciliation_successful=True,
            readiness_evidence=_ready_evidence(),
            acknowledgement="yes",
        )

    authorization = authorize_paper_smoke(
        _ready_settings(),
        reported_account_id="paper-1",
        durable_kill_switch_engaged=False,
        durable_reconciliation_successful=True,
        readiness_evidence=_ready_evidence(),
        acknowledgement=PAPER_SMOKE_ACKNOWLEDGEMENT,
    )

    assert authorization.authorized is True
    assert authorization.environment == "PAPER"


def test_paper_smoke_never_accepts_live_mode() -> None:
    live = _ready_settings().model_copy(
        update={
            "TRADING_MODE": "LIVE",
            "BROKER_ENVIRONMENT": "LIVE",
            "LIVE_TRADING_ACKNOWLEDGED": True,
            "ALPACA_LIVE_API_KEY": "live-key",
            "ALPACA_LIVE_API_SECRET": "live-secret",
        }
    )

    with pytest.raises(ValueError, match="paper mode"):
        authorize_paper_smoke(
            live,
            reported_account_id="paper-1",
            durable_kill_switch_engaged=False,
            durable_reconciliation_successful=True,
            readiness_evidence=_ready_evidence(),
            acknowledgement=PAPER_SMOKE_ACKNOWLEDGEMENT,
        )


def test_paper_smoke_requires_current_and_durable_readiness() -> None:
    with pytest.raises(ValueError, match="durable reconciliation"):
        authorize_paper_smoke(
            _ready_settings(),
            reported_account_id="paper-1",
            durable_kill_switch_engaged=False,
            durable_reconciliation_successful=False,
            readiness_evidence=_ready_evidence(),
            acknowledgement=PAPER_SMOKE_ACKNOWLEDGEMENT,
        )

    with pytest.raises(ValueError, match="MARKET_DATA_UNHEALTHY"):
        authorize_paper_smoke(
            _ready_settings(),
            reported_account_id="paper-1",
            durable_kill_switch_engaged=False,
            durable_reconciliation_successful=True,
            readiness_evidence=_ready_evidence().model_copy(update={"data_healthy": False}),
            acknowledgement=PAPER_SMOKE_ACKNOWLEDGEMENT,
        )


def test_cli_paper_smoke_refuses_an_empty_reconciliation_ledger(tmp_path: Path) -> None:
    database = Database(tmp_path / "quantbot.db")
    KillSwitchController(database).clear(
        reason="all paper gates verified",
        evidence=_ready_evidence(),
        updated_at=datetime(2026, 8, 14, 21, 5, tzinfo=UTC),
    )
    invocations: list[str] = []

    def smoke_handler(_args: object) -> dict[str, object]:
        invocations.append("paper-smoke")
        return {"ok": True}

    context = CLIContext(
        settings=_ready_settings(),
        database=database,
        handlers={"paper-smoke": smoke_handler},
        reported_account_id="paper-1",
        clearance_evidence=_ready_evidence(),
    )

    with pytest.raises(ValueError, match="durable reconciliation"):
        main(
            ["paper-smoke", "--acknowledgement", PAPER_SMOKE_ACKNOWLEDGEMENT],
            context=context,
            output=StringIO(),
        )

    assert invocations == []

    with database.transaction() as session:
        StorageRepository(session).save_reconciliation(
            "reconciliation-1",
            status=ReconciliationStatus.RECONCILED,
            started_at=datetime(2026, 8, 14, 21, 4, tzinfo=UTC),
            completed_at=datetime(2026, 8, 14, 21, 5, tzinfo=UTC),
            diffs=(),
        )

    assert (
        main(
            ["paper-smoke", "--acknowledgement", PAPER_SMOKE_ACKNOWLEDGEMENT],
            context=context,
            output=StringIO(),
        )
        == 0
    )
    assert invocations == ["paper-smoke"]
    database.close()


def test_cli_dispatches_injected_operations_and_status_never_prints_secrets(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "quantbot.db")
    settings = _ready_settings()
    context = CLIContext(
        settings=settings,
        database=database,
        handlers={"backtest": lambda _args: {"ok": True, "result": "complete"}},
        reported_account_id="paper-1",
        clearance_evidence=_ready_evidence(),
    )
    backtest_output = StringIO()
    status_output = StringIO()

    assert main(["backtest"], context=context, output=backtest_output) == 0
    assert main(["status"], context=context, output=status_output) == 0

    assert '"result": "complete"' in backtest_output.getvalue()
    assert "paper-key" not in status_output.getvalue()
    assert "paper-secret" not in status_output.getvalue()
    database.close()


def test_registering_a_hypothesis_defaults_to_spending_nothing() -> None:
    """`--commit` is opt-in, because registration is not reversible in the way that matters.

    It reserves protected evaluation windows and permanently raises the multiple-testing burden
    for everything registered after it. A CLI that froze on the default invocation would make
    burning a holdout the easiest thing to do by accident.
    """
    parser = build_parser(output=StringIO())

    preview = parser.parse_args(
        ["register-hypothesis", "--draft", "d.json", "--critique", "c.json"]
    )
    assert preview.commit is False

    explicit = parser.parse_args(
        ["register-hypothesis", "--draft", "d.json", "--critique", "c.json", "--commit"]
    )
    assert explicit.commit is True


def test_a_registration_requires_both_a_draft_and_a_critique() -> None:
    """Neither is optional. A registration without a critique is unreviewed by construction."""
    parser = build_parser(output=StringIO())

    for argv in (
        ["register-hypothesis", "--draft", "d.json"],
        ["register-hypothesis", "--critique", "c.json"],
        ["register-hypothesis"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_research_status_reports_being_unconfigured_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "Research cannot run yet" is a state to display, not an error that hides everything else.

    An operator checking status while no model is configured should still see the task queue.
    Raising here would mean the one command that reports readiness is the one that cannot run
    until the system is already ready.
    """
    from quantbot.research.composition import CRITIC, ENDPOINT, GENERATOR

    for name in (ENDPOINT, GENERATOR, CRITIC):
        monkeypatch.delenv(name, raising=False)

    parser = build_parser(output=StringIO())
    parsed = parser.parse_args(["research-status"])

    assert parsed.command == "research-status"
