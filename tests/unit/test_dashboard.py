"""What the operator dashboard may render, and what it must never be able to render again.

`scripts/dashboard.py` used to carry a hardcoded `RESEARCH` tuple of findings and a hardcoded
`BENCHMARKS` table of backtest results, both rendered unconditionally. That made it a second
source of truth on a system whose whole premise is that institutional memory must be durable
rather than regenerated, and it had already drifted: fourteen literal rows against twenty-four
entries in `REFUTED.md`, with zero rows in the database it claimed to be reporting on.

These tests assert the specific protection rather than a proxy for it. Checking that a known
string is absent would only catch the literal someone happened to delete; the assertion here is
that each research panel holds **exactly as many rows as the store holds records**. A
reintroduced literal fails that by arithmetic, whatever it is named and however it is spelled.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

from quantbot.domain import (
    BrokerOrder,
    Fill,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.research import (
    ComparisonStructure,
    CriticVerdict,
    Critique,
    DataRole,
    DataWindow,
    EconomicProfile,
    EffectSpecification,
    EpistemicStatus,
    Estimand,
    EvidenceBasis,
    HypothesisDraft,
    HypothesisRegistry,
)
from quantbot.research.budget import BudgetGovernor, Cap, Resource
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
from quantbot.storage import Database, StorageRepository

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "scripts" / "dashboard.py"
NOW = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)

#: Every research panel, with the message it must show when its table has no rows.
PANELS = {
    "panel-hypotheses": "No hypotheses registered",
    "panel-records": "No research records stored",
    "panel-tasks": "No research tasks",
    "panel-trials": "No trials recorded against any budget",
    # #15's deferred operator views, buildable once #22 gave windows a state.
    "panel-windows": "No protected evaluation window is claimed",
    "panel-attention": "Nothing needs you",
    "panel-ladder": "No strategy is on the promotion ladder",
    "panel-compute": "No sandboxed compute has been charged to any budget",
    "panel-overrides": "No budget cap has been overridden",
}


def _dashboard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("quantbot_dashboard", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["quantbot_dashboard"] = module
    spec.loader.exec_module(module)
    return module


def _body(page: str, panel_id: str) -> str:
    table = re.search(rf'<table id="{panel_id}">(.*?)</table>', page, re.S)
    assert table is not None, f"the page has no {panel_id} panel"
    body = re.search(r"<tbody>(.*?)</tbody>", table.group(1), re.S)
    assert body is not None, f"{panel_id} has no body"
    return body.group(1)


def _rows(page: str, panel_id: str) -> list[str]:
    """Data rows in one panel. The empty-state placeholder is not a data row."""
    return [
        row
        for row in re.findall(r"<tr>(.*?)</tr>", _body(page, panel_id), re.S)
        if "class='empty'" not in row
    ]


def render(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Build the real page against the database at `tmp_path`, with no broker reachable."""
    monkeypatch.delenv("ALPACA_PAPER_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAPER_API_SECRET", raising=False)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "dashboard.html"
    _dashboard().build(out)
    return out.read_text(encoding="utf-8")


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    database = Database(tmp_path / "quantbot.db")
    database.close()
    return tmp_path


def cleared(hypothesis_id: str = "H-2026-100", version: int = 1) -> Critique:
    return Critique(
        hypothesis_id=hypothesis_id,
        hypothesis_version=version,
        critic="deterministic",
        verdict=CriticVerdict.PROCEED,
        reasons=("no mechanical check produced an objection",),
        produced_at=NOW,
    )


