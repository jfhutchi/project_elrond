# Whole-share fallback implementation plan

> **For agentic workers:** Use superpowers:executing-plans task by task; one independent final review.

**Goal:** Add affordable stocks and a one-share fallback without increasing planned loss limits.
**Architecture:** An explicit entry-policy amendment extends the existing cohort/sizing/send
path. Its distinct guarded binding preserves the base commission, economics and history.
**Tech Stack:** Python, Pydantic, SQLAlchemy/SQLite, native Windows scheduled services.
**Spec:** `docs/superpowers/specs/2026-10-15-whole-share-fallback-design.md`.

## Global constraints

- Existing 25bps planned loss ceiling, 100bps stop, 95% buying-power budget, one open core lot.
- Existing six daily reservations, 30-minute horizon and 120-second exit buffer.
- Fractions first; explicit false eligibility permits whole shares; unknown eligibility refuses.
- Immutable base config/commission/model/history and ten-session all-history learning remain.
- No manual trades, shorts, inherited-owner transfer, paid CI or paid subscriptions.

## Review focus

- A later provider response removes fractional capability: original fractional POST must refuse.
- A minimum share fits cash but exceeds buying-power reserve or loss budget: it must refuse.
- An old decision or binary crosses the amendment boundary: its entry authority must refuse.
- A possibly sent or partially filled lot exists during transition: preserve recovery/exits.
- Old config/evidence payloads are read by the successor: hashes and model/history must survive.

## Tasks

### 1. Contracts and sizing

Files: create `src/quantbot/paper_core/entry_policy.py`; update contracts.py and release.py;
test `tests/unit/paper_core/test_whole_share_policy.py`.
Interfaces: EntryPolicy, AssetEligibility, entry_quantity(account, price, policy, fractionable),
entry_risk_allowed(account, quantity, price, minimum_whole_share=False).

- [ ] Write/run failing cases for one-share minima, flooring, cash/BP/risk refusal and old hashes.
- [ ] Add immutable optional contracts, preserving absent-field serialization.
- [ ] Run scoped tests and commit after integrated caller checks.

### 2. Cohort, claim and actual send

Files: alpaca_adapter.py, selection.py, trader.py, store.py, health.py, runtime.py,
send_guard.py and execution/coordinator.py; integration `test_whole_share_entries.py`.
Interfaces: current entry policy/effective symbols, optional policy IDs on decisions,
fresh pre-send eligibility acquisition and locked eligibility receipt validation.

- [ ] Write/run RED whole-share integration and changed/unknown eligibility cases.
- [ ] Reuse existing selection/reservation and owned-exit paths with the explicit amendment.
- [ ] Prove fractional/whole entries, exact exit ownership, atomic refusal and unknown-send retention.
- [ ] Run current funds/selection/final-send regressions and lint changed files.

### 3. Explicit policy amendment authority

Files: authority.py, __main__.py; integration amendment tests alongside existing final-repair tests.
Interface: transition_entry_policy(store, successor, approval), distinct binding kind and CLI command.

- [ ] Write/run RED exact-config and open/ambiguous-lot refusal tests.
- [ ] Add guarded amendment CAS; keep ordinary code-only transition equality unchanged.
- [ ] Verify historical model/config/commission/stop/rows and legacy hashes are preserved.

### 4. Review, deployment and handover

- [ ] Obtain independent review and resolve substantive findings with regression tests.
- [ ] Stage exact native successor and compatible console reader; verify source/dependency pins.
- [ ] Fence entries; preserve/wait for old owned exits; verify backups and old writer absence.
- [ ] Apply explicit amendment, start guardian/outcomes, verify fresh readiness and resume.
- [ ] Verify real broker behavior, protection and learning status without inventing outcomes.
- [ ] Update operational guidance/evidence, push and open a PR; no paid CI or automatic merge.
