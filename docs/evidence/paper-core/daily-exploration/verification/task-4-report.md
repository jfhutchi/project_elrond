# Task 4 integrated source verification report

Status: DONE review fix round 1/5; I1 focused regression and scoped covering gate GREEN, ready for scoped rereview.

Base: `d2d3503baa249ac669290a5bfd3c45d232e980d8`.
Worktree: `C:/Users/leepa/.codex/worktrees/paper-core-m0/project_elrond`.
Branch: `codex/daily-paper-exploration`.
Commit: `8bae89e164b8831eba86ec0dde4f7f7abb7aa1ea` — `fix(paper-core): continue daily learning across provider delays`.

## Scope and interfaces

This implementer owns only the combined integration witness, its external-provider fixture extensions, three integration-reproduced source repairs approved by the controller, and the approved Task3 minor cleanup. Root owns all existing current-document edits, the implementation plan, the durable evidence package, successor preparation and publication. None of those root-owned paths is staged by this implementer. No subagents were dispatched.

`tests/integration/paper_core/test_paper_funds.py` keeps its default behavior. `prepare(tmp_path, *, hybrid=True, provider_factory=FundsProvider)` adds only an optional external HTTP transport factory, and `FundsProvider.fill_price` defaults to the same Decimal 100. POST response, FILL activity and cash changes now consistently use that configurable external execution price. No production adapter/model/selector/coordinator/claim/outcome/active pointer is monkeypatched in the combined witness.

`tests/integration/paper_core/test_daily_paper_loop.py` supplies `DailyProvider`, a provider transport subclass. It returns query-bounded provider calendar, five-minute bars, actual fixture fill prices and paginated date-bounded activities. Its virtual clock is explicitly declared `BROKER_PAPER:OFFLINE_PROVIDER_FIXTURE_TIME_V1`. Calendar jumps overnight/weekends are followed by actual synchronization polls until the existing six-hour source cursor catches up; proof timestamps/generations are never rewritten. The role heartbeat remains Task3's explicit independent-role fixture attestation. It is not evidence that different OS processes ran.

The combined witness initializes a fresh commissioned hybrid store in target-neutral COLLECT; actually sends and independently protects six entries per session over ten exchange sessions; closes all sixty with alternating gross +20/-20 bps, genuine owned-fill joins and unknown fees/net; invokes actual `daily_consideration` after every completed session; retains nine real warmup NO_CHANGE receipts acknowledged by subsequent COLLECT decisions; and performs the real bounded subprocess V3 procedure, operational candidate commissioning and activation on day ten. Day eleven presents AAPL positive/MSFT negative, then an all-negative cohort; the real selector sends AAPL SIGNAL and later MSFT EXPLORATION with the same exact active model/receipt/activation attribution. The printed evidence contains full candidate, receipt, authority, activation, both decisions/cohort features and predictions, all sixty corpus decision/outcome IDs, final source proof/acquisition IDs and nine warmup receipt IDs. All 62 closed outcomes retain null actual fees and net P&L and the cash-activity table remains empty.

## Necessary source repairs and their boundaries

1. `src/quantbot/brokers/alpaca.py`: `_utc_parameter` now uses `datetime.isoformat()` rather than truncating microseconds. Whole-second inputs retain the same wire strings; fractional `after`/`until` boundaries survive for both performance activities and ordinary FILL reads. The correct date-bounded external provider reproduced a SELL activity being excluded from an immediate post-exit query whose `until` had been floored to the previous whole second. The existing broad fixture returned all fills regardless of `until` and had concealed that seam. The official alpaca-py serializer labels this RFC3339 and uses `datetime.isoformat()` too: https://raw.githubusercontent.com/alpacahq/alpaca-py/master/alpaca/common/requests.py (lines 43-49, inspected October 15). Local broker contract regression checks both exact fractional bounds and both adapter consumers.

