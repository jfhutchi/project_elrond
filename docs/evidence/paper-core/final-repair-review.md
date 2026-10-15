# Consolidated final repair — same-reviewer scoped re-review

Verdict: **REWORK — R1 NOT ADDRESSED in full; R2, R3 and R4 ADDRESSED.** One reproducible P2 residual remains in R1. No additional general redesign is requested. This is a software review, not operational acceptance or authorization.

Reviewed exact range `31cd5167dc7ef9a50a39850df9c131f7c70f84c4..ee079d387148e691f3b11152e218dc11dfcc4a1e` in the designated managed worktree. HEAD was independently checked. The supplied text package hashes to `4b20f6bc0c5571059a0add558282b3594001f8e5e7a5c42b30852b6ba578b555`. Current proposed release is `c8324309ebd348452ff8eef9d6cb927a25a3a5209c7e897e5e7a9c3a8ea1167c`, config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`, schema36. Owner documentation edits present during review were not changed by this reviewer.

## Strengths and per-finding disposition

| Finding | Verdict | Source and evidence assessment |
|---|---|---|
| R1 retained-history work | **NOT ADDRESSED in full** | The performance repair is substantive: bounded durable work selection, indexed immutable references/proof-fill memberships, current heartbeat-attested receipts, per-plan receipt memberships, exclusion of settled lots from ordinary guardian work, and linear ledger lookups remove the identified repeated historical resolution. Existing late-fee/restart/revocation and during-resolution CAS controls are useful. However, the notification/cutoff race below can permanently suppress a valid correction and clear its learning exclusion. |
| R2 executable maintenance | **ADDRESSED** | `authority.py:354` implements explicit immutable executable-binding transition and a CAS current pointer while retaining the original economic config/commission/model/receipt graph. It requires exact prior binding/releases/fence/activation, fresh approval and fencing evidence, equal schema and complete RuntimeConfig. Startup (`__main__.py:87`), claim and final-send authority check the current binding. Historical identities are not rewritten; predecessor authority refuses. This is an explicit v36-compatible code-only protocol, not arbitrary semantic migration or v35 operational adoption. |
| R3 fenced-entry resume | **ADDRESSED** | `authority.py:412` supplies the previously missing audited producer. Exact fence/commission/runtime/activation, fresh proof and independent guardian receipt/heartbeat/time evidence, current economic authority, unresolved-send checks, boundaries and health are checked under the writer fence. Separate manual stops and quarantine remain. The integrated fixture preserves and resolves the original unknown client/owned exit while fenced, refuses bad resumes, then demonstrates a fresh resume/new claim and later-fence send refusal. |
| R4 initial commission interruption | **ADDRESSED** | `authority.py:161` and `activation.py:238` write commission, baseline/canonical/event, initial COLLECT activation and initial executable binding in one transaction. Injected model-write failure rolls authority back; restart can retry the original still-fresh approval. Completed exact replay validates original bootstrap state and cannot overwrite a learned/changed activation. The precreated identity alone is inert. |

The added readiness/certification repairs remain within the adjudicated scope. Current receipt selection follows the actual heartbeat reference. Proof lookup uses exact fill membership rather than comparing host and provider timestamps. Provider acquisition remains outside the local construction/certification transaction; the transaction closes the generation gap without rebasing an actual economic change. Exact named CAS recovery is limited to two total acquisitions within one shared five-second budget. Same-boundary claim retries retain the original decision/deadline/evidence and rerun locked guards. The construction exception guard preserves the original error and rollback. I found no additional concrete breakage in those inspected repairs.

The independent-cash fingerprint change is narrowly conditional: allocation revision IDs are omitted only when complete accepted independent exact-fill cash actually governs cash. Actual cash movements and other economic identities remain material; incomplete/disputed/cost-derived cases retain refusal. Its baseline defect attribution is supported by the supplied accepted-`bcc0f118` source/provenance diagnostic, rather than claiming it was introduced by this repair. The new derivation marker requires fresh proof authority without rewriting old proof IDs.

## Important residual — P2: acknowledge only work covered by the outcome cutoff

**Location:** `src/quantbot/paper_core/outcomes.py:686` and `:713–720` (the new-outcome acknowledgement at `:757–760` has the same unqualified version semantics). Live caller: `src/quantbot/paper_core/roles.py:278`. Learning pending guard: `src/quantbot/paper_core/learning.py:239–251`.

The caller samples `cutoff` before work selection and before the per-lot `BEGIN IMMEDIATE`. A newer cost/source notification can commit after that cutoff but before line 686 captures `work.version`. Resolution correctly excludes evidence later than cutoff. When it reproduces the currently stored outcome, lines 713–720 nevertheless mark the newer version processed. The next current-time poll selects no work, so the correction is lost until an unrelated later notification happens to dirty the lot. Restart does not repair the acknowledged durable state.

This is different from `test_dirty_closed_cost_restart_revocation_and_notification_during_processing` at `tests/integration/paper_core/test_final_repair.py:419`: its monkeypatch at lines 481–487 increments the version **after** resolution has started and after the version capture. That existing control correctly exercises CAS mismatch. It does not cover evidence arriving **after cutoff but before version capture**.

### Narrow reproduction and exact result

Used the existing isolated, credential-stripped, external-network-disabled `check.py` wrapper with a single scratch pytest and the synthetic `core_m2` fixture. No operating database, provider, credentials or operational process was accessed. No broad suite was rerun. The diagnostic asserts the defect, so its PASS is evidence of the failure mechanism, not a fix.

1. Create one owned synthetic entry/exit and a COMPLETE outcome with explicit zero fees at `2026-10-14T14:30:00Z`.
2. Retain that poll cutoff. At `14:30:01Z`, import two valid superseding fee revisions of 0.10 each.
3. Call `process_outcomes` with the retained cutoff, then again with the current cutoff.
4. Compare stored current outcome with a direct current-time `reconcile_round_trip`.

Observed state:

| Check | Result |
|---|---|
| Dirty before old-cutoff processing | version 19, processed_version 15 |
| After old-cutoff processing | version 19, processed_version 19; zero new outcomes |
| After next current-time poll | still 19/19; zero new outcomes |
| Retained current outcome | COMPLETE, fees 0, net 0; unchanged ID `ef02ae2eeeeff4218a1f6937ad452b477a60ed5489f3da333ac1409a521540fb` |
| Direct current-time resolution | fees 0.20; corrected ID `7df0ea2b50a2a8e61c737dfcf0f7fde2706e8c184ccddf8fa7cc09c2ecd64e0f` |

Lot ID: `954f52485e95e2d2d4441756bb4d7aaf0f5760552e70bf5dbdbf081bd276ac40`. The correction documents remain stored; the problem is the consumed work notification, not lost raw evidence.

The learning pending guard is consequently removed because version equals processed_version. Source review establishes that an otherwise compatible, unprotected COMPLETE outcome can then pass that guard with stale net costs. **This diagnostic did not demonstrate actual learning eligibility or fitting:** its fixture remains excluded as `INCOMPATIBLE_CONTRACT_OR_LANE`, including a second characterization run matching the outcome dataset/policy/lane. No learned-model update or profit impact is claimed. The stale COMPLETE accounting result alone makes the finding material.

Final scratch test: `C:/Users/leepa/AppData/Local/Temp/final-review-r1-cutoff-test.py`, SHA256 `3224d2782c180b1d3e19f54091ddaa30ed2bde8fc66f78c3f8b3f1f280acfcf2`.

Final diagnostic log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-n2ukb22c/result.txt`, SHA256 `f84ac11956b9d11d4596510b5848bbe42b3c1e6be43eca260a1354fb1c9aeb3f`, **1 passed in 3.88s**. Initial diagnostic log: `C:/Users/leepa/AppData/Local/Temp/paper-core-check-en_9lw2r/result.txt`, SHA256 `c91d2fafc38566e9711e778083492370325ac4d80e5ee24961e592818cee2697`, **1 passed in 3.64s**. These are two characterizations of the same narrow defect, not additional covering gates.

