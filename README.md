# Project Elrond

An autonomous quantitative research platform that tries hard not to fool itself.

> **Current runtime policy (2026-09-08):** the Raspberry Pi is retired entirely and must not be
> contacted or used for any role. The HP Omen is the sole permitted PAPER coordinator, with no
> Alienware or other-host dependency. Local-account startup tasks and PAPER reconciliation are
> verified. The tested entry-budget fix is deployed; measured readiness passed and the kill
> switch is clear. The prior failed cycle remains in the ledger. See
> [runtime topology](docs/runtime-topology.md).

Elrond discovers candidate market strategies, attacks them, and promotes only the ones that
survive — through pre-registration, statistical power gates, protected holdouts, adversarial
testing and multiple-testing accounting — into shadow and then **paper** trading, where it
accumulates genuine forward evidence.

The measure of success is not a backtest number. It is repeatable, risk-adjusted, executable
positive expectancy after realistic costs, demonstrated forward. Most ideas are expected to
fail, and the system is built so that failing is cheap, honest and permanent.

---

## The safety boundary

**Live trading is disabled. Real-money trading is not authorized.**

The two settings that say so are `TRADING_MODE` (default `PAPER`) and
`LIVE_TRADING_ACKNOWLEDGED` (default `false`); `TradingMode.LIVE` without the acknowledgement is
refused in `config.py`, and credentials are read from the pair matching the mode. There is no
setting called `LIVE_TRADING` — the project's prose has used that as shorthand for the boundary,
including in `CLAUDE.md`, and it is worth knowing that a file which *sets* it is configuring
nothing.

Paper trading against Alpaca's paper environment *is* authorized and runs autonomously. The two
are never treated as equivalent. The promotion ladder terminates at `LIVE_REVIEW_ELIGIBLE` and
there is no stage after it — implemented by the **absence of a destination** in the transition
table (`ALLOWED_PROMOTIONS[Stage.LIVE_REVIEW_ELIGIBLE]` is the empty set), not by a permission
check that could be misconfigured.

No agent may enable live trading, insert real broker credentials, weaken a risk control to
improve apparent performance, clear a kill switch without verifying the underlying conditions,
or fabricate a forward observation. Only the human operator may ever authorize real capital.

---

## Quick start

```bash
uv sync
```

```bash
uv run quantbot doctor
```

| Command | What it does |
|---|---|
| `quantbot create-database` | Bootstrap a new store. The only command that creates one |
| `quantbot migrate-database` | Upgrade an existing store. The only command that rewrites one |
| `quantbot backup-database` | Copy a store to a new path. Refuses to overwrite |
| `quantbot console` | Operator console on http://127.0.0.1:8787. Mostly read; see below |
| `quantbot config` | Every setting in force, its source, and why it is immutable |
| `quantbot status` | Durable state: kill switch, last run, reconciliation |
| `quantbot doctor` | Preflight checks against config, ledger and broker |
| `quantbot preflight` | What this host is configured to do, and what each capability is missing. Exits 1 if anything is unready |
| `quantbot sync-data` | Pull bars into the durable store |
| `quantbot run-once` | One operational cycle |
| `quantbot daemon` | Continuous paper trading |
| `quantbot protection-daemon` | After-entry position protection, independent of strategy and research |
| `quantbot reconcile` | Reconcile the ledger against the broker |
| `quantbot kill-switch engage --reason ...` | Halt trading. Always permitted |
| `quantbot kill-switch clear --reason ...` | Resume, **only** if readiness probes pass |
| `quantbot paper-smoke --acknowledgement ...` | One order against the paper account, behind every paper gate plus an exact second acknowledgement |
| `quantbot backtest` | Historical evaluation |
| `quantbot report-weekly` | Regenerate a week's report from the ledger |
| `quantbot hypotheses` | Registered hypotheses |
| `quantbot register-hypothesis` | Freeze a pre-registration |
| `quantbot research-status` | Research queue and model configuration |
| `quantbot deployment-status` | What is actually running, without filesystem archaeology |
| `quantbot ingestion-status` | Collection cursors, run history, incidents, heartbeats |
| `quantbot counterfactual-report` | [Read-only gate-value report](docs/counterfactual-report.md) for rejected candidates |
| `quantbot research-capacity` | What statistical capacity is left, without spending any |
| `quantbot research-cycle` | Advance the research loop; `--propose-when-idle` (what the scheduled component runs) asks for one question when nothing is actionable |
| `quantbot research-propose` | Report the proposal slate; `--commit` opens at most one catalogue question, recording every attempt in `research_proposals` |
| `quantbot integrity-sweep` | Demote strategies with unresolved incidents |
| `quantbot verify-manifest` | Check an experiment bundle's invariants |
| `quantbot reproduce-experiment` | Re-run the bundle a manifest names |

