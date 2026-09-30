# Learner market replay implementation plan

**Goal:** Resume the existing learner with protected-safe stored market inputs and a fixed,
after-cost portfolio comparison. User authorized all three recovery/development/verification steps.

**Architecture:** Reuse the learner, SQLite research copy, registry, broker calendar and existing
5 bps per-side research baseline. Keep the operational database at schema 28 and deployed code
at a49203d. The research copy alone may migrate to schema 29. No new provider or scheduler.

**Scope:** Historical exploratory replay of the existing fixed ETF roster. Stored adjusted bars
have no historical receipt timestamps or membership vintages. Close-time availability is an
explicit replay assumption, never authenticated point-in-time evidence. No promotion authority.

## Implementation

- [x] Add failing tests for guarded SQL reads, protected/forward exclusions, missing sessions,
  duplicate provenance, and exact calendar alignment. Implement store dataset and resolution CLI.
- [x] Add failing tests for next-open entries, predetermined fifth-close exits, nonoverlap,
  full coverage, costs on both sides, matched SPY/all-universe baselines and drawdown.
  Implement a fixed top-quintile portfolio report with immutable output and input hashes.
- [x] Exercise mean/ridge and one immutable ridge successor on one predeclared market split;
  retain negative results, record every fit in the copied registry's cumulative trial burden.
- [x] Run learner/protection regressions, Ruff, Linux and Windows mypy targets and applicable
  native Windows suite. Linux execution stays NOT RUN when its runtime is unavailable.
- [x] Commit implementation and a measured handoff. Do not deploy, merge, change tasks or
  clear controls. Record the highest-value remaining prerequisite.

## Fixed economic contract

Equal-weight top quintile (include all cutoff ties), long only, fractional units, no leverage.
First eligible validation cohort then greedily nonoverlapping five-session cohorts. Buy next
session open, sell target close, liquidate fully between cohorts, zero interest on cash. Constant
scores select the entire roster. Apply 5 bps adversely to each fill, zero commissions as the
existing baseline. Compare all-universe and SPY on exactly the same dates and assumptions.
Mark at daily closes; report total net/excess return, close-based drawdown, traded notional,
cost, daily market exposure and descriptive predictive diagnostics. No IID significance claim.
This is an execution simulation without spread/impact calibration or capacity qualification.

## Recovery measured before implementation

Backup: D:\Elrond\backups\learner-recovery-20260921T140713Z.db; integrity_check=ok; schema 28;
lineage cd2a5911-853a-484e-bbc8-d0c27b5d27aa. Last successful cycle September 16. September 21
doctor: PAPER account verified, risk healthy, MARKET_DATA_STALE; reconcile: zero diffs/intents.
Protection at 14:00 UTC: nine evaluated, zero unprotected/exits required. No shutdown-gap
qualification or predictions manufactured. Existing tasks remain unchanged and sign-in dependent.

## Review and verification ledger

Independent review found incomplete-replay coverage and restored-store vintage weaknesses.
Regression tests now remove a whole validation cohort and mutate an entry price; evaluation
refuses both, and resolution also verifies the frozen source snapshot. Full-panel source
verification is deliberately more conservative than checking target closes alone.

Before the first implementation commit: 197 focused learner, registry, configuration and failure
tests passed; Ruff and Linux/win32 mypy targets passed across 244 source/helper files.
The reviewer did not access the operational database or any external service.

The first real 22-ETF replay exposed repeated full-dataset decoding in `_issue`: identical
protected-range checks ran once per symbol. Ruling: check the union once per cohort, because
every input interval contains the same decision cutoff; the union preserves all exclusions.
The interrupted research run retains 572 committed forecasts and its one mean-model fit attempt.
Resume that model through the idempotent CLI, then train the two remaining versions; do not
restart the experiment in a fresh store. Post-change learner tests: 105 passed.

Completed the preserved run: one dataset, three immutable models, 3,696 historical forecasts
and outcomes; all seven prior search records retained plus exactly three fit attempts. Four
report hashes verified. Ridge and successor match across 1,232 observations; neither has
promotion authority. Ridge after-cost return -3.4606%, all-universe -0.1304%, matched SPY
-6.0158%; ridge mean rank IC -0.1643. No positive-expectancy conclusion.

Native Windows CI on 8a78dc3: PASS, 3,062 passed / 47 skipped / one deprecation warning.
Combined local-CI exit 1 reflects Linux NOT RUN (Docker absent), not a Windows failure.
The subsequent 419f545 protection-check optimization passed 105 targeted tests, both full
mypy targets and Ruff, with independent review. Full suite was not repeated for this later
commit. See docs/learner-recovery-2026-09-21.md for the measured handoff and evidence limits.
