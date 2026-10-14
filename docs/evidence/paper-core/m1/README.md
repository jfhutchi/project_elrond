# M1 synthetic protected round trip

This is fixture-only engineering evidence, not deployed PAPER or profitability acceptance. `contracts.CoreConfig` permits only `SYNTHETIC_TEST`; the separate M0 `inventory.CoreConfig` remains an unchanged read-only manifest. The runtime boundary requires an explicit matching `SyntheticBroker` and offers no broker adapter factory, operational defaults or role launcher.

The authoritative implementation plan is [the PAPER-core plan](../../../superpowers/plans/2026-10-13-paper-core.md); root owns review status and next-ready handoff. This bundle implements only M1. Costs, ambiguous/partial/restart recovery, timing replacement, learning, activation and commissioning remain later milestones.

## Evidence

- [joined-trace.json](joined-trace.json): safe-runner-generated observation → eligible COLLECT decision → canonical entry → durable protection + guardian handoff → immutable owned fill → independent deadline exit → two terminal orders → certified owned-flat `COSTS_PENDING` round trip. Entry/exit are 5 shares at $100; gross P&L is $0, net P&L is null. The inherited 2 SPY shares remain untouched.
- [red.txt](red.txt): initial missing M1 path and actual two-pass legacy authority failure (second read silently became a baseline).
- [red-claim.txt](red-claim.txt), [red-basis.txt](red-basis.txt), [red-fixture.txt](red-fixture.txt), [red-replay.txt](red-replay.txt), [red-proof.txt](red-proof.txt): expected-red evidence for forged quantity, fabricated $100 basis, historical stop/adoption, replay after completed stop, and unsupported baseline/order provenance. `red-claim.txt` also includes an incorrect follow-up fixture size expectation; that expectation was corrected after checking the fixture's cash/equity scenario.
- [retained-initial.txt](retained-initial.txt): 253 passing and two named historical downgrade regression failures. Those tests created HEAD and encountered the new v30 refusal before the historical 0027 refusal. Their fixtures are now explicitly pinned to v29, preserving their original assertions; v30 refusal has its own test.
- [green-focused.txt](green-focused.txt): 257 tests passed in 75.89 seconds across M1 plus retained execution, deadline, protection, migration, storage and reconciliation checks. Final supplemental proof-provenance/serialization tests are recorded in [green-final-m1.txt](green-final-m1.txt).
- [ruff.txt](ruff.txt), [mypy.txt](mypy.txt): scoped lint and strict type validation. Root owns the single broad project-suite gate and independent review.

The subprocess test reconstructs the synthetic provider's saved state in a separate guardian process while a trader coroutine remains stalled. It opens the same fixture SQLite store and closes the owned lot through the production coordinator. The test verifies one additional sell and a completed owned-flat outcome. Stop/deadline tests also reopen the database, retain inherited inventory, verify fresh versus stale mark behavior, and prove replay does not add POSTs. These tests do not establish Windows service scheduling or healthy polling latency.

## Authority and migration

Migration 0030 is additive from current 0029. Normal database opening continues to refuse migration. Only temporary fixtures were migrated; manual pauses and legacy rows are preserved, SQLite integrity and online backup/restore are checked, and v30 refuses destructive downgrade. Reused protection accepts only snapshots affirmatively linked to validated core certifications. Legacy snapshots lacking proof remain unproven. Same-run historical reconciliation was deliberately not grandfathered because the legacy runner/recovery path can compare newly ingested raw cash against itself. Raw protection snapshots are still retained and marked; repeated mismatches cannot advance certified state.

Certification derives cash, aggregate quantity, per-owned-lot basis and fill/order watermark from the immutable synthetic initial baseline plus canonical owned fills. It checks actual quantities/cash/basis, the baseline ID, watermark and generation. Refused proofs remain durable; the certification generation does not advance. Marks are preserved rather than replaced with a fabricated price. The existing legacy mark-timestamp issue is outside this narrow repair and remains M3 work.

One writer transaction records decision, canonical intent, protection plan and position-slot reservation. The independent guardian module acknowledges that handoff before synthetic POST; the fixture harness invokes that acknowledgement in process for entry, then verifies independent process exit after the fill. Canonical entry identity is based on binding and observation, with a unique SQLite entry-observation claim. The existing `ExecutionCoordinator` alone handles broker submission/lifecycle and final manual-pause checking. Its existing account gates remain enabled using explicit nonsecret synthetic placeholders. Drawdown, veto, clock/session and initial ownership evidence are fixture assumptions; they cannot authorize a real broker.

Rollback of this uncommissioned work is a source revert in the isolated branch. Retain evidence and temporary backup copies. Do not restore a pre-trade database over newer broker events. A deployed v30 store would require compatible readers/protection or forward repair, not destructive downgrade; no operational migration/deployment is claimed here.

## Reproduction

Use the prepared offline interpreter and existing credential-stripping/network-restricting runner, from the isolated worktree:

```powershell
.venv/Scripts/python.exe .superpowers/sdd/2026-10-13-paper-core/check.py tests/integration/paper_core/test_thin_loop.py tests/integration/paper_core/test_migration.py tests/fault/paper_core/test_protection_authority.py tests/unit/operations/test_position_protection_supervisor.py tests/integration/test_execution_coordinator.py tests/unit/learner/test_paper_deadlines.py tests/unit/learner/test_deadline_fill_sync.py tests/fault/test_deadline_presend_refusal.py tests/unit/test_migration_authority.py tests/unit/test_storage.py tests/unit/execution
.venv/Scripts/python.exe .superpowers/sdd/2026-10-13-paper-core/check.py tests/integration/paper_core/test_thin_loop.py -k export_joined -s
```

The second command emits `M1_JSON:` followed by the joined export in the runner's saved log; `joined-trace.json` is that JSON document. No credentials, broker calls, deployed task/process changes, security changes, stop clearance, operational migration, paid CI or model fitting were performed.
