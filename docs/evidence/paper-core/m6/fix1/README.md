# M6 repair 1 — PREPARED / WAITING

This isolated repair addresses the three accepted independent review findings at base `2fd17e18aa860a47bc35c1ba96d4ecc78bcfd202`. It does not commission or deploy anything. Broker state remains NOT_PROBED. Original M6 evidence, failed stages and manifest remain unchanged; this directory supersedes their executable proposal, rather than replacing their history.

The proposed release is `1e39ceb94155aaa746a879b69306ca3150a182c19d491a139cd81c8a1a58e302`, with unchanged input config `8c95e278bc674c64557bb0485957d1b6666546e1d67a20e43d30d2079b085c8c`. Typed JSON reload and exact executable inventory verification succeed. The earlier `0c55ea726d4bfe6cf390379789d96527e89450f3b24a8c286e9b147926d08f9e` manifest correctly refuses the nine repaired executable files. No existing operational commission has been transferred; executable/owner-generation transfer remains unsupported.

Guardian acknowledgements use each verified typed durable plan ID. At locked claim and send, a newer certified proof may succeed only when both proofs remain complete/current/unrevoked and the economic ledger is unchanged. Its optional fingerprint covers canonical order status/quantities, fills, external owner/cash movements and actual cost revisions; it excludes receipt times, observation documents and market marks. The fingerprint is rechecked against the current ledger under the same writer lock. Original decision/model/time/plan/owner binding remains immutable. Current equity, buying power, cash and risk capacity are checked against the latest proof, so ordinary mark refresh can succeed while reduced capacity or changed economics refuses. Legacy proofs without a fingerprint retain their original IDs and cannot authorize a successor.

Late nonzero FEE/CFEE activity durably contradicts the exact account/contract. A finite snapshot bounds indexed retained-cost scans, independently of the two-day provider overlap. Only retained owned fills to which that exact contract applies receive UNKNOWN revisions; aggregate fees are never allocated. Immutable raw acquisition documents and the first contradiction remain preserved. A restartable pending marker prevents affected learning/materialization/activation until source revisions and derived outcome revisions both catch up. Guardian/outcomes and an otherwise valid current entry model remain independent. Date-only fee activity retains cost uncertainty and UNKNOWN cash timing without inventing an intraday boundary.

The final flat-start witness uses three independent OS role processes and a separate credential-free provider journal. The trader creates a new decision/intent, the guardian acknowledges the exact plan, and actual fixture POST receipts show exactly one BUY and one SELL by the distinct trader/guardian PIDs. Importers ingest original provider IDs into the consumer store, protection exits owned quantity and outcomes progress. This SYNTHETIC_TEST witness and the separate seeded-owned restart witness do not establish natural broker-PAPER acceptance or profitability.

The complete ordered handover and forward-safe recovery remain in [the original runbook](../README.md). Every future command and proposed task action must use the superseding files below. All rollout commands remain **unexecuted and require separate explicit approval** after scoped re-review and whole-branch review:

```powershell
$Repo = 'C:\Users\leepa\.codex\worktrees\paper-core-m0\project_elrond'
$Evidence = Join-Path $Repo 'docs\evidence\paper-core\m6\fix1'
$Python = Join-Path $Repo '.venv\Scripts\python.exe'
$Config = Join-Path $Evidence 'prepared-config.json'
$Manifest = Join-Path $Evidence 'prepared-release.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command verify
# Only after the separately approved backup/compatibility/token/writer-fence sequence:
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command migrate
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command acquire --approval-document 'D:\Elrond\review\m6-acquisition-request.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command commission --approval-document 'D:\Elrond\review\m6-commission.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --command status
& $Python -B -m quantbot.paper_core --config $Config --command export --decision-id 'ACTUAL_DURABLE_DECISION_ID'
# Approved forward recovery only; preserves owned risk reduction and all later events:
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command fence --approval-document 'D:\Elrond\review\m6-fence.approved.json'
& $Python -B -m quantbot.paper_core --config $Config --manifest $Manifest --command rollback-model --approval-document 'D:\Elrond\review\m6-model-rollback.approved.json'
```

Three updated XML proposals point to these exact files and remain disabled, submission-disabled, LeastPrivilege, trigger-free and PT0S continuous lifetime. They are not registered or started. Decode archived/proposed XML explicitly as UTF-8 text. Selected leepa Limited token, supported policy/path/job tree and fresh owner handover remain unverified; shell identity is not task-token proof. Keep legacy Protection/Deadline and their nine lots under the original adaptive owner. No adoption, flattening or stop clearing is authorized.

Initial COLLECT, DAILY_BOUNDED_V2, fixed risk limits, ten completed sessions, ALL eligible history and fixed 2x latest ten remain unchanged. Proposed first learning session is 2026-10-15; a later handover requires an updated, independently reviewed release/config. Fresh broker/account/calendar/clock/current cash/component applicability, canonical metadata, retained-owner provenance, tighter controls, approved backups/migration/commission, atomic writer fencing and natural protected PAPER roundtrip plus later eligible learned activation remain named prerequisites. Date-only cash ambiguity needs separately proven provider timing/correction; no residual rebaseline resolves it.

Preserve schema35 and all later orders/fills/client IDs in recovery. Never restore an old DB over new activity or use older executable code after migration. The optional economic fingerprint introduces no schema migration and does not rewrite historical proofs. Existing current-cost and economic-ledger reads retain prior full-history behavior; the new fee backfill itself is bounded by indexed keyset batches and a finite watermark. No new operational action, external service or dependency is part of this repair.
