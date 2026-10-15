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

- [x] Write/run failing cases for one-share minima, flooring, cash/BP/risk refusal and old hashes.
- [x] Add immutable optional contracts, preserving absent-field serialization.
- [x] Run scoped tests and commit after integrated caller checks.

### 2. Cohort, claim and actual send

Files: alpaca_adapter.py, selection.py, trader.py, store.py, health.py, runtime.py,
send_guard.py and execution/coordinator.py; integration `test_whole_share_entries.py`.
Interfaces: current entry policy/effective symbols, optional policy IDs on decisions,
fresh pre-send eligibility acquisition and locked eligibility receipt validation.

- [x] Write/run RED whole-share integration and changed/unknown eligibility cases.
- [x] Reuse existing selection/reservation and owned-exit paths with the explicit amendment.
- [x] Prove fractional/whole entries, exact exit ownership, atomic refusal and unknown-send retention.
- [x] Run current funds/selection/final-send regressions and lint changed files.

### 3. Explicit policy amendment authority

Files: authority.py, __main__.py; integration amendment tests alongside existing final-repair tests.
Interface: transition_entry_policy(store, successor, approval), distinct binding kind and CLI command.

- [x] Write/run RED exact-config and open/ambiguous-lot refusal tests.
- [x] Add guarded amendment CAS; keep ordinary code-only transition equality unchanged.
- [x] Verify historical model/config/commission/stop/rows and legacy hashes are preserved.

### 4. Review, deployment and handover

- [x] Obtain independent review and resolve substantive findings with regression tests.
- [x] Stage exact native successor and compatible console reader; verify source/dependency pins.
- [x] Fence entries; preserve/wait for old owned exits; verify backups and old writer absence.
- [x] Apply explicit amendment, start guardian/outcomes, verify fresh readiness and resume.
- [x] Verify real broker behavior, protection and learning status without inventing outcomes.
- [x] Update operational guidance/evidence, push and open a PR; no paid CI or automatic merge.

## Execution evidence

The implementation and three independent review passes are recorded in the [October15 handover](../../evidence/paper-core/whole-share-2026-10-15/README.md). The first real AAPL lot closed before the flat amendment. Two obsolete guardian inventory waits were recovered using the existing proof-based API; explicit stops were untouched. Native validation caught and repaired the expanded-cohort call before any amended entry could submit. The amended runtime remains enabled; the full30-minute feature horizon prevents late-session entries. Real nonfractionable fills and ten-session learning remain separately measured acceptance limits.