2. `src/quantbot/paper_core/alpaca_adapter.py` and `guardian.py`: a genuine publication delay can leave stable acknowledged filled quantities ahead of the actual activities feed. The current immutable acquisition now retains only the pending owned orders observed identically in the existing two readbacks. Guardian may classify a failed quantity certificate as account-scoped `OWNED_FILL_ACTIVITY_PENDING` only when the current persisted source digest/account/release/endpoint/capture interval/watermark and exact current/certified baseline match, every signed quantity discrepancy is exactly explained by genuinely linked current owned order acknowledgements, repository fills/order bindings agree, no unknown send exists, and remaining position basis agrees. It does not issue any certificate, funds permission, fill, fee or cash activity, and never clears a prior integrity incident. A baseline-only cash calculation marked complete cannot establish a present contradiction while that exact owned fill is demonstrably missing; its fields remain unchanged on the failed proof. Known cash mismatch with complete current source and no qualifying missing fill still quarantines. The next genuine activity poll permits ordinary certification/outcomes/recovery. Unrelated symbol deltas still produce integrity refusal. The helper reads the exact current acquisition and indexed order bindings, without a historical payload crawl.

3. `src/quantbot/paper_core/learning.py`: the actual day-ten fit failed because a COLLECT decision with a genuine later warmup NO_CHANGE receipt fell through to PREDICT's estimator requirement. The new COLLECT-only branch requires the exact durable receipt mapping and content IDs, unchanged COLLECT model, past completion/availability in the same execution domain, compatible target/procedure/policy/dataset/lane/release, exact historical ACKNOWLEDGE event and exact authority/model/receipt/previous-activation binding. PREDICT validation and its existing negative controls are unchanged. Forged, future, wrong-target and wrong-model controls retain genuine content digests and receipt mappings where applicable; they cannot become eligible historical comparators. Negative controls create additional fault artifacts, never change the active pointer.

4. `trader.py`: removed the Task3 review's identically false certification comparison. The obsolete Optional alias was eliminated and the already narrowed certification tuple is used directly, preserving source-only mypy without introducing a different authority test.

## TDD and development chronology

All pytest commands use the local credential-stripping/socket-blocking harness:
`.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py ...`

Complete original logs are retained under `task4-logs/<run-id>.txt`; their original paths are `C:/Users/leepa/AppData/Local/Temp/paper-core-check-<run-id>/result.txt`. These are the entire logs, not the harness's last-12000-character terminal excerpt.

The fixture RED and production REDs are distinct:

- `ft6kseiu`, integrated file `-s`: **fixture RED**, first actual protected outcome was flat (`0/500`) rather than the intended +20 bps before variable provider execution prices existed. This was not a production defect or production TDD claim.
- `29f1ha4v`, integrated file `-s`: after correct date-bounded provider responses, first later entry WAITed on `INVENTORY_OWNERSHIP_CONTRADICTION` because fractional SELL activity was outside the truncated query. `79wwzdg4`, integrated file `-k fractional`: **precision production RED**, 1 failed in 5.52s at `POST_EXIT_RECONCILIATION_WAIT`. After the one-line serializer repair, `4452tczy`, same command: **GREEN**, 1 passed/1 deselected in 5.61s.
- `32l6eq5o`, integrated file `-k delayed`: **publication-lag production RED after the precision fix**, 1 failed/2 deselected in 5.91s because real delayed owned activity produced persisted `INTEGRITY_FAILURE`. After the source-bound pending check, `teq4pbbf`, integrated file `-k 'fractional or delayed'` plus broker contract file: **GREEN**, 3 passed/27 deselected in 8.95s. The `-k` filtered the contract file too; this is not represented as its full contract gate.
- `91k66sn1`, integrated file `-s`: completed all sixty real outcomes and then failed the combined case at day ten. The actual new receipt reason was `INVALID_HISTORICAL_PREDICTIVE_MODEL`; the active pointer truthfully remained on its last warmup receipt. `o470awmk`, integrated file `-k 'collect_history and None'`: **historical COLLECT production RED**, 1 failed/7 deselected in 6.09s directly at that error. `6fc_umyk` then had the valid COLLECT positive and two semantic refusals passing, but two missing/mapping-error refusals still escaped as StateConflictError; normalized those to the learner's ValueError refusal contract. `6066tcwx`, integrated file `-k 'not daily_hybrid'` plus full broker contract file: all COLLECT and broker contract controls passed; 34 passed/1 negative-fixture expectation failed in 32.19s. The negative was already correctly raising genuine INVENTORY_MISMATCH; the fixture initially expected a return value.
- `rn8asfxk`, integrated file `-s`: **combined witness GREEN** in a file run with 8 passed/1 failed in 100.21s. Actual fitting/commission/activation and both day-eleven decisions passed. The sole failure was the unrelated-delta test expecting WAIT when the old full certificate had correctly been revoked and `enter` raised CASH_AUTHORITY_REVOKED. This is not represented as an all-green file run. Selected scores were +19.93355481727601 and -19.933554817275482 bps; full retained evidence is in this log.
- `254rxti2`, integrated file `-k 'not daily_hybrid'`: **all narrow controls GREEN**, 8 passed/1 deselected in 31.33s, including corrected genuine integrity refusal and valid-content/mapping COLLECT fault controls.
- `5g1xcq21`, integrated file `-k delayed_exit`: **source binding/refusal matrix and next-poll recovery GREEN**, 1 passed/8 deselected in 5.49s. Stale source, wrong account, wrong endpoint and unowned order are refused; original source restoration does not manufacture fills and actual source recovery happens only after a real activity becomes available. The preceding `3ru4ayk_` was a fixture attempt to point a foreign-key-constrained cursor at a nonexistent document; SQLite correctly refused that impossible setup. Removed the impossible fault instead of weakening schema constraints.

