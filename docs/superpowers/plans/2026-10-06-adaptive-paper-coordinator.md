# Adaptive PAPER Coordinator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. Native execution is recommended here; the user must review this written plan and select the method before product implementation.

**Goal:** Deliver the approved Omen research-to-PAPER-to-learning loop and coherent research/data, execution/protection, learning/update, and performance dashboards, with experiment/model drilldowns and independent forward acceptance.

**Architecture:** One durable experiment coordinator owns observation, decision, outcome and bounded research transitions. Existing PAPER gateway, independent protection/deadlines, risk and canonical research guards retain authority. A separate additive SQLite experiment store feeds read-only FastAPI/Jinja dashboard views; no second application or order-submit UI is introduced.

**Tech Stack:** Existing Python 3.11+, Pydantic, SQLite/SQLAlchemy, FastAPI/Jinja, pytest, Ruff and strict mypy. Existing provider/broker adapters and frozen dependencies; no new dependency, GPU, paid AI or hosted-runner requirement.

**Spec:** `docs/superpowers/specs/2026-10-06-adaptive-paper-coordinator-design.md`, approved at commit `2b21f2ec8af19b50a9f8cff005338c8ee2984df5`. Dashboard coverage was explicitly confirmed with written-spec approval.

## Global Constraints

- PAPER only. No live routes, credentials/access expansion, paid providers, billable CI or hardware stress.
- One new learner position; maximum 5% value, 0.25% entry risk, 1% stop, 30-minute horizon; preserve every tighter existing control and kill switch.
- Grandfathered positions retain original ownership and aggregate risk treatment; no reset, implicit sale or inferred exit attribution.
- Seven genuine completed five-minute bars, 35-minute warmup, newest required cutoff within 120 seconds, and the full horizon before actual session close.
- Six to ten metadata/coverage-qualified research instruments frozen before performance inspection/fitting; exact deployed roster still needs activation review.
- Raw instrument 30-minute return target, ten existing price features, constant baseline and pooled ridge alpha 1/seed 0; no quadratic or news alpha in this release.
- Ten-basis-point round-trip floor retained. Assumed-cost simulations and actual fee-bearing fills are separate evidence.
- Protected history guarded before reads; at least 20 valid training sessions and five validation sessions, purged chronological boundaries.
- At most six new engine calls: initial constant/ridge and four ridge successors. Permanent pre-call canonical charge, failed calls included; existing burden never reset.
- Successor eligibility after five additional coverage-valid sessions and sufficient matured labels. No automatic active-model promotion.
- Five-session zero-eligibility review; 20 valid performance-day evaluation within 30 scheduled sessions from frozen prospective start. No quotas, forced fills, window resets or guaranteed profits.
- Daily profitable percentage includes all valid days, flat included as nonprofitable; missing/invalid days excluded and disclosed. No live switch at any percentage.
- Source-only tests lack real broker credentials. Runtime/provider receipts remain private; publish only sanitized source/test evidence.

## Review Focus

1. Concurrent wakeups or uncertain submission must not duplicate entries or restore an old owner over an outstanding intent: Tasks 2 and 5.
2. Protection-initiated partial exits, legacy inventory and ambiguous fees must remain correctly owned or incomplete: Task 4.
3. Late news, corrected labels, reset/deposit evidence and missing previous closes must not create hindsight or profit: Tasks 3, 6 and 8.
4. Early closes, clock failures, DST and expected off-hours waits must not misclassify a dead component or healthy waiting worker: Tasks 3 and 9.
5. Fit crashes, exhausted budgets, consent already recorded and delayed approvals must not create free trials, misleading requests or reset evaluation windows: Tasks 2 and 7.

## Execution and verification convention

Implementation starts only after plan review. Reuse this task's context, but create an isolated execution worktree through the worktree skill. Do not modify deployed release trees while building source. Inspect applicable repository instructions in that worktree and preserve user work.

Each product task uses red/green tests, targeted lint/type checks and a small local commit. For commands below, `$planPython` is the approved verification interpreter, and `$planTemp` a unique workspace test directory for that invocation. Set `PYTHONPATH=src`; tests use synthetic providers/stores, never the real application credential context. Run:

`& $planPython -m pytest <task test paths> -q --basetemp $planTemp`

