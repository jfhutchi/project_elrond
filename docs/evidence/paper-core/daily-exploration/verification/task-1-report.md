# Task 1 implementation report

Status: DONE. Base: `6af19ce0d6ff3bf75a25ae7f80c20f34a5643e52`. Branch: `codex/daily-paper-exploration`. Commit: `69e8edd` (`Add signal-first PAPER exploration and durable session reservations`). All examples, fills and provider transports used here are explicitly synthetic/offline.

## Final accepted behavior and interfaces

- `CoreConfig.symbols=()` and `selection_policy=MODEL_ONLY_V1` are optional, omitted when absent, and leave legacy hashes/replay intact. `entry_symbols` resolves the configured tuple or `(symbol,)`. Explicit universes are unique uppercase supported equity names, exclude SPY, and are bounded to 32. Multi-member selection requires `SIGNAL_OR_EXPLORE_V1`. The prepared ten-stock universe remains Task 3's explicit configuration responsibility.
- New immutable `ObservationCohort` captures one completed five-minute source boundary, verified session bounds, available observations and explicit unavailable-member reasons. New optional `Decision.selection: SelectionEvidence` retains the cohort, boundary ID, signal/exploration/unavailable provenance, durable daily/prior counts, and independently evaluated exclusions. Absent Decision fields remain omitted.
- `selection.evaluate_cohort` evaluates every observed member with the original inventory, activation and time projection. It searches the entire eligible universe for a valid activated prediction strictly above 10 bps. Within qualifying signals, today's unused members come first, then durable total usage, descending prediction and configured order. A used qualifying stock beats an unused weak stock. Only when none qualify does unused/least-used exploration apply. Negative predictions and COLLECT's absent prediction remain truthful. Missing activation is an authority wait, including COLLECT.
- `selection.record_cohort` persists the complete captured cohort in existing `core_provider_acquisitions` and its member observations atomically. Claims verify this exact immutable document before replay; a forged unavailable reason cannot hide a captured member. All-unavailable cohorts are retained, then the runtime records a truthful `NO_ELIGIBLE_COHORT_MEMBERS` blocker through its heartbeat; there is no invented observation or fill.
- `selection.reservation_state` derives counts and semantic boundaries from existing decisions/ENTRY links/intents. Historical queries hydrate scalar selection metadata rather than old feature cohorts. They are owner scoped with an upper session bound; durable prior rotation necessarily examines earlier scalar reservation history. ERROR reservations are released only through the existing exact `definitively_unsent` proof. Completed, rejected, generic-error and unknown reservations otherwise remain consumed.
- `selection.boundary_id` binds account, owner, verified session open/close and completed source end, independent of selected symbol, receipt time, model and release. Under `BEGIN IMMEDIATE`, claim rejects duplicate semantic boundaries and a seventh possibly-sent reservation (`SIGNAL_OR_EXPLORE_SESSION_LIMIT=6`), then retains the existing global open-lot and unresolved-send checks. Final send rechecks membership and cap while excluding its own reservation from the preceding count. Legacy mode has no newly imposed quota.
- `trader_cycle` accepts `Observation | ObservationCohort`; legacy callers keep their original API/behavior. The hybrid path records cohort evidence, acquires current time/inventory, recovers health for each actually fresh member, selects once and claims by full replay. Selection time requires the cohort's opening boundary to match provider time evidence, in addition to existing closing/source-end checks.
- `AlpacaCoreBroker.observation_cohort()` requests one bounded bars cohort for the universe plus SPY, builds each member independently, and retains missing/invalid/restricted member reasons. Read-only asset responses must establish matching `us_equity`, active, tradable and fractionable status. Individual eligibility failures do not suppress healthy members. `observation()` remains the single-symbol legacy entry point.
- Trader roles acquire the hybrid cohort when configured. `roles.owned_open_symbols` derives mark-refresh symbols from actually owned filled active lots, including symbols outside current candidates. Scoped entry health, recovery, resume readiness and cost-contract coverage use the actual universe/selected member. Resume requires at least one healthy universe member; claim/final send still check the selected member exactly. `daily_consideration` changes only to pass the existing configured economic `policy_id`; target/procedure work is Task 2.