def draft(**overrides: object) -> HypothesisDraft:
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-100",
        "family_id": "trend-following",
        "question": "Does a 200-day trend filter on SPY beat buy-and-hold on Sharpe?",
        "prediction": "Sharpe is higher by at least 0.10.",
        "null_hypothesis": "The Sharpe difference is zero.",
        "falsified_if": "The paired difference fails to clear the luck bar.",
        "universe": ("SPY",),
        "features": ("close_sma_200",),
        "target": "next-session excess return",
        "windows": (
            DataWindow(
                dataset="sip-us-equities-daily",
                snapshot="2026-08-20",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(2016, 1, 4),
                end=date(2026, 8, 20),
            ),
        ),
        "primary_estimand": "annualised Sharpe difference against SPY buy-and-hold",
        "effect": EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("1.0"),
            minimum_practical=Decimal("0.5"),
            justification="Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
            comparison=ComparisonStructure.SINGLE_SAMPLE,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        ),
        "available_observations": 2669,
        "confounders": ("regime dependence",),
        "proposed_by": "claude-opus-5",
        "basis": EvidenceBasis(
            citations=(),
            status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE,
        ),
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def populate(root: Path) -> None:
    """One row in each research table, so each panel has a known, countable truth."""
    database = Database(root / "quantbot.db")
    with database.transaction() as session:
        HypothesisRegistry(session).register(draft(), now=NOW, critique=cleared())

        memory = ResearchMemory(session)
        memory.record(
            ResearchRecord(
                record_id="finding-refuted",
                kind=RecordKind.FINDING,
                verdict=Verdict.REFUTED,
                subject="momentum ranking on US sector ETFs",
                statement="Top-minus-bottom is t=0.77 against a 2.87 bar.",
                source="cycle 15",
                recorded_at=NOW,
            )
        )
        memory.record(
            ResearchRecord(
                record_id="finding-underpowered",
                kind=RecordKind.FINDING,
                verdict=Verdict.UNDERPOWERED,
                subject="BTC trend standalone",
                statement="Could only detect 1.60 Sharpe; measured 0.53.",
                source="refuted-power-audit-2026-08-19",
                recorded_at=NOW,
            )
        )

        director = ResearchDirector(session)
        director.open_task(
            ResearchTask(
                task_id="T-2026-050",
                state=TaskState.PROPOSED,
                question="Is there anything left to test on this dataset?",
                family_id="trend-following",
                created_at=NOW,
                updated_at=NOW,
            ),
            actor="operator",
            reason="opened for the dashboard test",
        )
        director.advance(
            "T-2026-050",
            TaskState.BLOCKED,
            actor="operator",
            reason="waiting on a new data source",
            now=NOW,
        )

        governor = BudgetGovernor(
            session,
            caps=[
                Cap(
                    budget_key="dataset:sip-us-equities-daily",
                    resource=Resource.TRIALS,
                    limit=Decimal("100"),
                )
            ],
        )
        governor.spend(
            "dataset:sip-us-equities-daily",
            Resource.TRIALS,
            Decimal("4"),
            task="dashboard test",
            now=NOW,
        )

        _seed_ladder(session)
        _seed_compute(session)
    database.close()


def _seed_compute(session: object) -> None:
    """One charged run and one override, so both budget panels have a countable truth."""
    from quantbot.research.compute import record_sandbox_run
    from quantbot.sandbox.runner import SandboxResult

    book = BudgetGovernor(
        session,
        caps=[
            Cap(
                budget_key="compute:sandbox",
                resource=Resource.WALL_SECONDS,
                limit=Decimal("5"),
            )
        ],
    )
    # Deliberately over the cap, so the override panel has its row as well. An override is
    # the only way a budget row differs from every other budget row, so a fixture without
    # one cannot tell whether the panel filters or merely renders everything.
    record_sandbox_run(
        book,
        SandboxResult(
            ok=True,
            exit_code=0,
            stdout="",
            stderr="",
            duration_seconds=9.0,
            peak_memory_mb=64.0,
        ),
        budget_key="compute:sandbox",
        task="fixture run",
        now=NOW,
    )


#: Three sessions and two fills, which is roughly the real account's position and nowhere near
#: the thirty-day window. The panel has to say so rather than round it up into progress.
LADDER_DAYS = 3
LADDER_FILLS = 2


def _seed_ladder(session: object) -> None:
    """A strategy on the ladder, plus the durable trading facts behind its forward counters."""
    repository = StorageRepository(session)
    repository.save_strategy_deployment(
        StrategyIdentity(
            strategy_id="adaptive-momentum",
            version="1.2.0",
            git_commit="abc1234",
            configuration_hash="cfg-abcdef012345",
            deployment_timestamp=NOW,
        )
    )
    for index in range(LADDER_DAYS):
        trading_date = date(2026, 8, 17 + index)
        repository.record_qualification_day("adaptive-momentum", trading_date, qualified=True)
        if index >= LADDER_FILLS:
            continue
        repository.create_order_intent(
            OrderIntent(
                intent_id=f"intent-{index}",
                client_order_id=f"client-{index}",
                strategy_id="adaptive-momentum",
                symbol="SPY",
                signal_date=trading_date,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                quantity="1",
                created_at=NOW,
                state=IntentState.RISK_APPROVED,
            )
        )
        repository.save_broker_order(
            BrokerOrder(
                broker_order_id=f"broker-{index}",
                client_order_id=f"client-{index}",
                symbol="SPY",
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                quantity="1",
                filled_quantity="1",
                status="filled",
                submitted_at=NOW,
            )
        )
        repository.record_fill(
            Fill(
                fill_id=f"fill-{index}",
                broker_order_id=f"broker-{index}",
                symbol="SPY",
                side=OrderSide.BUY,
                quantity="1",
                price="500.00",
                occurred_at=NOW,
                fee="0",
            )
        )

    PromotionLedger(session).enter(
        PromotionState(
            strategy_id="adaptive-momentum",
            stage=Stage.PAPER_OBSERVATION,
            strategy_version="1.2.0",
            configuration_hash="cfg-abcdef012345",
            reason="seeded for the dashboard test",
            actor="operator",
            updated_at=NOW,
        )
    )


