# Project Elrond: evidence-based PAPER redesign review

Review date: **October 13, 2026, America/New_York**. Runtime extraction: **2026-10-14 01:37 UTC** (October 13, 9:37 p.m. Eastern). This is a review and proposed migration, not a deployment or permission to change operational state.

**Recommendation:** incrementally replace the orchestration with a small PAPER core; retain and repair the execution, ownership, protection, and evidence primitives. Do not attempt a clean broker/ledger rewrite. Use one operational store, one entry owner, one independent guardian, one bounded outcome learner, and a read-only status view. Disable optional research services only after verified handover. Details: [design](../../superpowers/specs/2026-10-13-paper-core-design.md), [authoritative implementation plan and Sol briefs](../../superpowers/plans/2026-10-13-paper-core.md), [executable test plan](test-plan.md).

The user's refinement is incorporated: ordinary PAPER faults use automatic, scoped recovery rather than a global kill-switch reset. Protection and trustworthy outcome processing/learning remain independent. Manual pause and narrowly scoped integrity quarantine retain explicit meanings.

## What was actually examined

| Evidence class | Verified observation | Limit |
|---|---|---|
| Workspace | Clean at start; `codex/elrond-learner`, `9f5ece9e4df7f070947b4b315dbab8cadff8f104` | Older than current main; not used as current source authority |
| GitHub main | `b812c64270f190b0518322abb08b9a17dfa3f1c0`, merge of PR286 | Source, not runtime |
| PR287 | OPEN, unmerged, `8bb84cf9e035771189d700247b99eec281888b94` | [PR](https://github.com/jfhutchi/project_elrond/pull/287) reports 4,348 passing/47 skipped; not independently rerun as a full aggregate |
| PR288 | OPEN, unmerged, `a6f9eb36c4bd632157fff166907c7bd50f70ef15`; unchanged from user's reviewed head | [PR](https://github.com/jfhutchi/project_elrond/pull/288) reports 4,381 passing/47 skipped; no hosted check rollup and no deployed acceptance |
| This review's tests | 140 focused PR288 tests passed in 46.18 s; execution reviewer separately ran 124 main tests and 28 PR288 tests | Overlapping suites; do not sum these as an aggregate. Existing local environment, not fresh locked installation |
| Additional acceptance probes | Three isolated synthetic probes fail against PR288 for named defects below | Deliberately failing proposed acceptance tests, not regressions introduced by this review |
| Host | Read-only Task Scheduler inventory, process executable paths/start times, five clean release checkouts, SQLite read transactions, receipt files, Windows time status/peers | No broker API call, task change, stop clearance, resync, migration, credentials, external messaging, or paid CI |
| Historical trading | Three exploration entry/exit pairs in operational fill records, linked experiment lot/protection/flat/feedback evidence | Recorded broker-PAPER evidence; not a fresh independent broker re-query. Fees and learning break whole-loop acceptance |

Source references below use immutable GitHub commits because files in the user's workspace are older. Local evidence is preserved in [runtime-evidence.json](runtime-evidence.json), [task-inventory.json](task-inventory.json), and the logs/probes linked in the test plan. SQLite stores were read separately in `mode=ro`, `query_only` transactions; these are **not an atomic cross-store snapshot**. Raw credentials and account numbers are omitted from the saved extraction.

## Deployment truth

Nineteen Elrond tasks were observed; two were disabled. Task state is a scheduler observation, not proof of useful progress. Five different active application pins matter to the core:

| Role | Observed release | Evidence and implication |
|---|---|---|
| Exploration / learner | `b496109563656df820c80b993db9bda4e793f13c` | Task/module path, running exploration Python process, clean checkout, current receipt and operational deployment rows agree |
| Independent deadline | `cb5556c6ad3fd2b0282bac3694ba66be63d72801` | Task and running Python executable use this release |
| Legacy daemon / ingestion | `5c1db64f427c45f9ee99f8710568a1b82b59b3ef` | Task, process and last recorded coordinator deployment agree |
| Legacy position protection | `90490a3aabae5c49c2381d97c322cf3ba2b4d045` | Task and running Python executable; report/research also pinned here |
| Console / daily close | `b9e5e9f4fe2e40169a76f7b22a8613f476666415` | Task and console process; learner deployment JSON still names an older console pin |

Other scheduled lanes include news/position research, five-minute observation, trade-learning shadow, alerts, and an Ollama model server. This is not one release with a single accountable health result. Preserve the existing local Windows runtime; the retired Pi was not contacted.

At extraction, the operational ledger had 18 fills, 760 reconciliation records, 593 legacy protection cycles, and nine inherited positions in the latest recorded account snapshot. The learner DB contains copied historical trading rows (12 fills and September reconciliation), so its counts must not be added to the operational ledger. The experiment event store has 3 `ExecutionReceipt`, 3 `ExplorationFeedback`, 2 `FitReceipt`, 1 `ActivationPackage`, and state `CANDIDATE_SHADOW` with `activation_id=null`. These categories represent different things.

The latest evening receipt said `MARKET_CLOSED`, `NORMAL_WAIT`, zero new intents. The latest recorded exploration entry cycle at 19:31:43 UTC was `HALTED / KILL_SWITCH_ENGAGED`, even though its reconciliation was clean. The later kill-switch row read `engaged=0`, reason `stop`, updated 01:17:42 UTC. **This review did not clear it and cannot attribute that transition or certify readiness from that row.** Do not carry forward the earlier stop as current, and do not interpret the evening wait as proof of successful recovery. The last recorded reconciliation (20:05 UTC) and legacy protection (19:45 UTC) are historical by evening, not fresh entry authority.

## Findings ranked by impact

### 1. P1 — protection can promote an unreconciled observation into its own baseline

`PositionProtectionSupervisor` reads the latest account snapshot, compares quantities/entry cost, then saves the newly observed broker snapshot into that same table even when the comparison failed. A subsequent pass reads that new observation as its local baseline. Sources: [protection.py:267](https://github.com/jfhutchi/project_elrond/blob/a6f9eb36c4bd632157fff166907c7bd50f70ef15/src/quantbot/operations/protection.py#L267), [comparison:285](https://github.com/jfhutchi/project_elrond/blob/a6f9eb36c4bd632157fff166907c7bd50f70ef15/src/quantbot/operations/protection.py#L285), [save:448](https://github.com/jfhutchi/project_elrond/blob/a6f9eb36c4bd632157fff166907c7bd50f70ef15/src/quantbot/operations/protection.py#L448).

The isolated two-pass probe seeds owned/local quantity 1 and reports broker quantity 2. First pass: one unprotected position. Second pass: zero unprotected positions and a protective-exit requirement, without any intervening reconciliation proof. The probe has no submitter and makes no real order. This proves a classification/authority defect in source, **not that a wrong live exit occurred**. Separate raw observations from certified inventory; only a reconciliation proof derived from owned fills can advance certified state. Fix this before relying on reused protection in the replacement.

### 2. P1 — missing fee evidence becomes supposedly complete net economics

[`Fill.fee`](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/domain/models.py#L157) defaults to zero; [Alpaca FILL conversion](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/brokers/alpaca.py#L650) supplies zero. [Lot import](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/experiment_lot_reader.py#L255) carries it forward; [outcome resolution](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/experiment_lots.py#L266) rejects `None`, not an unsupported zero. The [scorecard](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration_scorecard.py#L112) can call this verified fees.

All 18 stored operational fills have fee `0`; all three exploration feedback rows say `actual_fees=0`. The cross-boundary probe feeds a provider fill without fee evidence and obtains a `COMPLETE` positive net result. This verifies the concern in code and shows why stored zeros are insufficient evidence. Preserve them as historical values with **legacy unknown provenance**, not retroactively as verified or necessarily wrong amounts.

The inspected actual-fill path subtracts fees once. **The suspected double charging of slippage is not confirmed here.** Retain that property: executed price already incorporates execution-price effects; model additional economics separately.

### 3. P1 — no deployed trade-outcome → parameter-update → later-decision chain

The [exploration policy](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration.py#L24) is explicitly nonpredictive. [Selection](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration.py#L147) is sorted-roster rotation by completed-lot count. Win/loss magnitude does not update predictor parameters. The three recorded feedback receipts explicitly have `predictor_fit=false`.

Real standardized ridge fitting exists in [estimators.py:261](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/estimators.py#L261). But [exploration launch:90](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration_launch.py#L90) rejects configured research sources. The two recorded historical fits predate the three trades. Market-label refitting and nonpredictive sampling coexist; they are not a completed learning loop.

[PR287](https://github.com/jfhutchi/project_elrond/pull/287) adds useful lineage, consideration and switch contracts. Its `ExperienceReview` records `NO_CHANGE` and grants no fitting/activation; exported APIs have no coordinator/launcher call sites. Main explicitly refuses a second active artifact without a switch procedure ([activation:197](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/experiment_activation.py#L197)). Merging PR287 alone would not solve this.

### 4. P1 — completed feedback depends on new-entry preparation

[Complete cohort](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration_launch.py#L249) requires the whole roster; [preparation](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration_launch.py#L348) returns without entry context on missing/stale coverage. Affordability and pending-order recovery also affect context. [Coordinator:309](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/experiment_coordinator.py#L309) refreshes lots independently but journals exploration feedback only with that entry context. Thus refresh is not the same as durable learning consideration.

This is a verified source dependency, not proof that a particular historical outcome was lost. Give outcomes their own cursor and loop, including while entries are paused, a symbol is unavailable, buying power is insufficient, or the market is closed.

### 5. P1 — benign acquisition instability still produces durable global stops

The two-sample account/position bracket is a useful acquisition defense, but changing marked prices across non-atomic endpoints can exhaust acquisition. Main routes this through hard failure. PR288 narrows some timeout/retryable-response failures into entry pauses, but **explicitly excludes unstable acquisition** from that recovery class. See [recovery:311](https://github.com/jfhutchi/project_elrond/blob/a6f9eb36c4bd632157fff166907c7bd50f70ef15/src/quantbot/execution/recovery.py#L311), [cycle:414](https://github.com/jfhutchi/project_elrond/blob/a6f9eb36c4bd632157fff166907c7bd50f70ef15/src/quantbot/operations/cycle.py#L414), [snapshot contract](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/docs/stable-snapshot-reconciliation.md).

An isolated sequence changing account marks by cents returned `HALTED`, not a recoverable entry wait. Recorded October 13 cycles also show clean reconciliations followed by stop refusals; they do not alone prove every earlier stop was harmless. Replace failure classification, not reconciliation tolerances. Unknown acquisition never proves ledger agreement; real cash/quantity/ownership contradictions still latch.

### 6. P1 — PR288's relative-clock bound is useful; its Windows predicate is not a demonstrated uptime contract

PR288 retains `STRICT_V0` as default. `RELATIVE_V1` requires two advancing broker samples, a full request bracket within ±2 seconds, a five-second acquisition budget, 120-second proof lifetime, wall/monotonic continuity, and Windows health. The [health predicate](https://github.com/jfhutchi/project_elrond/blob/a6f9eb36c4bd632157fff166907c7bd50f70ef15/src/quantbot/operations/paper_clock.py#L133) additionally requires exact source `time.windows.com,0x9`, poll ≤1,024 s, zero sync error and age ≤min(2×poll, 2,048 s). Entry and deadline wiring exist in source; deployment was not observed.

At 01:34 UTC, read-only host status reported last good sync age ~2,124 s, `Last Sync Error: 2`, reported poll 1,024 s, and peer next-poll remaining ~30,643 s. Later saved evidence shows the age increasing. That sample fails PR288 regardless of a good broker-relative sample. It does **not** establish actual UTC accuracy or justify a resync. Microsoft documents that the special-interval flag can govern polling separately from the displayed ordinary poll interval ([Windows time settings](https://learn.microsoft.com/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings?tabs=config)). The displayed poll value alone cannot validate the assumed health-refresh cadence.

Preserve ±2 seconds. The design proposes a separately reviewed, decision-specific clock contract; do not silently change PR288 or Windows configuration. Keep current refusals until its replacement passes the stated tests and review.

### 7. P2 — software changes disconnect useful experience; deployed feedback also lacks revision repair

[Policy content identity](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration.py#L29) includes code hash; [execution hash composition](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration_paper.py#L62) includes feedback/launcher and pooled components. Feedback selection uses exact policy IDs. A test explicitly expects no predecessor feedback after a code-only change ([revision test:108](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/tests/unit/learner/test_exploration_feedback_revisions.py#L108)). The current commissioned policy differs from the policies on all three stored feedback rows.

Pinned `b496109` uses one feedback receipt per lot and can invalidate that receipt after later source changes without recording a valid successor. Main's [revision-aware feedback](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/learner/exploration_feedback.py#L130) repairs this, but is not the observed pin. Keep strict release provenance while separating economic/feature/exit semantics from implementation identity. Never grant new execution authority by relabeling history.

### 8. P2 — operational complexity obscures the blocker and constrains cadence

Main contains 59 learner Python files (~19,439 lines), 61 research files (~27,117), 33 operations files (~9,983), and 41 storage files (~9,812), excluding top-level modules. This is a measured source footprint, not proof every line is bad. Nineteen tasks, multiple ledgers and stale metadata add real operating ambiguity. A task can be Running while the last entry attempt is HALTED and a receipt says MARKET_CLOSED. Show entry/protection/outcome/learning separately and retain the causal first refusal.

The actual commissioned limits are one new position, 50 bps (0.5%) of equity, 25 bps maximum entry risk, 100 bps stop, 30-minute horizon. A 390-minute session supports only roughly 13 non-overlapping full-horizon episodes before buffers and delays. A few dozen trades is a software capacity test, not a present-strategy expectation. Do not shorten the horizon, expand concurrency, loosen gates, or manufacture entries to meet a count.

### 9. P2 — a received position mark is recorded as though its price timestamp were current

[Protection evaluation:286](https://github.com/jfhutchi/project_elrond/blob/b812c64270f190b0518322abb08b9a17dfa3f1c0/src/quantbot/operations/protection.py#L286) supplies loop time as both `priced_at` and `observed_at`; persistence writes that value as `price_as_of`. The freshness gate therefore sees zero age without a provider mark timestamp. This pattern exists in the inspected deployed protection source as well. Closed-market handling helps but does not prove in-session mark freshness. Record provider as-of separately from receipt; require evidence for a price stop while preserving independently triggered deadline exits.

## End-to-end stage trace

| Stage | Actual implementation and dependencies | Failure / evidence / disposition |
|---|---|---|
| Market data | Alpaca market-data adapter; five-minute source and calendar, existing feed entitlement, current symbol plus SPY | Main source and recorded observations exist. No entitlement purchase. Isolate per-symbol missing coverage; keep genuine benchmark dependencies |
| Features | `learner/five_minute.py`, ten frozen causal features, receipt times and 30-minute target | Keep mathematics and as-of tests. Enforce availability, not merely source timestamp. New outcome target receives new semantic ID |
| Decision | `exploration.py`, `exploration_launch.py`, baseline diagnostics from two historical artifacts | Deployed route is nonpredictive rotation. Keep it only as explicitly commissioned collection during cold start; replace it as the claim of learning |
| Eligibility | `paper_runner.py`, risk/veto, exact policy/commission, clock, calendar, cash, writer/account ownership, prior episode and deadline heartbeat | Preserve mandatory controls; remove unrelated-symbol and optional-research dependencies. Stage is source-tested and has three historical executions, not reliable current readiness |
| PAPER execution | `execution/coordinator.py`, durable intents/client IDs, pending/unknown recovery; experiment outbox | Keep idempotence and uncertainty semantics. Collapse duplicate orchestration behind one owner; never retry ambiguous POST with a new ID |
| Protection / exit | `paper_deadlines.py` durable pre-send handoff, independent deadline worker; legacy `operations/protection.py` | Keep independence and per-lot exit owner. Repair observational-baseline promotion. Historical protected exits exist; Running alone is insufficient |
| Reconciliation | `snapshot_capture.py`, `recovery.py`, `reconciliation.py`, fills/orders/positions and expected ledger | Keep strict economic/ownership comparison. Replace acquisition classification; independently certify baseline. Snapshot freshness is not provider atomicity |
| Round trip | `experiment_lot_reader.py`, `experiment_lots.py` links fill/terminal/protection/flat evidence | Retain evidence requirements; remove entry-context dependence. Three historical exploration pairs, no verified fee provenance |
| Costed outcome | Fill fee field, lot P&L, exploration feedback/scorecard | Replace provenance model and truthful status. Preserve existing no-double-slippage behavior |
| Learning | Descriptive feedback; separate `experiment_research.py`, `experiment_adaptation.py`, `estimators.py` | Real fitting exists on market labels, but not a demonstrated outcome-driven deployed update. Retain estimator and holdout/budget primitives; replace orchestration and target contract |
| Activation / later use | Exact artifact approval on main; PR287 switch APIs; source decisions carry artifact provenance | Missing integrated switch plus later-use evidence. No successful whole-loop claim. Add atomic activation receipt and first-later-decision witness |
| Deployment / status | Pinned Windows wrappers, `uv.lock`, startup tasks, several JSON receipts and dashboards | Keep local account/job cleanup/lockfile. Replace multiple launch configurations and competing summaries with one manifest and scoped health |

Runtime version attribution for that trace: market/feature/exploration preparation, decision, entry, lot import and feedback are launched from `b496109`; its nested execution/recovery code uses that same release. Independent deadline/exit code is launched from `cb5556c`, while legacy protection is `90490a3` and the legacy daemon/reconciliation is `5c1db64`. Thus several versions can write observations to the same operational DB; a table's newest row does not identify every writer. Historical fit artifacts have their own stored code/input hashes; their exact producing release was not re-established here. There is no demonstrated activated successor to assign a deployed learning version to. Clean Git directories do not establish installed dependency parity; that remains a release acceptance check.

## An attributable historical example—and the broken links

The saved extraction's example lot is `aaed112a…010d6` (XLI, October 9). Observation `75368470…625cdd` links to entry intent `learner-entry-79f37b6d…dbf60c`, client order `qb-79f37b6d041601048c441ef756dfd10c019c596f`. Operational fills show 0.014766 shares bought at 169.404 at 15:16:36.201113 UTC and sold at 169.116 at 15:45:08.190470 UTC. The lot journal records protection binding, exit order/fill, terminal evidence and reconciled flat at 15:45:20.489623 UTC; feedback arrived at 15:45:32.473745 UTC.

The fill-price gross P&L is **−$0.004252608**. The receipt calls this net using zero fees and says `predictor_fit=false`. This demonstrates that parts of the real PAPER loop have executed. It does **not** establish trustworthy after-cost outcome, learned parameters, or a later decision using an updated artifact. Two XLF round trips are also present. Three small historical observations cannot establish profitability or reliable daily throughput.

## KEEP / REPLACE / DISABLE inventory

“Disable” means remove from the future runtime after reviewed handover; preserve source, historical evidence, and currently owned-position protection. No disable action occurred here.

| Area / paths | Decision | Dependencies and reason |
|---|---|---|
| `brokers/base.py`, `brokers/alpaca.py`, transports | KEEP + repair fee parsing | PAPER endpoint/account pin, typed error classes, client-ID lookup. No new broker framework |
| `execution/coordinator.py`, order lifecycle, recovery IDs | KEEP | Durable claim before send, no ambiguous resend; use through one execution boundary |
| `execution/snapshot_capture.py`, reconciliation arithmetic | KEEP capture; REPLACE classification/certification | Same account, fills and ledger; do not widen money tolerances to hide non-atomic marks |
| `operations/protection.py`, `learner/paper_deadlines.py` | KEEP behavior/independence; REPLACE baseline authority and duplicate exit ownership | Owner, fill ledger and verified clock; existing positions retain owner through tested migration |
| `risk/*`, kill-switch semantics | KEEP applicable controls; REPLACE overloaded state handling | Entry limits, cash, concentration, drawdown and manual stop. No research permission to clear a stop |
| `market_data/*`, `learner/five_minute.py` | KEEP narrow equity/ETF lane | Existing Alpaca feed, calendar and SPY dependencies; irrelevant symbol data must not stop another symbol |
| `storage/database.py`, migrations, IDs, Decimal encoding | KEEP; add explicit contracts | SQLite outside OneDrive, one schema authority, explicit migrations. Preserve historical DBs |
| Experiment event store / second outbox | REPLACE in new path | Import source evidence by stable ID into operational store; do not dual-write business state across DBs |
| `experiment_lots.py`, lot reader, revision feedback | KEEP rules; REPLACE trigger and fee contract | Terminal fills, owner, protection, flat proof, per-revision provenance; independent cursor |
| `estimators.py`, canonical holdout and trial ledger | KEEP | Deterministic small ridge, prior search burden, protected ranges; no data-lab reset |
| `experiment_*` coordinator/research/adaptation/activation, PR287 | REPLACE orchestration; reuse selected validation contracts | One update journal and atomic activation path. Do not carry the entire experiment framework into core |
| Nonpredictive exploration | KEEP temporary, labeled collection mode | Explicit commission and existing tighter limits; cannot count as predictor improvement |
| News/thesis, position research, model debate, discovery, synthesis, codegen, anomaly routing | DISABLE from core | Optional workers currently add sources/bindings/cadence; not needed for thin outcome learning |
| Kronos, LLM server, sandbox, remote workers, node placement | DISABLE unless separately commissioned | No required core dependency or inference model; preserve research artifacts, do not contact retired hosts |
| Multiple intraday/daily/five-minute shadow learners | DISABLE redundant scheduled lanes after import | Choose one five-minute core. Historical and shadow evidence remain separate |
| WSL/systemd/Pi deployment, PostgreSQL/distributed alternatives | DISABLE from supported core | Native Omen + SQLite is sufficient for stated capacity; no platform migration |
| Console/control and reports | KEEP useful read views; REPLACE summary/control routing | One first blocker per scope, no broker submission from UI; explicit audited pause/resume separate from display |
| Alert transports, daily close/report jobs | OPTIONAL | Read-only consumers; inability to notify cannot block protection/outcomes. Do not configure external messaging in this work |
| Backtests/research results/`REFUTED.md` | KEEP archival evidence | Do not relabel as forward trades or erase failed hypotheses |

## Remaining uncertainty

Fresh broker account/order/activity truth was not independently fetched; recorded evidence is not enough to certify present ownership or fee completeness. Service-token access to clock information was not tested; this session's read permissions are not proof of the runtime account's. No current natural eligible round trip was induced. No absolute UTC bound was measured. No source change, model activation, schema migration, scheduled task change, historical deletion, holdings reset, or security change occurred. These are explicit later acceptance requirements, not reasons to invent success or weaken gates.