Run the named tests red before implementation (a genuine absent/incorrect behavior, not an unrelated environment error), then green. Lint changed source/tests and type-check changed modules on both `linux` and `win32` targets. Final aggregate and frozen-dependency checks are Task 10, once for the final candidate; do not rerun expensive suites after documentation-only changes.

No implementation code, dependency installation, fits, provider reads or runtime writes have occurred during preparation of this plan. Task interfaces below are planned contracts, not claims that they exist.

## File and contract map

Keep new coordinator modules in `src/quantbot/learner/`, each with one responsibility:

| New module | Responsibility |
| --- | --- |
| `experiment_contract.py` | Immutable manifest, typed envelopes and guard/charge protocols |
| `experiment_store.py` | Additive store, transitions, leases and outbox |
| `experiment_observation.py` | Frozen roster, per-instrument genuine coverage and labels |
| `experiment_news.py` | Existing public research inputs and shadow thesis provenance |
| `experiment_lots.py` | Durable lot/order ownership and economic revisions |
| `experiment_execution.py` | Existing PAPER gateway adaptation and dispatch |
| `experiment_coordinator.py` | One bounded tick and recoverable workflow |
| `experiment_research.py` | Precharged constant/ridge and successor scheduling |
| `experiment_evaluation.py` | Matched forward comparisons and stopping rules |
| `experiment_launch.py` | Pinned native launcher, clocks and limits |

`console/experiments.py` owns one shared read model; existing `console/app.py` and Jinja templates render it. Reuse `operations/daily_performance.py`, `close_capture.py`, `console/performance_reader.py` and calendar components rather than duplicate accounting.

Task 1 defines Pydantic models used below: `ExperimentManifest`, `InstrumentIdentity`, `CoverageReport`, `ObservationBatch`, `DecisionRecord`, `ExperimentState`, `TickReceipt`, `LotEvent`, `EconomicOutcome`, `FitRequest`, `FitReceipt`, `ActivationReceipt`, `EvaluationReport`, `NewsEvidence`, and `ThesisRecord`. Use existing `FiveMinuteFeatureRow`, `FiveMinuteLabel`, `OrderIntent`, `PaperModelBinding` and broker/storage types where applicable. Define `CanonicalGuard.permits(start: datetime, end: datetime) -> bool`, `TrialCharger.used_calls(experiment_id: str) -> int`, and `TrialCharger.charge(trial_id: str, manifest_hash: str) -> None`; implementations adapt existing canonical research authority, not a new local replacement registry. Canonical charging must atomically enforce the registered remaining budget, not rely on a separate read/count race.

### Task 0: Resolve the source baseline and compatibility matrix

**Files:** create `docs/adaptive-paper-compatibility.md`; inspect existing learner, console, monitor, protection and news modules plus `scripts/ci_checks.py`. No runtime mutations.

The planning branch contains the tracking work, but native services use separately pinned releases. Do not assume it contains every later deployed change. Build a sanitized table of revisions, package interfaces, operational schema, ownership and retained fixes. In particular, preserve newer monitoring and position-research behavior if absent from this branch.

Assessment refs to verify at execution start: entry `5c1db64f427c45f9ee99f8710568a1b82b59b3ef`; observer `054ed1ac2a07893a867fe75de5632e10e92b9bd9`; console/monitor `4e643ae6aef82ad6def47f9a2d920b616b7cc14f`; protection/deadline/close `90490a3aabae5c49c2381d97c322cf3ba2b4d045`; news `4ee6e5ff82355dfb2821f9f187406e661ae875ee`; tracking `2ccd3d22a62082c8e617a025ea73088f7f751278`. These are previously verified source refs, not a claim of a fresh runtime inventory. The approved design commit is the planning base; runtime pins are never changed by source integration.

- [ ] Read exact source refs, relevant instructions and previous tests; record missing/conflicting interfaces without exposing private runtime artifacts.
- [ ] Select an execution base containing approved safety/monitor/calendar/news fixes, using source-only integration in the isolated worktree; never silently merge unrelated chore PRs.
- [ ] Record operational schema compatibility and exact gateway/protection/news adapter entry points. If incompatible changes require a new design or operational migration, stop that portion with a concrete decision.
- [ ] Verify source diffs and run only relevant retained regressions; commit the compatibility document and any explicitly necessary source integration with its evidence.

**Checkpoint:** known base, retained behavior and protection interface. No provider calls, fits or deployment merely to establish the base.

### Task 1: Immutable experiment contracts and canonical authority adapters

