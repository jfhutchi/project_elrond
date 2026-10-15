# Project Elrond

> **Runtime handover, October 15 at 08:13 Eastern:** both stores are now schema 36; upgraded dashboard, inherited protection and deadline services are running. The bounded deployment task waits for the original IWM exit before commissioning guardian/outcomes and enabling trader after fresh protection checks. Trading is not active at this capture. See the [measured runtime handover](docs/evidence/paper-core/runtime-handover-2026-10-15/README.md).

An autonomous quantitative research and PAPER trading platform that tries hard not to fool itself.

The implemented PAPER core has three independent roles: a trader, a guardian, and an outcomes
worker with daily learning. Ordinary data, provider, or clock interruptions cause scoped waits
and automatic recovery when fresh evidence permits. Explicit manual entry stops and genuine
identity, ownership, or accounting failures retain their controls. Protection, exits, and
trustworthy outcome processing remain independent of entry and learning failures.

The local upgrade now includes the operating/learner database migration, obsolete-writer fence,
and merged protection/deadline/dashboard services. A one-time deployment task is running;
the original IWM exit must reconcile before it commissions the three new core roles.
The broker reports the next opening at 09:30 Eastern. The fresh inherited protection assessment
requires the guardian's certified inventory; the trader stays disabled until that check passes.
See the [runtime handover](docs/evidence/paper-core/runtime-handover-2026-10-15/README.md)
for measured state, backups, exact pins, continuation limits and live local status paths.

