# PAPER core implementation and handover

> **Runtime handover, October 15 at 08:13 Eastern:** both stores are now schema 36; upgraded dashboard, inherited protection and deadline services are running. The bounded deployment task waits for the original IWM exit before commissioning guardian/outcomes and enabling trader after fresh protection checks. Trading is not active at this capture. See the [measured runtime handover](runtime-handover-2026-10-15/README.md).

**Earlier installation capture at 06:23 Eastern, superseded by the runtime handover above.** [PR 290](https://github.com/jfhutchi/project_elrond/pull/290) merged as `14352cc428dcd999750138d52fbd5f30347991a1`. The [current local installation record](local-deployment-2026-10-15/README.md) contains release `0734974b39088b449abe1279a385c89202756b61f3420aa910d6b2d772a46d4c`, config `620d31ec42915eee76779a972521428adaeb9c0a5536694422d8c27e2b6acebd`, exact installed pins and the actual limited-task verifier pass at 10:23 UTC. The new core tasks are disabled/uncommissioned; the operating store remains schema 29. A fresh 10:15 UTC broker GET confirms the original IWM exit remains accepted and unfilled. Legacy task definitions and inherited ownership remain unchanged.

The preceding baseline source was d56ff4dcfcdb55bd060aa8898d65c1ff97df89c0 on codex/paper-core-m0. Security update a3d5880f02f9d57104e3e833474a4648d1ec181e locked patched Mako 1.4.3; its application and launcher code were unchanged. The historical records below remain evidence of that earlier package.

The measured baseline [HP Omen deployment package](omen-deployment/README.md) is prepared release `ec15cbd8dc28a1f90adcb03b26669552eb94b7fba4fa6e208c7cf5d342baf645`, config `aa6c93bccdf4f28a5c1cfda1d94efd1ace09bc1c7e5fd10da3e6298cb68679e2`, schema 36. Preflight corrected the account-digest convention for the same verified account; all application and dependency pins are unchanged from the [Mako security fix](mako-security/README.md). Its affected local gate passes 169 tests with 26 PostgreSQL-only skips. Those pins and checks belong to the older XLE package, not the approved hybrid/V3 successor. Preserve its frozen bytes; a successor needs its own manifest/configuration and verification.

The baseline exact-release verifier passed under the existing limited task identity after user-authorized CurrentUser RemoteSigned. The `2026-10-15T02:00:02.7976062Z` task capture recorded replacement tasks disabled. Copy-only operational and learner rehearsals migrated to schema 36 while retaining original data values. The broker GET-only capture at `2026-10-15T01:44:15.825306Z` recorded an accepted, unfilled IWM SELL of 0.169669 shares while the market was closed. These are dated observations requiring refresh before handover. No operating migration or commission has been performed by this work.

The replacement has three independent roles: trader, guardian, and outcomes with daily learning. Ordinary provider or clock interruptions cause scoped waits and recovery after fresh evidence. Manual stops and genuine ownership/accounting failures retain their explicit controls.

Learning starts considering updates after ten completed exchange sessions, including quiet sessions. Each later day considers all eligible retained history. The latest ten sessions receive raw weight 2 and older sessions weight 1, normalized before fitting. Past-only validation and protected-data exclusions remain. Insufficient or unreliable evidence produces a recorded NO_CHANGE. Old history is retained; corrected outcomes are excluded while their durable reprocessing is pending.

Independent reviews closed all four identified findings: bound repeated historical polling, preserve economic/model identity across compatible executable maintenance, provide audited entry resume, and commit commissioning plus initial activation atomically. Late fee corrections now remain pending until their evidence is covered, including restart and legacy recovery. The integration owner accepts the isolated implementation and preparation; operational acceptance remains separate.

## Implemented successor: source and operational acceptance are separate

The [daily hybrid specification](../../superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
supersedes baseline single-XLE/no-quota selection. The feature is implemented and locally tested,
including a ten-session mixed-outcome corpus, actual V3 fit/activation and later positive-signal
and negative-prediction exploratory entries. [PR 290](https://github.com/jfhutchi/project_elrond/pull/290)
contains the implementation; it is merged to main in PR #290 and locally installed, but not commissioned.
The [successor evidence package](daily-exploration/README.md) holds exact test/review results,
candidate-preparation status, pins and limitations. The baseline verifier does not cover this
successor, and offline evidence does not establish operating acceptance.

`SIGNAL_OR_EXPLORE_V1` prefers any valid activated signal above 10 bps; otherwise it selects
labeled exploration with the actual zero/negative score. The configured stock universe and
current/prior-session reservation counts determine rotation under the spec's exact ranking.
Six durable reservations per verified session coexist with one concurrent core lot. Completed
and unknown-send cases count; only definitive pre-send proof releases a reservation. Cohort
selection replays at claim/send, and guardian marks/health follow selected or owned symbols.

V3 targets `owned-executed-paper-gross-return-v1`, admitting established gross outcomes from
closed, owned, reconciled lots only when `FEE_UNAVAILABLE` is the sole gap. Fees remain unknown,
`COSTS_PENDING` remains and net stays null. V2 remains strict net. V3 retains overlapping
feature windows within training/history while preserving past-only folds, label availability,
purge/embargo, protected exclusions, ten-session warmup, daily all-history consideration and
normalized latest-ten/older raw weights 2/1. Failed/rejected fits retain the incumbent while
exploration continues. Initial successor commission starts from COLLECT and must authorize
`DAILY_BOUNDED_V3`; no older net model or commission is silently reinterpreted.

The current-funds contract uses fresh stable PAPER account/position/open-order evidence bound
to quantity proof, current ledger and exact commissioned identity to authorize bounded sizing.
It leaves `expected_cash=None`, `cash_complete=False`, unknown fees and null net when historical
cash is incomplete. It cannot invent cash activity/reconciliation or clear discrepancies,
quarantine, unknown sends or manual stops. Capture stability, 120-second freshness and repeated
claim/gate/locked-send/recovery checks remain required. Legacy model-only mode keeps exact cash.

This is an economic selection/learning/funds change. The integrated verification and distinct
successor preparation are recorded in the package above; reverify its exact pins before fresh initial commission. An
already commissioned store cannot simply reopen with changed config identity. Existing IWM
order IDs, inherited owners, protection/deadline responsibilities and measured handover blockers
remain intact; this documentation grants no operational authority.

## Evidence and its limits

The original review, milestone plan and independent milestone reviews remain linked below. Synthetic integration demonstrates attributable entry, independent protection/exit, reconciliation and truthful cost status. Separate synthetic learning evidence changes a later decision from WAIT to BUY. Recorded real market data supplies a separate simulation with honest warmup and cost limitations. These are distinct evidence sets, not a naturally completed broker-PAPER lifecycle.

The final repair's first broad gate reported 449 passed, 3 failed and 26 PostgreSQL skips. Its three failures remain archived. The repaired frozen source passed the affected gate: 296 tests passed, 26 PostgreSQL skips, in 283.28 seconds, plus six successful local lint/type checks. A subsequent minimal exception guard passed its final six-file gate: 105 tests in 166.52 seconds, with six current lint/type checks. That preceding source and its evidence remain preserved. These phases are not summed into a full-repository-green claim. Its existing synthetic 50-trade capacity witness completed guardian plus durable outcome processing in 0.500 seconds against a ten-second bound; real-provider latency and quota capacity remain unmeasured.

The residual correction has its own preserved 28-test gate (51.12 seconds) and final nine-test gate (16.31 seconds), with six successful lint/type checks at each freeze. The final gate covers late-cutoff corrections, restart, missing legacy metadata, already-acknowledged work, out-of-order new notifications, archived-corpus eligibility, idle history, pending-learning exclusion and concurrent notification preservation. No heavy fit, capacity or actual-role witness was repeated for this narrow repair.

The final missing-work correction passed ten focused tests in 17.25 seconds and six lint/type checks. It explicitly separates actual new-claim initialization from recovery; the added control proves an older notification cannot hide retained corrections when work metadata is absent. Claim replay cannot use new-claim initialization. These results remain separate from the earlier stages.

## Pre-security release, preserved as historical evidence

- Source commit: d56ff4dcfcdb55bd060aa8898d65c1ff97df89c0.
- Prepared release: 2377ee8f1c8f00baa7c441e8eea56c257df201d86f704fa89ab802222ff35fc7.
- Config: 8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c; schema 36.
- Final source-pins manifest: e86de9cac1e76098abb37eae5a5b74229b89955e4ce113bc1ef5a85706fccd4c, 419 files.
- [Artifact index](missing-work-recovery/artifact-index.json): 29c972bef23120353e9b778c44c1fcfde358db6267476c3cdbb68063fbad0b1e.

For the preceding consolidated repair, the owner independently checked all 180 artifact files against working and committed bytes, 41 staged logs against original and compressed hashes, 16 final and two historical source-marker exports, 11 role/provider log archives, all 418 working/archive source pins, six code-check logs, typed release/config identities, static task XMLs, and the final typed process chain. All 283 prior M6 artifact records remain unchanged. That preceding process witness closes five synthetic shares with two fills; actual fees and net remain unknown (COSTS_PENDING). It does not claim a fee-complete broker-PAPER trade.

For the preceding residual package, the owner verified all 84 artifact files against working and committed bytes, 16 original/compressed logs, seven final and six historical marker exports, all 419 working/archive source pins, six code checks, typed release/config identities and four static task proposals. All 463 prior M6/consolidated-repair records remain unchanged. Exact-cutoff tests demonstrate fees corrected from zero to 0.20 and exclusion from learning until reconciliation. The archived corpus remains independently eligible under its original contracts.

For the final package, the owner verified 44 working/committed artifact files, six raw/compressed/original logs, eight final marker exports, all 419 working/archive source pins, six code checks, typed release/config and four static task proposals. All 547 prior artifact records remain unchanged. The final evidence covers the previously missing-work-arrival case and selects only the corrected outcome after reconciliation.

## Ordered operational review for the successor

1. The installed successor's exact verifier passed under the existing leepa Interactive/Limited task identity, with no operating database, environment file, broker or functional role startup. All four replacement tasks now point to the installed release and are disabled/trigger-free; functional roles retain submission/entry clamps. The verifier-only task was disabled after its run. Functional process-tree ownership and role readiness still need verification after commission; selected-token file verification is not functional startup.
2. Refresh writer/task/ownership evidence and WAL-aware backups with a restore into a separate path. Review the exact old-entry-writer fence and additive schema 36 migration. Keep the existing Protection/Deadline services and nine original-owner positions. Preserve original client IDs and resolve uncertain sends; never restore an older database over later activity.
3. Acquire fresh account/position/cash evidence against the migrated store and exact reviewed release. Review the commission document within its 120-second freshness window; baseline capture must remain within five seconds of its original commission timestamp. Reacquire when stale. A combined acquire-and-commit operation requires explicit bounded approval; timestamps are never edited to simulate freshness.
4. After approved commissioning, start submission-disabled guardian and outcomes before trader. Verify independent processes, fresh readiness and useful progress. Removing submission/entry clamps and scheduling are separately named changes. Explicit entry resume never clears another manual stop or quarantine.
5. Observe real signal/exploratory broker-PAPER entries, protected exits, owned-flat reconciliation, truthful gross/cost/net outcomes and later daily learning receipt/model attribution. Prove successive entry-funds continuity without independent cash activity or invented fees. A fee-only gap may permit V3 gross learning but never a verified-net claim. Record genuine blockers and retry; do not fabricate fills, liquidate inherited holdings for capacity or claim forced parameter improvements. The old package's no-forced-entry and fee-complete learning expectations remain baseline evidence, not the successor selection/target rule.

The candidate learning origin, October 15, 2026 at 13:30 UTC, remains provisional; verify the actual handover/session contract and regenerate the candidate if it changes. Preserve truthful cost metadata and unknown fees: V3 gross labels do not establish verified net returns. Current retained-owner provenance, source authority and recent-order provider latency/quota capacity remain operational prerequisites. Selected-token verification now covers the exact installed successor; the earlier worktree candidate's direct verification remains a separate historical result.

The measured prepared configuration remains single-symbol XLE with one concurrent core position. It does not implement the approved successor contract above. Preserve its release evidence and dates; publish successor pins, review results and runtime measurements only when actually established. The staged XLE release must not be presented as satisfying the daily hybrid/V3/current-funds requirement.

## Review package

- [Original source-backed audit](../../reviews/2026-10-13-paper-redesign/audit.md)
- [Design](../../superpowers/specs/2026-10-13-paper-core-design.md)
- [Approved daily hybrid, V3 and current-funds successor](../../superpowers/specs/2026-10-14-daily-paper-exploration-design.md)
- [Reviewed successor evidence, prepared pins and remaining release work](daily-exploration/README.md)
- [Authoritative implementation plan](../../superpowers/plans/2026-10-13-paper-core.md)
- [Whole-branch findings](final-review.md)
- [Current HP Omen deployment package, exact status and blocker](omen-deployment/README.md)
- [Mako security fix and regression evidence](mako-security/README.md)
- [Pre-security prepared release and final implementation regression evidence](missing-work-recovery/README.md)
- [Preceding residual correction and evidence](residual-cutoff/README.md)
- [Consolidated repair runbook and recovery semantics](final-repair/README.md); use the current omen-deployment release paths for future actions.
- [Implementation report and staged test results](final-repair/final-fix-report.md)
- [Consolidated repair scoped review](final-repair-review.md): R2–R4 addressed.
- [Residual cutoff review](residual-cutoff-review.md): normal and existing-work paths addressed; original residual preserved in this historical report.
- [Final scoped acceptance](missing-work-review.md): missing-work-arrival correction accepted; all identified R1–R4 findings closed.
- [Committed artifact inventory for the accepted implementation](missing-work-recovery/artifact-index.json)
- [Owner decisions and preserved chronological rulings](decisions.md)

PR #289 was merged into main at 2026-10-15 01:50:03 UTC. The user separately authorized HP Omen deployment and persistent script enablement. Subsequent staging performed the GET-only PAPER account/order check, copied-database rehearsal and disabled-task registration documented in the current Omen package. No operating-store migration, commission, replacement trading role, forced trade or LIVE enablement has occurred. The existing console remains the legacy deployment. The latest measured blocker and staged state take precedence over the earlier implementation-only status in historical bundles.
