# Residual R1 cutoff repair — same-reviewer verification

**Verdict: REWORK for one exact missing-work recovery combination. The original normal-path cutoff defect is addressed; R2–R4 remain accepted and were not reopened.** No general review, redesign or further exploratory cases are requested.

Exact reviewed range: `ee079d387148e691f3b11152e218dc11dfcc4a1e..fa7a1fb561ccd113e6d47d017c12bd1d89713426`. HEAD was checked independently. The supplied text package hashes to `efb90335a993cd171c4439cff59a1f51afa37470d23bfcfcd66627c2a4db12b7`. Current release is `3ddda51746ff8d469a9bd2344129aaad658ac4ef9230a062335a7905f99fd034`; config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`, schema36 unchanged. Read-only source review covered the five changed executable files and focused tests, brief, README, staged results and selected exports. The only checkout write by this reviewer is this report.

## What is addressed

`process_outcomes` now requires a version-coupled notification watermark to be covered by the caller cutoff before acknowledging either unchanged or newly revised outcomes (`outcomes.py:747,787`). The existing version CAS remains, so a notification during resolution still defeats acknowledgement. Production source/cost/claim/proof/receipt/conflict/revocation call sites supply explicit originating timestamps in their business transactions. Neither the wall clock nor caller cutoff substitutes for ordinary notification availability.

The frozen normal-path export shows the original case correctly: old cutoff retains work19/15, restart/current cutoff records fees0.20 and work19/19, compatible learning selects only the corrected outcome, and a later historical poll cannot roll it back. Existing-work missing-watermark/preacknowledged recovery, including an older notification before catch-up, is also covered by the supplied tests.

Cold recovery remains bounded to the existing sixteen-lot selection. It derives a conservative availability watermark from linked sources/costs, account proof/publication indexes and decision evidence once, without hydrating all history every poll. Dirty rows are excluded from learning regardless of origin; missing work/watermark exclusion uses the same owned ENTRY predicate as recovery. The separate immutable archived learning corpus retains eligibility without fabricated runtime notification authority. The healthy historical fixture uses production bounded catch-up rather than rewriting its evidence.

## One remaining P2: a first notification can misclassify missing legacy work as new work

**Location:** `src/quantbot/paper_core/work.py:79–83`, especially `previous is None` at line80. Consequence flows through `outcomes.py:747` and the pending filter in `learning.py:239–275`.

When an existing owned lot has retained outcome/cost evidence but its work row and watermark are missing, an arriving notification with an explicit timestamp is classified as known merely because `previous is None`. That is valid for a genuinely new claim, but does not prove coverage for legacy recovery. If this notification predates already retained fee corrections, the created version1 watermark is too old. An old-cutoff poll acknowledges1/1, and subsequent current polls skip the stale outcome. The required cold reconstruction is bypassed because both newly created metadata rows appear present and version-matched.

**Reachability limit:** the ordinary v36 migration seeds every ENTRY lot (`0036_core_recovery.py:29–34`), and new claims create their work row transactionally (`trader.py:345–359`). This review does **not** claim those paths spontaneously lose their rows. The defect is in the explicitly advertised, required and tested missing-work recovery contract. The reproduction combines that accepted initial state with the same older-notification-before-catch-up ordering already tested for preacknowledged existing work. It does not require a new external operational scenario or broaden the review scope.

### Exact isolated reproduction

Derived a scratch test from the new production regression file, changing only the older-notification variant to remove both work and watermark before the notification. Used the existing credential-stripped, external-network-disabled `check.py` wrapper and synthetic fixture. No source edits, broad suite, operating database, provider or actual role execution occurred.

Sequence: retain a COMPLETE zero-fee outcome at14:30:00; import two superseding0.10 fees at14:30:01; simulate the supported missing-work+watermark state; notify at the older14:30:00 timestamp before recovery; process old cutoff, then current cutoff.

| Observation | Result |
|---|---|
| After notification | version1 / processed0 |
| After old cutoff | version1 / processed1 |
| After current cutoff | still1/1; zero new outcomes |
| Stored current outcome | COMPLETE, fees0, net0 |
| Direct current resolution | fees0.20 |
| Compatible learning selection | stale original outcome selected; no exclusions |

Lot: `6039e683c8ff9d773d27ab1771eb28b6d85aa9d2771b5efe402a29c3203a4133`. Stale selected outcome: `4cbb2d66e4edb8f0be4310c8470bb29751db9ee84aecd03907316bb990ebb3df`. This diagnostic establishes actual selector acceptance of the stale label; no fit, model activation or trading impact was executed or claimed.

The invariant test failed **1 failed,5 deselected in3.89s**, asserting `1 > 1`. Raw log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-m0eqe6p2/result.txt`, SHA256 `2012cfb702d84e6ba9ad1ce632475303a4ed7100b8c61358e2a71c64a397550e`.