**Minimal repair semantics:** an acknowledgement must consume only the dirty notifications whose evidence is covered by the resolution cutoff. Preserve a durable pending state when newer notifications/evidence are outside that cutoff; a historical cutoff must never consume later work. Merely retaining the existing version CAS is insufficient because the newer version can already exist before it is read. Do not solve this by truncating history, silently accepting future evidence into an old cutoff, or broad historical polling. A cutoff-compatible notification watermark/version snapshot or equivalent narrowly bounded acknowledgement is sufficient.

**Narrow validation:** add this exact cutoff-before-notification-before-lock ordering; assert work remains pending, then a current poll records the 0.20 revision and clears only covered work. Preserve the existing during-resolution CAS control and historical no-rollback behavior. Assert pending exclusion until reconciliation and compatible eligibility only after the corrected outcome where a compatible fixture is available. No further general redesign or broad test rerun is requested by this review.

## Evidence scope and operational sequence

Frozen results remain distinct: phase1 **449 passed, 3 failed, 26 PostgreSQL skips / 582.74s**; repaired phase2 **296 passed, 26 PostgreSQL skips / 283.28s**; final exception-guard gate **105 passed / 166.52s**, plus six final CI checks. The 0.500s synthetic 50-trade capacity witness belongs to phase1. No summed/full-repository-green claim follows. Root's independent artifact verification covers 180 working/Git artifacts, 41 raw/compressed/original logs, 16 final and two historical marker exports, 11 process archives, 418 source/archive pins, six CI logs and unchanged prior M6 records. Those complete inventory checks are root-provided evidence, not claimed as rerun by this reviewer.

