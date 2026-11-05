# Elrond engineering acceptance checkpoint — November 4, 2026 ET

Status: **implementation in progress; not deployed; comprehensive acceptance incomplete**.

## Additional acceptance requirements

1. Demonstrate one entry engine and one authoritative trading ledger from task registrations, process ancestry, configured store identities, runtime commissions and writer exclusion tests. Multiple roles or auxiliary evidence databases do not by themselves establish competing engines. Treat contention and version competition as hypotheses until measured.
2. Explain each cycle using independent observational outcomes: waiting for evaluation boundary, market closed, missing/stale data, no qualifying candidate, risk rejection, awaiting guardian, execution attempted, execution confirmed, or operational failure. Associate reasons with cycle/evidence IDs. A heartbeat, signal or submission attempt is not a fill. Preserve private provider text outside the browser.
3. Evaluate learning reproducibly with chronological out-of-sample evidence, realistic costs and fills, model versions, drawdown and net expectancy. Exploration, training completion, candidate acceptance and profitable realized results are separate facts. Win rate alone is insufficient.
4. Report product source additions/removals separately from tests/documentation, services actually eliminated, and measured deployed reliability changes. Do not convert a source optimization into an invented runtime result.

## Implementation and verification

- `9f155f3`: deadline SQL filters unrelated history before JSON hydration; pending reconciliation uses acknowledgement exclusion; handoff reads precede the first write. Preserves recovery, ownership and order identities. Query scans remain possible without supporting indexes.
- `05b88b8`: four Windows launchers share interpreter/environment/Job Object bootstrap. Includes installer prerequisites and behavioral wrapper tests. No service retired.
- `39984dc`: shared local Origin/Host/peer checks before console mutations; bounded accounting reads fail unavailable rather than returning partial totals; visible fee evidence and safe blockers with age/expiry. Reviewer found invalid fee activity incorrectly described as absent; corrected and regression-tested. Latest blocker followup: 23 focused tests passed in 11.61 seconds, including private manual-pause suffix redaction and four source-verified codes.
- Removed unused `paper_core/maintenance.py` after static/dynamic entrypoint reference inspection. It was not an implemented maintenance-resume protocol.
- Earlier guardian membership growth repair and batched health recovery remain committed and staged for a future controlled release. Production growth has not yet been shown to stop.

Latest integration commands, through the bounded `run_checks.py` runner:

```
pytest tests/unit/console tests/unit/reporting/test_paper_costs.py -v --tb=short -p no:cacheprovider
# 320 passed, 1 existing Starlette/httpx deprecation warning; 86.61 seconds

pytest tests/unit/learner/test_paper_deadlines.py tests/unit/learner/test_deadline_publication_retry.py tests/unit/test_storage.py tests/integration/test_storage_parity.py -v --tb=short -p no:cacheprovider
# 112 passed, 26 skipped; 43.48 seconds
```

The skips require an explicitly disposable PostgreSQL service; runtime PostgreSQL remains disabled. An initial invocation used nonexistent `tests/unit/storage` and collected no tests; it was corrected, not counted as validation. Wrapper evidence records 18 focused passes in 65.37 seconds. Earlier separate core verification records 458 passes and one missing local archive skip, plus three isolated process checks. These are distinct scopes/runs, not a single full-suite coverage claim.

Ruff lint passed for all changed Python files. Formatting check reported 12 files with formatting differences, including pre-existing deployed source style; only the small console/new-test changes were formatted. A repository-wide formatting pass has not passed and is not claimed. `git diff --check` passed. Coverage measurement is unavailable in the current preserved test environment.

## Measured comparison

The initial inventory used an older checkout. Compare source against `f2d9639`, after active dashboard/deadline deltas were preserved, to avoid counting recovered working code as new complexity. `source-comparison.json` contains the current exact counts and definitions. It includes production code/assets under src/scripts/deploy, excluding tests/docs; LOC excludes blank/full-line comments but includes docstrings.

| Metric | Baseline | Current / result |
|---|---:|---|
| Maintained source files | 488 | See source-comparison.json |
| Maintainable LOC | 118,614 | See source-comparison.json; no net reduction claimed |
| Direct production dependencies | 13 | 13; zero removed |
| Scheduled services | 7 | 7; zero eliminated |
| Service descendant processes | 35 | No deployed change |
| Working set sample | 878,223,360 bytes | After unavailable: no cutover |
| Machine CPU sample | 8.99% over 10.20 seconds | After unavailable: no cutover |
| Trader cycle mean | 3.396 seconds | After unavailable |
| Guardian cycle mean | 17.425 seconds | After unavailable |
| Outcomes cycle mean | 5.066 seconds | After unavailable |
| Operating SQLite file | 131,318,099,968 bytes | Not pruned or compacted |
| Redundant offline DB copies | Four proven identical copies | 53,005,099,008 bytes removed earlier |
| Bounded account-cost read | Prior latency unavailable | 0.074 seconds, 12 days, read-only; not a speedup comparison |
| Startup, lock incident rate, physical disk I/O, external request rate, recovery time | Unavailable | Need comparable instrumented runtime samples |

