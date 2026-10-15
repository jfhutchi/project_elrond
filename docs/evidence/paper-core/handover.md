# PAPER core implementation and handover

**Isolated implementation and preparation ACCEPTED; operational acceptance WAITING.** Implemented source: commit d56ff4dcfcdb55bd060aa8898d65c1ff97df89c0 on codex/paper-core-m0. No deployment has occurred.

The replacement has three independent roles: trader, guardian, and outcomes with daily learning. Ordinary provider or clock interruptions cause scoped waits and recovery after fresh evidence. Manual stops and genuine ownership/accounting failures retain their explicit controls.

Learning starts considering updates after ten completed exchange sessions, including quiet sessions. Each later day considers all eligible retained history. The latest ten sessions receive raw weight 2 and older sessions weight 1, normalized before fitting. Past-only validation and protected-data exclusions remain. Insufficient or unreliable evidence produces a recorded NO_CHANGE. Old history is retained; corrected outcomes are excluded while their durable reprocessing is pending.

Independent reviews closed all four identified findings: bound repeated historical polling, preserve economic/model identity across compatible executable maintenance, provide audited entry resume, and commit commissioning plus initial activation atomically. Late fee corrections now remain pending until their evidence is covered, including restart and legacy recovery. The integration owner accepts the isolated implementation and preparation; operational acceptance remains separate.

## Evidence and its limits

The original review, milestone plan and independent milestone reviews remain linked below. Synthetic integration demonstrates attributable entry, independent protection/exit, reconciliation and truthful cost status. Separate synthetic learning evidence changes a later decision from WAIT to BUY. Recorded real market data supplies a separate simulation with honest warmup and cost limitations. These are distinct evidence sets, not a naturally completed broker-PAPER lifecycle.

The final repair's first broad gate reported 449 passed, 3 failed and 26 PostgreSQL skips. Its three failures remain archived. The repaired frozen source passed the affected gate: 296 tests passed, 26 PostgreSQL skips, in 283.28 seconds, plus six successful local lint/type checks. A subsequent minimal exception guard passed its final six-file gate: 105 tests in 166.52 seconds, with six current lint/type checks. That preceding source and its evidence remain preserved. These phases are not summed into a full-repository-green claim. Its existing synthetic 50-trade capacity witness completed guardian plus durable outcome processing in 0.500 seconds against a ten-second bound; real-provider latency and quota capacity remain unmeasured.

The residual correction has its own preserved 28-test gate (51.12 seconds) and final nine-test gate (16.31 seconds), with six successful lint/type checks at each freeze. The final gate covers late-cutoff corrections, restart, missing legacy metadata, already-acknowledged work, out-of-order new notifications, archived-corpus eligibility, idle history, pending-learning exclusion and concurrent notification preservation. No heavy fit, capacity or actual-role witness was repeated for this narrow repair.

The final missing-work correction passed ten focused tests in 17.25 seconds and six lint/type checks. It explicitly separates actual new-claim initialization from recovery; the added control proves an older notification cannot hide retained corrections when work metadata is absent. Claim replay cannot use new-claim initialization. These results remain separate from the earlier stages.

## Exact local release

- Source commit: d56ff4dcfcdb55bd060aa8898d65c1ff97df89c0.
- Prepared release: 2377ee8f1c8f00baa7c441e8eea56c257df201d86f704fa89ab802222ff35fc7.
- Config: 8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c; schema36.
- Final source-pins manifest: e86de9cac1e76098abb37eae5a5b74229b89955e4ce113bc1ef5a85706fccd4c, 419 files.
- [Artifact index](missing-work-recovery/artifact-index.json): 29c972bef23120353e9b778c44c1fcfde358db6267476c3cdbb68063fbad0b1e.

For the preceding consolidated repair, the owner independently checked all 180 artifact files against working and committed bytes, 41 staged logs against original and compressed hashes, 16 final and two historical source-marker exports, 11 role/provider log archives, all 418 working/archive source pins, six code-check logs, typed release/config identities, static task XMLs, and the final typed process chain. All 283 prior M6 artifact records remain unchanged. That preceding process witness closes five synthetic shares with two fills; actual fees and net remain unknown (COSTS_PENDING). It does not claim a fee-complete broker-PAPER trade.

