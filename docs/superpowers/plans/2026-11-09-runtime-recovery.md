# Runtime recovery implementation plan

Goal: repair quote expiration caused by local work, Guardian budget starvation, and repeated learner acknowledgement conflicts without changing financial safety or model-promotion requirements.

The user authorized implementation, tests and controlled deployment. Use the existing checkout and a dedicated branch; no additional operating ledger copy or engine.

- [x] Quote task: measure source age and local validation time; add a failing real-ledger regression; consolidate repeated validation within one transaction. Preserve source-age limits and locked revalidation after awaits.
- [x] Inventory task: reproduce Guardian exhausting its own noncritical request allocation; schedule refreshes using the existing current-proof invariants. Keep protection/exit polling and forced refresh on work or changed authority.
- [x] Learner task: reproduce ACTVATION_UNRESOLVED_CLAIM from a rejected request with no terminal acknowledgement. Preflight the existing activation boundary and report acknowledgement deferral before writing authorities. Do not declare the unresolved request settled or bypass the boundary.
- [ ] Run focused regressions, the full supported test command, static checks and failure-path tests. Successful synthetic scratch must be removed.
- [ ] Review the combined patch. Fence entries, stop all five old task trees, verify quiescence, deploy verified source and seal, advance the existing lease, start dependencies before one Trader, and resume through ordinary readiness checks.
- [ ] Verify current broker/ledger evidence, fresh role health, repeated trading cycles and automatic news progress. Distinguish real fills and learning evidence from synthetic tests, shadow features and future-window gates. Record residual blockers and rollback.

Review focus: stale quotes still rejected; unknown sends remain reserved; current-proof reuse cannot survive changed ledger/source/config; owned exits are never throttled; learning deferral creates no authority or trial charge.

Actual handoff: affected suites and independent review completed; full-repository sweeps incomplete and disclosed. Tested code-only component deployment is running with exact old configuration and the entry fence retained. No trading resume is claimed. Original BTC provider evidence is required before completing the guarded entry handover/resume. See docs/evidence/runtime-recovery-20261109/README.md.