`quantbot-kronos` is a second entry point for the shadow forecasting worker
([docs/kronos-shadow.md](docs/kronos-shadow.md)); it has no order path.

`quantbot learner --database PATH` exposes supervised dataset, train, predict, resolve, evaluate,
compare and status commands. The initial loop uses explicitly local-declared data, immutable
models and separately committed predictions/outcomes. It establishes software capability, not
profitability or forward market evidence. See [Elrond Learner](docs/elrond-learner.md).

**The console is not read-only.** It was, through #82 slice 2; slice 3 added a control plane.
It can engage and clear the kill switch, trigger `reconcile` and `sync-data`, and propose,
apply and roll back portfolio-veto policy versions. What it cannot do is bounded by
construction rather than by convention: the route table is an allowlist, order submission is
not on it, credentials never round-trip through a browser, an apply appends a *successor*
policy version instead of editing one that historical refusals already cite, and
`PolicyAuthority.OPERATOR` is unreachable from any automated path.

Python 3.11+. Run the suite with `uv run pytest tests/`.

---

## How it is organised

```
market data ──▶ discovery ──▶ ELROND TRUST BOUNDARY ──▶ evidence ──▶ promotion ──▶ paper
                                        │
              novelty · search burden · power gate · integrity checks
              critic · frozen registration · protected holdout · robustness
              multiple-testing correction
```

Every package under `src/quantbot/`:

| Package | Responsibility |
|---|---|
| `domain/` | Ids, enums, lifecycle and the models the other packages share |
| `strategy/` | Strategy identity, configuration, versioning, rebalance tranching |
| `market_data/` | Point-in-time data, validation, survivorship, FRED, instruments |
| `backtest/` | The engine that scores a signal, including externally generated ones |
| `risk/` | Sizing, gates, drawdown, vetoes, exposure, protection, portfolio state |
| `execution/` | Order intents, broker orders, fills, reconciliation, recovery |
| `brokers/` | Alpaca paper adapter behind a provider-neutral interface |
| `operations/` | Cycle, kill switch, readiness, supervisor, protection, qualification |
| `storage/` | Durable SQLite ledger, Alembic migrations, repositories |
| `research/` | Registry, power, critic, memory, budget, workers, promotion, synthesis |
| `ingestion/` | Durable collection state: cursors, run history, incidents, heartbeats |
| `anomaly/` | Anomaly and catalyst detection, and the routing it earns |
| `forecasting/` | The Kronos shadow worker and its separate forecast-only ledger |
| `sandbox/` | Isolation for generated code — Windows and WSL2 backends |
| `node/` | Machine identity, capability detection, role policy |
| `console/` | The operator console: pages, state, and the policy control plane |
| `reporting/` | Weekly reports, performance, `PROJECT_STATUS.md` |

**Elrond is the judge, not another voter.** Kronos, TradingAgents, RD-Agent and any language
model are *untrusted research workers*. They may propose; only Elrond's empirical gates may
conclude. Three models agreeing is not evidence, and a model reporting 92% confidence is not 92%
statistical confidence.

---

## Where to read next

