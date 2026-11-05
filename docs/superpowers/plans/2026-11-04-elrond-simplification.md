# Elrond simplification implementation plan

> For agentic workers: use bounded subagent-driven development with no nested
> agents. One writer per file group. Parent owns integration and live state.

**Goal:** Reduce reachable PAPER runtime work and improve control/financial
truth while preserving every trading and recovery authority.

**Architecture:** Incremental repair in the preserved modular monolith. Changes
are grouped by actual deployed component and reviewed before cutover.

**Tech stack:** Python 3.11, SQLite/SQLAlchemy, FastAPI/Jinja, Windows Task Scheduler.

**Spec:** `docs/superpowers/specs/2026-11-04-elrond-simplification-design.md`

## Global constraints

PAPER only; no invented cost, trade, fill or result. Never relax manual stop,
unknown send, ownership, risk or freshness checks. No C: project writes;
requested E: absent, use D: topology. Keep one worktree and one authoritative
trading writer. Production database read-only until a reviewed maintenance plan.
No agent broker requests or credential reads. Initial child budgets eight minutes
and about twenty tool calls; parent authorizes explicit narrow followups.

## Review focus

- Missing or malformed Origin on control POST must cause refusal before callback.
- Unrelated historic signals/events must not be hydrated every deadline cycle.
- Ambiguous sends and due exits must survive deadline selection after restart.
- Fee report budget exhaustion must say unavailable, never partial zero or net.
- Active release source/tests must not be overwritten by older checkout content.

### Task 0 — Existing core repair and baseline

- [x] Preserve deployed PAPER core and exact dashboard/deadline deltas in Git.
- [x] Commit bounded guardian and health repairs; verify core 458 pass/1 skip.
- [x] Inventory source, processes, dependencies, runtime call graph and SQLite
      query plans; save measured baseline and limits.
- [ ] Finish interrupted active component characterization run; save exact result.
- [ ] Record startup, cycle and lock measurements if they can be measured safely;
      mark unavailable otherwise.

### Task 1 — Deadline query and writer interval

Files: `src/quantbot/learner/paper_deadlines.py`,
`src/quantbot/storage/repositories.py`, relevant learner deadline unit tests.

- [ ] Characterize pending/closed/foreign handoff and event ordering on a
      fixture with unrelated history.
- [ ] Record failing query/hydration assertion for broad reads.
- [ ] Select only relevant handoffs and events in SQL before JSON hydration.
- [ ] Preserve due exit before reconciliation, ambiguous-send recovery,
      idempotent fills, heartbeat and failure audit.
- [ ] Verify `EXPLAIN QUERY PLAN`, query counts, concurrency/restart/fault
      tests and representative fixture cycle cost. Review then commit.

### Task 2 — Console controls and bounded accounting read

Files: `src/quantbot/console/app.py`, `src/quantbot/reporting/paper_costs.py`,
their focused unit tests and templates.

- [ ] Characterize all seven mutating POST paths including missing/foreign
      Origin and legitimate same-origin stop/resume.
- [ ] Apply one shared origin/client/host guard before any form parsing/write.
- [ ] Set a measured read deadline and cumulative byte/row budget for the
      account cost reader; fail visibly if incomplete.
- [ ] Show existing fee status and source detail as account evidence while
      leaving native per-trade net unknown.
- [ ] Focused console/report tests, read-budget fault tests, static checks,
      review and commit.

### Task 3 — Evidence-backed simplification

- [ ] Check repeated Windows wrapper bootstrap against all active role/rollback
      paths; consolidate only if release packaging/rollback remain identical.
- [ ] Prove any dead module with CLI, config, dynamic loader, task, migration
      and test reference search before removal.
- [ ] Keep conditional legacy owner services while owned work remains.
- [ ] Remeasure maintainable LOC/files/dependencies/processes/startup/I/O.

### Task 4 — Functional gaps and deployment

- [ ] Isolate crypto core extension with explicit 24/7 clock/session, sizing,
      fill/fee, risk and learning contracts; use existing adapters where valid.
- [ ] Define honest fee allocation policy only if period ownership/completeness
      evidence permits it; never call an estimate broker reported exact cost.
- [ ] Resolve account fee-only dates, blocker detail and calendar/P&L gaps.
- [ ] Validate paper-only isolation, lock/fault/restart, risk, accounting,
      dashboard, learning, simulations and deployment checks.
- [ ] Stage one successor with all compatible edits and exact manifests.
- [ ] At an eligible window, fence/quiesce the three core roles, preserve old
      protection, install exact release, resume with fresh evidence, verify one
      writer/positions/orders/learner; monitor and report.
- [ ] Compact operating SQLite only after proof of complete historical receipt
      projection, quiesced copy, validated rollback and observed growth control.