Other fixture development failures are fully retained and do not count as production RED evidence: `lfjae05i` scheduled entry before real setup receipts completed; `hzkaagk3` incorrectly expected COLLECT prediction 0 instead of truthful None; `5l8lhkwt` scheduled a later boundary before real HTTP capture time had advanced; `k18banbd` omitted the operational execution-domain declaration; `yylpm01_` jumped overnight without source catchup; `tzp4ng2q` supplied only four polls for a weekend jump; `kmuo_7xc` processed outcomes before the helper's later synchronization had recovered a complete genuine exit activity. The fixture now honors actual external temporal bounds and real poll ordering.

## Final covering verification

The first broad source snapshot froze before run `lyea33w3` began. No source/test edits occurred during that gate. It covers 23 focused files and includes the full affected shared fixture protocol, existing restart/cap controls, current entry/send/protection/recovery/quantity/refusal controls, exact predictive historical acknowledgement controls, all temporal and canonical guards, activation races and additive migration/refusal behavior. No entire project suite was run. The named process seed failure was investigated once; no entire broad gate was repeated for it.

Command:

```text
.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_paper_loop.py tests/integration/paper_core/test_paper_funds.py tests/contract/test_alpaca_paper_broker.py tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py tests/fault/paper_core/test_final_send_authority.py tests/fault/paper_core/test_health_authority.py tests/fault/paper_core/test_transient_recovery.py tests/fault/paper_core/test_protection_authority.py tests/fault/paper_core/test_restart_matrix.py tests/fault/paper_core/test_outcomes_independent.py tests/fault/paper_core/test_fill_collision_scope.py tests/integration/paper_core/test_operational_adapter.py tests/fault/paper_core/test_learning_temporal.py tests/fault/paper_core/test_learning_daily.py tests/fault/paper_core/test_learning_gross.py tests/fault/paper_core/test_learning_boundaries.py tests/fault/paper_core/test_activation_races.py tests/integration/paper_core/test_runtime_canonical.py tests/integration/paper_core/test_migration.py tests/integration/paper_core/test_release_recovery.py tests/integration/paper_core/test_inventory.py tests/integration/paper_core/test_outcome_cutoff_recovery.py -s
```

Result `lyea33w3`: **328 passed/1 failed in 543.09s**. The sole failure was existing `test_release_recovery.py::test_actual_three_role_processes_restart_fence_and_continue_outcomes` at its synthetic seed assertion, before any child process started. The durable decision was BUY with empty gates, but no claim/plan/POST was persisted. Its short fixture gives 1.5 seconds before the 120-second exit buffer; the decision had about 1.4 seconds left. The exact unchanged focused rerun result is recorded below. All new integrated, funds, broker contract, temporal/daily/gross/canonical learning, activation and migration cases passed. This is not represented as an all-green broad gate.

