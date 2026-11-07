# Project Elrond — verified PAPER repair and autonomous news-learning release

Verified through **2026-11-07T00:35:33.701238+00:00** on **Leannes-Laptop**. This is the current generation40 report, replacing generation39.

## A. Executive result

Deployed feature source **becd2f2f88968fd5613aaec9304978df0133c8ed**, dependency patch source **b2f577c000d6507b5efdd4a7cbf84750e7504456**, runtime **d8519ab91155b0e3af388a5f0d7034a7d730f88ba73542322c2d88d6b519c353**, with **403 executable pins verified**. Console deployed commit **a4e397186fd5e9b71385d08b5a00cb4446c9ac87**. Account lease **40**.

One Trader34352, Guardian12308, Outcomes33608, Research576 and Console16192 use the established deployment. Five existing application tasks run; old Deadline/Protection tasks stay disabled. Venv launchers and base Python children are one process tree each. No new persistent service, production dependency, operating database, model download or paid API was added.

The ledger retains **154 completed round trips,336 fills and195 outcome revisions**. Crypto has **1 intent,0 broker orders,0 fills**. That ETH intent was definitively unsent at22:05UTC because its independent quote became stale before final authorization. No demonstration trade was forced.

The latest **00:35UTC Nov7** evaluation actually contains BTC/USD,ETH/USD,SOL/USD. Its retained WAIT reason is **CRYPTO_COST_AWARE_SIGNAL_NOT_QUALIFIED**; the Trader then reports no authority blocker. Stock discovery gaps remain separately scoped. Profitability is unproven; no profitable-day claim is made.

## B. Architecture comparison

Before: single PAPER execution core with independent Guardian/Outcomes; Research had an older source; context fitting APIs were not scheduled and semantic reviews had no producer.

After: the same five persistent components. Research shares the sealed core source and also schedules one bounded local teacher task. Outcomes schedules context evidence, semantic evaluation and paired price-vs-context studies. A local model is started on demand in an owned Windows process job, then terminated/unloaded; there is no persistent model listener. Source/news, semantic calibration, outcome evaluation and trading authority remain distinct.

Execution path: dynamic market discovery/data -> price signals -> portfolio/risk -> one paper order coordinator/ledger -> outcomes/learning -> read-only dashboard. Research supplies immutable observed news, prospective shadow features and separately tagged teacher opinions. No news/model output can submit an order directly.

## C. Removal and consolidation report

**No entire module or production dependency was deleted without usage proof.** Source diff versus eed788b: **365 lines removed**, **10928 added**. Removed lines are not all dead code. Services eliminated in this release: **0**; production dependencies remain13.

Consolidations removed duplicate quantity hydration and inconsistent cached-discovery/readiness behavior. Research now executes the authoritative sealed source instead of its older source copy. The existing experiment journal was relocated intact from C to D, not cloned; its manifest/history and canonical trial burden were preserved. Health now reads one indexed run timestamp rather than decoding about188,000 complete records.

The broad source-reduction/dead-code-purge objective is **not complete**. This repair adds required crypto/learning/provenance code; it is not a net code reduction.

## D. Implemented refactoring and learning behavior

