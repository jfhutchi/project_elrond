# PAPER Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL at execution time: use superpowers:executing-plans, or superpowers:subagent-driven-development if the user selects delegated implementation. The user authorized implementation on 2026-10-14. Execute only the next ready milestone and preserve its independent review gate. Steps use checkboxes for evidence-gated tracking.

**Goal:** Establish an attributable, reliable PAPER trading-to-costed-outcome-to-learning-to-later-decision loop, preserving existing safety and evidence.

**Architecture:** Incrementally replace orchestration with a Trader, independent Guardian, and Outcome/Learning worker over one operational SQLite store. Reuse repaired execution/ownership primitives, causal features and deterministic ridge; disable optional research only after verified ownership handover.

**Tech stack:** Existing Python 3.11.16 baseline, `uv.lock`, Pydantic, Decimal, SQLite/SQLAlchemy and explicit Alembic migrations, current Alpaca PAPER adapter, existing Windows native hidden process host. No new service/framework.

**Spec:** [PAPER core design](../specs/2026-10-13-paper-core-design.md). **Audit:** [evidence and ranked defects](../../reviews/2026-10-13-paper-redesign/audit.md). **Tests:** [executable test plan](../../reviews/2026-10-13-paper-redesign/test-plan.md).

## Global constraints

- Status: **M0–M3 ACCEPTED; M4 READY**. M4–M6 not started. Implementation approval covers isolated code and tests, including timing and learning contracts; it does not authorize deployment, operational model activation, OS resync or clearing existing stops.
- PAPER only; no LIVE path, paid CI, new credentials/services, OS resync/security/policy changes, forced trades, holdings reset, deleted history, silent stop clearance, or untested protection removal.
- Existing limits remain: one new position, 50 bps of equity per position, 25 bps maximum entry risk, 100 bps stop, 30-minute horizon, ≥120 s session-exit buffer, 500 bps buying-power reserve. Existing tighter portfolio controls win.
- Relative skew bound remains **±2 s**. The locally approved `PAPER_TIME_V2` implementation keeps 5 s acquisition, 120 s maximum evidence age and initial 50 ms continuity bound. Exact operational commissioning remains part of M6 review; do not silently substitute it for an existing runtime's STRICT_V0/RELATIVE_V1 profile.
- The locally approved ridge policy uses alpha=1, seed=0, prediction clip ±100 bps, at most one 30 s fit/session, 30 training episodes/≥5 sessions, 10 later validation episodes/≥5 sessions, ≥30 min embargo, last 60 eligible sessions. Implementation approval does not commission operational model use or unattended activation. Use the canonical holdout and permanent trial registry.
- Preserve source/time/cost/evidence-lane provenance. Unknown fees never become zero. Simulated results never become broker-PAPER acceptance. A passed unit suite does not complete a milestone.
- User refinement: no automatic global PAPER kill switch for ordinary clock/data/provider/acquisition faults. Use scoped waits with automatic fresh-evidence recovery; keep protection and trustworthy outcomes/learning running. Manual pause remains explicit; genuine contradictions quarantine only necessary authority/evidence.
- Current workspace is old (`9f5ece9`), current main audited at `b812c64`, PR287 at `8bb84cf`, PR288 at `a6f9eb3`. At execution, resolve heads and docs again; create an isolated branch from reviewed main. Do not build from the old workspace branch or silently inherit unrelated work.
- Keep the old runtime unchanged until M6, except a **separately reviewed and authorized** urgent safety fix. Diagnostic probes are not such a fix.

## One integration owner and one authoritative plan

The primary coordinating Codex chat owns this plan, interface decisions, integration branch and readiness matrix. A successor coordinator must record that handover. Sol receives **only the next ready brief**, the spec, relevant evidence and current source pins. Sol returns its diff, exact tested SHA, commands/logs, joined integration evidence, risks and rollback. It does not declare the overall system complete or start the next milestone.

After each milestone a reviewer who did not implement it reviews both diff and integrated evidence. The integration owner resolves findings, updates this plan and source map, records ACCEPTED/REWORK/WAITING with evidence links, then presents only the next ready handoff. No automated merge/deploy/communication is implied. Preserve failed attempts and rejected designs when they explain changed contracts; do not keep parallel competing plans.