**Files:** create `learner/experiment_contract.py`, `tests/unit/learner/test_experiment_contract.py`; reuse `learner/five_minute.py`, `research/manifest.py`, canonical guard/trial interfaces.

**Interfaces:** `validate_manifest(manifest: ExperimentManifest, *, guard: CanonicalGuard) -> str` returns the content hash; `ExperimentManifest` binds roster, features, raw target, dates, costs, budgets, window and risk policy versions. All envelopes bind parent IDs and actual UTC availability; exchange-day dates remain calendar-derived.

- [ ] Write failing tests `test_manifest_fixes_limits_and_budget`, `test_guard_precedes_archive_open`, `test_metadata_identity_not_performance_selects_roster`, `test_invalid_target_or_live_mode_rejected`. Assert six-call cap, floor >=10 bps, 1/5%/0.25%/1%/30-minute limits, protected access never invoked and six-to-ten qualified identities.
- [ ] Run the contract tests red.
- [ ] Implement strict, immutable contracts, finite numeric checks, source/version hashes and explicit approval status. Freeze dates and roster before performance reads; record rejected-screening identities separately.
- [ ] Run tests green plus changed-file lint/types.
- [ ] Commit `feat: define immutable adaptive paper experiment contracts`.

### Task 2: Durable store, leases, transitions and evaluation window

**Files:** create `learner/experiment_store.py`, `tests/unit/learner/test_experiment_store.py`.

**Interfaces:** `ExperimentStore(path: Path, *, create: bool = False)`; `append(record: BaseModel, *, idempotency_key: str) -> bool`; `transition(experiment_id: str, *, expected_revision: int, target: str, reason: str, now: datetime) -> ExperimentState`; `acquire_lease(experiment_id: str, *, owner: str, now: datetime, duration: timedelta) -> bool`; `read_as_of(experiment_id: str, now: datetime) -> tuple[BaseModel, ...]`. Transactional outbox records are not broker acknowledgements.

- [ ] Write tests `test_unrelated_database_refused`, `test_repeated_event_is_idempotent`, `test_conflicting_evidence_retained_and_faulted`, `test_two_wakeups_only_one_owner`, `test_evaluation_start_never_resets_on_delay`, `test_consent_and_activation_are_distinct`. Assert lease race has one winner and stale revision cannot transition.
- [ ] Run red.
- [ ] Implement separate SQLite schema v1 for manifests/events/transitions/leases/outbox/ownership. Use transactions and append-only revisions; no operational DB migration. Preserve fixed start and 30-session limit through restart.
- [ ] Run green, including process/crash transaction recovery tests; lint/types.
- [ ] Commit `feat: persist experiment lifecycle and exclusive coordinator lease`.

### Task 3: Multi-instrument observations and provenance-safe news shadow

**Files:** create `learner/experiment_observation.py`, `learner/experiment_news.py`; tests `test_experiment_observation.py`, `test_experiment_news.py` in `tests/unit/learner/`. Reuse `forward_source.py`, `five_minute.py`, `anomaly/news.py`, `research/filings.py` and approved position-research adapters from Task 0.

**Interfaces:** `build_observation_batch(manifest: ExperimentManifest, snapshot: Snapshot, *, now: datetime) -> ObservationBatch`; `resolve_batch_labels(batch: ObservationBatch, snapshot: Snapshot, *, now: datetime) -> tuple[FiveMinuteLabel, ...]`; `build_news_evidence(identity: InstrumentIdentity, documents: Sequence[NewsEvidence], *, decision_at: datetime) -> tuple[NewsEvidence, ...]`; `journal_thesis(store: ExperimentStore, decision: DecisionRecord, evidence: Sequence[NewsEvidence]) -> ThesisRecord`.

The roster uses one pooled price-feature contract, not six-to-ten separately charged models. Each symbol needs its own complete panel plus genuine SPY benchmark inputs. A missing symbol yields its own coverage rejection; it cannot cause backward sliding or fake bars in another symbol's panel.

