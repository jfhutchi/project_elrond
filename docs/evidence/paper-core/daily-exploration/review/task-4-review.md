### Spec Compliance

- ❌ Scoped Task 4 source issues found: a partially published first BUY does not take the required source-bound retry path (`src/quantbot/paper_core/guardian.py:131`; Important I1 below). The fractional timestamp repair, durable COLLECT acknowledgement branch, combined witness and Task3 cleanup otherwise match the delegated source scope.
- ⚠️ Cannot verify from this task diff: current documentation, durable evidence/source hashes, successor config/manifest and publication are controller-owned pending work. The eight-file source package contains none of those deliverables; this verdict does not certify Task 4 as a whole or successor authority.
- ⚠️ Cannot verify from this task diff: the combined witness itself has no OS-process restart or cap scenario (`tests/integration/paper_core/test_daily_paper_loop.py:414`). The retained gates include unchanged restart/cap controls from earlier tasks; the controller must link those results in the final whole-branch evidence. PostgreSQL, platform deployment and natural broker acceptance remain explicitly untested.

### Strengths

- `tests/integration/paper_core/test_daily_paper_loop.py:414` exercises real selection, current-funds continuation, durable outcomes, the bounded fitting subprocess, commissioning and activation. Sixty gross outcomes across ten sessions include both signs and symbols, while actual fee/net fields remain unknown (`:447`, `:471`). Day eleven checks signal priority and negative-prediction exploration with exact model/receipt/activation attribution.
- `src/quantbot/paper_core/guardian.py:70` binds the new wait predicate to the current content-addressed acquisition, account, release, PAPER endpoint, capture interval, watermark and baseline; indexed owned-order bindings and all-symbol quantity equality constrain the exception. The first-BUY controls preserve the failed cash proof and deny certificate/entry authority, including prior integrity preservation (`tests/integration/paper_core/test_daily_paper_loop.py:223`).
- `src/quantbot/paper_core/learning.py:408` adds a COLLECT-only branch with durable receipt mapping/content validation, past completion and availability, exact ACKNOWLEDGE/authority/previous-activation bindings and target/procedure compatibility. The PREDICT branch remains unchanged. Valid-content temporal/target/model fault controls are present (`tests/integration/paper_core/test_daily_paper_loop.py:318`).
- `src/quantbot/brokers/alpaca.py:128` preserves fractional wire timestamps; `tests/contract/test_alpaca_paper_broker.py:491` checks both activity consumers. Existing whole-second encoding remains the same.

### Issues

#### Critical (Must Fix)

- None found in the reviewed source scope.

#### Important (Should Fix)

- **I1 — Historical basis lookup fails when the first BUY is only partly published.** `src/quantbot/paper_core/guardian.py:131` calls `store.latest_proof()` after computing the exact missing quantity. For an acknowledged BUY of 5 with only a 2-unit activity published, canonical import correctly revokes the prior full cash certificate. The failed current proof has expected quantity 2, actual quantity 5, and the exact source-bound acknowledgement explains the missing 3. Nevertheless, `latest_proof()` rejects the old certificate at `src/quantbot/paper_core/store.py:119`, raising `CASH_AUTHORITY_REVOKED_RECONCILIATION_REQUIRED` before the helper can return true. `synchronize` consequently misses its account-scoped `OWNED_FILL_ACTIVITY_PENDING` path (`guardian.py:286`); `guardian_cycle` treats the result as `INVENTORY_MISMATCH` (`guardian.py:381`). This is a genuine publication delay that the new partial-quantity predicate explicitly admits (`alpaca_adapter.py:300`), so the repair is incomplete. Read the prior certified proof as historical basis evidence within the existing read transaction, without requiring or restoring its current cash authority, and preserve all source/quantity/basis/integrity guards. Add a partial-first-BUY regression covering the wait, no certificate or entry authority, and genuine subsequent fill recovery.
- **Reproduction:** exactly one focused external-provider-only probe, no production patch or suite rerun: `.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py C:/Users/leepa/AppData/Local/Temp/task4-review-partial-buy-37f519e213f5452a9a17fd1d35176af6/test_partial_buy.py` → **1 failed in 5.87s**, expected `TimeoutError(OWNED_FILL_ACTIVITY_PENDING)`, actual revoked-cash `StateConflictError`. The provider splits the genuine 5-unit fixture execution into 2+3 activity rows and withholds only the second; real acquisition/import/database/reconciliation code runs unchanged. Retained exact probe: `.superpowers/sdd/2026-10-14-daily-paper-exploration/task4-review-partial-buy-probe.py` (SHA256 `555c524398c20654dfe795a74b8f7da0470bf0d9df9145d449d527d9f2131226`). Full retained result: `task4-logs/review-je2mjqfi.txt` (SHA256 `df8e6cb84cc86b125d2d60ce97a4ebc9d570024756026bb4cd244c41e275c01f`).

