# Verified PAPER execution and scheduled news-learning release

Verified 2026-11-06T23:11:54.169408+00:00 on Leannes-Laptop. This replaces the earlier generation37 status.
Core source **d6278971c704bdc69d0bc167fe687c21fa3a5126**; runtime **c0966c23a13141146b14e1560ee443173b83ba0a88eb29d6fc638fb5b4a194b3**;
402 executable pins verified. Console **6453a770921f122bdb0c4dce8bf7c014d2a5b499** (health optimization source d773baa). Account lease **38**.

## Current running result

One authoritative Trader PID5452, Guardian15680, Outcomes32744, Research15892 and Console32124.
The five existing tasks run; old Protection/Deadline tasks remain disabled. Python venv launchers
and their base-interpreter children are one process tree per component, not duplicate engines.
The existing authoritative PAPER ledger remains D:/Elrond/state/quantbot-paper-fresh-20260908.db.

The new 23:10 UTC evaluation contains BTC/USD, ETH/USD and SOL/USD, with quote/trade provenance.
Its retained decision is WAIT: CRYPTO_COST_AWARE_SIGNAL_NOT_QUALIFIED. The cycle now reports
NO_QUALIFYING_CANDIDATE instead of mislabeling that strategy rejection as a risk failure.
The running engine has fresh heartbeats and no current authority blocker. Six dashboard routes
returned HTTP200, including the new learning panel and matching /api/state metadata.

Retained ledger:154 completed round trips,336 fills. Crypto:1 intent,0 broker orders,0 fills.
That ETH intent at22:05 UTC was definitively unsent: its independently sourced quote expired
before final authorization. The five-second quote/account freshness requirements remain enforced.
No order was forced to demonstrate activity. No live endpoint/order authority was enabled.

## News is scheduled; improved decisions are not demonstrated

At 2026-11-06T23:14:05.756093+00:00: 6557 NEWS_RESEARCH_V1 documents retained.
Research also retains SEC company evidence; those documents are not counted as news articles.
Current-head collection is bounded and separate from historical backfill. Alpaca's existing
no-additional-charge entitlement was confirmed by the operator. X/Reddit remain unavailable;
no paid access or scraping was added.

The scheduled Outcomes worker actually wrote state **COLLECTING** at 2026-11-06T23:08:25.825658+00:00.
Binding **52db71da3c8376c67a0d03959c4ebfc0867e21e027dbc4aa145bfd51f2b43d24**. Plan **fb5b627b3469daeec57a87414c205268b07bf6b9c494d9435a47c7f5fe149815**.
The existing experiment journal was moved intact to D:/Elrond/learner/adaptive-paper-research-20261006/experiment.db;
its old C file was not retained as a duplicate. Canonical fit charges remain **2 of6**, in the
existing MAIN ledger; commissioning did not reset or spend the budget.

Observed scheduled counts: snapshots journaled into this prospective plan0; linked outcome feedback0;
independent reviews0; eligible reviewed examples0; candidate_id null; interpreter_version_id null.
There is no authentic fitted news candidate and no active context trading filter. Current price
model inputs remain price-only; price model mode COLLECT is a nonpredictive baseline, not a fitted edge.
Earlier observed news snapshots and lexical interpretations are evidence, not proof of learned behavior.

Exact current gates: **INDEPENDENT_REVIEW_UNAVAILABLE** and
**PREDECLARED_EVALUATION_CUTOFF_NOT_REACHED**. Training starts2026-11-07T06:00:00Z;
chronological split2026-11-14T06:00:00Z; evaluation cutoff2026-11-21T06:00:00Z.
Independent reviews must reference the exact retained reading/document, provide an independent
semantic correctness label with provenance, and be recorded at their actual import time.
Wins/losses cannot be substituted for reading correctness or backdated into the training window.
The semantic review importer exists, but no automatic independent semantic-review producer is scheduled.
Its authentic inputs currently require an independent human/source-correction review with provenance;
there is no authorized paid review API or commissioned reviewer. Existing local Ollama/Qwen
artifacts have now been found; bounded local capability verification/producer implementation is in
progress without new downloads, charges or a persistent model service. This still blocks deployed
semantic calibration until that separate change passes tests and commissioning.
An explicit untrained lexical baseline for autonomous outcome-based impact learning is now being
implemented separately, so outcome learning need not depend on inventing semantic review labels.
Source permissions, first-seen times, protected history, sufficient eligible chronological examples
and the canonical six-fit limit remain enforced. Cutoff arrival alone does not grant fitting.