| Milestone | Dependency | Integrated capability | Status |
|---|---|---|---|
| M0 | User approves design/plan | Reproducible read-only release/ownership/blocker inventory and legacy trade trace | ACCEPTED |
| M1 | M0 accepted | Thin synthetic observation → guarded intent → owned fill → independent exit → flat round trip; protection baseline repair | ACCEPTED |
| M2 | M1 accepted | Cost provenance and restart-safe uncertain/partial-order/outcome processing | ACCEPTED |
| M3 | M2 accepted; clock contract reviewed | Scoped recovery/timing and per-symbol eligibility; outcome/protection progress during entry failure | ACCEPTED |
| M4 | M2 accepted; learning envelope reviewed | Real bounded outcome-driven parameter fitting and explicit NO_CHANGE, in shadow | READY |
| M5 | M3 + M4 accepted | Atomic model activation and later decision witness; synthetic whole loop; real-data local simulation | NOT STARTED |
| M6 | M5 accepted; concrete rollout review | Frozen release handover and a naturally eligible broker-PAPER whole-loop evidence bundle | NOT STARTED |

M3 and M4 can be **developed independently after M2** against frozen contracts, but the owner hands off only one milestone at a time unless the user changes that workflow. Within a ready milestone, fixture construction, source review and documentation can run independently with separate ownership; shared contracts/schema, production rollout and activation are sequential. No parallel broker writers. M6 is not ready merely because M5 unit tests pass.

## File and contract map

Create `src/quantbot/paper_core/` as the narrow supported path; old code remains until retirement. Proposed files are new, not claims they already exist:

| File | Responsibility / owner milestone |
|---|---|
| `inventory.py` | Explicit read-only release/store/ownership/evidence inspection; M0 |
| `contracts.py`, `store.py` | Immutable contracts in spec; short transactions, uniqueness and source cursors; M1 with additive M2/M4 extensions |
| `features.py`, `trader.py` | Adapt existing per-symbol five-minute features; record decision and final eligibility; M1, M3 |
| `guardian.py` | Independent lot recovery/protection/exit ownership, certified baseline; M1–M3 |
| `simulation.py` | Synthetic and real-data simulation adapters, with distinct lane and no broker secrets; M1, M5 |
| `outcomes.py`, `costs.py` | Durable fill/terminal/flat join, cost provenance and revisions; M2 |
| `time_evidence.py`, `health.py` | Reviewed clock contract, scoped states and first blocker; M3 |
| `learning.py` | Fixed-target ridge fit, holdout/budget/validation and learning receipt; M4 |
| `activation.py` | Compare-and-swap activation, restart and rollback; M5 |
| `runtime.py`, `__main__.py` | Three role entry points and one config/manifest loader; M1 prototype, finalized M6 |
| `deploy/omen/start-paper-core.ps1` | Supported-policy role launch through existing service host; M6 |

Existing paths to retain/adapt are listed in each brief. All relative source paths refer to the isolated **reviewed main** checkout, not the old user workspace. Alembic revision number is allocated from the actual branch head during M1 to avoid collisions; migration content and supported schemas are pinned in the release manifest. This is an explicit sequencing choice, not permission for implicit migrations.

Public interfaces below share immutable types from `contracts.py` defined by the spec table: `Observation`, `Decision`, `EntryIntent`, `ProtectionPlan`, `BrokerEvent`, `ReconciliationProof`, `RoundTrip`, `LearningReceipt`, `Activation`, `Health`. Additional wrappers are simple typed collections: `InventorySnapshot`, `EvidenceBundle`, `CoreConfig`, `TimeEvidence`, `CycleReceipt`, `OutcomeBatch`, `LearningPolicy`, `ModelArtifact`, `ActivationAuthority`, `PreparedRelease`, `DeploymentReceipt`. Their contents are the named records/IDs and status/reasons described here; no hidden execution authority in a wrapper. Dependencies are explicit arguments; never ambient production paths.

## Review focus

1. A repeated raw broker observation cannot certify previously unexplained inventory — M1 two-pass protection test and post-restart counterpart in M2.
2. A cancel acknowledgement may race with another partial fill — M2 reserve final filled quantity until terminal/activity watermark proof, with no oversell.
3. A late fee/corporate-action correction can change a previously learned target — M2 outcome revision plus M4 exact input supersession/next-consideration tests.
4. A wall-clock jump or daylight-saving boundary can occur while an exit is due — M3 monotonic due-time and restart/calendar tests; do not defer the exit to a new entry tick.
5. An activation may commit immediately before a crash or an entry claim — M5 transaction/claim race tests; M6 process restart witness and ledger-preserving rollback.

---

## M0 — Sol implementation brief: reproduce operational truth