- [ ] Write `test_seven_bars_and_120_second_bound`, `test_missing_member_rejected_without_imputation`, `test_early_close_has_no_full_horizon`, `test_dst_open_uses_exchange_calendar`, `test_labels_include_vetoed_predictions`, `test_late_news_excluded`, `test_etf_company_filings_not_inferred`, `test_provider_failure_is_unknown_not_empty`.
- [ ] Run red.
- [ ] Implement per-symbol coverage/provenance, exact target-label resolution and immutable journals. Query existing providers only through approved adapters during later runtime stages. News records shadow buy/hold/exit context with no execution authority; absent SEC contact remains explicitly unavailable.
- [ ] Run green; prove future first-seen/retrieval evidence cannot enter past decisions and all valid labels deduplicate by feature/target identity; lint/types.
- [ ] Commit `feat: connect qualified observations and provenance-safe research context`.

### Task 4: Complete learner-lot and protection-exit attribution

**Files:** create `learner/experiment_lots.py`; modify `learner/paper_deadlines.py`, `operations/protection.py`, `learner/trade_learning.py` only at handoff/exit ownership boundaries; tests `test_experiment_lots.py`, existing trade/deadline/protection regression groups.

**Interfaces:** `record_lot_event(store: ExperimentStore, event: LotEvent) -> bool`; `resolve_lot(store: ExperimentStore, lot_id: str, *, now: datetime) -> EconomicOutcome`. Every event binds actual quantity, broker/client order identity, originating decision and owner. The new store maps existing durable deadline/stop orders without inferring old ownership.

- [ ] Write tests `test_protection_owner_exit_completes_owned_lot`, `test_partial_entry_protects_only_actual_quantity`, `test_partial_exit_residual_is_incomplete`, `test_missing_fee_is_not_zero`, `test_legacy_inventory_never_adopted`, `test_equal_time_ambiguity_not_guessed`, `test_restart_retains_exit_owner`.
- [ ] Run red.
- [ ] Add durable attribution links before acknowledgement completion; preserve protection independence and hard stop/time behavior. Query existing operational fills snapshot-consistently. Append outcome revisions; finalization requires matching terminal quantities, actual fees and post-fill reconciliation.
- [ ] Run green plus prior protective/deadline and trade attribution tests; lint/types. If the current durable operational interface cannot preserve ownership without a schema change, return the exact migration/compatibility requirement instead of fabricating a workaround.
- [ ] Commit `feat: attribute independent protective exits to learner lots`.

### Task 5: Existing PAPER gateway plus transactional dispatch

**Files:** create `learner/experiment_execution.py`; narrowly adapt `five_minute_paper.py`, `paper_runner.py` and `execution_worker.py`; tests `test_experiment_execution.py`.

**Interfaces:** `prepare_entry(manifest: ExperimentManifest, decision: DecisionRecord, *, binding: PaperModelBinding, services: IntradayPaperServices) -> OrderIntent | None`; `dispatch_pending(store: ExperimentStore, *, services: IntradayPaperServices, now: datetime) -> tuple[LotEvent, ...]`. Services come from existing PAPER construction, not a new submitter with reduced checks.

- [ ] Write `test_live_route_unconstructible`, `test_existing_risk_and_cost_vetoes_preserved`, `test_outstanding_buy_blocks_second_entry`, `test_uncertain_send_reconciles_before_retry`, `test_duplicate_tick_one_client_order`, `test_unowned_held_symbol_veto`, `test_protection_handoff_failure_blocks_new_entry`.
- [ ] Run red.
- [ ] Adapt the existing gateway with exact approved binding and exclusive entry ownership. Predeclared eligible ranking is greatest raw predicted return above the unchanged floor, lexical symbol tie-break; consider only fully valid symbols and retain every ordinary risk veto. No fallback threshold relaxation or order quota.
- [ ] Run green with synthetic broker call audit showing only PAPER endpoints and zero live calls; verify crash before/after acknowledgement and lease takeover; lint/types.
- [ ] Commit `feat: dispatch experiment entries through retained paper safety gateway`.

### Task 6: One recoverable tick and honest lifecycle progress

**Files:** create `learner/experiment_coordinator.py`, `learner/experiment_launch.py`; tests `test_experiment_coordinator.py`, `test_experiment_launch.py`.

**Interfaces:** `coordinator_tick(store: ExperimentStore, manifest: ExperimentManifest, *, now: datetime, observation: ObservationBatch | None, execution: IntradayPaperServices | None) -> TickReceipt`; `run_experiment(config_path: Path, *, once: bool = False) -> int`. Dependencies stay injectable; real order construction is unavailable in shadow mode.

