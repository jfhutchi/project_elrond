# Project Elrond

An autonomous quantitative research platform that tries hard not to fool itself.

Elrond discovers candidate market strategies, attacks them, and promotes only the ones that
survive — through pre-registration, statistical power gates, protected holdouts, adversarial
testing and multiple-testing accounting — into shadow and then **paper** trading, where it
accumulates genuine forward evidence.

The measure of success is not a backtest number. It is repeatable, risk-adjusted, executable
positive expectancy after realistic costs, demonstrated forward. Most ideas are expected to
fail, and the system is built so that failing is cheap, honest and permanent.

---

## The safety boundary

**`LIVE_TRADING` is disabled. Real-money trading is not authorized.**

Paper trading against Alpaca's paper environment *is* authorized and runs autonomously. The two
are never treated as equivalent. The promotion ladder terminates at `LIVE_REVIEW_ELIGIBLE` and
there is no stage after it — implemented by the **absence of a destination** in the transition
table, not by a permission check that could be misconfigured.

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
| `quantbot status` | Durable state: kill switch, last run, reconciliation |
| `quantbot doctor` | Preflight checks against config, ledger and broker |
| `quantbot run-once` | One operational cycle |
| `quantbot daemon` | Continuous paper trading |
| `quantbot reconcile` | Reconcile the ledger against the broker |
| `quantbot kill-switch engage --reason ...` | Halt trading. Always permitted |
| `quantbot kill-switch clear --reason ...` | Resume, **only** if readiness probes pass |
| `quantbot backtest` | Historical evaluation |
| `quantbot hypotheses` | Registered hypotheses |
| `quantbot register-hypothesis` | Freeze a pre-registration |
| `quantbot research-status` | Research queue and model configuration |
| `quantbot research-cycle` | Advance the research loop |
| `quantbot integrity-sweep` | Demote strategies with unresolved incidents |
| `quantbot verify-manifest` | Check an experiment bundle's invariants |

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

| Package | Responsibility |
|---|---|
| `strategy/` | Strategy identity, configuration, versioning |
| `market_data/` | Point-in-time data, validation, survivorship, FRED, SEC filings |
| `backtest/` | The engine that scores a signal, including externally generated ones |
| `risk/` | Sizing, gates, drawdown, portfolio state |
| `execution/` | Order intents, broker orders, fills, reconciliation |
| `brokers/` | Alpaca paper adapter behind a provider-neutral interface |
| `operations/` | Cycle, kill switch, readiness, control surface, supervisor, qualification |
| `storage/` | Durable SQLite ledger, Alembic migrations, repositories |
| `research/` | Registry, power, critic, memory, budget, workers, promotion, synthesis |
| `sandbox/` | Isolation for generated code — Windows and WSL2 backends |

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
| [deploy/pi/README.md](deploy/pi/README.md) | Deployment, dashboard, and the operator control surface |
| [deploy/omen/README.md](deploy/omen/README.md) | Bringing the coordinator up on WSL2, and the operator steps it deliberately leaves |
| [docs/runtime-topology.md](docs/runtime-topology.md) | Which machine runs what, measured, and why the Pi Zero W is not the coordinator |
| [docs/kronos-shadow.md](docs/kronos-shadow.md) | The Kronos forecasting worker: setup, limits, and what it does not establish |

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

## Status

Engineering on the v0.2 research architecture is in progress; forward paper evidence is
accumulating. Both are tracked in the [v0.2 roadmap issue](https://github.com/jfhutchi/project_elrond/issues/2).

`version_01` is the preserved known-good baseline and is never developed on directly.
