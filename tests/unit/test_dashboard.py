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
from quantbot.storage import Database

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
    database.close()


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
