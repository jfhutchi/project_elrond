# M4 independent review

Reviewed base `c8fbf4d3b1256856aeadf45066ac0371c0f0cd9a` to source `30abc2804e6e32c1d2e411554a7cb74b042d5316`. Independent Astra review: **REWORK**, two Important/P2 temporal-contract findings. This is the initial verdict, not acceptance.

## Findings

1. **P2: receipt completion and availability are backdated to the input cutoff.** `src/quantbot/paper_core/learning.py:477` initializes `recorded_at_utc=cutoff`; final construction at line 621 retains it. Busy receipts at line 421 and recovery from reservation fields at line 383 have the same problem. Canonical `started_at` and `finished_at` at lines 548–549 are both the cutoff. `LearningReceipt` at `contracts.py:479` has no distinct completion time. A historical fit or later recovery therefore cannot establish when its result became available, contrary to the specification and the later activation handoff. Separate input cutoff from explicit execution/start/completion time, supporting injected synthetic time; preserve idempotent replay and stamp recovery honestly. Verify delayed fits and later recovery.
2. **P2: a future input cutoff prematurely expires protected reservations.** `learning.py:246` and the pre-materialization recheck at line 584 pass `cutoff.date()` as canonical registry lifecycle time. The existing predicate at `research/registry.py:2106` drops RESERVED claims whose expiry precedes that supplied date. A caller can therefore bypass a still-live protected or forward-PAPER reservation without operational broker authority. Use authoritative execution time independently of data cutoff for both checks; add future-cutoff/live-reservation materialization canaries.

The integration owner verified both source chains and accepts both findings for one scoped repair round. No wider registry lifecycle redesign is required. Original immutable payloads, IDs, evidence and permanent charges must remain preserved.

## Verified strengths and evidence

The reviewer independently checked the 40 unique costed joins, 30/10 split across five plus five sessions, windows/embargo, observation/decision/outcome IDs, fill/fee arithmetic, protection/reconciliation links, saved-parameter predictions and MSE `0.017543090215340434`. Metadata checks precede materialization and canonical permanent charging precedes fitting. Admission/worker locks, permission cancellation and the child watchdog cover separate crash boundaries; automatic Windows exit 124 is asserted by the process fixture. Populated-v32 replay and conditional serialization preserve predecessor identities. Candidates remain unactivated.

The reviewer checked the exact diff package hash/size, all 14 source pins, five raw/compressed export hashes, 28 packaged log hashes and six final CI logs. The frozen affected gate reports **508 passed, 26 skipped in 590.12 seconds**. No test suite or reproduction was rerun during review. Focused outside-diff inspection covered process lock lifetime, finite estimator output, outcome-index provenance, canonical reservation lifecycle/aliases and clock interfaces.

No PostgreSQL execution, operational runtime, broker-PAPER validation or profitability is established. Review made no file or operating-state changes. Root writes this report from the independent verdict.

## User amendment during repair

The user subsequently requested daily learning after ten completed trading sessions and explicitly selected training on all eligible history with greater weight on the latest ten. The updated authoritative specification supersedes the old 60-session/fixed-split fitting envelope. Root chose fixed 2× recent/1× older weights normalized to mean 1. A design-only independent review conditionally approved five expanding chronological folds, historical as-of revisions and contemporaneous incumbent-policy comparisons, followed by an all-history refit after procedure gates. Fold validation is not an independent held-out score for that final refit.

The original two temporal findings remain required repairs. The same reviewer also confirmed that an immutable canonical charge row may truthfully use actual charge start=end time with explicit pre-fit charge semantics; actual fit completion belongs to the linked receipt. Re-review will cover those repairs **and** the amended learning policy, numerical evidence and changed shared interfaces. Design approval is not implementation acceptance; original evidence above remains a historical result.

### Integration-owner check at the expanded history boundary

While reviewing the amended all-history selection, root identified another concrete eligibility seam: `outcomes._resolve` refuses a lot with unresolved source-evidence quarantine, but `process_outcomes` retains that lot's earlier COMPLETE pointer when resolution fails. Original M4 selection checked the pointer's status without checking current lot quarantine. New fitting could therefore reuse disputed data. The implementer has been instructed to exclude implicated lots from current and historical-as-of selection and recheck immediately before materialization, using incident metadata without reading full source-event payloads. Unrelated healthy lots and old immutable artifacts must remain available. A completed-pointer/later-conflict canary is required; this is an owner finding pending implementation and independent verification, not a claim that the initial reviewer found or accepted it.

## Re-review at52ec2a3beecc54bfef8d0a7e0589ccf915b38f3b

Independent verdict: REWORK, one new Important/P2 in the amended policy. Both original temporal findings are closed. The reviewer verified truthful delayed completion/recovery, cross-domain null availability, actual pre-fit charge timing, execution-time reservation guards, modern default/legacy IDs, quarantine rechecks, as-of fee revisions, separate original-fit/NO_CHANGE acknowledgement provenance and the child-contained procedure.

**P2 future-cutoff recency:** learning.py:880 exports unrestricted caller cutoff; learning_procedure.py:159 and275 derive final recency from calendar closes through it. Admission uses min(cutoff,execution_now), so final weighting can count uncompleted sessions. Read-only witness over frozen joined-update: at execution2026-10-27T23:40Z, ten completed sessions give40weights of2. Caller cutoff2026-10-30T20:00Z yields18weights of1 plus22weights of2, while parent still admits October27. Refuse modern future cutoffs before materialization/charge, retain requested cutoff in the refusal receipt, and verify ordinary/historical/equality boundaries.

**Nonblocking P3 duplicate comparator payloads:** learning.py:872 exports complete qualifying/ack receipt documents per validation episode, including identical documents. A40-input receipt is48,011bytes, so ten predictive episodes referring to it twice duplicate approximately960KB. This grows with all-history inputs. The reviewer recommends identity-keyed unique documents with exact per-episode references, but did not establish a30-second failure or capacity threshold. Root defers this minor optimization to final whole-branch review; it does not justify a new performance project during the narrow cutoff fix.

The reviewer matched the exact diff,20sourcepins,13exports,17logs andsixCI; independently checked both40-trade joins and weighted fits, procedureMSE0.01396565351680062, late-correction causal scores,41-input/66-session boundary and originalv33 IDs. Read final691passed26PostgreSQLskipped641.72s. No suite rerun, source changes or operating access. Initial and amended evidence remain preserved. Acceptance awaits the single P2 repair and same-reviewer scoped re-review.

## Final scoped acceptance at579e0a352549c61edf08039a5c2934ce480755ab

Spec compliance ACCEPT; code quality Approved. The reviewer closed the remaining future-cutoff P2. learning.py:479 refuses modern future cutoffs before admission/materialization/fitting/charge, preserves requested cutoff and actual completion, and keeps parent=result. Line574 separates refusal identity while preserving the prior busy identity. Immutable replay does not block later valid consideration when execution reaches the cutoff.

The reviewer independently checked the exact diff,20sourcepins(18unchanged),4export hashes/typed payloads/raw-marker equality,3packaged/originallogs andsixCI. Final focused61passed43.66s supplements the distinct earlier691passed/26PostgreSQLskips gate. Equal/historical controls retain exact prior model/input-export IDs. No tests rerun during review, no new blocking finding. P3 duplicate comparator payloads remains explicitly deferred/nonblocking for final whole-branch review.

The integration owner accepts M4's isolated shadow-learning capability and hands off M5. This is not operational activation, deployment, profitability or natural broker-PAPER acceptance. All earlier evidence and failed attempts remain preserved.
