# Exact missing-work-arrival residual — same-reviewer verification

**Verdict: ACCEPT. The exact residual is ADDRESSED. No actionable finding remains in this scoped correction.** The normal cutoff path and previously accepted R2–R4 remain accepted; they were not reopened. Together with the preceding scoped dispositions, this closes the identified R1 cutoff/notification residuals. Operational acceptance remains separate and waiting.

Reviewed range: `fa7a1fb561ccd113e6d47d017c12bd1d89713426..d56ff4dcfcdb55bd060aa8898d65c1ff97df89c0`. Independently verified current HEAD and package SHA256 `bdbef1976b0d900875723b94ce936467c656dd6deab1243044e0654962b77eb7`. Current prepared release is `2377ee8f1c8f00baa7c441e8eea56c257df201d86f704fa89ab802222ff35fc7`, config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`, schema36 unchanged.

## Source assessment

The change is confined to the missing-work distinction previously requested. `src/quantbot/paper_core/work.py:67–91` defaults `_initial_claim` to false. An explicit timestamp alone can no longer classify absent work as known coverage. Generic notifications preserve their timestamp but remain unknown until the existing bounded cold derivation covers retained evidence. Existing known-watermark/version matching remains intact.

The sole production opt-in is `src/quantbot/paper_core/trader.py:359`, after actual intent and ENTRY-link creation within the existing `BEGIN IMMEDIATE` transaction. The existing observation/ENTRY replay guard at lines284–289 returns before this branch. Source search found no other production opt-in. The added normal-case control calls the identical claim a second time before any send or economic change, verifies replay returns None, and observes initialization exactly once. It exercises the real claim path rather than granting coverage through a mock.

This is an appropriately small correction: no new schema, cold-query, learning-selector, authority or runtime-recovery redesign. The sixteen-lot bound, evidence-time watermark, version CAS, dirty-learning exclusion and historical no-rollback behavior remain as previously reviewed.

## Exact regression and evidence

The complete cutoff file now includes the exact `missing_work_then_older_notification` variant. Its assertions cover exclusion before recovery, old-cutoff pending, restart, current corrected outcome, acknowledgement, compatible selection and historical no-rollback. The frozen raw marker reports:

| Point | Observed result |
|---|---|
| Older notification after absent work/watermark | work1/0 |
| Old-cutoff processing at14:30:00 | remains1/0 |
| Restart and covered cutoff at14:30:02 | corrected fees0.20; work1/1 |
| Compatible learning selection | only corrected outcome `32f194051b3826a946a88c0119eaa1cba711d65d0335007f43bb3d0411ef2ff5` |
| Historical processing | no rollback |

This directly reverses the preceding review's load-bearing failure, where work became1/1 before correction and the stale zero-fee outcome was selected. The normal marker separately confirms initial claim/replay validation and its covered19/19 acknowledgement.

The frozen gate reports **10 passed in17.25s**, covering the complete cutoff file and the same three R1 idle-history, healthy/dirty-learning and during-resolution CAS/revocation controls. Six CI checks passed. I independently read and hashed the compressed raw gate, confirming raw SHA256 `842fb986ea22144922a67b725acef73b5cbde0016f10b838f5d8e3d736159ac6`, and inspected the two relevant source markers. I independently checked all419 working source pins against the manifest and every corresponding frozen ZIP entry, and all six CI exit codes/log hashes. Source-pin manifest: `e86de9cac1e76098abb37eae5a5b74229b89955e4ce113bc1ef5a85706fccd4c`.

Root additionally verified44 working/Git artifacts, six original/raw/compressed logs, eight marker exports, the static proposals and typed release verification, and preservation of all547 prior artifact records. Those exhaustive inventory/operational-proposal checks are root-provided evidence, not claimed as rerun here. The earlier RED, focused7-pass run, preceding residual9-pass gate and earlier stages retain their separate source bindings; they are not summed or relabeled as this final gate.

No test suite, provider, operating store, actual role, capacity or heavy-fit run was performed by this reviewer. Verification used source inspection and frozen evidence only. This report is the sole checkout write.

## Declined to judge and retained limits

- R2–R4, the accepted normal cutoff path and unrelated behavior: closed by prior reviews and outside this exact correction.
- Additional hypothetical metadata-loss scenarios: explicitly outside scope; no new cases were explored.
- Why an operating work row might be missing: ordinary v36 migration seeds ENTRY work and new claims create it transactionally; this correction fulfills the explicitly supported missing-work recovery contract.
- Actual trained-model behavior or profitability: the regression verifies accounting and metadata selection, not fitting, qualification or activation.
- Actual selected-token/host/policy execution, backups, legacy-writer fencing, operating migration/commission/resume, current provider/account/cost/owner authority and natural PAPER/daily-learning acceptance: not performed or authorized; prior operational review gates remain.
- Full-repository, PostgreSQL, capacity or new independent-process claims: no such gate was rerun; older evidence keeps its original source version.
- Owner documentation pending acceptance: expected packaging state, not a new finding.

**Spec and quality verdict: ACCEPT for the exact missing-work-arrival correction.** The private initialization contract distinguishes genuinely new claim creation from recovery and closes the reproduced stale-cost/selection path without expanding behavior. No further repair is requested by this scoped review. Acceptance does not authorize deployment, task changes, trading, migration or any operational action.