**Execution:** User approved implementation 2026-10-14. Owner remains the coordinating chat.
Isolated branch `codex/paper-core-m0` starts at refreshed main `b812c64270f190b0518322abb08b9a17dfa3f1c0`; PR287/288 heads remain unchanged. Evidence: [M0 record](../../evidence/paper-core/m0/README.md), [independent review](../../evidence/paper-core/m0/review.md). Final focused tests: 15 passed independently. Full regression initially had environment-related failures; every failure/error passed on corrected-harness rerun, with separate-run counts and skips preserved in the record. Owner and independent reviewer ACCEPT M0. No runtime deployment or kill-switch behavior change is claimed by M0.

**Scope:** Produce a read-only CLI/exporter that joins existing release/task/process/store identity and traces the three known historical exploration lots. It may inspect only the permitted Omen paths/account evidence; no broker mutation, clock repair, migration, task change or secret output. Do not implement a trader yet.

**Relevant existing code:** `operations/deployment.py`, `operations/inventory.py`, `operations/store_inventory.py`, `storage/database.py:Database.read_only`, `learner/experiment_store.py:read_as_of`, `learner/experiment_lot_reader.py`. Read the audit's live-path/source-pin distinction and `CLAUDE.md` operational truth rule. Existing user request supersedes older instructions to preserve all complexity.

**Create:** `paper_core/inventory.py`; `tests/integration/paper_core/test_inventory.py`; nonsecret `docs/evidence/paper-core/m0/` manifest and sample bundle. Use explicit `sqlite3 mode=ro/query_only` or verified `Database.read_only`, never runtime-building CLI commands.

**Interfaces:** `inspect_inventory(config: CoreConfig, *, as_of: datetime) -> InventorySnapshot`; `trace_legacy_lot(snapshot: InventorySnapshot, lot_id: str) -> EvidenceBundle`. Output includes source-store identity/cutoff, source release, lane, gaps and conflicting authorities. `CoreConfig` at this stage is a read-only path/account-hash manifest with no credentials.

**Deliverables/definition of done:** One deterministic export traces observation, intent, fill, protection, exit, flat and feedback for the XLI example and labels fee/learning gaps. It identifies authoritative operational DB versus copied learner DB and current first blockers. Read-only test verifies hashes/row counts/mtimes of fixture stores unchanged; importing twice does not count copied fills twice. Fresh observed runtime metadata is timestamped and cannot be mislabeled as fresh broker truth. No real-account values beyond necessary redacted evidence enter Git.

- [x] Write failing integration tests for copied-ledger duplication, stale deployment JSON, missing artifact and `read_only` access with newer schema.
- [x] Run `python -m pytest tests/integration/paper_core/test_inventory.py -q`; record the named red assertions.
- [x] Implement the exporter with explicit paths; record limitations instead of filling missing data.
- [x] Run the tests and a permitted read-only export; review the joined evidence manually against the known lot. Exporter success is not fee/learning success.
- [x] Commit exporter/tests/redacted evidence and the reviewed planning package in the isolated branch; independently review diff/evidence; update plan before handoff.

**Migration/rollback:** None. Delete no state; an exporter can be withdrawn without operational effect. Dependency for M1 is an accepted source/store/owner map, not a green test count.

## M1 — Sol implementation brief: a thin protected round trip

**Execution:** ACCEPTED at source commit `d24a7f45cf74c1b0079922172379090814634148`, following initial implementation `8ccdb39` and one independently reviewed fix round. [Review and evidence](../../evidence/paper-core/m1/review.md) verify the immediate synthetic round trip and expired-entry refusal followed by a fresh observation. Broad regression at `8ccdb39`: 4,336 passed, 11 failed, 47 skipped, one existing dependency warning. Every failed case and the final-send review defect passed in the final 325-test affected/retained run; full Ruff and both-platform type checks pass. Counts are separate runs, not a wholly green final-commit full-suite claim. M2 is now ready; operational deployment remains unchanged.

**Scope:** Build the new path in temporary stores with an explicit synthetic broker, plus repair the reused protection baseline boundary. Demonstrate the complete trading-to-outcome mechanics using one eligible symbol; outcomes may correctly be COSTS_PENDING until M2. No deployed task or broker writer changes.

**Relevant code:** `execution/coordinator.py`, `execution/recovery.py`, `execution/reconciliation.py`, `storage/repositories.py`, `operations/protection.py:267,285,448`, `learner/paper_runner.py`, `learner/paper_deadlines.py`, `learner/five_minute.py`, `risk/`, and the saved expected-red protection probe. Retain client-ID generation, pre-send deadline handoff, fixed limits, manual-stop checks and immutable fill IDs. Retire none of the existing exits in this milestone.