The production path journals fresh eligible captures and verified completed outcomes, performs
registered semantic evaluation after cutoff when evidence qualifies, permits only a better
reading holdout to enter SHADOW_ONLY, preregisters prospective paired cost-aware impact evaluation,
and requires explicit fenced approval plus held-out evidence before any TRADING_FILTER use.
No automatic trading promotion is enabled. A filter can veto an existing price BUY; it cannot
create a trade, override price WAIT, or bypass execution/risk controls. Pending, quarantined or
superseded outcomes suspend labels; selected outcomes are revalidated in the current canonical
transaction at promotion/final send. The same declared sample cannot be refilled around a bad label.
This path is implemented, scheduled and genuinely data/review-gated. Profitability is unproven.

## Tests and database integrity

Final combined core/context/critical integration gate: **528 passed**,255.68s.
Non-context learner suite: **852 passed**,233.98s. Console/operations:396 passed; two native
Windows service tests initially rejected a development-only interpreter, then **2 passed** under
the actual stable host interpreter,51.53s. Test cleanup regressions: **16 passed**,10.50s.
All successful final synthetic scratch was removed. No operating database test copies were made.
Ruff passed; mypy found no issues in all19 changed Python source files; git diff --check passed.

Example commands (PYTHONPATH=D:/Elrond/App/src, plugin autoload disabled, TEMP/TMP on D:/Elrond/scratch):

```powershell
python -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider tests/unit/paper_core <all tests/unit/learner/test_context*.py> tests/unit/learner/test_experiment_store.py <crypto_paper_protocol, continuous_capital, crypto_resume, crypto_review_regressions, offline_asset_capabilities, final_repair, costed_loop integration files> --basetemp=D:/Elrond/scratch/context-final-release-gate-20261106
python -B -m pytest -q -p pytest_asyncio.plugin -p no:cacheprovider <non-context tests/unit/learner/test*.py> --basetemp=D:/Elrond/scratch/context-learner-noncontext-final-20261106
```

The angle-bracket lists above describe the actual expanded file sets, not literal shell syntax.
Exact logs remain D:/Elrond/review/context-final-release-gate-20261106.log,
context-learner-noncontext-final-20261106.log, context-console-final-20261106.log,
context-host-runtime-clean-final-20261106.log and context-cleanup-root-final-20261106.log.
These are broad targeted suites, not a claim that every repository test/coverage measurement ran.
The full read-only ledger quick_check(1) returned **ok** in445.796s. Two shorter bounded attempts
expired first; no corruption result was suppressed. This full scan was manual release validation,
not recurring trading-loop work. Schema remains37; no migration or historical trade deletion occurred.

## Architecture and measurable changes

Before: existing single PAPER execution core and independent safety/accounting workers;
Research used an older source; context candidate APIs lacked production scheduling.
After: the same five components and ledgers; Research shares sealed authoritative source;
Outcomes also schedules bounded context feedback/evaluation. No new framework/service/database
or production dependency was added. Duplicate quantity hydration and inconsistent readiness/
cached-discovery paths were consolidated. No confirmed module/dependency deletion is claimed.

|Metric|Before eed788b|Current core release|Result|
|---|---:|---:|---|
|Tracked Python source files|385|400|+15|
|Maintainable source lines|107203|115877|+8674; not a reduction|
|Production dependencies|13|13|unchanged|
|Application processes|5|5|unchanged; one Trader|
|Source diff lines removed|—|338|not all dead code|
|Source diff lines added|—|9670|new execution/learning/safety functionality|
|Operating DB test copies|—|0|synthetic fixtures only|
|Comparable startup, CPU, peak memory, cycle/query/API volume, recovery benchmarks|unavailable|unavailable|no invented improvement|
|Critical test coverage percentage|unavailable|unavailable|no coverage tool measurement|
|Database lock incident rate|unavailable|unavailable|no comparable monitoring interval|

