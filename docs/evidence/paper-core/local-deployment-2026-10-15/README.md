# Merged successor installed on the HP Omen

[PR 290](https://github.com/jfhutchi/project_elrond/pull/290) merged to main at `2026-10-15T10:12:31Z` (06:12 Eastern), commit `14352cc428dcd999750138d52fbd5f30347991a1`. The approved signal-first/exploratory selection and V3 all-history gross learning are now merged. The preceding [source review and test package](../daily-exploration/README.md) is a sealed pre-merge snapshot and remains unchanged.

## What is installed

The merged source was exported into a new isolated release at `D:\Elrond\releases\paper-core-14352cc-20261015`. Locked dependencies were installed into its own native Python 3.11.16 environment; no running environment was changed. The four existing Paper Core task definitions now use this release and its separate configuration directory, `D:\Elrond\config\paper-core-14352cc-20261015`.

| Installed identity | Value |
| --- | --- |
| Config | `620d31ec42915eee76779a972521428adaeb9c0a5536694422d8c27e2b6acebd` |
| Release | `0734974b39088b449abe1279a385c89202756b61f3420aa910d6b2d772a46d4c` |
| Exact pins | 366 executable files, 46 installed distributions, interpreter and base runtime |
| Direct prepare/verify | Exit 0, PREPARED, no objections |
| Actual task-identity verification | Exit 0 at `2026-10-15T10:23:28Z`; `leepa / Interactive / Limited` |
| Functional state | Guardian, outcomes and trader disabled, trigger-free and clamped; uncommissioned |

The verifier-only task ran once and was disabled again. It verified the exact installed source, dependencies and interpreter without opening the operating database/env file or probing the broker. All 19 legacy task definitions remain unchanged, including existing protection/deadline services. Task state is not a fresh proof of protection health.

## Why trading activation is still waiting

The GET-only broker capture at `2026-10-15T10:15:09Z` (06:15 Eastern) confirms the original IWM market DAY SELL of 0.169669 shares is still accepted with zero filled. Client `qb-595f587acd1b769f4bd0d59282e631ad14ecb9f7`, broker order `28523181-cd04-4f74-bd67-bcebe4582164`. The broker reports the next opening at `2026-10-15T13:30:00Z` (09:30 Eastern). The order was neither cancelled nor replaced.

The operating store is still schema 29, identity `cd2a5911-853a-484e-bbc8-d0c27b5d27aa`, with no core commission. The new code requires schema 36. Starting it now would fail its schema/commission checks; migrating the store without a compatible legacy protection transition would leave old executables unable to restart against it. Nine inherited positions retain their original ownership. The manual entry stop is disengaged and was not changed.

These are handover prerequisites, not a confidence gate on paper exploration. They do not prevent installation or source merge. Once the original exit becomes terminal, reconcile it through its original IDs, refresh ownership/open-order evidence, establish a compatible protection/deadline transition, fence old entry writers and take a fresh WAL-aware backup before the reviewed migration and commissioning. Reverify the actual session/learning origin and exact pins. Start independent guardian/outcomes and establish readiness before enabling the trader. Natural PAPER execution and learning remain acceptance observations after that start, not proof already supplied by installation.

## Evidence and installation qualifications

- [Fresh broker GET evidence](readonly-broker-preflight.json) and [query-only store evidence](readonly-store-preflight.json).
- [Source/export provenance](install-provenance.json), [installed preparation commands](installed-preparation.json), [exact config](runtime-config.json), [manifest](runtime-manifest.json), and [direct verification](installed-verification.json).
- [Registered task results](installed-tasks.json), [actual task verification](selected-token-verification.json), and its [new log segment](selected-token-verifier.log).
- Post-install [query-only store check](post-install-store.json) and [task/process check](post-install-tasks.json): schema/identity/manual stop unchanged, no core commission, four disabled tasks and zero functional core processes.

The initial strict comparison correctly refused native Git export line endings that differed from the reviewed worktree bytes. Full comparison then found 353 exact matches and 13 CRLF/LF-only differences across the 366 executable inputs; no code difference was accepted. The distinct installed manifest binds every actual installed byte. The original manifests were not rewritten to conceal the difference. `uv sync --locked` succeeded; its cross-volume hardlink warning resulted in successful full-copy installation. No application suite or heavy fitting case was rerun for an installation with no code change.

No operating migration, commission, functional core-role startup, new broker order, inherited-order edit, legacy writer fence, manual-stop clearing or paid CI was performed. The source merge did not start the new trader. The source/preparation and older deployment bundles remain sealed historical evidence. The new local release and pre-change task XML backups remain under `D:\Elrond`; no credential value or env-file contents are included here.
