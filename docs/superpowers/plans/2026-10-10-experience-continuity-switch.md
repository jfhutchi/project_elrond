# Experience continuity and exact switch procedure

Scope approved: implement the five identified source gaps using isolated synthetic stores, no real fits or runtime changes. Applying this code remains separately approved. Existing protection, stop, PAPER limits, trial charges and holdouts remain unchanged.

## Design

Two independent additive journals extend existing contracts. `experience_continuity.py` admits explicitly approved, exactly registered code-only exploration policy transitions; all other policy/manifest changes are incompatible. Ancestor feedback remains attributed to its original policy. `experience_consideration.py` records original lot/source-revision consideration and an explicit no-change decision; it exposes current deduplicated lineage feedback without changing the deployed selector. `experiment_switch.py` implements exact approved staging/completion and rollback in the experiment store. Staging atomically fences model entries by leaving ACTIVE_PAPER; completion requires verified target operational binding and readiness, then atomically changes sidecar activation. It performs no operational registration, task update or broker action. Unfinished stages survive restart and refuse automatic model fallback. Exact approved rollback preserves receipts and budgets.

## Review focus

- An unapproved, ambiguous, incompatible or circular policy transition must not inherit experience.
- Late genuine evidence changes current consideration, not the number of trades; duplicates and later conflicts fail closed while earlier reads remain truthful.
- No-change is durable and never grants fitting/application; real economic feedback cannot replace protected chronological research data.
- A crash between staging and binding verification must keep entries fenced; wrong consent, stale state, open lots and pending intents cannot complete a switch.
- Rollback uses an explicitly approved retained binding and preserves newer evidence and permanent charges; it does not clear stops.

## Tasks and interfaces

1. Create `src/quantbot/learner/experience_continuity.py` and `tests/unit/learner/test_experience_continuity.py`. Functions: `propose_policy_transition(store, source, target, *, now)`, `approve_policy_transition(store, proposal, *, approval, now)`, `verified_policy_lineage(store, policy, *, now)`. Red tests for compatible registered transitions, exact approval, semantic rejection, duplicate edges, chain/as-of verification and retained original ownership; then minimal implementation and green regression check.
2. Create `experience_consideration.py` and its unit tests. Functions: `lineage_feedback(store, policy, *, now)`, `consider_experience(store, policy, *, now)`, `current_considerations(store, policy, *, now)`. Red tests for retries, loss/flat inclusion, source revisions, incomplete/late/conflicting proof, code-only continuity and stable no-change decisions. Use existing verified feedback and lot resolution; never fit or grant activation.
3. Extract read/verify portion of existing activation into `_validate_activation_request`, preserving existing first-activation tests. Create `experiment_switch.py` and switch tests. APIs stage/complete exact switch and rollback; approvals freeze incumbent activation/revision, package, binding and purpose. Journal proposal/approval/stage and fenced state atomically; completion verifies exact target services, no pending intents or incomplete owned lots, and immutable retained package lineage before atomic activation. Recovery repeats verification on existing stage; no mixed binding fallback. Tests exercise failed gates/restart and evidence-preserving rollback.
4. Independent review of each source increment, address all material findings with regressions. Run full frozen offline aggregate on final clean committed head; publish a draft PR with synthetic-versus-runtime limitations and exact deployment dependencies. No deployment, operational writes or real fits.
