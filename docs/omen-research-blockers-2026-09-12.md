# Why the Omen produced no research evidence — 2026-09-12

Measured on `LEANNES-LAPTOP` against the authoritative durable store
`D:\Elrond\state\quantbot-paper-fresh-20260908.db`, read from a copy so the live WAL was never
touched. Nothing here is inferred from repository files.

## Establishing what is actually deployed

A git checkout is not evidence of what runs. These came from the scheduled-task definitions and
the store itself:

| Fact | Value | Source |
|---|---|---|
| Deployed commit | `5025b21` | task arg `-Commit`, and `runtime_deployments.git_commit` |
| Runtime branch | `codex/omen-paper-runtime` | `git rev-parse` in the deployed checkout |
| Authoritative store | `D:\Elrond\state\quantbot-paper-fresh-20260908.db` | task arg `-Database` |
| Store identity | `cd2a5911-853a-484e-bbc8-d0c27b5d27aa` | `runtime_deployments.store_identity` |
| Environment file | `C:\Users\leepa\.config\quantbot\quantbot.env` | task arg `-EnvironmentFile` |
| Service working directory | `D:\Elrond\state` | task `WorkingDirectory` |
| Trading mode | `PAPER`, every row | `runtime_deployments.trading_mode` |
| Account | `ddd5231b-5c1e-4a64-9ea1-74f21775466b`, consistent throughout | `account_snapshots` |
| Kill switch | `engaged = 0`, set 2026-09-10T21:15:23Z | `kill_switch_state` |

`quantbot deployment-status` run without arguments reports the store `ABSENT` and almost everything
`UNKNOWN`. That is not a fault in the runtime — it is looking at `quantbot.db` relative to the
current directory, which is not where the store lives. See finding 2.

## Blocker 1 — a generated file locks every service out of the host

**Severity 1. Fixed on `claude/status-file-deadlock`.**

`write_project_status` wrote `Path("PROJECT_STATUS.md")`, a bare relative path resolved against the
process working directory. `PROJECT_STATUS.md` is tracked, so a successful cycle modified a
version-controlled file inside the deployed checkout. `operations/service.py` then refuses to start
any component while `git status --porcelain --untracked-files=all` is non-empty.

The two controls are mutually exclusive, and the deadlock sustains itself — nothing makes the tree
clean again.

```
D:\Elrond\state\launcher-logs\research.log
  2026-09-12T15:15:37Z quantbot service refused: CHECKOUT_NOT_CLEAN
D:\Elrond\state\launcher-logs\ingestion.log
  2026-09-12T15:15:36Z  refused: CHECKOUT_NOT_CLEAN
  2026-09-12T15:45:36Z  refused: CHECKOUT_NOT_CLEAN
  2026-09-12T16:15:36Z  refused: CHECKOUT_NOT_CLEAN

git status in the deployed checkout:  M PROJECT_STATUS.md
that file's own body:                 Generated at: 2026-09-12T15:15:36Z
```

The file that dirtied the tree is the one a run wrote, seconds before the refusals begin.

**Consequences, both larger than a missing report.**

* `Elrond Research` runs `research-cycle --max-steps 1 --commit`. It is the only thing that creates
  research tasks, it has never completed against this store, and `research_tasks` is empty. That is
  the whole of `tasks: 0` and `actionable_now: 0`.
* `Elrond Daemon`, `Elrond Console` and `Elrond Protection` are running only because they started
  before the tree went dirty. A restart would be refused identically, so **the paper coordinator
  could not have come back from a reboot.**

## Blocker 2 — no execution sandbox (separate, and also real)

`mine_for_anomalies` and `codegen` refuse any backend whose `os_enforced` is False. The only
backend that satisfied them was `WslSandbox`, and this host has no WSL distribution, no Docker, and
no Windows Sandbox (Home SKU). So even once the research cycle runs, no generated experiment could
execute.

Fixed on `claude/windows-sandbox-backend`; proven on this host in
[`omen-sandbox-proof-2026-09-12.md`](omen-sandbox-proof-2026-09-12.md).

**These are independent.** Fixing the sandbox alone would not have produced a single research task,
and fixing the deadlock alone would have produced tasks that could not execute.

## Research-memory state

| Table | Rows | Reading |
|---|---|---|
| `research_records` | 25 | `REFUTED.md` findings, all `kind=FINDING`, seeded and intact |
| `research_tasks` | 0 | the cycle has never run here |
| `hypotheses` | 0 | nothing pre-registered against this store |
| `power_assessments` | 0 | — |
| `rejected_candidates` | 0 | — |

Institutional memory survived the move to a fresh store. Nothing was lost; nothing has been added.

## Is broad-market beta already prevented from looking like edge?

The paper portfolio is roughly 100% long broad ETFs, so the question is whether accumulated P&L
could be mistaken for strategy evidence. **The framework already prevents this, mechanically, in
several independent places.** No addition is required.