`TickReceipt` includes event IDs, reason/next expected event, label/intent/fit counts and eligible `FitRequest` IDs. A tick may queue a request but never run fitting inline; the launcher's single isolated worker consumes immutable requests through Task 7. Clock verification is checked before new decisions and resampled after blocking provider work, not inferred from the injected timestamp alone.

- [ ] Write `test_observation_to_decision_to_outcome_recovery`, `test_dead_coordinator_does_not_stop_protection`, `test_clock_sample_after_provider_response`, `test_closed_session_still_resolves_pending_labels`, `test_collection_does_not_increment_fit_count`, `test_bad_configuration_cannot_reenable_entry`.
- [ ] Run red.
- [ ] Implement one bounded tick: verify pins/clock/manifest, acquire lease, resolve old evidence, reconcile outbox and lots, consume current coverage, record deterministic decision and permitted dispatch. Record next expected wake/event independently of economic eligibility. Launcher caps workers at two and isolates fitting from ordinary ticks.
- [ ] Run green with synthetic crash/restart timeline; lint/types.
- [ ] Commit `feat: coordinate the recoverable adaptive paper workflow`.

### Task 7: Precharged pooled fitting, successors and concrete activation packages

**Files:** create `learner/experiment_research.py`; reuse `estimators.py`, `adaptive_research.py`, canonical registration/guard APIs without changing old experiment behavior; tests `test_experiment_research.py`.

**Interfaces:** `research_eligibility(store: ExperimentStore, manifest: ExperimentManifest, *, now: datetime) -> FitRequest | None`; `run_registered_fit(request: FitRequest, *, guard: CanonicalGuard, charger: TrialCharger) -> FitReceipt`; `activation_package(store: ExperimentStore, fit: FitReceipt) -> ActivationReceipt`. Here `ActivationReceipt` is a proposed package until an authenticated exact-artifact approval is recorded; it cannot itself grant consent.

- [ ] Write `test_initial_constant_and_ridge_charge_before_engine`, `test_six_call_limit_includes_failed_timeout`, `test_repeated_fit_request_never_invokes_engine_again`, `test_20_train_5_validation_and_purged_boundaries`, `test_successor_waits_five_new_sessions`, `test_pooled_symbols_are_not_independent_session_counts`, `test_no_refit_of_seen_validation`, `test_artifact_approval_does_not_transfer_to_successor`.
- [ ] Run red with engine spies and canonical-charge failure injection.
- [ ] Implement one pooled constant and ridge using existing estimator primitives; do not call the old helper that automatically fits quadratic ridge. Freeze alpha 1/seed 0, raw target and chronological recipe. Persist a single-start execution fence for each request before invoking the engine; a crash between charge and completion remains charged and cannot invoke that request again. Set 15-minute fit wall limit; terminate only that child process, preserve its charge and diagnostic. Fit worker accepts only an immutable precharged request.
- [ ] Implement packages naming code/model/data/config hashes, roster, risk controls, evidence limitations, rollback and owner. A registered update yields subsequent fresh shadow predictions; explicit activation is required for subsequent active decisions.
- [ ] Run green with at most six spy calls, no real fits; lint/types; commit `feat: register bounded adaptive fits and artifact-specific activation packages`.

### Task 8: Independent evaluation and durable daily-account compatibility

**Files:** create `learner/experiment_evaluation.py`; minimally extend `operations/daily_performance.py`, `operations/close_capture.py` and `console/performance_reader.py` only if required for revisions/attribution; tests `test_experiment_evaluation.py` and existing close/calendar groups.

**Interfaces:** `evaluate_experiment(store: ExperimentStore, manifest: ExperimentManifest, *, now: datetime) -> EvaluationReport`; report separates forecast error, reference simulation, actual learner P&L and account equity, plus legacy/unattributed components and matched baseline windows.

- [ ] Write `test_forecast_gain_not_broker_profit`, `test_flat_day_in_denominator`, `test_missing_close_deposit_and_reset_not_profit`, `test_close_revision_keeps_prior_evidence`, `test_five_session_no_eligibility_review`, `test_30_scheduled_sessions_stop_without_reset`, `test_correlated_windows_not_independent_samples`.
- [ ] Run red.
- [ ] Implement counts using coverage-valid research sessions separately from cash-flow/reconciliation-valid performance days. Use existing durable close capture, exchange-day cutoff and early-close behavior. Report 20-day assessment only when valid; otherwise explicit insufficiency at the 30-session limit. Stop new research/entries for the experiment without stopping owned-position protection.
- [ ] Run green including signed USD, exact fees and missing/unrealized contribution accounting; lint/types.
- [ ] Commit `feat: assess forward paper economics without simulated-profit substitution`.

