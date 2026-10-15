### Finding Verdicts

- **I1 — Partial first-BUY historical basis lookup incorrectly requires revoked current cash authority — ADDRESSED.** `src/quantbot/paper_core/guardian.py:131` now resolves the latest certified proof ID and its payload in the predicate's existing transaction. The lookup no longer calls the current-authority accessor. Missing certificate/document refuses, and `guardian.py:143` requires the exact proof digest and matching baseline before using historical position basis. The fix grants no present cash or entry authority and leaves all earlier source, ownership, all-symbol quantity and basis guards intact.
- **Regression evidence:** `tests/integration/paper_core/test_daily_paper_loop.py:297` publishes only 2 of an acknowledged BUY's 5 units through the external provider boundary. It checks the required synchronization and guardian retry paths, unchanged certificate, persistent cash revocation, unavailable entry authority (`:362`), and continued refusal by the original current-authority accessor (`:363`). Publishing the remaining activity (`:374`) then yields genuine quantity certification, protected close with both entry-fill IDs and unknown fee/net values, followed by the next eligible entry.

### New Breakage in the Fix Diff

- None. The production diff changes only historical basis retrieval; it does not restore revocations, certify the failed proof, change accounting values or modify learning/model/activation behavior.

### Out-of-Scope Observations

- None newly identified. Controller-owned deliverables and the qualifications recorded in the original task review remain outside this scoped fix verdict.

### Checks

- Read the `8bae89e164b8831eba86ec0dde4f7f7abb7aa1ea..36d61811e15a350da401eb15e6ac8980e7aea4f1` two-file fix package once and the appended fix report. No source/index/HEAD mutation, subagents, operating resources, env files, network, CI or push operations; no tests were rerun by this reviewer.
- Read actual retained RED `task4-logs/fix1-_93ivtig.txt`: **1 failed, 11 deselected in 5.40s**, at the exact revoked-cash historical-basis defect. Identical focused GREEN `task4-logs/fix1-hc17kkx8.txt`: **1 passed, 11 deselected in 5.80s**.
- Read actual retained covering gate `task4-logs/fix1-urfrn2_a.txt`: **91 passed, 1 deselected in 192.64s**, with no warnings or failures. The report names the seven covering files and explicit `-k 'not daily_hybrid'` exclusion; the unchanged heavy combined fit was not represented as rerun.
- Read actual `task4-fix1-lint.txt`: **All checks passed!** Read actual `task4-fix1-mypy.txt`: **Success: no issues found in 1 source file**.

### Verdict

**Fix round:** All findings addressed, no new Critical/Important breakage. Scoped Task 4 source spec compliance and quality are approved after fix round 1; no open source finding remains from the task review.