Logical process read/write counters are not physical disk throughput or proof of database growth. No current source change establishes a measured decrease in deployed failure rate, CPU, memory or database size.

## Architecture and ownership evidence

```
Dynamic equity discovery -> core Trader -> shared durable execution outbox -> PAPER adapter
                             |                        |
                     portfolio/risk gates       authoritative SQLite ledger
                             |                        |
                  independent Guardian <---- owned lots and deadlines
                                                      |
                       Outcomes -> chronological learner -> guarded candidate activation
                                                      |
                              read-oriented console + accounting views

Legacy deadline/protection -> retained owned exits, using the shared execution/ownership boundary
Separate research worker -> observational evidence only
```

Source responsibilities and deployed process topology remain unchanged in this increment. Four repeated launcher bootstraps become one helper; three broad deadline hydration paths become filtered repository queries. No message broker, new database or framework was added.

Saved task inventory shows one enabled core Trader entry registration; Guardian and Outcomes are entry-disabled. Deadline/protection source submits exits with ownership checks. Research and learner stores hold auxiliary evidence. This supports a single registered entry path, but does not prove the absence of arbitrary unscheduled processes or eliminate all ledger contention. Retained legacy ownership must be proven discharged before removing those services.

## Reliability and profitability findings

- Deadline loops unnecessarily hydrated and globally sorted unrelated signal/event history, including growing heartbeat history. Moving selection before audit insertion reduces writer-transaction work by construction; actual deployed improvement still needs measurement.
- Guardian receipts repeatedly included thousands of durably unsent plans. The earlier fix bounds that active membership while preserving authoritative historical receipts. Safe historical projection verification, backup/restore and compaction are still outstanding.
- Non-trading diagnostics are incomplete: empty cohort reasons are retained in cohort evidence but lost from the batch return/heartbeat; intervening polls reset the decision; loop progress time can advance during blocked iterations; informational learner outcomes enter objections. Safe browser mappings help but do not fulfill the every-cycle requirement. This is a release acceptance gap requiring explicit observational cycle results, without changing send permission.
- Historical GOOGL lot held about 223.64 minutes against a 30-minute policy. Audited October 16 events record a targeted GOOGL quarantine fix at 18:54 UTC and exit at 18:55. The prior guardian release differed from the plan release and changed near exit. This establishes timing and prior intervention, not the exact historical source-level cause or a newly proven present defect.
- Observed 147 closed core exploration lots: gross P&L approximately -$0.016007592, 74 wins/73 losses, profit factor 0.96685. All were exploration/COLLECT, not an accepted predictive model. Three candidates failed validation; no fit failures in the inspected learner records. Rejection must remain visible.
- Exact per-trade fees remain unknown. Retained provider CAT/REG account activities show cash fees but lack fill/order references. Account fee cash flow is not an exact allocation to core trades. Unknown net remains unknown; fee-only dates remain a reporting gap.
- Active core crypto execution is unsupported. Existing crypto data/legacy components are not a commissioned 24/7 core capability. Starting another entry engine is not an acceptable shortcut.

## Deployment and remaining work

Host: HP OMEN `LEANNES-LAPTOP`. Repository: `D:/Elrond/App`; single repair worktree: `D:/Elrond/worktrees/runtime-repair`, branch `hutch/elrond-simplification-20261104`. E: is absent. No new C: project files or duplicate checkout created.

Current core release remains `D:/Elrond/releases/paper-core-14352cc-20261015`, release identity `5cee6eb...`. Existing staged three-file repair is not installed. New source commits are not the active deployment. Source rollback is Git; runtime rollback additionally requires exact preserved manifests/configuration and audited authority transition. A Git reset alone is not a deployment rollback.

After-hours code transition can preserve a fence, but current explicit resume requires fresh in-session evidence. No safety fence was bypassed or left newly engaged. Required before completion: safe cutover and one-writer verification, preserved-history backup/restore validation, historical receipt projection and cleanup, observed growth/latency/recovery comparison, complete cycle explanations, fee/calendar gaps, validated crypto design/execution, and reproducible economic evaluation. Profitable paper performance has not been demonstrated.

Agent use: at most three workers concurrently, one owner per file scope, no nested agents, no extra worktrees. Completed workers stop; followups are narrowly scoped and bounded. Account usage readings are rounded account-wide values, not task-specific metering.