| Requirement | Where | How |
|---|---|---|
| Beta to benchmark | `research/critic.py::hidden_beta` | OLS of strategy on benchmark returning `beta`, `alpha_per_period`, `alpha_t`, `r_squared` |
| Alpha with significance | same | alpha is never reported without its t-statistic |
| Excess return | `research/forward_excess.py::measure_forward_excess` | paired daily excess against the benchmark |
| Benchmarks available | `backtest/benchmarks.py` | `SPY_BUY_AND_HOLD`, `SPY_SMA200`, `SPY_DONCHIAN`, `PURE_MOMENTUM_12_1`, `MOMENTUM_TREND` |
| Benchmark-relative drawdown | `research/mandate.py` | `max_drawdown_bps` under a `BENCHMARK_RELATIVE_GROWTH` objective |
| Market regime | `research/critic.py::regime_split`, `market_regime_snapshots` | results split by era |
| Factor concentration | `risk/exposure.py` | correlation-derived exposure groups rather than sector labels |
| Correlation among positions | `risk/dependence.py`, `veto_policy.correlation_threshold` | enforced at execution as a veto |

`forward_excess.py` is explicit about the exact failure the audit was worried about:

> A raw strategy return would flatter the system every time the market rose, and "we made 8%" is
> not evidence of skill in a year the benchmark made 12%.

It refuses three ways: fewer than `MINIMUM_SESSIONS = 60` paired sessions, an excess whose |t| does
not clear the threshold, or missing data on either leg. **With 3 qualification days recorded
(2026-09-09 qualified, 2026-09-10 not, 2026-09-11 qualified) and 11 fills, it refuses on the first
count.** The concern that three days is nowhere near sufficient is already enforced by code rather
than by discipline.

**The one genuine gap**: every named benchmark variant is SPY-based. `hidden_beta` takes any
series, so a QQQ or factor regression works if one is supplied, but there is no QQQ variant to ask
for. For a broad-ETF book SPY is the appropriate benchmark, so this is a missing convenience rather
than a scientific hole. Recorded, not built — inventing a second benchmark nobody has asked to
regress against would add surface without adding evidence.

## Lower-priority findings, with measured status

1. **`paper-smoke` consumes environment-derived health flags.** Not re-examined this session.
   Unchanged.
2. **`deployment-status` mischaracterises the live store.** Confirmed. Run without arguments it
   resolves `quantbot.db` relative to the working directory, finds nothing, and reports
   `store_authority: UNKNOWN`, `state: ABSENT`, `ok: false` with seven blockers — while the real
   WAL-backed store sits at `D:\Elrond\state\quantbot-paper-fresh-20260908.db` with a 4.2MB `-wal`
   written the same morning. The command is not wrong about what it looked at; it is misleading
   about what exists. It should take the deployment's configured database the way the service does.
3. **One unresolved incident.** Confirmed: exactly one, `incident-87c3499d…`,
   `UNHANDLED_OPERATIONAL_EXCEPTION` / `BrokerRequestError` at `alpaca.py:233`, occurred
   2026-09-08T20:05:04Z during `run-20260908T200500Z`, `resolved_at` still null. Every other
   incident in the store is resolved. `alpaca.py:233` raises on an unexpected HTTP status — and
   `detail_json` records only `exception_type`, `origin` and `reasons`, dropping the
   `status_code` the exception carries. That missing field is why the incident cannot be diagnosed
   from the store four days later. Capturing it is the smaller and more useful fix.
4. **`component_heartbeats` has a writer and no caller.** Confirmed twice over:
   `IngestionControlPlane.heartbeat()` and `latest_heartbeat()` exist in `ingestion/control.py`,
   no production code outside that module calls either, and the table has **0 rows** on the
   deployed store.
5. **`strategy_deployments.git_commit = d15495e` vs runtime `5025b21`.** Intentional, and the
   difference is the point. `d15495e` ("Record PAPER activation evidence and keep launcher
   fixtures native", 2026-09-08, an ancestor of `main`) is the commit at which the *strategy
   configuration* was deployed, stamped once at `deployment_timestamp` 2026-09-08T16:54:00Z.
   `runtime_deployments.git_commit` separately records the commit each *process* is running, and
   correctly shows the runtime moving `55cb000 -> f125bd5 -> 5025b21` across restarts. Two
   different facts in two different tables; collapsing them would lose strategy provenance every
   time the runtime was patched.

## Safety boundary — verified unchanged

Checked after all sandbox work on this host:

* `TRADING_MODE` / `runtime_deployments.trading_mode`: `PAPER`, every row.
* Live credentials: none configured; no `.env` in the deployed checkout.
* `kill_switch_state`: `engaged = 0`, `updated_at` 2026-09-10T21:15:23Z — untouched by this work.
* Latest reconciliation `RECONCILED`, `diff_count: 0`, `allows_new_orders: true`.
* Account identity unchanged: `ddd5231b-5c1e-4a64-9ea1-74f21775466b`.
* Equity $497.18 at 2026-09-11T20:05:00Z, cash $2.04 — the last durable observation; today's
  20:05Z cycle had not yet run when this was recorded.
* Coordinator, console and protection tasks still `Running`, with process start times of
  2026-09-10 and 2026-09-11 — all predating this session.

No file in the deployed checkout was modified, and no service was stopped, started or reconfigured.