- Native crypto terms, paper-only eligibility, custody/coin-fee handling and non-marginable buying power; stock paths remain isolated. Quote-derived zero-volume crypto prices retain explicit provider provenance rather than being rejected as fabricated carry-forward.
- Readiness reads its evidence in one transaction. Short capital-publication gaps retry only within the original signal window. Current quote/account freshness, emergency stop, ownership, duplicate prevention, exposure, time and paper-isolation checks remain enforced.
- Current news is collected before historical backfill. Unchanged context captures are reused; observed, shadow-used and trading-used evidence are separate.
- An explicitly **UNTRAINED_FIXED_LEXICAL_BASELINE** now enters **SHADOW_ONLY** prospectively. There is **1 authentic fixed-shadow preparation receipt**, not a learned candidate or proof of decision improvement.
- Outcome-based paired evaluation no longer requires semantic-review labels. Only current, owned, reconciled outcomes enter studies; pending/quarantined/corrected sources suspend old labels and comparisons. Original chronological sampling cannot be refilled around an inconvenient label.
- Semantic calibration has an actual scheduled producer using the existing hash-pinned **qwen2.5:3b** and Ollama0.33.3. Inputs are retained permitted text/excerpts and the target symbol, without price, outcome or baseline-polarity inputs. Records retain prompt/source/model/response hashes, exact quote, confidence, uncertainty and excerpt coverage. Labels explicitly say **MODEL_GENERATED** and evaluate **MODEL_TEACHER_AGREEMENT_V1**, not human truth.
- Local reviewer bounds: one attempt/hour, one pending task, two CPU threads,2048 context,128 response tokens,60-second maximum, loopback/no-cloud, credential-free child environment, D-only profile/temp and keep-alive0. Owned crash leftovers recover under a lock; unknown artifacts/junctions remain untouched. There is no model listener running now.
- A better held-out semantic candidate may automatically replace the fixed shadow baseline. Semantic evaluation has priority before spending baseline paired-fit calls. Trading-filter promotion still needs explicit fenced approval, valid held-out cost-aware evidence and current canonical outcomes. A filter can veto a price BUY; it cannot create a BUY or override risk checks.

## E. Measurements

|Metric|Before|After|Interpretation|
|---|---:|---:|---|
|Tracked source Python files|385|401|+16|
|Maintainable source lines|107203|117022|+9819; not a reduction|
|Production dependencies|13|13|unchanged|
|Persistent application processes|5|5|one Trader|
|API state smoke probe|4.688s|1.172s|75% less elapsed time in these individual probes|
|Homepage smoke probe|9.500s|3.234s|66% less elapsed time in these individual probes|
|Twelve-trade synthetic outcome revisions|78|12|84.6% fewer, same12 economic trades|
|Operating ledger test copies|—|0|synthetic fixtures only|
|Startup/CPU/peak memory/average cycle/recovery/query/API volume benchmarks|unavailable|unavailable|no controlled comparison|
|Test coverage percentage|unavailable|unavailable|not measured|
|Database lock incident rate|unavailable|unavailable|no comparable monitoring interval|

Source counts use tracked src/**/*.py, nonblank/non-comment lines including docstrings. Smoke timings are individual local requests, not average-latency benchmarks. EXPLAIN confirms the existing ix_runs_started_at covering index. All187,961 run records remain stored. Historical working-set samples were not comparable peak-memory measurements; no memory percentage improvement is claimed.

The primary ledger is still about56.7GB. No further primary-file compaction or source-wide disk reduction is claimed. Changes stop unnecessary future work/revisions; they do not erase historical trade/evidence records.

## F. Reliability root causes and corrections

The full scheduled synthetic protocol exposed provenance-only revision churn: ordinary later spending revoked a current cash snapshot, and the historical outcome resolver incorrectly treated that as retracting an earlier witnessed lot closure. Proof IDs/timestamps changed despite identical economic fills/costs. Historical proof selection now retains validated preferred evidence for unrelated accepted source changes; unknown revocations, same-lot corrections and quarantine remain fail-closed. Current spending revocation is untouched. Twelve closed lots stay at12 revisions; a seeded legacy rev2 stays current (13 total), never rolling backwards. Genuine fee corrections still advance revisions. Production currently has154 trades/195 revisions; the synthetic78-to12 result is not presented as a measured production incident-rate reduction.

Other corrections cover partial crypto fee custody, unsent-claim retirement, scoped stock capital, current quote/trade provenance, precise session/data/risk reasons and resource cleanup. Strategy rejection now reports NO_QUALIFYING_CANDIDATE rather than a risk failure. Transient Guardian acquisition failures observed after restart recovered automatically; checks were not suppressed.

Actual fees/all-in net remain unknown where provider evidence is not finalized/allocatable. Posted daily fees and modeled scenarios are separate. The last verified daily report showed7 Nov6 stock/fund round trips and gross0.065072990USD; that is not verified net profit. New research contracts declare stock10bps and crypto60bps round-trip estimates (50 fee estimate+10 slippage for crypto), explicitly not actual fees. Legacy50-bps contract IDs remain intact.