| Document | For |
|---|---|
| [CLAUDE.md](CLAUDE.md) | The project charter — mission, boundaries, engineering standard |
| [docs/research-architecture.md](docs/research-architecture.md) | The research subsystem, its trust boundaries, and the design patterns it repeats |
| [STATUS.md](STATUS.md) | Agent orientation: current state, known defects, open questions |
| [REFUTED.md](REFUTED.md) | **Read before proposing research.** Every idea already measured and killed |
| [docs/agent-runbook.md](docs/agent-runbook.md) | Operating the paper-trading system |
| [deploy/pi/README.md](deploy/pi/README.md) | Retired Pi deployment, preserved as historical evidence |
| [deploy/omen/README.md](deploy/omen/README.md) | Native Windows PAPER runtime on the sole designated Omen host |
| [docs/runtime-topology.md](docs/runtime-topology.md) | Current single-host policy, measured capacity, and unresolved runtime evidence |
| [docs/node-deployment.md](docs/node-deployment.md) | What a second machine would need, and what the repository actually assumes today |
| [docs/deployed-state.md](docs/deployed-state.md) | What is actually running, stated by the runtime instead of reconstructed later |
| [docs/ingestion-control.md](docs/ingestion-control.md) | Collection cursors, run history and heartbeats — a stopped collector that is answerable |
| [docs/database-target.md](docs/database-target.md) | The storage seam, and why it deliberately enables no new backend |
| [docs/kronos-shadow.md](docs/kronos-shadow.md) | The Kronos forecasting worker: setup, limits, and what it does not establish |
| [docs/kronos-worker-v2-qualification.md](docs/kronos-worker-v2-qualification.md) | The patched worker environment, measured rather than assumed |
| [docs/worker-environment-identity.md](docs/worker-environment-identity.md) | Why a worker's environment identity is recomputed per backend, never held constant |
| [docs/tranching-spec.md](docs/tranching-spec.md) | Staggering the rebalance date, its measurements, and what is still unwired |
| [docs/per-session-trend-spec.md](docs/per-session-trend-spec.md) | A resolved investigation, six wrong diagnoses kept as written |
| [docs/anomaly-routing-value.md](docs/anomaly-routing-value.md) | The falsification gate the anomaly detector had to clear before it was built on |
| [docs/COMPETITIVE_FRONTIER_2026-08-29.md](docs/COMPETITIVE_FRONTIER_2026-08-29.md) | A dated audit against the outside world. A snapshot, not a live document |

`PROJECT_STATUS.md` is generated by `run-once` and `doctor`; do not hand-edit it.

---

## The engineering standard, in one rule

**A test must assert the specific protection it claims.**

A test that passes because of a timeout, a generic exception, or any unrelated failure mode does
not demonstrate the property it names — and is worse than no test, because the suite reports
green while the protection is absent.

This is written down because it happened here. The sandbox's memory-limit test accepted either
`memory` or `wall-clock` termination, and that leniency hid a broken monitor for a full run: it
was measuring a ~5MB trampoline while the experiment allocated 600MB. The test passed the whole
time and proved nothing.

So: assert the **named** failure mode, set unrelated limits generously so they cannot cover for
it, and prefer an assertion that fails if the mechanism is deleted. If removing the feature
leaves the test green, the test is measuring something else.

---

## Continuous integration

`.github/workflows/ci.yml` runs the shared `scripts/ci_checks.py` command contract: locked
dependency verification/sync, lint, `mypy` for **both** platforms, `pytest`, and a failing dirty-tree
gate on **ubuntu-latest and windows-latest**. Hosted CI is temporarily **manual-only** as of
2026-09-05 while the hosted allowance is exhausted. The required status checks to protect `main` are
**`checks (ubuntu-latest)`** and **`checks (windows-latest)`**.

For local verification, run `.\scripts\local-ci.ps1` from a clean, committed checkout. It exports
isolated source snapshots for Linux containers and native Windows, and reports unavailable
platforms as NOT RUN. See [setup, coverage, cleanup and limitations](docs/local-ci.md). Local runs
consume no hosted minutes and do not publish independent GitHub status checks.

Both runners, and its first run is why rather than a guess: `mypy src` had been reported clean
for months and fails with seven errors on Linux, because `msvcrt.locking` and `ctypes.windll`
sit behind `os.name` / `platform.system()` guards that mypy does not narrow. The claim was true
on the author's machine and nowhere else. Elrond retains Windows and Linux code paths even though
the current coordinator is native Windows and the Pi/WSL deployments are retired or historical;
one runner only ever checked one platform family.

Before pushing, check the half your machine does not:

```bash
uv run mypy --platform linux src && uv run mypy --platform win32 src
```

