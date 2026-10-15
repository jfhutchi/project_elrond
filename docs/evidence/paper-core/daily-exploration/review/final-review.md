# Independent whole-branch review — daily PAPER exploration

## Scope and review basis

- Base: `0a5b6c47524f2c61f289e50973969881b6a55b89`.
- Reviewed source head: `36d61811e15a350da401eb15e6ac8980e7aea4f1`, branch `codex/daily-paper-exploration`.
- Binding requirements: `docs/superpowers/specs/2026-10-14-daily-paper-exploration-design.md` and the current `docs/superpowers/plans/2026-10-14-daily-paper-exploration.md`, including the integration-reproduced repair amendment.
- Source package: `review-0a5b6c4..36d6181.diff`, 323,066 bytes, SHA256 `87915374e712c316f2f2c7589bc3625dab604ab8937c905523941be77e020781`.
- Separate root-owned documentation snapshot: `final-current-docs.diff`, 113,239 bytes, SHA256 `041dfece2739f274672e9e1d80b6e8496f483417f3b65d067eee7c3abfe666eb`. These documents were pending commit, rather than part of the source head. The new `docs/evidence/paper-core/daily-exploration/README.md` was read explicitly.

I performed the review myself without subagents. The supplied source package was inspected in sequential, nonoverlapping partitions through line 6836, and the documentation snapshot through line 1255. The first documentation-heavy source-package display hit output truncation; the binding specification/current plan were also read directly. Subsequent source and documentation partitions were bounded. No source diff was regenerated for review.

The progress ledger and earlier task reports/reviews supplied context, but the verdict rests on the actual changed code, tests, focused surrounding code and retained raw evidence. Surrounding inspection was limited to concrete seams cut by the hunks: certification/revocation and ledger generation, exact runtime-adapter admission, final transactional send, synchronization exception/protection flow, learning selection/fold construction and offline preparation dispatch. This was a read-only checkout/index/HEAD review, with this report the only reviewer-written file. No operating store, env file, credentials, broker/network mutation, CI, push, role startup, migration or preparation helper execution occurred.

## Plan alignment

The implementation satisfies the source requirements across Tasks 1–4. Signal priority applies to the entire eligible cohort before diversity ranking; exploration retains the actual weak/negative prediction. Reservations use a semantic source boundary and durable possibly-sent usage. V3 gross learning is explicitly versioned, while legacy omitted-field serialization and V2/net behavior remain available. Current sizing funds remain separate from historical cash reconstruction, and authority is rechecked at claim, execution, compatible refresh, locked send and recovery/resume.

The three integration repairs are justified by the approved amended plan: preserving fractional provider timestamps, treating an exactly explained unpublished owned-fill delta as pending acquisition, and recognizing genuine historical COLLECT acknowledgements. The final partial-first-BUY fix reads a prior certified document only as historical position-basis evidence; it does not reinstate revoked cash authority.

Root-owned preparation, final evidence metadata, actual PR links and publication remain outstanding deliverables after this gate. This review does not mark the entire implementation/publication plan complete.

## Strengths

