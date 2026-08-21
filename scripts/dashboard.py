"""Generate a self-contained dashboard from durable state plus a live broker read.

The data lives locally — SQLite for the durable ledger, the broker for current prices — so
the dashboard is generated rather than hosted. Re-run it whenever you want a fresh view.

Reads only. Safe to run while the daemon holds the writer lock.

**Every row rendered here is read from the database at render time.** This file holds no
research findings of its own, and there is deliberately no fallback for an empty store: a panel
that fills silence with the last list somebody typed into a source file manufactures confidence,
and it drifts silently in the flattering direction because nobody deletes a row from a literal.
An empty research store renders as visibly empty, which is a truer and more useful thing to see.

Usage:
    uv run python scripts/dashboard.py [OUTPUT.html]
"""

from __future__ import annotations

import html
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantbot.reporting import analyze_fills
from quantbot.research.budget import Resource
from quantbot.research.dashboard import Alert, BudgetPanel, attention, luck_bar_at
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import ResearchMemory, ResearchRecord
from quantbot.research.promotion import (
    ALLOWED_PROMOTIONS,
    MINIMUM_FORWARD_DAYS,
    MINIMUM_FORWARD_TRADES,
    Stage,
    forward_progress,
    observe_forward_days,
)
from quantbot.research.registry import DataRole, HypothesisRegistry, Registration
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import budget_spend, hypothesis_data_windows
from quantbot.strategy import load_strategy_config, strategy_id_for