## Tests and exact commands

All pytest commands below ran in the isolated worktree through `.venv/Scripts/python.exe -B .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py`, whose subprocess strips credentials, uses temporary stores/profile, and blocks external socket connects. Original logs remain at `C:/Users/leepa/AppData/Local/Temp/paper-core-check-<name>/result.txt`. Copies are retained in `task1-logs/<name>.txt` beside this report. No full-repository or unbounded suite was run.

### Red/green record

| Exact arguments after `check.py` | Result | Original log suffix |
| --- | --- | --- |
| `tests/integration/paper_core/test_daily_selection.py` | RED: 5 failed, 4 passed; missing config/cohort interfaces | `y94l8tx1` |
| same | Intermediate: 1 failed, 8 passed; new restart fixture lacked refreshed guardian readiness | `xns2ehbr` |
| `tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_thin_loop.py tests/fault/paper_core/test_final_send_authority.py` | GREEN: 42 passed | `30_luyx9` |
| `tests/integration/paper_core/test_daily_selection.py` | Intermediate: 1 failed, 13 passed; new repeated-trade fixture replayed old fee revisions/marks | `t6e_icu3` |
| `tests/integration/paper_core/test_daily_cohort_adapter.py` | RED: missing cohort acquisition method | `4j2oki5d` |
| `tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py tests/integration/paper_core/test_operational_adapter.py` | Intermediate: 1 failed, 23 passed; same new repeated-trade fixture issue | `4qxd_yhq` |
| `tests/integration/paper_core/test_daily_selection.py -k signal_beats` | RED after final user ordering clarification: used signal incorrectly lost to exploration | `wgjek9zn` |
| `tests/integration/paper_core/test_daily_cohort_adapter.py` | RED: hybrid role still used legacy acquisition; cost contract covered only default | `xnko6lsd` |
| `tests/integration/paper_core/test_daily_selection.py` | Intermediate: 1 failed, 13 passed; fixed repeated-stock entry marks in the new fixture | `9z1humjv` |
| `tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py` | GREEN: 17 passed | `_z_6dqgi` |
| `tests/integration/paper_core/test_daily_selection.py -k 'substituting or signal_beats'` | RED: substituted missing reason accepted; repeat signal ignored usage rank | `ipzc0snz` |
| `tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py` | GREEN: 18 passed | `j8zwgj4g` |
| `tests/integration/paper_core/test_daily_selection.py -k collect_without` | RED: uncommissioned COLLECT could explore | `vwsydc7h` |
| `tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py` | GREEN: 20 passed, including completed/unknown sixth reservation controls | `s3n2q9bw` |
| `tests/integration/paper_core/test_daily_selection.py` | GREEN after scalar history-query refactor: 17 passed | `s3p6lcam` |
| `tests/integration/paper_core/test_daily_selection.py -k all_unavailable` | RED: all-missing cohort blocker was not retained durably | `wvbhaxul` |
| `tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py tests/integration/paper_core/test_final_repair.py::test_fence_owned_exit_bad_resume_fresh_resume_new_claim_and_stale_send tests/fault/paper_core/test_health_authority.py::test_reconciliation_generation_race_is_retryable_not_integrity` | Final GREEN: 23 passed | `e42_7kmj` |

Final new controls: signal versus weak/negative, used signal versus unused weak, fair signal repeats, COLLECT/negative exploration, uncommissioned activation refusal, missing/held alternatives, forged fallback and substituted cohort refusal, six completed or unknown reservations, definitive-unsent-only release, concurrent different-receipt boundary claims, restart rotation, non-default stop, scoped health recovery, all-unavailable durable evidence, bounded provider cohort/restrictions, role acquisition/owned mark refresh and whole-universe cost coverage.