The follow-up characterization asserts the defective next-poll/selector behavior and reported **1 passed,5 deselected in3.40s**; this PASS demonstrates the bug, not a repair. Raw log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-cu401oyw/result.txt`, SHA256 `f2db960a4896b7408710bda5fdbf9b7d6e5c6ec364f0b3d04cf04738698812ba`. Final scratch file: `C:/Users/leepa/AppData/Local/Temp/residual-review-missing-work-arrival.py`, SHA256 `fbceeb2b663d66343267e71476bc6d6608fdd4fecf7d727ae980705b63cba191`. An earlier collection attempt lacked the fixture import path and performed no test (`paper-core-check-uo74lqsi/result.txt`); it is not product evidence.

**Smallest required semantics:** missing work alone cannot establish that no earlier evidence exists. Distinguish explicit genuinely new claim initialization from recovery of an existing lot, or conservatively leave first-arrival coverage unknown until the existing bounded cold derivation covers retained evidence. An older notification must not turn unknown missing-work recovery into known coverage. Preserve transactionality, exact timestamps, version CAS, sixteen-lot bound and archived-corpus eligibility.

**Validation:** add this exact missing-work-then-older-notification variant to the existing regression. Require pending after old cutoff, corrected0.20 outcome at covered cutoff, acknowledgement only afterward, and only corrected compatible learning selection. Retain the already frozen normal/new-claim, existing-work legacy, during-resolution CAS and historical-no-rollback controls. No broader audit or redesign is justified by this finding.

## Frozen evidence and limits

Final guard: **9 passed in16.31s plus six CI checks**. Prior frozen stage1: **28 passed in51.12s plus six CI checks**, kept distinct. Earlier RED/intermediate logs retain their own source/version limitations. The supplied exports substantiate five owned variants and the separate forty-outcome archived corpus, but omit this missing-work-arrival combination.

Root independently verified84 working/Git artifacts,16 raw/compressed/original logs,7 final+6 historical exports,419 working/archive pins and six CI logs, with all prior M6 and final-repair records unchanged. Those exhaustive inventory checks are root-provided, not claimed as independently rerun here. Source/package inspection and the narrow reproduction are this reviewer's own verification. No capacity, broad fitting, full repository, PostgreSQL or independent-role gate was rerun or implied.

## Declined to judge

- R2–R4 and unchanged runtime architecture: already accepted and explicitly outside this verification.
- Why an operating store might lack a work row: no normal migration/claim loss is demonstrated; missing-work recovery itself is the expressly supplied contract under review.
- Additional hypothetical corrupt-metadata combinations: not explored; the verdict is restricted to the reproduced combination.
- Actual Limited-token/host/policy behavior, backup/migration/commission, legacy owner protection, provider/current account/cost applicability, natural PAPER trading and daily learning: no operational evidence or authorization; prior approval limits remain.
- Profitability, trained-model harm or actual execution from the stale selection: not exercised; only selector acceptance is demonstrated.
- Owner handover/plan pending wording: expressly pending this acceptance, not a new defect.

**Scoped assessment:** normal seeded-work cutoff repair accepted; complete residual R1 recovery contract not yet accepted because of the single P2 above. R2–R4 remain accepted. Owner adjudication is required; no fix, deployment or further dispatch was performed by this reviewer.