It references **no secrets**. The suite must be incapable of reaching a broker, so it is never
handed the means to.

This exists because a green suite reported by the agent that wrote the code is the last claim
in the project still exempt from its own rule: *a gate must not trust the party that benefits
from it passing.*

**`main` is unprotected, and on this repository's current plan it cannot be protected.** Both
the branch-protection and the rulesets APIs answer `403 Upgrade to GitHub Pro or make this
repository public to enable this feature` — verified 2026-09-02, not inferred. That dated inspection
found no enforcement at the merge button; the current manual-only workflow also does not report
checks automatically. Local verification does not change either repository setting.

Two ways out, both operator decisions: GitHub Pro, or making the repository public. Until one
of them happens, the command below returns 403 rather than configuring anything, and CI is a
signal an operator has to read rather than a gate that blocks.

```bash
gh api -X PUT repos/jfhutchi/project_elrond/branches/main/protection -F required_status_checks[strict]=true -F 'required_status_checks[contexts][]=checks (ubuntu-latest)' -F 'required_status_checks[contexts][]=checks (windows-latest)' -F enforce_admins=false -F required_pull_request_reviews=null -F restrictions=null -F allow_force_pushes=false
```

The two contexts are named exactly as above, verified against a real run's job names rather
than guessed from the job key. An earlier version of this command required a context called
`checks`, which is the *job* id and not a status any run reports: branch protection would have
waited forever for a check that never arrives, and the gate would have read as configured while
blocking everything. A matrix job reports one context per cell, `<job name> (<matrix value>)`.

An earlier version of this section said only that protection was outstanding, which read as
"nobody has run the command yet" when the truth is that the command cannot succeed. That
distinction is the whole point: a control believed to be one click away is not treated the same
way as one that is unavailable.

Left to the operator deliberately: branch protection is a repository setting, and an agent
changing the rules it is judged by is the shape of defect this file is full of.

---

## Two kinds of progress, and only one of them is the point

**Engineering progress** is what most of this repository measures: issues closed, gates built,
frameworks integrated, panels added. It moves when code ships.

**Profitability progress** is a separate ladder, `quantbot.research.milestone`, running M0 to M5.
It moves only when evidence about the market changes, and it is derived from durable state rather
than from prose — a milestone cannot be reached by writing about it. `quantbot research-status`
reports both side by side, with the ladder and the reason it is stuck.

The dashboard does **not** carry the ladder. `quantbot.research.dashboard` holds evidence and
budget panels as data models with no renderer behind them, and earlier revisions of this file
described an "edge-progress panel" that was never built. A document claiming a view exists is the
same defect as a milestone reached by writing about it, which is why it is corrected rather than
quietly dropped.

The project is at **M0**: the platform runs valid experiments and honestly refutes them.
`create-database` seeds `REFUTED.md`'s settled findings into research memory and reports the
count, so a freshly created store reaches M0 without any credential configured (#52, #190).
Before that, a new store reported **BELOW_M0** — nothing showed it could reject a hypothesis —
while its refutation history sat checked into the repository, and the reported milestone
depended on an LLM endpoint that has nothing to do with whether anything was ever refuted.

**No candidate edge has been established**, and no level above M1 is close. Reaching M0 is a
statement about the platform, not about the market. Concluding `NO_EDGE_ESTABLISHED` is a
legitimate research result, not a software failure.

---

## Status

Engineering on the v0.2 research architecture is in progress, tracked in the
[v0.2 roadmap issue](https://github.com/jfhutchi/project_elrond/issues/2). 2,402 tests (2,384
passing, 18 skipped for an absent WSL distribution and an unconfigured model endpoint); `ruff`
and `mypy --strict` clean on both platforms across 219 source files. Schema is at V27.

Forward paper evidence is a separate question and this repository is not the place to read it
from. The committed `PROJECT_STATUS.md` is a snapshot generated on 2026-08-18 reporting **day 1
of the 30-day window**; the ledger it was generated from is not in version control (`*.db` is
ignored), so a fresh clone can confirm nothing about what has accrued since. Run `quantbot
status` against the real store for the current figure, and treat any prose in this repository
about forward progress — including this paragraph — as older than that store.

`version_01` is the preserved known-good baseline and is never developed on directly.