**Create/modify:** New `paper_core/contracts.py`, `store.py`, `features.py`, `trader.py`, `guardian.py`, `simulation.py`, basic `runtime.py`; explicit additive migration for core identity/observation/decision/protection/certification tables. Modify reused protection read/write paths to separate raw observations and certified inventory. Add `tests/integration/paper_core/test_thin_loop.py`, `tests/fault/paper_core/test_protection_authority.py`.

**Interfaces:** `record_observation(store, observation: Observation) -> str`; `evaluate_entry(config: CoreConfig, observation: Observation, model: ModelArtifact, inventory: ReconciliationProof) -> Decision`; `claim_entry(store, decision: Decision, plan: ProtectionPlan) -> EntryIntent | None`; `guardian_cycle(store, broker, *, now: datetime) -> CycleReceipt`; `certify_inventory(store, proof: ReconciliationProof) -> int`. `ModelArtifact` may be the explicitly labeled nonpredictive COLLECT baseline; no fitted-model claim. Existing `ExecutionCoordinator` is the only submit/recovery primitive.

**Integrated assertions:** One causal synthetic observation yields one recorded eligible decision, one deterministic intent and durable protection handoff before fake POST, owned fill, independent stop/deadline exit, terminal order and reconciled flat lot. Killing/stalling trader activity cannot prevent guardian exit. Duplicate observation/intents cause exactly one fake POST. Raw mismatched quantity remains uncertified on repeated passes; only fill-derived proof can certify. Exit can never exceed owned quantity or consume inherited positions. A manual pause arriving between decision and final claim prevents POST.

- [x] Add the thin-loop tests and copy/adapt the two-pass expected-red authority probe into owned suite tests.
- [x] Run `python -m pytest tests/integration/paper_core/test_thin_loop.py tests/fault/paper_core/test_protection_authority.py -q`; preserve red evidence.
- [x] Implement only the path and authority repair described; leave optional research disconnected.
- [x] Execute the integrated trace, duplicate/race tests, retained execution/deadline regression and proof export. Examine order sequence and quantity conservation, not just pytest status.
- [x] Commit and obtain independent diff/evidence review; record known cost incompleteness and accepted interfaces in this plan.

**Integration-owner ruling:** Historical snapshots do not become certified merely because they lack a new raw marker. Matching legacy reconciliation status/run/time is also insufficient: the old runner can ingest broker cash before comparing against that same snapshot. Only affirmative, independently validated certification may establish the new protection baseline. Preserve all unproven history and require explicit ownership handover at M6. Protection fixtures must establish synthetic certified baselines instead of silently trusting bare snapshots.

**Migration/rollback:** Schema changes exercised only in fixture copies. Production protection remains running; source repair has no deployment claim. Revert code on isolated branch if rejected. Done requires joined evidence and persistent two-pass refusal, not a mocked assertion of protection health.

## M2 — Sol implementation brief: cost provenance and crash recovery

**Execution:** ACCEPTED at source `800e67bf5d15e3cf1d878b46f50775dc0bdf6f82`, following initial `0d9510e` and independently reviewed repairs `63970bb` and `800e67b`. [Independent review](../../evidence/paper-core/m2/review.md) closed all findings and accepted the integrated cash/restart/cost/quarantine evidence. The initial full regression recorded 4,372 passes, 21 failures and 47 skips; all 21 failures passed in the stable 398-pass/26-skip repair gate. The final six-line collision-scope repair passed 16 adjacent cases, then the expanded third-lot regression; exact lint and both-platform type gates passed. Counts are separate runs, not a final-head full-suite claim. M3 is ready. Provider completeness, disposable PostgreSQL execution and deployment retain documented limits.

**Integration-owner rulings:** A quantity-only proof permits attributable protection but never grants cash authority. Complete independently sourced cash activity may establish current cash while outcome fees remain pending; do not require every past outcome to be fee-complete before future entries. A relevant cost/source revision must invalidate obsolete cash authority transactionally, and the final authority check must share the transaction that enters SUBMITTING. Expired pre-send claims may release their slot only through durable proof and a state fence that prevents a delayed original sender from POSTing. Broker absence alone never proves an intent unsent. Rejected batches preserve the complete envelope while quarantining only the actually implicated evidence and lots. Every repair needs an integrated progress/race witness, not only a refusal assertion.

**Scope:** Make the thin loop durably correct across ambiguous POST, partial/cancel races, restarts, fees and late evidence. Completed outcomes run without fresh observations or entry eligibility. This milestone owns outcome/cost contracts, not predictive learning or timing-rule changes.