The final actual-process typed chain is separate from mocked maintenance: trader PID23124 BUY and guardian PID14648 SELL, original IDs/two fills/five shares closed, gross zero, COSTS_PENDING with unknown fees/net, SYNTHETIC_TEST. The maintenance fixture uses COLLECT and null learning receipt; the populated-v35 migration separately preserves the learned model and non-null qualifying receipt. These must not be presented as one learned operational handover.

README and the owner draft handover now provide a usable staged approval sequence: exact verifier-only selected-token proposal before commissioning; separately approved writer fencing and WAL-aware backup/restore/migration; fresh acquisition and commission within original approval/capture windows; submission-disabled guardian/outcomes before trader after commissioning; separately reviewed clamp removal/scheduling. Reacquire stale approval evidence rather than altering timestamps. A maintenance switch keeps the durable entry fence and original owned plans, requires compatible reviewed bytes/exact binding and actual old-writer fencing, then fresh successor protection and resolution of original unknown sends before explicit resume. No command or XML proposal grants operational approval. Current release should not be described as fully accepted while this P2 remains.

## Declined to judge

- P3 repeated learning comparator/receipt evaluation: explicitly deferred by owner and outside the changed R1–R4 scope.
- A new general throughput/control redesign: unnecessary to resolve the concrete residual; the recorded synthetic timing is not a provider-capacity claim.
- Actual selected leepa Limited-token, host/job-tree/script-policy/log behavior: only static proposals and isolated tests exist; execution is a separately approved action.
- Actual provider latency/quota/current account/cash/cost applicability and canonical/retained-owner provenance: operating access was prohibited and no fresh operational evidence is supplied.
- Legacy nine protected positions, real old-writer fencing, live backup/restore, schema migration and commission: not performed or authorized; preserve original protection/ownership until explicit operational review.
- Natural broker-PAPER lifecycle, actual daily warmup/learning/activation, profitability or any forced trade: separate operational acceptance remains waiting; synthetic, mocked and real-data simulation lanes do not establish these outcomes.
- Arbitrary semantic/owner transfer or operational v35 commission adoption: not part of the approved same-schema v36 code-only successor protocol.
- PostgreSQL operation or full-repository success: selected store is SQLite and frozen skipped/affected gates do not establish these broader claims.
- Actual fitting/selection harm from the reproduced stale outcome: no fit was executed, and this diagnostic fixture is incompatible with the learning policy; the removed pending guard is source-backed, while downstream acceptance is conditional.

## Assessment

**Spec: REWORK on R1; R2–R4 satisfy the scoped repair requirements. Quality: with the one P2 fix required. Ready to accept the consolidated repair as complete: No.** The narrowed projections and explicit recovery authority are sound improvements, but acknowledging evidence outside the resolution cutoff breaks truthful corrected costs and durable lost-notification safety. Owner adjudication under the single-final-wave process is required; this reviewer implemented no fix and dispatched no further work.
