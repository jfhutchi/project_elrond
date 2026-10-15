# Executable verification and acceptance plan

October 13, 2026. Companion to the [audit](audit.md), [design](../../superpowers/specs/2026-10-13-paper-core-design.md) and [authoritative milestone plan](../../superpowers/plans/2026-10-13-paper-core.md).

There are three distinct levels: runnable current-source diagnostic probes; future integrated replacement tests specified below; and genuine broker-PAPER acceptance after reviewed rollout. A pass at one level does not satisfy another. At the October13 audit, the replacement was not implemented and the future test paths did not yet exist. Implementation and reviewed results now appear in the [authoritative milestone plan](../../superpowers/plans/2026-10-13-paper-core.md) and [milestone evidence](../../evidence/paper-core/); the dated diagnostic results below remain historical.

## Runnable now: three acceptance gaps

[Probe source](probes/test_acceptance_gaps.py) calls actual adapter, outcome resolver, protection supervisor, capture/recovery/cycle code against fake brokers and temporary SQLite databases. [Runner](probes/run_probes.py) uses an allowlisted subprocess environment, isolated paths and socket guards; the Windows asyncio local self-pipe is allowed. It imports no credentials and invokes no production service or CLI. Run with an existing Python environment containing the repository's pytest/dependencies; this command installs nothing.

Exact reviewed PR288 source snapshot exists locally at `C:/Users/leepa/AppData/Local/Temp/elrond-review-20261013/pr288`. It was extracted with `git archive` from `a6f9eb36c4bd632157fff166907c7bd50f70ef15`. On another machine, supply an isolated checkout/archive at that commit. Do not point tests at a live runtime directory or use its venv as a deployment upgrade target.

```powershell
& 'C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond/.venv/Scripts/python.exe' `
  'C:/Users/leepa/OneDrive/Documents/GitHub/project_elrond/docs/reviews/2026-10-13-paper-redesign/probes/run_probes.py' `
  --source 'C:/Users/leepa/AppData/Local/Temp/elrond-review-20261013/pr288'
```

Expected current result: **3 failures / exit 1**, all on the named business assertions:

1. Missing provider fee must remain unknown; current adapter/resolver returns finalized net +1.90.
2. A second unchanged mismatched position observation must remain unreconciled; current supervisor reports no unprotected position and requires an exit without reconciliation evidence.
3. Non-atomic moving-mark acquisition must pause/retry without a global stop; current PR288 returns HALTED.

Use `--main-only --source 'C:/Users/leepa/AppData/Local/Temp/elrond-review-20261013/main'` for the first two on main `b812c64`; the third uses a PR288-only cycle fixture. Expected main result: 2 failures, 1 deselected. These are deliberately failing acceptance probes, not xfails and not changes to product source. Logs remain in a newly allocated OS temporary directory printed by the runner. They are not part of the normal test collection.

## Tests actually run during this review

| Source / coverage | Result | Evidence |
|---|---|---|
| PR288 clocks, relative entry/deadline paths, acquisition faults, feedback revisions, lot outcomes, coordinator and restart recovery | 140 passed / 46.18 s | [log](focused-regression.txt) |
| Main ambiguity, stable snapshots, paper deadlines, lot ownership/outcomes, accounting, protection supervisor | 124 passed / 38.46 s | [log](execution-regression-main.txt) |
| PR288 transient acquisition and clock acquisition additions | 28 passed / 12.26 s | [log](execution-regression-pr288.txt) |
| Three new synthetic PR288 acceptance probes | 3 failures, expected on audited implementation | [log](acceptance-gaps-pr288.txt), portable runner above |

These sets overlap. Do not add them into a new aggregate count. The PR descriptions' 4,381/47 and 4,348/47 totals remain reported prior aggregate evidence, not this review's rerun. No paid/hosted CI ran. Tests used the existing Python 3.11.16 environment, not a fresh frozen release installation. Network-free tests cannot certify broker idempotency, real fees, service-account permissions, or continuous deployment health.