**Relevant code:** `domain/models.py:Fill`, `brokers/alpaca.py:_fill_from_payload`, `execution/coordinator.py`, `execution/accounting.py`, `learner/experiment_lots.py`, `experiment_lot_reader.py`, `exploration_feedback.py` main's revisions, `storage/database.py`, and PR287's immutable lineage concepts. Preserve canonical fill/intent IDs. Retain cash/quantity tolerances and uncertain-submission rules.

**Create/modify:** `paper_core/outcomes.py`, `costs.py`, core store extensions for source cursors/cost items/outcome revisions. Modify provider/domain fee ingestion to nullable/provenance-aware values without rewriting history. Explicit migration/backfill maps every legacy unproven zero to unknown provenance. Create `tests/integration/paper_core/test_costed_loop.py`, `tests/fault/paper_core/test_restart_matrix.py`, `tests/fault/paper_core/test_outcomes_independent.py`.

**Interfaces:** `import_broker_events(store, events: tuple[BrokerEvent,...]) -> tuple[str,...]`; `reconcile_round_trip(store, lot_id: str, *, as_of: datetime) -> RoundTrip`; `process_outcomes(store, *, cutoff: datetime) -> OutcomeBatch`; `recover_intents(store, broker) -> CycleReceipt`. Recovery imports evidence using the same client ID and does not expose a “retry unknown POST” method.

**Required outcomes:** Quantity and cash conservation hold across multiple partial fills; exit reservations prevent concurrent stop/deadline oversell. Crash points before claim, after claim/before send, after send/before ack, during fill import, before/after outcome commit each recover without a second POST or duplicate sample. A 404 after an ambiguous POST remains uncertain. Empty fills cannot become a completed round trip. Outcome status is COSTS_PENDING for missing fee evidence; known zero requires documented authority. Late fee revision updates one current outcome while preserving earlier versions. Actual gross/net and modeled economics are separate, with no second slippage deduction.

- [x] Implement failing tests for every restart cut and the fee-source probe, with credential-free fake broker logs persisted across subprocess restarts.
- [x] Run `python -m pytest tests/integration/paper_core/test_costed_loop.py tests/fault/paper_core/test_restart_matrix.py tests/fault/paper_core/test_outcomes_independent.py -q` and capture red evidence.
- [x] Implement provenance, single-store transaction boundaries and replay-safe import. Use main's revision behavior; do not reintroduce b496109's one-receipt-per-lot limitation.
- [x] Run the same full scenario in a fresh process, replay all events twice, and compare current outcome IDs, quantities and money. Close market and remove entry data; prove completed outcome still appears within a healthy ≤15 s worker poll.
- [x] Restore a consistent fixture backup and re-import subsequent events; prove no lost/duplicated evidence. Commit, independently review and update plan.

**Migration/rollback:** Test additive migration on copies of all legacy store variants identified in M0. Backups use SQLite online backup and restore integrity check. Unknown historic cost is never “fixed” by overwriting a zero. No production import yet. Once deployed later, rollback cannot restore pre-trade state over newer broker events.

## M3 — Sol implementation brief: scoped recovery, timing and useful health

**Acceptance:** Independent reviewer and integration owner ACCEPT source `1730b658f1a67132525abdfaab40547f0a3e624c`. [Evidence](../../evidence/paper-core/m3/README.md) and [review](../../evidence/paper-core/m3/review.md) retain the initial findings and repairs. Final affected gate: **424 passed, 26 skipped**, six CI gates passed. The eight-stage synthetic timeline proves scoped refusal/recovery with independent owned exits and two costed outcomes; healthy fetch latency, conservative close-buffer expiry, integrity-first blockers and populated prior-schema identity replay are verified. Actual M3 OS-process timing restart, operational guardian readiness, retry scheduling, provider commissioning and PostgreSQL execution remain explicit M6/infrastructure limits. No runtime behavior was changed.

**Scope:** Replace blanket stops for acquisition uncertainty with scoped states, remove unrelated-symbol entry coupling, and implement the explicitly reviewed timing contract in both entry and guardian. Preserve genuine contradictions/manual pauses. This milestone may follow M2 independently of M4, but its execution still requires the owner's next-ready handoff.

**Relevant code:** PR288 `operations/paper_clock.py`, `execution/snapshot_capture.py`, `execution/recovery.py`, `operations/cycle.py`, `learner/paper_runner.py`, `paper_deadlines.py`, `exploration_launch.py:249,348`, `market_data/calendar.py`, `five_minute.py`. Read the saved host time query and proposed V2 contract; passing PR288 tests is not approval to silently deploy its health predicate.