After this source freeze, root identified that the conservative complete-cash guard also rejected a first BUY whose FILL activity had not yet published. Separate ignored protocol probe `v3iro1m6` reproduced this without altering the frozen gate/source: **1 failed in 5.41s**. One owned BUY acknowledgement existed, local fills were empty, computed baseline-only cash was marked complete at 100000 while current cash was 99500 and quantity AAPL5. Synchronization raised INVENTORY_MISMATCH and persisted INVENTORY_CASH_OR_IDENTITY_CONTRADICTION. `ua8nucy4` was the preceding ignored-probe import-path setup error. Root approved refining the same pending predicate only after the frozen gate completed, with no certificate or mutation of cash/fee fields while pending and an affected final-source gate to follow.

The exact unchanged process test rerun `hzoihh6t` passed in 14.01s. No production timing/exit condition or inherited process assertion was changed. The broad failure remains recorded above; it was not erased by this focused passing run.

After both completed, removed only the overbroad cash-complete guard and tightened current acquisition watermark/baseline binding; pending health is explicitly account scoped. The same ignored first-BUY probe command then passed in `y40ni_2e`: **1 passed in 5.15s**. Its full regression was added to the committed integration file, including real next-poll fill recovery, protected close, next eligible entry, and an independent preexisting cash-integrity fixture which must remain active. `faou6y07`, integrated file `-k first_buy`: **2 passed/9 deselected in 9.14s**. The pending proof remains `cash_complete=True`, `expected_cash=100000`, current cash=99500, actual AAPL5, no canonical fills; latest certification does not advance and the central funds validator refuses it. Only actual delayed fills create the subsequent genuine quantity certificate and unknown-fee accounting. Root's prior-integrity control keeps the incident active and later entry WAITs while protection/outcomes proceed.

Final affected-source command, frozen before `bsjgrc12` began:

```text
.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_paper_loop.py tests/integration/paper_core/test_paper_funds.py tests/contract/test_alpaca_paper_broker.py tests/integration/paper_core/test_daily_selection.py tests/integration/paper_core/test_daily_cohort_adapter.py tests/fault/paper_core/test_final_send_authority.py tests/fault/paper_core/test_health_authority.py tests/fault/paper_core/test_transient_recovery.py tests/fault/paper_core/test_protection_authority.py tests/fault/paper_core/test_restart_matrix.py tests/fault/paper_core/test_outcomes_independent.py tests/fault/paper_core/test_fill_collision_scope.py tests/integration/paper_core/test_inventory.py tests/fault/paper_core/test_timing.py -s
```

Final affected-source result `bsjgrc12`: **180 passed in 332.24s**, no warnings or failures. The complete integrated file now contains 11 cases and the full combined fit/activation witness is included. Unchanged temporal/daily/gross/canonical learner, activation and migration cases passed in the broader pre-amendment gate and were not repeated merely by habit; no learning source changed after that gate began. The final source-only mypy and Ruff below were rerun after the first-BUY amendment.

Final combined evidence is extracted without alteration to `task4-witness.json`. Its source database remains `C:/Users/leepa/AppData/Local/Temp/paper-core-check-bsjgrc12/pytest/test_daily_hybrid_collect_fit_0/funds.db` for read-only controller packaging. The pre-amendment witness is explicitly named `task4-witness-pre-first-buy.json` and must not be substituted for the final source witness.

- Active model: `3800155d604f532ffe95967d481eb681d90b2e6e0e4de353a2e4993942cdb6d0`.
- Active learning receipt: `92fdbf65ca7322dbf035b737cf035bdaa8b1b9fb8ac41e370cce8ff8cbe64b9b`.
- Exact activation: `1eeb078963839119478eb55197ba00b87a3442c2a5b2478f806ebc03ca950833`.
- First day-eleven cohort: AAPL `+19.93355481727601` bps, MSFT `-19.933554817275482` bps; actual AAPL SIGNAL.
- Later cohort: both `-19.933554817275482` bps; actual MSFT EXPLORATION.
- Sixty training outcomes from ten sessions, 62 total closed outcomes, 124 provider-shaped orders, zero CashActivity records; all actual fees/net null.