### Task 9: Coherent dashboards, drilldowns and truthful monitor states

**Files:** create `console/experiments.py`, templates `experiments.html`, `experiment_detail.html`, `_experiment_status.html`; modify existing `console/app.py`, `templates/base.html`, `learner.html`, `operations.html`, `research.html`, `calendar.html`, `_performance_summary.html`; narrowly adapt `learner/progress.py`, `operations/monitor.py`, reviewed alert catalog as needed. Tests: `tests/unit/console/test_experiment_panels.py`, `tests/unit/operations/test_experiment_health.py`, `tests/integration/test_experiment_console.py`.

**Interfaces:** `read_experiment_panel(store_path: Path, *, experiment_id: str | None, now: datetime) -> ExperimentPanel` defined in `console/experiments.py`. Routes `GET /experiments`, `/experiments/{id}`, `/api/experiments`, `/api/experiments/{id}` are read-only; use the same panel for HTML and JSON. Existing performance/calendar reader remains the sole P&L source.

Information architecture within the existing console:

| View | Required information |
| --- | --- |
| Research/data | Frozen hypothesis/roster, identity, coverage/rejections, provenance, news limitations, target and split dates |
| Execution/protection | Real intent/ack/fill/lot status, risk vetoes, independent stop/deadline/reconciliation, original owner |
| Learning/updates | Labels collected, actual fits/charges, model/artifact lineage, update eligibility, shadow versus active, exact pending decision |
| Performance | Calendar, signed net USD, learner/account/legacy separation, profitable/losing/flat counts, denominator, drawdown/sample/coverage |
| Experiment/model detail | Full linked decision/evidence/trade/update timeline, hashes, budget, next event, owner and baseline comparison |

- [ ] Write tests `test_panels_distinguish_labels_fits_and_fills`, `test_consent_recorded_not_ai_model_request`, `test_closed_warmup_wait_and_dead_worker_are_distinct`, `test_early_close_and_clock_rootcause`, `test_json_matches_html_and_no_order_post`, `test_missing_pnl_and_denominator_visible`, `test_news_content_escaped`.
- [ ] Run red.
- [ ] Implement coherent navigation and read-only views using approved dark theme: canvas #0E171B, cards #152329, text #EDF4F1, secondary #A0B2AE, profit #90E4AF, loss #FFA298, lime #C2EF8D. Preserve month navigation and accessible signs/text; missing/flat remain distinct. No demo values in production.
- [ ] Extend component-specific monitor expectations using actual calendar and evidence age. Preserve faults for unknown/dead workers; do not suppress held-position protection requirements. Update predefined catalog through the existing reviewed route before enabling new external codes/text.
- [ ] Run green; render desktop and narrow viewport with a clearly synthetic fixture, then later verify actual deployed unknown/real states. Compare screenshots with the approved layout; HTTP success alone is not visual QA. Commit `feat: expose adaptive paper progress and evidence across operator dashboards`.

### Task 10: Whole-candidate safety review and aggregate verification

**Files:** create `tests/integration/test_adaptive_paper_loop.py`, `tests/fault/test_adaptive_paper_recovery.py`, `docs/adaptive-paper-acceptance.md`; existing `scripts/ci_checks.py` unchanged unless an independently justified bug is found.

- [ ] Write a synthetic full-loop test asserting timestamped observation → versioned decision → PAPER intent/fill → independent protective exit → fee-bearing attributed outcome → registered successor engine spy → fresh next decision. Assert that each edge exists, no live call occurs, and engine-spy outcomes are not marked real performance.
- [ ] Run red if the integrated behavior is incomplete; complete only already planned integration, then green. Add kill-switch/clock/protection loss, duplicate submission, restart and rollback fault traces.
- [ ] Commit the final candidate, then run shared frozen checks: `uv lock --check`, frozen sync, Ruff `src tests`, both mypy platform targets, helper checks, and aggregate pytest following `scripts/ci_checks.py`. Use approved local/offline caches where possible. Environment-appropriate partitions are allowed only with collected-test accounting proving every test is covered; report skips/failures, not fabricated equivalence.
- [ ] Have one independent reviewer assess the exact candidate's safety/lineage/ownership changes and whole-branch evidence. Fix substantive findings and rerun affected checks; rerun aggregate only when changed behavior justifies it. Never weaken repository required protections or repeatedly launch blocked hosted CI.
- [ ] Produce the acceptance report with candidate hash, complete local checks, hosted/required-check status and synthetic-versus-real distinctions. Publish a focused draft PR only within existing authorized publication scope and sanitized content.

