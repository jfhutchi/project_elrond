### Spec Compliance

- ❌ Issues found: signal ranking uses total historical plus current-session usage before prediction strength, contrary to the accepted design step 4 and the controller's explicit review clarification. `src/quantbot/paper_core/selection.py:172-182` must rank current-session usage, then prediction strength; historical use may only break otherwise equal signal ties. The signal-before-exploration requirement itself is correctly implemented at `selection.py:170-185`.
- ✅ All task-listed source files have corresponding changes. Optional legacy serialization, complete cohort capture/replay, transaction-scoped reservations, selected-symbol health handling, guardian mark refresh, and cost coverage are implemented. Learning procedure/target work remains correctly deferred to Task 2 (`contracts.py:46`, `trader.py:303`, `selection.py:76`, `roles.py:80`, `release.py:73`, `roles.py:214`).
- ⚠️ The full preserved runtime/PAPER identity and immutable-history guarantees span unchanged code and later tasks. This task does not modify frozen fixtures, migrations, model/receipt/authority schemas, or their digest construction. Existing compatibility evidence was inspected, but this task-scoped review is not the later integrated or operational acceptance gate.

### Strengths

- `selection.py:26-72` defines a semantic boundary independent of receipt time, model and release, validates a complete configured cohort, and persists observations plus cohort evidence atomically. Claim replay checks the exact stored cohort and each member before comparing the complete decision (`trader.py:303-350`).
- `selection.py:76-135` counts durable ENTRY reservations, including completed and unknown submissions, and releases only ERROR entries with the existing definitive-unsent proof. Claim runs under `BEGIN IMMEDIATE` (`trader.py:206`); final send rechecks membership/boundary/cap while excluding its own reservation (`send_guard.py:201`, `selection.py:224`). The existing global open-lot guard remains (`trader.py:359-367`).
- `contracts.py:46-50` and `contracts.py:244-267` omit absent optional config/decision fields, preserving the legacy serialization approach. Fixed position, stop, horizon and concurrency constants remain unchanged (`contracts.py:39-45`).
- `roles.py:80-98` derives refresh symbols from owned filled lots, rather than the configured default. Selected-member health and whole-universe cost coverage are propagated through claim/resume/runtime paths (`roles.py:51-61`, `health.py:164-171`, `authority.py:499`, `release.py:73`).
- New integration controls exercise real durable selection/claim/guardian paths for boundary concurrency, six completed/unknown reservations, definitive-unsent release, restart rotation, nondefault stops and forged cohort refusal (`tests/integration/paper_core/test_daily_selection.py:136-466`).

### Issues

#### Critical (Must Fix)

- None identified in this task-scoped review.

#### Important (Should Fix)

- **Signal ranking lets historical exploration usage override stronger signals and current-session fairness.** `src/quantbot/paper_core/selection.py:172-182`: after retaining unused qualifiers when present, the signal key starts with `daily + prior`, before `-prediction`. For example, with both stocks unused today, AAPL at 25 bps with ten prior reservations loses to MSFT at 11 bps with zero prior reservations. When all qualifiers have been used, a stock used once today with a long history can likewise lose to one used twice today with less history. Both violate the clarified ordering: current-session use first, highest qualifying prediction within that tier, then historical use/configured order for ties. Give signal selection its own key such as `(daily_count, -prediction, prior_count, universe_index)` while retaining historical rotation for exploration. Extend `tests/integration/paper_core/test_daily_selection.py:83-131` with differing prior counts for unused qualifiers and for unequal current-session counts; the current signal controls supply no `prior_counts`, so they do not expose this error.

#### Minor (Nice to Have)

- None identified.

### Assessment

**Task quality:** Needs fixes.

**Reasoning:** The authority and reservation design is cohesive and preserves the existing durable lifecycle boundaries. One deterministic ranking defect changes which stock is traded and must be corrected before accepting Task 1.

### Review scope and verification

- Reviewed BASE `6af19ce0d6ff3bf75a25ae7f80c20f34a5643e52` to HEAD `69e8edd508adb7b1fe74e7218dc535b5800f29ee` using the supplied review package. The first tool output truncated the package; subsequent bounded reads recovered the missing sections. No git command or broad repository crawl was used.
- Completed functions whose diff hunks cut off authority context: `trader.py:106-287,349-392`, `send_guard.py:1-290`, and `roles.py:358-422`. Named risks checked: preservation of global open-lot/manual-stop/current-inventory gates; final-send lock and definitive-unsent semantics; durable all-unavailable heartbeat reporting. These checks supported the claim/send/heartbeat conclusions above.
- Focused unchanged-interface checks: `brokers/base.py:15-32` confirms provider request/protocol/transport errors inherit RuntimeError and are caught by per-member asset eligibility handling; `features.py:8-30` and `time_evidence.py:204-242` confirm causal/freshness/session checks used for each member; `market_data/alpaca.py:192-235` confirms the adapter's single bounded BarQuery is acquired through the existing paginated client. `learner/five_minute.py` exception locations were checked for the per-member feature-builder failure contract. No change was requested to those existing interfaces.
- No tests rerun: the ranking defect follows directly from the comparator, and reported test results already cover the remaining reviewed paths. Inspected retained final logs `task1-logs/e42_7kmj.txt` (23 passed), `task1-logs/tbwhs7vw.txt` (7 passed, 20 deselected), `task1-lint.txt` (all checks passed), and `task1-mypy.txt` (no issues in nine source files). Those inspected final outputs contain no warnings. The earlier reported test-double failure and mypy cache-path problem were repaired before these final results.
- Only this requested review report was written. No implementation, fixture, runtime, broker, environment, branch, index, CI, or remote state was changed.