**Create/modify:** `paper_core/time_evidence.py`, `health.py`, trader/guardian adapters. Tests: `tests/fault/paper_core/test_timing.py`, `test_transient_recovery.py`, `tests/integration/paper_core/test_scoped_progress.py`. Model/risk/holdout gates are unchanged.

**Interfaces:** `acquire_time_evidence(broker, clocks) -> TimeEvidence`; `entry_time_objections(observation: Observation, evidence: TimeEvidence, config: CoreConfig) -> tuple[str,...]`; `exit_due(plan: ProtectionPlan, evidence: TimeEvidence | None, elapsed_seconds: float | None) -> bool`; `record_health(store, event: Health) -> str`. Timing uncertainty cannot supply ownership; exit_due is a trigger only, not permission to sell.

**Acceptance:** ±2.000 s boundary honored both directions; ±2.001 s rejected. Two samples must advance, agree and finish in ≤5 s; proof expires after 120 s and on process restart/clock discontinuity. Boundary overlap waits for entry and does not extend owned holding time. Windows status text does not grant or fabricate UTC accuracy. Changing marks or 429/timeout pauses affected entries; fresh complete proof recovers without operator reset or global switch mutation. True unowned quantity/cash contradiction remains quarantined until explicitly resolved by proof, not another identical snapshot. One missing roster symbol blocks only decisions that actually depend on it; missing SPY still blocks features requiring SPY. Guardian and trustworthy outcome/learning work continue through each entry refusal. Old broker marks cannot become current by stamping receipt time; independent deadline exits remain available.

- [x] User authorized implementation of the reviewed changes on October 14, 2026. Implement and test the specified V2 contract locally; exact operational commissioning remains subject to the M6 artifact review. No OS resync or policy change is authorized.
- [x] Add and run the three named test files with exact boundary/race assertions and an equivalent new-core unstable-acquisition case; the original PR288-dependent audit probe remains historical evidence.
- [x] Implement classification and shared timing evidence; retain historical stop records and distinguish manual/integrity pauses from new retryable waits.
- [x] Run an integrated sequence: valid entry, provider timeout, stale unrelated symbol, market close, wall jump, recovery; show zero unauthorized POSTs, continued exit/outcome progress and fresh-evidence-only resumption.
- [x] Export first-blocker timelines and test logs; independent review then plan update.

**Migration/rollback:** No existing stop automatically changes class or clears. Historical stops need separate reason attribution/recovery review at rollout. New health states only apply to new events. Old clock profile remains selectable for rollback with matching entry/guardian config; never mix profiles. No w32tm write, task, registry, access or script-policy action.

## M4 — Sol implementation brief: bounded learning from costed trades

**Scope:** Implement actual numerical updates in shadow from M2 fee-complete outcomes under the proposed fixed `owned-executed-net-return-v1` contract. No model activation or broker authority. Design/learning-envelope approval is prerequisite. A failure/no-op must produce a truthful receipt.

**Relevant code:** `learner/estimators.py`, canonical training/holdout/trial gates in `experiment_research.py`, `experiment_adaptation.py`, `experiment_contract.py`, `research/registry.py`; M2 outcome revisions. Reuse deterministic ridge implementation, not the entire experiment scheduler. Old fixed-horizon market-label targets must never be mixed with executed-exit targets.

**Create/modify:** `paper_core/learning.py`, immutable learning/model storage extensions; `tests/integration/paper_core/test_learning_receipts.py`, `tests/fault/paper_core/test_learning_boundaries.py`. The fit subprocess gets an immutable eligible input export and scratch result path, no broker credentials and no operational DB handle. Do not claim OS sandbox isolation or change security settings to create it.

**Interfaces:** `consider_learning(store, policy: LearningPolicy, *, cutoff: datetime) -> LearningReceipt`; `fit_candidate(outcomes: tuple[RoundTrip,...], parent: ModelArtifact, policy: LearningPolicy) -> ModelArtifact`; `validate_candidate(candidate: ModelArtifact, parent: ModelArtifact, validation: tuple[RoundTrip,...]) -> dict[str,float]`. Permanent trial charge occurs before fit invocation; input selection/availability/embargo/holdouts are checked before data materialization. Recorded validation includes per-session metrics, IDs and exclusions, not only the metric dictionary.

