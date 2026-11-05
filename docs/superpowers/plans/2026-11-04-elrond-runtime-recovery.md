# Elrond runtime recovery Implementation Plan
> For agentic workers: use superpowers:subagent-driven-development with bounded phases and no nested agents.
**Goal:** Stop redundant database growth, safely reclaim verified redundant disk data, and restore an observable working PAPER trade/outcome/learning loop.
**Architecture:** Repair the preserved deployed core with its SQLite ledger and existing runtime authority. Isolate code changes in a managed Git worktree; coordinator alone performs live maintenance.
**Tech Stack:** Python 3.11, SQLite, SQLAlchemy, pytest, Windows Task Scheduler.
**Spec:** docs/superpowers/specs/2026-11-04-elrond-recovery-design.md

## Global Constraints
- PAPER only; no live trading, fabricated observations/fills, or weakened freshness/statistical/risk checks.
- Keep unknown sends, actual exposure, fill/outcome IDs and audit provenance.
- No agent accesses credentials or invokes broker endpoints; coordinator owns verified maintenance.
- Use existing interpreter/dependencies, isolated test env/temp state, explicit test timeouts.
- At most three child agents concurrently, no nested agents; initial phases 10–12 minutes/~25 calls and explicit continuation decisions.
- No blind deletion, no unbounded production queries; validate resolved deletion paths and recovery identity.
## Review Focus
- Terminal ERROR can include unknown sends: retain unless definitively-unsent evidence proves retirement safe.
- A failed entry never becomes a completed round trip: repeated guardian cycles must have bounded membership growth.
- Delay after selection cannot authorize stale sends: refresh/reconsider before admission, never bypass expiry.
- Quiet/reused learning inputs must yield truthful NO_CHANGE; eligible corrected inputs must be reconsidered.
- Maintenance interrupted midway must preserve authoritative original and deny unsafe activation.

### Task 0: Preserve baseline and diagnostics
- [x] Create managed isolated worktree at current main.
- [x] Copy deployed src/tests/scripts/deploy/config (no runtime secrets/data) and hash inventory.
- [ ] Commit baseline snapshot before repairs.
- [ ] Record independent trading and learner diagnosis in task reports.

### Task 1: Protection lifecycle and bounded acknowledgements
Files: src/quantbot/paper_core/guardian.py, work.py, roles.py; tests/unit/paper_core/test_guardian_retention.py (or existing appropriate suite).
Consumes: existing intent/outbox/send-refusal states, GuardianReceipt and store APIs.
Produces: compatible acknowledgement behavior without failed-entry history amplification.
- [ ] Write test: definitively-unsent ERROR entry is excluded, unknown-send/possibly-sent/held exposure remains.
- [ ] Write test: repeated unchanged cycles do not multiply full memberships unnecessarily; a changed live plan set refreshes promptly and readiness freshness remains valid.
- [ ] Observe expected regression failure.
- [ ] Implement minimal lifecycle/query/ack fix; keep old receipt wire identities readable.
- [ ] Run focused guardian/send/ownership/health regression suite in isolated environment; inspect diff, review then commit.

### Task 2: Trading and learning loop
Files: exact affected paper_core modules/tests determined by the two bounded diagnosis reports. Only one implementer edits shared files at a time.
Consumes: actual source-preserved runtime and Task 1 compatible interfaces.
Produces: root-cause fixes with regression tests for observed expiry/learning stall, or concrete evidence of legitimate unavailable prerequisites.
- [ ] Trace one admitted batch and latest learning attempt from immutable records.
- [ ] Write failing behavior tests for verified faults, then minimum fixes.
- [ ] Verify existing expiry/unknown-send and past-only learning gates.
- [ ] Run a bounded deterministic trade->outcome->learning witness using real application components with synthetic broker fixtures clearly labeled.
- [ ] Review and commit changes; never claim synthetic witness is forward PAPER performance.

### Task 3: Redundant-data recovery
Files: scripts/recovery tooling and tests, docs/evidence/runtime-repair-20261104 cleanup manifests.
- [ ] Inventory candidate files, paths, hashes/retention dependencies; choose keepers.
- [ ] Remove only hash-verified redundant offline copies and verified disposable outputs, preserving one required source.
- [ ] Establish exact references and derive lossless historical encoding/projection retention before compacting live state.
- [ ] Test compaction on small fixture for referenced receipt retention, fill/outcome preservation and interrupted cutover.
- [ ] Coordinator performs bounded maintenance, produces validated replacement, atomically swaps only after all writers are quiescent and validations pass; retain recoverable original until replacement verified.
- [ ] Measure actual reclaimed storage and regrowth.

### Task 4: Release and verify
- [ ] Check release-transition contracts, snapshot source/config/task metadata and small recovery records.
- [ ] Install exact reviewed code through compatible prepared manifest and audited transition.
- [ ] Restart only affected tasks and verify fresh identities, heartbeat readiness, current positions/orders and learner verdict.
- [ ] Run appropriate full isolated core checks once; log any baseline failures explicitly.
- [ ] Complete whole-change review, update operator evidence and account-usage ledger.