To reproduce the 140-test selection in an isolated credential-free environment, from the PR288 source root:

```powershell
python -m pytest -q `
  tests/unit/operations/test_paper_clock.py `
  tests/unit/operations/test_paper_clock_acquisition.py `
  tests/unit/learner/test_relative_clock_paths.py `
  tests/fault/test_paper_transient_acquisition.py `
  tests/fault/test_stable_snapshot_recovery.py `
  tests/unit/learner/test_exploration_feedback_revisions.py `
  tests/unit/learner/test_experiment_lots.py `
  tests/integration/test_execution_coordinator.py `
  tests/fault/test_restart_recovery.py
```

Use temporary DB/lock/report paths and no inherited broker credentials for that environment. Do not run the full repository live-provider opt-in suite against ambient settings. At each later milestone run the shared local CI contract in the approved isolated checkout with frozen dependencies, plus the integrated cases below. Native launcher tests must honor existing script policy; do not “fix” a refused fixture by changing policy or requesting another identity.

## Future executable test matrix

Each row specifies a named assertion and owner. Implement it in the exact test files in that milestone's brief; then run that brief's `python -m pytest ...` command. Use parameterized cuts/scenarios rather than tests that mirror internal methods. Every integration run exports the actual event chain to `docs/evidence/paper-core/mN/` with source/fixture/seed/config digests. Current acceptance status and reviewed evidence are recorded in the authoritative implementation plan; a matrix row alone is not a passing claim.

| ID / milestone | Stimulus | Required observable result |
|---|---|---|
| I01 M0 | Same fills in operational and copied learner stores | One trade attribution; copied history identified, no summing counts |
| I02 M0 | Stale deployment JSON disagrees with task/process/release | Actual observed identity wins; mismatch shown; no fabricated current readiness |
| E01 M1 | Eligible observation, ordinary fake fill/exit | Joined observation→decision→intent→handoff→fill→exit→terminal→flat chain, lane SYNTHETIC_TEST |
| E02 M1 | Duplicate observation and simultaneous claims | One intent identity and one fake POST; conflicting request digest refuses |
| E03 M1 | Pause inserted after decision/before send | Final pre-send check rejects; no queued replay after pause |
| P01 M1 | Qty 1 certified, broker qty 2, repeat and restart | Still unowned/unreconciled; no observation-based ownership adoption |
| P02 M1/M2 | Stop and deadline race, partial entry/exit | Single exit reservation; sold quantity ≤owned uncommitted quantity; every partial fill protected |
| R01 M2 | Crash before/after claim, POST, ack, fill, outcome commit | Durable recovery by same client ID; each fill/outcome counted once; no stale-entry replay |
| R02 M2 | POST timeout, temporary lookup 404, later accepted | Unknown exposure remains reserved; lookup-only recovery; no new POST/ID |
| R03 M2 | Cancel ack followed by late partial fill | Filled residual imported/protected; no oversell or premature flat outcome |
| R04 M2 | Duplicate event vs same ID/different payload; paginated activity overlap | Identical duplicate inert; conflict quarantined; pagination complete before certification |
| R05 M2 | Expired RISK_APPROVED/ORDER_CREATED claim, delayed original sender, fresh observation | Atomic precise unsent refusal releases the slot; fresh observation can POST once, original sender cannot; SUBMITTING/unknown remains reserved despite 404 |
| C01 M2 | Missing fee, explicit reported zero, applicable contractual zero, modeled estimate | Four distinct provenance paths; only proven actual zero finalizes broker net |
| C02 M2 | Entry 100, exit 102, fee 0.10 each; reference 99/103 | Actual net 1.80; reference shortfall remains diagnostic; no second slippage charge |
| C03 M2 | Late fee, correction/bust, currency/adjustment gap | Append revision; one current sample, earlier history intact; unresolved amount blocks verified net |
| C04 M2 | Fee revision after cash certification, racing final send; independent complete cash activity | Obsolete fee-derived cash authority revoked transactionally; actual send-state transition sees current proof; still-authoritative independent cash activity permits progress while fee allocation remains pending |
| C05 M2 | Mixed batch with one conflicting old source and an unrelated healthy lot | Whole business batch rolls back and rejected envelope persists; only implicated source/lot authority quarantined; healthy outcome remains processable |
| O01 M2 | Entry paused/closed/missing symbol/no buying power/learning crashed | Certified completed outcomes continue within healthy worker cadence; zero new entries |
| D01 M3 | One roster symbol missing; candidate and SPY fresh | Other candidate evaluated; SPY-dependent missing data blocks only dependent decisions |
| T01 M3 | Full bracket at ±2.000 s, outside at ±2.001 s | Inclusive approved bound; outside pauses affected entry, never global stop |
| T02 M3 | 5.001 s acquisition, 120.001 s proof, repeated samples, wrong provider | Specific refusal; old proof not reused; fresh proof required for automatic recovery |
| T03 M3 | Wall jump forward/backward; monotonic steady; restart | Invalidate affected proof, no negative/extended hold; reacquire after restart |
| T04 M3 | Open/close/expiry uncertainty overlap, DST, half-day session | Conservative entry wait; earliest-safe due exit; actual calendar, no assumed 390-minute day |
| T05 M3 | Broker mark old/unknown as-of but received now | No fabricated current mark or price-stop authority; independent deadline path remains |
| T06 M3 | Healthy nonzero account/position acquisition latency after time proof | Current validated evaluation accepts the newly certified inventory; immutable acquisition evidence is not refreshed; genuinely expired/discontinuous evidence still refuses |
| T07 M3 | Positive/negative skew, close-buffer-clipped deadline, then clock outage | Monotonic expiry is derived from matching time domains and never extends the conservative provider deadline |
| A01 M3 | 429, timeout, unavailable service, changing snapshot marks, then healthy | Scoped wait/retry, no manual reset; fresh proof + newly evaluated decision resumes |
| A02 M3 | Genuine cash/unowned inventory/conflicting fill/manual pause | Relevant authority quarantined/manual pause retained; no identical-read self-clear; unaffected trustworthy outcomes continue |
| A03 M3 | Retained closed-session health wait plus unresolved dependent source integrity incident | First blocker identifies integrity ahead of ordinary waits; every objection and original evidence remains available |
| L01 M4 | Protected/canonical consumed window canary, future label, late receipt | Refuse before estimator input access; no look-ahead or recycled holdout claims |
| L02 M4 | 29 training episodes, insufficient days, incomplete costs, duplicate consideration | Justified NO_CHANGE with exact IDs; no fit/trial on repeated no-op; prior=result |
| L03 M4 | 30 initial train/10 later scored outcomes over 5+5 sessions, balanced feature-linked ±20 bps | Five expanding past-only folds pass fixed procedure gates; final refit uses all 40 outcomes; fold and final parameters/weights recorded separately, finite clipped prediction, single permanent trial |
| L04 M4 | Fit timeout/failure, late correction, different target/release-only change | Trial retained on attempted fit; corrected outcome reconsidered once; semantic incompatibility excluded; compatible code-only release retains experience |
| L05 M4 | Daily consideration after ten completed exchange sessions; quiet/holiday dates and unchanged evidence | Verified calendar controls cadence and latest-ten recency; truthful NO_CHANGE without a redundant fit/charge; no forced trade or parameter change |
| L06 M4 | Eligible history beyond 60 sessions, recent and older episodes | All eligible history affects final fit; fixed raw weight ratio 2:1 normalized to mean 1, consistently applied to scaler and ridge loss; no rolling history deletion |
| L07 M4 | Later fee revision and a current parent trained after an earlier validation date | Fold training reconstructs only as-of revisions; comparison uses contemporaneous available decision models, never retrospectively trained parent; scoring availability and exact comparator IDs exported |
| L08 M4 | Delayed fit/recovery and future input cutoff beyond a live reservation expiry | Execution completion/availability stays distinct from cutoff; replay remains immutable; protected/forward canaries unread at selection and immediate materialization recheck |
| L09 M4 | Future cutoff/calendar, repeat request, then execution reaches the cutoff | Future request refuses before materialization/fit/charge; exact cutoff is retained; repeat refusal is immutable; later valid request proceeds with actual completed-session weights and preserves refusal history |
| M01 M5 | Approved candidate activation and same later feature/gate state | Active digest actually loaded; abstain under incumbent, eligible buy under candidate in synthetic fixture |
| M02 M5 | Crash immediately before/after activation commit; competing switch/entry claim | One coherent pointer/event; correct restart model; no stale approval or dual claim |
| M03 M5 | Model NO_CHANGE, bad artifact, rollback | Pointer unchanged/no entry on bad binding; rollback keeps later evidence and open-lot model/exit attribution |
| S01 M5 | Real-data local simulator with stale/missing quotes, partials, reject, latency/cost stress | Truthful assumptions, no fabricated observed quotes; lane LOCAL_SIM_REAL_DATA; zero broker network/credential capability |
| S02 M5 | 50 synthetic round trips + fitting load + restart | No duplicates/lost outcomes; protection meets proposed cadence and lock budget; this proves software capacity only |
| D02 M6 | Release/config/schema mismatch, restart under existing account | Refused entry with exact blocker; guardian/outcome compatible recovery; no install-on-start or security mutation |
| D03 M6 | Backup/restore drill and rollback after new fill | Integrity verified in new path; no lost new broker events; old binary used only if schema/owner compatible |
| B01 M6 | Naturally eligible post-deployment broker-PAPER opportunity | Actual joined broker evidence below; no forced trade or synthetic substitution |

## Whole-loop acceptance bundle

The exporter must fail completeness if any required link is missing or inconsistent. Include immutable IDs and raw-evidence digests, not narrative assertions:

1. Actual release/config/schema/account/store and lane; causal market observation and available-at proof.
2. Decision with exact policy/model/activation, first/all gate results and fresh eligibility proof.
3. Durable intent and protection handoff preceding possible POST; client ID and broker order linkage.
4. Entry execution IDs, quantities/prices/times; partial-fill conservation and unambiguous lot owner.
5. Guardian progress/ownership throughout holding interval, stop/deadline evidence, one exit reservation and actual exit executions.
6. Terminal orders, complete activity watermark and fresh owned-lot flat reconciliation. No pending unknown send/cancel remains.
7. Cost components/provenance and exact arithmetic; gross, fee-proven broker net and modeled economics separate. Unknown required costs mean acceptance still incomplete.
8. Learning receipt referencing this outcome revision: prior/result model, exclusions, validation or justified NO_CHANGE, attempt charge, activation reference/time or null.
9. A later actual decision proving the resulting active artifact was loaded. It may legitimately abstain. A new fitted model is not required for a small natural sample; synthetic M01 separately proves a changed decision is possible.

For B01, independently compare exported order/fill/activity IDs to fresh read-only broker responses under existing access. Do not infer current broker truth from a DB, task state or receipt alone. No new provider account or reset. If fee documentation/activity, fresh clock, natural eligibility or ownership proof is unavailable, status stays WAITING with a concrete reason. The acceptance plan contains no order-forcing command.

## Measuring reliability and improvement

Track evaluated opportunities, truly eligible opportunities, refused entries by first blocker, eligible intents submitted, unknown submissions, protected owned quantity, exit lateness, outcome backlog/age, fee completeness, learning consideration coverage and actual model-use witnesses. Denominators and excluded/closed-session periods must be explicit; raw trade count is not the success metric.

Evaluate economics and prediction performance by version/session with sample sizes and dependence-aware uncertainty. Seen-validation improvement is not unseen profitability. The final protected evaluation stays separate from training/selection; prospective comparisons must be registered before exposure. Zero naturally eligible trades or NO_CHANGE can be correct behavior. A simulated capacity run and green unit suite cannot close the deployed acceptance milestone.
