# Daily PAPER exploration implementation plan

Spec: [daily PAPER signals, exploration and learning](../specs/2026-10-14-daily-paper-exploration-design.md).

Base: `0a5b6c47524f2c61f289e50973969881b6a55b89` on `codex/daily-paper-exploration`. Worktree: `C:\Users\leepa\.codex\worktrees\paper-core-m0\project_elrond`. User clarification supplies the hybrid behavior; six entries, universe and gross learning target are explicitly labelled project defaults. Implement sequentially with independent task review; read-only review/preparation can run concurrently. No broker/runtime mutation, paid CI, or credentials in tests.

## Global constraints

- Preserve old serialized config, model, decision, policy, receipt and authority digests when new fields are absent. Do not mutate frozen fixtures or historical evidence to hide incompatibility.
- PAPER only, original account/owner/store/client IDs, one concurrent lot, 50-bps position value, 100-bps stop, 30-minute horizon, existing reserves and manual stop/integrity boundaries.
- The new policy selects signals before exploration over a causal multi-symbol cohort, never by bypassing invalid authority. All reservations are durable and revalidated under the existing claim/send transaction.
- V3 gross-return learning changes eligibility/target explicitly; original fee/net fields remain unchanged. V2 remains replayable and strict.
- Use meaningful TDD controls against real database/selection/learning code; fake only external provider boundaries. Run isolated checks through the existing credential-stripping/socket-blocking harness, copied into this plan's ignored workspace. A copied harness is local tooling, not a tracked `.superpowers` file.
- Commit each coherent task. Root owns publishing and successor deployment preparation. Workers must not spawn helpers, touch operating databases, read env files, dispatch workflows, merge or push.

## Task 1: Hybrid multi-symbol entry and durable daily reservations

Files: `src/quantbot/paper_core/contracts.py` (CoreConfig/Decision and new selection evidence only), `trader.py`, `roles.py` (entry/guardian paths), `alpaca_adapter.py`, `health.py`, `authority.py` (resume readiness), `send_guard.py`, `release.py`; focused new unit/integration tests beside existing paper-core tests. Prefer a small `selection.py` module to keep deterministic cohort and reservation logic separate from provider acquisition.

Add optional `symbols: tuple[str, ...] = ()` and `selection_policy` with legacy `MODEL_ONLY_V1` and new `SIGNAL_OR_EXPLORE_V1`. Absent fields preserve old hashes and an empty universe resolves to `(symbol,)`. Validate finite, unique uppercase supported stock symbols and benchmark separation. The new policy has a fixed named six-entry/session limit; do not change old-mode quotas. Add optional immutable cohort/selection metadata to Decision, omitting absent fields for old documents. Record qualifying signal versus exploration explicitly.

Acquire one bounded completed-bar cohort, build each symbol independently with SPY, and retain unavailable-member reasons. Selection must prioritize not-yet-used eligible symbols, then >10-bps valid predictions, else durable least-used/rotating exploration. Repeats are allowed only when the currently eligible universe has been covered. Keep the negative/zero prediction. Preserve inventory/guardian/time/model/health gates and inherited-held exclusions.

Claim-time replay verifies the complete captured selection, not a caller Boolean. Derive stable account/owner/session/source-end boundary identity. Within BEGIN IMMEDIATE, reject duplicates and the seventh possibly-sent session reservation, including completed trades; definitive-unsent is the only release. Final send rechecks membership/limit and counts its reservation once. Existing unknown-send/global-open-lot protection remains. Use current durable intent/decision tables; add no schema solely for convenience.

Propagate actual symbol through observation/health recovery, claim/resume checks, cost-contract coverage and guardian open-lot mark refresh. Keep legacy public entry APIs working for old fixtures/callers. Do not modify daily_consideration's learning policy yet except to pass the actual `store.config.policy_id` if needed; Task 2 owns target/procedure selection.

TDD controls: strong-vs-weak selection; all negative/no predictive model explores; forged fallback refuses; missing/held member permits another stock; restart rotation; duplicate boundary with new receipt timestamps/concurrent claimers; six completed/unknown reservations refuse seventh; definitive-unsent release; non-default-symbol stop/health; old hashes and old decision replay. Run relevant existing trader/guardian/final-send/runtime tests as well as new tests. Record exact red/green commands/results, commit, and report interfaces and any remaining concerns.

