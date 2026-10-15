# M5 independent review

Reviewed source: `c83d93faeb1c16a711c307f3b55e81b223e3e9bb`, base `fbdfca23f74bec146d19c630f001786c1123b2a6`. Fresh independent Astra reviewer `/root/m5_review`; integration owner records the verdict here.

## Initial verdict: REWORK / Needs fixes (closed below)

**P1 — V2 timing overwrites model selection identity.** In `src/quantbot/paper_core/trader.py:137`, `evaluate_entry` replaces the dictionary containing activation ID, generation, model digest, prediction and mode when adding `PAPER_TIME_V2` timing. A commissioned model can produce a BUY evaluation with no gates, but claim rejects the missing binding as `CORE_MODEL_SELECTION_STALE`. Predictive history also loses prediction provenance. Merge timing fields into the existing dictionary and prove initialized and genuinely learned activated V2 decisions retain both timing and model authority through production claim/final send. Retain stale-generation/ABA refusals.

Independent isolated reproduction: `C:/Users/leepa/AppData/Local/Temp/m5-review-v2-binding/test_v2_binding.py`, raw log `C:/Users/leepa/AppData/Local/Temp/paper-core-check-u1a6k1le/result.txt`: **1 failed, 1 passed in 4.19s**. Uncommissioned V2 control posts once; commissioning the same baseline produces missing activation/generation/mode, a stale-selection objection and zero posts. Root separately inspected the affected source and raw reproduction output.

## Verified evidence and limits

The reviewer independently verified 25 source pins, 39 original/compressed log pairs, seven exports and six final CI hashes; atomic event/pointer CAS and ABA fencing; canonical receipt/trial qualification; cutoff-aware complete terminal activity and unresolved-submission boundaries; append-only rollback and original lot protection.

The same observation/config/inventory changes WAIT to BUY after the actual 40-input learned model activates. Exact model/run/qualifying-receipt/authority/event joins, prediction19.900497512437546bps, fill arithmetic, NO_CHANGE acknowledgement and candidate-owned lot closure after rollback were checked. The accepted populated-v33 fixture and migration/replay evidence preserve history. Guarded local real-data replay retains seven dates/six completed sessions, warmup NO_CHANGE and null verified net; modeled economics remain explicitly partial.

Results are distinct: original affected gate **2 failed / 386 passed / 26 PostgreSQL skips**; test-only repairs **2 passed**; final pre-review activation/migration gate **28 passed**; six final CI checks passed. This is not a whole-repository green claim. Capacity shows 50 deterministic synthetic trades and8.797s guardian/durable-outcome progress while the actual child fit lock is held, not CPU saturation, natural daily throughput or operational scheduling.

The initial verdict required the focused V2 repair below. No operational activation or rollout is authorized by this review.

## Final verdict: ACCEPT / Approved

The same independent reviewer closed the single P1 at `43629124bdc7952a11e14560adc22853af94a261`; no new actionable findings. `trader.py:137` now merges V2 timing fields into existing model metadata, preserving the original timing expressions and bounds.

The scoped re-review independently verified the exact diff, all25 final pins (only trader and its integration test changed), six repair log pairs, four exports against original markers, six final CI results, and exact AST equivalence for the archived tested source versus a formatting-only line wrap. Both initialized and genuinely learned V2 decisions retain observation/config/inventory, two-sample timing/projection, model/digest/event/generation, conservative protection, durable intent, successful locked final send, exactly one POST and matching filled quantity. The learned candidate retains its actual qualifying receipt/run and19.900497512437546bps prediction. An old generation remains fenced after rollback/reactivation.

Repair evidence: meaningful two-test RED, two-test GREEN, then **61 passed in167.64s** across the four affected activation/timing files, plus six exact CI checks. Earlier assertion and Ruff failures are retained separately. Root independently verified the same artifacts and the final all40 weighted-ridge/trade/rollback evidence. Earlier M5 evidence scopes and limitations remain unchanged.

This accepts local M5 implementation and permits M6 preparation. Operational commissioning, deployment and natural broker-PAPER acceptance remain unverified and separately controlled.