**Definition of done:** Numerical coefficients change for the registered synthetic positive/negative feature signal. Exact replay yields same artifact. With <30 training episodes, fewer sessions, missing costs, protected data, incompatible policy or reused input, record deterministic NO_CHANGE or a named refusal without fitting. Fit failure consumes one trial and preserves parent. Late fee revisions do not add a sample; old model retains its old input references and next consideration cites the corrected revision. Every update or justified NO_CHANGE records parent=result/new model, input IDs, validation or explicit reason absent, and null activation time. Sim/real/synthetic data cannot enter another lane's training.

- [ ] Add tests for all fixed numeric envelope values and a canary protected row that must never reach the estimator.
- [ ] Run `python -m pytest tests/integration/paper_core/test_learning_receipts.py tests/fault/paper_core/test_learning_boundaries.py -q`; save red assertions.
- [ ] Implement fixed deterministic fitting and failure receipts; no tuning loop, regime search or automatic risk change.
- [ ] Produce two integrated M2→M4 bundles: a real parameter update from synthetic costed trades and a justified NO_CHANGE. Include prior/resulting coefficient values and validation split evidence.
- [ ] Review target semantics, selection bias and leakage independently; commit and update plan before activation work.

**Migration/rollback:** Legacy data is compatible only through explicit target/policy/cost contracts. Do not train from copied DB counts, unknown fee zeros or existing market-label artifacts as if they were executed outcomes. Fitting is shadow only; rollback leaves parent and immutable failed/candidate records intact.

## M5 — Sol implementation brief: activation and whole-loop proof

**Scope:** Connect a validated M4 candidate to an atomic active-model pointer and prove a later production decision used it. Demonstrate full loop in synthetic tests and a separate local simulation lane using existing permitted real market data. No genuine broker submission yet.

**Relevant code:** `experiment_activation.py`, PR287 `experience_continuity.py`, `experience_consideration.py`, `experiment_switch.py`, M1 claims/guardian, M2 outcomes, M3 timing, M4 artifacts. Reuse exact artifact/review checks where they fit, but do not claim unwired PR287 functions are an integrated solution.

**Create/modify:** `paper_core/activation.py`, trader runtime model read, simulation fill/cost adapter; `tests/integration/paper_core/test_learning_changes_decision.py`, `test_real_data_simulation.py`, `tests/fault/paper_core/test_activation_races.py`.

**Interfaces:** `activate_model(store, *, expected_model_id: str, candidate_id: str, authority: ActivationAuthority, at: datetime) -> Activation`; `read_active_model(store) -> ModelArtifact`; `rollback_model(store, *, expected_model_id: str, target_id: str, reason: str, at: datetime) -> Activation`; `export_loop_evidence(store, *, decision_id: str) -> EvidenceBundle`. Activation atomically binds pointer/event/digest and fences claims; never changes open-lot exit ownership or historical model attribution.

**Acceptance:** Use 30 train/10 later validation synthetic round trips across 5+5 sessions, balanced ±20 bps target linked to a causal feature, incumbent −20 bps. Ridge beats constant/incumbent and its positive-feature prediction exceeds the 10 bps modeled reserve. The **same later observation and gate state** abstains with incumbent and buys with candidate after real activation code. Assert used artifact digest and receipt ID, not merely registry existence. A competing activation, crash at every commit edge, wrong digest, stale approval, or unresolved submission cannot expose an unapproved model. NO_CHANGE preserves pointer and decision behavior. Rollback retains all subsequent fill/learning history.

- [ ] Write and run the named synthetic activation/race tests; require the existing production decision function in both before/after comparisons.
- [ ] Implement atomic activation, acknowledgement, restart selection and reviewed compatibility lineage. No auto-activation without a commissioned authority envelope.
- [ ] Build local simulation from recorded real market observations with provenance, spread/latency/partial-fill/reject scenarios, explicit missing-cost assumptions and no broker credentials. Bars-only data cannot pretend to provide observed NBBO/queue execution.
- [ ] Export the entire joined chain and show lane separation. Run 50 deterministic synthetic round trips under load as a **capacity** test; no claim they occurred naturally or on one real trading day.
- [ ] Independent diff and evidence review; update plan. Simulated economic returns are not acceptance of profitability or broker-PAPER execution.

**Migration/rollback:** No operational model switch. Test all migrations/backups on fixture copies. Compatibility receipts retain evidence across code-only maintenance; feature/target/exit changes refuse inherited training without explicit mapping. Recovery of interrupted activation never un-fences entries just because the service restarted.

## M6 — Sol implementation brief: commission one reproducible PAPER runtime