def test_an_empty_research_store_renders_as_empty(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest render of a store with no rows, and the state the real database is in.

    Not "shows a plausible history": shows nothing, and says so. A panel that falls back to a
    literal when the query comes back empty is the same defect with an extra branch.
    """
    page = render(ledger, monkeypatch)

    for panel_id, message in PANELS.items():
        assert _rows(page, panel_id) == [], f"{panel_id} rendered rows the database does not hold"
        assert message in _body(page, panel_id)


def test_the_panels_render_exactly_what_the_database_holds(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One row in, one row out, per panel — the count is the assertion.

    Counting is what makes this resistant to the defect it exists to prevent. Asserting only
    that the seeded text appears would still pass with a hardcoded row rendered beside it.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    assert len(_rows(page, "panel-hypotheses")) == 1
    assert len(_rows(page, "panel-records")) == 2
    assert len(_rows(page, "panel-tasks")) == 1
    assert len(_rows(page, "panel-trials")) == 1

    hypothesis = _rows(page, "panel-hypotheses")[0]
    assert "H-2026-100 v1" in hypothesis
    assert "Does a 200-day trend filter on SPY beat buy-and-hold on Sharpe?" in hypothesis
    assert "trend-following" in hypothesis

    task = _rows(page, "panel-tasks")[0]
    assert "T-2026-050" in task
    assert "BLOCKED" in task

    trials = _rows(page, "panel-trials")[0]
    assert "dataset:sip-us-equities-daily" in trials
    assert ">4<" in trials
    # The bar the four trials already imply, computed rather than quoted.
    assert str(_dashboard().luck_bar_at(4)) in trials

    for message in PANELS.values():
        assert message not in page


def test_an_underpowered_record_is_not_rendered_as_a_refutation(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question the data could not resolve was never tested, and the page must not blur that.

    Both rows are findings and both are terminal, so a renderer that reaches for one "verdict"
    style would collapse them. The verdict string and the tone are both carried through.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    rows = {row.split("</td>")[0]: row for row in _rows(page, "panel-records")}
    refuted = next(row for key, row in rows.items() if "momentum ranking" in key)
    underpowered = next(row for key, row in rows.items() if "BTC trend standalone" in key)

    assert "pill--critical" in refuted and "REFUTED" in refuted
    assert "pill--warning" in underpowered and "UNDERPOWERED" in underpowered
    assert "REFUTED" not in underpowered


def test_the_dashboard_holds_no_hand_maintained_records(ledger: Path) -> None:
    """The literals cannot come back, and this fails the moment one does.

    A module-level collection literal is how both `RESEARCH` and `BENCHMARKS` were spelled, and
    it is the only shape in which a hand-maintained table can be smuggled back into a generator
    whose panels are otherwise built from query results. There is nothing this module needs one
    for: every constant it renders is either read from the database or computed from it.
    """
    module = ast.parse(SOURCE.read_text(encoding="utf-8"))
    literals = [
        target.id
        for node in module.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
        and isinstance(node.value, ast.Tuple | ast.List | ast.Dict | ast.Set)
    ]
    assert literals == [], (
        f"{SOURCE.name} declares hand-maintained data at module level: {literals}. "
        "Research state belongs in the durable store, which the page already reads."
    )


def test_a_reserved_window_is_not_shown_as_a_spent_one(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#15 and #22: reserved and consumed are different facts and the panel keeps them apart.

    A reservation is a claim on data nobody has looked at, and it lapses. A consumption is
    permanent, because contamination is a fact about what was seen. Collapsing them into one
    "used" count would tell an operator a holdout is gone when it is merely spoken for -- which
    is the capacity loss #22 was opened to fix, reintroduced at the display layer.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    rows = _rows(page, "panel-windows")
    assert len(rows) == 1, "one window in the ledger, one row rendered"
    window = rows[0]
    assert "H-2026-100" in window
    assert "RESERVED" in window
    assert "CONSUMED" not in window, "an unspent claim must not read as a spent one"


def test_the_ladder_panel_counts_forward_evidence_the_ledger_supports_and_no_more(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#15 and #16: the view an operator reads before considering real capital.

    A flattering number here is the most expensive wrong number in the system, so the counters
    are read from the trading ledger rather than supplied. The seeded strategy has three
    qualified sessions and two fills; the panel must say exactly that, and must state the
    shortfall rather than describing the strategy as progressing.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    rows = _rows(page, "panel-ladder")
    assert len(rows) == 1, "one strategy on the ladder, one row rendered"
    row = rows[0]

    assert "adaptive-momentum" in row
    assert "PAPER_OBSERVATION" in row
    # Exactly the ledger's counts, not a rounding of them.
    assert f"{LADDER_DAYS}/30 days" in row, row
    assert f"{LADDER_FILLS}/30 trades" in row, row
    # And the gap named, in the units the operator has to wait out.
    assert f"{30 - LADDER_DAYS} more forward days" in row, row
    assert f"{30 - LADDER_FILLS} more trades" in row, row


def test_the_ladder_panel_never_offers_a_route_to_live_trading(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The absent destination, asserted at the display layer too.

    `Stage` has no LIVE member, so the panel cannot render one. This checks the rendered page
    rather than the enum because a dashboard is where a human forms an intention, and a column
    that looked like a path to deployment would be dangerous even with nothing behind it.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    body = _body(page, "panel-ladder")
    assert "LIVE_TRADING" not in body
    assert ">LIVE<" not in body


def test_the_attention_panel_shows_a_blocked_task_and_not_routine_activity(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#15: the ranked what-needs-you view, rendered rather than merely available.

    `attention()` existed in `research/dashboard.py` and the script never called it, so the one
    view that decides what an operator looks at first was reachable only from a test. The
    seeded task is BLOCKED, which is a blocker; the registered hypothesis and the stored records
    are routine and must not appear here.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    rows = _rows(page, "panel-attention")
    assert rows, "a blocked task needs the operator"
    body = " ".join(rows)
    assert "T-2026-050" in body or "STUCK" in body or "BLOCKED" in body
    # Routine activity is dropped, not ranked lower.
    assert "Does a 200-day trend filter" not in body


def test_research_volume_is_shown_next_to_forward_evidence_not_instead_of_it(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#15: the juxtaposition is the point, and it only lands when the two are adjacent.

    One registered hypothesis beside three forward days is a very different picture from either
    number alone, and confusing research volume with forward evidence is the specific error the
    project goal document was written to prevent. Two panels apart, the comparison is one a
    reader has to assemble; in one readout it is unavoidable.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    readout = re.search(r"Research vs forward.*?</div>\s*</div>", page, re.S)
    assert readout is not None, "the page has no research-versus-forward readout"
    body = readout.group(0)

    assert "1 registered" in body, body
    assert f"{LADDER_DAYS}/30 days" in body, body
    assert "backtest volume is not forward evidence" in body


def test_an_unconfigured_research_runtime_says_so_rather_than_going_quiet(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#15: "nothing could run" and "nothing was worth running" must not render identically.

    Omitting the readout when no model is configured would make an unconfigured system look
    like an idle one, which is the more flattering of the two readings and the wrong one.
    """
    from quantbot.research.composition import CRITIC, ENDPOINT, GENERATOR

    for name in (ENDPOINT, GENERATOR, CRITIC):
        monkeypatch.delenv(name, raising=False)

    populate(ledger)
    page = render(ledger, monkeypatch)

    assert "Research models" in page
    assert "not configured" in page
    # And it is a statement about configuration, not a health claim nobody verified.
    assert "configuration, not a health check" in page


def test_compute_spend_and_the_overrides_that_exceeded_it_both_render(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#14: an override nobody looks at is a cap that quietly stopped applying.

    Both halves matter and they are separate facts. The compute panel says what work cost; the
    override panel says where a cap stopped constraining it. A dashboard showing only the first
    would report a budget that was never exceeded, because the row that exceeded it looks
    exactly like the rows that did not.
    """
    from quantbot.research.compute import record_sandbox_run
    from quantbot.sandbox.runner import SandboxResult

    populate(ledger)
    database = Database(ledger / "quantbot.db")
    with database.transaction() as session:
        book = BudgetGovernor(
            session,
            caps=[
                Cap(
                    budget_key="compute:research",
                    resource=Resource.WALL_SECONDS,
                    limit=Decimal("10"),
                )
            ],
        )
        # Well inside the cap.
        record_sandbox_run(
            book,
            SandboxResult(
                ok=True, exit_code=0, stdout="", stderr="", duration_seconds=4.0,
                peak_memory_mb=64.0,
            ),
            budget_key="compute:research",
            task="experiment-a",
            now=NOW,
        )
        # And one that blew straight through it.
        record_sandbox_run(
            book,
            SandboxResult(
                ok=False, exit_code=None, stdout="", stderr="", duration_seconds=90.0,
                peak_memory_mb=900.0, terminated_reason="wall-clock",
            ),
            budget_key="compute:research",
            task="experiment-b",
            now=NOW,
        )
    database.close()

    page = render(ledger, monkeypatch)

    compute = [row for row in _rows(page, "panel-compute") if "compute:research" in row]
    assert len(compute) == 1, "one row per budget key"
    assert "94.0" in compute[0], "both runs charged, including the one that was killed"
    assert ">2<" in compute[0].replace(" ", ""), compute[0]

    overrides = [row for row in _rows(page, "panel-overrides") if "WALL_SECONDS" in row]
    assert overrides, "the overrun has to be visible somewhere"
    assert all("compute-accounting" in row for row in overrides)


def test_the_research_pipeline_panel_shows_every_stage_including_empty_ones(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The funnel is the one panel whose zeros are the message.

    "Nothing is running" is a fact an operator needs. A funnel that omits its empty stages
    silently redraws itself every time the system changes shape, which is precisely what a
    funnel exists to make obvious.
    """
    from quantbot.research.director import TaskState

    populate(ledger)
    page = render(ledger, monkeypatch)

    rows = _rows(page, "panel-pipeline")
    assert len(rows) == len(list(TaskState)), "every stage, not only the occupied ones"

    body = " ".join(rows)
    for state in TaskState:
        assert state.value in body, state.value

    # The seeded task is BLOCKED, so that row carries the count and the others are zero.
    blocked = next(row for row in rows if "BLOCKED" in row)
    assert ">1<" in blocked.replace(" ", ""), blocked


def test_every_pipeline_stage_carries_an_explanation(ledger: Path) -> None:
    """A new stage must not reach the page unexplained.

    UNDERPOWERED sitting beside REFUTED with no gloss is how "the data could not resolve this"
    gets read as "this does not work", and that misreading permanently retires a live idea.
    """
    from quantbot.research.director import TaskState

    dashboard = _dashboard()
    for state in TaskState:
        meaning = dashboard._stage_meaning(state.value)
        assert meaning and meaning != state.value.lower(), (
            f"{state.value} has no explanation and would render as its own name"
        )

    # And the distinction that matters is actually drawn.
    assert "not a refutation" in dashboard._stage_meaning("UNDERPOWERED")


def test_the_kill_switch_readout_carries_the_reason_not_only_the_state(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"ENGAGED" alone sends an operator to the journal.

    The reason usually tells them whether it is a data problem, a reconciliation problem or a
    deliberate stop — which is the difference between acting now and reading logs first. The
    live Pi has been halted on RECONCILIATION_FAILED for days; that string is the whole message.
    """
    from quantbot.operations.kill_switch import KillSwitchController

    populate(ledger)
    database = Database(ledger / "quantbot.db")
    KillSwitchController(database).engage(reason="RECONCILIATION_FAILED", updated_at=NOW)
    database.close()

    page = render(ledger, monkeypatch)

    assert "ENGAGED" in page
    assert "RECONCILIATION_FAILED" in page


def test_the_control_link_appears_only_when_a_control_surface_is_configured(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link to a control surface that is not running is worse than no link.

    It sends somebody to a dead page during the minutes they are trying to stop trading, so the
    URL is read from the environment rather than assumed from the dashboard's own host.
    """
    populate(ledger)

    monkeypatch.delenv("QUANTBOT_CONTROL_URL", raising=False)
    assert "control</a>" not in render(ledger, monkeypatch)

    monkeypatch.setenv("QUANTBOT_CONTROL_URL", "http://192.168.1.118:8081/")
    with_link = render(ledger, monkeypatch)
    assert 'href="http://192.168.1.118:8081/"' in with_link
    assert "control</a>" in with_link


def test_the_page_declares_its_encoding_and_a_doctype(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both load-bearing, both found by reading the deployed bytes rather than a screenshot.

    The page carries UTF-8 -- em-dashes for missing values, middot separators -- and
    `python -m http.server` sends `text/html` with no charset parameter. Without a declaration a
    browser falls back to a locale default and renders them as mojibake, which is how a "—" in
    the "last moved" column became a replacement character on the live Pi.

    Without the doctype the page renders in quirks mode, which changes box sizing under the
    panel layout.
    """
    populate(ledger)
    page = render(ledger, monkeypatch)

    assert page.lstrip().lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in page.lower()
    # And the page really does contain the non-ASCII this protects.
    assert any(ord(ch) > 127 for ch in page), "nothing here needed the declaration"