The [approved successor policy](docs/superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
uses qualifying model signals first and otherwise makes exploratory PAPER entries across stocks,
learning from both wins and losses. It permits six durable reservations per exchange session,
with one concurrent core lot. Its V3 target is actual executed gross return; a separate
current-funds policy preserves continued entry capacity without inventing fee or cash history.

**The successor is implemented, reviewed and merged to main. Its installed trader is not yet commissioned.**
The integrated witness collects ten sessions of wins and losses,
fits and activates V3, then selects a qualifying signal and a negative-prediction exploratory
trade. The earlier measured XLE package remains separate. An attempted trade cannot guarantee
a broker fill or improved daily performance.
The [successor evidence](docs/evidence/paper-core/daily-exploration/README.md) records
test/review results, candidate-preparation status and remaining release work separately from
baseline deployment evidence.

Start with [the PAPER core overview](docs/paper-core.md), then use the
[handover](docs/evidence/paper-core/handover.md) and
[current deployment evidence](docs/evidence/paper-core/local-deployment-2026-10-15/README.md) for exact pins,
verification results, the blocker, and the remaining rollout sequence.

## What the implemented core does

```text
Real market data -> Trader -> durable decision and protected entry intent -> PAPER broker
                                                                              |
                   Guardian <- owned fills and order evidence -----------------+
                      |
                 protected exits -> reconciliation -> Outcomes and daily learning
                                                           |
                            later trader decision <- approved immutable model
```

| Role | Responsibility |
|---|---|
| Trader | Evaluate fresh five-minute opportunities using one active policy/model; the approved successor selects qualifying signals before labeled exploration, with durable boundary/session limits |
| Guardian | Independently recover uncertain submissions, reconcile owned fills, and protect/close owned lots; never create new exposure or adopt unexplained inventory |
| Outcomes | Reconcile complete round trips and costs continuously; consider bounded model updates after completed exchange sessions, with outcome processing ahead of fitting |

The roles share one authoritative operational SQLite store and retained execution primitives.
The optional dashboard is a view, not execution authority. The Omen is the sole permitted PAPER
coordinator; the Pi is retired and must not be contacted.

Learning begins consideration after **ten completed exchange sessions, including quiet sessions**.
It then considers every completed session using all eligible, compatible history, including wins
and losses. Outcomes from the latest ten completed sessions receive raw weight 2; older outcomes
receive weight 1, normalized before fitting. There is no current 60-session history cutoff.
Past-only validation, protected-holdout exclusions, permanent trial accounting, fixed risk limits,
and exact model attribution remain mandatory. Insufficient, unchanged, or unreliable evidence
produces an honest `NO_CHANGE`; ten sessions do not guarantee a fit. A failed fit or rejected
candidate retains the incumbent while the hybrid policy continues exploration.

V2 retains its strict net-return contract. The approved V3 learner admits a closed, owned,
reconciled outcome with established gross P&L when `FEE_UNAVAILABLE` is its only remaining
incompleteness. Fees stay unknown, status stays `COSTS_PENDING` and broker net P&L stays null.
V3 retains overlapping feature windows within training while preserving past-only fold boundaries,
label availability and purge/embargo. Its gross-target activation needs `DAILY_BOUNDED_V3`;
an older net model or commission cannot be silently reused as gross authority.

The approved `BROKER_PAPER_BALANCE_V1` entry-funds policy uses fresh stable PAPER account/position
evidence and current open-order coverage, bound to quantity proof and the current ledger. It
authorizes sizing from current cash/buying power, not reconstructed historical cash. Incomplete
cash remains `expected_cash=None` and `cash_complete=False`; no balancing residual or invented
reconciliation is allowed. Legacy model-only entries keep their strict exact-cash requirement.

## The safety and evidence boundary

**Real-money trading is disabled and unauthorized.** The new core fixes its broker endpoint to
`https://paper-api.alpaca.markets` and binds authority to the verified account, store, release,
policy, model, and role. Existing configuration uses `TRADING_MODE=PAPER` and
`LIVE_TRADING_ACKNOWLEDGED=false`; there is no operative setting named `LIVE_TRADING`.

Paper trading is authorized within its commissioned boundaries. A code merge, passing test, model
file, or scheduled task does not commission a runtime. Manual entry stops require explicit,
audited resume with fresh readiness. A routine timeout does not become a persistent global stop,
and repeated observations do not repair a genuine accounting contradiction.

The target evidence chain is observation -> decision -> intent -> owned fills -> independent
protected exit -> flat reconciliation for that lot -> truthful gross/cost/net evidence -> learning receipt -> later
decision acknowledging the exact active model. Unknown actual costs remain `COSTS_PENDING`;
modeled costs never become verified broker net P&L. Synthetic tests, real-data simulation, and
broker-PAPER observations stay separate. A broker-PAPER lifecycle under the replacement is still
required; no deployed learning or profitability is claimed.

Elrond also retains the broader research discipline: pre-registration, statistical power gates,
protected holdouts, adversarial testing, multiple-testing accounting, and permanent positive and
negative findings. External models may propose; Elrond's evidence gates decide. Agreement among
models is not statistical evidence. The research promotion ladder ends at
`LIVE_REVIEW_ELIGIBLE`, which grants no broker authority. Only the human operator may authorize
future use of real capital.

## Development start

Python 3.11+ and `uv` are required. In an isolated development checkout:

```bash
uv sync --locked
uv run python -m quantbot.paper_core --help
```

This installs locked development dependencies and shows the core's explicit command/role
interface. Use the [Omen deployment guide](deploy/omen/README.md) and exact reviewed release for
runtime work. Do not sync dependencies into a running environment or infer deployment from a
checkout.

The core entry point is `python -m quantbot.paper_core`. It exposes explicit preparation,
verification, status/export, migration, acquisition/commissioning, fencing/resume, model rollback,
and release-transition commands, plus the `trader`, `guardian`, and `outcomes` roles. Preparation,
migration, commissioning, and role startup are separate operations. See the
[agent runbook](docs/agent-runbook.md) for operational context.

The existing `quantbot` commands remain available for the retained system and research tools:

| Command | Purpose |
|---|---|
| `quantbot status`, `quantbot deployment-status` | Inspect the selected retained store and recorded deployment state; fresh runtime evidence is still required |
| `quantbot doctor`, `quantbot preflight` | Diagnose readiness; doctor can contact the configured broker |
| `quantbot create-database`, `quantbot migrate-database`, `quantbot backup-database` | Explicit store creation, upgrade, and backup operations |
| `quantbot console` | Operator console at `http://127.0.0.1:8787`; includes a control plane and is not read-only |
| `quantbot research-status`, `quantbot research-capacity` | Inspect research evidence and remaining statistical capacity |
| `quantbot hypotheses`, `quantbot register-hypothesis` | Inspect and freeze research registrations |
| `quantbot backtest`, `quantbot verify-manifest`, `quantbot reproduce-experiment` | Historical evaluation and reproducibility |
| `quantbot learner --database PATH` | Separate supervised learner tools; see [the learner guide](docs/elrond-learner.md) |

Legacy `daemon`, `run-once`, protection, and kill-switch commands retain their own runtime
contracts. They are not shortcuts for activating the replacement. `quantbot-kronos` remains a
[shadow forecast worker](docs/kronos-shadow.md) with no order path.

## Source and documents

| Path | Responsibility |
|---|---|
| [`src/quantbot/paper_core/`](src/quantbot/paper_core/) | Three-role runtime, authority, timing, ownership, outcomes, learning, and release contracts |
| [`src/quantbot/execution/`](src/quantbot/execution/), [`src/quantbot/brokers/`](src/quantbot/brokers/) | Retained broker integration, intent/fill handling, and recovery primitives |
| [`src/quantbot/risk/`](src/quantbot/risk/), [`src/quantbot/operations/`](src/quantbot/operations/) | Retained risk, protection, readiness, and operational paths |
| [`src/quantbot/storage/`](src/quantbot/storage/) | Durable ledger and explicit migrations |
| [`src/quantbot/research/`](src/quantbot/research/) | Research registry, power, memory, budgets, workers, and promotion |
| [`src/quantbot/market_data/`](src/quantbot/market_data/), [`src/quantbot/backtest/`](src/quantbot/backtest/) | Point-in-time data and historical evaluation |

| Document | Read it for |
|---|---|
| [PAPER core overview](docs/paper-core.md) | Merged baseline, approved hybrid/V3/current-funds successor, implementation status and acceptance contract |
| [Accepted design](docs/superpowers/specs/2026-10-13-paper-core-design.md) | Detailed baseline design and the October 14 all-history learning amendment |
| [Implementation plan](docs/superpowers/plans/2026-10-13-paper-core.md) | Milestones and implementation review history |
| [Handover](docs/evidence/paper-core/handover.md) | Accepted implementation evidence and remaining operational work |
| [Current deployment package](docs/evidence/paper-core/omen-deployment/README.md) | Dated host evidence, exact release/config, migration rehearsal, and activation blocker |
| [CLAUDE.md](CLAUDE.md) | Project charter, agent guidance, boundaries, and engineering standard |
| [STATUS.md](STATUS.md) | Agent orientation and links to measured state |
| [PROJECT_STATUS.md](PROJECT_STATUS.md) | Historical generated snapshot, not current forward progress |
| [REFUTED.md](REFUTED.md) | Settled research findings; read before proposing research |
| [Research architecture](docs/research-architecture.md) | Retained research subsystem and trust boundaries |
| [Intraday direction](docs/intraday-paper-direction.md) | Intraday objective and evidence acceptance context |
| [Agent runbook](docs/agent-runbook.md), [Omen deployment](deploy/omen/README.md) | Operational procedures and host ownership |
| [Runtime topology](docs/runtime-topology.md), [dashboard access](docs/dashboard-access.md) | Single-host policy and dashboard access |
| [Local CI](docs/local-ci.md) | Isolated Windows/Linux verification and its limitations |
| [Documentation map](docs/documentation-index.md) | All current guides and preserved historical Markdown records |

Root `.superpowers/`, `.codex/`, and other ignored scratch directories are local tooling state and
must stay out of origin. Durable design/plan documents under `docs/superpowers/` and reviewed
artifacts under `docs/evidence/` are intentional repository records. Preserve historical evidence
and original source identities when updating current guidance. `version_01` remains the preserved
baseline and is never developed on directly.

## Verification and honest progress

**A test must assert the specific protection it claims.** A timeout or unrelated exception is not
proof of the named safety property. Prefer checks that fail when the mechanism is removed.
Choose checks suited to the change; documentation changes need source and link verification.

The shared CI contract in `scripts/ci_checks.py` covers locked dependencies, lint, typing for both
Windows and Linux, tests, and a clean-tree check. Use
[`scripts/local-ci.ps1`](scripts/local-ci.ps1) for isolated local verification and
[the local CI guide](docs/local-ci.md) for unavailable-platform reporting. A local result does not
publish independent GitHub status checks or establish current branch protection. Tests must not
receive credentials or reach a broker.

The current dependency constraint is `mako>=1.4.2,<2`, locked to **1.4.3**. Its
[affected regression gate](docs/evidence/paper-core/mako-security/README.md) passed **169 tests**
with **26 PostgreSQL-only skips**. Earlier scoped results remain separately attributed in the
handover; they are not summed into a full-repository-green claim. The prepared core targets
**schema 36**. Only backup copies have been migrated from schema 29; the operating store has not.

Engineering progress and market evidence are separate. The research milestone ladder reports
what durable research evidence supports; the PAPER-core M0-M6 implementation milestones do not
replace it. A tested implementation, a successful simulation, or ten elapsed sessions cannot
establish an edge. `NO_EDGE_ESTABLISHED`, abstention, and `NO_CHANGE` are legitimate evidence
results; the new daily exploration policy must record attempts, fills, and outcomes truthfully.
