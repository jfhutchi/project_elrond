# Residual cutoff acknowledgement repair

This bundle repairs only the remaining R1 defect identified by the same final reviewer: a cutoff sampled before late notifications must not acknowledge the newer version captured under the lot lock. Base commit is `ee079d387148e691f3b11152e218dc11dfcc4a1e`. R2–R4 remain accepted from that commit. Owner authorization explicitly permits this bounded residual correction; no operational action occurred.

## Behavior and compatibility

`core_document_index` now holds one `core_outcome_notifications` watermark per lot, with the dirty version as its reference and the maximum originating evidence availability. Every production claim/source/cost/receipt/proof/conflict/revocation notification supplies its explicit evidence timestamp in the same business transaction as the work version. No wall clock or poll cutoff supplies notification availability. Acknowledgement requires both that captured watermark be covered by the historical cutoff and that the existing version CAS still matches. Historical resolution still excludes future evidence and cannot roll the current outcome backwards. Notifications during resolution still defeat the CAS.

For missing pre-repair v36 metadata, the existing sixteen-lot selection also selects owned ENTRY lots lacking a watermark, including absent work and previously acknowledged19/19. The lot lock initializes unknown recovery work and obtains conservative availability from exact indexed linked-client sources, linked-fill costs, account proofs, the maximum-publication guardian pointer, and the original decision. Indexed timestamp maximum queries return one value per source class; no whole-history payload hydration or recurring full scan is added. The guardian publication pointer advances by serialized UTC timestamp, not insertion order. Broker contracts normalize UTC. Cold coverage uses the end of the selected UTC second to conservatively handle existing variable fractional serialization within that second. That next-second bound applies only to cold initialization; ordinary notifications preserve exact availability. An existing explicit timestamp is retained in the cold maximum. Unknown provenance cannot silently authorize acknowledgement; quarantine/refusal remains pending.

Processed version alone does not establish known coverage. Even a new explicitly timed notification on an already acknowledged legacy row remains unknown unless a matching prior watermark exists. The extra arrival control supplies a timestamp older than retained late fees and demonstrates why arrival order cannot replace coverage.

Learning excludes all existing dirty work and owned ENTRY outcomes lacking work/watermark before their first bounded recovery pass. Immutable archived decision/observation/outcome corpora without runtime ENTRY ownership retain their existing eligibility and protection validation. No fixture-specific authority, history cap, weighting change, target hydration shortcut, schema change or immutable evidence rewrite is introduced. The directly affected healthy-M5 test performs production bounded catch-up before expecting readiness; raw fixtures remain unchanged.

## Validation and phase boundaries

- Original cutoff RED:2 failed in4.23s, durable19/19 despite later two0.10 fees. The reviewer diagnostic independently demonstrated the original defect; its passing assertion is not repaired-product success.
- Earlier intermediate controls and failures are preserved separately in `logs.json`, including learning exclusion before catch-up, healthy legacy setup, archived-corpus eligibility, and interrupted-turn completion. Intermediate sources were not individually frozen; exact raw assertions/results are retained without claiming those runs apply to the final source.
- Stage1 frozen affected gate:28 passed in51.12s plus six CI checks. Exact nodes/commands/pins/proposal/archive/log remain under `stage1/`. This source preceded the final narrow known-predicate correction.
- Extra legacy-arrival RED:1 failed,5 deselected in3.28s at20/20. Exact stage1 production code and added test bytes/hash are retained in `legacy-arrival-red-provenance.json`.
- Final frozen guard gate:9 passed in16.31s. Complete cutoff file (five owned variants plus archived-corpus control), existing idle-history, healthy/dirty-learning and during-resolution CAS/revocation controls. Final six CI checks pass: Ruff src/tests; mypy Linux and win32 over358 source files; Ruff and both mypy platforms over the three CI helpers. Commands and exact log hashes are in `commands.json`, `covering-result.json`, `ci-results.json` and `logs.json`.

Final regression exports show old-cutoff pending, restart, corrected total fees0.20, only the corrected outcome selected by compatible learning policy, covered-current acknowledgement, and historical no-rollback. The archived corpus separately selects40 outcomes without runtime ENTRY ownership. The retained-history export records settled idle behavior and the existing notification-during-resolution guard. These are isolated synthetic accounting/selection controls, not natural broker or trained-model execution claims.

No capacity, heavy fit, complete repository, PostgreSQL or actual independent-role witness was rerun. Historical process/capacity/learned-model proofs remain in `../final-repair/` and earlier bundles with their original source bindings; they are not relabeled as final-source witnesses. `prior-artifact-preservation.json` verifies all180 final-repair records plus its index against the base commit and all123/83/77 M6 records. All old bundles and fixture bytes remain unchanged.

## Exact prepared successor

Final source:419 pins; manifest SHA256 `0f2e071f5f44f3bcfe3f9425235d2de4a3672801400c9bb965b12d5f6da35eca`; archive SHA256 `4dddbd5578128a7f3f3274342e627b126f7acd8cb240f950737ea670fad29a8c`. Changed executable files use the existing Windows CRLF checkout policy before freeze. Nested evidence attributes preserve captured bytes.

Prepared release `3ddda51746ff8d469a9bd2344129aaad658ac4ef9230a062335a7905f99fd034`, unchanged config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`, schema36. Typed config/release roundtrips and read-only executable verification pass. Previous final-repair release `c8324309ebd348452ff8eef9d6cb927a25a3a5209c7e897e5e7a9c3a8ea1167c` correctly refuses the five changed executable pins. Stage1's prepared successor is separately retained, superseded by this proposal. Supported same-schema executable-binding protocol remains unchanged; no binding transition, acquisition, migration, commission or resume was performed.

Four XML proposals name the current residual config/manifest and exact launcher bytes, with no triggers, disabled state, LeastPrivilege and PT0S. Functional roles retain submission/entry clamps. The mutually exclusive Verify action performs the existing executable verifier only. Nothing was registered or started, and actual selected-token/host/policy behavior remains unproven. Prior commissioning and recovery gates remain in the final-repair runbook; any future approved action must use this successor's exact config/manifest paths rather than predecessor paths.

```powershell
$Repo = 'C:\Users\leepa\.codex\worktrees\paper-core-m0\project_elrond'
$Evidence = Join-Path $Repo 'docs\evidence\paper-core\residual-cutoff'
& "$Repo\.venv\Scripts\python.exe" -B -m quantbot.paper_core `
  --config "$Evidence\prepared-config.json" --manifest "$Evidence\prepared-release.json" --command verify
```

This is a concrete later verifier proposal, not evidence that the Limited-token hidden-host action ran. Operating-store migration, backups, legacy-writer fencing, current account/owner/cost evidence, actual guardian/outcomes readiness, natural PAPER trading and daily learning/activation retain separate approval/evidence requirements. No service or security policy changed.

## Artifact layout

`artifact-index.json` retains the prior schema: relative `files` with bytes/SHA256, `export_provenance`, `historical_export_provenance`, and `process_logs`. Selected JSON exports are tied to exact raw log marker/occurrence/source line and the correct phase's source-pins hash. `process_logs` is empty because no new actual-role/provider witness was generated. Current and stage1 source archives, proposals, CI and raw gzip logs are separate. Historical setup/intermediate raw logs are evidence of their own runs only.