- **Selection is deterministic and replayable.** `src/quantbot/paper_core/selection.py:139` forms the qualifying pool first, then applies the exact daily-count/prediction/prior-count/universe tuple. Exploration uses its separate tuple. Captured cohort membership, unavailable reasons and immutable observations are checked during claim replay in `trader.py:302`. The tests cover a used qualifying stock beating unused weak stocks, unequal historical counts, forged selection and hiding a captured member.
- **Daily limits survive lifecycle changes.** `selection.py:26` excludes acquisition time, symbol, model and release from boundary identity. `selection.py:76` retains completed, rejected-after-send and unknown reservations, releasing only definitive unsent ERRORs. Claim uses the writer transaction, and `send_guard.py:196` rechecks membership/boundary/cap while excluding the current reservation once. Tests exercise concurrent claimers, restart, changed receipts, cross-day rotation and a seventh reservation after completed or unknown sends.
- **Current funds do not falsify accounting.** `funds.py:59` binds persisted certification, baseline, account/release/lane, current ledger/fills/orders, revocations and typed acquisition content to the exact store/config/commission. Five-second capture and 120-second age limits, restrictions, positive funds and empty open-order coverage remain enforced. Existing quantity certification returns before full-cash RECONCILED creation (`store.py:516`). The actual adapter/coordinator witness retains zero CashActivity rows, unknown fees and null net values.
- **Entry authority is checked at the consequential boundary.** `send_guard.py:37` checks unresolved intents before its same-proof return and validates original source evidence for compatible refresh. `runtime.py:147` checks current account, positions/basis and competing orders. The coordinator invokes the transactional send hook under BEGIN IMMEDIATE before the SUBMITTING transition (`src/quantbot/execution/coordinator.py:581`). The race controls first establish allowed gates, change the source or ledger, then prove no POST occurs.
- **Learning economics and causality are explicit.** `learning_procedure.py:38` dispatches net versus executed gross targets; only the fee-unavailable pending case is added. `activation.py:466` and `:484` bind target/procedure/policy to receipt, model and commission, including permanent trial-charge provenance. V3 retains training-history overlap while preserving past-only session folds, availability and train/validation embargo. `learning_procedure.py:187` weights the latest ten completed sessions 2 versus 1 for older history, and the older-than-sixty-session controls verify retained history affects fitting.
- **Recovery remains bounded by evidence.** `guardian.py:39` requires the current content-addressed source, account/release/PAPER endpoint, watermark/capture/baseline, genuinely linked stable owned acknowledgements, no global unknown send, exact all-symbol quantity explanation and position basis. Its final historical lookup at `guardian.py:131` preserves revocation. The 2+3 first-BUY regression checks the pending state, no new certificate/entry authority, real remaining activity, normal recovery and protected close. Prior integrity incidents remain active, and unrelated deltas refuse.
- **The integrated evidence tests the intended loop.** `tests/integration/paper_core/test_daily_paper_loop.py:517` collects sixty real-component fixture outcomes across ten sessions, performs the bounded fitter and operational activation, then verifies AAPL SIGNAL and MSFT negative-score EXPLORATION with exact model/receipt/activation attribution. Provider query bounds and activity publication are respected. The package accurately labels synthetic external inputs and heartbeat attestation.

## Issues

### Critical (Must Fix)

None found.

### Important (Should Fix)

None found. The prior Task 4 Important partial-first-BUY finding is addressed in the reviewed source head; no source finding remains open from that review or this whole-branch pass.

### Minor (Nice to Have)

No open minor source finding.

Two documentation finalization items were observed and resolved by the root during this review:

1. The supplied documentation snapshot's undated current introduction in `docs/evidence/paper-core/decisions.md:16` still described current-funds implementation as in progress. The current introduction now states implementation/task-review and combined-witness status, while directing readers to exact final review/preparation status. Chronological ruling 62 is preserved as history.
2. The same introduction linked the historical ledger through a removed current-tree `.superpowers` path. `docs/evidence/paper-core/decisions.md:25` now uses an explicit historical GitHub blob. This prevents the current guidance from implying scratch must be restored to the current tree.

The draft successor README still intentionally has pending whole-branch-review/preparation/publication fields (`docs/evidence/paper-core/daily-exploration/README.md:7`, `:31`). Update these only from actual post-gate results. Their pending state is a publication checklist item, not an implementation defect or authority to claim preparation already passed.

## Verification evidence and limits

No suite or heavy fit was rerun merely to duplicate retained evidence. I read actual retained results and inspected their code/test scope:

- Task 1 final focused selection gate: **25 passed in 17.70s**, `task1-logs/z5gx0l50.txt`. Compatibility/race subset: **7 passed, 20 deselected in 10.39s**, `task1-logs/tbwhs7vw.txt`.
- Task 2 final affected gate: **108 passed, 2 deselected in 254.68s**, `task2-logs/o5j1wldt.txt`. The excluded performance/interrupted-bootstrap cases are named in its report; this is not a whole-repository gate.
- Task 3 final-source gate: **90 passed in 199.24s**, `task3-logs/5qh4dhby.txt`, plus the receipt-age control **1 passed, 39 deselected in 5.42s**, `task3-logs/wb8em13r.txt`.
- Task 4 integrated affected gate on **8bae89e**: **180 passed in 332.24s**, `task4-logs/bsjgrc12.txt`. It includes the combined heavy learning witness and separate restart/send controls.
- Later partial-first-BUY correction on **36d6181**: **91 passed, 1 unchanged heavy fitting case deselected in 192.64s**, `task4-logs/fix1-urfrn2_a.txt`. Its exact regression changed from **1 failed, 11 deselected** in `fix1-_93ivtig.txt` to **1 passed, 11 deselected** in `fix1-hc17kkx8.txt`. The 180-test gate is not represented as having run on this later commit.
- The broader pre-first-BUY gate retained **328 passed, 1 failed in 543.09s**, `task4-logs/lyea33w3.txt`. The failure was the unchanged process test's seed assertion before child launch. Its exact unchanged focused rerun retained **1 passed in 14.01s**, `task4-logs/hzoihh6t.txt`. This qualification remains; the broad gate is not all-green.
- Retained Task 4 Ruff and five-source mypy logs report success; the later correction has its own Ruff and guardian-only mypy success. Earlier test-fixture typing failures are disclosed separately and are not presented as passing source checks.

