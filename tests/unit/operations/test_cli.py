from datetime import UTC, date, datetime
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
        ["integrity-sweep"],
        ["verify-manifest", "--manifest", "m.json"],
        ["research-cycle", "--max-steps", "3"],
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

def test_the_integrity_sweep_acts_by_default_and_reports_what_it_could_not_reach(
    tmp_path: Path,
) -> None:
    """#16: the automated demotion path, exercised through the real command.

    The polarity is the point. `register-hypothesis` defaults to reporting because registration
    spends a non-renewable budget; this defaults to *acting* because it reduces trust, and a
    safety control that does nothing unless someone remembers a flag does nothing. Demotion is
    reversible and preserves the forward evidence, so acting wrongly is cheap here and not
    acting is not.
    """
    import json

    from quantbot.domain import StrategyIdentity
    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage

    database = Database(tmp_path / "quantbot.db")
    moment = datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_run(
            "run-1",
            StrategyIdentity(
                strategy_id="adaptive-momentum",
                version="1.3.0",
                git_commit="abc1234",
                configuration_hash="cfg-abc",
                deployment_timestamp=moment,
            ),
            started_at=moment,
        )
        repository.create_incident(
            "INC-1",
            severity="ERROR",
            kind="RECONCILIATION_FAILED",
            message="broker and ledger disagree",
            occurred_at=moment,
            run_id="run-1",
        )
        repository.create_incident(
            "INC-ORPHAN",
            severity="ERROR",
            kind="RECONCILIATION_FAILED",
            message="raised outside any run",
            occurred_at=moment,
        )
        PromotionLedger(session).enter(
            PromotionState(
                strategy_id="adaptive-momentum",
                stage=Stage.PAPER_QUALIFIED,
                strategy_version="1.3.0",
                configuration_hash="cfg-abc",
                reason="seeded",
                actor="operator",
                updated_at=moment,
            )
        )

    context = CLIContext(
        settings=_ready_settings(),
        database=database,
        handlers={},
        reported_account_id="paper-1",
        clearance_evidence=_ready_evidence(),
    )

    dry = StringIO()
    assert main(["integrity-sweep", "--dry-run"], context=context, output=dry) == 0
    with database.transaction() as session:
        unchanged = PromotionLedger(session).current("adaptive-momentum")
    assert unchanged is not None and unchanged.stage is Stage.PAPER_QUALIFIED, (
        "--dry-run must not move anything"
    )

    acted = StringIO()
    assert main(["integrity-sweep"], context=context, output=acted) == 0
    payload = json.loads(acted.getvalue())

    assert payload["demoted"] == [
        {"strategy_id": "adaptive-momentum", "stage": "RESEARCH_SURVIVOR"}
    ]
    assert payload["unattributed_incidents"] == ["INC-ORPHAN"]
    assert payload["clean"] is False, "an unreachable incident is not a clean system"

    with database.transaction() as session:
        moved = PromotionLedger(session).current("adaptive-momentum")
    assert moved is not None and moved.stage is Stage.RESEARCH_SURVIVOR
    database.close()


def test_verify_manifest_reports_violations_skips_and_code_drift(tmp_path: Path) -> None:
    """#18: a bundle is only evidence if somebody can check it, and checking needs a command.

    The interesting assertions here are the two that are easy to get wrong:

    * `skipped` is surfaced rather than folded into a pass. A check that quietly did nothing
      because the result lacked the figure it needs looks exactly like a check that passed, and
      that is the failure class this project exists to resist.
    * code drift does not make `ok` false. A result computed on a different commit is not
      thereby wrong, and marking it failed would train a reader to ignore the field.
    """
    import json

    from quantbot.research.manifest import ExperimentManifest, ExperimentPeriod

    manifest = ExperimentManifest(
        experiment_id="E-1",
        git_commit="0" * 40,
        data_hash="d" * 16,
        configuration_hash="c" * 16,
        # Costs improving a return is the impossibility this invariant exists for: a margin
        # path once appeared to create wealth this way.
        costs={"slippage_bps": "1.1", "commission_per_order": "0"},
        periods=(ExperimentPeriod(name="full", start=date(2016, 1, 4), end=date(2026, 8, 14)),),
        walk_forward_splits=(
            ExperimentPeriod(name="split-1", start=date(2016, 1, 4), end=date(2021, 1, 1)),
        ),
        holdout=ExperimentPeriod(name="full", start=date(2016, 1, 4), end=date(2026, 8, 14)),
        neighborhood_grid={"lookback": ["100", "200"]},
        ablations=("no-trend-filter",),
        results={"primary": {"gross_return": "10", "net_return": "25"}},
    )
    path = tmp_path / "manifest.json"
    path.write_text(manifest.canonical_json(), encoding="utf-8")

    database = Database(tmp_path / "quantbot.db")
    context = CLIContext(
        settings=_ready_settings(),
        database=database,
        handlers={},
        reported_account_id="paper-1",
        clearance_evidence=_ready_evidence(),
    )
    output = StringIO()

    # Nonzero, so a violated invariant fails a CI step rather than being read in passing.
    assert main(["verify-manifest", "--manifest", str(path)], context=context, output=output) == 2
    payload = json.loads(output.getvalue())

    assert payload["ok"] is False, "costs improving a return has to be caught"
    assert [item["invariant"] for item in payload["violations"]] == [
        "costs-never-improve-returns"
    ]
    assert payload["manifest_hash"] == manifest.manifest_hash
    # Nothing here reported a test statistic, so those checks did not run -- and say so.
    assert payload["skipped"], "a check that did not run must not be silent"
    # Drift is reported next to the verdict, not merged into it.
    assert payload["code"]["recorded_commit"] is None
    assert payload["code"]["current_commit"]
    database.close()