**Checkpoint:** source release-ready, not deployed, no real fitting engine calls made by tests. Unknown branch protection is an explicit merge limitation, not permission to bypass.

### Task 11: Controlled rollout and prospective acceptance ownership

**Files:** create `deploy/omen/adaptive-paper-rollout.ps1`, `deploy/omen/adaptive-paper-rollback.ps1`, `docs/adaptive-paper-runbook.md`; tests `tests/unit/operations/test_adaptive_rollout.py` cover manifest/task selection and aborts without registration.

- [ ] Write rollout dry-run tests proving secret files excluded, no unrelated task changed, source/config hashes pinned, protection left running and rollback cannot restore stale operational fills. Run red; implement scripts using existing limited scheduled-task pattern, then green and commit.
- [ ] Prepare exact current compatibility, consistent database backup/integrity/restore rehearsal, task export and ownership inventory. Obtain action-time approval for any genuinely necessary schema migration, protection outage, new security/access or different risk scope; this plan does not preapprove them. No arbitrary full release-tree credential copy.
- [ ] Merge/deploy only after reviewed exact head and required protections permit. Install coordinator shadow first with existing credentials and no protection interruption. Verify actual receipts, task cadence, compatible console readers and rendered dashboards. Retain an audit of old/new pins.
- [ ] Freeze actual roster/split/trial registration, verify canonical total and remaining budget immediately before authorized fitting. Obtain exact initial-artifact PAPER activation, then transfer exclusive entry ownership with no unresolved outbox/lot. Do not assume plan approval grants model promotion.
- [ ] Own the prospective acceptance until naturally eligible fill, independent protected exit, reconciled fee/outcome, registered real successor, fresh evaluation and authorized subsequent active decision are evidenced. If entry is vetoed, report the precise veto; evaluate five-/30-session stops honestly. No fabricated trade to finish a ticket.
- [ ] Verify account calendar and all four dashboard views against genuine receipts, plus continued protection/reconciliation. If failure occurs, stop new entries and rollback compatible pins without overwriting later fills; verify recovery evidence. Continue independent protection and auditable evidence after research stop.

## Evidence milestones and ownership

The implementer owns running tests, runtime checks and scheduled acceptance; do not leave an in-flight process unowned at turn end. Significant checkpoints name exact candidate, tests and remaining authority, not a count of labels presented as learning.

- M1: Tasks 0–3, coverage-qualified immutable experiment and shadow inputs.
- M2: Tasks 4–6, synthetic full execution/ownership/recovery with independent protection.
- M3: Tasks 7–9, bounded update workflow and every requested dashboard view.
- M4: Task 10, exact tested/reviewed source and merge constraints stated.
- M5: Task 11, actual shadow deployment, genuine provider/config/runtime receipts and visual QA.
- M6: actual fill/exit/attribution/update/authorized next active decision; partial gates disclosed.
- M7: 20 valid forward days or explicit 30-session insufficiency/earlier zero-opportunity stop.

No milestone is a profitability promise. User communication continues on the existing approved chat route, with deduplicated sanitized fields and reviewed catalog; no new automation or credentials. The fixed sample and model approvals may span multiple sessions. Safe independent work continues while a specific decision is pending.

## Plan review and execution choice

Recommend **Native** execution: the same worker implements these tightly connected interfaces in this Omen task, using small tested commits and one independent whole-candidate review. This preserves context and credits; synthetic fault tests and the final review address safety without spawning a worker per slice. Model/effort stays economical for routine tasks; stronger reasoning is reserved for substantive safety findings.

Alternative **Subagent-driven** gives a fresh implementer and reviewer per task, with more context and credit overhead. Select it only if that extra isolation is desired.

The written design is already approved. The remaining immediate decision is review of this plan and selection of Native or Subagent-driven execution. After that, invoke the corresponding execution skill and begin Task 0; do not ask to approve the design again. Exact roster/model activation and materially broader operational changes remain the separate decisions documented above.