#### Minor (Nice to Have)

- None.

### Checks and Evidence

- Reviewed the provided `d2d3503baa249ac669290a5bfd3c45d232e980d8..8bae89e164b8831eba86ec0dde4f7f7abb7aa1ea` package once in three non-overlapping partitions (1–390, 391–790, 791–1140). No git commands, source edits, operating resources, credentials, env files, network, CI or push operations were used.
- Focused surrounding-code checks were limited to named risks: cut guardian synchronization/catch context and prior-integrity semantics (`guardian.py:140`, `health.py:14`); acquisition/cursor and owned-ack provenance (`alpaca_adapter.py:236`, `outcomes.py:799`); COLLECT receipt/content mapping and predictive-boundary preservation (`activation.py:103`, `:371`, `learning.py:374`, `:465`); no-certificate/funds and partial-first-BUY behavior (`store.py:108`, `:131`, `:187`, `:412`, `funds.py:59`). The last check produced I1. No further tests ran.
- Read retained production RED evidence for fractional activity (`task4-logs/79wwzdg4.txt`), publication lag (`32l6eq5o.txt`), historical COLLECT (`o470awmk.txt`) and first-BUY lag (`v3iro1m6.txt`), plus their reported focused GREEN records. The production failures are distinguishable from the disclosed fixture-development failures.
- Retained final affected gate `task4-logs/bsjgrc12.txt:21`: **180 passed in 332.24s**, no warning section or failure. Its actual witness and `task4-witness.json` agree on candidate `3800155d604f532ffe95967d481eb681d90b2e6e0e4de353a2e4993942cdb6d0`, receipt `92fdbf65ca7322dbf035b737cf035bdaa8b1b9fb8ac41e370cce8ff8cbe64b9b`, activation `1eeb078963839119478eb55197ba00b87a3442c2a5b2478f806ebc03ca950833`, 60 training outcomes, nine warmup receipts, 62 closed outcomes, 124 provider-shaped orders, zero cash activities and null actual fee/net fields.
- Retained broader pre-first-BUY gate `task4-logs/lyea33w3.txt:149`: **328 passed, 1 failed** at the existing process test's seed assertion before child-process startup. Exact unchanged focused rerun `task4-logs/hzoihh6t.txt:2`: **1 passed in 14.01s**. The original failure remains a qualification; the broader gate is not represented as all-green or as covering the later first-BUY amendment.
- Retained `task4-lint.txt:1`: **All checks passed!** Retained `task4-mypy.txt:1`: **Success: no issues found in 5 source files**. No suite was rerun to duplicate this evidence.

### Assessment

**Task quality:** Needs fixes.

**Reasoning:** The source changes and real-component witness are focused and strongly evidenced, but the new missing-activity classification depends on a current-authority accessor when it needs historical basis. I1 must be repaired and covered before this source gate can approve; controller-owned deliverables and the final whole-branch review remain separate.