Ruff, retained `task4-lint.txt`:

```text
.venv/Scripts/ruff.exe check src/quantbot/brokers/alpaca.py src/quantbot/paper_core/alpaca_adapter.py src/quantbot/paper_core/guardian.py src/quantbot/paper_core/learning.py src/quantbot/paper_core/trader.py tests/contract/test_alpaca_paper_broker.py tests/integration/paper_core/test_paper_funds.py tests/integration/paper_core/test_daily_paper_loop.py
All checks passed!
```

Source-only typing, retained `task4-mypy.txt`:

```text
.venv/Scripts/mypy.exe src/quantbot/brokers/alpaca.py src/quantbot/paper_core/alpaca_adapter.py src/quantbot/paper_core/guardian.py src/quantbot/paper_core/learning.py src/quantbot/paper_core/trader.py --follow-imports=silent --cache-dir=.superpowers/sdd/2026-10-14-daily-paper-exploration/mypy-task4-cache
Success: no issues found in 5 source files
```

Exact-path diff whitespace check passed. Git's LF-to-CRLF advisory on the shared test file is line-ending configuration, not a Ruff/type/test warning. An earlier typing run found the Optional alias narrowing lost by the minor cleanup and a nullable broker-average-price mapping; both were corrected before the source gates. There is no source/test change after the final clean lint/typing runs or after `bsjgrc12` began. The earlier broad run is expressly not represented as covering the subsequent first-BUY amendment.

## Self-review and limitations

Self-review read the full source diff and shared fixture/adapter contract diffs. All production edits are the four listed seams; no ledger, fee, cost target, risk limit, schema or frozen evidence rewrite was made. No real credentials, envfile, operating database, external broker socket, runtime role/task, paid CI, push or PR operation was used. All database/artifact activity was within temporary test stores or this ignored plan workspace.

Evidence status is OFFLINE_PROVIDER_PROTOCOL_FIXTURE. Provider data and fills are intentionally synthetic, and the clock advances through an explicit offline domain. The witness proves the real components' behavior against that protocol fixture, not natural broker acceptance, current credentials/account readiness, real market performance or profitability. It does not claim OS process independence; existing retained process separation/restart/cap evidence is reused for unchanged controls. PostgreSQL and actual platform deployment were not tested. Root task review, final whole-branch review, durable evidence publication, successor preparation and host/runtime authority remain separate steps.

Exact staged-path inspection and cached diff whitespace gate passed before committing. Commit contains exactly five source paths (`brokers/alpaca.py`, `paper_core/alpaca_adapter.py`, `guardian.py`, `learning.py`, `trader.py`) and three test paths (`test_alpaca_paper_broker.py`, `test_daily_paper_loop.py`, `test_paper_funds.py`). Post-commit status has only root-owned current documents/plan and the root-owned untracked evidence package; no Task4 source/test changes remain. All 28 complete pytest development/final logs, final lint/mypy output, final witness JSON and explicitly labeled pre-first-BUY JSON remain in this ignored workspace for root review/packaging.

## Review fix round 1/5 — I1 partial first-BUY publication

Fix base: `8bae89e164b8831eba86ec0dde4f7f7abb7aa1ea`. Fix commit: `36d61811e15a350da401eb15e6ac8980e7aea4f1` — `fix(paper-core): retain historical basis during partial fill acquisition`.

Verified the independent finding against both the supplied external-provider probe and a new durable regression. With a genuine acknowledged BUY of five units and only the two-unit activity published, ordinary canonical import correctly revokes the prior full cash certificate. The current proof expects two, observes five, and the current stable owned acknowledgement explains the missing three. The pending predicate's `store.latest_proof()` call incorrectly required present cash authority while reading historical position basis, so it raised CASH_AUTHORITY_REVOKED before returning the required retry classification.

