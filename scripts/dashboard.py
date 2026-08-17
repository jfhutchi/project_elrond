"""Generate a self-contained dashboard from durable state plus a live broker read.

The data lives locally — SQLite for the durable ledger, the broker for current prices — so
the dashboard is generated rather than hosted. Re-run it whenever you want a fresh view.

Reads only. Safe to run while the daemon holds the writer lock.

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

from quantbot.reporting import analyze_fills
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config, strategy_id_for

# Findings from cycles 1-9, recorded in REFUTED.md. Sequence is real information here:
# each cycle was designed after reading the previous one's result.
RESEARCH: tuple[tuple[str, str, str, str], ...] = (
    (
        "1",
        "Momentum parameter tuning",
        "refuted",
        "31 candidates; winner's edge existed only at momentum_long=252",
    ),
    (
        "2",
        "Full shipped strategy worth running",
        "refuted",
        "Ranked 6 of 6. CAGR 0.50%, Sharpe 0.15",
    ),
    (
        "3",
        "Signal-family ensembles",
        "refuted",
        "Components correlate 0.75+; no diversification available",
    ),
    (
        "4",
        "Exposure normalisation",
        "refuted",
        "Sharpe is scale-invariant; low drawdown was under-risk",
    ),
    ("5", "Crypto momentum", "refuted", "-79.9%, Sharpe -0.45, 80.6% drawdown"),
    ("5", "BTC trend standalone", "refuted", "Sharpe 0.53, 95% CI [-0.55, 1.61] spans zero"),
    (
        "6",
        "Shorter horizons",
        "refuted",
        "1d reversal: strongest raw edge (+427.9% gross), -28.8% net",
    ),
    ("7", "Options for faster gains", "refuted", "Affordable contracts are 0DTE at 10-18% spreads"),
    (
        "8",
        "Machine learning / meta-labelling",
        "refuted",
        "PF 2.29 looked strong; p=0.085, luck threshold 2.22",
    ),
    (
        "9",
        "Energy predictable via geopolitics",
        "refuted",
        "Trend persistence 21.2d < equities 26.5d",
    ),
    ("9", "FX diversification", "refuted", "Lowest correlation (0.02) and Sharpe -0.14"),
    ("—", "BTC as portfolio diversifier", "refuted", "Blend improvement 0.23 sigma, p~0.82"),
    ("—", "Reddit / social sentiment", "refuted", "Literature: alpha indistinguishable from zero"),
    (
        "—",
        "Coinbase paper trading",
        "impossible",
        "Sandbox is static fixtures; Agents is real money only",
    ),
)

BENCHMARKS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("SPY buy & hold", "$454.70", "15.36%", "0.90", False),
    ("SPY 200-day trend", "$283.50", "10.34%", "0.91", False),
    ("Pure 12-1 momentum", "$282.24", "10.30%", "0.77", False),
    ("Momentum + trend (deployed)", "$232.78", "8.31%", "0.76", True),
    ("Sleeve ensemble", "$120.51", "1.78%", "0.82", False),
    ("Full strategy (retired)", "$105.41", "0.50%", "0.15", False),
)


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
    }


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


def _research_row(cycle: str, name: str, verdict: str, detail: str) -> str:
    tone = "critical" if verdict == "refuted" else "warning"
    return (
        f"<tr><td class='cycle'>{_esc(cycle)}</td><td>{_esc(name)}</td>"
        f"<td>{_pill(verdict, tone)}</td>"
        f"<td class='quiet-text'>{_esc(detail)}</td></tr>"
    )


def _bench_row(name: str, final: str, cagr: str, sharpe: str, live: bool) -> str:
    row_class = "is-live" if live else ""
    tag = "<span class='tag'>deployed</span>" if live else ""
    return (
        f"<tr class='{row_class}'><td>{_esc(name)} {tag}</td>"
        f"<td class='num'>{_esc(final)}</td>"
        f"<td class='num'>{_esc(cagr)}</td>"
        f"<td class='num'>{_esc(sharpe)}</td></tr>"
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

    total_pl = sum(float(p.get("unrealized_pl", 0) or 0) for p in positions)
    exposure = sum(float(p.get("market_value", 0) or 0) for p in positions)
    open_pct = (exposure / equity * 100) if equity else 0.0

    status_pills = []
    if kill is not None:
        status_pills.append(_pill("KILL SWITCH ENGAGED" if kill.engaged else "Armed",
                                  "critical" if kill.engaged else "good"))
    if reconciliation is not None:
        ok = reconciliation.status.value == "RECONCILED" and not reconciliation.diffs
        status_pills.append(_pill(reconciliation.status.value.replace("_", " ").title(),
                                  "good" if ok else "critical"))
    if market_open is not None:
        status_pills.append(_pill("Market open" if market_open else "Market closed",
                                  "accent" if market_open else "quiet"))
    status_pills.append(_pill(f"Strategy {version}", "quiet"))

    position_rows = "".join(_position_row(p) for p in positions) or _empty(7, "No open positions")
    run_rows = "".join(
        _run_row(r, signals) for r in reversed(runs[-12:])
    ) or _empty(4, "No cycles recorded yet")
    fill_rows = "".join(
        _fill_row(f) for f in sorted(fills, key=lambda x: x.occurred_at, reverse=True)[:12]
    ) or _empty(5, "No fills yet")
    research_rows = "".join(_research_row(*row) for row in RESEARCH)
    bench_rows = "".join(_bench_row(*row) for row in BENCHMARKS)

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
.mono, .num, .sym, .cycle {{
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
}}
  font-variant-numeric: tabular-nums; letter-spacing: -.02em; }}
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
.cycle {{ color: var(--accent); font-weight: 650; }}
.quiet-text {{ color: var(--ink-quiet); font-size: 12.5px; }}
.empty {{ color: var(--ink-quiet); text-align: center; padding: 22px; font-style: italic; }}
.is-live {{ background: var(--accent-soft); }}
.tag {{ font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); padding: 1px 5px;
  border-radius: 2px; margin-left: 6px; }}
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
  <div class="pills">{''.join(status_pills)}</div>

  <div class="readouts">
    <div class="readout"><div class="k">Equity</div><div class="v">{_money(equity)}</div>
      <div class="note">cash {_money(cash)}</div></div>
    <div class="readout"><div class="k">Open P&amp;L</div>
      <div class="v {'up' if total_pl >= 0 else 'down'}">{_money(total_pl, 4)}</div>
      <div class="note">{len(positions)} position{'s' if len(positions) != 1 else ''}</div></div>
    <div class="readout"><div class="k">Exposure</div><div class="v">{open_pct:.1f}%</div>
      <div class="note">{_money(exposure)} deployed</div></div>
    <div class="readout"><div class="k">Cycles</div><div class="v">{len(runs)}</div>
      <div class="note">{len(fills)} fills recorded</div></div>
    <div class="readout"><div class="k">Completed trades</div><div class="v">{len(completed)}</div>
      <div class="note">{win_note}</div></div>
    <div class="readout"><div class="k">Trades / cycle</div>
      <div class="v">{trades_per_day:.2f}</div>
      <div class="note">target sample: 30</div></div>
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
    <h2>Measured over 10.6 years — $100 start, costs applied</h2>
    <div class="scroll"><table>
      <thead><tr><th>Configuration</th><th class="num">Final</th><th class="num">CAGR</th>
        <th class="num">Sharpe</th></tr></thead>
      <tbody>{bench_rows}</tbody>
    </table></div>
    <p class="caveat">Nothing built here beat buying and holding the index. The deployed
      configuration is the best of those the live system can express, not a strategy with a
      demonstrated edge — it loses to SPY on risk-adjusted return.</p>
  </section>

  <section>
    <h2>Research ledger — hypotheses tested and killed</h2>
    <div class="scroll"><table>
      <thead><tr><th>Cycle</th><th>Hypothesis</th><th>Verdict</th><th>Evidence</th></tr></thead>
      <tbody>{research_rows}</tbody>
    </table></div>
    <p class="caveat">Every apparent winner evaporated under a significance test. Fourteen
      hypotheses refuted with measurement; ~43 candidate evaluations against one dataset now
      put the luck threshold at t&nbsp;≈&nbsp;2.22, so each new result needs deflating by the
      cumulative count rather than the current cycle's.</p>
  </section>

  <footer>
    Generated {_esc(generated)} · strategy {_esc(version)} · universe {universe_size} symbols,
    roster {roster} · durable ledger and a live broker read, no synthesised values.
  </footer>
</div>
"""
    out.write_text(page, encoding="utf-8")
    print(f"wrote {out}")
    print(f"  equity {_money(equity)}  positions {len(positions)}  cycles {len(runs)}"
          f"  fills {len(fills)}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("reports") / "dashboard.html")
