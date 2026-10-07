# Outcome-first PAPER exploration implementation plan

> Implement natively with test-first steps; independent safety review before rollout.

**Goal:** real bounded PAPER exploration, durable outcomes and subsequent learning.
**Spec:** ../specs/2026-10-07-paper-exploration-design.md
**Architecture:** reuse execution and safety services; isolate exploratory selection
and its immutable journal from frozen forecaster selection and evidence.
**Tech:** Python, Pydantic, SQLite, existing Windows limited task identity.

## Constraints and review focus

PAPER only; no credential/security changes, paid CI, changed fit budget or holdouts.
One concurrent learner position; no daily quota. Grandfathered inventory untouched.
Test restart/uncertain submissions, stale/partial panels, unknown fees/ownership,
closed-versus-unknown sessions, and outcomes crossing training/evaluation cutoffs.

## 1. Session-aware operator status

- [ ] Add failing closed/open/stale-clock/calendar tests in test_standby.py and
  worker/progress regression tests. Strict standby_status must still reject stale health.
- [ ] Implement read-only expected-wait classification using existing bounded PAPER
  market probe; preserve all non-heartbeat errors and ordinary authority validation.
- [ ] Run standby, worker, monitor and receipt regression groups; commit.

## 2. Separate exploratory execution policy

- [ ] Test immutable exploratory contract, complete-cohort selection independent of
  baseline scores, and inherited/pending position exclusions.
- [ ] Add exploratory contract/runner around retained sizing and risk vetoes; factor
  only selection/economic-edge hooks out of the existing runner. Keep forecaster
  defaults unchanged and bind exploratory behavior to its own exact identity.
- [ ] Test existing forecaster regression and synthetic protected execution cycle.

## 3. Attributed outcomes and feedback

- [ ] Test real ledger evidence: partial fills, independent exit linkage, unknown
  fees, residual inventory, duplicates and immutable feedback revisions.
- [ ] Reuse existing lot/episode readers; journal descriptive updates and baseline
  errors. Connect sufficient evidence to the existing registered successor pathway
  without extra fits, borrowed validation data or automatic model promotion.
- [ ] Test entry -> protected exit -> outcome -> update -> later evaluation.

## 4. Commission and observe

- [ ] Verify final aggregate, lint/typing and exact pinned release; publish coherent
  draft PR, review and respect normal repository protections.
- [ ] Back up nonsecret task/config/state, register real policy consent/deployment,
  transfer exclusive entry ownership and retain protection/observation/deadlines.
- [ ] Verify natural receipts and actual PAPER entry/exit/feedback, or report a precise
  safety/data/access blocker. Never manufacture an order or hide a veto.