## G. Tests and integrity

Final broad core/context/critical integration gate: **545 passed**,287.07s. Final Console suite: **341 passed**,108.51s. Last owned-review lifecycle patch: **10 passed**,2.45s, including actual Windows junction refusal, lock ownership and crash recovery. Prior non-context learner suite: **852 passed**,233.98s. Native service wrapper/installer tests: **2 passed** under the actual stable host interpreter. Cleanup regressions: **16 passed**. Earlier development-interpreter service refusals were reproduced and correctly preserved; host tests passed without weakening the path guard.

Ruff passed; mypy passed all12 Python sources changed in the final stage (the preceding19-source stage also passed). Successful final synthetic scratch was removed; no operating DB was copied for tests. These are broad targeted suites, not a claim that every archive/platform test or coverage analysis ran.

Actual commands used Python-B, pytest-q, pytest_asyncio.plugin, disabled plugin autoload/cache, explicit D:/Elrond/scratch basetemp. Exact expanded command construction is in [test-commands.md](test-commands.md). The core command expands tests/unit/paper_core, all learner/test_context*.py, experiment_store and crypto/continuous-capital/resume/review/offline-capability/final-repair/costed-loop integration files. Full logs:

- D:/Elrond/review/autonomous-news-final-release-20261106.log
- D:/Elrond/review/autonomous-news-console-final-20261106.log
- D:/Elrond/review/autonomous-review-lifecycle-final-20261106.log
- D:/Elrond/review/context-learner-noncontext-final-20261106.log
- D:/Elrond/review/context-host-runtime-clean-final-20261106.log

Full read-only primary-ledger quick_check(1) returned **ok** in445.796s after two shorter attempts expired. This was manual release validation, not recurring work. The research journal also returned **ok** after commissioning. No schema migration occurred; schema remains37.

Actual local model smoke: initial prompt without numeric sign convention produced a confident wrong sign. With the final explicit convention, positive/negative examples returned correct directions and exact quotes in11.797/12.469s; listeners closed. This proves local capacity/direction/lifecycle on those synthetic examples, not broad semantic quality or calibrated confidence.

The final dependency patch passed **78 focused regression tests**,35.28s, after installing the patched packages. See D:/Elrond/review/security-dependencies-final-20261106.log. Both actual runtime environments passed dependency compatibility checks; installed-version and compromised-root checks are in [installed-verification.json](installed-verification.json).

## H. Deployment and rollback

Controlled cutover fenced new entries, stopped/disabled all five old task trees, verified quiescence, applied checked exact component patches, sealed403 pins and transitioned the same economic identity. Guardian/Outcomes/Research/Console started first; fresh proof/time/readiness then bound lease40 and accepted resume; one Trader started. A stale Guardian approval was refused before a fresh ordinary attempt succeeded. No manual stop was cleared or guard bypassed.

Core source becd2f2f88968fd5613aaec9304978df0133c8ed is a tested component delta into the established flat deployment D:/Elrond/releases/paper-core-14352cc-20261015 (not a separate Git checkout). Console is pinned toa4e397186fd5e9b71385d08b5a00cb4446c9ac87. Research shares the core interpreter/source/seal. Six current dashboard routes returnedHTTP200 with matching learner metadata. No older engine remains active.

Prospective teacher policy **e7157471e027df8fbc4e23e3939ef21cb40e90b5561e5ca9d894d5c3ac56454b**; current binding **543903c9a7116593f34563755144b263a0de5c212e9370f5febed8b77fbb5dcd**; plan **9f93e6894e8bf871448b5a4e26ed9ed9fd85ea338f344c71a7ce965fa3df8b49**. The old human-review plan was superseded before training, with unchanged windows/parent and no prior context fit. The old plan/history remain. Canonical charges remain **2 of6**; commissioning spent0 and promoted no trading model. New cost contract is versioned; old contracts were not rewritten.