The only production change replaces that accessor inside `_pending_owned_activity` with a read of the exact prior certified proof ID/payload in its existing transaction. Missing certificate/document, content digest mismatch and baseline mismatch refuse. The historical proof supplies basis only; all current source, account, release, watermark, baseline, exact all-symbol signed quantity, owned order, unknown-send and basis guards remain. No cash revocation is removed, no certificate or present funds/entry permission is issued while partial, and no cash/fee/net field is changed. No learning/model/activation source changed.

The new external-only `PartialPublicationProvider` splits the real fixture BUY activity into two and three units, withholding only the second until explicitly published. The regression proves pending retry under synchronization and the actual guardian cycle, unchanged latest certificate, persistent old cash revocation, refused entry authority, no integrity failure, and genuine later acquisition of all five units. It then closes through actual protection/outcomes with both entry fill IDs and unknown fees/net, preserves historical revocations, and permits the next eligible entry. No internal production mocks or active-pointer edits were used.

RED command:

```text
.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_paper_loop.py -k partial_first_buy
```

Result `_93ivtig`: **1 failed/11 deselected in 5.40s**, actual `CASH_AUTHORITY_REVOKED_RECONCILIATION_REQUIRED` at the historical-basis accessor instead of `TimeoutError(OWNED_FILL_ACTIVITY_PENDING)`. This is the intended production RED, not a fixture setup failure. GREEN with the identical command after the narrow source change: `hc17kkx8`, **1 passed/11 deselected in 5.80s**. Full logs retained as `task4-logs/fix1-_93ivtig.txt` and `fix1-hc17kkx8.txt`; original harness paths follow the earlier `paper-core-check-<run-id>/result.txt` convention. The independent review's original exact failure remains `task4-logs/review-je2mjqfi.txt`.

Scoped final-source gate (source/test froze before it began):

```text
.venv/Scripts/python.exe .superpowers/sdd/2026-10-14-daily-paper-exploration/check.py tests/integration/paper_core/test_daily_paper_loop.py tests/integration/paper_core/test_paper_funds.py tests/fault/paper_core/test_health_authority.py tests/fault/paper_core/test_protection_authority.py tests/fault/paper_core/test_final_send_authority.py tests/integration/paper_core/test_inventory.py tests/fault/paper_core/test_outcomes_independent.py -k 'not daily_hybrid'
```

Result `urfrn2_a`: **91 passed/1 deselected in 192.64s**, no warnings or failures. Full output is retained as `task4-logs/fix1-urfrn2_a.txt`; original harness output is `C:/Users/leepa/AppData/Local/Temp/paper-core-check-urfrn2_a/result.txt`. The unchanged heavyweight full daily hybrid fitting case is deliberately excluded as requested by the controller. All remaining integrated controls, including partial/wholly delayed BUY, delayed exit, source binding, unrelated quantity and prior-integrity negatives, are included alongside all affected shared funds and health/protection/send/inventory/outcome controls. Existing combined/learning evidence remains explicitly at the original source snapshot; it is not described as rerun on this fix.

Scoped Ruff (retained `task4-fix1-lint.txt`):

```text
.venv/Scripts/ruff.exe check src/quantbot/paper_core/guardian.py tests/integration/paper_core/test_daily_paper_loop.py
All checks passed!
```

Source-only typing (retained `task4-fix1-mypy.txt`):

```text
.venv/Scripts/mypy.exe src/quantbot/paper_core/guardian.py --follow-imports=silent --cache-dir=.superpowers/sdd/2026-10-14-daily-paper-exploration/mypy-task4-fix1-cache
Success: no issues found in 1 source file
```

Self-review found no additional source change needed. Only `src/quantbot/paper_core/guardian.py` and `tests/integration/paper_core/test_daily_paper_loop.py` are owned by this fix; root documents, plan and evidence package remain unstaged. No operating resources, credentials, envfile, external network, runtime tasks, CI or push were used. No subagents were dispatched.

Exact-path and cached whitespace checks passed. Before commit, the index contained exactly the two owned paths; the exact-path commit changed only those files. Post-commit source/test status is clean and the index is empty; root-owned documents/plan and untracked evidence package remain untouched and unstaged. There was no source/test edit after `urfrn2_a` began or after the clean scoped Ruff/mypy runs. All three fix-round pytest logs and both static outputs remain in the ignored workspace for controller review/packaging.
