# Elrond completion evidence — deployment in progress

This record is an implementation and validation report, not a declaration that
deployment has finished. Runtime measurements and the cutover result below must
be replaced with observed results before release acceptance.

## A. Executive summary

The refactoring preserves the existing Python modular monolith, SQLite ledger,
PAPER broker boundary, Windows scheduler, and dashboard. It fixes excessive
guardian projection growth, narrows historical queries, explains every Trader
poll, bounds launcher logs and accounting reads, consolidates launcher setup,
and makes protection and fee reporting reflect available evidence.

Historical trades, raw guardian receipts, model artifacts, and losing outcomes
are retained. The cleanup tool only removes redundant index memberships for
complete histories of canonically proven pre-submission refusals. It writes a
new offline candidate and verifies all other table rows before installation.

The code changes do not demonstrate profitability. The retained 147 closed
core trades have gross P&L -0.016007592, 74 gross wins and 73 losses, profit factor
0.966853, and six positive/six negative realized trading days. Exact trade-level
costs are unavailable, so net P&L is unknown. These are exploratory COLLECT
trades; three evaluated model candidates were rejected by validation. Rejected
models are not promoted to improve apparent activity or win rate.

## B. Architecture comparison

```text
Before:
Dynamic discovery -> Trader -> durable execution coordinator -> PAPER broker
                        |                |                         |
                        +------ authoritative SQLite ledger -------+
                                     |             |
                                  Guardian      Outcomes/learner
                                     |
Legacy Protection + Legacy Deadline (retained ownership/recovery paths)
Research (observational evidence) + Console

Intended verified cutover:
Dynamic discovery -> Trader -> same durable coordinator -> same PAPER broker
                        |                |                         |
                        +------ same authoritative ledger ---------+
                                     |             |
                                  Guardian      Outcomes/learner
Research (observational evidence) + Console
```

The legacy services may be retired only after their outstanding ownership and
handoffs are discharged. The recorded audit found no pending protection actions,
unresolved intents, or unacknowledged fill-reconciliation work; three deadline
handoffs were closed and a fourth never created an intent and had expired.
Fresh broker inventory must also be flat at cutover. Their source and original
task definitions remain available for recovery, without an enabled second engine.

## C. Dead-code removal

- Removed the unreferenced 41-line `paper_core/maintenance.py` helper after
  checking runtime, CLI, configuration, tests, and migration references.
- Consolidated repeated setup in four Windows launchers into one shared helper.
- Retained legacy execution, protection, and deadline modules: public runners,
  historical readers, and recovery paths still reference them. Disabled runtime
  services are not evidence that all their source can safely be deleted.
- Removed no production dependency: all 13 direct dependencies remain used.

The comparable preserved-component baseline contains 118,614 maintainable lines
in 488 files. After the maintenance-tool corrections, the result contains
120,385 lines in 492 files: 2,250 physical lines added and 333 removed. Safety,
diagnostic, and verified-cleanup code accounts for growth. There is no claimed
net source reduction or achievement of the aspirational 20–40% target.

## D. Refactoring changes

- Guardian membership generation excludes terminal plans with exact proof that
  no order was submitted. Active and ambiguous orders retain protection.
- Deadline queries filter handoffs and acknowledgements in SQL before hydration
  and acquire write transactions after expensive read preparation.
- Health and account-cost queries have bounded work; an incomplete result is
  reported as unavailable instead of a partial zero.
- Every Trader poll records a diagnostic outcome, including waiting for the next
  strategy boundary, unavailable/stale evidence, session closure, signal/risk
  rejection, and operational blockers. These diagnostics do not grant orders.
- Console mutation routes share Origin/Host/peer validation. Protection warnings
  use verified current core ownership; historical protection records are labeled.
- Fee-only account cash dates appear on the calendar. Account cash fees without
  order/fill references are not fabricated as exact per-trade allocations.
- Launcher logs rotate at 32 MiB with three archives per role while retaining
  Job Object descendant cleanup and exit semantics. Twelve oversized/old launcher
  logs were retained in a hash-verified ZIP; removing matching originals saved
  221,704,436 bytes net and prevents oversized legacy archives at startup.

## E. Performance comparison