### Existing affected gates

Exact broad command:

```powershell
.venv/Scripts/python.exe -B .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py tests/integration/paper_core/test_runtime_canonical.py tests/fault/paper_core/test_protection_authority.py tests/fault/paper_core/test_health_authority.py tests/fault/paper_core/test_final_send_authority.py tests/fault/paper_core/test_restart_matrix.py tests/fault/paper_core/test_timing.py tests/integration/paper_core/test_release_recovery.py tests/integration/paper_core/test_operational_adapter.py tests/integration/paper_core/test_learning_changes_decision.py
```

Result: 120 passed, 1 failed in 152.97 s. Log `sf62iu3k`. The failure was `test_reconciliation_generation_race_is_retryable_not_integrity`: its test double did not accept the already-existing `session=` argument from untouched guardian code. `git diff BASE -- guardian.py test_health_authority.py` was empty before correction; `git show BASE:<file>` confirmed the mismatch. Single original failure reproduced under log `o4l2o0p8`. Root authorized repairing the mutable test double. It now accepts/forwards `session` to peer and original certification, preserving every original wait/no-integrity/resume assertion. Corrected single regression passed under `5436yvjr`, and passed again in the final 23-test gate. No frozen fixture/evidence bytes changed.

Additional exact compatibility/race command:

```powershell
.venv/Scripts/python.exe -B .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_costed_loop.py::test_populated_accepted_m1_v30_migration_preserves_all_document_ids_and_payloads tests/fault/paper_core/test_activation_races.py -k 'populated or active_predictor or unresolved_claim or corrupt_pointer or aba_model or final_send'
```

Result: 7 passed, 20 deselected, log `tbwhs7vw`. Covers frozen v30 payload IDs, activated missing features, unknown/open claim retention, corrupt pointers, ABA stale claims and ambiguous final-send authority. Existing populated v34/release/runtime/restart tests were part of the 120 passing broad gates. No unrelated heavy fit or full suite repeated.

### Static verification

Source files: `contracts.py selection.py trader.py roles.py alpaca_adapter.py health.py authority.py send_guard.py release.py`, each under `src/quantbot/paper_core/`.

- `.venv/Scripts/ruff.exe check <the nine source paths> tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py tests/fault/paper_core/test_health_authority.py` — all checks passed. Exact expanded PowerShell invocation and output retained by tool transcript; output saved `task1-lint.txt`.
- `.venv/Scripts/mypy.exe <the nine source paths> --follow-imports=silent --cache-dir=.superpowers/sdd/2026-10-14-daily-paper-exploration/mypy-task1-cache` — success, no issues in nine source files; output saved `task1-mypy.txt`. An initial Windows `--cache-dir=NUL` invocation hit a mypy internal error; using an ordinary ignored cache path resolved that tooling issue. Intermediate type errors were repaired before the final clean invocation.
- `git diff --check` — exit 0; only Git's configured LF-to-CRLF informational warnings.

## Self-review, constraints and concerns

No remaining implementation finding. Historical rotation reads owner-scoped scalar history and may grow with history length; it does not hydrate old cohorts or require a new schema. A wholly unavailable cohort deliberately yields durable acquisition evidence plus a role blocker instead of a fabricated Decision anchored to nonexistent market data. Six reservations are a bound, never a promise of six fills. Broker restriction assertions and executable lifecycle tests are synthetic and do not commission operational authority.

Root's updated tracked spec/plan wording is included in the task commit. No credential/environment files, operating D: stores/tasks/services, broker/network, CI, push or merge were accessed. No subagents were spawned. No migrations or frozen historical fixtures were changed. Independent review, Task 2 learning v3, Task 3 evidence/preparation and all operational actions remain with root.

## Review fix round 1