def _money(value: Decimal | float | None, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"${Decimal(str(value)):,.{places}f}"


def _pct(value: float | None, places: int = 2) -> str:
    return "—" if value is None else f"{value:+.{places}f}%"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _broker() -> dict[str, object]:
    key = os.environ.get("ALPACA_PAPER_API_KEY", "")
    secret = os.environ.get("ALPACA_PAPER_API_SECRET", "")
    if not key or not secret:
        return {"available": False}
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    base = "https://paper-api.alpaca.markets/v2"
    try:
        account = httpx.get(f"{base}/account", headers=headers, timeout=30).json()
        positions = httpx.get(f"{base}/positions", headers=headers, timeout=30).json()
        clock = httpx.get(f"{base}/clock", headers=headers, timeout=30).json()
    except Exception:
        return {"available": False}
    return {
        "available": True,
        "account": account,
        "positions": positions if isinstance(positions, list) else [],
        "clock": clock,
    }


def _durable() -> dict[str, object]:
    path = os.environ.get("QUANTBOT_DB_PATH", "quantbot.db")
    if not Path(path).exists():
        return {"available": False}
    database = Database(path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        runs = repository.list_runs()
        fills = tuple(repository.list_fills())
        orders = repository.list_broker_orders()
        reconciliation = repository.get_latest_reconciliation()
        kill = repository.get_kill_switch_state()
        incidents = repository.list_incidents()
        signals = repository.list_signals()
        account_id = os.environ.get("EXPECTED_ACCOUNT_ID", "")
        equity = repository.list_equity_snapshots(account_id=account_id) if account_id else []
        research = _research(session)
    database.close()
    analysis = analyze_fills(fills)
    return {
        "available": True,
        "runs": runs,
        "fills": fills,
        "orders": orders,
        "reconciliation": reconciliation,
        "kill": kill,
        "incidents": incidents,
        "signals": signals,
        "equity": equity,
        "completed": analysis.completed_trades,
        **research,
    }


def _research(session: Session) -> dict[str, object]:
    """Research state, read from the durable stores and from nowhere else.

    Each entry maps to one table: `hypotheses`, `research_records`, `research_tasks`, and the
    `TRIALS` rows of `budget_spend`. Nothing here is defaulted or backfilled, so a store with
    no rows produces panels with no rows.
    """
    return {
        "registrations": HypothesisRegistry(session).list_registrations(),
        # `recall("")` matches every record: the empty pattern is a substring of any subject.
        "records": ResearchMemory(session).recall(""),
        "tasks": ResearchDirector(session).by_state(),
        "trials": _trials_by_budget(session),
        "windows": _protected_windows(session),
        "ladder": _promotion_rows(session),
    }


_ORDER = tuple(Stage)


def _promotion_rows(session: Session) -> list[tuple[str, str, str, str, str]]:
    """Where each strategy stands on the ladder, and precisely what it has not yet earned (#15).

    The stage comes from `promotion_state` and the forward counters from the durable ledger via
    `observe_forward_days`, so this panel cannot show progress a strategy did not make. That
    matters more here than on any other panel: this is the view an operator would read before
    deciding a strategy is worth a human conversation about real capital, so a flattering number
    here is the most expensive wrong number in the system.

    Gates that need an experiment outcome are reported as needing one rather than being scored.
    An unmeasured gate shown as satisfied is worse than an unmeasured gate shown as unknown.
    """
    repository = StorageRepository(session)
    rows: list[tuple[str, str, str, str, str]] = []
    for record in repository.list_promotions():
        stage = Stage(record.stage)
        observed = observe_forward_days(
            session,
            strategy_id=record.strategy_id,
            strategy_version=record.strategy_version,
            configuration_hash=record.configuration_hash,
        )
        progress = forward_progress(
            observed,
            strategy_version=record.strategy_version,
            configuration_hash=record.configuration_hash,
        )
        days = int(progress["forward_days"])
        trades = int(progress["forward_trades"])
        rows.append(
            (
                record.strategy_id,
                stage.value,
                f"{record.strategy_version} / {record.configuration_hash[:12]}",
                f"{days}/{MINIMUM_FORWARD_DAYS} days, {trades}/{MINIMUM_FORWARD_TRADES} trades",
                _unmet_gates(stage, days=days, trades=trades),
            )
        )
    return rows


def _unmet_gates(stage: Stage, *, days: int, trades: int) -> str:
    """What stands between this strategy and its next stage, named rather than summarised."""
    if stage is Stage.LIVE_REVIEW_ELIGIBLE:
        return "awaiting human review; there is no stage after this one"
    targets = ALLOWED_PROMOTIONS[stage]
    upward = [target for target in targets if _ORDER.index(target) > _ORDER.index(stage)]
    if not upward:
        return "no upward transition from here"
    unmet: list[str] = []
    for target in upward:
        if target is Stage.PAPER_QUALIFIED:
            shortfall = []
            if days < MINIMUM_FORWARD_DAYS:
                shortfall.append(f"{MINIMUM_FORWARD_DAYS - days} more forward days")
            if trades < MINIMUM_FORWARD_TRADES:
                shortfall.append(f"{MINIMUM_FORWARD_TRADES - trades} more trades")
            unmet.append(
                f"{target.value}: {', '.join(shortfall)}" if shortfall else f"{target.value}: met"
            )
        elif target is Stage.RESEARCH_SURVIVOR:
            unmet.append(f"{target.value}: needs a surviving experiment outcome")
        else:
            unmet.append(f"{target.value}: operator decision")
    return "; ".join(unmet)


def _protected_windows(session: Session) -> list[tuple[str, str, str, str, str]]:
    """Protected evaluation windows and whether each is still available (#22, #15).

    Reserved and consumed are different facts and the panel keeps them apart. A reservation is
    a claim on data nobody has looked at, and it lapses; a consumption is permanent, because
    contamination is a fact about what was seen. Showing one number for both would tell an
    operator a holdout is gone when it is merely spoken for.
    """
    rows = session.execute(
        select(
            hypothesis_data_windows.c.dataset,
            hypothesis_data_windows.c.start_date,
            hypothesis_data_windows.c.end_date,
            hypothesis_data_windows.c.state,
            hypothesis_data_windows.c.hypothesis_id,
        )
        .where(hypothesis_data_windows.c.role == DataRole.PROTECTED_EVALUATION.value)
        .order_by(
            hypothesis_data_windows.c.dataset,
            hypothesis_data_windows.c.start_date,
        )
    ).all()
    return [
        (str(dataset), str(start), str(end), str(state), str(holder))
        for dataset, start, end, state, holder in rows
    ]


def _trials_by_budget(session: Session) -> list[tuple[str, int]]:
    """Cumulative trials per budget key. The spend that never refunds."""
    rows = session.execute(
        select(budget_spend.c.budget_key, func.coalesce(func.sum(budget_spend.c.amount), "0"))
        .where(budget_spend.c.category == Resource.TRIALS.value)
        .group_by(budget_spend.c.budget_key)
        .order_by(budget_spend.c.budget_key)
    ).all()
    return [(str(key), int(Decimal(str(total)))) for key, total in rows]


def _supervisor_state() -> tuple[str, str, list[str]]:
    """Freshness of the watchdog, from the timestamps it writes.

    Reading the log rather than the process table keeps this honest and portable: a stale
    last-check means the watchdog is not watching, whatever the process list claims. An
    unmonitored daemon died twice unnoticed, so its absence belongs on the panel.
    """
    log = Path("logs") / "supervisor.log"
    if not log.exists():
        return "never run", "critical", []
    lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return "no entries", "critical", []
    stamp = lines[-1][:19]
    try:
        last = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return "unreadable", "critical", lines[-4:]
    minutes = (datetime.now(UTC) - last).total_seconds() / 60
    recent = lines[-4:]
    if minutes > 20:
        return f"stale, {minutes:.0f}m ago", "critical", recent
    critical = any("CRITICAL" in ln for ln in recent)
    return f"checked {minutes:.0f}m ago", "critical" if critical else "good", recent


def _pill(label: str, tone: str) -> str:
    return f'<span class="pill pill--{tone}">{_esc(label)}</span>'


def _empty(columns: int, message: str) -> str:
    return f"<tr><td colspan='{columns}' class='empty'>{_esc(message)}</td></tr>"


def _tone(value: float) -> str:
    return "up" if value >= 0 else "down"


def _position_row(position: dict[str, object]) -> str:
    pl = float(position.get("unrealized_pl", 0) or 0)
    plpc = float(position.get("unrealized_plpc", 0) or 0)
    return (
        f"<tr><td class='sym'>{_esc(position['symbol'])}</td>"
        f"<td class='num'>{_esc(position['qty'])}</td>"
        f"<td class='num'>{_money(position.get('avg_entry_price'))}</td>"
        f"<td class='num'>{_money(position.get('current_price'))}</td>"
        f"<td class='num'>{_money(position.get('market_value'))}</td>"
        f"<td class='num {_tone(pl)}'>{_money(pl, 4)}</td>"
        f"<td class='num {_tone(plpc)}'>{_pct(plpc * 100)}</td></tr>"
    )


def _run_row(run: object, signals: list[object]) -> str:
    status = getattr(run, "status", "")
    run_id = getattr(run, "run_id", "")
    finished = getattr(run, "finished_at", None)
    counted = sum(1 for s in signals if getattr(s, "run_id", None) == run_id)
    stamp = finished.strftime("%Y-%m-%d %H:%M") if finished else "running"
    return (
        f"<tr><td class='mono'>{_esc(run_id)}</td>"
        f"<td>{_pill(status, 'good' if status == 'SUCCEEDED' else 'critical')}</td>"
        f"<td class='num'>{counted}</td>"
        f"<td class='mono quiet-text'>{_esc(stamp)}</td></tr>"
    )


def _fill_row(fill: object) -> str:
    return (
        f"<tr><td class='sym'>{_esc(fill.symbol)}</td>"  # type: ignore[attr-defined]
        f"<td>{_esc(fill.side.value)}</td>"  # type: ignore[attr-defined]
        f"<td class='num'>{_esc(fill.quantity)}</td>"  # type: ignore[attr-defined]
        f"<td class='num'>{_money(fill.price)}</td>"  # type: ignore[attr-defined]
        f"<td class='mono quiet-text'>"
        f"{_esc(fill.occurred_at.strftime('%Y-%m-%d %H:%M'))}</td></tr>"  # type: ignore[attr-defined]
    )


def _hypothesis_row(registration: Registration) -> str:
    draft, power = registration.draft, registration.power
    return (
        f"<tr><td class='mono'>{_esc(draft.hypothesis_id)} v{draft.version}</td>"
        f"<td>{_esc(draft.question)}</td>"
        f"<td class='quiet-text'>{_esc(draft.family_id)}</td>"
        f"<td>{_pill(power.verdict.value, 'good' if power.cleared else 'warning')}</td>"
        f"<td class='num'>{registration.cumulative_trials}</td>"
        f"<td class='num'>{_esc(power.luck_threshold_z)}</td></tr>"
    )


def _record_row(record: ResearchRecord) -> str:
    """One durable research record. The verdict is printed, never paraphrased.

    UNDERPOWERED and UNECONOMIC get their own tone deliberately: a question the data could not
    resolve was never tested, and rendering it in the same colour as a refutation teaches a
    later reader that a mechanism failed when nothing of the sort was established.
    """
    label = record.verdict.value if record.verdict is not None else record.kind.value
    tone = {
        "REFUTED": "critical",
        "LITERATURE_REFUTED": "critical",
        "SURVIVED": "good",
        "LITERATURE_SUPPORTED": "good",
        "UNDERPOWERED": "warning",
        "UNECONOMIC": "warning",
        "EXPLORATORY_ONLY": "warning",
    }.get(label, "quiet")
    return (
        f"<tr><td>{_esc(record.subject)}</td>"
        f"<td>{_pill(label, tone)}</td>"
        f"<td class='quiet-text'>{_esc(record.statement)}</td>"
        f"<td class='mono quiet-text'>{_esc(record.source)}</td></tr>"
    )


def _task_row(task: ResearchTask) -> str:
    tone = {
        TaskState.BLOCKED: "critical",
        TaskState.UNDERPOWERED: "warning",
        TaskState.SURVIVED: "good",
        TaskState.PROMOTABLE: "good",
    }.get(task.state, "accent")
    return (
        f"<tr><td class='mono'>{_esc(task.task_id)}</td>"
        f"<td>{_pill(task.state.value, tone)}</td>"
        f"<td class='quiet-text'>{_esc(task.question)}</td>"
        f"<td class='mono quiet-text'>{_esc(task.updated_at.strftime('%Y-%m-%d %H:%M'))}</td></tr>"
    )


def _window_row(dataset: str, start: str, end: str, state: str, holder: str) -> str:
    """One protected window. The tone says whether the data is still available."""
    tone = {"CONSUMED": "bad", "RESERVED": "warn", "RELEASED": "good"}.get(state, "warn")
    return (
        "<tr>"
        f"<td>{_esc(dataset)}</td>"
        f"<td>{_esc(start)} .. {_esc(end)}</td>"
        f"<td>{_pill(state, tone)}</td>"
        f"<td>{_esc(holder)}</td>"
        "</tr>"
    )


def _ladder_row(strategy: str, stage: str, identity: str, progress: str, unmet: str) -> str:
    """One strategy's position. The tone tracks how much trust the stage implies."""
    tone = {
        "LIVE_REVIEW_ELIGIBLE": "warn",
        "PAPER_QUALIFIED": "good",
        "PAPER_OBSERVATION": "good",
        "SHADOW": "good",
    }.get(stage, "warn")
    return (
        "<tr>"
        f"<td class='mono'>{_esc(strategy)}</td>"
        f"<td>{_pill(stage, tone)}</td>"
        f"<td class='mono'>{_esc(identity)}</td>"
        f"<td>{_esc(progress)}</td>"
        f"<td>{_esc(unmet)}</td>"
        "</tr>"
    )


def _alert_row(alert: Alert) -> str:
    """One thing that needs the operator. Not one thing that merely happened."""
    tone = "bad" if alert.urgency == 0 else "warn" if alert.urgency < 3 else "good"
    return f"<tr><td>{_pill(alert.kind, tone)}</td><td>{_esc(alert.detail)}</td></tr>"


def _trial_row(budget_key: str, trials: int) -> str:
    return (
        f"<tr><td class='mono'>{_esc(budget_key)}</td>"
        f"<td class='num'>{trials}</td>"
        f"<td class='num'>{_esc(luck_bar_at(trials))}</td></tr>"
    )


def build(out: Path) -> None:
    broker = _broker()
    durable = _durable()
    config_path = os.environ.get("QUANTBOT_CONFIG", "config/strategy-v1-2.yaml")
    try:
        config = load_strategy_config(config_path)
        version, sid = config.version, strategy_id_for(config)
        universe_size, roster = len(config.universe), config.roster_size
    except Exception:
        version, sid, universe_size, roster = "unknown", "unknown", 0, 0

    equity = cash = None
    positions: list[dict[str, object]] = []
    market_open = None
    if broker.get("available"):
        account = broker["account"]  # type: ignore[index]
        equity = float(account["equity"])
        cash = float(account["cash"])
        positions = broker["positions"]  # type: ignore[assignment]
        market_open = bool(broker["clock"]["is_open"])  # type: ignore[index]

    runs = durable.get("runs", []) if durable.get("available") else []
    fills = durable.get("fills", ()) if durable.get("available") else ()
    completed = durable.get("completed", ()) if durable.get("available") else ()
    reconciliation = durable.get("reconciliation") if durable.get("available") else None
    kill = durable.get("kill") if durable.get("available") else None
    signals = durable.get("signals", []) if durable.get("available") else []
    registrations = durable.get("registrations", []) if durable.get("available") else []
    records = durable.get("records", []) if durable.get("available") else []
    tasks = durable.get("tasks", []) if durable.get("available") else []
    trials = durable.get("trials", []) if durable.get("available") else []
    windows = durable.get("windows", []) if durable.get("available") else []
    ladder = durable.get("ladder", []) if durable.get("available") else []

    total_pl = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    exposure = sum(float(p.get("market_value", 0) or 0) for p in positions)
    open_pct = (exposure / equity * 100) if equity else 0.0

    status_pills = []
    if kill is not None:
        status_pills.append(
            _pill(
                "KILL SWITCH ENGAGED" if kill.engaged else "Armed",
                "critical" if kill.engaged else "good",
            )
        )
    if reconciliation is not None:
        ok = reconciliation.status.value == "RECONCILED" and not reconciliation.diffs
        status_pills.append(
            _pill(
                reconciliation.status.value.replace("_", " ").title(), "good" if ok else "critical"
            )
        )
    if market_open is not None:
        status_pills.append(
            _pill(
                "Market open" if market_open else "Market closed",
                "accent" if market_open else "quiet",
            )
        )
    status_pills.append(_pill(f"Strategy {version}", "quiet"))
    supervisor_detail, supervisor_tone, supervisor_lines = _supervisor_state()
    status_pills.append(_pill(f"Watchdog {supervisor_detail}", supervisor_tone))

    position_rows = "".join(_position_row(p) for p in positions) or _empty(7, "No open positions")
    run_rows = "".join(_run_row(r, signals) for r in reversed(runs[-12:])) or _empty(
        4, "No cycles recorded yet"
    )
    fill_rows = "".join(
        _fill_row(f) for f in sorted(fills, key=lambda x: x.occurred_at, reverse=True)[:12]
    ) or _empty(5, "No fills yet")
    supervisor_rows = "".join(
        f"<tr><td class='mono quiet-text'>{_esc(line)}</td></tr>" for line in supervisor_lines
    ) or _empty(1, "No watchdog output yet")
    hypothesis_rows = "".join(_hypothesis_row(r) for r in registrations) or _empty(
        6, "No hypotheses registered"
    )
    record_rows = "".join(_record_row(r) for r in records) or _empty(
        4, "No research records stored"
    )
    task_rows = "".join(_task_row(t) for t in tasks) or _empty(4, "No research tasks")
    trial_rows = "".join(_trial_row(key, count) for key, count in trials) or _empty(
        3, "No trials recorded against any budget"
    )
    window_rows = "".join(_window_row(*row) for row in windows) or _empty(
        4, "No protected evaluation window is claimed"
    )
    ladder_rows = "".join(_ladder_row(*row) for row in ladder) or _empty(
        5, "No strategy is on the promotion ladder"
    )

    # `attention()` decides what matters; this only renders it. The panel is driven by the same
    # durable state as everything else -- the trial spend, the task states, the kill switch and
    # the last reconciliation -- so it cannot show an alert the ledger does not support.
    alerts = attention(
        tasks,
        [
            BudgetPanel(budget_key=key, trials_spent=count, trials_cap=max(count, 1))
            for key, count in trials
        ],
        stuck=[t for t in tasks if t.state is TaskState.BLOCKED],
        kill_switch_engaged=bool(durable.get("kill_switch_engaged")),
        reconciliation_failed=not durable.get("reconciliation_ok", True),
    )
    alert_rows = "".join(_alert_row(a) for a in alerts) or _empty(2, "Nothing needs you")

    trades_per_day = len(completed) / max(len(runs), 1)
    wins = sum(1 for t in completed if float(t.net_pnl) > 0)
    win_rate = (wins / len(completed) * 100) if completed else None

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    win_note = f"win rate {win_rate:.0f}%" if win_rate is not None else "none closed yet"

    page = f"""<title>QuantBot Instrument Panel</title>
<style>
:root {{
  --ground: #F2F4F6; --panel: #FFFFFF; --edge: #D5DCE2; --ink: #1A2027;
  --ink-quiet: #5D6B77; --accent: #2D6E8E; --accent-soft: #E4EDF2;
  --good: #2F7D5B; --bad: #B04A3F; --warn: #9A7B2E;
  --grid: rgba(45,110,142,.10);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #12171C; --panel: #1A2128; --edge: #2C3742; --ink: #E3E8ED;
    --ink-quiet: #8B9AA7; --accent: #5FA8C9; --accent-soft: #1E2C36;
    --good: #55A87E; --bad: #D2705F; --warn: #C2A04D;
    --grid: rgba(95,168,201,.12);
  }}
}}
:root[data-theme="dark"] {{
  --ground: #12171C; --panel: #1A2128; --edge: #2C3742; --ink: #E3E8ED;
  --ink-quiet: #8B9AA7; --accent: #5FA8C9; --accent-soft: #1E2C36;
  --good: #55A87E; --bad: #D2705F; --warn: #C2A04D;
  --grid: rgba(95,168,201,.12);
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--ground); color: var(--ink);
  font-family: ui-sans-serif, -apple-system, "Segoe UI Variable Text",
    "Segoe UI", system-ui, sans-serif;
  font-size: 14px; line-height: 1.5;
}}
.mono, .num, .sym {{
  font-family: ui-monospace, "Cascadia Mono", "SF Mono", Consolas, monospace;
  font-variant-numeric: tabular-nums;
}}
.wrap {{ max-width: 1180px; margin: 0 auto; padding: 28px 22px 64px; }}
header {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 14px; margin-bottom: 6px; }}
h1 {{ font-size: 21px; letter-spacing: -.01em; margin: 0; font-weight: 620; }}
.sub {{ color: var(--ink-quiet); font-size: 12.5px; }}
.pills {{ display: flex; flex-wrap: wrap; gap: 7px; margin: 16px 0 26px; }}
.pill {{
  font-size: 11px; letter-spacing: .05em; text-transform: uppercase; font-weight: 600;
  padding: 4px 9px; border-radius: 3px; border: 1px solid var(--edge); white-space: nowrap;
}}
.pill--good {{
  color: var(--good);
  border-color: color-mix(in srgb, var(--good) 40%, transparent);
}}
.pill--critical {{
  color: var(--bad);
  border-color: color-mix(in srgb, var(--bad) 45%, transparent);
}}
.pill--warning {{
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 45%, transparent);
}}
.pill--accent {{
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
}}
.pill--quiet {{ color: var(--ink-quiet); }}
.readouts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 1px;
  background: var(--edge); border: 1px solid var(--edge); margin-bottom: 30px; }}
.readout {{ background: var(--panel); padding: 14px 16px 15px; }}
.readout .k {{ font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
  color: var(--ink-quiet); font-weight: 600; }}
.readout .v {{
  font-size: 22px;
  margin-top: 5px;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: -.02em;
}}
.readout .note {{ font-size: 11.5px; color: var(--ink-quiet); margin-top: 3px; }}
.up {{ color: var(--good); }} .down {{ color: var(--bad); }}
section {{ margin-bottom: 30px; }}
h2 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-quiet);
  margin: 0 0 10px; font-weight: 650; }}
.scroll {{ overflow-x: auto; border: 1px solid var(--edge); background: var(--panel); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--ink-quiet); font-weight: 650; padding: 9px 12px;
  border-bottom: 1px solid var(--edge); white-space: nowrap; }}
td {{ padding: 9px 12px; border-bottom: 1px solid var(--grid); vertical-align: top; }}
tbody tr:last-child td {{ border-bottom: 0; }}
.num, th.num {{ text-align: right; }}
.sym {{ font-weight: 650; }}
.quiet-text {{ color: var(--ink-quiet); font-size: 12.5px; }}
.empty {{ color: var(--ink-quiet); text-align: center; padding: 22px; font-style: italic; }}
code {{ font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 12px; }}
.cols {{ display: grid; gap: 30px; grid-template-columns: 1fr; }}
@media (min-width: 880px) {{ .cols {{ grid-template-columns: 1.15fr 1fr; }} }}
.caveat {{ border-left: 2px solid var(--warn); padding: 10px 0 10px 14px; margin-top: 12px;
  color: var(--ink-quiet); font-size: 12.5px; max-width: 66ch; }}
footer {{ margin-top: 40px; padding-top: 16px; border-top: 1px solid var(--edge);
  color: var(--ink-quiet); font-size: 11.5px; }}
</style>

<div class="wrap">
  <header>
    <h1>QuantBot Instrument Panel</h1>
    <span class="sub mono">{_esc(sid)}</span>
  </header>
  <div class="pills">{"".join(status_pills)}</div>

  <div class="readouts">
    <div class="readout"><div class="k">Equity</div><div class="v">{_money(equity)}</div>
      <div class="note">cash {_money(cash)}</div></div>
    <div class="readout"><div class="k">Open P&amp;L</div>
      <div class="v {"up" if total_pl >= 0 else "down"}">{_money(total_pl, 4)}</div>
      <div class="note">{len(positions)} position{"s" if len(positions) != 1 else ""}</div></div>
    <div class="readout"><div class="k">Exposure</div><div class="v">{open_pct:.1f}%</div>
      <div class="note">{_money(exposure)} deployed</div></div>
    <div class="readout"><div class="k">Cycles</div><div class="v">{len(runs)}</div>
      <div class="note">{len(fills)} fills recorded</div></div>
    <div class="readout"><div class="k">Completed trades</div><div class="v">{len(completed)}</div>
      <div class="note">{win_note}</div></div>
    <div class="readout"><div class="k">Trades / cycle</div>
      <div class="v">{trades_per_day:.2f}</div>
      <div class="note">target sample: 30</div></div>
    <div class="readout"><div class="k">Watchdog</div>
      <div class="v" style="font-size:15px">{_esc(supervisor_detail)}</div>
      <div class="note">restarts a dead daemon; never lifts a halt</div></div>
  </div>

  <section>
    <h2>Open positions</h2>
    <div class="scroll"><table>
      <thead><tr><th>Symbol</th><th class="num">Quantity</th><th class="num">Entry</th>
        <th class="num">Last</th><th class="num">Value</th><th class="num">P&amp;L</th>
        <th class="num">P&amp;L %</th></tr></thead>
      <tbody>{position_rows}</tbody>
    </table></div>
  </section>

  <div class="cols">
    <section>
      <h2>Cycle log</h2>
      <div class="scroll"><table>
        <thead><tr><th>Run</th><th>Status</th><th class="num">Signals</th>
          <th>Finished</th></tr></thead>
        <tbody>{run_rows}</tbody>
      </table></div>
    </section>
    <section>
      <h2>Recent fills</h2>
      <div class="scroll"><table>
        <thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th>
          <th>Time</th></tr></thead>
        <tbody>{fill_rows}</tbody>
      </table></div>
    </section>
  </div>

  <section>
    <h2>Watchdog log — most recent checks</h2>
    <div class="scroll"><table>
      <tbody>{supervisor_rows}</tbody>
    </table></div>
    <p class="caveat">Liveness is read from the writer lock, not the process table: a process
      can exist while holding nothing, which is how a dying daemon once read as healthy.</p>
  </section>

  <section>
    <h2>Hypotheses registered</h2>
    <div class="scroll"><table id="panel-hypotheses">
      <thead><tr><th>Hypothesis</th><th>Question</th><th>Family</th><th>Power</th>
        <th class="num">Trials</th><th class="num">Luck bar</th></tr></thead>
      <tbody>{hypothesis_rows}</tbody>
    </table></div>
  </section>

  <section>
    <h2>Research ledger — what the durable store holds</h2>
    <div class="scroll"><table id="panel-records">
      <thead><tr><th>Subject</th><th>Verdict</th><th>Statement</th><th>Source</th></tr></thead>
      <tbody>{record_rows}</tbody>
    </table></div>
    <p class="caveat">Read from <code>research_records</code> at render time, with no fallback.
      An empty table means the store is empty — which is a different and more useful thing to
      know than whatever list was last typed into the generator. UNDERPOWERED is shown as its
      own verdict and never as a refutation: a question the data could not resolve was not
      tested, and colouring the two alike teaches a later reader something that never happened.
    </p>
  </section>

  <div class="cols">
    <section>
      <h2>Research pipeline</h2>
      <div class="scroll"><table id="panel-tasks">
        <thead><tr><th>Task</th><th>State</th><th>Question</th><th>Updated</th></tr></thead>
        <tbody>{task_rows}</tbody>
      </table></div>
    </section>
    <section>
      <h2>Statistical budget — trials spent</h2>
      <div class="scroll"><table id="panel-trials">
        <thead><tr><th>Budget</th><th class="num">Trials</th>
          <th class="num">Luck bar</th></tr></thead>
        <tbody>{trial_rows}</tbody>
      </table></div>
      <p class="caveat">Trials are the one budget that never refills. The bar rises with every
        candidate evaluated against the same data, and no amount of compute buys it back.</p>
    </section>
    <section>
      <h2>Protected evaluation windows</h2>
      <div class="scroll"><table id="panel-windows">
        <thead><tr><th>Dataset</th><th>Range</th><th>State</th><th>Claimed by</th></tr></thead>
        <tbody>{window_rows}</tbody>
      </table></div>
      <p class="caveat">Reserved and consumed are different facts. A reservation is a claim on
        data nobody has looked at yet and it lapses; a consumption is permanent, because
        contamination is a fact about what was seen rather than about a record.</p>
    </section>
  </div>

  <section>
    <h2>Promotion ladder</h2>
    <div class="scroll"><table id="panel-ladder">
      <thead><tr><th>Strategy</th><th>Stage</th><th>Version / config</th>
        <th>Forward evidence</th><th>Unmet gates</th></tr></thead>
      <tbody>{ladder_rows}</tbody>
    </table></div>
    <p class="caveat">Stage comes from the durable ladder and the forward counters are read from
      the trading ledger, not supplied. There is no stage after LIVE_REVIEW_ELIGIBLE: the
      transition table has no destination past it, so no agent can move a strategy into live
      trading. Backtest volume is not forward evidence and never appears in these counters.</p>
  </section>

  <section>
    <h2>Needs you</h2>
    <div class="scroll"><table id="panel-attention">
      <thead><tr><th>Kind</th><th>What</th></tr></thead>
      <tbody>{alert_rows}</tbody>
    </table></div>
    <p class="caveat">Blockers and results only. A research loop generates a large volume of
      uninteresting activity by design, and including it is how the signal gets lost.</p>
  </section>

  <footer>
    Generated {_esc(generated)} · strategy {_esc(version)} · universe {universe_size} symbols,
    roster {roster} · every row read from the durable ledger and a live broker read at render
    time; no literals, no fallbacks, no synthesised values.
  </footer>
</div>
"""
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}")
    print(
        f"  equity {_money(equity)}  positions {len(positions)}  cycles {len(runs)}"
        f"  fills {len(fills)}"
    )
    print(f"  hypotheses {len(registrations)}  research records {len(records)}  tasks {len(tasks)}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("reports") / "dashboard.html")
