# Task 2 — daily all-history gross learning and exact activation

Worktree: `C:/Users/leepa/.codex/worktrees/paper-core-m0/project_elrond`; branch `codex/daily-paper-exploration`; base `b3ada4fa5ea88825f6ac1cdb006e77dddf4e025e`.

## Implementation and interfaces

- LearningPolicy and ModelArtifact accept `daily-paper-exploration-walk-forward-v3` / `owned-executed-paper-gross-return-v1`. LearningPolicy rejects mixed procedure/target pairs. Legacy omitted fields, v2 defaults, v2 candidate description, charge details and old hashes retain their earlier serialization.
- daily_consideration derives V3/gross only from SIGNAL_OR_EXPLORE_V1, preserving the store's exact economic policy_id. Legacy selection retains V2/net. No entry selection, reservation, claim or send semantics changed.
- The metadata selector reads only additional `cost_reason` before protection guards. V3 admits COMPLETE outcomes or fee-only COSTS_PENDING/FEE_UNAVAILABLE; every existing ownership, temporal, pending-work, quarantine, correction and protected-data guard remains in place. It retains within-history feature/holding overlap; V2's overlap exclusion is unchanged.
- Pure `_target(episode, target_contract=NET)` retains its old call shape and dispatches actual gross or actual net P&L. Direct gross fitting rejects other incomplete reasons and absent reconciliation/notional. It does not modify outcomes, fees, net, status or cost metadata. Gross artifact descriptions explicitly say before-fee.
- Fit and validation reject mismatched predictive parents. `_historical_comparator(..., target_contract=NET)` accepts the explicit current target, rejects net/gross historical predictors and verifies receipt/acknowledgement target and policy provenance. COLLECT remains target-neutral.
- V3 retains five expanding past-only folds, thirty training examples/five sessions, ten validation examples/five sessions, ten completed-session warmup, one charged procedure per session, fixed ridge, 2x raw latest-ten weights and all-history final refit. Direct V3 fold scoring additionally rejects future validation availability and wrong-session validation rows. Existing thirty-minute training/validation purge remains.
- Permanent V3 trial charge actor_version is the exact gross target and detail_json explicitly carries procedure_version, target_contract and policy_id. V2 charge detail serialization is unchanged.
- RuntimeConfig/RuntimeCommission accept DAILY_BOUNDED_V3; validation binds it to hybrid selection, and V2 to legacy selection. REVIEW_EACH_CANDIDATE remains supported. Operational commissioning copies receipt target/procedure/policy into ActivationAuthority. Activation and active-model reads require exact authority/receipt/model agreement and exact runtime commission identity. An approval reference cannot override an incompatible target or mode.

## Reusable fixture interfaces

`tests.fixtures.core_gross.gross_episodes(path)` returns `(db, store, broker, collect_parent, trips, joins)`: 60 actual synthetic executed/closed outcomes over ten sessions, six each day, alternating positive/negative gross results. Thirty-five-minute decisions produce overlapping feature/holding source windows. It uses legacy synthetic selection, establishing that compatible earlier history is eligible for V3; it is not a hybrid-selection or operational-readiness witness. It emits no fee events; actual_fees/net_pnl stay null and status stays COSTS_PENDING/FEE_UNAVAILABLE. Independent synthetic provider CashActivity supplies exact movements over all observed fills so subsequent synthetic entries retain cash certification.

`tests.fault.paper_core.test_learning_daily.daily_inputs(tmp_path, gross=False)` now accepts gross=True to replay immutable historical compatible outcomes under a target-neutral parent and V3 policy. Defaults remain unchanged.

`tests.integration.paper_core.test_final_repair.commissioned(tmp_path, ..., gross=False)` now accepts gross=True to create a fresh hybrid/V3 temporary commissioned fixture, rather than reopen a legacy store under a changed identity. Real commission, daily consideration and activation paths execute; only the external provider/calendar is synthetic.

`ActivationAuthority.fixture` deliberately retains V2 defaults for old callers. A V3 synthetic authority must explicitly supply gross target and V3 procedure before persistence. Operational authorities obtain these directly from the verified receipt.

## TDD and verification evidence

All pytest commands below ran through the requested credential-stripping, external-socket-blocking harness, using `.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py`. No operating database, environment file, broker, running role, task scheduler, CI or remote mutation was used. Exact full outputs are retained in `task2-logs/`; the original temporary log locations are `C:/Users/leepa/AppData/Local/Temp/paper-core-check-<id>/result.txt`.

RED:

1. `.../check.py tests/fault/paper_core/test_learning_gross.py -x`: 1 failed, V3/gross rejected by the pre-change LearningPolicy literals. Log `36ra25w7.txt`.
2. `.../check.py tests/fault/paper_core/test_learning_gross.py -k day_ten`: after correcting fixture source ingestion/cash setup, 1 failed (2 deselected), the 60 actual outcomes reached the missing V3 policy contract. Log `l4oxgd6m.txt` (51.72s).
3. `.../check.py tests/fault/paper_core/test_learning_gross.py -k runtime`: 3 failed, 2 passed, 3 deselected. V3 envelope unsupported, hybrid/V2 incorrectly accepted. Log `hv8it0v3.txt` (2.27s).
4. `.../check.py tests/fault/paper_core/test_learning_gross.py -k pure`: 2 failed, 1 passed, 8 deselected. Future validation label and wrong-session validation were accepted by the pure procedure; training crossing a fold was already refused. Log `xwbwigxz.txt` (4.70s).