**Scope:** Prepare and review an exact release, backup/rollback package and handover; then perform only the approved rollout. Verify a **naturally eligible** broker-PAPER round trip separately. This brief is not approval to execute rollout now.

**Relevant code:** `deploy/omen/service-host.cs`, `start-service.ps1`, `start-learner.ps1`, `setup-windows.ps1`, `operations/service.py`, current task inventory, M0 exporter, M1–M5 core. Existing standard account, credentials and security permissions remain unchanged. Check actual runtime identity; this review's shell access is not proof of service-token access.

**Create/modify:** `deploy/omen/start-paper-core.ps1`, finalized `paper_core/runtime.py` / `__main__.py`, one versioned config and release manifest, `tests/integration/paper_core/test_release_recovery.py`, `docs/evidence/paper-core/m6/` nonsecret evidence bundle and operator runbook. Retire optional task dependencies in the manifest; execute retirements only after protection/ownership acceptance.

**Interfaces:** `prepare_release(config: CoreConfig) -> PreparedRelease` has no trading side effects; `verify_release(release: PreparedRelease) -> EvidenceBundle`; the approved rollout procedure returns `DeploymentReceipt` naming actual processes/tasks/store schema/ownership generation. Runtime role CLI is `python -m quantbot.paper_core --config ABSOLUTE_PATH --role trader|guardian|outcomes`; status/export are explicitly read-only subcommands. No production paths are defaults in tests.

**Required preparation before rollout review:**

- [ ] Resolve fresh main/PR heads and conflicts; pin exact code, frozen lock, interpreter, installed dependencies, schema/config/model/clock/learning authority. Validate clean release and applicable local CI offline; do not trigger paid hosted CI.
- [ ] Online-back up authoritative stores and canonical holdout/trial data, archive nonsecret task XML/config, integrity-check and restore to a new temporary path. Document prior compatible binary and forward-only recovery if schema rollback is unsafe.
- [ ] Run a startup/restart under existing permitted service identity/policy with **submission disabled**; demonstrate manifest, current time-proof availability, store access, first blocker and independent guardian/outcome scheduling. If permissions are unavailable, report the exact blocker; do not elevate or change policy.
- [ ] Present exact rollout and rollback commands, release identity, migration result, inherited/open/unknown-order ownership map, proposed role/task changes, selected initial model and authority to the user. Await required rollout/model/clock-contract review; no generic yes substitutes for missing exact artifacts.

**Approved execution sequence:** fence/stop old entry writers; verify process-tree shutdown and no unknown pending sends; maintain all existing protection; perform explicit additive migration/import; reconcile broker activity/positions/cash fresh; start guardian and outcomes; acknowledge owned lots and compatible inherited-owner isolation; verify time and readiness; atomically transfer sole entry generation; start trader. Only then retire redundant optional tasks with no outstanding ownership. Never clear an existing manual/integrity stop as part of installation.

**Observable definition of done:** All role heartbeats cite the exact manifest and same store. Restart preserves claim/model/owner and resumes outcomes. A naturally eligible PAPER decision, observed market source, intent/client order, attributable entry fill, durable protection/deadline, exit fill, terminal status, lot-flat reconciliation and fee-proven outcome are exported and independently checked against fresh broker read-only evidence. A learning receipt records update or justified NO_CHANGE; a later actual decision (which may abstain) cites the resulting active model. With insufficient natural trades the correct learning receipt is NO_CHANGE. Synthetic M5 proves the code can change a decision; no real model is forced to change.

The acceptance state remains WAITING if the market is closed, no eligible signal occurs, fee provenance is unavailable or ownership/timing evidence is missing. Record that first blocker and expected next event; do not trade to complete the checklist. Future monitoring requires a separate requested schedule, not an implicit automation.

**Rollback after any broker activity:** fence new entries first; keep guardian/outcomes running; recover by original client IDs; choose the compatible prior release/model with audited generation change. Preserve all new rows. If prior binary cannot read the new schema or protect current lots, remain in the new protection/recovery-only mode and repair forward. Restoring the pre-rollout DB over later broker events is forbidden. Record rollback as evidence, not erased history.

## Evidence gate after every milestone

Each submission includes: exact tested code+config+schema; named tests with pass/fail/skip and environment; integrated input/event/decision/output trace; lane and cost provenance; fault-injection/restart evidence; known limitations; migration/rollback proof; reviewer identity and findings. The integration owner compares this to the brief, updates acceptance state and hands off only the next ready milestone. **No milestone is complete solely because its unit tests pass.**