Nonblank non-comment tracked src/**/*.py lines are counted, including docstrings.
The main ledger remains approximately56.7GB with no freelist pages. This release did not compact it
or demonstrate that all historical proof/event retention is necessary. The broad source-reduction
objective is not complete. Additional historical dead-code/storage work requires usage evidence.
Working-set point samples were585,773,056 then751,996,928 bytes across five processes; these are
not comparable peak-memory measurements (the later Console was serving the smoke-test pages).
Observed single Trader heartbeat cycle2.125s is not an average-latency improvement claim.

Dashboard smoke timings were8.609–10.063s for HTML and4.688s for /api/state. Separate bounded
profiling found health decoding about188,000 full run records merely for a latest timestamp;
the Console-only indexed health fix is now deployed (340 Console tests passed, root26-test
verification passed). Post-deployment smoke probes: /api/state1.172s, homepage3.234s, bothHTTP200.
EXPLAIN QUERY PLAN confirms ix_runs_started_at. All187,961 run records remain stored; health
no longer materializes them merely to obtain one timestamp. These are individual smoke probes,
not controlled average-latency or peak-memory benchmarks. Core PID5452 and lease38 stayed unchanged.

## Reliability, accounting and cutover

Changes include native crypto terms/custody/non-marginable buying power, explicit cost screening,
quote-derived zero-volume crypto bar provenance, scoped stock/crypto capital checks, causal
retained discovery comparisons, bounded current-news polling, versioned prospective consumption,
immutable modeled-vs-actual labels and outcome correction invalidation. Data integrity, cash,
ownership, duplicate prevention, paper isolation, emergency stop, time and current-quote checks remain.
Actual fees remain unknown where the broker evidence cannot allocate/finalize them. Estimated
cost scenarios remain labeled estimates. The prior verified daily report showed7 Nov6 round trips,
gross0.065072990USD, unknown all-in actual net; matching retained cash495.29USD is not profit proof.

Cutover fenced entries, disabled/stopped all five old process trees, installed checked exact
patches, verified402 pins, transitioned the same economic identity, commissioned the prospective
plan, then restarted safety/research/dashboard workers. First lease binding required a newer flat
proof; first resume refused an outdated Guardian snapshot. Fresh ordinary approvals succeeded;
no check was bypassed. One Trader then restarted and produced the new real evaluation.

Rollback source archives, config/manifest, task XML and instructions remain
D:/Elrond/deploy/crypto-paper-20261106/news-learning/ROLLBACK.md. Rollback uses a new forward lease
generation and fresh guarded cutover, preserving all ledgers/fit charges; restoring an old ledger
is not a code rollback. No destructive restore was rehearsed against the operating ledger.

## Remaining limits and blocked cleanup

Profitability, fee-finalized net returns, a fitted news edge and broad code-size reduction are
not demonstrated. Independent semantic reviews and prospective held-out evidence remain required;
X/Reddit are unavailable. Desktop-to-laptop remote dashboard access was not independently tested;
HTTP verification was on the authoritative laptop.

Automatic approval review rejected exact deletion commands with “blocked by policy” for
D:/Elrond/backups/pre-completion-20261105.zip, D:/Elrond/scratch/crypto-noncore-final-20261106,
and D:/Elrond/scratch/signal-diagnostics-red-20261106. These were not deleted through alternate means.
The newer current-ledger-20261105.db backup remains readable (same store identity/schema37,
147 retained round trips). Thus redundant backup cleanup is not fully complete; no new ledger
backup/copy was created by this release.

Evidence: [running verification](running-verification.json), [one-engine/task proof](process-verification.json),
[ledger integrity](ledger-readonly-check-final.json), [source metrics](measurements.json).