## Task 2: Daily v3 all-history gross learning and activation

Files: `contracts.py` (ModelArtifact/LearningPolicy and target/procedure fields), `learning.py`, `learning_procedure.py`, `activation.py`, `roles.py` (`daily_consideration`), focused new learning tests and existing temporal/activation tests. Consume Task 1's `selection_policy`; do not change entry selection/reservation behavior.

For `SIGNAL_OR_EXPLORE_V1`, construct LearningPolicy with exact configured policy_id, `daily-paper-exploration-walk-forward-v3`, and `owned-executed-paper-gross-return-v1`. Legacy mode remains v2/net. Extend target/procedure validation and serialization without changing older hashes. Carry exact receipt target/procedure/policy into permanent charge and activation authority; reject mismatched net/gross incumbents and authority envelopes.

V3 accepts COMPLETE gross outcomes and fee-only pending closed/reconciled outcomes with cost_reason FEE_UNAVAILABLE and established gross P&L. Preserve every other source/ownership/time/holdout/quarantine/correction exclusion and original fee/status/net fields. Dispatch target calculation explicitly: gross for v3, net for v2. Retain within-training feature overlap only for v3, preserving fold chronology and 30-minute train-versus-validation purge. Keep ten-session warmup, 30 examples/five training sessions, ten examples/five validation sessions, five folds, one charged procedure/day, ridge envelope, validation gates, 2x latest-ten weights and all-history final fit.

TDD controls: mixed positive/negative real gross targets influence actual fit; null fees/net remain unchanged; other incomplete outcomes excluded; v2 unchanged; within-history overlap retained; cross-fold unavailable/future/protected data refused; day-ten fixture meets sample floors and executes the actual bounded fitter; all retained older examples remain eligible with correct weights; mismatch activation refuses; daily failed/insufficient/rejected receipt remains truthful and does not block exploration. Run existing temporal/canonical/activation tests suited to changed interfaces. Commit with exact evidence.

## Task 3: Integrated witness, documentation and successor preparation

Files: focused integration test(s), current overview/README/CLAUDE/current learning/direction docs, new evidence bundle under `docs/evidence/paper-core/daily-exploration/`. Do not rewrite sealed prior bundles. Root handles host/package operations after review.

Exercise a deterministic credential-free real-component witness: independent protection, signal-led entry on one stock, exploration on another with negative prediction, restart and daily-cap enforcement, closed gross outcome with unknown fees, and later v3 learning/activation/model attribution. Include multiple symbols and both wins/losses; retain genuine test labels and exact source/evidence hashes. Do not manufacture real broker acceptance.

Run a covering local gate across new tests plus paper-core entry, send, protection, runtime, learning temporal/canonical/activation/migration tests; Ruff and relevant typing. No full-suite/heavy repeat merely by habit; broaden only for a concrete unresolved regression. Disclose unavailable PostgreSQL/platform coverage. Request a whole-branch independent review, resolve load-bearing findings, and preserve red/green evidence.

Update current documentation to describe tested behavior and actual limitations. Prepare a successor config/manifest using the configured stock universe and v3 policy, reviewed current account binding and existing store identity. Start from target-neutral COLLECT. Preserve old release artifacts and disabled tasks. Do not migrate/commission while the inherited exit is unresolved; fresh handover evidence and operational compatibility remain required. Publish the reviewed implementation branch/PR and update documentation on main under the user's existing push authorization; do not silently merge implementation.

## Review focus

1. A restart or concurrent acquisition changes observation IDs but must not create a second entry for one semantic boundary.
2. A daily count that omits completed, rejected-after-POST or unknown orders can exceed its limit; false unsent assumptions are forbidden.
3. A caller may try to forge exploration despite a stronger captured candidate, or an unavailable symbol may improperly block the whole cohort.
4. Non-default-symbol holdings need guardian marks and symbol-scoped recovery; XLE defaults must not leak into those paths.
5. Gross target eligibility must not admit ownership/correction/temporal incompleteness or report unknown net as known; old v2 and immutable artifacts must still replay.

## Completion record

Task status, review decisions and exact test commands live locally under this plan's ignored `.superpowers/sdd/` workspace. Durable final evidence belongs under `docs/evidence/`. Source implementation, a synthetic witness, host staging and natural broker-PAPER acceptance are separate statuses.