I parsed the retained `task4-witness.json`: sixty final learning inputs, nine warmup receipts, raw weights 2, candidate `3800155d604f532ffe95967d481eb681d90b2e6e0e4de353a2e4993942cdb6d0`, receipt `92fdbf65ca7322dbf035b737cf035bdaa8b1b9fb8ac41e370cce8ff8cbe64b9b`, AAPL signal `+19.93355481727601` bps and MSFT exploration `-19.933554817275482` bps, 124 provider-shaped orders, 62 closed outcomes, zero cash activities and null fee/net fields. It explicitly declares independent-role heartbeat fixture attestation only. Separate selection/restart/cap and actual three-role process evidence are linked in the package rather than attributed to this combined case.

Fresh read-only checks during this review found all **89** raw task-log copies byte-identical to the new durable package copies. HEAD remained the reviewed source head, and source/tests/lock/build/launcher paths were clean against it. These checks validate evidence retention and source identity; they do not replace the reported test runs.

## Recommendations

- Publish the reviewed source as the authorized implementation PR, with the exact later-fix and broader-gate qualifications above. Finish the root-owned status/evidence index/link checks, actual PR URLs and offline candidate preparation before presenting those deliverables as complete. Preserve the sealed predecessor artifacts.
- The inspected `prepare-successor.py` changes only the distinct candidate configuration and calls the existing prepare/verify CLI branches. Those branches return before broker construction, credential loading or operating-database access (`src/quantbot/paper_core/__main__.py:287`, `:298`); the helper strips credential environment variables and records the provisional learning origin and unperformed operational actions. I did not execute it or validate a resulting manifest. Review its actual outputs/pins after execution; a flag called reviews-complete is an assertion, not this review's result by itself.
- Keep the new economic configuration and V3 commission separate from executable-only maintenance. Before operational handover, refresh original IWM/order ownership, writer/protection and backup evidence, establish schema-compatible protection, and perform the explicit reviewed migration/commissioning sequence. Do not infer freshness or readiness from dated captures or a prepared manifest.

## Declined to judge

- **Natural Alpaca lifecycle, current account/order state, provider latency/quota capacity and genuine activity-publication latency:** require fresh broker/operating observations; this gate is offline/source-only. The dated 01:44 UTC IWM and 02:00 UTC disabled/uncommissioned captures are historical evidence only.
- **Successor prepare/verify results, final manifest pins, selected-task-token verification and actual PR/publication metadata:** root-owned post-review work has not yet occurred; the helper's named preparation side-effect boundary was inspected, but no resulting artifact or remote state is certified here.
- **Operating schema-29-to-36 migration, inherited-nine-owner handover, scheduler/process-tree rollout and compatible legacy protection transition:** explicitly separate operational acceptance requiring fresh evidence and controlled actions; no operating store or task was accessed by this review.
- **PostgreSQL parity and natural Windows deployment acceptance:** unavailable in the retained affected gates; SQLite protocol fixtures and an unchanged synthetic process rerun do not establish these platforms' operational results.
- **OS-process independence, restart and cap inside the combined learning case:** that case uses explicit heartbeat attestation and does not claim those observations. Separate durable reservation/restart/send and actual three-role-process tests supply their stated coverage, without turning the combined fixture into a natural deployment.
- **Guaranteed six daily fills, daily parameter changes, net profitability, calibrated confidence or monotonically improving performance:** the approved contract promises bounded attempts and daily consideration, subject to genuine gates; gross-return procedure validation and synthetic observations cannot establish these outcomes.
- **Arbitrary cross-owner/economic migration or automatic reinterpretation of an existing net predictive commission as gross:** the plan explicitly refuses such a transition. This source is reviewed for a separately prepared initial COLLECT commission and preserves refusal; no unsupported store reset is required to approve the source.
- **Unrelated repository functionality and additional full-suite/heavy-fit repetitions:** no concrete unresolved interaction justified expanding this review beyond the changed contracts and named surrounding seams. Existing retained broad-gate failure and all test exclusions remain disclosed.

## Assessment

**Ready to merge? Yes — technical source review approved.**

**Reasoning:** No open Critical, Important or Minor source defect was found in the reviewed range. The source and inspected documentation contract support publishing the authorized implementation PR, with root-owned factual metadata/preparation still to be completed; this is not permission to merge implementation silently or to deploy, migrate, commission or enable entries.

**Operational readiness: No.** The inherited-order/ownership/protection, migration, exact successor verification, commissioning and natural broker acceptance gates remain separate and unfulfilled by this review.