For the preceding residual package, the owner verified all 84 artifact files against working and committed bytes, 16 original/compressed logs, seven final and six historical marker exports, all 419 working/archive source pins, six code checks, typed release/config identities and four static task proposals. All 463 prior M6/consolidated-repair records remain unchanged. Exact-cutoff tests demonstrate fees corrected from zero to 0.20 and exclusion from learning until reconciliation. The archived corpus remains independently eligible under its original contracts.

For the final package, the owner verified 44 working/committed artifact files, six raw/compressed/original logs, eight final marker exports, all 419 working/archive source pins, six code checks, typed release/config and four static task proposals. All 547 prior artifact records remain unchanged. The final evidence covers the previously missing-work-arrival case and selects only the corrected outcome after reconciliation.

## Ordered operational review

1. Verify the accepted release under the existing leepa Limited task identity using the exact verifier-only proposal. It checks runtime files without the operating database, environment file, broker or functional roles. Static proposals are disabled and trigger-free; actual selected-token execution, host/job-tree and log access are not yet established. This step does not authorize functional startup, migration or trading.
2. Refresh writer/task/ownership evidence and WAL-aware backups with a restore into a separate path. Review the exact old-entry-writer fence and additive schema36 migration. Keep the existing Protection/Deadline services and nine original-owner positions. Preserve original client IDs and resolve uncertain sends; never restore an older database over later activity.
3. Acquire fresh account/position/cash evidence against the migrated store and exact reviewed release. Review the commission document within its 120-second freshness window; baseline capture must remain within five seconds of its original commission timestamp. Reacquire when stale. A combined acquire-and-commit operation requires explicit bounded approval; timestamps are never edited to simulate freshness.
4. After approved commissioning, start submission-disabled guardian and outcomes before trader. Verify independent processes, fresh readiness and useful progress. Removing submission/entry clamps and scheduling are separately named changes. Explicit entry resume never clears another manual stop or quarantine.
5. Observe an unforced broker-PAPER entry, protected exit, reconciliation, costed outcome and later daily learning receipt/model attribution. Existing XLE ownership or unknown actual costs may legitimately keep this acceptance waiting. Do not force trades, liquidations or model changes.

The proposed first learning origin is October 15, 2026 at 13:30 UTC; regenerate and review the config if the actual handover date makes that stale. Exact component-cost applicability, canonical metadata, current retained-owner provenance, selected-token execution and recent-order provider latency/quota capacity remain operational prerequisites.

## Review package

- [Original source-backed audit](../../reviews/2026-10-13-paper-redesign/audit.md)
- [Design](../../superpowers/specs/2026-10-13-paper-core-design.md)
- [Authoritative implementation plan](../../superpowers/plans/2026-10-13-paper-core.md)
- [Whole-branch findings](final-review.md)
- [Current prepared release, verifier command and artifact paths](missing-work-recovery/README.md)
- [Preceding residual correction and evidence](residual-cutoff/README.md)
- [Consolidated repair runbook and recovery semantics](final-repair/README.md); use the current missing-work-recovery release paths for future actions.
- [Implementation report and staged test results](final-repair/final-fix-report.md)
- [Consolidated repair scoped review](final-repair-review.md): R2–R4 addressed.
- [Residual cutoff review](residual-cutoff-review.md): normal and existing-work paths addressed; original residual preserved in this historical report.
- [Final scoped acceptance](missing-work-review.md): missing-work-arrival correction accepted; all identified R1–R4 findings closed.
- [Owner verification record](../../../.superpowers/sdd/2026-10-13-paper-core/missing-work-root-artifact-verification.json)
- [Owner decisions and preserved chronological rulings](decisions.md)

No operating-store migration, commission, task change, direct broker probe, forced trade, LIVE enablement or deployment occurred during implementation. At the user's request, a read-only dashboard check confirmed HTTP 200 from the existing deployed console on 127.0.0.1:8787; that console does not run this replacement. The user's original request requires review before disruptive migration. This package prepares that review; it does not grant operational approval.