| Metric | Before | After |
|---|---:|---|
| Maintainable source LOC | 118,614 | Final measurement pending |
| Source files | 488 | Final measurement pending |
| Direct production dependencies | 13 | 13 |
| Scheduled services | 7 | Cutover pending; planned 5 |
| OS descendant processes | 35 | Running-system measurement pending |
| Working-set sample | 878,223,360 bytes | Pending comparable sample |
| Machine CPU | 8.99% over 10.20 s | Pending; not application-only CPU |
| Mean Trader cycle | 3.396 s | Pending |
| Mean Guardian cycle | 17.425 s | Pending |
| Mean Outcomes cycle | 5.066 s | Pending |
| Operational DB, inventory baseline | 131,318,099,968 bytes | Pending cleanup |
| Operational DB at maintenance stop | 140,121,706,496 bytes plus WAL | Pending cleanup |
| Previously redundant DB copies | 53,005,099,008 bytes | Removed in preceding cleanup |
| Startup, lock incidents, query latency, external request volume, recovery | Comparable baseline unavailable | Do not invent improvement percentages |

## F. Reliability evidence

The guardian growth defect is repeated indexing of thousands of terminal,
definitively unsent plans. It is distinct from trade history and from arbitrary
SQLite corruption. Regression tests preserve raw-history fallback and prove
that repaired guardian renewal does not recreate retired memberships.

Multiple competing entry engines were a hypothesis. The inventory found one
registered core Trader; Guardian and Outcomes were entry-disabled. Launcher,
PowerShell, virtual-environment Python, and base Python descendants must not be
mistaken for independent trading engines. Process ownership is rechecked at
cutover, rather than inferred from scheduled-task state alone.

The historical overlong GOOGL hold has a recorded intervention followed by an
exit. The exact old source-level cause is not established, so it is not reported
as a newly reproduced and fixed defect. Database lock-incident reduction likewise
requires measured runtime evidence; narrower transactions alone do not prove it.

## G. Tests

Recorded unique results including the final maintenance-tool followup: 4,971 passed,
48 skipped, zero failures. The full run reached its 1,800-second process cap;
a continuation deselected all completed nodes and ran the remainder. The cap
was not reported as a completed full run. Node-level results are in
`test-results.json`.

The isolated sanitized runner is invoked with the existing interpreter:

```powershell
& D:\Elrond\App\.venv\Scripts\python.exe -B .superpowers/sdd/runtime-recovery/run_checks.py <name> <seconds> <pytest arguments>
```

Full Ruff checks passed. Mypy passed 383 source files for Windows, 386 source/CI
files for Linux, and the three CI helpers for Windows. The lockfile check passed
offline. The repository-wide formatter reports 318 files needing formatting;
unrelated formatting churn was not included or represented as passing.

Critical coverage: 550 passed, one local market-archive skip; seven in-process
modules cover 1,587/2,023 statements (78.45%) and 552/820 branches (67.32%). This
does not include subprocess or C# coverage and is not whole-application coverage.

Skips include PostgreSQL without a disposable server, real-data archives, live
LLM calls, WSL/generated environments, optional runtime probes, and platform
privileges. PAPER isolation, duplicate submission, emergency stop, accounting,
recovery, and cleanup corruption/refusal scenarios have executed test coverage.

## H. Deployment

Authoritative host: HP OMEN `LEANNES-LAPTOP`. Repository: `D:\Elrond\App`.
One repair worktree: `D:\Elrond\worktrees\runtime-repair`.
Requested E: is unavailable; the established D: topology is retained.

Deployment is pending verified backup and cleanup. All seven original tasks are
disabled under the recorded maintenance fence. The staged core successor is
`eaa4ef122e48016f0abf03da2239fc3da1a0aa03408ec0505402141e25dbc07b`.
Its exact patch comes from c0a2d65 and diagnostic followup 9cf0733, not every file
in the later development checkout. Console changes have separate staged pins.
No successor is described as running until its manifest, task actions, process
ancestry, heartbeat, dashboard, and checked resume are observed.

Rollback retains original files/manifests/task XML and a verified unpruned ledger.
After new trades, restoring the old snapshot would lose history and is forbidden.
Old guardian code requires rebuilding pruned memberships against the current
ledger, or restoring the original only before any subsequent writes.

## I. Remaining limitations

- Profitable trading has not been demonstrated; exact historical net returns
  cannot be established from account fee activities without fill attribution.
- Crypto mechanics have a tested synthetic, single-ledger capability slice;
  this is not a commissioned production 24/7 crypto strategy or predictive model.
- Baseline startup/recovery/request-rate measurements are incomplete.
- Optional infrastructure tests and repository-wide formatting remain explicit.
- Deployment and measured resource reductions are still pending.

## J. Further optimization justified by evidence

First observe guardian projection growth and role cycles after the corrected
deployment. Use chronological, cost-aware out-of-sample model evaluations before
promoting a strategy. Extend commissioned crypto only with real data capability,
session, precision, shared portfolio-risk, and recovery proofs; a second trading
engine is not a shortcut. Remove further source only when runtime/public/recovery
reachability has been disproven.
