# Crypto PAPER implementation plan

> **For agentic workers:** Use subagent-driven development or executing-plans task by task.

**Goal:** Enable cost-aware cryptocurrency PAPER operation in the existing engine and ledger.
**Architecture:** One account, one ledger, existing roles; explicit operational asset evidence,
instrument sessions and policy-bound conservative limits. No new services or dependencies.
**Tech stack:** Existing Python/Pydantic/SQLAlchemy/SQLite/Alpaca HTTP integration.
**Spec:** [Design](../specs/2026-11-06-crypto-paper-design.md).

## Global constraints

PAPER only. Preserve equity behavior, historical records, ownership, idempotency, protective
exits, explicit manual stops and unknown fee status. No production DB copies for tests. No
project writes to C:. Agents have narrow file ownership and no autonomous deployment authority.

## Review focus

- Base-asset fees reduce sellable inventory; cash-only assumptions must not strand exits.
- UTC midnight and DST must not reset Eastern account risk or duplicate reservations.
- Mixed cohorts must retain both markets without accepting stale stock data after hours.
- Broker lot increments, minimums and delayed activities must survive restart and partial fills.
- Historical serialized identities and release bindings must remain unchanged until transition.

## Tasks

- [x] Read-only account/asset eligibility and source/runtime-path discovery.
- [x] Verify successful synthetic-test cleanup with existing regression suite (17 passed).
- [ ] Entry-policy and operational capability contracts, backward-identity tests; root owns
  `entry_policy.py`, contracts/selection/batch/send guard/time integration.
- [ ] Broker data/quote/order acquisition and crypto cohort: assigned worker owns
  `alpaca_adapter.py` acquisition section and market-data crypto helpers; fail-first tests.
- [ ] Fee/accounting evidence and learning compatibility: assigned worker owns bounded fee
  primitives and agreed accounting files; test asset-denominated and delayed fee evidence.
- [ ] Shared risk day, sleep defaults, Guardian custody/exit integration, with regression tests.
- [ ] Preserve research catalog; documentation-only worker finishes the index and local links.
- [ ] Independent review, targeted suites, lint/type checks, broker read-only integration.
- [ ] Controlled paper-only cutover, rollback readiness, running-release verification and bounded
  actual integration evidence; never report activation from synthetic tests alone.
- [ ] Audit existing news/social sources, configured entitlements, freshness and current consumers.
  Reuse discovery_research/research_worker, research semantic/sources, and learner news modules.
- [ ] Immutable contemporaneous context/decision traces, reproducible asset-linked features and
  a separate versioned price-plus-context candidate-learning path; prove no future-news leakage.
- [ ] Verify authentic available-provider ingestion and candidate consumption, report exact
  X/Reddit access blockers, retain validation/promotion gates and honest price-only provenance.

Progress and blockers are recorded here and in the final implementation evidence. No broad
strategy backtests or unrelated cleanup are part of this implementation.