GREEN:

- `.../check.py tests/fault/paper_core/test_learning_gross.py tests/fault/paper_core/test_learning_daily.py`: 38 passed in 93.71s; log `hs30h8a4.txt`. This includes the real subprocess bounded fit over 60 actual unknown-fee outcomes, fold training counts 30/36/42/48/54, 30 validation observations, all 60 final inputs, exact raw weights, unchanged source outcomes, activation mismatch refusal and successful exact activation. It also covers older-history influence/normalized weighting, protected/forward/declared-holdout canaries, future-cutoff refusal and historical model target mismatch.
- `.../check.py tests/fault/paper_core/test_learning_gross.py -k operational`: 2 passed, 11 deselected in 7.64s; log `gffnp37_.txt`. Both actual temporary operational commissions execute daily warmup and exact acknowledgement. An incompatible target is refused even with a candidate approval reference.
- `.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/fault/paper_core/test_learning_gross.py tests/fault/paper_core/test_learning_temporal.py tests/fault/paper_core/test_learning_boundaries.py tests/fault/paper_core/test_activation_races.py tests/integration/paper_core/test_learning_receipts.py tests/integration/paper_core/test_runtime_canonical.py tests/integration/paper_core/test_final_repair.py -k 'not idle_closed_history and not interrupted_initial_model'`: **108 passed, 2 deselected in 254.68s**. Log `o5j1wldt.txt`. This final run includes additional permanent-charge actor/procedure/policy tamper refusals, authority procedure/policy mismatch refusals, and confirms the active pointer stays unchanged until exact activation. Existing V33 legacy hashes/replay, temporal completions, canonical protection, daily warmup, activation races and commission/recovery behavior pass. The two unrelated closed-history performance/interrupted-bootstrap tests were deliberately excluded.

Development fixture/setup failures were not treated as production evidence: `lh7tsi4s`/`751xkpmk` exposed already-processed outcomes then missing synthetic cash certification; `rexwjfua` used the wrong CoreConfig universe field; `97kbkk_3` left the final cash-triggered outcome work undrained; `5u7x7u6p` initially used unequal comparator dimensions for wrong-session input; `ktk96nmf` supplied only one calendar session; `tx1p3u8n` omitted the runtime clock domain normally set by __main__. Each setup was corrected, and its log is retained.

Source-only typing command:
`.venv/Scripts/mypy.exe src/quantbot/paper_core/contracts.py src/quantbot/paper_core/learning.py src/quantbot/paper_core/learning_procedure.py src/quantbot/paper_core/activation.py src/quantbot/paper_core/roles.py src/quantbot/paper_core/release.py src/quantbot/paper_core/authority.py --follow-imports=silent --cache-dir=.superpowers/sdd/2026-10-14-daily-paper-exploration/mypy-task2-cache`
Result: `Success: no issues found in 7 source files`; retained `task2-mypy.txt`.

Scoped Ruff check names those seven production files plus `tests/fault/paper_core/test_learning_gross.py`, `test_learning_daily.py`, `tests/fixtures/core_gross.py`, and `tests/integration/paper_core/test_final_repair.py`. Result: `All checks passed!`; retained `task2-lint.txt`. `git diff --check` passes. .superpowers remains ignored.

## Self-review and downstream concern

Reviewed the diff for V2 serialization/description stability, protected metadata-before-data ordering, target dispatch in every fit/validation path, predictive parent/historical compatibility, exact charge/authority matching and commissioning mode constraints. No change to immutable old evidence, migration schemas or entry-selection/reservation semantics. Tests remain synthetic evidence, never actual broker experience. The existing large learning.py and activation.py modules were changed locally without restructuring.

Concrete downstream operational gap, reported to root and explicitly assigned outside Task2: production currently never constructs CashActivity. alpaca_adapter.py retains `cash_authority=NOT_CERTIFIED` for acquired activities and linked core fills receive no independent cash-movement authority. CoreStore.ledger marks unknown-fee fill cash incomplete unless such evidence is available. Therefore this synthetic 60-outcome fixture does not prove the real unknown-fee loop can continue entering after its first trade. Root is handling a separate bounded current-balance authority design. No fee zero, cash completeness, or net P&L was inferred by Task2.

No whole unrelated suite was run, per the controller's explicit affected-gates/source-only instruction.

## Changed files

Production: contracts.py, learning.py, learning_procedure.py, activation.py, roles.py, release.py, authority.py under src/quantbot/paper_core. Tests: tests/fault/paper_core/test_learning_gross.py (new), test_learning_daily.py (optional V3 replay and parameterized temporal/protection/history controls); tests/fixtures/core_gross.py (new); tests/integration/paper_core/test_final_repair.py (optional fresh V3 commissioning fixture only). Root-owned spec/plan modifications are excluded from this task commit.

Commit: b37e664ffbd85ef8dde749358899454eca3a70e0 — feat(paper-core): add daily gross learning with exact v3 activation. Staged diff whitespace gate passed after removing an extra final blank line in the new fixture. Post-commit status contains only the two root-owned spec/plan modifications; .superpowers reports/logs are ignored.