Exact source/config/manifest/task rollback artifacts remain D:/Elrond/deploy/crypto-paper-20261106/autonomous-news. See ROLLBACK.md there: the older source cannot parse new context metadata. While the current interpreter is SHADOW_ONLY, archive its operational metadata into the same research journal and suspend optional context pointers before restoring older code; preserve all plans/labels/fit charges. Never use that suspension procedure for an approved TRADING_FILTER. Code rollback must fence/stop the same tasks and use forward lease generation41 or later, fresh sealed source and Guardian evidence; do not restore an old lease or ledger and discard subsequent paper history. Keep the patched dependency versions during any rollback; do not restore vulnerable dependency locks. Restore a verified compatible Console predecessor only through its stopped task. No destructive restore was rehearsed against the operating ledger.

The final dependency-only cutover repeated the fenced stop/verify/start/bind/resume procedure, advanced lease39 to40, retained the existing schema/strategy/plan and verified all403 pins. Actual Core and Console environments now use certifi2026.10.19 and opentelemetry-api1.45.1. The former removes the compromised certificate authority described in [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2); the latter fixes the unbounded tracestate parse in [GHSA-xcq7-p3pf-p88h](https://github.com/advisories/GHSA-xcq7-p3pf-p88h). Dependency count remains unchanged. Small dependency/config/task rollback artifacts are under D:/Elrond/deploy/crypto-paper-20261106/security-dependencies; no additional ledger copy was made.

## I. Exact current gates and remaining risks

At 2026-11-07T00:35:33.701238+00:00: **10482 actual news documents**, independent human/source reviews0, model-generated real reviews0, eligible semantic examples0, fitted news candidates0, paired studies0, actual news trading-filter promotions0. The current price model is COLLECT, a nonpredictive baseline; its latest daily evaluation reports VALIDATION_GATE_FAILED. One real fixed shadow receipt is preparation evidence, not training or trading influence.

The scheduled producer actually reports **DECLARED_TRAINING_WINDOW_NOT_STARTED**. Training starts **2026-11-07T06:00:00Z** (Nov7,1am Eastern); split **2026-11-14T06:00:00Z**; semantic/fixed-baseline evaluation cutoff **2026-11-21T06:00:00Z**. Producer and learner are installed, commissioned and scheduled. No human review/input or new paid service is needed for their automatic path. Eligible permitted sources, sufficient chronological teacher examples and actual completed outcomes still must accumulate. Cutoff arrival alone grants neither fitting nor promotion. Better semantic models get a separate future prospective impact window before trading approval.

OPTIONAL_SEMANTIC_REVIEW_UNAVAILABLE means fixed-baseline outcome research does not depend on semantic labels; semantic calibration itself now has an automatic producer. Teacher agreement is not truth or profitability. Profitability, fee-finalized net performance and improved decisions are unproven. X/Reddit remain unavailable; no paid access was added. Remote Alienware dashboard access was not separately exercised; HTTP checks ran on the laptop. Broad dead-code elimination, net code reduction and controlled CPU/memory/storage benchmarks remain incomplete.

Automatic approval review rejected deletion with “blocked by policy” for D:/Elrond/backups/pre-completion-20261105.zip, D:/Elrond/scratch/crypto-noncore-final-20261106 and D:/Elrond/scratch/signal-diagnostics-red-20261106. These were not deleted through alternate means. The newer current-ledger-20261105.db backup remains readable with schema37/same store/147 trades. Redundant backup cleanup therefore remains incomplete; this release created no new ledger backup/copy.

## J. Evidence-based next optimizations

The separate profiler measured experiment-panel loading around3.4s on HTML routes; avoid constructing it where it is not rendered while retaining verified history on relevant pages. Profile the still-large historical proof/event storage and reference closure before any additional pruning. Measure comparable CPU, peak memory, startup and provider volume before claiming further gains. Evaluate future model versions on the declared held-out/cost-aware data rather than win-rate targets or extra trade quotas.

[Running evidence](running-verification.json) · [process/task proof](process-verification.json) · [source metrics](measurements.json) · [primary integrity](ledger-readonly-check-final.json) · [local teacher smoke](local-teacher-smoke.json) · [health optimization](console-health-verification.json)
