# Daily PAPER experience successor

This package records the successor to the merged three-role baseline in [PR 289](https://github.com/jfhutchi/project_elrond/pull/289). Work is on [`codex/daily-paper-exploration`](https://github.com/jfhutchi/project_elrond/tree/codex/daily-paper-exploration). The baseline XLE preparation remains sealed in [the preceding deployment bundle](../omen-deployment/README.md). Its config, release and selected-task-token verification do not authorize this economic policy change.

## Status

All four source tasks and the independent whole-branch review are approved, with no open source findings, at `36d61811e15a350da401eb15e6ac8980e7aea4f1`. The combined ten-session collection/fit/activation witness and its 180-test affected gate passed on the preceding source snapshot `8bae89e164b8831eba86ec0dde4f7f7abb7aa1ea`. Integration review's partial-first-BUY finding was corrected and approved after 91 covering tests passed, with one unchanged heavy fitting case deselected. The distinct candidate passed offline prepare/verify with no objections. It is not merged to main, deployed or commissioned, and this is not a natural broker-acceptance record. Publication is the remaining delivery step at this preparation snapshot.

## Behavior

At each eligible five-minute boundary, `SIGNAL_OR_EXPLORE_V1` first looks across all eligible stocks for an activated prediction strictly above 10 bps. A qualifying signal wins over exploration. Within the qualifying pool, selection orders by current-session reservation count, descending prediction, prior-session count, then configured universe order. If none qualifies, exploration orders by current-session count, prior-session count and universe order, preserving the real zero or negative prediction. Missing or invalid model authority is not permission to explore.

The prepared universe is AAPL, MSFT, AMZN, GOOGL, META, NVDA, JPM, JNJ, XOM and PG; SPY supplies the benchmark. Broker asset eligibility and usable data are acquired for the actual cohort. Selection and later replay retain unavailable-member reasons, so an unavailable stock does not hide an eligible stronger signal or automatically block the whole cohort.

Six durable reservations per verified exchange session and one concurrent core lot bound collection. Reservations survive restarts and changes of acquisition, symbol, model or release. Completed, rejected-after-send and uncertain sends count; only definitive evidence that no send occurred releases a reservation. Existing position, cash reserve, buying-power reserve, stop and holding-period limits remain. The system seeks daily PAPER experience, but cannot guarantee a broker fill when the market is closed, data or account authority is unavailable, an order is uncertain, or another entry constraint applies. Protection and outcome processing continue after the entry cap.

`daily-paper-exploration-walk-forward-v3` learns `owned-executed-paper-gross-return-v1` from actual reconciled closed owned lots. A fee-only `COSTS_PENDING/FEE_UNAVAILABLE` outcome can participate when gross P&L is established. Its actual fees and net P&L remain unknown, and its cost status is unchanged. Positive and negative outcomes both influence training. Gross prediction is before fees and is not evidence of net profitability.

After ten completed sessions, daily consideration retains all eligible history, with raw weight 2 for the latest ten sessions and 1 for older sessions, normalized for fitting. It preserves minimum episode/session counts, past-only validation, label availability, a 30-minute train/validation embargo, protected-data and correction exclusions, one charged procedure per session, validation gates and all-history final refitting. A rejected or failed fit retains the incumbent; it does not itself disable eligible exploration. Shared feature windows within training are allowed for V3. Legacy V2/net contracts and serialized artifacts retain their prior behavior.

The separate `DAILY_BOUNDED_V3` commission binds the exact procedure, gross target and existing holding/exit policy. Initial commissioning starts from target-neutral COLLECT. An old net predictive model or V2 commission cannot silently become gross authority.

## Current funds versus exact cash accounting

The approved `BROKER_PAPER_BALANCE_V1` contract addresses a production gap: ordinary Alpaca fill responses do not provide the independent net-cash movements used by some synthetic learning fixtures. Unknown actual fees therefore leave exact ledger cash incomplete after a fill.

For the hybrid PAPER mode, the new contract uses a persisted, stable, bounded acquisition of current broker account funds, positions and open orders, bound to the exact account, store, config and commission. It authorizes bounded current sizing while quantity and source integrity remain established. It does not invent a cash movement or fee, fill `expected_cash`, mark `cash_complete`, resolve a disputed-cash incident, create a false full-cash reconciliation or grant LIVE authority. Legacy selection retains its strict exact-cash rule. The implemented path and decisive no-cash-bridge witness have passed task review.

The API distinction is documented in Alpaca's [account activities](https://docs.alpaca.markets/us/docs/account-activities) and [account fields](https://docs.alpaca.markets/us/docs/account-plans). Neither source supplies a basis for replacing unknown fill fees with zero.

The combined fixture respects the actual activity query window and publication timing. It exposed three integration blockers: whole-second query serialization could omit a fractional-second exit fill; a delayed owned fill could be misclassified as an ownership contradiction; and valid warmup `NO_CHANGE` acknowledgements could make historical COLLECT decisions fail predictive-model checks. Their reviewed repairs preserve exact timestamps, permit only evidence-bound acquisition waits without granting a certificate, and validate compatible past-available COLLECT acknowledgements while retaining predictive provenance checks. Unexplained inventory remains an integrity failure.

The pending-fill rule also covers the first BUY: opening-balance arithmetic can still report `cash_complete=True` before that known owned fill publishes. It supplies no current certificate, funds permission or new-entry authority. The actual fill must arrive before normal certification/recovery; no flag, cash amount or fee is rewritten, and an existing genuine cash-integrity incident stays active.

## Verification and review

Reports and raw logs are preserved separately from claims about operating broker state:

| Scope | Retained evidence | Result |
| --- | --- | --- |
| Entry/cohort/reservations and ranking repair | [Task 1 report](verification/task-1-report.md), [initial review](review/task-1-review.md), [repair review](review/task-1-fix1-review.md) | Final focused gate: 25 passed; prior compatibility subset: 7 passed. Ranking review finding fixed. |
| Gross target, chronology, all-history learning and activation | [Task 2 report](verification/task-2-report.md), [review](review/task-2-review.md) | Final affected gate: 108 passed, 2 deliberately deselected. Additional gross/daily gate: 38 passed; operational contract controls: 2 passed. |
| Current-funds continuity | [Task 3 report](verification/task-3-report.md), [review](review/task-3-review.md) | Final-source focused gate: 90 passed, plus the added receipt-age control. Actual offline adapter/coordinator witness executes six entries/exits, starting AAPL then MSFT, with zero cash activities and unchanged unknown fees/net. Approved with one cleanup carried into integration. |
| Combined hybrid collection, learning and later signal/exploration | [Task 4 report](verification/task-4-report.md), [combined witness](verification/task4-witness.json), [integrated gate](verification/task4-logs/bsjgrc12.txt), [partial-fill fix gate](verification/task4-logs/fix1-urfrn2_a.txt) | Integrated source snapshot: 180 passed. Later partial-fill correction: 91 passed, 1 unchanged heavy fitting case deselected. The actual bounded fit/activation case passed: 60 training outcomes across ten sessions, then AAPL SIGNAL +19.93355481727601 bps and MSFT EXPLORATION −19.933554817275482 bps. Total 62 closed outcomes, 124 provider-shaped orders, zero cash activities, and unknown fees/net throughout. |
| Whole-branch review | [Final review](review/final-review.md), [integration repair review](review/task-4-fix1-review.md), [rulings and risks](review/design-rulings.md) | Source approved; no open source findings. Operational readiness remains unapproved. |
| Candidate preparation | [Config](prepared-config.json), [manifest](prepared-release.json), [verification](prepared-verification.json), [provenance](preparation-provenance.json) | PREPARED with no objections; no operational commission or broker probe. |

Passing results above are separate runs with overlapping tests; do not add their counts as unique coverage. Tests use temporary stores and explicitly synthetic provider inputs through a credential-stripping, external-network-blocking harness. No operating database, broker submission or paid CI is part of this evidence. The Task 2 corpus supplies independent synthetic cash activity solely to isolate learning; it is not evidence of production daily continuation. Reports retain development failures and limitations rather than relabeling every run as passing. Source typing and scoped Ruff passed for Tasks 1 and 2; the Task 1 attempt to type-check integration fixtures is separately recorded as failing and is not represented as a passing gate.

Task 4's earlier broader gate recorded 328 passed and one failure in an existing process test's short initial seed window, before child processes launched. Its exact unchanged focused rerun passed. That broader run predates the wholly delayed first-BUY amendment; the subsequent 180-test gate covers that amendment and full combined witness. No production timing guard or inherited process assertion was weakened. Five-source mypy and scoped Ruff also passed at that snapshot. PostgreSQL and natural platform deployment were not tested.

The later partial-first-BUY review finding is retained in the [initial integration review](review/task-4-review.md). Its fix reads the prior certified proof only as historical position-basis evidence, without requiring or restoring current cash authority. A two-of-five published fill stays pending with the previous certificate revoked; only genuine remaining activity enables normal certification and recovery. The final 91-test gate covers this correction and authority/refusal controls; scoped Ruff and guardian-only mypy also passed. The unchanged heavy learning case was not rerun after that correction, and the 180-test witness is not presented as a run on the later commit.

The combined learning case uses an explicit independent-role heartbeat fixture; it does not establish separate OS processes or contain its own restart/cap scenario. Those controls have separate evidence: Task 1's durable selection/reservation tests, the restart and send-authority cases in the [affected gate](verification/task4-logs/bsjgrc12.txt), and the [unchanged three-role process rerun](verification/task4-logs/hzoihh6t.txt). They are not relabeled as properties observed inside the combined learning case.

Absolute `.superpowers/sdd/` and temporary paths in the archived reports identify their original local evidence. The corresponding reports, review decisions and task log directories are copied here for durable review; the local scratch directory is not a tracked repository dependency.

## Prepared candidate

The real CLI `prepare` and `verify` commands both returned exit 0. Verification at `2026-10-15T05:05:54.009964Z` reported `state=PREPARED`, `objections=[]`, `operational_commission=NOT_PERFORMED` and `broker_state=NOT_PROBED`.

| Identity | Exact value |
| --- | --- |
| Reviewed executable source commit | `36d61811e15a350da401eb15e6ac8980e7aea4f1` |
| Candidate config | `8ecad4bb682852ce1e25faf10970cd3271da46bd3a7203261a4d5d9f90385801` |
| Candidate release | `091369467a5d9744b20e3704e1eab4e2b624e65b13aff26f90d1bda2e1d36be9` |
| Required schema | 36; no operating migration performed |
| Pins | 366 executable files, 46 dependency distributions, plus recorded interpreter/runtime identity |

The candidate retains the existing store/account digest and endpoint binding, starts from target-neutral COLLECT, selects `DAILY_BOUNDED_V3` and has no invented cost contract. Its configured `submission_enabled=true` is an uncommissioned candidate value, not evidence of running or enabled tasks. The exact repository/import path and commands are in provenance. No env file or operating database was opened; no broker socket, scheduler, runtime role, migration, commission or CI was used. All 18 predecessor artifacts remain unchanged. Direct verification does not establish successor selected-task-token verification, which remains unperformed.

## Operating handover still required

The last deployment capture, `2026-10-15T02:00:02.7976062Z`, recorded the old core roles disabled and uncommissioned. The last broker GET-only capture, `2026-10-15T01:44:15.825306Z`, recorded the original IWM SELL 0.169669 still accepted with zero filled. These are dated observations, not fresh runtime checks. Original client ID: `qb-595f587acd1b769f4bd0d59282e631ad14ecb9f7`; broker order ID: `28523181-cd04-4f74-bd67-bcebe4582164`. This work does not cancel, replace or resubmit it.

Before commissioning, acquire fresh evidence of that order, inherited ownership and open orders; make a compatible protection/deadline transition; then take a fresh fenced backup and perform the reviewed schema transition. The preceding operating-store observation was schema 29, whereas the core requires schema 36. A prepared manifest would not establish that the operating store was migrated. Existing legacy protection must not be restarted blindly against the new schema.

The provisional learning origin in the preceding candidate, `2026-10-15T13:30:00Z`, does not prove baseline collection began. Set the intended origin from the verified actual handover/exchange-session contract and regenerate the candidate if it changes. No completed ten-session baseline is claimed here. A source-only release transition cannot authorize the new selection and learning economics. If fresh evidence finds an existing core commission, use an explicitly supported configuration transition; do not reset or reopen the store under a new identity.

The baseline selected-task-token verifier result remains baseline evidence. The direct successor prepare/verify result above establishes only its exact source/environment/config pins, not broker readiness, a successful successor task-token run or permission to begin entries.

The complete behavior contract is in the [approved design](../../../superpowers/specs/2026-10-14-daily-paper-exploration-design.md), with the [implementation plan](../../../superpowers/plans/2026-10-14-daily-paper-exploration.md). See [handover](../handover.md) for the earlier acceptance record and inherited-order boundary.
