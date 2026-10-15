# Missing-work recovery correction

This bundle fixes only the same reviewer's missing-work-plus-missing-watermark arrival defect. Base is `fa7a1fb561ccd113e6d47d017c12bd1d89713426`. Normal seeded-work cutoff handling and R2–R4 remain accepted; the owner authorized this exact extension of the residual correction. The prior `residual-cutoff/`, `final-repair/` and M6 bundles remain immutable.

Production changes are limited to `work.py` and one call in `trader.py`. A generic missing work row does not imply a new lot. The private `_initial_claim=False` argument allows known first-version coverage only when actual new ENTRY intent/link creation explicitly opts in, after insertion under the existing writer transaction. Existing claim replay exits before that creation branch. Other notifications retain their explicit evidence timestamp but remain unknown when prior coverage is absent, then use the unchanged bounded cold derivation. Matching existing known watermark behavior, version CAS, historical no-rollback, sixteen-lot selection, learning pending exclusion, quarantine and archived-corpus eligibility are unchanged. There is no schema/query redesign or evidence rewrite.

The new exact regression retains a COMPLETE zero-fee outcome, imports two superseding0.10 fees one second later, removes work/watermark, then notifies with the original older timestamp before recovery. The old cutoff retains1/0; restart and covered current cutoff produce fees0.20, acknowledge1/1, and select only the corrected compatible policy/feature-version metadata. Historical processing cannot roll it back. A normal-case control replays the identical claim before any send/economic change: new creation emits fresh initialization once, replay returnsNone without emitting it again. This validates actual claim/replay behavior, not a mocked grant of coverage.

## Exact validation

- Reviewer invariant RED:1 failed,5 deselected in3.89s. Its later characterization PASS demonstrates stale outcome/selector acceptance, not a repair. An earlier fixture import-path setup failure performed no test. Raw logs and final reviewer scratch bytes/hash are retained with their base-source provenance.
- Local exact RED before production edits:1 failed,6 deselected in3.28s at1/1. Exact added test bytes and base-source binding are in `red-provenance.json`.
- Focused fixed-source cutoff controls:7 passed in10.88s. Final EOL normalization preceded freeze and self-pin tests.
- Frozen gate:10 passed in17.25s, plus six CI checks, all exit0. Exact nodes/commands/log hashes are in `gate-selection.json`, `commands.json`, `logs.json`, `covering-result.json` and `ci-results.json`. Complete cutoff file7 plus the same three R1 idle-history, healthy/dirty-learning and notification-during-resolution CAS/revocation controls. Ruff src/tests and Linux/win32 mypy358 source files; Ruff and both mypy platforms over3 CI helpers. No larger suite, capacity, actual independent-role or heavy fitting run.

Eight final marker exports comprise six owned variants, the40-outcome archived corpus, and retained-history CAS/idle evidence. They are isolated synthetic accounting and metadata-selection controls, not model fitting, qualification/activation or natural PAPER trading. The raw frozen log is6591 bytes, SHA256 `842fb986ea22144922a67b725acef73b5cbde0016f10b838f5d8e3d736159ac6`. Six diagnostic/RED/focused/frozen logs retain original/raw/compressed hashes. Prior witnesses remain tied to their historical source versions rather than relabeled as current-source evidence.

## Exact prepared bytes and limits

Source419pins SHA256 `e86de9cac1e76098abb37eae5a5b74229b89955e4ce113bc1ef5a85706fccd4c`; archive SHA256 `a3a9659dd7f8ea4bc6eaa2fbbf5786dc8546be0fd63632d398f76f87334d48b4`. Changed executable files follow existing Windows CRLF checkout policy before freeze. Evidence bytes have nested -text attributes. Source remained unchanged throughout gate/CI and packaging.

Prepared release `2377ee8f1c8f00baa7c441e8eea56c257df201d86f704fa89ab802222ff35fc7`; unchanged config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`; schema36. Typed roundtrips and read-only release verification are PREPARED with zero objections. Predecessor3ddda517 correctly refuses only the two changed executable pins. Existing same-schema executable-binding protocol is unchanged; no actual binding transition, operating-store read/migration, acquisition, commission, resume, broker action, secret read, task registration/start, policy change, push or merge occurred.

Four static proposals point to this bundle's config/manifest through the existing launcher/hidden host. They remain disabled, trigger-free, LeastPrivilege and PT0S; functional roles retain submission/entry clamps, Verify is mutually exclusive and store/provider-free. Actual selected-token/host behavior was not exercised. Later separately approved verification must use this successor's exact paths:

```powershell
$Repo = 'C:\Users\leepa\.codex\worktrees\paper-core-m0\project_elrond'
$Evidence = Join-Path $Repo 'docs\evidence\paper-core\missing-work-recovery'
& "$Repo\.venv\Scripts\python.exe" -B -m quantbot.paper_core `
  --config "$Evidence\prepared-config.json" --manifest "$Evidence\prepared-release.json" --command verify
```

Operating backup/migration/writer fencing/current account/cost/owner readiness, natural broker-PAPER and actual daily learning/activation remain prior owner gates. Source/schema/learning weighting and scheduling are unchanged. Cold coverage remains conservatively delayed until retained genuine evidence availability is covered; pending/incomplete work is not forced complete.

`artifact-index.json` uses the existing files/export_provenance/historical_export_provenance/process_logs schema. Historical exports and process logs are empty; prior source-bound role/capacity proofs remain in older bundles. `prior-artifact-preservation.json` verifies84 residual-cutoff and180 final-repair artifacts plus both indexes against their exact commits, and123/83/77 M6 records unchanged. Root-owned owner documentation/progress is excluded from this focused commit.