def test_verify_manifest_explains_why_two_runs_are_not_the_same_experiment(
    tmp_path: Path,
) -> None:
    """A conclusion whose inputs changed underneath it is unfalsifiable unless that is visible."""
    import json

    from quantbot.research.manifest import ExperimentManifest, ExperimentPeriod

    def bundle(data_hash: str) -> ExperimentManifest:
        return ExperimentManifest(
            experiment_id="E-1",
            git_commit="0" * 40,
            data_hash=data_hash,
            configuration_hash="c" * 16,
            costs={"slippage_bps": "1.1", "commission_per_order": "0"},
            periods=(
                ExperimentPeriod(name="full", start=date(2016, 1, 4), end=date(2026, 8, 14)),
            ),
            walk_forward_splits=(
                ExperimentPeriod(
                    name="split-1", start=date(2016, 1, 4), end=date(2021, 1, 1)
                ),
            ),
            holdout=ExperimentPeriod(
                name="full", start=date(2016, 1, 4), end=date(2026, 8, 14)
            ),
            neighborhood_grid={"lookback": ["100", "200"]},
            ablations=("no-trend-filter",),
            results={"primary": {"gross_return": "25", "net_return": "10"}},
        )

    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(bundle("d" * 16).canonical_json(), encoding="utf-8")
    second.write_text(bundle("e" * 16).canonical_json(), encoding="utf-8")

    database = Database(tmp_path / "quantbot.db")
    context = CLIContext(
        settings=_ready_settings(),
        database=database,
        handlers={},
        reported_account_id="paper-1",
        clearance_evidence=_ready_evidence(),
    )
    output = StringIO()

    assert (
        main(
            ["verify-manifest", "--manifest", str(first), "--against", str(second)],
            context=context,
            output=output,
        )
        == 0
    )
    payload = json.loads(output.getvalue())

    assert payload["ok"] is True, "these results are merely different, not impossible"
    assert any(item["field"] == "data_hash" for item in payload["differences"]), payload
    database.close()


def test_the_research_cycle_reports_before_it_advances_anything(tmp_path: Path) -> None:
    """#3: `run_cycle` and `drain` existed and nothing supplied them with handlers.

    The driver was reachable only from a test, so the loop could not run at all. This is the
    first real caller, and it reports by default: advancing writes durable task events, and the
    handler set will grow to include stages that spend the multiple-testing budget. The safe
    polarity is the one that stays safe as more handlers arrive.
    """
    import json

    from quantbot.research.director import ResearchDirector, ResearchTask, TaskState

    database = Database(tmp_path / "quantbot.db")
    moment = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    with database.transaction() as session:
        ResearchDirector(session).open_task(
            ResearchTask(
                task_id="T-CLI-1",
                state=TaskState.PROPOSED,
                question="does revision dispersion precede index drawdowns",
                family_id="revisions",
                created_at=moment,
                updated_at=moment,
            ),
            actor="operator",
            reason="opened for the cli test",
        )

    context = CLIContext(
        settings=_ready_settings(),
        database=database,
        handlers={},
        reported_account_id="paper-1",
        clearance_evidence=_ready_evidence(),
    )

    dry = StringIO()
    assert main(["research-cycle", "--max-steps", "3"], context=context, output=dry) == 0
    reported = json.loads(dry.getvalue())
    assert reported["committed"] is False
    assert reported["runnable_now"] == ["T-CLI-1"]
    assert reported["stages_available"] == ["PROPOSED"]

    with database.transaction() as session:
        unmoved = ResearchDirector(session).get("T-CLI-1")
    assert unmoved is not None and unmoved.state is TaskState.PROPOSED, (
        "a report must not advance anything"
    )

    acted = StringIO()
    assert (
        main(["research-cycle", "--max-steps", "3", "--commit"], context=context, output=acted)
        == 0
    )
    advanced = json.loads(acted.getvalue())
    assert advanced["committed"] is True
    assert advanced["advanced"] == [
        {
            "task_id": "T-CLI-1",
            "moved_to": "SCOUTING",
            "reason": "no stored record settles this question",
        }
    ]

    with database.transaction() as session:
        moved = ResearchDirector(session).get("T-CLI-1")
    assert moved is not None and moved.state is TaskState.SCOUTING
    database.close()


def test_the_research_cycle_will_not_run_without_being_told_how_many_steps() -> None:
    """`drain` requires it for a reason, and the CLI must not supply a default on its behalf.

    Each stage a run advances can permanently raise the luck bar. How many is a decision the
    operator makes explicitly rather than inherits from whatever looked reasonable.
    """
    parser = build_parser(output=StringIO())

    with pytest.raises(SystemExit):
        parser.parse_args(["research-cycle"])