FIX_BASE: `69e8edd508adb7b1fe74e7218dc535b5800f29ee`. Fix commit: `b3ada4fa5ea88825f6ac1cdb006e77dddf4e025e`. The independent review found: **Signal ranking lets historical exploration usage override stronger signals and current-session fairness.** The earlier implementation description above is superseded by this exact final order:

- Signals: `(current-session count ascending, prediction descending, prior-session count ascending, configured order)`.
- Exploration: `(current-session count ascending, prior-session count ascending, configured order)`.

Both comparator keys now keep current and prior counts separate. Signal-before-exploration priority remains unchanged. Root's clarified tracked spec/plan updates are included in this fix commit.

The mixed-signal test no longer monkeypatches `learning.predict_bps` or supplies an invalid feature/model combination. Its test input helper calls the real production `fit_estimator` on four deterministic full-feature rows, constructs a valid immutable ridge ModelArtifact, and executes the real production prediction path. The pure evaluator receives explicit immutable activation evidence bound to this artifact; this test does not assert that the artifact was commissioned in a durable store. Existing claim/activation integration controls remain unchanged.

New parameterized regressions cover unequal historical counts for unused qualifiers, unequal current-session counts, stronger signals within an equal current-session tier, equal predictions with unequal prior counts, and equal prediction/count ties resolved by configured order. An exploration regression covers current-session counts taking precedence over long historical usage. A new executed two-session control completes two AAPL-only entries, restarts the store, then completes the next day's actual synthetic sequence `MSFT,NVDA,AAPL,MSFT,NVDA,AAPL`, verifying empty current-session counts and retained prior `AAPL=2` at the new-day boundary. Provider fee evidence applies only to each pair of actual synthetic fills. No internal prediction/fit/claim/outcome function is replaced.

Exact commands and retained results (all in the same isolated worktree):

```powershell
.venv/Scripts/python.exe -B .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_selection.py -k 'signal_rank or exploration_today or signal_beats'
```

RED before the comparator change: 4 failed, 3 passed, 17 deselected. Three signal cases chose MSFT incorrectly and the exploration case chose MSFT over the less-used-today AAPL. Log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-e3i5rdlj/result.txt`, copy `task1-logs/e3i5rdlj.txt`.

```powershell
.venv/Scripts/python.exe -B .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_selection.py -k cross_day
```

RED before the comparator change: 1 failed, 24 deselected. The eighth executed entry was incorrectly MSFT rather than AAPL. Log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-qgtuswba/result.txt`, copy `task1-logs/qgtuswba.txt`.

```powershell
.venv/Scripts/python.exe -B .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_selection.py
```

GREEN after the change: **25 passed in 17.70 s**. Log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-z5gx0l50/result.txt`, copy `task1-logs/z5gx0l50.txt`.

```powershell
.venv/Scripts/ruff.exe check src/quantbot/paper_core/selection.py tests/integration/paper_core/test_daily_selection.py
.venv/Scripts/mypy.exe src/quantbot/paper_core/selection.py --follow-imports=silent --cache-dir=.superpowers/sdd/2026-10-14-daily-paper-exploration/mypy-task1-cache
git diff --check
```

Ruff: all checks passed (`task1-fix1-lint.txt`). Source mypy: no issues in one source file (`task1-fix1-source-mypy.txt`). Diff whitespace check: exit 0, only Git's informational CRLF warnings.

An attempted broader strict mypy command additionally named `tests/integration/paper_core/test_daily_selection.py` and reported 46 test-fixture untyped-call/optional-value errors, with no selection.py error; exact output is preserved in `task1-fix1-mypy.txt`. Root explicitly ruled integration fixture typing outside this comparator fix and directed the established source-only gate above. No broad runtime/learning suite was repeated. No additional self-review implementation finding remains. No broker/runtime/environment/remote state, subagents, frozen evidence or fixtures were touched.
